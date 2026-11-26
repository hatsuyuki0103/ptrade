# -*- coding: utf-8 -*-
"""v17 判据：交易记录面板（逐笔流水）。

真源：`.omx/specs/deep-interview-ptrade-v17.md` · `.omx/plans/prd-ptrade-v17.md`

用户原话：「我还想要个面板，看我的交易记录，哪一天哪个价位买的，和哪一天哪个价位卖的」
用户裁决：持仓卡内 tab · **逐笔流水** · 被拒委托默认隐藏

★ 本文件的分工：
* **V17-1..8**：真调 `WebApp.dispatch("GET", "/api/ledger", ...)`（in-process，不占端口）
* **V17-9..11、V17-13**：契约级（只读性 / 页面结构 / 幂等 / 不用 ds）—— **永远跑**
* **V17-12**：真 Chrome（CDP）—— INV-66b 回归（4 档视口默认态不滚动）
  ★ 浮层用 `position:fixed`（**不是** absolute）：absolute 的 containing block 若同时是
  滚动容器（`.info-col` 有 `overflow-y:auto`），其溢出**仍计入** `scrollHeight`
  ⇒ 实测 1920×1080 点开后 986 → 1116 ⇒ 破 INV-66b ✗（本次实测发现并改掉）
"""

from __future__ import annotations

import inspect
import json
import re
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ptrade import webapp as webapp_mod  # noqa: E402
from ptrade.config import load_config  # noqa: E402

# --------------------------------------------------------------------------- #
# 固定台账（确定性；覆盖 filled / rejected / 多标的 / 同日多笔）
# --------------------------------------------------------------------------- #
def _row(order_id, trade_date, code, name, side, qty, price, status="filled",
         realized=0.0, err="OK"):
    return {
        "order_id": order_id, "ts": trade_date + "T10:00:00", "trade_date": trade_date,
        "code": code, "name": name, "side": side, "qty": qty, "price": price,
        "status": status, "error_code": err, "reason": "", "realized_pnl": realized,
        "fee": 1.0, "tax": 0.0, "transfer_fee": 0.0, "cash_delta": 0.0,
        "debt_delta": 0.0, "margin_used": 0.0,
        "price_source": "request", "price_asof": trade_date, "price_fetched_at": "",
        "source": "test",
    }


FIXTURE_ROWS = [
    _row("O0001", "2026-11-03", "600594", "益佰制药", "short_sell", 100, 3.19),
    _row("O0002", "2026-11-09", "300869", "康泰医学", "short_sell", 200, 9.81),
    _row("O0003", "2026-11-25", "300869", "康泰医学", "buy_to_cover", 200, 8.36, realized=290.0),
    _row("O0004", "2026-11-25", "300869", "康泰医学", "buy_to_cover", 200, 0.0,
         status="rejected", err="NO_MARKET_DATA"),
    _row("O0005", "2026-11-09", "601238", "广汽集团", "short_sell", 300, 4.92),
    _row("O0006", "2026-11-25", "601238", "广汽集团", "buy_to_cover", 300, 4.43, realized=147.0),
]


def _write_ledger(root: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "ledger.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return p


def _make_app(tmp_path: pathlib.Path, rows: list[dict] | None = None) -> webapp_mod.WebApp:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    if rows is not None:
        _write_ledger(root, rows)
    cfg = load_config(str(root))
    return webapp_mod.WebApp(cfg, offline=True, lock_timeout=2.0)


def _ledger(app, **query) -> dict:
    """调 `dispatch`，返回信封里的 `data`。★ query 的值按 parse_qs 惯例给 **list**。"""
    q = {k: ([str(v)] if not isinstance(v, (list, tuple)) else [str(x) for x in v])
         for k, v in query.items()}
    resp = app.dispatch("GET", "/api/ledger", q, None)
    assert resp.status == 200, f"/api/ledger 返回 {resp.status}"
    payload = resp.payload or {}
    assert payload.get("ok") is True, f"ok 不为 True：{payload.get('error')}"
    assert payload.get("command") == "ledger", f"command 错：{payload.get('command')}"
    return payload["data"]


# --------------------------------------------------------------------------- #
# V17-1 … V17-8：端点行为（真调 dispatch）
# --------------------------------------------------------------------------- #
def test_v17_1_envelope_and_fields(tmp_path):
    """★ V17-1：五要素齐全 + data 里 5 个字段都在。"""
    app = _make_app(tmp_path, FIXTURE_ROWS)
    d = _ledger(app)
    for key in ("orders", "count", "total_lines", "include_rejected", "limit"):
        assert key in d, f"data 缺字段 {key}：{sorted(d)}"
    assert isinstance(d["orders"], list) and d["orders"]
    assert d["include_rejected"] is False
    assert d["limit"] == 50


def test_v17_2_default_excludes_rejected(tmp_path):
    """★★ V17-2：默认**过滤**被拒委托（用户裁决：默认隐藏）。

    台账 6 行里 1 条 `rejected` ⇒ 默认必须只剩 5 条。
    ★ 这一条直接对应线上台账里那 3 条 `price=0.0` 的 NO_MARKET_DATA 失败单。
    """
    app = _make_app(tmp_path, FIXTURE_ROWS)
    d = _ledger(app)
    assert d["total_lines"] == 5, f"默认应 5 笔（6 − 1 被拒），实际 {d['total_lines']}"
    assert not any(o.get("status") == "rejected" for o in d["orders"])


def test_v17_3_include_rejected(tmp_path):
    """★ V17-3：`include_rejected=1` ⇒ 被拒行重新出现。"""
    app = _make_app(tmp_path, FIXTURE_ROWS)
    d = _ledger(app, include_rejected="1", limit="100")
    assert d["include_rejected"] is True
    assert d["total_lines"] == 6
    rej = [o for o in d["orders"] if o.get("status") == "rejected"]
    assert len(rej) == 1 and rej[0]["error_code"] == "NO_MARKET_DATA"


def test_v17_4_limit_clamped(tmp_path):
    """★ V17-4：`limit` 钳到 [1, 500]，非法值回落默认 50（查询串来自浏览器，不可信）。"""
    app = _make_app(tmp_path, FIXTURE_ROWS)
    assert _ledger(app, limit="0")["limit"] == 1
    assert _ledger(app, limit="10000")["limit"] == 500
    assert _ledger(app, limit="abc")["limit"] == 50
    d3 = _ledger(app, limit="3")
    assert d3["limit"] == 3 and d3["count"] == 3 and len(d3["orders"]) == 3


def test_v17_5_sorted_latest_first(tmp_path):
    """★ V17-5：排序 = 最新在上（`trade_date` 降序，同日按 `order_id` 降序）。"""
    app = _make_app(tmp_path, FIXTURE_ROWS)
    orders = _ledger(app, include_rejected="1", limit="100")["orders"]
    keys = [(o["trade_date"], o["order_id"]) for o in orders]
    assert keys == sorted(keys, reverse=True), f"顺序不对：{keys}"


def test_v17_6_code_filter(tmp_path):
    """★ V17-6：`code` 过滤生效。"""
    app = _make_app(tmp_path, FIXTURE_ROWS)
    d = _ledger(app, code="600594")
    assert d["total_lines"] == 1 and d["orders"][0]["code"] == "600594"
    d2 = _ledger(app, code="600594", include_rejected="1")
    assert d2["total_lines"] == 1, "600594 没有被拒单，含已拒时仍是 1 笔"


def test_v17_7_missing_ledger_is_empty_not_error(tmp_path):
    """★ V17-7：`data/ledger.jsonl` **不存在** ⇒ `orders: []` 且 `ok=true`（空台账合法）。"""
    app = _make_app(tmp_path, rows=None)          # 故意不写台账
    d = _ledger(app)
    assert d["orders"] == [] and d["count"] == 0 and d["total_lines"] == 0


def test_v17_8b_invalid_utf8_does_not_break_endpoint(tmp_path):
    """★★ V17-8b（code-review B2 / ultraqa F2 **两道闸独立命中**）：台账含**非法 UTF-8 字节** ⇒ 仍 200。

    ★ 现实可达：ledger 是 append-only，**突然断电/被杀进程**会在多字节字符中间撕裂写入，
      留下 `\xff\xfe` 这类字节。严格解码时 `UnicodeDecodeError` 在**行迭代器**里抛出
      （在行级 try 之外）⇒ 冒到路由层变 400/500 ⇒ 整个交易记录面板「加载失败」✗
    """
    app = _make_app(tmp_path, FIXTURE_ROWS)
    led = pathlib.Path(app.cfg.root) / "data" / "ledger.jsonl"
    with led.open("ab") as fh:                      # ★ 二进制追加：写入非法 UTF-8
        fh.write(b'{"order_id":"BAD","name":"\xff\xfe"}\n')
    resp = app.dispatch("GET", "/api/ledger", {}, None)
    assert resp.status == 200, f"非法字节把端点打成了 {resp.status}"
    d = (resp.payload or {}).get("data") or {}
    # ★ 修复后**比原计划更好**：errors="replace" 把非法字节换成 \ufffd ⇒ 该行变成合法 JSON
    #   **被保留**（不丢数据），而不是靠「丢弃整行」来保证不崩。
    #   ⇒ 判据改为：① 端点 200；② **5 笔已知好行全部在**；③ 坏字节行不导致任何异常。
    ids = {o.get("order_id") for o in (d.get("orders") or [])}
    for expect in ("O0001", "O0002", "O0003", "O0005", "O0006"):
        assert expect in ids, f"好行 {expect} 丢了：{sorted(ids)}"
    assert d.get("total_lines") >= 5, f"好行应照常返回，实际 total_lines={d.get('total_lines')}"


def test_v17_8c_bom_does_not_drop_first_row(tmp_path):
    """★ V17-8c（ultraqa FL1）：台账带 **BOM** 时**首条合法记录不得被丢**（`utf-8-sig`）。"""
    root = tmp_path / "root_bom"
    root.mkdir(parents=True, exist_ok=True)
    led = root / "data" / "ledger.jsonl"
    led.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in FIXTURE_ROWS) + "\n"
    led.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))   # ★ UTF-8 BOM + 全部合法行
    app = webapp_mod.WebApp(load_config(str(root)), offline=True, lock_timeout=2.0)
    d = _ledger(app)
    assert d["total_lines"] == 5, f"BOM 丢了首条：应 5 笔 filled，实际 {d['total_lines']}"


def test_v17_8_bad_lines_skipped(tmp_path):
    """★ V17-8：台账含**坏行**（非 JSON / 非对象）⇒ 跳过坏行、其余正常返回（不 500）。"""
    app = _make_app(tmp_path, FIXTURE_ROWS)
    led = pathlib.Path(app.cfg.root) / "data" / "ledger.jsonl"
    with led.open("a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
        fh.write("[1,2,3]\n")             # 合法 JSON 但不是 dict
        fh.write("\n")                     # 空行
    d = _ledger(app)
    assert d["total_lines"] == 5, f"坏行应被跳过，仍应 5 笔 filled，实际 {d['total_lines']}"


# --------------------------------------------------------------------------- #
# V17-9 … V17-11：契约（只读性 / 页面结构 / 不用 ds）
# --------------------------------------------------------------------------- #
def test_v17_9_ledger_endpoint_does_not_touch_state(tmp_path):
    """★ V17-9：`/api/ledger` **只读**，不得改写 `state.json`。

    ★ 用 mtime + 字节内容双重校验：端点调用前后 state 文件必须**完全不变**。
    （避免「顺手把 state 也写了」这类越界；INV-40 的读写边界。）
    """
    app = _make_app(tmp_path, FIXTURE_ROWS)
    st = pathlib.Path(app.cfg.root) / "data" / "state.json"
    st.write_text('{"state": {"trade_date": "2026-11-25"}}', encoding="utf-8")
    before_bytes = st.read_bytes()
    before_mtime = st.stat().st_mtime_ns
    _ledger(app)
    _ledger(app, include_rejected="1")
    assert st.read_bytes() == before_bytes, "state.json 内容被改了 ✗"
    assert st.stat().st_mtime_ns == before_mtime, "state.json 被重写（mtime 变了）✗"


def test_v17_10_page_structure_contract():
    """★ V17-10：页面含 tab 与浮层；浮层**不是** card/section/article（否则破 `infoCards` 判据）。"""
    with __import__("contextlib").suppress(Exception):
        from ptrade.webui import APP_HTML as html
    html = html  # noqa: F821
    for must in ('id="tab-positions"', 'id="tab-trades"', 'id="trades-pop"',
                 'id="trades-wrap"', 'id="trades-include-rejected"', 'id="trades-close"'):
        assert must in html, f"页面缺 {must}"
    # 浮层标签必须是 div（不是 .card / section / article）
    assert '<div class="trades-pop" id="trades-pop"' in html, (
        "浮层必须是 <div> —— test_v4_l5 的 infoCards 判据取 '.card, section, article'，被当成卡片会触发重叠断言"
    )
    # 默认隐藏（0px）
    assert 'data-open="0"' in html and 'aria-hidden="true"' in html
    # ★★ code-review 阻断1：以下结构断言原在 CDP-gated 的 V17-12 里（无 Chrome 就一条不跑 ⇒ 空守卫），
    #   已**移到本测试**（永远跑）。改用**正则**而不是精确子串，避免 CSS 属性重排就假红。
    assert re.search(r"\.trades-pop\{[^}]*position:fixed", html), (
        "浮层必须 position:fixed —— absolute 会因 containing block 同时是滚动容器而计入 "
        ".info-col 的 scrollHeight（实测 1920×1080 撑到 1116 > 986）"
    )
    assert re.search(r'\.trades-pop\[data-open="1"\]\{[^}]*display:flex', html), (
        "打开态必须 display:flex（默认 display:none 才能保证 0px）"
    )
    assert re.search(r"pop\.style\.left\s*=\s*Math\.round\(r\.left", html), (
        "positionTrades 必须按 .info-col 的 rect 给 fixed 浮层设显式尺寸"
    )
    assert "function positionTrades(" in html and "function openTrades(" in html
    # ★ code-review 建议1：浮层必须**在 <body> 下、不在 .info-col 内** ——
    #   否则将来右列祖先加了 transform/filter/contain，fixed 会静默退化 ⇒ 0px 回归且判据抓不到
    idx_pop = html.find('id="trades-pop"')
    idx_main_end = html.find("</main>")
    assert idx_main_end != -1 and idx_pop > idx_main_end, (
        "浮层必须在 </main> 之后（与 .info-col 平级），不得留在 .info-col 内部"
    )
    # ★ ultraqa F1：bindTradesPanel 必须幂等（否则匿名闭包被绑两次 ⇒ 勾选双请求）
    assert "if (TRADES_STATE.bound) { return; }" in html, "bindTradesPanel 缺幂等 guard"
    # ★ code-review 建议5 + F3：并发去重**但不得丢最后一次请求**（pending 补拉机制）
    assert "if (TRADES_STATE.loading) { TRADES_STATE.pending = true; return; }" in html, (
        "loadTrades 缺并发去重（或去重实现没有 pending 补拉 —— 那会丢掉加载期间的勾选变化，见 F3）"
    )
    assert "if (TRADES_STATE.pending) { TRADES_STATE.pending = false; loadTrades(); }" in html, (
        "loadTrades 缺 pending 补拉：飞行期间的请求必须在本轮完成后补发一次"
    )
    # JS 入口与渲染函数在位
    for fn in ("function renderTrades(", "function loadTrades(", "function openTrades(",
               "function closeTrades(", "function bindTradesPanel("):
        assert fn in html, f"缺 {fn}"
    assert 'ledger: "/api/ledger"' in html, "API 表缺 ledger 入口"


def test_v17_11_api_ledger_does_not_use_datasource():
    """★ V17-11：`api_ledger` **不得**调用 `_ds()`/`DataSource`。

    ★ 这是性能与正确性双重要求：读台账是纯文件操作，一旦引 ds 就有
      「锁内取价 ⇒ INV-42」的风险（v12/v13 的 `close` 正是这么被拒的）。
    """
    src = inspect.getsource(webapp_mod.WebApp.api_ledger)
    assert "_ds(" not in src, f"api_ledger 不应调用 _ds()：{src[:200]}"
    assert "DataSource(" not in src, "api_ledger 不应构造 DataSource"
    # 反向确认：它确实读的是 service.read_ledger_rows（单一真源）
    assert "read_ledger_rows" in src, "api_ledger 应走 service.read_ledger_rows（单一读法）"


# --------------------------------------------------------------------------- #
# V17-12：★★ INV-66b 回归（真 Chrome）—— 默认 + 点开浮层 **两种状态**都不许滚动
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("vw,vh", [(1600, 900), (1920, 945), (1920, 1080), (2560, 1440)])
def test_v17_12_right_column_no_scroll_default_and_opened(tmp_path, vw, vh):
    """★★ V17-12：右列 4 档视口**都不许滚动** —— 默认状态 **与** 点开交易记录后。

    ★ 为什么必须验「点开后」：右列余量实测 = **0px**，交易记录 9 行比持仓 0 行高得多；
      若浮层参与 `.info-col` 的布局就会把右列撑高 ⇒ `scrollH > clientH` ⇒ 破 INV-66b ✗
    ★ 实现用 **`position:fixed`** + 默认 `display:none` + **浮层挂在 `<body>` 下**
      （绝对定位不够：absolute 的 containing block 若同时是滚动容器，溢出仍计入 scrollHeight，
       实测 1920×1080 点开后 986 → 1116）。
    ★★ 「点开后」用**原子单帧**测量（`ATOMIC_JS`：同一次 evaluate 内 量→开→量），
      两次读之间没有事件循环间隙 ⇒ analysis 轮询/自动展开插不进来。
    """
    try:
        from tests import _cdp
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"_cdp 不可用：{exc}")
    if not _cdp.status(probe=True).get("available"):
        pytest.skip("Chrome/CDP 不可用（如实跳过，不静默降级）")

    app = _make_app(tmp_path, FIXTURE_ROWS)
    # ★ 必须用**带夹具**的 root：CDP 的 `measure()` 会点「加载图表（日线）」并**等待图表首帧**，
    #   空 root 没有 `daily_*` 夹具 ⇒ 30s 超时 ⇒ CDPUnavailable ✗
    #   ⇒ 复用 `test_ui_layout_v4._build_root(0)`（同一个夹具构造器，不复制知识）。
    from tests.test_ui_layout_v4 import _build_root

    chart_root = _build_root(0)
    # ★ 把确定性台账也放进 chart_root：这样「打开交易记录」时浮层里**真有内容**（9→6 行），
    #   才能真正验「浮层有内容但不撑高右列」这个关键点。
    _write_ledger(chart_root, FIXTURE_ROWS)
    chart_cfg = load_config(str(chart_root))
    # ★★★ 原子单帧测量（code-review 阻断1 给的方案）：在**同一次 `Runtime.evaluate`** 里
    #   「量 → 开 → 再量」，两次读之间**没有事件循环间隙** ⇒ analysis 轮询/自动展开**插不进来**
    #   ⇒ 我原来被污染的三个源（measure 副作用 / analysis 展开 / 两次独立测量对照）全部消失。
    ATOMIC_JS = r"""
    (() => {
      const col = document.querySelector('.info-col');
      if (!col) return JSON.stringify({err: 'no-info-col'});
      const a = document.getElementById('analysis-details');
      if (a) a.open = false;                       // 固定混淆变量：先折叠分析卡
      const before = col.scrollHeight;
      if (typeof window.openTrades === 'function') { window.openTrades(); }
      else { document.getElementById('tab-trades').click(); }
      const after = col.scrollHeight;
      const pop = document.getElementById('trades-pop');
      const cs = getComputedStyle(pop);
      const pr = pop.getBoundingClientRect();
      return JSON.stringify({
        before: before, after: after, delta: after - before,
        pos: cs.position, disp: cs.display,
        w: Math.round(pr.width), h: Math.round(pr.height),
        inView: pr.left >= -1 && pr.top >= -1
                && pr.right <= innerWidth + 1 && pr.bottom <= innerHeight + 1,
      });
    })()
    """

    with _cdp.serve(chart_cfg, offline=True) as base:
        sess = _cdp._session()
        # ① INV-66b 主判据（与 test_ui_layout_v4 同口径）：默认状态右列不得滚动。
        m_ctrl = sess.measure((vw, vh), url=base)
        assert m_ctrl["infoColScrollH"] <= m_ctrl["infoColClientH"] + 1, (
            f"{vw}x{vh} 默认状态右列滚动：{m_ctrl['infoColScrollH']} > {m_ctrl['infoColClientH']}"
        )
        # ② ★★ 点开浮层：**原子单帧**对比 scrollHeight（before/after）
        m_atomic = sess.measure((vw, vh), url=base, extra_eval=ATOMIC_JS)
        raw = (m_atomic.get("extraEval") or m_atomic.get("extra_eval") or "")
        diag = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
        assert diag.get("err") is None, f"{vw}x{vh} 原子测量失败：{raw}"
        assert diag["after"] <= diag["before"] + 1, (
            f"{vw}x{vh} 点开浮层后**右列 scrollHeight 被撑高**：{diag['before']} → {diag['after']}"
            f"（delta {diag['delta']}）—— 浮层必须脱离右列布局（position:fixed + 不在 .info-col 内）"
        )
        # ③ 顺带验浮层的**几何**（fixed 定位真的生效、在视口内、非 0 尺寸）
        assert diag.get("pos") == "fixed", f"{vw}x{vh} 浮层 position={diag.get('pos')}（应 fixed）"
        assert diag.get("disp") == "flex", f"{vw}x{vh} 打开态 display={diag.get('disp')}（应 flex）"
        assert diag.get("w", 0) > 0 and diag.get("h", 0) > 0, f"{vw}x{vh} 浮层尺寸为 0：{diag}"
        assert diag.get("inView") is True, f"{vw}x{vh} 浮层超出视口：{diag}"
        # ★ 结构契约（fixed / display:none / 显式尺寸 / 浮层在 body 下 / 幂等 / 去重）
        #   已**统一放在 `test_v17_10_page_structure_contract`**（永远跑，不受 CDP 可用性影响）
        #   —— 这里不再重复断言，避免「两份来源」漂移（code-review LOW-1）。
        # ★ code-review 要求：无 Chrome 时结构断言也必须跑 ⇒ 已归 V17-10 ✓
