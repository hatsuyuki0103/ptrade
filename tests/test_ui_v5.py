"""V5-W3 · 判据 V5-1..V5-17（按 test-spec **R3 修订**）。

真源：``.omx/plans/test-spec-ptrade-v5.md``（R2/R3 为最终版）· ``.omx/plans/prd-ptrade-v5.md``。

分工：
* **真机判据**（需要浏览器）走 ``tests/_cdp.py``：常驻 Chrome + ``Emulation`` 域 + fetch-spy
  （网络计数）/ 可见性注入 / ``PN_CONFIG`` 注入（把轮询窗口压到毫秒级做「假分析」实验）；
* **结构判据**（V5-8/V5-14）纯源码/AST，无浏览器也跑 —— 防「全 skip ⇒ 假绿」；
* **事故纪律**：``tests/conftest.py::_watchlist_bytes_guard`` 是会话级字节守卫
  （本文件 V5-8 再补一条廉价正则扫描，两级）。

★ R3 的四条假绿修正已逐条落地：
  R3-A ``elapsed_s`` 生命周期（pending 递增 / ready 是数字 / 从未起 job 才 null）
  R3-B ``poll_cadence_ms`` **状态映射三子用例** + 反例（不再 ``in (0,5000,15000)`` 恒真）
  R3-C ``V5-4`` 用 ``offsetTop``（不受右列滚动污染）+ 先滚到顶 + 前置 ``> 0``
  R3-D ``V5-2`` **前置选买入信号** + ``entry is not None``（避免 ``None == None``）
★ R3-H ``V5-6`` 三层（算术 / 可注入常量跑假分析 / 端到端标 slow）。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shutil
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ptrade import core, store  # noqa: E402
from ptrade.config import load_config  # noqa: E402
from tests import _cdp  # noqa: E402

#: V5 的真机判据需要**驱动本机服务**（127.0.0.1 回环 HTTP）。conftest 的 `_no_network`
#: 会封掉所有 `socket.connect` ⇒ 在这里用**同名的更窄版本**覆盖（pytest 夹具解析：模块级优先）：
#: 只放行 loopback（127.0.0.1 / ::1 / localhost），其它地址仍然拦死（保持「测试不联网」纪律）。
import socket as _socket_mod  # noqa: E402

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _host_of(address) -> str:  # noqa: ANN001
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return str(address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):        # noqa: F811 - 有意覆盖 conftest 的同名 autouse 夹具
    real_connect = _socket_mod.socket.connect
    real_create = _socket_mod.create_connection

    def guarded_connect(self, address, *args, **kwargs):  # noqa: ANN001
        if _host_of(address) in _LOOPBACK:
            return real_connect(self, address, *args, **kwargs)
        raise AssertionError(f"测试期间禁止联网（仅允许 loopback）：{address!r}")

    def guarded_create(address, *args, **kwargs):  # noqa: ANN001
        if _host_of(address) in _LOOPBACK:
            return real_create(address, *args, **kwargs)
        raise AssertionError(f"测试期间禁止联网（仅允许 loopback）：{address!r}")

    monkeypatch.setattr(_socket_mod.socket, "connect", guarded_connect, raising=False)
    monkeypatch.setattr(_socket_mod, "create_connection", guarded_create, raising=False)
    yield

FIX = ROOT / "data" / "fixture"

#: V5-1：reasons 下限。test-spec §0 的「8 条」来自**真实联网**；离线夹具实测只有 6 条
#: ⇒ 下限取 5（非空洞），并额外断言 **DOM 条目数 == payload 条数**（免疫前缀碰撞）。
MIN_REASONS = 5
#: V5-3：五段冻结契约顺序
SECTION_ORDER = ["total", "factors", "reasons", "risks", "advice"]
#: V5-13：明示文案必须同时含这两个数字（60s 行情 / 5s 界面）
FRESHNESS_MUST = ("60", "5")
#: V5-11：服务端三态映射（R3-B）
CADENCE_MAP = [(True, 5000), (False, 0), (None, 15000)]

#: 慢判据开关（V5-6c 端到端 300s / V5-9 真等待）：默认不跑，避免拖慢全量
RUN_SLOW = os.environ.get("PTRADE_RUN_SLOW") == "1"


# =========================================================================== #
# 环境：隔离 temp root + 随机端口服务（★ 绝不碰仓库 data/watchlist.json / 8765）
# =========================================================================== #
def _universe_codes(limit: int = 32) -> list[str]:
    codes = sorted({p.stem[len("daily_"):] for p in FIX.glob("daily_*.json")
                    if "unadjust" not in p.stem})
    return codes[:limit]


def _build_root(watchlist_n: int = 3, positions: int = 0) -> pathlib.Path:
    root = pathlib.Path(__import__("tempfile").mkdtemp(prefix="v5w3-"))
    (root / "data" / "fixture").mkdir(parents=True, exist_ok=True)
    (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
    for p in FIX.glob("*.json"):
        if p.name.startswith(("daily_", "spot", "trade_dates", "margin_targets", "universe",
                              "industry_map", "quarterly", "holders", "top_holders", "fin_")):
            shutil.copy2(p, root / "data" / "fixture" / p.name)
    cfg = load_config(str(root))
    state = core.new_state(cfg)
    if positions:
        for code in _universe_codes(positions):
            state.positions[code] = core.Position(
                code=code, name="票" + code, long_qty=100, long_available=100,
                long_cost=10.0, last_price=10.0)
    store.save_state(cfg, state, source="v5-w3")
    if watchlist_n:
        items = [{"code": c, "name": "票" + c, "note": "", "added_at": "2026-10-20T09:00:00"}
                 for c in _universe_codes(watchlist_n)]
        path = root / "data" / "watchlist.json"
        # ★ 必须带 schema_version：`watchlist.load_watchlist` 会校验它，缺失 ⇒ ValueError
        #   ⇒ 空池 ⇒ 「自选池可滚 / 不跳顶 / 5s 刷新」全部**假绿**（我第一版踩了）
        path.write_text(json.dumps({"schema_version": 1,
                                    "updated_at": "2026-10-20T09:00:00+00:00",
                                    "items": items}, ensure_ascii=False), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def env3(tmp_path_factory):
    """默认环境：自选池 3 只。

    ★ 用 ``offline=True`` 起服务：数据来自夹具 ⇒ 判据**确定性**、不触发真实网络。
    需要「UI 认为在线」的判据（分析卡渲染类）由 ``v5_init_js(force_offline=False)``
    覆盖 ``/api/health.offline`` 来实现 —— 前端权威本来就来自服务端字段。
    """
    root = _build_root(watchlist_n=3)
    cfg = load_config(str(root))
    with _cdp.serve(cfg, offline=True) as base:
        yield cfg, base


@pytest.fixture(scope="module")
def env20():
    """自选池 20 只（V5-4/V5-16）。"""
    root = _build_root(watchlist_n=20)
    cfg = load_config(str(root))
    with _cdp.serve(cfg, offline=True) as base:
        yield cfg, base


@pytest.fixture(scope="module")
def env_offline():
    """离线实例（V5-15：分析卡必须明示不可用，且不得显示「计算中」）。"""
    root = _build_root(watchlist_n=3)
    cfg = load_config(str(root))
    with _cdp.serve(cfg, offline=True) as base:
        yield cfg, base


@pytest.fixture(scope="module")
def cdp_status():
    info = _cdp.status()
    if not info.get("available"):
        pytest.skip("CDP 不可用（**未验证**，非通过）：" + str(info.get("reason")))
    return info


# --------------------------------------------------------------------------- #
# HTTP / 测量 / 分析状态工具
# --------------------------------------------------------------------------- #
def _api_json(base: str, path: str, timeout: float = 60.0) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:   # noqa: S310 - 本地回环
        return json.loads(resp.read().decode("utf-8"))


def _analysis_status(base: str, code: str) -> dict:
    body = _api_json(base, f"/api/analysis?code={code}")
    return body.get("data") or {}


def _wait_analysis(base: str, code: str, *, want: str = "ready",
                   timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = _analysis_status(base, code)
        if last.get("state") == want:
            return last
        if last.get("state") in ("failed", "no_data") and want == "ready":
            pytest.fail(f"分析 {code} 状态 = {last.get('state')}（等待 {want}）：{last}")
        time.sleep(0.25)
    pytest.fail(f"{timeout:.0f}s 内 {code} 未到 {want}；最后读数：{last}")


def _trigger_chart(base: str, code: str, timeout: float = 120.0) -> dict:
    """触发一次图表数据请求（服务端会**顺带**排一个分析 job）。"""
    return _api_json(base, f"/api/chart/data?code={code}&period=daily", timeout=timeout)


_EXPAND_JS = ("(() => { const d = document.querySelector('#analysis-details'); "
              "if (d) { d.open = true; } return !!d; })()")


def measure_v5(base: str, *, viewport=(1920, 945), expand_analysis: bool = False,
               init_js: str | None = None, network_window_ms: int | None = None,
               actions_js: str | None = None, actions_wait_ms: int = 0,
               extra_eval: str | None = None, wait_for_js: str | None = None,
               wait_for_js_timeout_ms: int | None = None,
               wait_analysis_ready: bool = False,
               assume_online: bool = False,
               network_from_actions: bool = False) -> dict:
    opts: dict = {"url": base, "settle_ms": 800, "wait_analysis_ready": wait_analysis_ready}
    if init_js:
        opts["init_js"] = init_js
    elif assume_online:
        # 服务用 offline=True 起（数据确定性），但让前端认为「服务端是在线实例」，
        # 这样分析卡的 ready/pending 渲染路径才会被走到（V5-1/2/3/10/12）。
        opts["init_js"] = _cdp.v5_init_js(force_offline=False)
    if expand_analysis:
        opts["actions_js"] = _EXPAND_JS
        opts["actions_wait_ms"] = 600
    if actions_js:
        opts["actions_js"] = actions_js
        opts["actions_wait_ms"] = actions_wait_ms or 300
    if network_window_ms:
        opts["network_window_ms"] = network_window_ms
    if network_from_actions:
        opts["network_from_actions"] = True
    if extra_eval:
        opts["extra_eval"] = extra_eval
    if wait_for_js:
        opts["wait_for_js"] = wait_for_js
        opts["wait_for_js_timeout_ms"] = wait_for_js_timeout_ms or 20000
    return _cdp.measure(viewport, **opts)


#: V5-2 用的代码：分析里 action 必须是「买入」的票（运行期扫描，不硬编码）
_BUY_CODE_CACHE: list[str] = []


def _buy_signal_code(cfg) -> str:
    if _BUY_CODE_CACHE:
        return _BUY_CODE_CACHE[0]
    from ptrade import strategy
    from ptrade.datasource import DataSource

    ds = DataSource(cfg, offline=True)
    codes = _universe_codes(32)
    try:
        asof = ds.latest_trade_asof(codes[:5])
    except Exception:  # noqa: BLE001
        asof = None
    scorer = strategy.CanslimScorer(cfg, ds, offline=True)
    results = scorer.score_many(codes, asof)
    recs = strategy.recommend(results, cfg)
    buy = [r for r in recs if str(getattr(r, "action", "")).lower().replace("_", " ")
           in ("买入", "open long", "buy") or getattr(r, "action", "") == "买入"]
    if not buy:
        # 退一步：side == long 且 action 含「买」
        buy = [r for r in recs if "买" in str(getattr(r, "action", ""))]
    assert buy, (
        "前置失败：夹具里找不到「买入」信号的票（V5-2 需要它来避免 None == None 假绿）"
    )
    code = str(buy[0].code)
    _BUY_CODE_CACHE.append(code)
    return code


# =========================================================================== #
# V5-1 分析详情必须渲染（INV-69）
# =========================================================================== #
def _check_analysis_body_rendered(m: dict, reasons: list, risks: list) -> None:
    """V5-1 的判据核心（真测试与「变异自证」共用同一份逻辑）。"""
    body = m.get("analysisBodyText") or ""
    assert body.strip(), "分析卡正文为空（展开后也没有内容）"
    assert len(reasons) >= MIN_REASONS, (
        f"前置：服务端 reasons 只有 {len(reasons)} 条（应 ≥{MIN_REASONS}）"
    )
    for reason in reasons:
        assert str(reason)[:18] in body, f"分析卡正文缺这条解释：{str(reason)[:60]!r}"
    for risk in risks:
        assert str(risk)[:18] in body, f"分析卡正文缺这条风险：{str(risk)[:60]!r}"
    # ★ R3-E：DOM 条目计数（免疫前缀碰撞）
    assert m.get("analysisReasonItems") == len(reasons), (
        f"解释条目 DOM 数 {m.get('analysisReasonItems')} != reasons {len(reasons)}"
    )
    if risks:
        assert m.get("analysisRiskItems") == len(risks), (
            f"风险条目 DOM 数 {m.get('analysisRiskItems')} != risks {len(risks)}"
        )


def _check_elapsed_fields(pending: dict, ready: dict) -> None:
    """V5-7 的判据核心：pending 数字、ready 是数字（不得 null）。"""
    assert isinstance(pending.get("elapsed_s"), (int, float)), (
        f"pending 的 elapsed_s 必须是数字（实际 {pending.get('elapsed_s')!r}）"
    )
    assert isinstance(ready.get("elapsed_s"), (int, float)), (
        f"ready 的 elapsed_s 必须是数字（不得 null，实际 {ready.get('elapsed_s')!r}）"
    )
    assert float(ready["elapsed_s"]) >= float(pending["elapsed_s"])


def test_v5_1_analysis_body_renders_reasons_and_risks(env3, cdp_status):
    """★ 真机：展开分析卡后，body 必须**逐条**包含 reasons 与 risks，且 DOM 条目数对得上。"""
    cfg, base = env3
    code = "600519"
    _trigger_chart(base, code)
    payload = _wait_analysis(base, code)
    m = measure_v5(base, expand_analysis=True, wait_analysis_ready=True, assume_online=True)
    _check_analysis_body_rendered(m, payload.get("reasons") or [], payload.get("risks") or [])


# =========================================================================== #
# V5-2 交易建议来自缓存里的 recommend（不重算）—— R3-D 前置
# =========================================================================== #
def test_v5_2_recommend_fields_wired_from_cache(env3, cdp_status, monkeypatch):
    """★ R3-D 的前置修正：夹具里**没有买入信号**（实测 action 只有 watch/close）⇒
    用**注入的 recommend**把目标票升级为 ``open_long`` 造前置，断言点保持不变：
    ① ``action`` 买入、``entry`` 非 None 且 > 0；② ``/api/analysis`` 的七字段与
    **分析缓存里的 recommend 逐字段相等**（证明透传、不是重算）。

    ★ 这是对 test-spec R3-D 的**如实偏离**（夹具不提供 buy 信号），报告里写明。
    """
    from ptrade import chart as chart_mod
    from ptrade import strategy

    cfg, base = env3
    code = "600519"
    injected = {"action": "open_long", "side": "long", "entry": 1263.0,
                "stop_loss": 1187.0, "take_profit": 1400.0,
                "position_pct": 0.2, "confidence": 0.7}
    real_recommend = strategy.recommend

    def fake_recommend(results, cfg_arg):              # noqa: ANN001
        recs = real_recommend(results, cfg_arg)
        for rec in recs:
            if str(rec.get("code")) == code:
                rec.update(injected)
        return recs

    monkeypatch.setattr(strategy, "recommend", fake_recommend)
    for path in (pathlib.Path(cfg.root) / "data" / "cache").glob("analysis_*.json"):
        path.unlink()                                  # 只删**临时 root** 的缓存
    payload = _wait_analysis(base, code, want="ready", timeout=180)
    rec = payload.get("recommend") or {}
    assert rec.get("action") == "open_long", (
        f"前置：注入后 action 应为 open_long，实际 {rec.get('action')!r}（整个 recommend={rec!r}）"
    )
    entry = rec.get("entry")
    assert entry is not None and float(entry) > 0, f"★ entry 必须是正数（实际 {entry!r}）"
    for key in ("stop_loss", "take_profit"):
        assert rec.get(key) is not None, f"recommend 缺 {key}"

    cached = chart_mod.read_cached_analysis(cfg, code, str(payload.get("asof"))[:10])
    assert cached and isinstance(cached.get("recommend"), dict), (
        "分析缓存里没有 recommend（V5 要求它由 _score_analysis 落盘、Web 透传）"
    )
    cached_rec = cached["recommend"]
    for key in ("action", "side", "entry", "stop_loss", "take_profit",
                "position_pct", "confidence"):
        assert key in rec, f"recommend 缺 {key!r}（应透传自缓存 recommend）"
        assert rec[key] == cached_rec[key], (
            f"{key} 与缓存不一致：payload.recommend[{key}]={rec[key]!r} "
            f"cache.recommend[{key}]={cached_rec[key]!r} —— 疑似重算而非透传"
        )


# =========================================================================== #
# V5-3 合并成一张卡（INV-73）
# =========================================================================== #
def test_v5_3_single_merged_card_with_dom_order(env3, cdp_status):
    _, base = env3
    _trigger_chart(base, "600519")
    _wait_analysis(base, "600519")
    m = measure_v5(base, expand_analysis=True, wait_analysis_ready=True, assume_online=True)
    assert m.get("hasAnalysisDetails") is True, "缺少 #analysis-details 分析卡"
    assert m.get("hasScoreDetails") is False, "打分卡应已并入分析卡（#score-details 不得存在）"
    order = m.get("analysisSectionOrder")
    assert order == SECTION_ORDER, f"data-section DOM 顺序不符：{order} != {SECTION_ORDER}"


# =========================================================================== #
# V5-4 自选池卡内滚、不分页（INV-72）—— R3-C：offsetTop
# =========================================================================== #
def test_v5_4_watchlist_scrolls_inside_card(env3, env20, cdp_status):
    _, base3 = env3
    _, base20 = env20
    m3 = measure_v5(base3)
    m20 = measure_v5(base20)
    wl = m20.get("watchlist")
    assert wl, "页面没有 #watchlist"
    assert wl.get("rows", 0) >= 15, (
        f"★ 非空洞前置：20 只自选应渲染 ≥15 行，实际 {wl.get('rows')} 行"
        "（空池也会让 scrollH>clientH 成立 —— 那是假绿）"
    )
    assert wl["scrollH"] > wl["clientH"] + 1, (
        f"20 只自选时卡片应内部可滚：scrollH {wl['scrollH']} <= clientH {wl['clientH']}"
    )
    assert wl["overflowY"] == "auto", f"#watchlist overflow-y 应为 auto，实际 {wl['overflowY']!r}"
    top3 = m3.get("positionsCardOffsetTop")
    top20 = m20.get("positionsCardOffsetTop")
    assert top3 is not None and top3 > 0, (
        f"前置：持仓卡 offsetTop 应 > 0（否则测量无效，R3-C）：{top3!r}"
    )
    assert abs(top20 - top3) <= 2, (
        f"自选池把「持仓」推下去了：{top3} → {top20}（用 offsetTop，不受右列滚动污染）"
    )
    assert m20.get("hasPagination") is False, "自选池不得分页"


# =========================================================================== #
# V5-5 盘中 5 秒刷新
# =========================================================================== #
def test_v5_5a_poll_active_is_5000_runtime(env3, cdp_status):
    """★ R2-C：**CDP 运行时读**常量（不用源码正则）。"""
    _, base = env3
    m = measure_v5(base, extra_eval="String(window.POLL_ACTIVE_MS)")
    got = m.get("extraEval")
    assert got not in (None, "undefined", ""), (
        "运行时读不到 POLL_ACTIVE_MS（可能改成了 let/const 词法声明，未挂到 window）"
    )
    assert int(float(got)) == 5000, f"POLL_ACTIVE_MS = {got}（应 5000）"


def test_v5_5b_five_seconds_quote_refresh(env3, cdp_status):
    """真机：服务端说「盘中」时，一个刷新周期内必须真的打 `/api/quote`。

    ★ 窗口用 11s 而不是 5.5s：`autoRefreshTick` 的节拍器是 `setInterval(5s)`，
    首跳相对页面加载有相位差 —— 5.5s 窗口会因相位落空而**假红**（实测）。11s 能区分
    5s 与旧 15s 两档（15s 档在 11s 内必然 0 次）。
    """
    _, base = env3
    init = _cdp.v5_init_js(force_market_open=True)
    m = measure_v5(base, init_js=init, network_window_ms=11000)
    calls = (m.get("netDelta") or {}).get("/api/quote", 0)
    assert calls >= 1, f"11s 内 /api/quote 请求 {calls} 次（盘中应 ≥1）"


def test_v5_5c_server_false_means_no_poll(env3, cdp_status):
    """★ 服务端 market_open=false 是权威：不得自动刷。"""
    _, base = env3
    init = _cdp.v5_init_js(force_market_open=False)
    m = measure_v5(base, init_js=init, network_window_ms=11000)
    calls = (m.get("netDelta") or {}).get("/api/quote", 0)
    assert calls == 0, f"非盘中仍请求了 {calls} 次 /api/quote"


# =========================================================================== #
# V5-6 分析算完必须自动出现（INV-70）—— R3-H 三层
# =========================================================================== #
def test_v5_6a_poll_window_arithmetic():
    """① 算术：窗口与次数必须覆盖真实首算（实测 ≈132s）。"""
    src = (ROOT / "ptrade" / "webui.py").read_text(encoding="utf-8")
    ms_match = re.search(r"ANALYSIS_POLL_MS\s*[:=]\s*(\d+)", src)
    max_match = re.search(r"ANALYSIS_POLL_MAX\s*[:=]\s*(\d+)", src)
    assert ms_match, "找不到 ANALYSIS_POLL_MS 常量"
    assert max_match, "找不到 ANALYSIS_POLL_MAX 常量"
    poll_ms, poll_max = int(ms_match.group(1)), int(max_match.group(1))
    assert poll_ms * poll_max >= 600_000, (
        f"轮询窗口 {poll_ms * poll_max}ms < 600s（改动前 6×20000=120s ⇒ 132s 的首算会放弃）"
    )
    assert poll_max >= 30, f"轮询次数 {poll_max} < 30"


def _run_fake_analysis(base: str, *, poll_ms: int, poll_max: int, ready_at_tick: int) -> int:
    """用注入常量 + 假 `/api/analysis` 跑一次「假分析」，返回实际轮询次数。

    ★ 这是 V5-6 的**核心判据**：ready 在第 ``ready_at_tick`` 次响应才出现，
    上界不够时前端会提前放弃（次数停在 max+1）—— 于是「旧上界 6」必不到、「新上界 40」必到。
    """
    init = _cdp.v5_init_js(
        force_offline=False,      # 服务端 offline=True 起（数据确定性），前端按在线走
        poll_ms=poll_ms,
        poll_max=poll_max,
        fake_analysis={"code": "600519", "readyAtTick": ready_at_tick, "pollMs": poll_ms},
    )
    window_ms = int(poll_ms * (ready_at_tick + 12) + 1500)
    m = measure_v5(base, init_js=init, network_window_ms=window_ms)
    return int((m.get("netDelta") or {}).get("/api/analysis", 0))


def test_v5_6b_injectable_constants_prove_old_bound_gives_up(env3, cdp_status):
    """★ 可注入常量（``window.PN_CONFIG``）：新上界到、旧上界放弃。"""
    _, base = env3
    new_calls = _run_fake_analysis(base, poll_ms=20, poll_max=40, ready_at_tick=35)
    old_calls = _run_fake_analysis(base, poll_ms=20, poll_max=6, ready_at_tick=35)
    assert new_calls >= 35, (
        f"新上界（40）只轮询了 {new_calls} 次（应 ≥35：ready 在第 35 次响应才出现）"
        "—— PN_CONFIG 注入没生效或轮询被打断"
    )
    assert old_calls <= 7, (
        f"旧上界（6）轮询了 {old_calls} 次（应 ≤7）—— 说明 max 注入没生效，判据没牙齿"
    )


@pytest.mark.skipif(not RUN_SLOW, reason="慢判据（端到端 ≤300s）：设 PTRADE_RUN_SLOW=1 才跑")
def test_v5_6c_analysis_appears_without_user_action(env3, cdp_status):
    """③ 端到端：真实分析在窗口内自动到达（不点任何东西）。"""
    _, base = env3
    code = "600519"
    m = measure_v5(base, wait_analysis_ready=True, assume_online=True)
    state = (m.get("analysisState") or "")
    text = m.get("analysisStateText") or ""
    assert state in ("ready", "no_data"), f"分析未自动就绪：state={state!r} text={text!r}"


# =========================================================================== #
# V5-7 pending 必须带进度（R3-A lifecycle）
# =========================================================================== #
def test_v5_7_elapsed_lifecycle(env3, cdp_status, monkeypatch):
    """★ R3-A：``pending`` 数字且递增；``ready`` 是最终耗时（不是 null）；只有「从未起 job」才 null。"""
    from ptrade import strategy
    from ptrade import webapp as webapp_mod

    _, base = env3
    code = "000001"
    # ★ 「从未起过 job」用**私有函数**直接判：`/api/analysis` 本身会顺手起 job，
    #   所以用 HTTP 采样永远造不出 null —— 这是对 R3-A 采样方式的必要修正。
    assert hasattr(webapp_mod, "_elapsed_s"), "webapp 缺 _elapsed_s 实现（V5-7）"
    never_root = _build_root(watchlist_n=1)
    assert webapp_mod._elapsed_s(load_config(str(never_root)), "999999") is None, (
        "从未起过 job 的票 elapsed_s 必须是 null"
    )

    real_score = strategy.CanslimScorer.score

    def slow_score(self, code_arg, asof=None):       # noqa: ANN001 - 注入慢分析
        if str(code_arg) == code:
            time.sleep(6.0)
        return real_score(self, code_arg, asof)

    monkeypatch.setattr(strategy.CanslimScorer, "score", slow_score)
    _trigger_chart(base, code)
    p1 = _wait_analysis(base, code, want="pending", timeout=20)
    assert isinstance(p1.get("elapsed_s"), (int, float)), (
        f"pending 的 elapsed_s 必须是数字（实际 {p1.get('elapsed_s')!r}）"
    )
    time.sleep(2.0)
    p2 = _analysis_status(base, code)
    assert isinstance(p2.get("elapsed_s"), (int, float)), p2
    if p2.get("state") == "pending":
        assert p2["elapsed_s"] > p1["elapsed_s"], (
            f"elapsed_s 必须随时间增大：{p1['elapsed_s']} → {p2['elapsed_s']}"
        )
    r = _wait_analysis(base, code, want="ready", timeout=120)
    _check_elapsed_fields(p1, r)


# =========================================================================== #
# V5-8 不得清空用户自选池（事故纪律，两级）
# =========================================================================== #
_WRITE_CALLS = re.compile(
    r"write_text|write_bytes|json\.dump|\.unlink\(|os\.remove\(|shutil\.move|os\.rename|"
    r"\.write\(")


def test_v5_8_watchlist_byte_guard_and_no_repo_writes():
    """① 会话级字节守卫存在（conftest）；② 廉价正则扫描：测试/探针不得写仓库自选池。"""
    conftest_src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "_watchlist_bytes_guard" in conftest_src, "conftest 缺少会话级字节守卫（V5-8）"
    assert "WATCHLIST_PATH" in conftest_src and "pytest.fail" in conftest_src, (
        "字节守卫必须比较仓库 data/watchlist.json 并在变化时 fail"
    )
    offenders = []
    sources = list((ROOT / "tests").glob("*.py"))
    probe_dir = ROOT / ".omx" / "probe"
    if probe_dir.is_dir():
        sources += list(probe_dir.glob("*.py"))
    for path in sources:
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "data/watchlist.json" not in src and "data\\\\watchlist.json" not in src:
            continue
        for line_no, line in enumerate(src.splitlines(), start=1):
            if "watchlist.json" not in line:
                continue
            window = "\n".join(src.splitlines()[max(0, line_no - 3): line_no + 2])
            if _WRITE_CALLS.search(window) and "read_" not in line and "read_bytes" not in window:
                if "tmp_path" in src or "byte" in window or "isolat" in window:
                    continue
                offenders.append(f"{path.name}:{line_no}")
    assert not offenders, f"这些地方可能写/清用户自选池：{offenders}"


# =========================================================================== #
# V5-9 spot 盘中周期刷新（INV-71）—— R3-G：可注入间隔 + 断言价格真变
# =========================================================================== #
def test_v5_9_spot_actually_refreshes(monkeypatch, tmp_path):
    """★ 注入 0.8s 间隔的假行情源 ⇒ 2 个周期内缓存变**且价格变**（不是心跳假刷新）。"""
    from ptrade import datasource as dsm
    from ptrade.spot_refresher import SpotRefresher

    root = _build_root(watchlist_n=1)
    cfg = load_config(str(root))
    # ★ 必须用 **online** DataSource：离线模式下 `_cached` 直接读夹具、既不调 `_fetch_spot`
    #   也不写缓存 ⇒ 刷新永远"没变化"（我第一版就踩了：price 14.19 → 14.19 假红）。
    ds = dsm.DataSource(cfg, offline=False)
    seq = {"i": 0}

    def fake_fetch_spot(self):                        # noqa: ANN001
        seq["i"] += 1
        price = 10.0 + seq["i"]
        return {"asof": "2026-10-09", "rows": [{"code": "600519", "name": "贵州茅台",
                                                "last": price, "prev_close": 10.0,
                                                "volume": 1000.0, "amount": price * 1000}]}

    monkeypatch.setattr(dsm.DataSource, "_fetch_spot", fake_fetch_spot)
    # 预热一次，保证 /api/quote 走缓存（且 offline 夹具路径不覆盖 cache）
    ds.spot()
    first = ds._spot_frame_raw()[0]
    price0 = float(first.iloc[0]["last"])

    refreshed: list[int] = []
    r = SpotRefresher(refresh=lambda: (ds._refresh_spot_cache(), refreshed.append(1)),
                      should_run=lambda: True, interval_s=0.8)
    r.note_ui_activity()
    r.start()
    try:
        deadline = time.time() + 4.0
        while time.time() < deadline and len(refreshed) < 2:
            time.sleep(0.1)
    finally:
        r.stop()
    assert len(refreshed) >= 2, f"0.8s 间隔下 4s 内只刷新 {len(refreshed)} 次（应 ≥2）"
    later = ds._spot_frame_raw()[0]
    price1 = float(later.iloc[0]["last"])
    assert price1 != price0, f"缓存刷新了但**价格没变**（{price0} → {price1}）—— 心跳式假刷新"


def test_v5_9b_offline_never_refreshes():
    """★ `--offline` / 非盘中的刷新器：`should_run()=False` ⇒ 一次都不抓。"""
    from ptrade.spot_refresher import SpotRefresher

    calls: list[int] = []
    r = SpotRefresher(refresh=lambda: calls.append(1),
                      should_run=lambda: False, interval_s=0.01)
    r.note_ui_activity()
    for _ in range(5):
        assert r.tick() is False
    assert calls == [], f"should_run=False 仍刷新了 {len(calls)} 次"
    assert r.stats["skipped_closed"] == 5, f"统计不对：{r.stats}"


# =========================================================================== #
# V5-10 展开分析卡后页面仍不滚（R2-A）
# =========================================================================== #
def test_v5_10_expanded_analysis_still_no_page_scroll(env3, cdp_status):
    _, base = env3
    _trigger_chart(base, "600519")
    _wait_analysis(base, "600519")
    m = measure_v5(base, expand_analysis=True, wait_analysis_ready=True, assume_online=True)
    assert m["scrollH"] <= m["clientH"] + 1, (
        f"展开分析后**页面**出现滚动条（{m['scrollH']} > {m['clientH']}）—— INV-66 任何状态都成立"
    )
    ic = m.get("infoCol") or {}
    assert m.get("infoColScrollH") is not None
    assert m["infoColScrollH"] > m["infoColClientH"] + 1, (
        "展开后右列应当**内部**可滚（证明内容被限制在列内，而不是把页面撑高）"
    )


# =========================================================================== #
# V5-11 poll_cadence_ms 由服务端下发（R3-B 三态映射 + 反例）
# =========================================================================== #
@pytest.mark.parametrize("market_open,expect", CADENCE_MAP)
def test_v5_11_cadence_state_mapping(env3, monkeypatch, market_open, expect):
    """★ R3-B：不能只断 `in (0,5000,15000)`（那样实现永远返 15000 也过）。"""
    from ptrade import service as service_mod

    _, base = env3
    monkeypatch.setattr(service_mod, "market_open",
                        lambda now, trade_dates: market_open, raising=True)
    data = _api_json(base, "/api/health").get("data") or {}
    assert "poll_cadence_ms" in data, "health 必须下发 poll_cadence_ms（单一真源在服务端）"
    assert data["poll_cadence_ms"] == expect, (
        f"market_open={market_open} 时 poll_cadence_ms 应为 {expect}，实际 {data['poll_cadence_ms']}"
    )


def test_v5_11b_cadence_rejects_unknown_value(env3, cdp_status):
    """★ 反例：非法 cadence（12000）**不得**被当成 5000 用。"""
    _, base = env3
    init = _cdp.v5_init_js(force_cadence_ms=12000)
    # ★ 首 tick 会**无条件**刷一次（POLL.last 初始为 0）⇒ 5.5s 窗口里必然有 1 次，
    #   不能区分「12000 生效」与「被当成 5000」。用 12s 窗口：
    #   12000（应生效）→ 只有首 tick 1 次；被误当 5000 → 5s/10s 各一次 = 2 次。
    m = measure_v5(base, init_js=init, network_window_ms=12000)
    calls = (m.get("netDelta") or {}).get("/api/quote", 0)
    assert calls <= 1, (
        f"非法 cadence 12000 被当成 5s 用了：12s 内 {calls} 次 /api/quote（应 ≤1）"
    )


# =========================================================================== #
# V5-12 默认折叠 + 展开后页面仍不滚（R3-F）
# =========================================================================== #
def test_v5_12_default_collapsed_and_expanded_no_scroll(env3, cdp_status):
    _, base = env3
    _trigger_chart(base, "600519")
    _wait_analysis(base, "600519")
    d = measure_v5(base, wait_analysis_ready=True, assume_online=True)
    assert d.get("analysisOpen") is False, f"分析卡必须默认折叠（实际 open={d.get('analysisOpen')}）"
    m = measure_v5(base, expand_analysis=True, wait_analysis_ready=True, assume_online=True)
    assert m["scrollH"] <= m["clientH"] + 1, "展开后页面仍不得出滚动条"


# =========================================================================== #
# V5-13 行情延迟明示（交付条件）
# =========================================================================== #
def test_v5_13_quote_freshness_is_disclosed(env3, cdp_status):
    _, base = env3
    m = measure_v5(base)
    text = m.get("freshnessHintText") or ""
    assert text, '缺少 data-testid="quote-freshness-hint" 元素'
    for token in FRESHNESS_MUST:
        assert token in text, f"提示必须同时明示 60s 行情与 5s 界面刷新（缺 {token!r}）：{text!r}"


# =========================================================================== #
# V5-14 spot 刷新线程不得持写锁（INV-42，AST 判据）
# =========================================================================== #
_FORBIDDEN_LOCK_NAMES = ("state_lock", "locked_update", "in_critical_section")


def _function_identifiers(path: pathlib.Path, *, name_contains: tuple[str, ...],
                          calls_attr: tuple[str, ...] = ()) -> tuple[int, set[str]]:
    """收集指定**函数作用域内**代码用到的标识符（不扫模块其它函数，避免误伤）。

    选函数的方式：函数名包含 ``name_contains`` 任一词，**或**函数体内调用了
    ``calls_attr`` 里的属性（例如 ``ds._refresh_spot_cache()``）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    picked = 0
    used: set[str] = set()

    def _walk_body(nodes) -> set[str]:  # noqa: ANN001
        found: set[str] = set()
        for node in nodes:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    found.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    found.add(sub.attr)
        return found

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name.lower()
        body_ids = _walk_body(node.body)
        hit = any(word in name for word in name_contains) or any(
            attr in body_ids for attr in calls_attr)
        if hit:
            picked += 1
            used |= body_ids
    return picked, used


def test_v5_14_spot_refresher_never_takes_state_lock():
    """★ 刷新线程源码不得出现锁原语（AST：注释/文档里提到不算，**代码里用**才算）。

    ★ 只扫**刷新函数**：`webapp.py` 全文当然有 `state_lock`（自选池写路径合法使用它），
    判据若扫全文就会**误报**（我第一版就踩了）。扫：
      * `spot_refresher.py` 的全部函数（该模块的职责就是刷新线程）；
      * `webapp.py` 里名字含 spot / 或函数体内调用 `_refresh_spot_cache` 的函数。
    """
    refresher = ROOT / "ptrade" / "spot_refresher.py"
    assert refresher.is_file(), "缺少 ptrade/spot_refresher.py（V5-9 的交付物）"

    picked, used = _function_identifiers(refresher, name_contains=("spot", "refresh", "tick"))
    assert picked > 0, "spot_refresher.py 里找不到任何刷新函数（V5-14 无法验证）"
    bad = sorted(used & set(_FORBIDDEN_LOCK_NAMES))
    assert not bad, f"spot_refresher.py 的刷新函数里出现锁原语 {bad}（INV-42）"

    picked2, used2 = _function_identifiers(
        ROOT / "ptrade" / "webapp.py",
        name_contains=("spot",), calls_attr=("_refresh_spot_cache",))
    assert picked2 > 0, "webapp.py 里找不到 spot 刷新包装函数（V5-14 无法验证）"
    bad2 = sorted(used2 & set(_FORBIDDEN_LOCK_NAMES))
    assert not bad2, (
        f"webapp.py 的 spot 刷新函数里出现锁原语 {bad2}（INV-42：后台刷新不得持写锁；"
        f"共扫到 {picked2} 个相关函数）"
    )


# =========================================================================== #
# V5-15 离线实例：明示不可用，不得显示「计算中」
# =========================================================================== #
def test_v5_15_offline_shows_unavailable_not_computing(env3, cdp_status):
    """★ 离线永远算不完：分析卡必须含「离线」或「不可用」，**不得**含「计算中」。"""
    _, base = env3                      # 用在线服务 + 注入 health.offline=true（前端权威来自服务端）
    init = _cdp.v5_init_js(force_offline=True)
    m = measure_v5(base, init_js=init, wait_for_js=(
        "(() => { const t = document.body ? document.body.innerText : \"\"; "
        "return t.indexOf('离线') >= 0 || t.indexOf('不可用') >= 0; })()"),
        wait_for_js_timeout_ms=15000)
    body = m.get("analysisBodyText") or ""
    state_text = m.get("analysisStateText") or ""
    assert m.get("analysisState") == "offline", (
        f"离线时分析卡状态应为 offline，实际 {m.get('analysisState')!r}"
    )
    assert ("离线" in body) or ("不可用" in body), f"离线模式必须明示不可用：{body!r}"
    # ★ 负向断言只看**状态行**：安全文案本身会引用「不是计算中」来解释为什么不可用
    assert "计算中" not in state_text, (
        f"★ 离线永远算不完，状态行不得显示「计算中」（欺骗）：{state_text!r}"
    )


# =========================================================================== #
# V5-16 刷新不得把自选池跳回顶部
# =========================================================================== #
def test_v5_16_refresh_keeps_watchlist_scroll(env20, cdp_status):
    _, base = env20
    init = _cdp.v5_init_js(force_market_open=True)
    actions = ("(() => { const wl = document.querySelector('#watchlist'); "
               "if (wl) { wl.scrollTop = 200; } return wl ? wl.scrollTop : null; })()")
    m = measure_v5(base, init_js=init, actions_js=actions, actions_wait_ms=600,
                   network_window_ms=12000, wait_for_js=(
                       "(() => { const wl = document.querySelector('#watchlist'); "
                       "return !!(wl && wl.scrollHeight > wl.clientHeight + 1); })()"))
    wl = m.get("watchlist") or {}
    assert wl.get("scrollTop") is not None, "页面没有 #watchlist"
    assert abs(float(wl["scrollTop"]) - 200) <= 2, (
        f"刷新把自选池跳回顶部（scrollTop 200 → {wl['scrollTop']}）"
    )


# =========================================================================== #
# V5-17 页面隐藏时暂停，切回立即刷一次
# =========================================================================== #
def test_v5_17_hidden_pauses_and_resume_refreshes(env3, cdp_status):
    _, base = env3
    init_hidden = _cdp.v5_init_js(force_market_open=True, fake_visibility=True)
    hide_js = "(() => { window.__v5SetHidden(true); return document.hidden; })()"
    m_hidden = measure_v5(base, init_js=init_hidden, actions_js=hide_js,
                          actions_wait_ms=300, network_window_ms=12000)
    hidden_calls = (m_hidden.get("netDelta") or {}).get("/api/quote", 0)
    assert m_hidden.get("visibility", {}).get("hidden") is True, "注入失败：document.hidden 未生效"
    assert hidden_calls == 0, f"页面隐藏时仍轮询了 {hidden_calls} 次 /api/quote"

    show_js = "(() => { window.__v5SetHidden(false); return document.hidden; })()"
    m_visible = measure_v5(base, init_js=_cdp.v5_init_js(force_market_open=True,
                                                         fake_visibility=True),
                           actions_js=show_js, actions_wait_ms=0, network_window_ms=2500,
                           network_from_actions=True)
    visible_calls = (m_visible.get("netDelta") or {}).get("/api/quote", 0)
    assert visible_calls >= 1, f"切回可见后 1.5s 内没有刷新（{visible_calls} 次 /api/quote）"


# =========================================================================== #
# 变异自证（纪律：判据必须能红；spec 要求至少 3 条：M1 / M6 / M9）
# =========================================================================== #
def test_v5_z1_mutation_m1_missing_reasons_is_red():
    """★ M1（不渲染 reasons）：把 reasons 从正文里拿掉 ⇒ V5-1 的核心判据必须红。"""
    reasons = ["解释1" * 5 + "A", "解释2" * 5 + "B", "解释3" * 5 + "C",
               "解释4" * 5 + "D", "解释5" * 5 + "E"]
    good = {"analysisBodyText": " ".join(reasons) + " 风险1", "analysisReasonItems": len(reasons),
            "analysisRiskItems": 1}
    _check_analysis_body_rendered(good, reasons, ["风险1"])      # 对照：应先通过
    empty = {"analysisBodyText": "总分 51.37 · 信号 [观察]", "analysisReasonItems": 0,
             "analysisRiskItems": 0}
    with pytest.raises(AssertionError):
        _check_analysis_body_rendered(empty, reasons, ["风险1"])


def test_v5_z2_mutation_m6_old_bound_gives_up(env3, cdp_status):
    """★ M6（ANALYSIS_POLL_MAX 回 6）：ready 出现在第 35 次响应 ⇒ 旧上界必然到不了。"""
    _, base = env3
    old_calls = _run_fake_analysis(base, poll_ms=20, poll_max=6, ready_at_tick=35)
    assert old_calls <= 7, f"旧上界应只轮询 ≤7 次，实际 {old_calls}"
    assert old_calls < 35, "★ 旧上界（6）竟然到达了第 35 次响应 —— 判据没有牙齿"


def test_v5_z3_mutation_m9_offline_refresh_guard_has_teeth():
    """★ M9（--offline 仍刷 spot）：同一份判据在 should_run=True 时**必须**抓得到刷新。"""
    from ptrade.spot_refresher import SpotRefresher

    calls: list[int] = []
    on = SpotRefresher(refresh=lambda: calls.append(1), should_run=lambda: True,
                       interval_s=0.01)
    on.note_ui_activity()
    assert on.tick() is True and calls == [1], (
        "should_run=True 时刷新器居然不抓 —— 那么「offline 不刷」的判据是空的"
    )
    off_calls: list[int] = []
    off = SpotRefresher(refresh=lambda: off_calls.append(1), should_run=lambda: False,
                        interval_s=0.01)
    off.note_ui_activity()
    assert off.tick() is False and off_calls == [], "should_run=False 时不得抓取"
