"""v10 判据：分数按交易日刷新（A1/A3/M1）+ 账户实时盈亏（B1/B3/B5/M2）。

真源：`.omx/plans/prd-ptrade-v10-r2.md`（合并 Architect 8 条 + Critic 3 条）

## 评审要求写进判据的关键点
* **B1**：`live_state_summary` 必须用 `core.mark_to_market → core.refresh_risk_flags → state_summary`
  ★ **不得**用 `mark_positions_live` 拼 state（它返回 dict 行，会字段错位）
* **B3**：`/api/state` **不得**出现 `mark_positions_live` / `live_state_summary`（INV-40）
* **B7**：风险指标必须跟随实时价变化
* **B5**：`as_of` / `source` 必须存在；前端有 `data-testid="live-pnl-source"`
* **A1**：`ok=False` **永不**进「同交易日永久」分支（失败仍按 120s）
* **A3**：前端有 `data-testid="analysis-asof"`，**不靠文本扫描**判状态
* **M1**：`api_watchlist_score` 必须把结果**写回** `analysis_<code>_<asof>` 缓存（两条路径共用）
* **M2**：`refreshState()` 必须读 `API.live_state`
"""

from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from ptrade import chart as chart_mod
from ptrade import service
from ptrade import webapp as webapp_mod
from ptrade import webui as webui_mod

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── 假 DS：`_spot_frame_raw()` 返回 `(DataFrame, asof)`（真实签名，见 datasource.py:1252）
class _FakeDS:
    def __init__(self, price: float | None = 3.16, asof: str = "2026-11-09"):
        self._price = price
        self._asof = asof

    def _spot_frame_raw(self):  # noqa: SLF001
        import pandas as pd

        if self._price is None:
            return pd.DataFrame(columns=["code", "last"]), ""
        return pd.DataFrame([{"code": "600594", "last": self._price}]), self._asof


class _FakeCfg:
    class root:
        pass

    class risk:  # noqa: D106
        pass

    class margin:  # noqa: D106
        pass


# ── B1 · live_state_summary 用实时价重算（含 total_pnl）
def test_v10_b1_live_state_recomputes_total_pnl():
    """V10-B1：`live_state_summary` 必须用**实时价**重算 `total_pnl` 与各仓 `short_pnl`。"""
    from ptrade import store

    cfg = ROOT.parent  # 不真正读盘：用 store.read_state 会在没账户时返回 None
    # 构造一个最小 state（用 store.read_state 的真实现太重 ⇒ 用 core.AccountState）
    from ptrade import core

    st = core.AccountState()
    st.trade_date = "2026-11-09"
    st.initial_cash = 500000.0
    st.available_cash = 499961.9
    st.frozen_collateral = 50000.0
    pos = core.Position(code="600594", name="益佰制药")
    pos.short_qty = 15700
    pos.short_cost = 3.19
    pos.last_price = 3.19
    st.positions = {"600594": pos}

    from ptrade.config import load_config

    real_cfg = load_config(str(ROOT))
    data, source, as_of = service.live_state_summary(real_cfg, _FakeDS(3.16), st)
    assert source == "live", f"应走实时路径，实际 {source}"
    assert as_of == "2026-11-09"
    p = data["positions"][0]
    assert p["last_price"] == pytest.approx(3.16), f"实时价未生效：{p['last_price']}"
    assert p["short_pnl"] == pytest.approx((3.19 - 3.16) * 15700, abs=0.01), (
        f"空头浮盈应为 +471，实际 {p['short_pnl']}"
    )
    # ★ 关键：total_pnl 也必须变（v9 只重算 positions，total_pnl 还是旧值 ✗）
    base = service.state_summary(st)
    assert data["total_pnl"] != base["total_pnl"], "total_pnl 未按实时价重算 ✗"


# ── B4 · 取不到价 ⇒ 整份降级 source=state（不编造）
def test_v10_b4_falls_back_when_no_prices():
    """V10-B4：`_spot_frame_raw` 给空 ⇒ `source="state"` 且数值 == `state_summary`。"""
    from ptrade import core

    st = core.AccountState()
    st.trade_date = "2026-11-09"
    st.available_cash = 499961.9
    pos = core.Position(code="600594", name="益佰制药")
    pos.short_qty = 15700
    pos.short_cost = 3.19
    pos.last_price = 3.19
    st.positions = {"600594": pos}

    from ptrade.config import load_config

    real_cfg = load_config(str(ROOT))
    data, source, _ = service.live_state_summary(real_cfg, _FakeDS(None), st)
    assert source == "state"
    assert data["total_pnl"] == service.state_summary(st)["total_pnl"]


# ── B1/B3 · 源码级：不得用 mark_positions_live 拼 state；/api/state 不得重估
def test_v10_b1b_does_not_use_mark_positions_live():
    """V10-B1b（Architect B1）：`live_state_summary` **不得**用 `mark_positions_live` 拼 state。"""
    import ast as _ast
    import textwrap as _textwrap

    src = inspect.getsource(service.live_state_summary)
    # ★ **只检查代码体**：剥掉 docstring —— 否则会被我自己那句
    #   「★ 不要用 mark_positions_live …」的**警告注释**命中 ✗
    #   （这是今天第 8 次子串坑，见 NOTES 铁律 12）
    tree = _ast.parse(_textwrap.dedent(src))
    fn = tree.body[0]
    if (
        fn.body
        and isinstance(fn.body[0], _ast.Expr)
        and isinstance(getattr(fn.body[0], "value", None), _ast.Constant)
    ):
        fn.body = fn.body[1:]
    code_only = _ast.unparse(fn)
    assert "mark_positions_live" not in code_only, "会字段错位（返回 dict 行，不是 Position）"
    assert "mark_to_market" in code_only
    assert "refresh_risk_flags" in code_only
    assert "state_summary" in code_only


def test_v10_b3_api_state_untouched():
    """V10-B3（INV-40）：`/api/state` 源码**不得**出现实时重估。"""
    src = inspect.getsource(webapp_mod.WebApp.api_state)
    for bad in ("live_state_summary", "mark_positions_live"):
        assert bad not in src, f"api_state 不得用 {bad} —— 它受 INV-40 约束（与 status --json 逐字段一致）"


# ── B5 · as_of/source + 前端 testid
def test_v10_b5_live_state_has_source_and_asof():
    """V10-B5：`api_live_state` 必须返回 `as_of` 与 `source`；前端必须有 live-pnl-source。"""
    src = inspect.getsource(webapp_mod.WebApp.api_live_state)
    assert 'data["as_of"]' in src and 'data["source"]' in src
    # ★ 必须与 `/api/state` **真正同形** —— 特别要 `ledger_consistency`：
    #   缺它 ⇒ 前端 `renderDivergence` 拿到 `diverged===undefined` ⇒ 渲染**灰色横幅**
    #   ⇒ 右列 +高度 ⇒ `test_ui_layout_v4` 4 条红 ✗（实测 888 > 807）
    assert 'data["ledger_consistency"]' in src, "缺 ledger_consistency ⇒ 灰横幅占高度 ⇒ 布局红"
    html = webui_mod.APP_HTML
    # 前端读 live_state（M2），并在 hover title 里给出「盈亏来源」
    assert "api(API.live_state)" in html


# ── M2 · 前端读 live_state
def test_v10_m2_frontend_reads_live_state():
    """V10-M2（Critic 必补）：`refreshState()` 必须读 `API.live_state`，否则新端点白做。"""
    html = webui_mod.APP_HTML
    assert "api(API.live_state)" in html, "refreshState 仍读 /api/state ✗"
    assert 'live_state: "/api/live_state"' in html


# ── A3 · 分数时效
def test_v10_a3_analysis_asof_shown():
    """V10-A3：分析卡必须显示分数时效（`data-testid="analysis-asof"`），不靠文本扫描判状态。"""
    html = webui_mod.APP_HTML
    # ★ **零新增 DOM** 形态：asof 写进**已有元素** `#analysis-state` 的 `data-asof` 属性
    #   （新增独立 span / 追加文本都会被 `test_ui_layout_v4` 打红：左列头部一变高，
    #     整页 flex 重排 ⇒ 右列可用高度被压 ⇒ INV-66b / K线面板 ≥215px 全红 ✗）
    assert 'setAttribute("data-asof"' in html, "缺少 data-asof 属性写入"
    assert '"算于 "' in html, "缺少 hover 可见的 title 文案"
    # 禁止用文本扫描判状态（ANALYSIS_PENDING 注释明确禁止）
    assert 'indexOf("计算中")' not in html
    # ★ 反证：不得再新增独立的 analysis-asof 元素（会占高度）
    assert 'id="analysis-asof"' not in html, "不要再新增元素 —— 布局预算为 0px"


# ── A1 · 同交易日永久（仅 ok=True）；失败仍 120s
def test_v10_a1_trade_day_hit(tmp_path, monkeypatch):
    """V10-A1a：`trade_day == asof` 且 `ok=True` ⇒ **命中**（哪怕缓存很旧）。"""
    cfg = type("C", (), {"root": str(tmp_path)})()
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    key = chart_mod.analysis_cache_key("600594", "2026-11-09")
    payload = {"ok": True, "total": 32.0, "signal": "short", "factors": [], "computed_at": "2026-11-09T09:35:00"}
    (cache / f"{key}.json").write_text(
        json.dumps({"payload": payload, "fetched_at": "2026-01-01T00:00:00"}), encoding="utf-8"
    )
    got = chart_mod.read_cached_analysis(cfg, "600594", "2026-11-09", trade_day="2026-11-09")
    assert got is not None and got.get("total") == 32.0


def test_v10_a1_failed_never_permanent(tmp_path):
    """V10-A1b（★ Critic/A1c）：`ok=False` **永远**按 120s 过期，不许被「同交易日永久」放宽。"""
    cfg = type("C", (), {"root": str(tmp_path)})()
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    key = chart_mod.analysis_cache_key("600594", "2026-11-09")
    payload = {"ok": False, "error": "boom"}
    (cache / f"{key}.json").write_text(
        json.dumps({"payload": payload, "fetched_at": "2026-01-01T00:00:00"}), encoding="utf-8"
    )
    got = chart_mod.read_cached_analysis(cfg, "600594", "2026-11-09", trade_day="2026-11-09")
    assert got is None, "失败缓存被「同交易日永久」放宽了 ✗"


# ── M1 · 自选池写回同一缓存
def test_v10_m1_watchlist_score_writes_cache():
    """V10-M1（Critic 必补）：`api_watchlist_score` 必须把结果**写回** `analysis_<code>_<asof>`。

    ★ 这是「页面 33 分 vs 重算 32 分」的根因修复：两条打分路径从此共用同一份缓存。
    """
    src = inspect.getsource(webapp_mod.WebApp.api_watchlist_score)
    assert "write_cached_analysis" in src, "自选池打分没有写回分析缓存 ⇒ 两条路径仍会不一致 ✗"
    assert "computed_at" in src, "没写 computed_at ⇒ A3 无法显示计算时刻"
    # ★ 写回处必须有 INV-42 guard
    assert "except NetworkForbiddenInLock" in src


# ═══════════════════════════════════════════════════════════════════════════ #
# ★ code-review 要求的**行为判据**（评审建议 5：原来全是 `inspect.getsource` 文本断言，
#   恰好放过了 B1/B3/B5 三个行为缺陷）
# ═══════════════════════════════════════════════════════════════════════════ #
def test_v10_b1_payload_is_ready_shape(tmp_path):
    """★ V10-B1（CRITICAL，行为判据）：写回的分析载荷必须让 `analysis_state` 判 **ready**。

    ★★ 这条钉住的是本轮最严重的缺陷：
       曾把 `CanslimResult.to_dict()` 直接写进 `analysis_<code>_<asof>` —— 它**没有 `ok`**、
       `factors` 是 **list** ⇒ `analysis_state()` 判 `failed` ⇒ TTL 走 `ANALYSIS_FAIL_TTL=120s`
       ⇒ **A4 周末扫描（4~8h × 5024 只）的产出 2 分钟后全部作废** ✗
    """
    from ptrade import chart, strategy

    class _F:
        def __init__(self, key, score, note=""):
            self.key, self.score, self.note = key, score, note
        def to_dict(self):
            return {"key": self.key, "score": self.score, "note": self.note}

    class _R:
        code, name, asof, total, signal = "600594", "益佰制药", "2026-11-09", 32.0, "short"
        reasons, risks = ["r1"], ["k1"]
        factors = [_F(k, 5.0, f"note-{k}") for k in "CANSLIM"]

    class _Cfg:
        class root:
            pass

    cfg = type("C", (), {"root": str(tmp_path)})()
    payload = chart.analysis_payload_from_result(_R(), cfg, computed_at="2026-11-09T15:31:00")

    # ① 形状必须是"缓存期望"的那一种
    assert payload["ok"] is True
    assert isinstance(payload["factors"], dict), "factors 必须是 dict（list 会被判 failed）✗"
    for k in "CANSLIM":
        assert k in payload["factors"], f"缺因子 {k}"
    assert payload["computed_at"] == "2026-11-09T15:31:00"

    # ② 真的能落盘 + 读回 + 判 ready（**端到端**，不是源码文本）
    chart.write_cached_analysis(cfg, "600594", "2026-11-09", payload)
    got = chart.read_cached_analysis(cfg, "600594", "2026-11-09", trade_day="2026-11-09")
    assert got is not None, "写进去却读不到"
    assert chart.analysis_state(got) == "ready", (
        f"analysis_state 判了 {chart.analysis_state(got)}（应为 ready）⇒ 会按 120s 失败 TTL 过期 ✗"
    )
    assert got["total"] == 32.0


def test_v10_b1_rejects_dirty_payload(tmp_path):
    """★ V10-B1b：`write_cached_analysis` 必须**拒绝**脏载荷（第二道防线）。"""
    from ptrade import chart

    cfg = type("C", (), {"root": str(tmp_path)})()
    with pytest.raises(ValueError, match="factors 必须是 dict"):
        chart.write_cached_analysis(
            cfg, "600594", "2026-11-09",
            {"ok": True, "total": 1.0, "factors": [{"key": "C", "score": 1.0}]},
        )


def test_v10_b3_trade_day_wired_in_production(tmp_path, monkeypatch):
    """★ V10-B3（HIGH，行为判据）：**生产调用点**必须真的传 `trade_day=`。

    ★ 缺陷：`chart.read_cached_analysis(..., trade_day=)` 写好了但**生产零调用**
    ⇒ 6h TTL 照旧 ⇒ 同一交易日内仍会重算 2–4 次 ⇒ 用户「当天内稳定」未达成 ✗
    """
    from ptrade import chart as chart_mod
    from ptrade import webapp as webapp_mod

    # ① 生产源码里必须出现关键字实参（两处）
    src = pathlib.Path(webapp_mod.__file__).read_text(encoding="utf-8")
    assert src.count("trade_day=_trade_day") == 2, (
        f"webapp 只有 {src.count('trade_day=_trade_day')} 处传了 trade_day（应 2 处）"
    )

    # ② 行为：7 天前的 ok=True 缓存 + trade_day == asof ⇒ 必须**命中**
    cfg = type("C", (), {"root": str(tmp_path)})()
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    key = chart_mod.analysis_cache_key("600594", "2026-11-09")
    payload = {"ok": True, "total": 32.0, "signal": "short", "factors": {"C": 1.0}}
    (cache / f"{key}.json").write_text(
        json.dumps({"payload": payload, "fetched_at": "2026-01-01T00:00:00"}), encoding="utf-8"
    )
    got = chart_mod.read_cached_analysis(cfg, "600594", "2026-11-09", trade_day="2026-11-09")
    assert got is not None, "同交易日的旧缓存没命中 ⇒ A1 未生效 ✗"
    assert chart_mod.analysis_state(got) == "ready"


def test_v10_b5_source_marker_is_visible():
    """★ V10-B5（MEDIUM-HIGH，行为判据）：用户必须**看得见**盈亏时效。

    ★ 缺陷：原来只有后端 `as_of`/`source` 字段，页面 `grep live-pnl-source` = **0 处**
    ⇒ 用户仍不知道「这个盈亏是哪一刻的」✗（正是用户抱怨的那半个问题）
    ★ 布局约束：**不许新增行**（`test_ui_layout_v4` 零容忍）⇒ 只在「总盈亏」的值里加 2 字标记
    """
    from ptrade import webui as webui_mod

    html = webui_mod.APP_HTML
    assert 'data-testid="live-pnl-source"' in html, "页面上找不到来源标记 ✗"
    # ★ 可见来源在**「总盈亏」的标签**里（零宽度增量）；值只包一层 title
    assert '"总盈亏" + (_src === "live" ? "（实时）" : "（结算）")' in html
    assert '<span data-testid="live-pnl-source" title="' in html
    # 反证：值里**不许**再叠加可见字符（那是布局红过的两次位置：851 / 869）
    assert "num(d.total_pnl) + \" \" + _srcTag" not in html
    # 完整时刻在 title（零高度），两态文案不同
    assert '"实时 · " + _asOf' in html
    assert '"截至 " + _asOf + "（未取到实时价）"' in html
