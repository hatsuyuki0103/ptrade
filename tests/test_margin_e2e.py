"""tests/test_margin_e2e.py — V1 两融完整矩阵**端到端对抗**（task-30）。

走 **CLI 真链路**（`subprocess` 调 `python -m ptrade ... --offline --json`），数据根是**隔离的
tmp 目录**（绝不碰仓库根 `data/cache`）。所有断言引用 `INTERFACES.md` v3.5 的公式行号。

契约真源与关键行号
------------------
* §3.2 L23 分子 = `available_cash + frozen_collateral + long_market_value`（**含**融券冻结券款，v3.1 修正）
* §3.2 L24 分母 = `financing_debt + short_market_value + interest_accrued`
* §3.2 L44-47 数值基准：融资满额 N=625000 → ratio 1.8；融券 20 万 → 担保物 700000 / ratio 3.5
* §3.2 L65/L67/L81-84 **小仓位本来就不该被强平**（1.30 对小仓位不可达）→ 必须有反例
* §3.2.1 L90-98 额度全部由**实时** `net_assets` 派生；L101 禁止缓存额度；L105 利息反向压缩额度
* §5 强约束 10 校验顺序：参数→停牌→涨跌停→风险门禁→白名单→手数→T+1→现金→保证金→杠杆→券源→仓位

每个判据的断言消息都带**判据标签**（如 `A2-formula`），便于变异验证核对「红来自预期断言」。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ptrade import core  # noqa: E402
from ptrade.config import load_config  # noqa: E402
from ptrade.datasource import DataSource  # noqa: E402

FIXTURE_DIR = ROOT / "data" / "fixture"
PY = sys.executable

#: 放宽到能建出「引擎可达的穿仓」仓位（默认 20% 单票上限下，1.30 在真实夹具里不可达 ——
#: 见 .omx/probe/margin_e2e_scenario.py：最深聚合跌幅 58.84% → ratio 1.4336 > 1.30）
REACHABLE_CONFIG = {
    "risk": {"max_position_pct": 2.0, "max_gross_exposure": 10.0, "max_leverage": 2.0},
    "margin": {"margin_ratio_long": 0.50},
}


# --------------------------------------------------------------------------- #
# CLI 驱动器
# --------------------------------------------------------------------------- #
class Cli:
    """隔离数据根 + CLI 调用器。"""

    def __init__(self, root: Path):
        self.root = root

    def run(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [PY, "-m", "ptrade", "--root", str(self.root), *args, "--offline", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
        )
        payload: dict = {}
        text = (proc.stdout or "").strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"ok": False, "error": f"非 JSON 输出: {text[:200]}"}
        return proc.returncode, payload

    def ok(self, *args: str) -> dict:
        code, payload = self.run(*args)
        assert code == 0 and payload.get("ok") is True, f"CLI 失败 {args}: {payload.get('error')}"
        return payload.get("data") or {}

    def order(self, **kw) -> dict:
        args = ["order", "--code", str(kw["code"]), "--side", str(kw["side"]), "--qty", str(kw["qty"])]
        if kw.get("price") is not None:
            args += ["--price", repr(float(kw["price"]))]
        if kw.get("trade_date"):
            args += ["--trade-date", str(kw["trade_date"])]
        data = self.ok(*args)
        return (data.get("orders") or [{}])[0]

    def settle(self, trade_date: str) -> dict:
        return self.ok("settle", "--trade-date", trade_date)

    def status(self) -> dict:
        return self.ok("status")

    def risk(self) -> dict:
        return self.ok("risk")

    def state(self) -> dict:
        """直接读 <root>/data/state.json 的 state 段（含 core 派生的两融字段）。"""
        doc = json.loads((self.root / "data" / "state.json").read_text(encoding="utf-8"))
        return doc["state"] if "state" in doc else doc

    def init_at(self, trade_date: str, *, cash: float = 500_000.0) -> dict:
        """把账户初始化到**指定交易日**（CLI 的 `init` 会钉在最近交易日，故测试自行落盘）。

        这是测试装置（写入自己的隔离数据根），之后所有计算仍走 CLI 真链路。
        """
        return seed_state(self, trade_date, cash=cash)


def make_cli(tmp_path: Path, name: str, *, config: dict | None = None) -> Cli:
    root = tmp_path / name
    (root / "data").mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DIR, root / "data" / "fixture")
    (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
    if config:
        (root / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return Cli(root)


@lru_cache(maxsize=None)
def _margin_flags() -> dict[str, tuple[bool, bool]]:
    frame = _fixture_ds().margin_targets()
    return {
        str(c): (bool(f), bool(s))
        for c, f, s in zip(frame["code"], frame["can_finance"], frame["can_short"])
    }


@lru_cache(maxsize=None)
def _daily_fixture_codes() -> frozenset[str]:
    """有日线夹具的代码（`code_last_date()` / 历史下单都依赖它，不能只在 spot 里有报价）。"""
    return frozenset(
        path.stem.split("_")[1]
        for path in FIXTURE_DIR.glob("daily_*.json")
        if "_unadjust" not in path.stem
    )


def find_code(*, short: bool | None = None, margin: bool | None = None) -> str:
    """找一只满足两融属性、**有行情且有日线夹具**的票（白名单/顺序/T+1 用例）。"""
    flags = _margin_flags()
    tradable = _tradable_codes()
    daily = _daily_fixture_codes()
    for code in sorted(daily):
        can_finance, can_short = flags.get(code, (False, False))
        if short is not None and can_short is not short:
            continue
        if margin is not None and can_finance is not margin:
            continue
        if code in tradable:
            return code
    pytest.skip(f"夹具里找不到 short={short} margin={margin} 且有行情+日线的标的")


def seed_state(
    cli: Cli,
    trade_date: str,
    *,
    cash: float = 500_000.0,
    financing_debt: float = 0.0,
    short_cost_debt: dict | None = None,
    short_qty_debt: dict | None = None,
    frozen_collateral: float = 0.0,
) -> dict:
    """写入 <root>/data/state.json（用 core 自己的序列化器，契约口径一致）。"""
    cfg = load_config(str(cli.root))
    state = core.new_state(cfg)
    state.trade_date = trade_date
    state.available_cash = float(cash)
    state.financing_debt = float(financing_debt)
    state.frozen_collateral = float(frozen_collateral)
    state.short_cost_debt = dict(short_cost_debt or {})
    state.short_qty_debt = dict(short_qty_debt or {})
    path = cli.root / "data" / "state.json"
    path.write_text(
        json.dumps(core.state_document(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return core.state_document(state)


# --------------------------------------------------------------------------- #
# 夹具行情工具
# --------------------------------------------------------------------------- #
def _fixture_ds() -> DataSource:
    return DataSource(load_config(str(ROOT)), offline=True)


@lru_cache(maxsize=None)
def series(code: str) -> dict[str, float]:
    """某票的日线收盘序列（按日期）。

    进程内缓存：spot/margin 夹具是 MB 级、全市场量级，反复解析会把用例拖到十几分钟。
    """
    frame = _fixture_ds().daily(code, "2024-01-01", "2030-12-31")
    return {str(d)[:10]: float(c) for d, c in zip(frame["date"], frame["close"])}


@lru_cache(maxsize=None)
def _tradable_codes() -> dict[str, float]:
    """最近交易日**有正价格**的代码 → 价格映射（spot 夹具是全市场快照）。"""
    ds = _fixture_ds()
    try:
        frame = ds.spot()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for code, last in zip(frame["code"], frame["last"]):
        try:
            value = float(last)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[str(code).zfill(6)] = value
    return out


def drop_pair(code: str, min_drop: float = 0.30) -> tuple[str, str, float, float]:
    """找 (D1 峰值日, D2 谷底日, P1, P2) 且跌幅 ≥ min_drop。"""
    px = series(code)
    dates = sorted(px)
    peak_i = 0
    best: tuple[float, str, str, float, float] | None = None
    for i, d in enumerate(dates):
        if px[d] > px[dates[peak_i]]:
            peak_i = i
        drop = (px[dates[peak_i]] - px[d]) / px[dates[peak_i]]
        if drop >= min_drop and (best is None or drop > best[0]):
            best = (drop, dates[peak_i], d, px[dates[peak_i]], px[d])
    if best is None:
        pytest.skip(f"{code} 在夹具内没有 ≥{min_drop:.0%} 的回撤，无法构造穿仓")
    return best[1], best[2], best[3], best[4]


def rally_pair(code: str, min_gain: float = 0.30) -> tuple[str, str, float, float]:
    """找 (D1 低点日, D2 高点日, P1, P2) 且涨幅 ≥ min_gain（用于融上再融浮盈）。"""
    px = series(code)
    dates = sorted(px)
    low_i = 0
    best: tuple[float, str, str, float, float] | None = None
    for i, d in enumerate(dates):
        if px[d] < px[dates[low_i]]:
            low_i = i
        gain = (px[d] - px[dates[low_i]]) / px[dates[low_i]]
        if gain >= min_gain and (best is None or gain > best[0]):
            best = (gain, dates[low_i], d, px[dates[low_i]], px[d])
    if best is None:
        pytest.skip(f"{code} 在夹具内没有 ≥{min_gain:.0%} 的涨幅")
    return best[1], best[2], best[3], best[4]


def latest_trade_date() -> str:
    return DataSource(load_config(str(ROOT)), offline=True).latest_trade_date()


def code_last_date(code: str) -> str:
    """该票**自己**日线序列的最后一天（用它做 asof 才能保证 `quote()` 有行情）。

    注意：`latest_trade_date()` 来自交易日历/全市场快照，个别票的日线夹具并没有覆盖到那天，
    直接用它当 asof 会被 `quote()` 判成停牌（实测 600519/600110 在 2026-10-15 均如此）。
    """
    return max(series(code))


def round_lot(qty: float) -> int:
    return int(qty // 100) * 100


# =========================================================================== #
# A. 维持担保比例 → 穿仓 → 强平
# =========================================================================== #
def ratio_of(state: dict) -> float:
    """按 §3.2 L23/L24 从 state 字段现算维持担保比例（不依赖产品内部属性）。"""
    cash = float(state.get("available_cash") or 0.0)
    frozen_collateral = float(state.get("frozen_collateral") or 0.0)
    long_qty = 0
    short_value = 0.0
    long_mv = 0.0
    for pos in (state.get("positions") or {}).values():
        px = float(pos.get("last_price") or 0.0)
        long_qty += int(pos.get("long_qty") or 0)
        long_mv += int(pos.get("long_qty") or 0) * px
        short_value += int(pos.get("short_qty") or 0) * px
    debt = (
        float(state.get("financing_debt") or 0.0)
        + short_value
        + float(state.get("interest_accrued") or 0.0)
    )
    collateral = cash + frozen_collateral + long_mv
    return float("inf") if debt <= 0 else collateral / debt


def test_a1_thresholds_are_exact_via_cli(tmp_path):
    """A1：警戒线 1.50 / 平仓线 1.30 的**真实数值**触发点（§3.2 L23/L24 + core 严格 `<`）。"""
    cli = make_cli(tmp_path, "a1")
    cash = 500_000.0

    # ratio == 1.30 恰好 → 只是追保（不是强平）
    seed_state(cli, latest_trade_date(), cash=cash, financing_debt=cash / 1.30)
    risk = cli.risk()
    assert risk["maintenance_ratio"] == pytest.approx(1.30, abs=1e-6), "A1-formula: ratio 应为 1.30"
    assert risk["status"] == "margin_call", "A1-threshold: 恰好 1.30 必须是 margin_call"
    codes = {a["code"] for a in risk["alerts"]}
    assert "MAINTENANCE_WARN" in codes and "MAINTENANCE_FORCED" not in codes, "A1-alert-codes"

    # ratio == 1.50 恰好 → 尚未追保（core 用严格 <）
    seed_state(cli, latest_trade_date(), cash=cash, financing_debt=cash / 1.50)
    risk = cli.risk()
    assert risk["maintenance_ratio"] == pytest.approx(1.50, abs=1e-6), "A1-formula: ratio 应为 1.50"
    assert risk["status"] == "safe", "A1-threshold: 恰好 1.50 不得判追保（严格 <）"

    # 略低于 1.50 → 追保
    seed_state(cli, latest_trade_date(), cash=cash, financing_debt=cash / 1.4999)
    assert cli.risk()["status"] == "margin_call", "A1-threshold: 1.4999 必须追保"

    # 略低于 1.30 → 强平
    seed_state(cli, latest_trade_date(), cash=cash, financing_debt=cash / 1.2999)
    risk = cli.risk()
    assert risk["status"] == "forced", "A1-threshold: 1.2999 必须强平"
    assert "MAINTENANCE_FORCED" in {a["code"] for a in risk["alerts"]}, "A1-alert-codes: forced"


def test_a1b_short_collateral_counts_in_numerator(tmp_path):
    """A1b（§3.2 L23/L46）：融券冻结券款**计入**分子 —— 与排除口径必须可区分。

    契约基准（L46）：融券卖出 20 万 → 担保物 700000 = 500000 现金 + 200000 券款 → ratio 3.5。
    v2 曾把券款排除在分子外（L17 的历史错误），L28-33 的 v3.1 修正把它改回**计入**。
    """
    cli = make_cli(tmp_path, "a1b")
    code = find_code(short=True)
    td = code_last_date(code)
    px = series(code)[td]
    # 单票 20% 上限 = 100000，故取 ~95000 名义金额（L46 的 20 万基准在默认参数下不可达）
    qty = round_lot(95_000 / px) or 100
    cli.init_at(td)
    short = cli.order(code=code, side="short_sell", qty=qty, price=px, trade_date=td)
    assert short["status"] == "filled", f"A1b-setup: {short.get('error_code')}"

    st = cli.state()
    risk = cli.risk()
    notional = qty * px
    cash = float(st["available_cash"])
    frozen_collateral = float(st["frozen_collateral"])
    interest = float(st["interest_accrued"])
    long_mv = 0.0  # 纯融券仓：没有多头市值
    short_mv = notional
    # §3.2 L23：分子 = available_cash + frozen_collateral + long_market_value
    inclusive = (cash + frozen_collateral + long_mv) / (short_mv + interest)
    # v2 的历史错误（L17）：把券款排除在分子外
    exclusive = (cash + long_mv) / (short_mv + interest)

    assert frozen_collateral == pytest.approx(notional, rel=1e-9), "A1b-setup: 券款应全额冻结"
    assert abs(inclusive - exclusive) > 1e-6, "A1b-setup: 两种口径必须可区分（非空洞）"
    # CLI 把 maintenance_ratio 序列化到 6 位小数，故容差取 1e-6
    assert risk["maintenance_ratio"] == pytest.approx(inclusive, abs=1e-6), (
        "A1b-numerator: 分子必须含 frozen_collateral（§3.2 L23/L46）"
    )
    assert risk["maintenance_ratio"] != pytest.approx(exclusive, abs=1e-6), (
        "A1b-numerator: 不得用「排除券款」的口径（那是 v2 的历史错误）"
    )


def test_a2_price_drop_forces_liquidation_with_formula(tmp_path):
    """A2：真实夹具回撤 → `maintenance_ratio < 1.30` → forced，且**数值与 §3.2 公式一致**。

    默认参数下真实夹具**不可达** 1.30（探针：最深聚合跌幅 58.84% → 1.4336），
    故用 config.json 放宽 `max_position_pct` 并降 `margin_ratio_long` 到 0.50，使仓位可达且
    开仓 ratio = (M+N)/N = 1.50；再用真实回撤（≥30%）把它压到 1.30 以下。
    """
    cli = make_cli(tmp_path, "a2", config=REACHABLE_CONFIG)
    code = "600110"
    d1, d2, p1, p2 = drop_pair(code, min_drop=0.30)
    cli.init_at(d1)
    # 990000 留出手续费余量：leverage = N/(M−F) 必须 ≤ 2.0（999733/499740 = 2.0005 → 会被 LEVERAGE_LIMIT 拒）
    qty = round_lot(990_000 / p1)
    open_order = cli.order(code=code, side="margin_buy", qty=qty, price=p1, trade_date=d1)
    assert open_order["status"] == "filled", f"A2-open: {open_order.get('error_code')}"

    st = cli.state()
    notional = qty * p1
    # 开仓后：§3.2 L23/L24 —— 分子含 available_cash + long_mv，分母 = financing_debt
    assert float(st["financing_debt"]) == pytest.approx(notional, rel=1e-9), "A2-debt=N 全额"
    assert ratio_of(st) == pytest.approx(1.50, abs=0.01), "A2-formula: 开仓 ratio=(M+N)/N=1.50"

    cli.settle(d2)
    st2 = cli.state()
    risk = cli.risk()
    expected = ratio_of(st2)
    assert expected < 1.30, f"A2-setup: 该回撤应把 ratio 压到 1.30 以下（实际 {expected:.4f}）"
    assert risk["maintenance_ratio"] == pytest.approx(expected, abs=1e-6), (
        "A2-formula: risk 报出的 ratio 必须 == (cash+frozen_collateral+long_mv)/"
        "(financing_debt+short_mv+interest)"
    )
    assert risk["status"] == "forced", "A2-status: 跌破 1.30 → forced"
    assert st2["trade_date"] == d2, "A2-chain: 必须真的 settle 到 D2"
    # 价格确实按夹具下跌
    assert p2 < p1


def test_a3_small_position_drop_does_not_force(tmp_path):
    """★ A3 反例（§3.2 L67/L81-84）：**小仓位**即使大幅下跌也**不该**被强平。"""
    cli = make_cli(tmp_path, "a3")  # 默认参数
    code = "600110"
    d1, d2, p1, p2 = drop_pair(code, min_drop=0.30)
    cli.init_at(d1)
    qty = round_lot(0.20 * 500_000 / p1)  # 单票 20% 上限内的**小仓**
    order = cli.order(code=code, side="margin_buy", qty=qty, price=p1, trade_date=d1)
    assert order["status"] == "filled", f"A3-open: {order.get('error_code')}"

    cli.settle(d2)
    risk = cli.risk()
    # 小仓位下 1.30 的代数根是负价（§3.2 L81-83），故 ratio 有下界 > 1.30
    floor = 500_000.0 / (qty * p1)
    assert risk["maintenance_ratio"] >= floor - 1e-6, f"A3-floor: ratio 下界应为 M/N={floor:.4f}"
    assert risk["status"] != "forced", "A3-counter-example: 小仓位下跌不得触发强平"
    assert risk["maintenance_ratio"] > 1.30, "A3-counter-example: ratio 仍应 > 1.30"


def test_a4_after_forced_only_reduction_and_reduction_works(tmp_path):
    """A4：强平状态下**加仓被拒**（BELOW_MAINTENANCE），但**减仓必须真的能过**。"""
    cli = make_cli(tmp_path, "a4", config=REACHABLE_CONFIG)
    code = "600110"
    d1, d2, p1, p2 = drop_pair(code, min_drop=0.30)
    cli.init_at(d1)
    qty = round_lot(990_000 / p1)
    assert cli.order(code=code, side="margin_buy", qty=qty, price=p1, trade_date=d1)["status"] == "filled"
    cli.settle(d2)  # 价格跌到谷底 → forced
    assert cli.risk()["status"] == "forced", "A4-setup: 需要 forced 状态"

    # 加仓 → 拒，且错误码是 BELOW_MAINTENANCE（风险门禁先于白名单/仓位）
    add = cli.order(code=code, side="buy", qty=100, price=p2, trade_date=d2)
    assert add["status"] == "rejected" and add["error_code"] == "BELOW_MAINTENANCE", (
        f"A4-gate: 强平后加仓必须 BELOW_MAINTENANCE，实际 {add.get('error_code')}"
    )
    assert cli.order(code=code, side="margin_buy", qty=100, price=p2, trade_date=d2)["error_code"] == (
        "BELOW_MAINTENANCE"
    ), "A4-gate: 融资加仓同样被拒"

    # 减仓 → 必须真的能过（T+1 已 settle，可用量足够）
    sell = cli.order(code=code, side="sell", qty=100, price=p2, trade_date=d2)
    assert sell["status"] == "filled", f"A4-reduce: 强平后减仓必须放行，实际 {sell.get('error_code')}"


def test_a5_margin_call_blocks_add_but_allows_repay(tmp_path):
    """A5：追保状态下加仓 → MARGIN_CALL_BLOCKED；**还款/还券必须能过**。"""
    cli = make_cli(tmp_path, "a5")
    code = find_code()  # 必须选一只在该 asof 真有行情的票（部分票的日线没覆盖到最近交易日）
    td = code_last_date(code)
    # ratio = 1.40 → 追保（1.30 ≤ 1.40 < 1.50）
    seed_state(cli, td, cash=500_000.0, financing_debt=500_000.0 / 1.40)
    risk = cli.risk()
    assert risk["status"] == "margin_call", "A5-setup: 需要 margin_call 状态"

    blocked = cli.order(code=code, side="buy", qty=100, trade_date=td)
    assert blocked["error_code"] == "MARGIN_CALL_BLOCKED", (
        f"A5-gate: 追保期加仓必须 MARGIN_CALL_BLOCKED，实际 {blocked.get('error_code')}"
    )
    _, payload = cli.run("order", "--code", code, "--side", "sell", "--qty", "100",
                         "--trade-date", td)
    code_err = (payload.get("data", {}).get("orders") or [{}])[0].get("error_code")
    assert code_err != "MARGIN_CALL_BLOCKED", f"A5-reduce: 减仓不得被追保门禁拦（实际 {code_err}）"


# =========================================================================== #
# B. 融上再融（INV-22 / §3.2.1）
# =========================================================================== #
def test_b6_all_three_quotas_grow_with_unrealized_profit(tmp_path):
    """B6：浮盈 → `net_assets` ↑ → ①单票上限 ②保证金额度 ③杠杆余量**三条都上调**。"""
    cli = make_cli(tmp_path, "b6")
    code = "600110"
    d1, d2, p1, p2 = rally_pair(code, min_gain=0.30)
    cli.init_at(d1)
    qty = round_lot(0.20 * 500_000 / p1)
    assert cli.order(code=code, side="margin_buy", qty=qty, price=p1, trade_date=d1)["status"] == "filled"

    before = cli.risk()["exposure"]
    cli.settle(d2)
    after = cli.risk()["exposure"]

    assert after["net_assets"] > before["net_assets"], "B6-setup: 浮盈应抬高 net_assets"
    cap_before = 0.20 * before["net_assets"]
    cap_after = 0.20 * after["net_assets"]
    assert cap_after > cap_before, f"B6-position: 单票上限应上升（{cap_before:.0f}→{cap_after:.0f}）"
    assert after["margin_available"] > before["margin_available"], (
        f"B6-margin: 可再融资额度应上升（{before['margin_available']:.0f}→{after['margin_available']:.0f}）"
    )
    lev_head_before = 2.0 * before["net_assets"] - before["total_debt"]
    lev_head_after = 2.0 * after["net_assets"] - after["total_debt"]
    assert lev_head_after > lev_head_before, "B6-leverage: 杠杆余量应上升"
    # 公式自洽（§3.2.1 L97）
    assert after["margin_available"] == pytest.approx(
        after["net_assets"] - after["margin_required"], abs=1e-6
    ), "B6-formula: margin_available == net_assets − margin_required"


def test_b7_quota_is_realtime_via_differential_cli(tmp_path):
    """★ B7：**开仓后立刻再开一仓**的可用额度 > 用**初始**额度静态算出的值（融上再融定义）。

    差分证明：同一笔 N2（> 初始上限 100000、≤ 浮盈后上限）在「浮盈后」账户**成交**，
    在「浮盈前」对照账户**被 POSITION_LIMIT_PCT 拒**。
    """
    code = "600110"
    d1, d2, p1, p2 = rally_pair(code, min_gain=0.30)
    second = "600036"

    def build(name: str, *, advance: bool):
        cli = make_cli(tmp_path, name)
        cli.init_at(d1)
        qty = round_lot(0.20 * 500_000 / p1)
        assert cli.order(code=code, side="margin_buy", qty=qty, price=p1, trade_date=d1)["status"] == "filled"
        if advance:
            cli.settle(d2)  # 浮盈
        return cli

    after = build("b7_after", advance=True)
    net_after = after.risk()["exposure"]["net_assets"]
    cap_after = 0.20 * net_after
    n2 = round_lot((cap_after * 0.95) / 20.0) * 20.0
    assert n2 > 100_000.0, f"B7-setup: N2={n2:.0f} 必须大于初始上限 100000"

    qty2 = round_lot(n2 / 20.0)
    hit_after = after.order(code=second, side="margin_buy", qty=qty2, price=20.0, trade_date=d2)
    assert hit_after["status"] == "filled", (
        f"B7-realtime: 浮盈后额度应放行 N2≈{qty2 * 20:.0f}（实际 {hit_after.get('error_code')}）"
    )

    before = build("b7_before", advance=False)
    hit_before = before.order(code=second, side="margin_buy", qty=qty2, price=20.0, trade_date=d1)
    assert hit_before["status"] == "rejected" and hit_before["error_code"] == "POSITION_LIMIT_PCT", (
        f"B7-differential: 同一笔单在浮盈前必须被仓位上限拒（实际 {hit_before.get('error_code')}）"
    )


def test_b8_quota_shrinks_on_unrealized_loss(tmp_path):
    """B8 反向压缩（§3.2.1 L105 要求必须可见）：浮亏 → 额度下降。"""
    cli = make_cli(tmp_path, "b8")
    code = "600110"
    d1, d2, p1, p2 = drop_pair(code, min_drop=0.30)
    cli.init_at(d1)
    qty = round_lot(0.20 * 500_000 / p1)
    assert cli.order(code=code, side="margin_buy", qty=qty, price=p1, trade_date=d1)["status"] == "filled"
    before = cli.risk()["exposure"]
    cli.settle(d2)
    after = cli.risk()["exposure"]
    assert after["net_assets"] < before["net_assets"], "B8-setup: 浮亏应压低 net_assets"
    assert after["margin_available"] < before["margin_available"], "B8-shrink: 可再融资额度必须下降"
    assert 0.20 * after["net_assets"] < 0.20 * before["net_assets"], "B8-position: 单票上限必须下降"


def test_b9_quota_uses_live_net_assets_no_cache(tmp_path):
    """★ B9 不得缓存：同一进程内改状态后，额度立刻反映**当时**的 `net_assets`（§3.2.1 L101）。"""
    import io
    from contextlib import redirect_stdout

    from ptrade import cli as cli_mod
    from ptrade.engine import Engine

    root = tmp_path / "b9"
    (root / "data").mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DIR, root / "data" / "fixture")
    (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
    cfg = load_config(str(root))
    state = core.new_state(cfg)
    state.trade_date = latest_trade_date()
    engine = Engine(cfg, DataSource(cfg, offline=True), state)

    first = engine.position_sizing("600519", 100.0, 90.0)
    # 直接改变实时净资产的来源（融资亏损）→ 同一引擎实例再问一次
    engine.state.financing_debt = 200_000.0
    second = engine.position_sizing("600519", 100.0, 90.0)
    assert float(engine.state.net_assets) < 500_000.0, "B9-setup: 负债应压低净资产"
    assert second["qty"] < first["qty"], (
        f"B9-no-cache: 额度必须由**当时**的 net_assets 派生（{first['qty']} → {second['qty']}）"
    )
    assert second["binding_constraint"] in {"position_pct", "cash", "exposure"}, "B9-constraint"


# =========================================================================== #
# C. T+1 × 两融
# =========================================================================== #
def test_c10_margin_buy_is_t1_locked_then_sellable(tmp_path):
    """C10：融资买入当日**不可卖**（T1_NOT_SETTLED），settle 后可卖。"""
    cli = make_cli(tmp_path, "c10")
    code = "600110"
    d1, d2, p1, p2 = drop_pair(code, min_drop=0.05)  # 只为取两个相邻交易日
    cli.init_at(d1)
    qty = 100
    assert cli.order(code=code, side="margin_buy", qty=qty, price=p1, trade_date=d1)["status"] == "filled"

    same_day = cli.order(code=code, side="sell", qty=qty, price=p1, trade_date=d1)
    assert same_day["error_code"] == "T1_NOT_SETTLED", (
        f"C10-t1: 融资买入当日必须锁定，实际 {same_day.get('error_code')}"
    )
    cli.settle(d2)
    # 跨日必须用**当日**价格（用 D1 价会撞上 D2 的涨停/跌停带，那是正确拒单而非 T+1 问题）
    after = cli.order(code=code, side="sell", qty=qty, price=p2, trade_date=d2)
    assert after["status"] == "filled", f"C10-after-settle: 跨日必须可卖（{after.get('error_code')}）"


def test_c11_short_sell_is_not_t1_constrained_independent_verification(tmp_path):
    """★ C11：**独立验证**「SHORT_SELL 不受 T+1 约束」——用差分证明，不抄既有结论。

    判据：融券**开仓**当日即可成交（不受 T+1）；而它产生的欠券当日**不可还**
    （`short_available == 0` → BUY_TO_COVER 当日 T1_NOT_SETTLED）。两者同时成立才说明
    「融券开仓不受 T+1，但券的可用性受 T+1」。
    """
    cli = make_cli(tmp_path, "c11")
    code = find_code(short=True)  # 融券开仓需要**可融券**标的
    td = code_last_date(code)
    cli.init_at(td)
    px = series(code)[td]
    short = cli.order(code=code, side="short_sell", qty=100, price=px, trade_date=td)
    assert short["status"] == "filled", f"C11-open: 融券开仓当日必须可成交（{short.get('error_code')}）"
    assert short["error_code"] == "OK", "C11-open: 不得因 T+1 被拒"

    # 同一个「当日」里，买券还券必须被 T+1 拦住（说明券的可用性是 0，而不是「融券不受 T+1」）
    cover = cli.order(code=code, side="buy_to_cover", qty=100, price=px, trade_date=td)
    assert cover["error_code"] == "T1_NOT_SETTLED", (
        f"C11-differential: 当日还券必须 T1_NOT_SETTLED，实际 {cover.get('error_code')}"
    )
    st = cli.state()
    assert int(st["short_qty_debt"].get(code, 0)) == 100, "C11-state: 欠券应已入账"


def test_c12_cover_and_repay_t1_interactions(tmp_path):
    """C12：买券还券 / 卖券还款的 T+1 交互（当日拒、跨日过后成交且负债下降）。"""
    cli = make_cli(tmp_path, "c12")
    code = find_code(short=True)  # 融券 + 融资都需要两融标的
    d1, d2, p1, p2 = drop_pair(code, min_drop=0.05)
    cli.init_at(d1)
    px1 = series(code)[d1]
    px2 = series(code)[d2]  # 跨日必须用**当日**价（否则会撞 D2 的涨跌停带）

    # 融券：当日还券被拒 → 跨日成交，且 short_qty_debt 清零
    assert cli.order(code=code, side="short_sell", qty=100, price=px1, trade_date=d1)["status"] == "filled"
    assert cli.order(code=code, side="buy_to_cover", qty=100, price=px1, trade_date=d1)["error_code"] == (
        "T1_NOT_SETTLED"
    ), "C12-cover-t1"
    cli.settle(d2)
    cover = cli.order(code=code, side="buy_to_cover", qty=100, price=px2, trade_date=d2)
    assert cover["status"] == "filled", f"C12-cover: 跨日还券必须成交（{cover.get('error_code')}）"
    assert int(cli.state()["short_qty_debt"].get(code, 0)) == 0, "C12-cover: 欠券应清零"

    # 融资：当日卖券还款被拒（T+1）→ 跨日成交且 financing_debt 下降
    cli2 = make_cli(tmp_path, "c12b")
    cli2.init_at(d1)
    assert cli2.order(code=code, side="margin_buy", qty=100, price=px1, trade_date=d1)["status"] == "filled"
    debt_before = float(cli2.state()["financing_debt"])
    assert debt_before > 0
    same_day = cli2.order(code=code, side="repay_margin", qty=100, price=px1, trade_date=d1)
    assert same_day["error_code"] == "T1_NOT_SETTLED", f"C12-repay-t1: 实际 {same_day.get('error_code')}"
    cli2.settle(d2)
    repay = cli2.order(code=code, side="repay_margin", qty=100, price=px2, trade_date=d2)
    assert repay["status"] == "filled", f"C12-repay: 跨日还款必须成交（{repay.get('error_code')}）"
    assert float(cli2.state()["financing_debt"]) < debt_before, "C12-repay: 融资负债必须下降"


# =========================================================================== #
# D. 校验顺序确定性（§5 强约束 10）
# =========================================================================== #
def test_d13_order_of_checks_first_error_wins(tmp_path):
    """D13：多条件同时命中 → 返回**第一个**错误码（§5 强约束 10 的固定顺序）。"""
    cli = make_cli(tmp_path, "d13")
    ds = DataSource(load_config(str(ROOT)), offline=True)

    # 确实不可融券、但**有有效行情+日线**的标的（夹具里 601398 等剧情票都可融券）
    non_short = find_code(short=False)
    td = code_last_date(non_short)  # 用它自己的最后交易日，保证 quote 有行情
    cli.init_at(td)
    quote = ds.quote(non_short, td)

    # ① 手数非法 + 停牌/白名单：手数在**白名单之后**，故 150 股的非融券标的应报白名单
    order1 = cli.order(code=non_short, side="short_sell", qty=150, price=float(quote["last"]), trade_date=td)
    assert order1["error_code"] == "NOT_SHORT_TARGET", (
        f"D13-1: 白名单先于手数，实际 {order1.get('error_code')}"
    )

    # ② 涨跌停 先于 白名单：跌停价卖出方向 → LIMIT_DOWN_NO_SELL（而不是白名单/手数）
    order2 = cli.order(code=non_short, side="short_sell", qty=100,
                       price=float(quote["limit_down"]), trade_date=td)
    assert order2["error_code"] == "LIMIT_DOWN_NO_SELL", (
        f"D13-2: 涨跌停先于白名单，实际 {order2.get('error_code')}"
    )

    # ③ 风险门禁 先于 白名单 与 仓位：强平账户 + 非融资标的 → BELOW_MAINTENANCE
    seed_state(cli, td, cash=500_000.0, financing_debt=500_000.0 / 1.20)  # ratio 1.20 → forced
    order3 = cli.order(code=non_short, side="margin_buy", qty=100, price=float(quote["last"]), trade_date=td)
    assert order3["error_code"] == "BELOW_MAINTENANCE", (
        f"D13-3: 风险门禁先于白名单，实际 {order3.get('error_code')}"
    )

    # ④ 手数 先于 T+1：当日买入后卖 150 股（非整百）→ INVALID_LOT
    cli2 = make_cli(tmp_path, "d13b")
    code = find_code()
    td = code_last_date(code)
    px = series(code)[td]
    cli2.init_at(td)
    assert cli2.order(code=code, side="buy", qty=100, price=px, trade_date=td)["status"] == "filled"
    order4 = cli2.order(code=code, side="sell", qty=150, price=px, trade_date=td)
    assert order4["error_code"] == "INVALID_LOT", f"D13-4: 手数先于 T+1，实际 {order4.get('error_code')}"
    order5 = cli2.order(code=code, side="sell", qty=100, price=px, trade_date=td)
    assert order5["error_code"] == "T1_NOT_SETTLED", "D13-5: 手数合法后 T+1 命中"

    # ⑤ 参数合法性（stage 1）必须压过手数（stage 6）
    #    a) 空代码 → NO_MARKET_DATA 先命中（而不是 INVALID_LOT）
    empty_code = cli2.order(code="", side="buy", qty=150, price=px, trade_date=td)
    assert empty_code["error_code"] == "NO_MARKET_DATA", (
        f"D13-6: 参数合法性先于手数，实际 {empty_code.get('error_code')}"
    )
    #    b) 非法价格 → NO_PRICE 先命中
    bad_price = cli2.order(code=code, side="buy", qty=150, price=0.0, trade_date=td)
    assert bad_price["error_code"] == "NO_PRICE", (
        f"D13-7: 非法价格先于手数，实际 {bad_price.get('error_code')}"
    )
    #    注：完全未知的 `--side bogus` 由 argparse 的 choices 在**更外层**拒绝（exit 2），
    #        根本进不到引擎 —— 与 §5「参数合法性最优先」一致，故此处不重复断言。
