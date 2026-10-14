# -*- coding: utf-8 -*-
"""tests/test_webapp.py —— W3 网页服务契约测试（test-spec §4 的 W1–W11）。

测试策略
--------
* 绝大多数用例直接驱动 ``WebApp.dispatch()``（无 socket、确定性、可并发），HTTP 层只做协议适配；
  另有**真实 loopback HTTP** 用例覆盖协议适配、状态码与内联 HTML。
* **W6 / W7 是本任务的重点**，都带非空守卫：
  - ``test_w6_guard_naive_store_loses_updates`` 用**无锁** store 跑同一场景，断言它**必须丢更新**
    （若它不丢，说明 W6 的断言没有牙齿 → 该用例红）；
  - ``test_w7_*`` 用**真实子进程**持有写锁，证明网页写路径确实被跨进程锁挡住。
* **W1（store.py）未落地时**：本模块在导入期注入一个符合 PRD §5.1 签名的**临时**假 store，
  并有 ``test_real_store_is_in_use`` 作为**显式红灯守门**（shim 生效时该用例必红）——
  避免"假 store 静默长期存在"。W1 落地后 shim 自动失效（真 store 优先）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket as _socket
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ptrade.config import load_config  # noqa: E402
from ptrade import core  # noqa: E402
from ptrade.cli import build_parser  # noqa: E402
from ptrade.datasource import DataSource  # noqa: E402
from ptrade import webapp  # noqa: E402
from ptrade.webapp import WebApp, banner  # noqa: E402

# 供 loopback HTTP 用例恢复真实连接能力（conftest 的 _no_network 会封掉 socket.connect）
_REAL_SOCKET_CONNECT = _socket.socket.connect
_REAL_CREATE_CONNECTION = _socket.create_connection





# --------------------------------------------------------------------------- #
# 夹具与工具
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
def web_cfg(tmp_path):
    """隔离数据根（tmp）：真实夹具 + 已建仓账户（非空持仓，W2 才有意义）。

    日期用 :func:`_pinned_trade_date`（**有数据**的最近交易日）而不是
    ``latest_trade_date()`` —— 后者是日历最新日，可能没有日线数据
    （2026-10-13 实测：日历给 2026-10-12，日线止于 2026-10-09）。
    """
    _copy_fixtures(str(tmp_path))
    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=True)
    latest = _pinned_trade_date(cfg)
    prev = ds.prev_trade_date(latest)
    price = ds.last_close("600519", prev)
    state = core.new_state(cfg)
    order = core.Order(
        order_id=core.next_order_id(state), ts=f"{prev}T15:00:00", trade_date=prev,
        code="600519", name="贵州茅台", side=core.Side.BUY.value, qty=100, price=price,
    )
    commission, tax, transfer = core.calc_fees(order.side, price, order.qty, cfg.fee)
    order.fee, order.tax, order.transfer_fee = commission, tax, transfer
    plan = core.fill_plan(state, order, cfg.margin, cfg.fee)
    order.cash_delta, order.margin_used = plan["cash_delta"], plan["margin_used"]
    order.debt_delta, order.realized_pnl = plan["debt_delta"], plan["realized_pnl"]
    state = core.apply_fill(state, order, cfg.margin, cfg.fee)
    state.history.append(order)
    state = core.settle_t1(state, latest)
    _write_state(cfg, state)
    return cfg


def _write_state(cfg, state) -> str:
    path = os.path.join(cfg.root, "data", "state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(core.state_document(state), fh, ensure_ascii=False, indent=2)
    return path


def _read_state_doc(cfg) -> dict:
    with open(os.path.join(cfg.root, "data", "state.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_state(cfg):
    """state.json → AccountState（用 core 的同一反序列化器）。"""
    return core.state_from_document(_read_state_doc(cfg), cfg)


def _ledger_rows(cfg) -> list[dict]:
    path = os.path.join(cfg.root, "data", "ledger.jsonl")
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _app(cfg, **kw) -> WebApp:
    kw.setdefault("offline", True)
    kw.setdefault("lock_timeout", 5.0)
    return WebApp(cfg, **kw)


def _call(app: WebApp, method: str, path: str, query=None, body=None):
    """调用 dispatch 并解出 (status, payload)。"""
    response = app.dispatch(method, path, query or {}, body)
    payload = response.payload
    if payload is None:
        payload = json.loads(response.to_bytes().decode("utf-8"))
    return response.status, payload


def _pinned_trade_date(cfg) -> str:
    """本文件统一使用的交易日：**日线数据真正覆盖到**的最近一天。

    不能用 ``latest_trade_date()``：它读交易日历（覆盖到 2026-12-31），而日线夹具止于
    2026-10-09。系统日期一往前走，它给出的日期就没有行情，``quote()`` 标
    ``suspended=True`` → 引擎按 ``SUSPENDED`` 拒单（2026-10-13 实测本文件 5 个用例失败）。
    理由与实现见 ``tests/_guards.py::price_asof``。
    """
    from tests._guards import price_asof

    return price_asof(DataSource(cfg, offline=True))


def _buy(app: WebApp, code="000001", qty=100, **extra):
    """下单助手：**显式钉住交易日**（不省略 ``trade_date``）。

    省略 ``trade_date`` 会走 ``resolve_asof`` → 拿到「日历最新日」，
    而那可能没有行情数据（见 :func:`_pinned_trade_date`）。
    """
    extra.setdefault("trade_date", _pinned_trade_date(app.cfg))
    body = {"code": code, "side": "buy", "qty": qty, **extra}
    return _call(app, "POST", "/api/order", body=body)


class _NaiveStore:
    """**故意不加锁**的 store 替身：用于证明 W6 的断言有牙齿（必须丢更新）。"""

    def __init__(self, real, window: float = 0.01):
        self._real = real
        self._window = window
        self.StateLockTimeout = real.StateLockTimeout

    def locked_update(self, cfg, mutate, *, source="cli", timeout=10.0):
        state = self._real.read_state(cfg)
        time.sleep(self._window)                # 放大 read-modify-write 窗口
        new_state = mutate(state)
        if new_state is not None:
            self._real.save_state(cfg, new_state, source=source)
        return new_state

    def __getattr__(self, name):
        return getattr(self._real, name)


# --------------------------------------------------------------------------- #
# 守门：W1 地基必须是真 store（且 API 与 PRD §5.1 一致）
# --------------------------------------------------------------------------- #
def test_real_store_contract_is_in_use():
    """W3 依赖 W1 的真 store；同时锁住 §5.1 的冻结 API（缺一即红）。"""
    import ptrade.store as store

    assert getattr(store, "__is_shim__", False) is False, "不得使用假 store 交付"
    assert store.LOCK_FILE_NAME == ".ptrade.lock"
    for name in ("state_lock", "atomic_write_text", "atomic_write_json", "append_jsonl",
                 "read_state", "save_state", "locked_update", "StateLockTimeout"):
        assert hasattr(store, name), f"store 缺少 §5.1 冻结 API：{name}"
    assert store.StateLockTimeout.error_code == "STATE_LOCK_TIMEOUT"
    assert issubclass(store.StateLockTimeout, RuntimeError)


# --------------------------------------------------------------------------- #
# W1 / W10：探活与启动
# --------------------------------------------------------------------------- #
def test_w1_health_ok(web_cfg):
    status, payload = _call(_app(web_cfg), "GET", "/api/health")
    assert status == 200
    assert payload["ok"] is True
    assert payload["command"] == "health"
    assert payload["data"]["status"] == "ok"
    assert payload["error"] is None


def test_w1_health_over_real_http(web_cfg, monkeypatch):
    """真实 loopback HTTP：覆盖协议适配层（仅本机回环，不是联网）。"""
    import urllib.request

    monkeypatch.setattr(_socket.socket, "connect", _REAL_SOCKET_CONNECT, raising=False)
    monkeypatch.setattr(_socket, "create_connection", _REAL_CREATE_CONNECTION, raising=False)
    server = webapp.create_server(web_cfg, host="127.0.0.1", port=0, offline=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=10) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True and payload["command"] == "health"
        # 未预期端点 → 404（且仍是同一信封）
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://{host}:{port}/api/nope", timeout=10)
        assert excinfo.value.code == 404
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert body["ok"] is False and "未知端点" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_w10_default_bind_and_warning_banner():
    assert webapp.DEFAULT_HOST == "127.0.0.1"
    local = banner("127.0.0.1", 8765, offline=True, root=r"D:\x")
    assert "127.0.0.1:8765" in local
    assert "警告" not in local
    exposed = banner("0.0.0.0", 8765, offline=False, root=r"D:\x")
    assert "警告" in exposed and "没有鉴权" in exposed
    assert "0.0.0.0:8765" in exposed


def test_w10_health_reports_real_bound_address_not_defaults(web_cfg, monkeypatch):
    """**回归**：``/api/health`` 必须报**真实**监听地址，不得回显默认常量。

    曾经写死 ``DEFAULT_HOST:DEFAULT_PORT`` —— 于是 ``--port 8791`` 起服务时它仍报
    ``127.0.0.1:8765``，W-10「A1 可机械判定」的判据当场失效。修复过程中的**第一次尝试
    也是错的**：我把地址挂在 HTTP handler 的 ``self.server`` 上，但 ``api_health``
    实际是 ``WebApp`` 的方法（其上没有 ``server`` 属性），补丁静默无效 ——
    所以这条测试必须走**真实 socket**，而不是只断言常量。

    ``port=0``（系统分配）同样要覆盖：真实端口只能从 ``server_address`` 读出。
    """
    import urllib.request

    monkeypatch.setattr(_socket.socket, "connect", _REAL_SOCKET_CONNECT, raising=False)
    monkeypatch.setattr(_socket, "create_connection", _REAL_CREATE_CONNECTION, raising=False)

    for requested in (0, 8799):
        server = webapp.create_server(web_cfg, host="127.0.0.1", port=requested, offline=True)
        bound_port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{bound_port}/api/health", timeout=10
            ) as resp:
                assert resp.status == 200
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)

        assert payload["ok"] is True
        assert payload["data"]["bind"] == f"127.0.0.1:{bound_port}", (
            f"请求端口 {requested} 实际绑定 {bound_port}，"
            f"但 health 报 {payload['data']['bind']} —— 回显了默认常量"
        )
        if requested == 0:
            assert bound_port != webapp.DEFAULT_PORT, "port=0 应由系统分配非默认端口"


# --------------------------------------------------------------------------- #
# W2 / W3：与 CLI 逐字段一致（INV-40）
# --------------------------------------------------------------------------- #
def _cli_data(argv: list[str], cfg, capsys) -> dict:
    parser = build_parser()
    args = parser.parse_args([*argv, "--root", cfg.root])
    handler = cli_handlers()[argv[0]]
    assert handler(args, cfg) == 0
    out = capsys.readouterr().out
    return json.loads(out)["data"]


def cli_handlers() -> dict:
    from ptrade import cli

    return cli._HANDLERS


def test_w2_state_matches_status_json(web_cfg, capsys):
    """INV-40：``/api/state`` 的 data 必须与 ``ptrade status --json`` 的 data 逐字段一致。"""
    cli_data = _cli_data(["status", "--json"], web_cfg, capsys)
    # 非空守卫：账户必须有持仓，否则"都是 0"也能过
    assert cli_data["positions"], "夹具账户必须非空（否则 W2 是空守卫）"
    status, payload = _call(_app(web_cfg), "GET", "/api/state")
    assert status == 200 and payload["ok"] is True
    assert payload["data"] == cli_data, {
        "只差字段": ({k: (payload["data"].get(k), cli_data.get(k))
                   for k in set(payload["data"]) | set(cli_data)
                   if payload["data"].get(k) != cli_data.get(k)})
    }
    assert payload["data"]["positions"][0]["code"] == "600519"


def test_w2_state_without_account_is_business_error(tmp_path):
    _copy_fixtures(str(tmp_path))
    cfg = load_config(str(tmp_path))
    status, payload = _call(_app(cfg), "GET", "/api/state")
    assert status == 200 and payload["ok"] is False
    assert "init" in payload["error"]
    assert payload["data"] == {}


def test_w3_risk_matches_cli_and_null_ratio(web_cfg, capsys):
    cli_data = _cli_data(["risk", "--json", "--offline"], web_cfg, capsys)
    status, payload = _call(_app(web_cfg), "GET", "/api/risk")
    assert status == 200 and payload["ok"] is True
    assert payload["data"]["maintenance_ratio"] is None          # 无负债 → null（不是 inf）
    assert cli_data["maintenance_ratio"] is None
    assert set(payload["data"]) == set(cli_data)
    assert payload["data"]["alerts"] == cli_data["alerts"]
    assert payload["data"]["status"] == cli_data["status"]


def test_w3_risk_with_debt_reports_numeric_ratio(web_cfg):
    state = core.state_from_document(_read_state_doc(web_cfg), web_cfg)
    state.financing_debt = 200000.0                              # 造出负债场景
    state = core.refresh_risk_flags(state, web_cfg.risk, web_cfg.margin)
    _write_state(web_cfg, state)
    status, payload = _call(_app(web_cfg), "GET", "/api/risk")
    assert status == 200 and payload["ok"] is True
    ratio = payload["data"]["maintenance_ratio"]
    assert isinstance(ratio, float) and ratio > 0
    assert payload["data"]["exposure"]["max_leverage"] == web_cfg.risk.max_leverage


# --------------------------------------------------------------------------- #
# W4 / W5：参数非法 400；业务拒单 200
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body,needle", [
    ({"side": "buy", "qty": 100}, "code"),
    ({"code": "000001", "qty": 100}, "side"),
    ({"code": "000001", "side": "hold", "qty": 100}, "side 非法"),
    ({"code": "000001", "side": "buy"}, "qty"),
    ({"code": "000001", "side": "buy", "qty": "abc"}, "qty"),
    ({"code": "000001", "side": "buy", "qty": 10.5}, "qty"),
    ({"code": "000001", "side": "buy", "qty": 150}, "100 的整数倍"),
    ({"code": "000001", "side": "buy", "qty": 0}, "正整数"),
    ({"code": "000001", "side": "buy", "qty": -100}, "正整数"),
])
def test_w4_invalid_order_params_return_400(web_cfg, body, needle):
    status, payload = _call(_app(web_cfg), "POST", "/api/order", body=body)
    assert status == 400, (status, payload)
    assert payload["ok"] is False and needle in payload["error"]
    assert _ledger_rows(web_cfg) == []                           # 参数非法不得落任何单


def test_w4_invalid_price_and_settle_params(web_cfg):
    app = _app(web_cfg)
    for bad in ("abc", -1, 0, float("nan")):
        status, payload = _call(app, "POST", "/api/order",
                                body={"code": "000001", "side": "buy", "qty": 100, "price": bad})
        assert status == 400 and "price" in payload["error"], (bad, payload)
    status, payload = _call(app, "POST", "/api/settle", body={})
    assert status == 400 and "trade_date" in payload["error"]
    status, payload = _call(app, "GET", "/api/chart", query={"period": ["hourly"]})
    assert status == 400 and "code" in payload["error"]
    status, payload = _call(app, "GET", "/api/chart", query={"code": ["600519"], "period": ["hourly"]})
    assert status == 400 and "period" in payload["error"]


def test_w5_business_rejection_is_200_t1(web_cfg):
    """W5：当日买入当日卖出 → 200 + ok=true + rejected + T1_NOT_SETTLED。"""
    app = _app(web_cfg)
    status, payload = _buy(app, code="000001", qty=100)
    assert status == 200 and payload["ok"] is True
    assert payload["data"]["orders"][0]["status"] == "filled"
    trade_date = payload["data"]["trade_date"]

    status, payload = _call(app, "POST", "/api/order", body={
        "code": "000001", "side": "sell", "qty": 100, "trade_date": trade_date,
    })
    assert status == 200, payload
    assert payload["ok"] is True                                  # 业务拒单不是 HTTP 错误
    order = payload["data"]["orders"][0]
    assert order["status"] == "rejected"
    assert order["error_code"] == "T1_NOT_SETTLED"
    assert payload["error"] is None


# --------------------------------------------------------------------------- #
# W6：并发写不丢更新（+ 非空守卫）
# --------------------------------------------------------------------------- #
def _hammer(app: WebApp, threads: int = 8, qty: int = 100) -> list[tuple[int, dict]]:
    results: list[tuple[int, dict]] = [None] * threads            # type: ignore[list-item]
    barrier = threading.Barrier(threads)

    def _worker(index: int) -> None:
        barrier.wait()
        results[index] = _buy(app, code="000001", qty=qty)

    workers = [threading.Thread(target=_worker, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    return results


def test_w6_concurrent_orders_no_lost_update(web_cfg):
    """W6：8 线程并发下单 → ledger 行数 == 成功单数，且 state 与 ledger 一致。"""
    app = _app(web_cfg)
    results = _hammer(app, threads=8, qty=100)
    assert all(item is not None for item in results), "有线程超时未返回"
    filled = [r for r in results if r[0] == 200 and r[1]["ok"]
              and r[1]["data"]["orders"][0]["status"] == "filled"]
    assert len(filled) == 8, [(s, p.get("error")) for s, p in results]

    rows = _ledger_rows(web_cfg)
    ledger_qty = sum(int(r["qty"]) for r in rows if r["status"] == "filled" and r["code"] == "000001")
    assert len(rows) == 8, f"ledger 行数({len(rows)}) != 成功单数(8) → 丢更新"
    assert ledger_qty == 800

    state = _load_state(web_cfg)
    position = state.positions.get("000001")
    assert position is not None, "state 里没有 000001 持仓"
    assert position.long_qty == 800, f"state 只记到 {position.long_qty} 股 < ledger 的 800 股"
    assert len([h for h in state.history
                if h.code == "000001" and h.status == "filled"]) == 8


def test_w6_guard_naive_store_loses_updates(web_cfg, monkeypatch):
    """**非空守卫**：把 store 换成无锁实现后，W6 的不变量**必须**被破坏。

    若该用例失败，说明 W6 的断言没有牙齿（锁其实不是必需的，或并发没有真正发生）。
    """
    import ptrade.store as real_store

    monkeypatch.setattr(webapp, "_store", lambda: _NaiveStore(real_store, window=0.01))
    app = _app(web_cfg)
    _hammer(app, threads=8, qty=100)

    rows = _ledger_rows(web_cfg)
    state = _load_state(web_cfg)
    position = state.positions.get("000001")
    state_qty = 0 if position is None else int(position.long_qty)
    ledger_qty = sum(int(r["qty"]) for r in rows if r["status"] == "filled" and r["code"] == "000001")
    lost = len(rows) < 8 or state_qty < ledger_qty or ledger_qty < 800
    assert lost, (
        f"无锁实现竟然没丢更新（ledger 行数={len(rows)}, state={state_qty}, ledger={ledger_qty}）"
        "—— W6 的断言没有牙齿，必须重构该守卫"
    )


# --------------------------------------------------------------------------- #
# W7：与 CLI（其它进程）互斥 → 503
# --------------------------------------------------------------------------- #
_HOLDER_SCRIPT = r"""
import os, sys, time
root, ready, release = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, sys.argv[4])
from ptrade import store
with store.state_lock(root, timeout=60):
    with open(ready, "w", encoding="utf-8") as fh:
        fh.write("locked")
    deadline = time.time() + 60
    while time.time() < deadline and not os.path.exists(release):
        time.sleep(0.05)
"""


def test_w7_lock_held_by_other_process_returns_503(web_cfg, tmp_path):
    """W7：另一个**进程**（模拟 CLI）持锁时，网页写 API 必须 503 且不落单。"""
    ready = tmp_path / "lock_ready"
    release = tmp_path / "lock_release"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, web_cfg.root, str(ready), str(release), str(REPO_ROOT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "子进程未能取得写锁"

        app = _app(web_cfg, lock_timeout=0.3)
        status, payload = _call(app, "POST", "/api/order",
                                body={"code": "000001", "side": "buy", "qty": 100})
        assert status == 503, (status, payload)
        assert payload["ok"] is False
        assert "STATE_LOCK_TIMEOUT" in payload["error"]
        # 只读端点不被写锁阻塞（INV-34 / W11）
        assert _call(app, "GET", "/api/state")[0] == 200
        assert _ledger_rows(web_cfg) == []
    finally:
        release.write_text("go", encoding="utf-8")
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:                     # pragma: no cover
            proc.kill()


def test_w7_lock_timeout_maps_to_503_mechanism(web_cfg, monkeypatch):
    """机制层（不依赖真 store）：``StateLockTimeout`` 必须映射为 503 + error_code。"""
    import ptrade.store as store

    def _boom(cfg, mutate, *, source="cli", timeout=10.0):
        raise store.StateLockTimeout("STATE_LOCK_TIMEOUT: 模拟锁超时")

    monkeypatch.setattr(webapp, "_store", lambda: _SimpleProxy(store, locked_update=_boom))
    app = _app(web_cfg)
    for path, body in (("/api/order", {"code": "000001", "side": "buy", "qty": 100}),
                       ("/api/settle", {"trade_date": "2026-10-09"})):
        status, payload = _call(app, "POST", path, body=body)
        assert status == 503, (path, status, payload)
        assert "STATE_LOCK_TIMEOUT" in payload["error"]
    assert _ledger_rows(web_cfg) == []


class _SimpleProxy:
    def __init__(self, real, **overrides):
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


def test_w11_read_endpoints_not_blocked_by_write_lock(web_cfg):
    """W11：写锁被他人持有时，只读端点仍 200；写端点 503（INV-34 + W7）。

    注意：写锁**同进程同线程可重入**，所以持锁者必须来自**另一个线程**（或另一个进程），
    否则同线程重入会直接拿到锁 —— 那会让本用例变成空守卫。
    """
    import ptrade.store as store

    app = _app(web_cfg, lock_timeout=0.3)
    locked = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with store.state_lock(web_cfg.root, timeout=10):
            locked.set()
            release.wait(20)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert locked.wait(10), "持锁线程未能取得写锁"
        for path in ("/api/state", "/api/health"):
            status, payload = _call(app, "GET", path)
            assert status == 200 and payload["ok"] is True, (path, payload)
        status, payload = _call(app, "POST", "/api/order",
                                body={"code": "000001", "side": "buy", "qty": 100})
        assert status == 503 and "STATE_LOCK_TIMEOUT" in payload["error"], (status, payload)
        assert _ledger_rows(web_cfg) == []
    finally:
        release.set()
        holder.join(timeout=10)


# --------------------------------------------------------------------------- #
# W8：ledger 带 source
# --------------------------------------------------------------------------- #
def test_w8_ledger_records_source_web(web_cfg):
    app = _app(web_cfg)
    status, payload = _buy(app, code="000001", qty=100)
    assert status == 200 and payload["data"]["orders"][0]["status"] == "filled"
    rows = _ledger_rows(web_cfg)
    assert rows and all(row["source"] == "web" for row in rows)
    assert "source" not in payload["data"]["orders"][0]          # 信封不受影响


def test_w8_attach_ledger_source_labels_cli(web_cfg):
    """同一机制可用于 CLI 侧（``source="cli"``）—— 供 Lead 在 cli.py 接线时复用。"""
    from ptrade.engine import Engine

    ds = DataSource(web_cfg, offline=True)
    state = core.state_from_document(_read_state_doc(web_cfg), web_cfg)
    engine = Engine(web_cfg, ds, state)
    webapp.attach_ledger_source(engine, "cli")
    order = engine.submit(_order_request("000001", "buy", 100, state.trade_date))
    assert order.status == "filled"
    assert _ledger_rows(web_cfg)[-1]["source"] == "cli"


def _order_request(code: str, side: str, qty: int, trade_date: str):
    from ptrade.engine import OrderRequest

    return OrderRequest(code=code, side=side, qty=qty, trade_date=trade_date)


# --------------------------------------------------------------------------- #
# W9：K 线内联返回
# --------------------------------------------------------------------------- #
def test_w9_chart_inlines_html(web_cfg):
    """W9：``/api/chart`` 必须**内联返回** HTML（唯一不返回 JSON 信封的端点）。"""
    from ptrade.chart import no_external_refs

    response = _app(web_cfg).dispatch("GET", "/api/chart", {"code": ["600519"]}, None)
    assert response.status == 200
    html = response.to_bytes().decode("utf-8")
    assert html.lstrip().startswith("<!DOCTYPE html"), html[:80]
    assert "echarts" in html.lower()
    assert no_external_refs(html) is True
    assert "text/html" in response.content_type
    # 不是"文件路径字符串冒充"：正文必须是完整 HTML
    assert len(html) > 10_000
    assert not html.lstrip().startswith("{")


# --------------------------------------------------------------------------- #
# W2（顺带）：search / watchlist / score 端点（W2 模块未落地时 skip）
# --------------------------------------------------------------------------- #
def _write_name_cache(cfg, rows: list[dict]) -> str:
    """写一份新鲜的全市场名称表缓存（search 的离线数据源）。"""
    from datetime import datetime, timezone

    path = os.path.join(cfg.root, "data", "cache", "stock_list.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"fetched_at": datetime.now(timezone.utc).isoformat(), "payload": rows}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False)
    return path


def test_api_search_offline_with_cache_returns_results(web_cfg):
    """offline=True + 有缓存 → 命中；且**不触网**（conftest 的 socket 守卫在跑）。"""
    pytest.importorskip("ptrade.search", reason="search.py（W2）尚未就绪")
    _write_name_cache(web_cfg, [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "000001", "name": "平安银行"},
    ])
    status, payload = _call(_app(web_cfg), "GET", "/api/search", query={"q": ["600519"]})
    assert status == 200 and payload["ok"] is True
    assert payload["data"]["offline"] is True
    assert [row["code"] for row in payload["data"]["results"]] == ["600519"]
    assert payload["data"]["results"][0]["name"] == "贵州茅台"


def test_api_search_no_hit_is_empty_not_error(web_cfg):
    """INV-36：「没搜到」返回空列表（ok=true），**不是**错误。"""
    pytest.importorskip("ptrade.search", reason="search.py（W2）尚未就绪")
    _write_name_cache(web_cfg, [{"code": "600519", "name": "贵州茅台"}])
    status, payload = _call(_app(web_cfg), "GET", "/api/search", query={"q": ["zzzz不存在"]})
    assert status == 200 and payload["ok"] is True
    assert payload["data"]["results"] == []
    assert payload["data"]["count"] == 0


def test_api_search_without_cache_is_business_error(web_cfg):
    """INV-36：「拿不到数据」必须是显式失败（ok=false），不得静默返回空列表。"""
    pytest.importorskip("ptrade.search", reason="search.py（W2）尚未就绪")
    status, payload = _call(_app(web_cfg), "GET", "/api/search", query={"q": ["600519"]})
    assert status == 200, payload
    assert payload["ok"] is False
    assert "数据" in payload["error"] or "缓存" in payload["error"]


def test_api_search_invalid_limit_returns_400(web_cfg):
    pytest.importorskip("ptrade.search", reason="search.py（W2）尚未就绪")
    status, payload = _call(_app(web_cfg), "GET", "/api/search", query={"q": ["600519"], "limit": ["0"]})
    assert status == 400 and "limit" in payload["error"]


def test_api_watchlist_roundtrip_and_score(web_cfg):
    pytest.importorskip("ptrade.watchlist", reason="watchlist.py（W2）尚未就绪")
    app = _app(web_cfg)
    status, payload = _call(app, "POST", "/api/watchlist", body={"code": "600519", "name": "贵州茅台"})
    assert status == 200 and payload["ok"] is True
    codes = [item["code"] for item in payload["data"]["items"]]
    assert "600519" in codes

    status, payload = _call(app, "GET", "/api/watchlist")
    assert status == 200 and payload["data"]["count"] == len(codes)

    status, payload = _call(app, "POST", "/api/watchlist/score", body={})
    assert status == 200 and payload["ok"] is True
    assert payload["data"]["count"] >= 1
    for row in payload["data"]["results"]:
        assert row["no_data"] is False and row["total"] > 0       # INV-38：不得记 0 分

    status, payload = _call(app, "DELETE", "/api/watchlist", body={"code": "600519"})
    assert status == 200
    assert "600519" not in [item["code"] for item in payload["data"]["items"]]


def test_api_watchlist_score_marks_no_data(web_cfg):
    """INV-38：池内缺数据的票必须标注 ``no_data``，**不得**记为 0 分。"""
    watchlist = pytest.importorskip("ptrade.watchlist", reason="watchlist.py（W2）尚未就绪")
    watchlist.add_to_watchlist(web_cfg, "999999", name="不存在")
    status, payload = _call(_app(web_cfg), "POST", "/api/watchlist/score", body={})
    assert status == 200 and payload["ok"] is True
    missing = [item["code"] for item in payload["data"]["no_data"]]
    assert "999999" in missing
    assert all(row["code"] != "999999" for row in payload["data"]["results"])


def test_api_score_endpoint(web_cfg):
    app = _app(web_cfg)
    status, payload = _call(app, "POST", "/api/score", body={"codes": ["600519", "000001"]})
    assert status == 200 and payload["ok"] is True
    assert payload["data"]["count"] == 2
    status, payload = _call(app, "POST", "/api/score", body={})
    assert status == 400 and "codes" in payload["error"]


def test_index_html_served_when_webui_ready(web_cfg):
    pytest.importorskip("ptrade.webui", reason="webui.py（W4）尚未就绪")
    response = _app(web_cfg).dispatch("GET", "/", {}, None)
    assert response.status == 200
    assert "text/html" in response.content_type
    assert b"<html" in response.to_bytes().lower()


# --------------------------------------------------------------------------- #
# 对抗探测（Lead）发现的缺陷 —— 回归守卫
#
# 这四条都对应**实测复现过**的缺陷，不是想象的边界。每条都注明「修之前是什么样」，
# 以便日后有人改动此处时能立刻看出这条测试在守什么。
# --------------------------------------------------------------------------- #
def test_query_params_are_unwrapped_before_parsing(web_cfg):
    """**回归（实测缺陷）**：``parse_qs`` 的值永远是 **list**，取用前必须解包。

    修之前 ``/api/search?q=银行&limit=2`` 报
    ``400 limit 必须是整数（收到 ['2']）`` —— ``limit`` 参数**完全不可用**。
    同文件里 ``q`` / ``code`` / ``period`` / ``asof`` 都做了 ``[0]`` 解包，只有 ``limit`` 漏了。

    本测试直接喂 ``parse_qs`` 的真实形状（list 值），而不是手工传标量 ——
    否则测不到这一类缺陷。
    """
    from urllib.parse import parse_qs

    # 需要名称表缓存才能走到 limit 生效那一步（否则会先报「离线缺缓存」）
    _write_name_cache(web_cfg, [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "600036", "name": "招商银行"},
        {"code": "601398", "name": "工商银行"},
        {"code": "000001", "name": "平安银行"},
    ])

    app = _app(web_cfg)
    q = parse_qs("q=%E9%93%B6%E8%A1%8C&limit=2", keep_blank_values=True)
    assert isinstance(q["limit"], list), "前提：parse_qs 给的是 list，否则本测试无意义"
    response = app.dispatch("GET", "/api/search", q, None)
    payload = json.loads(response.to_bytes().decode("utf-8"))
    assert response.status == 200, f"limit 参数不可用：{payload.get('error')}"
    assert payload["ok"] is True, payload.get("error")
    assert payload["data"]["count"] <= 2, "limit 未生效"

    # 对照：limit=1 必须比 limit=2 更少（证明参数真的被使用，而不是被忽略）
    q1 = parse_qs("q=%E9%93%B6%E8%A1%8C&limit=1", keep_blank_values=True)
    p1 = json.loads(app.dispatch("GET", "/api/search", q1, None).to_bytes().decode("utf-8"))
    assert p1["ok"] is True and p1["data"]["count"] == 1, f"limit=1 未生效：{p1}"


def test_as_bool_handles_list_values(web_cfg):
    """**回归（实测缺陷）**：``_as_bool`` 收到 list 时必须取首元素。

    修之前 ``str(['true'])`` == ``"['true']"`` → 匹配失败 → **静默回落默认值**，
    于是请求级 ``?offline=`` 无声失效（Critic 的 V1 担心的正是这条路径）。

    非空守卫：同时断言 ``false`` 与 ``true`` 两个方向，避免「永远返回默认值」也能过。
    """
    assert webapp._as_bool(["true"], False) is True, "列表形式的 true 未被识别 → 静默失效"
    assert webapp._as_bool(["false"], True) is False, "列表形式的 false 未被识别 → 静默失效"
    assert webapp._as_bool([], True) is True, "空列表应回落默认值"
    assert webapp._as_bool("yes", False) is True, "标量形式不得被这次修改破坏"
    assert webapp._as_bool(None, True) is True, "None 应回落默认值"


def test_delete_watchlist_without_body_does_not_crash(web_cfg):
    """**回归（实测缺陷）**：DELETE 无请求体时 ``body`` 是 ``None``。

    修之前 ``DELETE /api/watchlist``（无 body、无 code）报
    ``500 AttributeError: 'NoneType' object has no attribute 'get'`` ——
    客户端参数缺失被暴露成服务端空指针。
    """
    app = _app(web_cfg)
    status, payload = _call(app, "DELETE", "/api/watchlist", None, None)
    assert status == 400, f"缺 code 应是 400（客户端错误），实际 {status}: {payload}"
    assert "code" in str(payload.get("error")), payload

    # 带 query 形式仍应正常工作
    status, payload = _call(app, "DELETE", "/api/watchlist", {"code": ["600519"]}, None)
    assert status == 200 and payload["ok"] is True, payload


def test_client_errors_are_400_not_500(web_cfg):
    """**回归（实测缺陷）**：客户端数据错误必须是 400，不得落进 500 兜底。

    修之前实测有 **7 处**客户端错误返回 500（非法/未来/回退交易日、离线缺数据的 chart 等）。
    500 只应留给真正的服务端故障；本测试逐条钉住那几个端点。

    非空守卫：若把 ``dispatch`` 里的 ``ValueError`` 分支改回 500，本测试立即变红。
    """
    app = _app(web_cfg)
    bad_date = "not-a-date"
    cases = [
        ("POST", "/api/order", None,
         {"code": "601398", "side": "buy", "qty": 100, "trade_date": bad_date}),
        ("POST", "/api/order", None,
         {"code": "601398", "side": "buy", "qty": 100, "trade_date": "2999-01-01"}),
        ("POST", "/api/settle", None, {"trade_date": "xx"}),
        ("POST", "/api/settle", None, {"trade_date": "2000-01-01"}),
    ]
    for method, path, query, body in cases:
        status, payload = _call(app, method, path, query, body)
        assert status == 400, (
            f"{method} {path} {body} 应是 400，实际 {status}: {payload.get('error')}"
        )

    # 离线缺数据的 chart 是**业务态**（200 + error），不是 500
    status, payload = _call(app, "GET", "/api/chart", {"code": ["999999"]}, None)
    assert status == 200, f"缺数据应是业务态 200，实际 {status}"
    assert payload["ok"] is False and payload["error"], payload


# --------------------------------------------------------------------------- #
# W-7（architect 评审）：网页端不得依赖 CLI 的私有助手
#
# 修之前 webapp 通过 `_cli()` 调 cli 的 12 处私有函数（`_make_ds` / `_state_path` /
# `_state_summary` / `_jsonable` / `_resolve_asof` / `_risk_from_core`），属**分层倒置**：
# 一个 UI 层反向依赖另一个 UI 层的内部实现，cli 一改内部结构就会打断网页端。
# 现在真源在 `ptrade.service`，cli 只是转发。
# --------------------------------------------------------------------------- #
def test_w7_webapp_does_not_use_cli_private_helpers():
    """**架构不变量**：webapp 不得出现 `cli._xxx`（含变量中转形式）。

    用 AST 判定，而不是字符串匹配 —— 第一版我用 `_cli()._` 模式复核，结果**漏掉了
    `cli = _cli()` 之后的 `cli._state_summary(...)`**，误报「0 残留」。
    AST 能同时覆盖 `_cli()._x()` 与 `cli._x()` 两种写法。
    """
    import ast
    import pathlib

    src = pathlib.Path(REPO_ROOT, "ptrade", "webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 收集所有「形如 X._name」的属性访问，其中 X 是 cli / _cli() 的结果
    offenders: list[str] = []
    cli_aliases = {"cli"}                       # 局部变量名 `cli`
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Call):
                    fn = node.value.func
                    if isinstance(fn, ast.Name) and fn.id == "_cli":
                        cli_aliases.add(tgt.id)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not node.attr.startswith("_"):
            continue
        base = node.value
        # 形式 1: _cli()._x
        if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "_cli":
            offenders.append(f"_cli().{node.attr} @L{node.lineno}")
        # 形式 2: cli._x（cli 来自 _cli()）
        if isinstance(base, ast.Name) and base.id in cli_aliases:
            offenders.append(f"{base.id}.{node.attr} @L{node.lineno}")

    assert not offenders, "webapp 仍依赖 CLI 私有助手（W-7 分层倒置复发）：" + ", ".join(offenders)


def test_w7_service_does_not_import_ui_layers():
    """**架构不变量**：`service` 是中立层，不得 import `cli` / `webapp`（否则成环）。"""
    import ast
    import pathlib

    src = pathlib.Path(REPO_ROOT, "ptrade", "service.py").read_text(encoding="utf-8")
    bad: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module in ("cli", "webapp"):
            bad.append(f"from .{node.module} @L{node.lineno}")
        if isinstance(node, ast.Import):
            bad += [f"import {a.name} @L{node.lineno}" for a in node.names if a.name in ("cli", "webapp")]
    assert not bad, f"service 反向依赖 UI 层（会造成循环依赖）：{bad}"


# --------------------------------------------------------------------------- #
# M3（code-review 发现，Lead 修复）：账实分叉必须对用户**可见**
#
# 修之前 `check_ledger_state_consistency` **零生产调用点** —— B3 的「可检出」
# 只存在于测试里，用户看不见。现在 `ptrade status` 与 `/api/state` **同时**给出
# `ledger_consistency`。两处必须一起加：INV-40 要求它们逐字段一致，
# 只加一边会打红既有的「逐字段比对」测试。
# --------------------------------------------------------------------------- #
def test_m3_state_exposes_ledger_consistency(web_cfg):
    """**M3**：`/api/state` 必须给出 `ledger_consistency`，且形状可判读。

    ``web_cfg`` 夹具用 ``_write_state`` **直写** state 文件（绕过 ``store.save_state``），
    因此**没有** ``app.ledger_lines`` 水位 → 检查器只能判「不可判定」。
    这不是缺陷（真实路径都走 ``save_state``），但测试必须先把水位建立起来才有意义 ——
    这里用**真实写入器**落一次盘。
    """
    from ptrade import service, store

    state = store.read_state(web_cfg)
    assert state is not None
    store.save_state(web_cfg, state, source="test")      # 建立 ledger_lines 水位

    status, payload = _call(_app(web_cfg), "GET", "/api/state", None, None)
    assert status == 200 and payload["ok"] is True, payload
    info = payload["data"].get("ledger_consistency")
    assert isinstance(info, dict), f"缺少 ledger_consistency：{sorted(payload['data'])}"
    for key in ("consistent", "ledger_lines", "recorded", "diverged"):
        assert key in info, f"ledger_consistency 缺 {key}：{info}"
    assert info["diverged"] is False, f"有水位且无分叉时应为 False：{info}"

    # 注入幽灵行 → 必须变成 diverged=True（证明它不是恒定 False）
    store.append_jsonl(store.ledger_path(web_cfg), {"order_id": "O9999", "_injected": True})
    status2, payload2 = _call(_app(web_cfg), "GET", "/api/state", None, None)
    assert status2 == 200
    info2 = payload2["data"]["ledger_consistency"]
    assert info2["diverged"] is True, f"注入分叉后未检出：{info2}"

    # 与 CLI 侧同一实现 → 值必须相同（INV-40 加强）
    assert info2 == service.consistency(web_cfg)


def test_m3_cli_and_web_report_same_consistency(web_cfg):
    """**INV-40 加强**：两侧的 `ledger_consistency` 必须**同值**（同一实现，不得各写一份）。"""
    from ptrade import service

    cli_value = service.consistency(web_cfg)
    status, payload = _call(_app(web_cfg), "GET", "/api/state", None, None)
    assert status == 200
    assert payload["data"]["ledger_consistency"] == cli_value, (
        f"两侧不一致：web={payload['data']['ledger_consistency']} cli={cli_value}"
    )


# --------------------------------------------------------------------------- #
# V1 硬闸（ultraqa 场景矩阵发现，Lead 修复）：请求级 ?offline= 不得解除进程级 offline
#
# 修之前：`--offline` 起服务时，`?offline=false` 能把请求级 offline 改成 False
# （ultraqa 实测 data.offline=False）→ 进程级红线被单次请求绕过。
# 修法：进程级是**下界** —— `_as_bool(...) or self.offline`，请求级只能更严不能更松。
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("requested", ["false", "0", "no", "off"])
def test_v1_process_offline_cannot_be_relaxed_by_request(web_cfg, requested):
    """**V1**：进程 offline 下，`?offline=<假值>` 不得把 offline 变成 False。"""
    _write_name_cache(web_cfg, [{"code": "600519", "name": "贵州茅台"},
                                {"code": "600036", "name": "招商银行"}])
    app = _app(web_cfg, offline=True)
    status, payload = _call(app, "GET", "/api/search", {"q": ["600"], "offline": [requested]}, None)
    assert status == 200, payload
    assert payload["ok"] is True, payload.get("error")
    assert payload["data"]["offline"] is True, (
        f"?offline={requested} 解除了进程级 --offline：data.offline={payload['data']['offline']}"
    )


def test_v1_process_online_allows_request_to_tighten(web_cfg):
    """**对照**：进程**在线**时，`?offline=true` 应能把本次请求收紧为离线。

    否则上面那条「永远是 True」也能通过 —— 那种判据没有鉴别力。
    """
    _write_name_cache(web_cfg, [{"code": "600519", "name": "贵州茅台"}])
    app = _app(web_cfg, offline=False)
    status, payload = _call(app, "GET", "/api/search", {"q": ["600"], "offline": ["true"]}, None)
    assert status == 200
    assert payload["data"]["offline"] is True, "进程在线时请求级 ?offline=true 未生效"


# --------------------------------------------------------------------------- #
# INV-43（独立复核 N1）：网页**写端点**也必须在锁外预热
#
# 第一版 BLOCK-1 修复只做了 CLI 侧，网页三个写端点零预热。复核者实测：
# `POST /api/order` 冷缓存下锁内取数 3 次，注入 0.6s/次延迟后并发写者
# `StateLockTimeout`，而 CLI 侧同一实验 0.02s 拿到锁 —— 即 BLOCK-1 只修了一半。
# --------------------------------------------------------------------------- #
def test_inv43_web_write_endpoints_prewarm_before_lock():
    """**机制断言**：三个网页写端点都必须调用 `prewarm_before_write`。

    用 AST 取函数源码段再查字符串 —— 与 `test_cli.py::test_block1_write_commands_prewarm_before_lock`
    同一手法。

    **行为证据请引用「模拟在线」的测量**（第三轮独立复核复现）：
    预热 OFF → 锁内真实取数 **4** 次 / 最长持锁 **3.93s** / **2×503**；
    预热 ON → **0** 次 / **0.12s** / **0×503**。

    ⚠️ **不要引用 `.omx/probe/verify_block1_web2.py`** —— 它测的是**离线**路径，
    而离线 `_cached` 不写缓存，「锁外预热」读了也白读，**物理上不可能**把锁内取数降到 0。
    复核者直接跑它得到「修后仍 3 次 / 1.87s / 2×503」，该脚本头部已加降级警示。
    我原先在这里列的那组数字（2~3→0 / 1.27s→0.06s / 503 2→0）**不可复现，已删除**。
    """
    import ast
    import pathlib

    src = pathlib.Path(REPO_ROOT, "ptrade", "webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("api_order", "api_close", "api_settle"):
            found[node.name] = "prewarm_before_write(" in (ast.get_source_segment(src, node) or "")
    assert set(found) == {"api_order", "api_close", "api_settle"}, f"未找到全部写端点：{sorted(found)}"
    missing = [name for name, ok in found.items() if not ok]
    assert not missing, (
        f"这些网页写端点没有在锁外预热：{missing} —— INV-43 复发"
        f"（在线冷缓存会在锁内联网，饿死并发写者）"
    )


def test_inv43_order_prewarm_receives_side_for_margin():
    """**N2 断言**：下单端点的预热必须把 `side` 传下去。

    融资/融券要在锁内查两融标的白名单（`margin_targets`，两个 akshare 接口，
    最坏 ~4.9s/个）；不传 `side` 就不会预热它，等于没修。
    """
    import ast
    import pathlib

    src = pathlib.Path(REPO_ROOT, "ptrade", "webapp.py").read_text(encoding="utf-8")
    seg = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "api_order":
            seg = ast.get_source_segment(src, node) or ""
    assert seg, "未找到 api_order"
    assert "side=side" in seg, (
        "api_order 的预热没有传 side= —— 融资/融券不会预热 margin_targets（N2 复发）"
    )


# --------------------------------------------------------------------------- #
# 守卫错误的 HTTP 映射（第三轮复核 P3-9 / 我修的一处真缺陷）
#
# 修之前 `dispatch` 用 `type(exc).__name__ == "DataUnavailable"` **按类名字符串**匹配。
# 问题有两层：
#   1. 该检查写在 `except ValueError` 分支里，而 `DataUnavailable` 继承 `RuntimeError`
#      **不是** `ValueError` → 那行**永远不可达**；
#   2. `NetworkForbiddenInLock` 是 `DataUnavailable` 的**子类**，类名字符串不匹配
#      → 守卫错误掉进通用 500。
# 现改为**按类型**匹配，并给守卫错误单独的 **409**（架构 S-1 ③ 要求的可执行提示）。
# --------------------------------------------------------------------------- #
def test_network_in_lock_maps_to_409(web_cfg, monkeypatch):
    """**守卫错误 → HTTP 409 + `NETWORK_IN_LOCK`**（不是 500，也不是 200 业务态）。

    **为什么改成直接 monkeypatch 路由处理函数**（2026-10-14 实测的测试设计问题）：
    第一版把 `WebApp._ds` 换成抛错的函数、再打 `/api/chart`。那条路径下
    **变异实验会卡死**（注掉 409 分支后子进程 >300s 不返回，我不得不杀进程）。

    机制上的解释：`NetworkForbiddenInLock` 是 `DataUnavailable` 的**子类**，
    注掉 `except NetworkForbiddenInLock` 后它会被 `except DataUnavailable` 接住 →
    返回 200 → 测试应**快速失败**，所以「卡住」与 409 分支无关，
    而在于**让整条 chart 路由跑起来**这件事本身在变异态下的行为不可预测。
    本用例只需要验**映射**，不该依赖整条路由 —— 直接替换处理函数即可，
    既更聚焦又不会卡。
    """
    from ptrade import webapp as webapp_mod
    from ptrade.datasource import NetworkForbiddenInLock

    def _boom(self, query, body):
        raise NetworkForbiddenInLock("注入：写锁临界区内禁止联网取数")

    # 直接替换路由处理函数：只触发 dispatch 的异常映射，不跑 chart 的取数/渲染
    app = _app(web_cfg)
    monkeypatch.setattr(app, "api_chart", lambda q, b: _boom(app, q, b), raising=True)
    status, payload = _call(app, "GET", "/api/chart", {"code": ["600519"]}, None)
    assert status == 409, f"守卫错误应映射为 409，实际 {status}: {payload.get('error')}"
    assert "NETWORK_IN_LOCK" in str(payload.get("error")), payload


def test_dataunavailable_still_maps_to_200_business_state(web_cfg, monkeypatch):
    """**对照**：普通 `DataUnavailable` 仍映射为 **200 业务态**（别把两件事混成一件）。

    若两者映射相同，上面那条就没有鉴别力。
    """
    from ptrade import webapp as webapp_mod
    from ptrade.datasource import DataUnavailable

    def _boom(*a, **k):
        raise DataUnavailable("离线模式缺少数据")

    monkeypatch.setattr(webapp_mod.WebApp, "_ds", _boom, raising=True)
    status, payload = _call(_app(web_cfg), "GET", "/api/chart", {"code": ["600519"]}, None)
    assert status == 200, f"DataUnavailable 应映射为 200 业务态，实际 {status}"
    assert payload["ok"] is False and payload["error"]


# --------------------------------------------------------------------------- #
# 异常映射的**纯函数**单测（2026-10-14）
#
# 为什么单独测纯函数：上面那两条走的是**整条路由**，于是「注掉实现看是否变红」的
# 变异实验会**卡死**（>300s，我为此杀过 4 次进程），导致
# 「NetworkForbiddenInLock → 409」长期拿不到变异证据。
# 抽出 `webapp.map_exception` 后，直接喂异常对象即可断言状态码 —— 不跑路由、不卡。
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc_factory,expected_status,needle", [
    (lambda: WebApp_MapProbe.param_error(), 400, "参数非法"),
    (lambda: WebApp_MapProbe.lock_timeout(), 503, "STATE_LOCK_TIMEOUT"),
    (lambda: WebApp_MapProbe.guard_error(), 409, "NETWORK_IN_LOCK"),
    (lambda: WebApp_MapProbe.data_unavailable(), 200, "缺少数据"),
    (lambda: ValueError("非法交易日"), 400, "非法交易日"),
])
def test_map_exception_status_codes(exc_factory, expected_status, needle):
    """**契约表**：`map_exception` 对 5 类异常的状态码与错误串。"""
    from ptrade import webapp as webapp_mod

    resp = webapp_mod.map_exception("/api/chart", exc_factory())
    assert resp is not None, "已知异常不该返回 None（那会被兜底成 500）"
    assert resp.status == expected_status, (
        f"{type(exc_factory()).__name__} 应映射为 {expected_status}，实际 {resp.status}"
    )
    payload = resp.payload or json.loads(resp.to_bytes().decode("utf-8"))
    assert needle in str(payload.get("error")), payload


def test_map_exception_returns_none_for_unknown():
    """**未预期异常返回 `None`**（交给调用方兜底 500）—— 否则兜底逻辑会被吞掉。"""
    from ptrade import webapp as webapp_mod

    assert webapp_mod.map_exception("/api/chart", RuntimeError("boom")) is None
    assert webapp_mod.map_exception("/api/chart", KeyError("k")) is None


def test_map_exception_guard_must_come_before_dataunavailable():
    """**顺序断言**：`NetworkForbiddenInLock` 是 `DataUnavailable` 的**子类** ——
    映射里它必须排在前面，否则会被父类分支接住变成 200 业务态。

    这条是「守卫错误曾经被判成 500」那类漏配的**机制性**守卫：
    它断言的是**子类关系**与**顺序**，而不是某个具体状态码。
    """
    from ptrade import webapp as webapp_mod
    from ptrade.datasource import DataUnavailable, NetworkForbiddenInLock

    assert issubclass(NetworkForbiddenInLock, DataUnavailable), "前提：它确实是子类"
    resp = webapp_mod.map_exception("/api/order", NetworkForbiddenInLock("注入"))
    assert resp.status == 409, (
        f"子类被父类分支接住了（得到 {resp.status}）—— 映射顺序错了"
    )


class WebApp_MapProbe:
    """测试用的异常构造器（避免在 parametrize 里写长 import）。"""

    @staticmethod
    def param_error():
        from ptrade import webapp as m
        return m._ParamError("参数非法")

    @staticmethod
    def lock_timeout():
        from ptrade import webapp as m
        return m._LockTimeout("锁超时")

    @staticmethod
    def guard_error():
        from ptrade.datasource import NetworkForbiddenInLock
        return NetworkForbiddenInLock("写锁临界区内禁止联网取数")

    @staticmethod
    def data_unavailable():
        from ptrade.datasource import DataUnavailable
        return DataUnavailable("离线模式缺少数据")
