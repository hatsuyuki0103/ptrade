"""tests/test_cli.py —— 命令行契约（INTERFACES.md v3.1 §7）。

用 ``subprocess`` 跑 ``python -m ptrade ... --offline --json``，断言：

* 退出码 0/1 与 ``{"ok","command","data","error"}`` 结构；
* **业务拒单不算错误**（``ok=true`` 且 ``data.orders[].status == "rejected"``）；
* ``--offline`` 真的不联网（对夹具外的代码必须报"离线缺少数据"，而不是偷偷联网）；
* 依赖 engine / strategy / chart 的命令：模块未就绪时给结构化错误，就绪时按契约工作。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "data", "fixture")
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


@pytest.fixture
def cli_root(tmp_path):
    """带夹具的临时 root（无 config.json → 默认配置）。"""
    dst = tmp_path / "data" / "fixture"
    dst.mkdir(parents=True, exist_ok=True)
    for name in os.listdir(FIXTURE_DIR):
        if name.endswith(".json"):
            shutil.copy2(os.path.join(FIXTURE_DIR, name), dst / name)
    return tmp_path


def run_cli(root, *args, timeout=180):
    cmd = [sys.executable, "-m", "ptrade", *args, "--root", str(root)]
    return subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env=ENV, timeout=timeout,
    )


def run_json(root, *args, **kwargs):
    proc = run_cli(root, *args, "--json", **kwargs)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:  # pragma: no cover - 结构崩坏时给出原始输出
        raise AssertionError(f"stdout 不是合法 JSON:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert set(payload.keys()) == {"ok", "command", "data", "error"}
    return proc, payload


def _dates(cli_root):
    """返回 ``(有数据的最新交易日, 其上一交易日, 其下一交易日)``。

    **必须用** :func:`tests._guards.price_asof` 而不是 ``ds.latest_trade_date()``：
    后者读交易日历（覆盖到 2026-12-31），而日线夹具止于 2026-10-09 ——
    系统日期一往前走，它就会返回「日历上有、却没有行情」的日期，
    下单被 ``SUSPENDED`` 拒掉（2026-10-13 实测 8 个用例因此失败）。
    """
    from tests._guards import price_asof

    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    ds = DataSource(load_config(str(cli_root)), offline=True)
    latest = price_asof(ds)
    return latest, ds.prev_trade_date(latest), ds.next_trade_date(latest)


# --------------------------------------------------------------------------- #
# 帮助 / 结构
# --------------------------------------------------------------------------- #
def test_help_lists_all_commands(cli_root):
    proc = run_cli(cli_root, "--help")
    assert proc.returncode == 0
    for command in ("init", "status", "sync", "scan", "recommend", "plan", "order",
                    "settle", "risk", "close", "ledger", "chart"):
        assert command in proc.stdout


def test_no_command_prints_help(cli_root):
    proc = run_cli(cli_root)
    assert proc.returncode == 0
    assert "init" in proc.stdout


def test_unknown_command_exits_2(cli_root):
    proc = run_cli(cli_root, "nonexistent")
    assert proc.returncode != 0


# --------------------------------------------------------------------------- #
# init / status
# --------------------------------------------------------------------------- #
def test_init_json_creates_state(cli_root):
    proc, payload = run_json(cli_root, "init", "--cash", "500000", "--offline")
    assert proc.returncode == 0
    assert payload["ok"] is True
    assert payload["command"] == "init"
    assert payload["error"] is None
    assert payload["data"]["initial_cash"] == 500000.0
    assert payload["data"]["available_cash"] == 500000.0
    state_path = cli_root / "data" / "state.json"
    assert state_path.is_file()
    doc = json.loads(state_path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 3
    assert "margin_used" not in doc["state"]
    assert "frozen_t1" in doc["state"] and "frozen_collateral" in doc["state"]


def test_init_twice_fails_without_reset(cli_root):
    assert run_cli(cli_root, "init", "--offline").returncode == 0
    proc, payload = run_json(cli_root, "init", "--offline")
    assert proc.returncode == 1
    assert payload["ok"] is False
    assert "已存在" in payload["error"]


def test_init_reset_succeeds(cli_root):
    run_cli(cli_root, "init", "--offline")
    proc, payload = run_json(cli_root, "init", "--reset", "--cash", "1000000", "--offline")
    assert proc.returncode == 0
    assert payload["data"]["initial_cash"] == 1000000.0


def test_status_before_init_fails(cli_root):
    proc, payload = run_json(cli_root, "status")
    assert proc.returncode == 1
    assert payload["ok"] is False
    assert "init" in payload["error"]


def test_status_after_init(cli_root):
    run_cli(cli_root, "init", "--offline")
    proc, payload = run_json(cli_root, "status")
    assert proc.returncode == 0
    data = payload["data"]
    assert data["net_assets"] == pytest.approx(500000.0)
    assert data["maintenance_ratio"] is None  # inf → null
    assert data["positions"] == []
    assert data["collateral_value"] == pytest.approx(500000.0)


def test_status_human_output_is_chinese(cli_root):
    run_cli(cli_root, "init", "--offline")
    proc = run_cli(cli_root, "status")
    assert proc.returncode == 0
    assert "可用现金" in proc.stdout
    assert "维持担保比例" in proc.stdout


# --------------------------------------------------------------------------- #
# sync（离线数据）
# --------------------------------------------------------------------------- #
def test_sync_offline_uses_fixtures(cli_root):
    latest, prev, _ = _dates(cli_root)
    proc, payload = run_json(cli_root, "sync", "--codes", "600519,002594",
                             "--start", "2025-04-01", "--end", "2025-10-09", "--offline")
    assert proc.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["codes"]["600519"] > 100
    assert payload["data"]["codes"]["002594"] > 100
    assert payload["data"]["failed"] == {}


def test_sync_offline_never_goes_online(cli_root):
    """夹具里没有的代码：必须报"离线缺少数据"，而不是偷偷联网取数。"""
    proc, payload = run_json(cli_root, "sync", "--codes", "999999",
                             "--start", "2025-04-01", "--end", "2025-10-09", "--offline")
    assert payload["ok"] is False
    assert proc.returncode == 1
    assert "离线" in payload["error"]
    assert "999999" in payload["data"]["failed"]


def test_sync_unadjust_flag(cli_root):
    proc, payload = run_json(cli_root, "sync", "--codes", "600519", "--unadjust",
                             "--start", "2025-04-01", "--end", "2025-10-09", "--offline")
    assert proc.returncode == 0
    assert payload["data"]["codes"]["600519"] > 100


# --------------------------------------------------------------------------- #
# ledger / risk
# --------------------------------------------------------------------------- #
def test_ledger_empty_then_filled(cli_root):
    run_cli(cli_root, "init", "--offline")
    proc, payload = run_json(cli_root, "ledger")
    assert proc.returncode == 0
    assert payload["data"]["orders"] == []
    assert payload["data"]["total"] == 0


def test_risk_contract(cli_root):
    run_cli(cli_root, "init", "--offline")
    proc, payload = run_json(cli_root, "risk")
    assert proc.returncode == 0
    data = payload["data"]
    assert data["status"] in {"safe", "warn", "margin_call", "forced"}
    assert "alerts" in data and "exposure" in data
    assert data["status"] == "safe"


# --------------------------------------------------------------------------- #
# strategy 依赖的命令
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _has("ptrade.strategy"), reason="strategy.py 未就绪（T3）")
def test_scan_json_serializes_canslim_results(cli_root):
    proc, payload = run_json(cli_root, "scan", "--codes", "600519,002594,000001", "--offline")
    assert proc.returncode == 0
    assert payload["ok"] is True
    results = payload["data"]["results"]
    assert len(results) == 3
    for row in results:
        assert set(row.keys()) >= {"code", "name", "asof", "total", "factors", "signal", "reasons", "risks"}
        keys = [f["key"] for f in row["factors"]]
        assert len(keys) == len(set(keys))  # 因子 key 唯一
        assert row["signal"] in {"buy", "watch", "avoid", "short"}
    assert payload["data"]["rank"]["asof"] == payload["data"]["asof"]


@pytest.mark.skipif(not _has("ptrade.strategy"), reason="strategy.py 未就绪（T3）")
def test_scan_signal_respects_gates(cli_root):
    """§6.2 信号闸门：buy 必须同时满足 total>=75 且 L>=60 且 M>=50。

    注意：真实夹具下 `600519` 的 C/L 偏低（2025 年白酒杀估值），**`buy` 不可达**是数据事实，
    不是实现缺陷；剧本数据的强票标定见 `data/scenario/`（`DataSource(scenario=True)`）。
    因此这里只断言"闸门一致性"，不硬编码某一票必须为 buy。
    """
    _, payload = run_json(cli_root, "scan", "--codes", "600519,002594,000001", "--offline")
    for row in payload["data"]["results"]:
        factors = {f["key"]: f["score"] for f in row["factors"]}
        if row["signal"] == "buy":
            assert row["total"] >= 75.0, row
            assert factors["L"] >= 60 and factors["M"] >= 50, row
        if row["total"] >= 75.0 and (factors["L"] < 60 or factors["M"] < 50):
            assert row["signal"] == "watch", row


@pytest.mark.skipif(not _has("ptrade.strategy"), reason="strategy.py 未就绪（T3）")
def test_recommend_contract(cli_root):
    proc, payload = run_json(cli_root, "recommend", "--codes", "600519,000001", "--offline")
    assert proc.returncode == 0
    for row in payload["data"]["recommendations"]:
        assert row["action"] in {"open_long", "open_short", "close", "watch"}
        assert row["side"] in {"long", "short", "none"}
        assert set(row.keys()) >= {"code", "score", "entry", "stop_loss", "take_profit", "position_pct"}


# --------------------------------------------------------------------------- #
# engine 依赖的命令（T+1 端到端）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _has("ptrade.engine"), reason="engine.py 未就绪（T2）")
def test_order_t1_end_to_end(cli_root):
    latest, prev, nxt = _dates(cli_root)
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    ds = DataSource(load_config(str(cli_root)), offline=True)
    # 用低价票（000001 ≈ 11 元）：600519 单笔 100 股即 12.6 万 > 20% 单票上限，
    # 会被引擎按 POSITION_LIMIT_PCT 正确拒绝，不适合做 T+1 流程用例。
    code, qty = "000001", 1000
    price = ds.last_close(code, prev)

    assert run_cli(cli_root, "init", "--offline").returncode == 0
    # 建仓日 = prev
    proc, payload = run_json(cli_root, "order", "--code", code, "--side", "buy",
                             "--qty", str(qty), "--price", f"{price}", "--trade-date", prev, "--offline")
    assert proc.returncode == 0 and payload["ok"] is True
    assert payload["data"]["orders"][0]["status"] == "filled", payload["data"]["orders"][0]

    # 当日卖出 → T+1 未解冻，业务拒单：ok=true + 退出码 0
    proc, payload = run_json(cli_root, "order", "--code", code, "--side", "sell",
                             "--qty", str(qty), "--price", f"{price}", "--trade-date", prev, "--offline")
    assert proc.returncode == 0
    assert payload["ok"] is True
    order = payload["data"]["orders"][0]
    assert order["status"] == "rejected"
    assert order["error_code"] == "T1_NOT_SETTLED"

    # 推进到次一交易日 → 卖出成功
    proc, payload = run_json(cli_root, "settle", "--trade-date", latest, "--offline")
    assert proc.returncode == 0 and payload["ok"] is True
    proc, payload = run_json(cli_root, "order", "--code", code, "--side", "sell",
                             "--qty", str(qty), "--price", f"{price}", "--trade-date", latest, "--offline")
    assert proc.returncode == 0
    order = payload["data"]["orders"][0]
    assert order["status"] == "filled", order
    assert float(order["cash_delta"]) == 0.0  # 普通卖出：T+1 到账
    assert payload["data"]["state"]["frozen_t1"] > 0

    # 再推进一天 → 资金到账（同价买卖，亏损应恰为双边手续费）
    proc, payload = run_json(cli_root, "settle", "--trade-date", nxt, "--offline")
    state = payload["data"]["state"]
    assert state["frozen_t1"] == 0
    assert state["available_cash"] < 500000.0
    assert state["total_pnl"] == pytest.approx(state["available_cash"] - 500000.0)
    assert -200.0 < state["total_pnl"] < 0.0

    # 流水：buy + 当日拒单 + 次日成交 = 3 条；且**不得重复落账**（engine 落账、CLI 只读）
    _, payload = run_json(cli_root, "ledger", "--limit", "10")
    assert payload["data"]["total"] == 3, payload["data"]
    ids = [row["order_id"] for row in payload["data"]["orders"]]
    assert len(ids) == len(set(ids)), f"同一笔委托被重复落账: {ids}"
    assert os.path.isfile(str(cli_root / "data" / "ledger.jsonl"))


@pytest.mark.skipif(not _has("ptrade.engine"), reason="engine.py 未就绪（T2）")
def test_settle_rejects_earlier_date(cli_root):
    latest, prev, _ = _dates(cli_root)
    assert run_cli(cli_root, "init", "--offline").returncode == 0
    run_cli(cli_root, "settle", "--trade-date", latest, "--offline")
    proc, payload = run_json(cli_root, "settle", "--trade-date", prev, "--offline")
    assert proc.returncode == 1
    assert payload["ok"] is False


@pytest.mark.skipif(not _has("ptrade.engine"), reason="engine.py 未就绪（T2）")
def test_plan_contract(cli_root):
    assert run_cli(cli_root, "init", "--offline").returncode == 0
    proc, payload = run_json(cli_root, "plan", "--codes", "600519,000001", "--offline")
    if not _has("ptrade.strategy"):
        assert payload["ok"] is False
        return
    assert proc.returncode == 0
    assert "actions" in payload["data"] and "skipped" in payload["data"]


@pytest.mark.skipif(not _has("ptrade.engine"), reason="engine.py 未就绪（T2）")
def test_close_position_contract(cli_root):
    latest, prev, _ = _dates(cli_root)
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    ds = DataSource(load_config(str(cli_root)), offline=True)
    code = "000001"
    price = ds.last_close(code, prev)
    run_cli(cli_root, "init", "--offline")
    _, bought = run_json(cli_root, "order", "--code", code, "--side", "buy", "--qty", "1000",
                         "--price", f"{price}", "--trade-date", prev, "--offline")
    assert bought["data"]["orders"][0]["status"] == "filled", bought["data"]["orders"][0]
    run_cli(cli_root, "settle", "--trade-date", latest, "--offline")
    proc, payload = run_json(cli_root, "close", "--code", code, "--ratio", "1.0",
                             "--trade-date", latest, "--offline")
    assert proc.returncode == 0
    assert payload["ok"] is True
    orders = payload["data"]["orders"]
    assert isinstance(orders, list) and len(orders) >= 1
    assert any(o["status"] == "filled" for o in orders)
    assert payload["data"]["state"]["positions"] == []


# --------------------------------------------------------------------------- #
# chart（G7 用户追加需求）
# --------------------------------------------------------------------------- #
def test_chart_without_module_reports_clean_error(cli_root):
    if _has("ptrade.chart"):
        pytest.skip("chart.py 已就绪，见 test_chart_generates_html")
    proc, payload = run_json(cli_root, "chart", "--code", "588000", "--offline")
    assert proc.returncode == 1
    assert payload["ok"] is False
    assert "chart 模块不可用" in payload["error"]


@pytest.mark.skipif(not _has("ptrade.chart"), reason="chart.py 未就绪（task-7）")
def test_chart_generates_html(cli_root):
    proc, payload = run_json(cli_root, "chart", "--code", "588000", "--offline")
    assert proc.returncode == 0, proc.stderr
    path = payload["data"]["path"]
    assert os.path.isabs(path)
    assert os.path.isfile(path)
    assert payload["data"]["period"] == "daily"
    html = open(path, "r", encoding="utf-8", errors="ignore").read()
    assert "<html" in html.lower()
    assert "MA12" in html or "ma12" in html.lower()


@pytest.mark.skipif(not _has("ptrade.engine"), reason="engine.py 未就绪（T2）")
def test_ledger_has_no_duplicate_orders(cli_root):
    """回归：engine.submit 落 ledger（§5），CLI 不得再追加一次（曾实测每笔写两行）。"""
    latest, prev, _ = _dates(cli_root)
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    ds = DataSource(load_config(str(cli_root)), offline=True)
    price = ds.last_close("000001", prev)
    run_cli(cli_root, "init", "--offline")
    for _ in range(3):
        run_json(cli_root, "order", "--code", "000001", "--side", "buy", "--qty", "100",
                 "--price", f"{price}", "--trade-date", prev, "--offline")
    _, payload = run_json(cli_root, "ledger", "--limit", "50")
    ids = [row["order_id"] for row in payload["data"]["orders"]]
    assert len(ids) == 3, payload["data"]
    assert len(set(ids)) == 3
    assert payload["data"]["total"] == 3


def test_init_with_start_trade_date_allows_historical_replay(cli_root):
    """D2 产品能力：`init --trade-date` 指定起点后可复盘任意历史交易日。"""
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    ds = DataSource(load_config(str(cli_root)), offline=True)
    start, nxt = "2025-10-09", "2025-10-10"
    assert ds.is_trade_date(start) and ds.is_trade_date(nxt)
    price = ds.last_close("000001", start)

    proc, payload = run_json(cli_root, "init", "--trade-date", start, "--offline")
    assert proc.returncode == 0
    assert payload["data"]["trade_date"] == start
    assert payload["data"]["hint"].startswith("起点交易日")

    # 非交易日应被拒
    proc, payload = run_json(cli_root, "init", "--reset", "--trade-date", "2025-10-01", "--offline")
    assert proc.returncode == 1 and "不是交易日" in payload["error"]

    if not _has("ptrade.engine"):
        pytest.skip("engine.py 未就绪（T2）：仅验证 init 起点能力")
    _, payload = run_json(cli_root, "init", "--reset", "--trade-date", start, "--offline")
    assert payload["data"]["trade_date"] == start
    proc, payload = run_json(cli_root, "order", "--code", "000001", "--side", "buy", "--qty", "1000",
                             "--price", f"{price}", "--trade-date", start, "--offline")
    assert payload["data"]["orders"][0]["status"] == "filled", payload["data"]["orders"][0]
    proc, payload = run_json(cli_root, "order", "--code", "000001", "--side", "sell", "--qty", "1000",
                             "--price", f"{price}", "--trade-date", start, "--offline")
    assert payload["data"]["orders"][0]["error_code"] == "T1_NOT_SETTLED"
    proc, payload = run_json(cli_root, "settle", "--trade-date", nxt, "--offline")
    assert payload["ok"] is True
    proc, payload = run_json(cli_root, "order", "--code", "000001", "--side", "sell", "--qty", "1000",
                             "--price", f"{price}", "--trade-date", nxt, "--offline")
    assert payload["data"]["orders"][0]["status"] == "filled", payload["data"]["orders"][0]
    assert payload["data"]["state"]["trade_date"] == nxt


@pytest.mark.skipif(not _has("ptrade.engine"), reason="engine.py 未就绪（T2）")
def test_short_sell_on_finance_only_target_is_rejected(cli_root):
    """D3：`can_short=False` 的融资标的必须端到端返回 NOT_SHORT_TARGET。

    用 600110 诺德股份：真实沪市两融标的、当日融券余量 0（保守判定为不可融券），
    且有日线夹具 → 不会被 SUSPENDED 抢先拦截。
    """
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    ds = DataSource(load_config(str(cli_root)), offline=True)
    assert ds.is_margin_target("600110") is True
    assert ds.is_short_target("600110") is False
    start = "2025-10-09"
    price = ds.last_close("600110", start)
    run_cli(cli_root, "init", "--trade-date", start, "--offline")
    proc, payload = run_json(cli_root, "order", "--code", "600110", "--side", "short_sell",
                             "--qty", "100", "--price", f"{price}", "--trade-date", start, "--offline")
    assert proc.returncode == 0  # 业务拒单：ok=true + 退出码 0
    order = payload["data"]["orders"][0]
    assert order["status"] == "rejected"
    assert order["error_code"] == "NOT_SHORT_TARGET", order
    # 同一标的的融资买入白名单是开的（证明不是"不在名单里"而是"不可融券"）
    assert ds.is_margin_target("600110") is True


def test_risk_json_has_no_bare_infinity(cli_root):
    """CR-9：`risk --json` 不得出现裸 Infinity/NaN；非有限值统一为 null。"""
    run_cli(cli_root, "init", "--offline")
    proc, payload = run_json(cli_root, "risk")
    assert proc.returncode == 0
    text = proc.stdout
    assert "Infinity" not in text and "NaN" not in text
    json.dumps(payload, allow_nan=False)  # 严格 JSON 必须可序列化
    # 空账户无负债 → 维持担保比例应为 null（与 order/status 口径一致）
    assert payload["data"]["maintenance_ratio"] is None


def test_status_and_order_share_the_same_ratio_convention(cli_root):
    run_cli(cli_root, "init", "--offline")
    _, status = run_json(cli_root, "status")
    _, risk = run_json(cli_root, "risk")
    assert status["data"]["maintenance_ratio"] is None
    assert risk["data"]["maintenance_ratio"] is None


@pytest.mark.parametrize("command", ["settle", "close"])
def test_settle_and_close_accept_offline(cli_root, command):
    """CR-11：断网闭环不得断链 —— settle / close 也必须支持 --offline。"""
    proc = run_cli(cli_root, command, "--help")
    assert proc.returncode == 0
    assert "--offline" in proc.stdout


# --------------------------------------------------------------------------- #
# BLOCK-1（架构评审发现，Lead 复现并修复）：写命令必须先**在锁外**预热数据
#
# 修之前：写命令把「读 → 改 → 写」整体放进锁，而引擎在临界区内会调
# trade_dates / next_trade_date / quote / last_close / daily / spot；离线时是本地夹具读
# （持锁 ≈0.12s，无害），但**在线 + 冷缓存**时是网络取数 —— 实测单段持锁 24.97s，
# 而网页端锁超时是 10s → 网页下单稳定 503，而账户本身没有任何问题。
# Lead 修复后实测（在线路径）：锁内缓存未命中 2 → **0**（两次未命中被移到锁外）。
# --------------------------------------------------------------------------- #
def test_block1_write_commands_prewarm_before_lock():
    """**BLOCK-1 的机制断言**：三个写命令都必须调用 `_prewarm_before_write`。

    这是静态断言；真正的行为证据（在线路径锁内缓存未命中 2→0）记录在
    `.omx/probe/verify_block1_fixed.py`。此处守住「调用点不会消失」。
    """
    import ast
    import pathlib

    src = pathlib.Path(ROOT, "ptrade", "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("cmd_order", "cmd_settle", "cmd_close"):
            body = ast.get_source_segment(src, node) or ""
            found[node.name] = "_prewarm_before_write(" in body
    assert set(found) == {"cmd_order", "cmd_settle", "cmd_close"}, f"未找到全部写命令：{sorted(found)}"
    missing = [name for name, ok in found.items() if not ok]
    assert not missing, (
        f"这些写命令没有在锁外预热：{missing} —— BLOCK-1 复发（在线冷缓存会在锁内联网）"
    )


def test_block1_prewarm_actually_warms_the_order_code():
    """**BLOCK-1 的关键细节**：预热必须真的覆盖本次下单标的的 quote 等数据。

    第一版修复栽在这里：直接用 ``args.trade_date``（``init`` 后为空）→
    ``trade_date=None`` → quote/last_close/daily 全被跳过，锁内仍是 6 次数据访问。
    """
    from ptrade import cli as cli_mod

    called: dict[str, object] = {}

    class FakeDS:
        def trade_dates(self):
            called["trade_dates"] = True
            return ["2025-10-09", "2025-10-10"]

        def next_trade_date(self, d):
            called["next_trade_date"] = d
            return "2025-10-10"

        def is_trade_date(self, d):
            return True

        def quote(self, code, asof):
            called[f"quote:{code}"] = asof
            return {"last": 7.02, "prev_close": 7.0}

        def last_close(self, code, asof):
            called[f"last_close:{code}"] = asof
            return 7.0

        def daily(self, code, start, end):
            called[f"daily:{code}"] = end
            return []

        def spot(self, codes=None):
            # **必须接住 `codes`**（第三轮独立复核指出）：`prewarm_for_write` 会调
            # `ds.spot(list(all_codes))` 来覆盖 `engine._enrich_spot`（`_float_shares` 的取数点）。
            # 原先这个 stub 签名是 `spot(self)`，传 `codes` 会 `TypeError`，
            # 而那个异常被 `_try` 的 best-effort 吞掉 → `warmed` 里没有 `spot:codes`
            # → **`spot([codes])` 的预热零测试覆盖**（注掉实现后全量仍绿）。
            called["spot" if codes is None else "spot:codes"] = True
            return None

    class FakeState:
        trade_date = ""
        positions: dict = {}

    warmed = cli_mod.prewarm_for_write(FakeDS(), FakeState(), "2025-10-09", extra_codes=("601398",))
    assert warmed, "预热什么都没做（空实现 = BLOCK-1 未修复）"
    assert called.get("quote:601398") == "2025-10-09", f"未预热下单标的的 quote：{called}"
    assert "trade_dates" in called, "未预热交易日历"
    assert called.get("next_trade_date") == "2025-10-09", "未按交易日预热 next_trade_date"
    # **`spot([codes])` 必须真的被预热**（第三轮独立复核：这处原先零覆盖）——
    # 它覆盖 `engine._enrich_spot`，也就是 `_float_shares`（券源上限）的取数点；
    # 不预热的话守卫开火会让 `source_limit` 变 0 = **券源上限静默跳过**。
    assert called.get("spot:codes") is True, (
        f"未预热 spot([codes]) —— `_enrich_spot` 会在锁内取数（F3 的隐患）：{called}"
    )
    assert "spot:codes" in warmed, f"warmed 里没有 spot:codes：{warmed}"

    # 非空守卫：trade_date 为 None 时，必须**明显少做事**（证明这个参数是必需的）
    called.clear()
    warmed_none = cli_mod.prewarm_for_write(FakeDS(), FakeState(), None, extra_codes=("601398",))
    assert not any(k.startswith("quote:") for k in called), (
        "trade_date=None 时不应假装预热了 quote —— 否则本测试无法区分参数是否被使用"
    )
    assert len(warmed_none) < len(warmed), (
        f"trade_date=None 的预热量应与正常情况不同：{len(warmed_none)} vs {len(warmed)}"
    )
