# -*- coding: utf-8 -*-
"""W3 · F5 服务端契约（v2）：T-W3-2（图内切换）/ T-W3-3 / T-W3-5 / T-W3-6。

真源：``.omx/plans/test-spec-ptrade-web-v2.md`` §3 与 ``.omx/plans/prd-ptrade-web-v2.md`` §F5。

## 三条「为什么这么测」

1. **图内周期按钮要查 ``/api/chart`` 返回的 HTML，不是 ``APP_HTML``**
   （T-W3-2）：按钮由 ``chart.py`` 渲染、活在 **iframe** 里。R1 评审时 Critic 只 grep 了
   ``webui.py`` 就断言「删掉下拉框即失去唯一入口」，被 Lead 实证否证。本文件正面断言
   **图表 HTML** 里有 ``switchPeriod``，并在 ``test_webui_v2.py`` 反向断言 ``APP_HTML`` 里没有。
2. **请求级 ``?offline=`` 是服务端能力**（T-W3-3 / Critic N2）：用户看到的「离线勾选无效」
   根因就在这里 —— 修前 ``api_chart`` 只解析 code/period/asof，走的是**进程级** offline。
   语义与 ``/api/search`` 一致：**进程级为下界**，请求级只能更严。
3. **``market_open`` 是纯计算**（T-W3-5）：日历只读**缓存条目**。
   判据「``trade_dates()`` 调用次数 == 0」必须配**正对照**（先证明计数器能涨），
   否则它可能是恒真（Architect 第 2 轮同款提醒）。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ptrade import chart as chart_mod  # noqa: E402
from ptrade import service as service_mod  # noqa: E402
from ptrade import webapp as webapp_mod  # noqa: E402
from ptrade.config import load_config  # noqa: E402
from ptrade.datasource import DataSource  # noqa: E402
from ptrade.webapp import WebApp  # noqa: E402

#: 冻结的三态日历（2026-10-14/15/16 都当交易日；15 日是「今天」）
CAL = ["2026-10-14", "2026-10-15", "2026-10-16"]


# --------------------------------------------------------------------------- #
# 夹具与工具（照 tests/test_webapp.py 的成例，但只复制夹具，不建账户）
# --------------------------------------------------------------------------- #
def _copy_fixtures(root: str) -> None:
    for name in ("fixture", "scenario"):
        source = REPO_ROOT / "data" / name
        if not source.is_dir():
            continue
        target = Path(root) / "data" / name
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.suffix == ".json":
                shutil.copy2(item, target / item.name)


@pytest.fixture
def cfg(tmp_path):
    root = str(tmp_path)
    _copy_fixtures(root)
    return load_config(root)


def _app(config, **kw) -> WebApp:
    kw.setdefault("offline", True)
    kw.setdefault("lock_timeout", 5.0)
    return WebApp(config, **kw)


def _call(app: WebApp, method: str, path: str, query=None, body=None):
    """调用 dispatch 并解出 (status, payload)。

    ``/`` 与 ``/api/chart`` 是**唯一两个返回 HTML 的端点**，不是 JSON 信封 ——
    所以非 JSON 正文按原样（字符串）返回，而不是抛 JSONDecodeError。
    """
    response = app.dispatch(method, path, query or {}, body)
    payload = response.payload
    if payload is None:
        text = response.to_bytes().decode("utf-8")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
    return response.status, payload


def _write_calendar(config, dates: list[str]) -> None:
    cache = Path(config.root) / "data" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    DataSource._write_entry(str(cache), "trade_dates", {"rows": list(dates)})


class _FakeDatetime(datetime):
    """注入 webapp 的时钟（``webapp.datetime`` 是模块属性，可换）。"""

    NOW = datetime(2026, 10, 15, 10, 30)

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - 与 datetime.now 同签名
        return cls.NOW


def _freeze(monkeypatch, moment: datetime) -> None:
    class Fake(_FakeDatetime):
        NOW = moment
    monkeypatch.setattr(webapp_mod, "datetime", Fake)


# --------------------------------------------------------------------------- #
# T-W3-2：图内切换仍在（查 /api/chart 的 HTML）
# --------------------------------------------------------------------------- #
def test_t_w3_2b_in_chart_switch_buttons(cfg) -> None:
    """★ 断言取 **`/api/chart` 返回的 HTML**（不是 APP_HTML）—— Critic 就误判在这里。"""
    status, _ = _call(_app(cfg), "GET", "/api/chart", {"code": ["600519"]})
    assert status == 200
    response = _app(cfg).dispatch("GET", "/api/chart", {"code": ["600519"]}, None)
    html = response.to_bytes().decode("utf-8")
    assert html.lstrip().startswith("<!DOCTYPE html>"), "图表端点应内联返回完整 HTML"
    for needle in ("switchPeriod(", 'id="btn-daily"', 'id="btn-weekly"', 'id="btn-monthly"'):
        assert needle in html, f"图内周期切换控件缺失：{needle}"
    # 交叉校验：这些按钮**不该**出现在页面壳里（否则本轮「删下拉框」就是白删）
    from ptrade.webui import APP_HTML

    assert "switchPeriod" not in APP_HTML


def test_t_w3_2b_minute_is_a_separate_branch(cfg) -> None:
    """INV-56：``minute`` 不在 ``chart.PERIODS``（否则所有出图路径一起红）。"""
    assert "minute" not in chart_mod.PERIODS
    assert "minute" in chart_mod.PERIODS_WITH_INTRADAY
    assert tuple(webapp_mod.VALID_PERIODS) == tuple(chart_mod.PERIODS_WITH_INTRADAY), (
        "webapp.VALID_PERIODS 必须是 chart.PERIODS_WITH_INTRADAY 的同一真源（不得各写一份）"
    )


# --------------------------------------------------------------------------- #
# T-W3-3：请求级 offline 生效 + 进程级为下界
# --------------------------------------------------------------------------- #
@pytest.fixture
def ds_recorder(monkeypatch):
    """记录 ``service.make_ds(cfg, offline)`` 收到的 offline 值。"""
    seen: list[bool] = []
    real = service_mod.make_ds

    def spy(config, offline: bool = False):
        seen.append(bool(offline))
        return real(config, offline)

    monkeypatch.setattr(service_mod, "make_ds", spy)
    return seen


@pytest.fixture
def fast_chart(monkeypatch):
    """把渲染换成轻量桩：本组用例只验 offline 管线，不验渲染（渲染另有 W9/W2 覆盖）。

    ★ 必须吃 `**kw`：`/api/chart` 现在还会传 `analysis=`（PN 占位/缓存载荷）。
    """
    monkeypatch.setattr(chart_mod, "render_chart",
                        lambda cfg, ds, code, asof, period="daily", **kw:
                        ("<html data-analysis-state=\"pending\">stub</html>", {"asof": asof}))
    monkeypatch.setattr(chart_mod, "write_chart", lambda path, html: str(path))


def test_t_w3_3b_request_offline_reaches_datasource(cfg, ds_recorder, fast_chart) -> None:
    """``?offline=1`` 必须真的传到 DataSource（修前它被忽略，用户勾选无效）。"""
    ds_recorder.clear()
    status, _ = _call(_app(cfg, offline=False), "GET", "/api/chart",
                      {"code": ["600519"], "offline": ["1"]})
    assert status == 200
    assert ds_recorder, "api_chart 未构造数据源"
    assert ds_recorder[-1] is True, f"请求级 offline=1 未生效：{ds_recorder}"


def test_t_w3_3b_process_offline_is_lower_bound(cfg, ds_recorder, fast_chart) -> None:
    """``--offline`` 起服务时，``?offline=0`` **不得**放宽（下界语义）。"""
    ds_recorder.clear()
    status, _ = _call(_app(cfg, offline=True), "GET", "/api/chart",
                      {"code": ["600519"], "offline": ["0"]})
    assert status == 200
    assert ds_recorder[-1] is True, f"进程级 offline 被请求级解除：{ds_recorder}"


def test_t_w3_3b_default_follows_process_level(cfg, ds_recorder, fast_chart) -> None:
    ds_recorder.clear()
    _call(_app(cfg, offline=False), "GET", "/api/chart", {"code": ["600519"]})
    assert ds_recorder[-1] is False, ds_recorder
    ds_recorder.clear()
    _call(_app(cfg, offline=True), "GET", "/api/chart", {"code": ["600519"]})
    assert ds_recorder[-1] is True, ds_recorder


def test_t_w3_3b_guard_lower_bound_is_real() -> None:
    """**非空洞**：把下界那一项去掉（修前的形态）→ 上面那条必须红。"""
    process_offline = True

    def fixed(query_value):           # 修后：进程级为下界
        return bool(query_value) or process_offline

    def mutated(query_value):         # 变异：只信请求级
        return bool(query_value)

    assert fixed(False) is True, "修后语义：请求级 0 不得解除进程级 offline"
    with pytest.raises(AssertionError):
        assert mutated(False) is True, "进程级 offline 被请求级解除"


def test_r5_ds_seam_is_parameterized(cfg, fast_chart, monkeypatch) -> None:
    """R5（task-28）：数据源缝隙必须**参数化** —— 路由把「有效 offline」交给 ``WebApp._ds(offline)``。

    非空洞：若实现退回 R5 之前的补丁形态（「先判断请求级是否更严，再决定走 ``_ds()``
    还是自建数据源」），请求级收紧那条路就**不会**经过 ``_ds`` → 这里收不到 ``True`` → 红；
    若改成调两次同样红（断言是**恰好一次**）。
    """
    seen: list = []
    real = WebApp._ds

    def spy(self, offline=None):
        seen.append(offline)
        return real(self, offline)

    monkeypatch.setattr(WebApp, "_ds", spy, raising=True)

    # ① 进程在线 + 请求级收紧 → 缝隙必须收到有效值 True（不是 None / False）
    _call(_app(cfg, offline=False), "GET", "/api/chart",
          {"code": ["600519"], "offline": ["1"]})
    assert seen == [True], f"`_ds` 应恰好被调用一次且带有效 offline；实际 {seen}"

    # ② 进程离线 + 请求级放宽 → 下界语义：缝隙仍收到 True
    seen.clear()
    _call(_app(cfg, offline=True), "GET", "/api/chart",
          {"code": ["600519"], "offline": ["0"]})
    assert seen == [True], f"进程级为下界时 `_ds` 仍应收到 True；实际 {seen}"

    # ③ 默认路径（无请求级参数）→ 同一条缝隙收到进程级值
    seen.clear()
    _call(_app(cfg, offline=False), "GET", "/api/chart", {"code": ["600519"]})
    assert seen == [False], f"默认路径应把进程级值交给同一缝隙；实际 {seen}"


# --------------------------------------------------------------------------- #
# T-W3-5：/api/health 增 market_open（纯计算）
# --------------------------------------------------------------------------- #
def test_t_w3_5_health_exposes_market_open_without_fetching(cfg, monkeypatch) -> None:
    """**不触发任何取数**：``trade_dates()`` 调用次数必须为 0。"""
    _write_calendar(cfg, CAL)
    _freeze(monkeypatch, datetime(2026, 10, 15, 10, 30))
    calls = {"n": 0}
    real = DataSource.trade_dates

    def counting(self):
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(DataSource, "trade_dates", counting)
    status, payload = _call(_app(cfg), "GET", "/api/health")
    assert status == 200 and payload["ok"] is True
    assert payload["data"]["market_open"] is True, payload["data"]
    assert calls["n"] == 0, f"health 不得取数（trade_dates() 被调 {calls['n']} 次）"


def test_t_w3_5_guard_counter_can_rise(cfg, monkeypatch) -> None:
    """★ **正对照**：先证明计数器能涨 —— 否则「计数 == 0」是恒真。"""
    _write_calendar(cfg, CAL)
    calls = {"n": 0}
    real = DataSource.trade_dates

    def counting(self):
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(DataSource, "trade_dates", counting)
    ds = DataSource(cfg, offline=True)
    assert ds.trade_dates() == CAL, "离线读缓存日历应成功"
    assert calls["n"] == 1, "计数器没有涨 —— 「调用次数 == 0」这条判据毫无鉴别力"


@pytest.mark.parametrize("calendar,moment,expected,label", [
    (CAL, datetime(2026, 10, 15, 10, 30), True, "盘中（上午）"),
    (CAL, datetime(2026, 10, 15, 14, 0), True, "盘中（下午）"),
    (CAL, datetime(2026, 10, 15, 12, 0), False, "交易日但午休"),
    (CAL, datetime(2026, 10, 15, 16, 0), False, "交易日但已收盘"),
    # ★ 「日历覆盖今天」但今天不是交易日（10-14 被跳过）→ False（**不是** None）
    (["2026-10-13", "2026-10-15", "2026-10-16"], datetime(2026, 10, 14, 10, 30), False,
     "日历覆盖该日但不是交易日"),
])
def test_t_w3_5_market_open_states(cfg, monkeypatch, calendar, moment, expected, label) -> None:
    _write_calendar(cfg, calendar)
    _freeze(monkeypatch, moment)
    _, payload = _call(_app(cfg), "GET", "/api/health")
    assert payload["data"]["market_open"] is expected, f"{label}: {payload['data']}"


def test_t_w3_5_stale_calendar_is_none_not_false(cfg, monkeypatch) -> None:
    """★ 陈旧日历必须归到 ``None``（**不是** False）—— 否则前端停轮询、自动刷新静默死掉。"""
    _write_calendar(cfg, ["2026-10-13", "2026-10-14"])
    _freeze(monkeypatch, datetime(2026, 10, 15, 10, 30))
    _, payload = _call(_app(cfg), "GET", "/api/health")
    assert payload["data"]["market_open"] is None, payload["data"]


def test_t_w3_5_missing_calendar_is_none(cfg, monkeypatch) -> None:
    """无缓存条目（冷启动/从未联网）→ ``None``（不可判定），不得猜。"""
    _freeze(monkeypatch, datetime(2026, 10, 15, 10, 30))
    _, payload = _call(_app(cfg), "GET", "/api/health")
    assert payload["data"]["market_open"] is None, payload["data"]


def test_t_w3_5_existing_health_fields_preserved(cfg, monkeypatch) -> None:
    _write_calendar(cfg, CAL)
    _freeze(monkeypatch, datetime(2026, 10, 15, 10, 30))
    _, payload = _call(_app(cfg), "GET", "/api/health")
    assert payload["command"] == "health"
    for field in ("status", "root", "offline", "bind", "source", "diverged", "consistent",
                  "ledger_consistency", "market_open"):
        assert field in payload["data"], f"health 丢了既有字段：{field}"


def test_t_w3_5_guard_market_open_uses_cache_only(cfg, monkeypatch) -> None:
    """**非空洞**：让 health 去调 ``trade_dates()``（变异形态）→ 「计数 == 0」那条必须红。"""
    _write_calendar(cfg, CAL)
    calls = {"n": 0}

    def counting(self):
        calls["n"] += 1
        return CAL

    monkeypatch.setattr(DataSource, "trade_dates", counting)
    ds = DataSource(cfg, offline=True)
    ds.trade_dates()                      # 变异实现会走这一步
    assert calls["n"] == 1
    with pytest.raises(AssertionError):
        assert calls["n"] == 0, "health 不得取数"


# --------------------------------------------------------------------------- #
# T-W3-6：HTTP 端到端（分时路由 / 业务态 / 首页）
# --------------------------------------------------------------------------- #
def test_t_w3_6_root_serves_app_html(cfg) -> None:
    from ptrade.webui import APP_HTML

    status, payload = _call(_app(cfg), "GET", "/")
    assert status == 200
    assert isinstance(payload, str) and payload.lstrip().startswith("<!DOCTYPE html>"), payload[:80]
    response = _app(cfg).dispatch("GET", "/", {}, None)
    assert response.to_bytes().decode("utf-8") == APP_HTML
    assert "text/html" in response.content_type


def test_t_w3_6_minute_routes_to_render_intraday(cfg, monkeypatch) -> None:
    """★ ``period=minute`` 必须**先**路由到 ``render_intraday``（不得交给 ``render_chart``）。"""
    seen: list[dict] = []

    def fake_intraday(config, ds, code, day=None):
        seen.append({"code": code, "day": day})
        return "<html>intraday</html>", {"day": "2026-10-14", "kind": "intraday"}

    def forbidden_render(*a, **k):
        raise AssertionError("不得把 minute 交给 render_chart（period not in PERIODS → ValueError）")

    monkeypatch.setattr(chart_mod, "render_intraday", fake_intraday)
    monkeypatch.setattr(chart_mod, "render_chart", forbidden_render)

    response = _app(cfg).dispatch("GET", "/api/chart", {"code": ["600519"], "period": ["minute"]}, None)
    assert response.status == 200
    assert response.to_bytes().decode("utf-8") == "<html>intraday</html>"
    assert seen == [{"code": "600519", "day": None}], seen


def test_t_w3_6_guard_minute_would_crash_the_kline_path() -> None:
    """**非空洞**：证明「先路由」不是装饰 —— ``minute`` 不在 ``PERIODS``，
    交给 K 线管线就会炸（``resample_ohlc`` / ``render_chart`` 对非 daily/weekly/monthly 抛 ValueError）。
    """
    assert "minute" not in chart_mod.PERIODS
    with pytest.raises(ValueError):
        chart_mod.resample_ohlc(None, "minute")


def test_t_w3_6_minute_offline_without_cache_is_business_state(cfg) -> None:
    """离线且无分时缓存 → **200 业务态 + 明确 error**（不是 500，也不是空图）。"""
    status, payload = _call(_app(cfg, offline=True), "GET", "/api/chart",
                            {"code": ["600519"], "period": ["minute"]})
    assert status == 200, f"应是业务态 200，实际 {status}"
    assert payload["ok"] is False and payload["error"], payload
    assert "无分时" in str(payload["error"]) or "分时" in str(payload["error"]) or "数据" in str(payload["error"])


def test_t_w3_6_invalid_period_still_400(cfg) -> None:
    status, payload = _call(_app(cfg), "GET", "/api/chart",
                            {"code": ["600519"], "period": ["hourly"]})
    assert status == 400 and "period" in payload["error"], payload


# --------------------------------------------------------------------------- #
# T-W3-7 ★「仅本地监听」硬闸（ultraqa F-2）
# --------------------------------------------------------------------------- #
def test_t_w3_7_non_loopback_host_is_rejected_by_default(cfg) -> None:
    """非回环地址**默认拒绝**；必须显式 `allow_remote=True` 才放行。

    ## 为什么（ultraqa `gate-verifier-3` 实测的 F-2）

    修前 `create_server(cfg, host="0.0.0.0", port=0)` **真的绑成功**
    （实测 `('0.0.0.0', 60886)`），只有启动横幅**警告一次** —— 而网页端**无鉴权**
    （`banner()` 自己写着「同网段的任何人都可以查看账户、下单、平仓与结算」）。
    ⇒ 「仅本地监听」是**用户级硬约束**，修前**只有一句提示、没有硬闸**。

    ★ 判据要点（**非空洞**）：
    * 只断言「`0.0.0.0` 被拒」是不够的 —— 若实现**一律拒绝**，那也过。
      **必须配正对照**：回环地址**必须能绑成功**，且 `allow_remote=True` 时 `0.0.0.0` 也能绑。
    * 保守策略：`example.com` 这类**名字**一律拒绝（不做 DNS 解析 —— 能解析到非回环的名字
      不该被当作本地）。
    """
    # ① 回环 → 必须允许（正对照，防「一律拒绝」也通过）
    for host in ("127.0.0.1", "localhost", "127.0.0.2"):
        srv = webapp_mod.create_server(cfg, host=host, port=0, offline=True)
        try:
            assert srv.server_address[0] in ("127.0.0.1", "127.0.0.2", "localhost"), host
        finally:
            srv.server_close()

    # ② 非回环 → 默认**必须拒绝**
    #    ★ architect 第 3 轮实测：第一版的 `startswith("127.")` 把**名字**也判成回环
    #      （`127.evil.com` / `127.0.0.1.nip.io` / `127.0.0.1.attacker.net` 全为 True），
    #      而 docstring 承诺「只认白名单、不做 DNS 解析」—— **文档说 A、代码做 B**。
    #      `create_server` 随后把 host 交给 `getaddrinfo` ⇒ 能解析到非回环的名字**穿过硬闸**。
    #      → 下面是回归名字（`nip.io` 是**真实的通配 DNS**：`127.0.0.1.nip.io` 解析到 127.0.0.1，
    #        但 `127.evil.com` 这类**由攻击者控制的域**可以解析到任意地址）。
    for host in ("0.0.0.0", "192.168.1.5", "example.com",
                 "127.evil.com", "127.0.0.1.attacker.net"):
        with pytest.raises(ValueError) as exc:
            webapp_mod.create_server(cfg, host=host, port=0, offline=True)
        msg = str(exc.value)
        assert "无鉴权" in msg and "allow-remote" in msg, f"{host} 的拒绝理由应可操作：{msg}"

    # ②b **数字形态**的回环仍必须放行（改名/收紧谓词后**不得**误伤）
    for host in ("127.1.2.3", "127.0.0.2"):
        srv = webapp_mod.create_server(cfg, host=host, port=0, offline=True)
        try:
            assert srv.server_address[0] == host, host
        finally:
            srv.server_close()

    # ②c 谓词本身：名字一律不认、数字按 `ipaddress` 判
    assert webapp_mod._is_loopback("127.0.0.1") is True
    assert webapp_mod._is_loopback("127.1.2.3") is True          # 127/8 整段
    assert webapp_mod._is_loopback("127.0.0.1.nip.io") is False  # 是名字，不是 IP 字面量
    assert webapp_mod._is_loopback("127.evil.com") is False
    assert webapp_mod._is_loopback("::1") is True

    # ③ 显式放行 → **必须允许**（否则②可能是「怎么都拒绝」而假绿）
    srv = webapp_mod.create_server(cfg, host="0.0.0.0", port=0, offline=True, allow_remote=True)
    try:
        assert srv.server_address[0] == "0.0.0.0"
    finally:
        srv.server_close()


def test_t_w3_7b_cli_serve_rejects_non_loopback_without_allow_remote(cfg, capsys) -> None:
    """CLI 侧同样有硬闸，且返回**可操作的退出码 2**（不是抛栈）。"""
    from ptrade import cli

    args = cli.build_parser().parse_args(["serve", "--host", "0.0.0.0", "--port", "0", "--offline"])
    rc = cli.cmd_serve(args, cfg)
    assert rc == 2, f"非回环且未加 --allow-remote 时应返回 2，实际 {rc}"
    err = capsys.readouterr().err
    assert "allow-remote" in err, f"错误提示应点明 --allow-remote：{err}"

    # 正对照：显式加 --allow-remote 后**不再**被 CLI 层拦下
    calls: list[dict] = []
    real_serve = webapp_mod.serve
    webapp_mod.serve = lambda *a, **k: (calls.append(k), 0)[1]      # type: ignore[assignment]
    try:
        args2 = cli.build_parser().parse_args(
            ["serve", "--host", "0.0.0.0", "--port", "0", "--offline", "--allow-remote"]
        )
        rc2 = cli.cmd_serve(args2, cfg)
    finally:
        webapp_mod.serve = real_serve                              # type: ignore[assignment]
    assert rc2 == 0, f"显式放行后应进入 serve（rc=0），实际 {rc2}"
    assert calls and calls[0].get("allow_remote") is True, (
        f"`--allow-remote` 必须透传到 webapp.serve，实际收到 {calls}"
    )


# =========================================================================== #
# PN（性能 · 分析异步化）—— T-PN-1/2/9/10/10b/12 + G2/G3（task-35）
#
# 背景实测：冷缓存下一次分析 57–153s，`/api/chart` 原先**同步**等它 ⇒ 首屏 180.71s。
# 修法：占位页**立即返回** + **有界**后台 daemon 线程算 + `/api/analysis` 小 JSON 报状态，
# 前端以 `state == "pending"` 为**续轮条件**做有界轮询、`ready` 后**只刷一次** iframe。
# =========================================================================== #
import gzip  # noqa: E402
import inspect  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import socket as _socket  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from ptrade import store as store_mod  # noqa: E402

#: conftest 的 `_no_network` 会封掉 socket.connect；T-PN-12 的「端口先就绪」需要真 loopback
_REAL_SOCKET_CONNECT = _socket.socket.connect
_REAL_CREATE_CONNECTION = _socket.create_connection

_MARKER_RE = re.compile(r'data-analysis-state="(?P<state>\w+)"')


def _asof_for(cfg, code: str = "600519") -> str:
    """该 code 的**看盘 asof**（与 `/api/chart` 内部同一入口）—— 测试用它去查缓存/轮询。

    为什么不从页面 HTML 里抠：`<body>` 上的 marker 只承载**状态**（R2 冻结契约），
    asof 该由**同一个解析入口**给出，抠文本会引入「文案一变、测试就红」的脆弱耦合。
    """
    return str(service_mod.viewer_asof(DataSource(cfg, offline=True), code, None, []))[:10]


def _make_cold(cfg, code: str, asof: str) -> None:
    """**主动**把该 ``code|asof`` 置冷 —— 判据**不该依赖「恰好还没被写」**。

    为什么需要（code-review F1 实测）：后台分析在夹具环境 **<1s** 就落盘，于是
    「先发请求（它会起后台线程）、再断言缓存是空的」这种写法是**反的** ——
    单跑 4 次里 1 次红；整文件与同伴并发时 2/2 红。

    ★ 只删**这一个字面量文件**（路径由 code/asof 算出）：**不做**任何通配/前缀批量删除
      （本项目已因此出过两次事故）。同时清掉该 ``root|code`` 可能残留的 job 记录，
      免得旧记录把新任务挡成 pending。
    """
    cache_file = (Path(webapp_mod.cache_dir_of(cfg))
                  / f"{webapp_mod.analysis_cache_key(code, asof)}.json")
    if cache_file.exists():
        cache_file.unlink()
    with webapp_mod._ANALYSIS_JOBS_LOCK:
        webapp_mod._ANALYSIS_JOBS.pop(webapp_mod._job_key(cfg, code), None)


def _chart_page(app, code="600519", **extra):
    response = app.dispatch("GET", "/api/chart", {"code": [code], **extra}, None)
    html = response.to_bytes().decode("utf-8")
    marker = _MARKER_RE.search(html)
    return response.status, html, (marker.group("state") if marker else None)


def _wait_until(predicate, timeout=90.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------- #
# T-PN-1：首屏不被分析区阻塞（核心）
# --------------------------------------------------------------------------- #
def test_tpn1_first_screen_is_immediate_with_pending_analysis(cfg) -> None:
    """★ 冷缓存下 `/api/chart` 必须**立即**返回可看的图 + pending 占位。"""
    app = _app(cfg)
    asof = _asof_for(cfg)
    _make_cold(cfg, "600519", asof)                      # ★ 主动构造冷状态（不假设）
    assert webapp_mod.read_cached_analysis(cfg, "600519", asof) is None, "本用例前提是**冷缓存**"
    t0 = time.time()
    status, html, state = _chart_page(app)
    dt = time.time() - t0
    assert status == 200 and dt <= 5.0, f"首屏 {dt:.2f}s > 5s —— 仍被分析区阻塞"
    for title in ("① K线", "② BOLL", "③ 成交量", "④ 成交额", "⑤ MACD", "⑥ SAR"):
        assert title in html, f"缺面板 {title}"
    assert state == "pending", f"marker 必须 pending，实际 {state!r}"
    assert "计算中" in html, "分析区必须是明确的「计算中」占位，而不是空白/旧数据"
    print(f"  [T-PN-1] 首屏 {dt:.2f}s / {len(html):,} 字节 / marker={state} / asof={asof}")


# --------------------------------------------------------------------------- #
# T-PN-2：后台算完后第二次请求是完整页面
# --------------------------------------------------------------------------- #
def test_tpn2_second_request_has_full_analysis(cfg) -> None:
    app = _app(cfg)
    asof = _asof_for(cfg)
    # ★ **冷断言必须在发请求之前**（F1）：`_chart_page` 会起后台分析线程，夹具环境 <1s 就写完
    #   —— 「先发请求、再断言冷」是**反的**，并发/单跑都会偶发假红。
    _make_cold(cfg, "600519", asof)
    assert webapp_mod.read_cached_analysis(cfg, "600519", asof) is None      # 非空洞：先证冷
    _, _, state = _chart_page(app)
    assert state == "pending", f"首屏 marker 应为 pending（= 当时确实未命中），实际 {state!r}"
    cached = _wait_until(lambda: webapp_mod.read_cached_analysis(cfg, "600519", asof))
    assert cached is not None, "后台任务在 90s 内没有写缓存"
    assert cached.get("ok") is True, cached
    status, html, state2 = _chart_page(app)
    assert status == 200 and state2 == "ready", f"marker 应为 ready，实际 {state2!r}"
    assert "计算中" not in html, "缓存已就绪，页面不该再显示「计算中」"
    assert "⑦ 分析区" in html
    for key in ("C", "A", "N", "S", "L", "I", "M"):
        assert key in html, f"完整分析缺少因子 {key}"
    print(f"  [T-PN-2] 后台完成 → 第二次 marker={state2}，七因子齐全")


def test_tpn2b_cached_path_matches_fresh_render(cfg) -> None:
    """缓存路径与现算路径的渲染输入应当一致（R5：先 `jsonable` 再比）。"""
    app = _app(cfg)
    _, _, _ = _chart_page(app)
    asof = _asof_for(cfg)
    cached = _wait_until(lambda: webapp_mod.read_cached_analysis(cfg, "600519", asof))
    assert cached is not None
    fresh = service_mod.jsonable(chart_mod._score_analysis(cfg, DataSource(cfg, offline=True),
                                                           "600519", asof))
    assert fresh == cached, "缓存载荷与现算载荷不一致（同一份 JSON 才是同一份渲染输入）"


def test_tpn2c_analysis_endpoint_works_without_asof(cfg) -> None:
    """★ 不带 `asof` 也能收口：后台算完后，`/api/analysis?code=X` 必须能**发现**结果。

    这是前端的实际调用形态（`webui` 轮询只带 code + offline）。若这里永远 pending，
    用户就会看到「分析永远在算」—— 与「25s 只重试一次」同族的缺陷。
    """
    app = _app(cfg)
    _, _, state = _chart_page(app)
    assert state == "pending"

    def ready():
        payload = json.loads(app.dispatch("GET", "/api/analysis", {"code": ["600519"]}, None).to_bytes())
        return payload["data"] if payload["data"]["state"] == "ready" else None

    data = _wait_until(ready, timeout=90)
    assert data is not None, "不带 asof 的轮询永远 pending（缺少「发现已算 asof」这一步）"
    assert data["asof"], f"发现结果后必须回填 asof：{data}"
    print(f"  [T-PN-2c] 无 asof 轮询 → ready，asof={data['asof']}")


# --------------------------------------------------------------------------- #
# T-PN-3（新口径）：并发分析调用**有界** —— 把「上界」写成契约
#
# 背景（Architect 验尸）：Critic 的「删 3 项」删掉了**分析级 single-flight**
# （理由：`DataSource._cached` 已有同 key 单飞）。但计划四处仍引用 `T-PN-3` 而全仓无该实体
# ⇒ `INV-60` 的判据清单里曾有一条**纸面守卫**。新口径：不做 single-flight，但把
# **「最坏 2× 重复计算、不出错值、不排队」这个上界钉住**。
#
# ★ **不重写**「同 key 只取一次数」—— 那条已存在：
#   `tests/test_datasource_v2.py::test_T_W1_10…`（锁外 8 路并发同 key → 真取数恰好 1 次）。
#   这里测的是**另一件事**：**后台 job 的并发上界**（`ANALYSIS_MAX_WORKERS`）。
# --------------------------------------------------------------------------- #
def test_tpn3_concurrent_analysis_calls_are_bounded(cfg, monkeypatch) -> None:
    """并发 → `_score_analysis` 调用有界 + 同 ``code|asof`` 只落 1 份缓存。

    判据分两半（**都很重要**）：
    * **同 code 并发 5 次** ⇒ 调用 1 次、缓存 1 份（任务级去重先把它挡到 1）；
    * **5 个不同 code 并发** ⇒ 后台**并发峰值** ≤ 2 —— 这才是信号量那条上界的可观测形态
      （同 code 时看到的是去重，不是信号量，所以必须用**不同**工作项才能测到它）。

    ★ 断言用的是**契约冻结值 2**，**不引用常量本身** `ANALYSIS_MAX_WORKERS` ——
      否则「把常量调大」会同时放宽判据，变异永远绿（自指式判据）。
    ★ 本判据**不受机器负载影响**（计的是**调用次数**，不是耗时）。
    """
    counter = {"calls": 0, "live": 0, "peak": 0}
    real = chart_mod._score_analysis

    def counting(*args, **kwargs):
        counter["calls"] += 1
        counter["live"] += 1
        counter["peak"] = max(counter["peak"], counter["live"])
        try:
            time.sleep(0.3)                     # 拉开窗口，让「并发」真的可能发生
            return real(*args, **kwargs)
        finally:
            counter["live"] -= 1

    monkeypatch.setattr(chart_mod, "_score_analysis", counting)

    # ① **正对照**：直接调一次 → 计数器必须涨（否则「≤2」可能只是「一次都没跑」= 空守卫）
    asof = _asof_for(cfg)
    chart_mod._score_analysis(cfg, DataSource(cfg, offline=True), "600519", asof)
    assert counter["calls"] == 1, f"正对照失败：直接调用后计数器 = {counter['calls']}"
    counter.update(calls=0, live=0, peak=0)

    # ② **主动构造冷态**（不假设「恰好还没被写」—— F1 那条竞态的教训）
    _make_cold(cfg, "600519", asof)
    assert webapp_mod.read_cached_analysis(cfg, "600519", asof) is None

    # ③ 同 code 并发 5 次
    app = _app(cfg)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = [f.result() for f in [pool.submit(_chart_page, app) for _ in range(5)]]
    assert all(status == 200 for status, _, _ in results), results

    # ④ 等后台空闲 → 断言上界（契约冻结 2）
    assert webapp_mod.ANALYSIS_MAX_WORKERS == 2, "契约冻结：后台并发上界 = 2（Critic R4）"
    cached = _wait_until(lambda: webapp_mod.read_cached_analysis(cfg, "600519", asof), timeout=90)
    assert cached is not None, "并发 5 次后缓存仍未落盘"
    _wait_until(lambda: not dict(webapp_mod._ANALYSIS_JOBS), timeout=60)
    assert 1 <= counter["calls"] <= 2, (
        f"同 code|asof 的 `_score_analysis` 调用 {counter['calls']} 次（上界 2，下界 1）"
    )
    same_code_calls = counter["calls"]

    # ⑤ 同一 code|asof 只落 **1 份**缓存
    files = sorted(p.name for p in
                   Path(webapp_mod.cache_dir_of(cfg)).glob("analysis_600519_*.json"))
    assert files == [f"analysis_600519_{asof}.json"], files

    # ④b 信号量上界的**可观测形态**：5 个**不同** code 并发 → 后台并发峰值 ≤ 2
    counter.update(calls=0, live=0, peak=0)
    codes = ["600519", "601398", "600110", "002594", "600036"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda c: _chart_page(app, c), codes))
    _wait_until(lambda: not dict(webapp_mod._ANALYSIS_JOBS), timeout=90)
    assert counter["peak"] <= 2, (
        f"后台**并发** `_score_analysis` 峰值 {counter['peak']} > 上界 2 —— 信号量没生效"
    )
    print(f"  [T-PN-3] 同 code 并发 5 次 → 调用 {same_code_calls} 次（≤2）；"
          f"5 个不同 code 并发 → 峰值 {counter['peak']} ≤ 2；缓存 1 份 = {files[0]}")


# --------------------------------------------------------------------------- #
# T-PN-9：失败态可见、带原因、且**不得永久 pending**
# --------------------------------------------------------------------------- #
def test_tpn9_failure_is_visible_with_reason(cfg, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("boom-analysis")

    monkeypatch.setattr(chart_mod, "_score_analysis", boom)
    app = _app(cfg)
    _, _, state = _chart_page(app)
    asof = _asof_for(cfg)
    assert state == "pending" and asof

    def failed():
        data = json.loads(app.dispatch("GET", "/api/analysis",
                                       {"code": ["600519"], "asof": [asof]}, None).to_bytes())["data"]
        return data if data["state"] == "failed" else None

    data = _wait_until(failed, timeout=60)
    assert data is not None, "分析抛错后状态永远停在 pending（必须变 failed）"
    assert "boom-analysis" in str(data.get("error")), f"失败态必须带原因文案：{data}"
    _, html, state2 = _chart_page(app)
    assert state2 == "failed"
    assert "boom-analysis" in html, "页面必须把失败原因显示出来（不得静默消失）"
    print(f"  [T-PN-9] state=failed，error={str(data['error'])[:60]!r}")


def test_pn_failed_state_always_carries_a_reason(cfg, monkeypatch) -> None:
    """★ G4（ultraqa **F-FAILMSG**）：`failed` 的 `data.error` **必须非空** —— 两种形态都测。

    实测（复核 ultraqa 的发现）：
    * **形态①生产路径**：后台任务抛错 → 写 `ANALYSIS_FAILED("后台分析失败：…")`
      → `/api/analysis` 的 `data.error` **非空** ✓（原因确实落在 `data.error`，前端 toast 也读它）；
    * **形态②注入形态**：缓存载荷**只有 `ok=False`、没有 `error` 字段** → `data.error` 曾是
      `None`（`{"state": "failed", "error": null, …}`）⇒ 那是**真缺口**（G4 要求无条件带原因）。

    现在：载荷有原因就**原样透出**（不覆盖生产文案），没有就**兜底合成**。
    """
    app = _app(cfg)
    asof = _asof_for(cfg)
    _make_cold(cfg, "600519", asof)

    # 形态②：塞一个「没有 error 字段」的失败载荷
    webapp_mod.write_cached_analysis(cfg, "600519", asof, {"ok": False, "pending": False})
    payload = json.loads(app.dispatch("GET", "/api/analysis",
                                      {"code": ["600519"], "asof": [asof]}, None).to_bytes())
    data = payload["data"]
    assert data["state"] == "failed", data
    assert data["error"], f"G4：失败态必须带原因文案，实际 {data}"
    assert "未提供原因" in data["error"] and "600519" in data["error"], data

    # 形态①：生产路径的原因必须**原样透出**（不被兜底覆盖）
    _make_cold(cfg, "600519", asof)

    def boom(*_a, **_k):
        raise RuntimeError("boom-failmsg")

    monkeypatch.setattr(chart_mod, "_score_analysis", boom)
    _chart_page(app)
    got = _wait_until(lambda: json.loads(
        app.dispatch("GET", "/api/analysis", {"code": ["600519"], "asof": [asof]},
                     None).to_bytes())["data"], timeout=60)
    assert got["state"] == "failed" and "boom-failmsg" in str(got["error"]), got
    print(f"  [G4] 形态② 兜底原因={data['error'][:40]!r}；形态① 生产原因={str(got['error'])[:40]!r}")


def test_pn_analysis_ready_carries_seven_factors(cfg) -> None:
    """★ V3（Lead 真机发现的回退）：`ready` 时 `/api/analysis` **必须带七因子 + 总分 + 信号**。

    修前只返 `state`，前端于是显示「分析：就绪」而总分/因子全是 `—`（**状态说就绪、数据是空的**）。
    """
    app = _app(cfg)
    asof = _asof_for(cfg)
    _make_cold(cfg, "600519", asof)
    _chart_page(app)

    def ready():
        data = json.loads(app.dispatch("GET", "/api/analysis",
                                       {"code": ["600519"], "asof": [asof]}, None).to_bytes())["data"]
        return data if data["state"] == "ready" else None

    data = _wait_until(ready, timeout=90)
    assert data is not None, "分析始终没到 ready"
    assert data.get("total") is not None, f"ready 但 total 为空（前端会显示 —）：{data}"
    assert str(data.get("signal") or "").strip(), f"ready 但 signal 为空：{data}"
    factors = data.get("factors") or {}
    assert sorted(factors) == ["A", "C", "I", "L", "M", "N", "S"], f"七因子不全：{factors}"
    assert all(isinstance(v, (int, float)) for v in factors.values()), factors
    print(f"  [V3] ready → total={data['total']:.2f} signal={data['signal']} "
          f"factors={ {k: round(float(v), 1) for k, v in factors.items()} }")


def test_tpn9b_leaked_job_key_self_heals(cfg) -> None:
    """★ **自愈**：job 记录被泄漏（超 `ANALYSIS_JOB_STALE_S` 仍挂着）→ 必须允许重开。

    否则任何异常路径漏掉一次 `pop`，该 ``code|asof`` 就**永远 pending**（T-PN-9 的变异形态）。
    """
    app = _app(cfg)
    _, _, _ = _chart_page(app)
    asof = _asof_for(cfg)
    key = webapp_mod._job_key(cfg, "600519")
    with webapp_mod._ANALYSIS_JOBS_LOCK:
        webapp_mod._ANALYSIS_JOBS[key] = time.monotonic() - webapp_mod.ANALYSIS_JOB_STALE_S - 1.0
    cached = _wait_until(lambda: webapp_mod.read_cached_analysis(cfg, "600519", asof), timeout=60)
    assert cached is not None, "陈旧 job key 把该 code|asof 永久锁成 pending（缺少自愈）"
    print("  [T-PN-9b] 陈旧 job key 未阻塞重算 → 自愈成立")


# --------------------------------------------------------------------------- #
# T-PN-10 / 10b：后台不持写锁（静态 + 行为双判据）
# --------------------------------------------------------------------------- #
def test_tpn10_background_has_no_lock_primitives_static() -> None:
    """**静态**：后台函数体内不得出现写锁原语（它压根不该碰账户状态）。"""
    src = inspect.getsource(webapp_mod._analysis_background)
    for bad in ("state_lock", "locked_update", "store.lock", "with self._lock"):
        assert bad not in src, f"后台分析函数体出现 {bad}（它只该读行情 + 写缓存文件）"


def test_tpn10b_background_completes_while_main_holds_write_lock(cfg) -> None:
    """**行为**：主线程持写锁时后台分析**照常完成**（证明它不等锁）。

    ★ 为什么必须双判据：`in_critical_section` 是**线程局部**的（`store._LOCK_DEPTH`），
      后台线程「天然不在临界区」只是**巧合级安心**；用行为把它变成**被守住的**性质。
    """
    app = _app(cfg)
    result: dict = {}
    with store_mod.state_lock(str(cfg.root), timeout=5.0):
        assert store_mod.in_critical_section() is True, "正对照：主线程确实在临界区里"
        _, _, state = _chart_page(app)
        asof = _asof_for(cfg)
        assert state == "pending" and asof
        result["cached"] = _wait_until(
            lambda: webapp_mod.read_cached_analysis(cfg, "600519", asof), timeout=60)
        result["still_locked"] = store_mod.in_critical_section()
    assert result["cached"] is not None, "主线程持写锁期间后台分析没能完成（它在等锁）"
    assert result["still_locked"] is True, "自检：等待期间锁应始终由主线程持有"
    print("  [T-PN-10b] 主线程持写锁期间后台分析正常完成")


def test_pn_analysis_background_guard_error_is_visible_then_raised(cfg, monkeypatch) -> None:
    """★ 守卫错误：**先写可见失败载荷**（页面不会永远 pending）**再原样 raise**（不静默）。

    Lead 指出「写可见失败载荷」是**行为**而非语法 —— 所以必须有行为判据：
    * 断言 ①：`/api/analysis` 读到 `state == "failed"` 且 `error` 含 `NETWORK_IN_LOCK`
      （UI 不会卡在「永远在算」）；
    * 断言 ②：异常**确实**被抛出（`threading.excepthook` 收到），没有被宽 handler 吞掉。

    **非空洞**：删掉「先写载荷」→ ① 红；把 `raise` 改成 `pass` → `test_W1`（语法级）红。
    """
    from ptrade.datasource import NetworkForbiddenInLock

    captured: list[BaseException] = []

    def hook(args) -> None:                     # 捕获 daemon 线程的未处理异常（不打印，免得污染输出）
        value = getattr(args, "exc_value", None)
        captured.append(value if value is not None else getattr(args, "exc_type", Exception))

    monkeypatch.setattr(threading, "excepthook", hook)

    def guard_boom(*_a, **_k):
        raise NetworkForbiddenInLock("注入：写锁临界区内禁止联网取数")

    monkeypatch.setattr(chart_mod, "_score_analysis", guard_boom)
    app = _app(cfg)
    status, _, state = _chart_page(app)
    assert status == 200 and state == "pending", (status, state)

    def failed():
        data = json.loads(app.dispatch("GET", "/api/analysis",
                                       {"code": ["600519"]}, None).to_bytes())["data"]
        return data if data["state"] == "failed" else None

    data = _wait_until(failed, timeout=60)
    assert data is not None, "守卫错误后页面永远 pending —— 失败载荷没有落盘（① 红）"
    assert "NETWORK_IN_LOCK" in str(data.get("error")), f"失败原因必须可见且可诊断：{data}"

    got = _wait_until(lambda: captured or None, timeout=30)
    assert got, "后台线程**吞掉**了守卫错误（threading.excepthook 没有收到）—— ② 红"
    assert isinstance(captured[0], NetworkForbiddenInLock), captured
    print(f"  [PN 守卫] ① state=failed error={str(data['error'])[:48]!r} ② 线程 excepthook 收到 {type(captured[0]).__name__}")


# --------------------------------------------------------------------------- #
# T-PN-12：预热是**一条**任务、**不阻塞**端口就绪、**跟随 --offline**
# --------------------------------------------------------------------------- #
def _startup_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("ptrade-startup")]


def test_tpn12_prewarm_is_single_task_and_does_not_block_port(cfg, monkeypatch) -> None:
    monkeypatch.setattr(_socket.socket, "connect", _REAL_SOCKET_CONNECT)
    monkeypatch.setattr(_socket, "create_connection", _REAL_CREATE_CONNECTION)
    trace: dict = {"threads": [], "done": False}
    real_cleanup = webapp_mod.cleanup_stale_cache

    def slow_cleanup(_cfg, **_kw):
        trace["threads"].append(threading.current_thread().name)
        time.sleep(2.0)                       # 模拟「预热还没做完」
        trace["done"] = True
        return real_cleanup(_cfg)

    monkeypatch.setattr(webapp_mod, "cleanup_stale_cache", slow_cleanup)
    before = len(_startup_threads())
    t0 = time.time()
    server = webapp_mod.create_server(cfg, host="127.0.0.1", port=0, offline=True)
    bind_s = time.time() - t0
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        host, port = server.server_address[0], int(server.server_address[1])
        assert bind_s < 1.0, f"create_server 花了 {bind_s:.2f}s —— 预热阻塞了端口就绪"
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True, payload
        # ★ 关键：端口**已经**在应答，而预热**还没做完** ⇒ 预热没有阻塞启动
        assert trace["done"] is False, "预热在应答前就做完了 —— 本用例没验到「不阻塞」"
        assert trace["threads"] and trace["threads"][0] != "MainThread", "预热跑在主线程里"
        _wait_until(lambda: trace["done"], timeout=20)
        assert trace["done"] is True, "预热没有完成（异常被吞了？）"
        assert len(set(trace["threads"])) == 1, f"预热被拆成多条任务：{set(trace['threads'])}"
        _wait_until(lambda: len(_startup_threads()) == before, timeout=10)
        assert len(_startup_threads()) == before, "预热线程结束后仍未收口"
    finally:
        server.shutdown()
        server.server_close()
    print(f"  [T-PN-12] 绑定 {bind_s:.3f}s；端口先应答、预热后完成（线程 {set(trace['threads'])}，1 条）")


def test_tpn12b_prewarm_follows_offline(cfg, monkeypatch) -> None:
    """★★ Critic G1：`serve --offline` 下预热**不得联网** —— 数据源必须是 offline 的。"""
    seen_ds: list[bool] = []
    seen_names: list[bool] = []
    real_make = service_mod.make_ds

    def spy_make(config, offline=False, **kw):
        seen_ds.append(bool(offline))
        return real_make(config, offline, **kw)

    monkeypatch.setattr(service_mod, "make_ds", spy_make)

    import ptrade.search as search_mod

    real_table = search_mod.load_name_table

    def spy_table(config, *, offline, **kw):
        seen_names.append(bool(offline))
        return real_table(config, offline=offline, **kw)

    monkeypatch.setattr(search_mod, "load_name_table", spy_table)
    thread = webapp_mod.start_startup_maintenance(cfg, True)
    thread.join(timeout=30)
    assert not thread.is_alive(), "预热线程 30s 未结束"
    assert seen_ds and all(seen_ds), f"预热用了在线数据源：{seen_ds}"
    assert seen_names and all(seen_names), f"名称表预热没跟随 offline：{seen_names}"
    assert thread.daemon is True, "预热线程必须是 daemon（不得拖住进程退出）"
    print(f"  [T-PN-12b] make_ds(offline={seen_ds}) / load_name_table(offline={seen_names})")


# --------------------------------------------------------------------------- #
# G3：启动按 mtime 清理旧缓存（**只碰自己的前缀**）
# --------------------------------------------------------------------------- #
def test_g3_cleanup_only_touches_its_own_prefixes(cfg) -> None:
    cache = Path(cfg.root) / "data" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    old = time.time() - 8 * 24 * 3600
    files = {
        "analysis_600519_2026-01-01.json": old,
        "analysis_600519_2026-10-09.json": time.time(),
        "chart_asof_600519_pre_close.json": old,
        "stock_list.json": old,            # ★ 别人的缓存：**不得**被删
        "trade_dates.json": old,           # ★ 同上
    }
    for name, mtime in files.items():
        path = cache / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (mtime, mtime))
    removed = webapp_mod.cleanup_stale_cache(cfg)
    assert removed == 2, f"应删 2 个（两个过期前缀文件），实际 {removed}"
    assert not (cache / "analysis_600519_2026-01-01.json").exists()
    assert not (cache / "chart_asof_600519_pre_close.json").exists()
    assert (cache / "analysis_600519_2026-10-09.json").exists(), "新文件被误删"
    assert (cache / "stock_list.json").exists(), "★ 通配删除：别人的缓存被删了"
    assert (cache / "trade_dates.json").exists(), "★ 通配删除：交易日历被删了"
    print(f"  [G3] 清理 {removed} 个过期分析/asof 缓存；stock_list/trade_dates 未被触碰")


def test_g3_cleanup_keeps_newest_even_if_old(cfg) -> None:
    """第二道闸：**最新的 keep_newest 个**即使超龄也不删（防一次跑偏扫光）。"""
    cache = Path(cfg.root) / "data" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    old = time.time() - 30 * 24 * 3600
    for i in range(5):
        path = cache / f"analysis_X_2026-01-0{i + 1}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (old + i, old + i))
    removed = webapp_mod.cleanup_stale_cache(cfg, max_age_s=3600.0, keep_newest=2)
    assert removed == 3, f"5 个文件、保留 2 个 → 应删 3 个，实际 {removed}"
    assert len(list(cache.glob("analysis_X_*.json"))) == 2


# --------------------------------------------------------------------------- #
# G2：渲染并发有上限（狂刷新不得 N×1 MB）
# --------------------------------------------------------------------------- #
def test_g2_render_concurrency_is_bounded(cfg, monkeypatch) -> None:
    monkeypatch.setattr(webapp_mod, "RENDER_WAIT_S", 0.05)
    held = [webapp_mod._RENDER_SEM.acquire(blocking=False)
            for _ in range(webapp_mod.RENDER_MAX_CONCURRENCY)]
    assert all(held), "本用例需要先占满渲染名额"
    try:
        response = _app(cfg).dispatch("GET", "/api/chart", {"code": ["600519"]}, None)
        body = response.to_bytes()
    finally:
        for _ in held:
            webapp_mod._RENDER_SEM.release()
    assert response.status == 200, response.status
    assert len(body) < 4096, f"饱和时仍渲染了 {len(body):,} 字节整页（G2 未生效）"
    text = body.decode("utf-8")
    assert "渲染繁忙" in text and 'data-analysis-state="pending"' in text
    print(f"  [G2] 渲染名额饱和 → {len(body)} 字节骨架页（而非 1 MB 整页）")


def test_pn_analysis_endpoint_is_small_json(cfg) -> None:
    """`/api/analysis` 必须是**小 JSON**（Critic 反对「marker 驱动 1 MB 重渲染」）。

    ★ v5（test-spec R2-7 / R3-I）：本用例原先**无条件**断言 `< 512 B`，而它只在 ``pending``
    下成立 —— v5 的 ``ready`` 载荷要带 `recommend`（七字段）+ 8 条 reasons/risks，
    天然 ~1.5 KB ⇒ 无条件上限会**间歇假红**。现按状态分档：

    * ``pending``：仍应是「几百字节」⇒ 上限 **1024**（v5 新增 ``elapsed_s`` 是**用户可见的
      进度**，不是泄漏，所以从 512 放宽到 1024 是**有理由的**而不是放水）；
    * ``ready`` / ``failed``：分析正文本身就在载荷里 ⇒ 只守「远小于 1 MB 整页」的上限
      （100 KB），**不是**「几百字节」的适用对象。
    """
    status, payload = _call(_app(cfg), "GET", "/api/analysis", {"code": ["600519"]})
    assert status == 200 and payload["ok"] is True
    data = payload["data"]
    assert set(data) >= {"state", "error", "asof", "code"}, data
    assert data["state"] in ("pending", "ready", "failed")
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if data["state"] == "pending":
        assert len(raw) < 1024, f"pending 的 /api/analysis 返回了 {len(raw)} 字节（应为几百字节）"
    else:
        assert len(raw) < 100_000, (
            f"/api/analysis（{data['state']}）返回 {len(raw)} 字节 —— 分析正文应留在小 JSON 里，"
            "但绝不该接近 1 MB 整页（那说明又被塞了整页内容）"
        )


def test_pn_analysis_requires_code(cfg) -> None:
    status, payload = _call(_app(cfg), "GET", "/api/analysis")
    assert status == 400 and "code" in payload["error"], payload


# --------------------------------------------------------------------------- #
# 前端有界轮询（**真跑 JS**）：≤6 次 / 总 ≤2min / ready 后**只刷一次** / failed 停
# --------------------------------------------------------------------------- #
POLL_HARNESS = r"""
"use strict";
var __els = {};
function __mk(id) {
  var el = {
    id: id, value: "", checked: false, textContent: "", innerHTML: "", className: "",
    style: {}, _src: "", sets: [], addEventListener: function () {},
    removeAttribute: function () {}, setAttribute: function () {},
    getAttribute: function (n) { return n === "src" ? this._src : null; },
    appendChild: function () {}, querySelector: function () { return null; },
    classList: { add: function () {}, remove: function () {}, toggle: function () {} }
  };
  Object.defineProperty(el, "src", {
    get: function () { return this._src; },
    set: function (v) { this._src = v; this.sets.push(v); }
  });
  return el;
}
var document = {
  getElementById: function (id) { return __els[id] || (__els[id] = __mk(id)); },
  createElement: function (tag) { return __mk(tag); },
  querySelectorAll: function () { return []; },
  addEventListener: function () {}
};
var window = { confirm: function () { return true; }, addEventListener: function () {} };
var __timers = []; var __seq = 0;
var setTimeout = function (fn, ms) { var id = ++__seq; __timers.push({ id: id, fn: fn, ms: ms || 0 }); return id; };
var clearTimeout = function (id) { __timers = __timers.filter(function (t) { return t.id !== id; }); };
var setInterval = function () { return 0; };
var __pollStates = []; var __polls = 0;
var fetch = function (url) {
  var body = { ok: true, command: "health", data: { status: "ok", market_open: false }, error: null };
  if (String(url).indexOf("/api/analysis") >= 0) {
    __polls += 1;
    var st = __pollStates.length ? __pollStates.shift() : "pending";
    body = { ok: true, command: "analysis",
             data: { state: st, asof: "2026-10-09", code: "600519",
                     error: st === "failed" ? "boom-analysis" : null }, error: null };
  }
  return Promise.resolve({ status: 200, text: function () { return Promise.resolve(JSON.stringify(body)); } });
};
__JS__
globalThis.__probe = {
  scheduleAnalysisPoll: scheduleAnalysisPoll, ANALYSIS: ANALYSIS, els: __els, timers: __timers,
  polls: function () { return __polls; }, setStates: function (s) { __pollStates = s; },
  runDue: function () { var t = __timers.shift(); if (!t) { return null; } t.fn(); return t.ms; },
  pending: function () { return __timers.length; },
  MAX: ANALYSIS_POLL_MAX, MS: ANALYSIS_POLL_MS
};
"""

POLL_PROBE = r"""
var P = globalThis.__probe;
function el(id, props) { var e = document.getElementById(id); Object.keys(props || {}).forEach(function (k) { e[k] = props[k]; }); return e; }
var frame = el("chart-frame", {});
function drive(states) {
  P.setStates(states.slice());
  P.timers.length = 0;
  P.ANALYSIS.done = false; P.ANALYSIS.n = 0;
  frame.sets.length = 0;
  document.getElementById("chart-code").value = "600519";
  loadChart("daily");                    /* 真实入口：设置 iframe src + 启动轮询 */
  P.scheduleAnalysisPoll("600519", "1"); /* 明确重置一次，避免定时器叠加 */
  return (async function () {
    var steps = 0;
    while (P.pending() && steps < 40) {
      P.runDue();
      await new Promise(function (r) { Promise.resolve().then(r); });
      await new Promise(function (r) { Promise.resolve().then(r); });
      steps += 1;
    }
    return { polls: P.polls(), frameSets: frame.sets.length, done: P.ANALYSIS.done,
             timers: P.pending(), n: P.ANALYSIS.n,
             lastSrc: frame.sets.length ? frame.sets[frame.sets.length - 1] : null };
  })();
}
globalThis.__run = async function () {
  var before = P.polls();
  var out = {};
  out.ready = await drive(["pending", "pending", "ready"]);
  out.ready.pollsThisRun = out.ready.polls - before;
  var base = P.polls();
  out.alwaysPending = await drive(["pending"]);
  out.alwaysPending.pollsThisRun = out.alwaysPending.polls - base;
  out.max = P.MAX; out.ms = P.MS; out.totalBudgetMs = P.MAX * P.MS;
  out.failed = await drive(["failed"]);
  return out;
};
"""


def _run_poll_probe(tmp_path: Path) -> dict:
    from ptrade.webui import APP_HTML

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", APP_HTML, re.S)
    js = max((s for s in scripts if s.strip()), key=len)
    src = (POLL_HARNESS.replace("__JS__", js) + POLL_PROBE
           + "globalThis.__run().then(function (r) { console.log(JSON.stringify(r)); });")
    path = tmp_path / "poll_probe.js"
    path.write_text(src, encoding="utf-8")
    proc = subprocess.run([shutil.which("node"), str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, f"node 失败：{(proc.stderr or '')[:800]}"
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    assert lines, f"探针没有输出：{proc.stdout[:300]!r}"
    return json.loads(lines[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能真跑前端 JS")
def test_pn_frontend_polling_is_bounded_and_never_reloads_the_page(tmp_path) -> None:
    """★ 前端（**V3 改写**）：轮询有界；`ready` 后**渲染小面板**，**不再重载图表面板**。

    修前（PN 阶段）这里断言 `frameSets == 2` ——「ready 后刷一次 iframe」。
    V3-A5 去掉了嵌套框架：图表在本页，分析就绪只需更新 `#analysis-panel`，
    **整页/图表重载为零**（这也正是 Critic 反对的「1 MB 重渲染」被彻底消除的证据）。
    """
    result = _run_poll_probe(tmp_path)
    ready = result["ready"]
    assert ready["pollsThisRun"] >= 2, f"没有真的轮询：{ready}"
    assert ready["done"] is True and ready["timers"] == 0, "ready 之后**不得**再排下一次"
    # ★ **V5 改写（需求变更，不是放宽）**：`ANALYSIS_POLL_MAX` 6→60、`ANALYSIS_POLL_MS` 20000→10000。
    #   原因：**实测 ready ≈132 秒**，旧窗口 `6×20000+1200 ≈ 101s` **覆盖不了** ⇒
    #   用户永远看不到分析（＝用户报的「算完不刷新，要切股票再切回来」）。
    #   ⇒ 上限**从源码读**（不写死），窗口改为「**必须覆盖真实耗时** 且 **仍然有界**」。
    from ptrade.webui import APP_HTML

    m_max = re.search(r"ANALYSIS_POLL_MAX\s*=\s*(\d+)", APP_HTML)
    m_ms = re.search(r"ANALYSIS_POLL_MS\s*=\s*(\d+)", APP_HTML)
    assert m_max and m_ms, "前端必须仍有两个轮询常量（可注入但要有默认）"
    poll_max, poll_ms = int(m_max.group(1)), int(m_ms.group(1))
    assert result["alwaysPending"]["pollsThisRun"] <= poll_max, (
        f"一直 pending 时轮询了 {result['alwaysPending']['pollsThisRun']} 次（上限 {poll_max}）"
    )
    assert result["max"] <= poll_max, result
    # ★ 窗口必须**覆盖实测耗时**（132s 的 4 倍以上），但**仍然有界**（≤20 分钟）。
    assert result["totalBudgetMs"] >= 600_000, (
        f"轮询窗口 {result['totalBudgetMs']}ms < 600s ⇒ 覆盖不了实测 132s 的分析耗时"
    )
    assert result["totalBudgetMs"] <= 1_200_000, (
        f"轮询窗口 {result['totalBudgetMs']}ms > 20min ⇒ 有界性失守"
    )
    assert result["failed"]["timers"] == 0, "failed 之后不得继续轮询"

    assert "refreshChartFrame" not in APP_HTML, "ready 后**不得**再重载图表（A5 已无框架可刷）"
    assert "CHART_FRAME" not in APP_HTML, "旧的框架句柄必须彻底删除"
    # ★ V5：`renderAnalysis` 已被 `analysisView`（纯函数）+ `applyAnalysis`（唯一写入口）取代。
    assert "function applyAnalysis(" in APP_HTML, "ready 时必须经唯一写入口 applyAnalysis 渲染"
    assert "function analysisView(" in APP_HTML, "analysisView 纯函数必须存在（可单测/无 DOM 依赖）"
    assert "setOption(" in APP_HTML, "图表必须通过同页 ECharts 更新"
    print(f"  [PN 前端] ready 路径 polls={ready['pollsThisRun']} 次（**零整页重载**）；"
          f"上限 {result['max']} 次 × {result['ms']}ms = {result['totalBudgetMs'] / 1000:.0f}s；"
          f"一直 pending 时 {result['alwaysPending']['pollsThisRun']} 次后停")


# --------------------------------------------------------------------------- #
# F-GZIP（ultraqa 的低优先发现）：大响应按需 gzip
# --------------------------------------------------------------------------- #
def test_pn_gzip_is_opt_in_and_only_for_large_bodies(cfg, monkeypatch) -> None:
    """★ 传输层：整页 ≈1.17 MB；**客户端请求 gzip 时**才压，且**只压大响应**。

    三条判据（缺一条都会变成「改了 HTTP 层但没人守」）：
    ① 请求头带 `Accept-Encoding: gzip` → `Content-Encoding: gzip` + `Vary: Accept-Encoding`，
       且**解压后与不压缩路径逐字节相同**（用静态的 `/` 做等价比较，避免分析状态变化引入噪声）；
    ② **不请求就不压**（`urllib` 默认 identity → 行为一字不变）；
    ③ **小响应（<1 KB）不压**：`/api/analysis` 只有 133 字节，压了反而更大。
    """
    monkeypatch.setattr(_socket.socket, "connect", _REAL_SOCKET_CONNECT)
    monkeypatch.setattr(_socket, "create_connection", _REAL_CREATE_CONNECTION)
    server = webapp_mod.create_server(cfg, host="127.0.0.1", port=0, offline=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[0], int(server.server_address[1])

        def get(path: str, *, gzip_ok: bool) -> tuple[bytes, dict]:
            headers = {"Accept-Encoding": "gzip"} if gzip_ok else {}
            request = urllib.request.Request(f"http://{host}:{port}{path}", headers=headers)
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.read(), dict(resp.headers)

        # ①/② 静态首页：压缩路径解压后必须与未压路径**逐字节相同**
        packed, head_gz = get("/", gzip_ok=True)
        plain, head_plain = get("/", gzip_ok=False)
        assert head_gz.get("Content-Encoding") == "gzip", head_gz.get("Content-Encoding")
        assert head_gz.get("Vary") == "Accept-Encoding", head_gz.get("Vary")
        assert head_plain.get("Content-Encoding") is None, "没请求 gzip 就不该压"
        assert gzip.decompress(packed) == plain, "解压后与不压缩路径内容不一致"
        assert len(packed) < len(plain) * 0.5, f"压缩比不足：{len(packed)} vs {len(plain)}"

        # ③ 大整页（图表，≈1.17 MB）也压；解压后仍是完整页面
        chart_packed, head_chart = get("/api/chart?code=600519", gzip_ok=True)
        assert head_chart.get("Content-Encoding") == "gzip"
        chart_html = gzip.decompress(chart_packed).decode("utf-8")
        assert chart_html.lstrip().startswith("<!DOCTYPE html>")
        for title in ("① K线", "② BOLL", "③ 成交量", "④ 成交额", "⑤ MACD", "⑥ SAR"):
            assert title in chart_html, f"解压后缺面板 {title}"

        # ④ 小响应不压（阈值 `_GZIP_MIN_BYTES` = 1024）
        #   ★★ v3 修订（**不是放宽**：换被测对象 + 加非空洞前置 + 补反向半边）：
        #     原用 `/api/analysis`（当时 133 B）；v3 修「分析区全横杠」后，`ready` 载荷
        #     **带上了七因子** ⇒ 实测涨到 1,568 B ⇒ **超过阈值、被压是正确行为**，
        #     所以「它必须不压」这条**前提已经不成立**。
        #     改用 `/api/health`（316 B，稳定小）作为「小响应」的被测对象，
        #     并用**阈值常量**做前置：将来它若也变大，这里会**明确报错**
        #     （而不是悄悄测了个大响应还宣称「小响应不压」通过）。
        small, head_small = get("/api/health", gzip_ok=True)
        assert len(small) < webapp_mod._GZIP_MIN_BYTES, (
            f"本判据的被测对象必须真的小于阈值；实际 {len(small)} B "
            f"≥ {webapp_mod._GZIP_MIN_BYTES} —— 请换一个更小的端点（**不要删判据**）"
        )
        assert head_small.get("Content-Encoding") is None, "小于阈值的小响应不该被压"
        assert json.loads(small)["ok"] is True

        # ④b ★ 反向半边（原来缺）：**超过阈值就必须压**
        #    `/api/analysis` 现在 1,568 B > 1024 ⇒ 必须带 gzip；否则说明阈值逻辑反了。
        _, head_an = get("/api/analysis?code=600519", gzip_ok=True)
        assert head_an.get("Content-Encoding") == "gzip", (
            "超过 _GZIP_MIN_BYTES 的响应**必须**被压（否则阈值逻辑反了）"
        )
        print(f"  [F-GZIP] 首页 {len(plain):,} → {len(packed):,} 字节（{100 * len(packed) / len(plain):.1f}%）；"
              f"图表页 {len(chart_packed):,} 字节（gzip）；小响应 /api/health {len(small)} B 未压 ✓")
    finally:
        server.shutdown()
        server.server_close()

