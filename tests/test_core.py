"""tests/test_core.py —— 领域模型与账户数学（INTERFACES.md v3.1 §3）。

覆盖：ErrorCode/Side 枚举、calc_fees、§3.3 账户数学表、§3.2 v3 数值基准表（INV-4）、
不变量 I1/I2/I3/I4 与 INV-5/6/8/9/10、纯函数性（不改入参）、T+1 结算、风险告警、序列化。
"""

from __future__ import annotations

import copy
import json
import math

import pytest

from ptrade import core
from ptrade.config import FeeConfig, MarginConfig, RiskConfig, load_config
from ptrade.core import AccountState, ErrorCode, Lot, Order, Position, Side

TOL = 1e-6
DATE = "2025-10-09"


def approx(value, expected, tol=TOL):
    return abs(value - expected) <= tol


def make_order(side, code="600519", qty=100, price=100.0, trade_date=DATE, name="测试") -> Order:
    return Order(
        order_id="O0001", ts=f"{trade_date}T15:00:00", trade_date=trade_date,
        code=code, name=name, side=side, qty=qty, price=price,
    )


def fill(state, side, code="600519", qty=100, price=100.0, trade_date=DATE, fees=False, cfg=None):
    """执行一笔成交并返回 (新状态, 订单)。``fees=True`` 时按 cfg.fee 计算费用。"""
    order = make_order(side, code, qty, price, trade_date)
    cfg = cfg or load_config()
    margin = cfg.margin
    if fees:
        commission, tax, transfer = core.calc_fees(side, price, qty, cfg.fee)
        order.fee, order.tax, order.transfer_fee = commission, tax, transfer
    plan = core.fill_plan(state, order, margin, cfg.fee if fees else None)
    order.cash_delta = plan["cash_delta"]
    order.margin_used = plan["margin_used"]
    order.debt_delta = plan["debt_delta"]
    order.realized_pnl = plan["realized_pnl"]
    new = core.apply_fill(state, order, margin, cfg.fee if fees else None)
    new.history.append(order)
    return new, order


@pytest.fixture
def cfg(tmp_path):
    return load_config(str(tmp_path))


@pytest.fixture
def state(cfg):
    return core.new_state(cfg)


# --------------------------------------------------------------------------- #
# 3.1 枚举与错误码
# --------------------------------------------------------------------------- #
def test_side_values():
    assert [s.value for s in Side] == [
        "buy", "sell", "margin_buy", "short_sell", "buy_to_cover", "repay_margin"
    ]


def test_error_codes_present_and_v3_renames():
    codes = {c.value for c in ErrorCode}
    for name in (
        "OK", "T1_NOT_SETTLED", "INSUFFICIENT_CASH", "INSUFFICIENT_POSITION",
        "NOT_MARGIN_TARGET", "NOT_SHORT_TARGET", "INSUFFICIENT_MARGIN_SPACE",
        "LEVERAGE_LIMIT", "MARGIN_CALL_BLOCKED", "BELOW_MAINTENANCE",
        "POSITION_LIMIT_PCT", "POSITION_LIMIT_COUNT", "EXPOSURE_LIMIT",
        "SHORT_SOURCE_LIMIT", "LIMIT_UP_NO_BUY", "LIMIT_DOWN_NO_SELL", "NO_PRICE",
        "NO_MARKET_DATA", "INVALID_LOT", "SUSPENDED", "ALREADY_EXISTS",
        "NOT_FOUND", "UNSUPPORTED",
    ):
        assert name in codes, name
    assert "INSUFFICIENT_MARGIN" not in codes  # v2 起改名，且不得保留兼容名


# --------------------------------------------------------------------------- #
# round_lot / calc_fees
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected", [(0, 0), (1, 0), (99, 0), (100, 100), (150, 100), (199, 100), (1000, 1000), (-50, -100)]
)
def test_round_lot(raw, expected):
    assert core.round_lot(raw) == expected


def test_calc_fees_buy_no_stamp():
    fee = FeeConfig()
    commission, tax, transfer = core.calc_fees(Side.BUY.value, 1000.0, 100, fee)
    assert approx(commission, 25.0)  # 100000 * 0.00025
    assert tax == 0.0
    assert approx(transfer, 1.0)  # 100000 * 0.00001


def test_calc_fees_sell_has_stamp():
    fee = FeeConfig()
    commission, tax, transfer = core.calc_fees(Side.SELL.value, 1000.0, 100, fee)
    assert approx(commission, 25.0)
    assert approx(tax, 50.0)  # 100000 * 0.0005
    assert approx(transfer, 1.0)


def test_calc_fees_stamp_tax_rule_per_contract():
    """卖出方向集合 = {SELL, SHORT_SELL}；BUY_TO_COVER 与买入类不收印花税。"""
    fee = FeeConfig()
    assert core.calc_fees(Side.SELL.value, 10.0, 1000, fee)[1] > 0
    assert core.calc_fees(Side.SHORT_SELL.value, 10.0, 1000, fee)[1] > 0
    assert core.calc_fees(Side.BUY_TO_COVER.value, 10.0, 1000, fee)[1] == 0
    assert core.calc_fees(Side.MARGIN_BUY.value, 10.0, 1000, fee)[1] == 0
    assert core.calc_fees(Side.REPAY_MARGIN.value, 10.0, 1000, fee)[1] == 0


def test_calc_fees_commission_min_and_zero_amount():
    fee = FeeConfig()
    assert approx(core.calc_fees(Side.BUY.value, 1.0, 100, fee)[0], 5.0)  # 触底 5 元
    assert core.calc_fees(Side.BUY.value, 0.0, 0, fee) == (0.0, 0.0, 0.0)


def test_calc_fees_transfer_flag_off():
    fee = FeeConfig()
    assert core.calc_fees(Side.BUY.value, 1000.0, 100, fee, transfer_fee=False)[2] == 0.0


# --------------------------------------------------------------------------- #
# §3.2 v3 数值基准表（INV-4）—— 零费用口径，逐格断言
# --------------------------------------------------------------------------- #
def test_inv4_baseline_row1_initial(state):
    assert approx(state.available_cash, 500000.0)
    assert approx(state.frozen_t1, 0.0) and approx(state.frozen_collateral, 0.0)
    assert approx(state.frozen_cash, 0.0)
    assert approx(state.long_market_value, 0.0)
    assert approx(state.financing_debt, 0.0)
    assert approx(state.collateral_value, 500000.0)
    assert approx(state.total_debt, 0.0)
    assert approx(state.net_assets, 500000.0)
    assert state.maintenance_ratio == math.inf
    assert approx(state.leverage, 0.0)


def test_inv4_baseline_row2_margin_buy_200k(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    assert approx(state.available_cash, 500000.0)  # v3：自有资金不出账
    assert approx(state.frozen_t1, 0.0) and approx(state.frozen_collateral, 0.0)
    assert approx(state.long_market_value, 200000.0)
    assert approx(state.financing_debt, 200000.0)  # v3：全额
    assert approx(state.collateral_value, 700000.0)
    assert approx(state.total_debt, 200000.0)
    assert approx(state.net_assets, 500000.0)
    assert approx(state.maintenance_ratio, 3.5)
    assert approx(state.total_assets, 700000.0)


def test_inv4_baseline_row3_price_down_20pct(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    state = core.mark_to_market(state, {"600519": 80.0})
    assert approx(state.available_cash, 500000.0)
    assert approx(state.long_market_value, 160000.0)
    assert approx(state.financing_debt, 200000.0)
    assert approx(state.collateral_value, 660000.0)
    assert approx(state.total_debt, 200000.0)
    assert approx(state.net_assets, 460000.0)
    assert approx(state.maintenance_ratio, 3.3)


def test_inv4_baseline_row4_short_sell_200k(cfg, state):
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=2000, price=100.0, cfg=cfg)
    assert approx(state.available_cash, 500000.0)
    assert approx(state.frozen_t1, 0.0)
    assert approx(state.frozen_collateral, 200000.0)
    assert approx(state.long_market_value, 0.0)
    assert approx(state.financing_debt, 0.0)
    assert approx(state.collateral_value, 700000.0)  # v3.1：券款计入担保物
    assert approx(state.total_debt, 200000.0)
    assert approx(state.net_assets, 500000.0)
    assert approx(state.maintenance_ratio, 3.5)


def test_inv4_baseline_row5_short_price_down_20pct(cfg, state):
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=2000, price=100.0, cfg=cfg)
    state = core.mark_to_market(state, {"000001": 80.0})
    assert approx(state.frozen_collateral, 200000.0)
    assert approx(state.short_market_value, 160000.0)
    assert approx(state.collateral_value, 700000.0)
    assert approx(state.total_debt, 160000.0)
    assert approx(state.net_assets, 540000.0)
    assert approx(state.maintenance_ratio, 4.375)


def test_baseline_with_fees_margin_buy_block(cfg, state):
    """v3.2 含费对照块（融资）：N=200000、F=52 → available 499948 / 担保物 699948 / ratio 3.49974。"""
    state, order = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg, fees=True)
    assert approx(order.total_fee, 52.0)
    assert approx(state.available_cash, 499948.0)
    assert approx(state.collateral_value, 699948.0)
    assert approx(state.maintenance_ratio, 3.49974, 1e-5)


def test_baseline_with_fees_short_block(cfg, state):
    """v3.2 含费对照块（融券）：N=200000、F=152 → available 499848 / 担保物 699848 / ratio 3.49924。"""
    state, order = fill(state, Side.SHORT_SELL.value, code="000001", qty=2000, price=100.0,
                        cfg=cfg, fees=True)
    assert approx(order.total_fee, 152.0)
    assert approx(state.available_cash, 499848.0)
    assert approx(state.frozen_collateral, 200000.0)
    assert approx(state.collateral_value, 699848.0)
    assert approx(state.maintenance_ratio, 3.49924, 1e-5)
    state = core.mark_to_market(state, {"000001": 80.0})
    assert approx(state.maintenance_ratio, 4.37405, 1e-5)


# --------------------------------------------------------------------------- #
# §3.3 账户数学表（六种方向）
# --------------------------------------------------------------------------- #
def test_table_buy(state):
    state, order = fill(state, Side.BUY.value, qty=100, price=1000.0)
    assert approx(state.available_cash, 500000.0 - 100000.0)
    assert approx(state.frozen_t1, 0.0) and approx(state.frozen_collateral, 0.0)
    assert approx(state.financing_debt, 0.0)
    assert approx(state.long_market_value, 100000.0)
    assert state.positions["600519"].long_qty == 100
    assert state.positions["600519"].long_available == 0  # 当日 locked
    assert state.positions["600519"].lots[0].kind == "cash"


def test_table_sell_goes_to_frozen_t1(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    state, order = fill(state, Side.SELL.value, qty=100, price=1000.0, trade_date="2025-10-10", cfg=cfg)
    assert approx(order.cash_delta, 0.0)
    assert approx(state.frozen_t1, 100000.0)
    assert approx(state.frozen_collateral, 0.0)
    assert approx(state.available_cash, 400000.0)  # 未变
    assert state.positions["600519"].long_qty == 0


def test_table_margin_buy_only_charges_fees(cfg, state):
    state, order = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg, fees=True)
    assert approx(order.cash_delta, -order.total_fee)
    assert approx(state.available_cash, 500000.0 - order.total_fee)
    assert approx(state.financing_debt, 200000.0)
    assert state.positions["600519"].lots[0].kind == "margin"


def test_table_short_sell_freezes_proceeds_and_records_debt(cfg, state):
    state, order = fill(state, Side.SHORT_SELL.value, code="000001", qty=1000, price=10.0, cfg=cfg)
    assert approx(state.frozen_collateral, 10000.0)
    assert approx(state.short_cost_debt["000001"], 10000.0)
    assert state.short_qty_debt["000001"] == 1000
    assert approx(order.debt_delta, 10000.0)


def test_table_buy_to_cover_releases_collateral(cfg, state):
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=1000, price=10.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    state, order = fill(state, Side.BUY_TO_COVER.value, code="000001", qty=1000, price=10.0,
                        trade_date="2025-10-10", cfg=cfg)
    assert approx(order.cash_delta, 0.0)  # -(N) + 券款
    assert approx(state.frozen_collateral, 0.0)
    assert state.short_qty_debt == {} and state.short_cost_debt == {}
    assert approx(state.net_assets, 500000.0)  # 同价还券，零损益


def test_repay_margin_is_full_repayment_only(cfg, state):
    """v3.2：REPAY_MARGIN 只支持全额还款（repay = financing_debt）。"""
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    plan = core.fill_plan(state, make_order(Side.REPAY_MARGIN.value, qty=2000, price=100.0,
                                            trade_date="2025-10-10"), cfg.margin)
    assert approx(plan["repay"], state.financing_debt)
    after = core.apply_fill(state, make_order(Side.REPAY_MARGIN.value, qty=2000, price=100.0,
                                              trade_date="2025-10-10"), cfg.margin)
    assert approx(after.financing_debt, 0.0)


def test_short_sell_is_not_t1_constrained(cfg, state):
    """v3.2：SHORT_SELL 是融券开仓，不受 T+1 可用量约束（同日可连续开仓）。"""
    state, first = fill(state, Side.SHORT_SELL.value, code="000001", qty=1000, price=10.0, cfg=cfg)
    state, second = fill(state, Side.SHORT_SELL.value, code="000001", qty=1000, price=10.0, cfg=cfg)
    assert first.status == core.FILLED and second.status == core.FILLED
    assert state.short_qty_debt["000001"] == 2000
    assert approx(state.frozen_collateral, 20000.0)
    # 但当日买券还券受 T+1 约束
    plan = core.fill_plan(state, make_order(Side.BUY_TO_COVER.value, code="000001", qty=1000, price=10.0), cfg.margin)
    assert plan["error"] == ErrorCode.INSUFFICIENT_POSITION.value


def test_table_repay_margin(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    state, order = fill(state, Side.REPAY_MARGIN.value, qty=2000, price=100.0,
                        trade_date="2025-10-10", cfg=cfg)
    assert approx(order.debt_delta, -200000.0)
    assert approx(state.financing_debt, 0.0)
    assert approx(state.available_cash, 500000.0)
    assert approx(state.net_assets, 500000.0)


def test_short_cover_at_higher_price_realizes_loss(cfg, state):
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=2000, price=100.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    state, order = fill(state, Side.BUY_TO_COVER.value, code="000001", qty=2000, price=120.0,
                        trade_date="2025-10-10", cfg=cfg)
    assert approx(order.realized_pnl, -40000.0)
    assert approx(state.net_assets, 460000.0)
    assert approx(state.frozen_collateral, 0.0)


# --------------------------------------------------------------------------- #
# 不变量 I1 / I2 / I3 / I4
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "side,code,qty,price",
    [
        (Side.BUY.value, "600519", 100, 1000.0),
        (Side.MARGIN_BUY.value, "600519", 2000, 100.0),
        (Side.SHORT_SELL.value, "000001", 2000, 100.0),
        (Side.SELL.value, "600519", 100, 1000.0),
        (Side.BUY_TO_COVER.value, "000001", 1000, 10.0),
        (Side.REPAY_MARGIN.value, "600519", 2000, 100.0),
    ],
)
def test_I1_open_keeps_net_assets_zero_fee(cfg, side, code, qty, price):
    """I1：零费用口径下，六种方向成交瞬间 net_assets == initial_cash。"""
    state = core.new_state(cfg)
    if side in (Side.SELL.value, Side.REPAY_MARGIN.value):
        state, _ = fill(state, Side.MARGIN_BUY.value if side == Side.REPAY_MARGIN.value else Side.BUY.value,
                        code=code, qty=qty, price=price, cfg=cfg)
        state = core.settle_t1(state, "2025-10-10")
        trade_date = "2025-10-10"
    elif side == Side.BUY_TO_COVER.value:
        state, _ = fill(state, Side.SHORT_SELL.value, code=code, qty=qty, price=price, cfg=cfg)
        state = core.settle_t1(state, "2025-10-10")
        trade_date = "2025-10-10"
    else:
        trade_date = DATE
    plan = core.fill_plan(state, make_order(side, code, qty, price, trade_date), cfg.margin)
    assert plan["error"] is None
    state = core.apply_fill(state, make_order(side, code, qty, price, trade_date), cfg.margin)
    assert approx(state.net_assets, state.initial_cash)


def test_I1_with_fees_net_equals_initial_minus_fees(cfg):
    """I1 有费口径：三类开仓都恰好少 F（v3 修洞后一致）。"""
    for side, code, qty, price in (
        (Side.BUY.value, "600519", 100, 1000.0),
        (Side.MARGIN_BUY.value, "600519", 2000, 100.0),
        (Side.SHORT_SELL.value, "000001", 2000, 100.0),
    ):
        state = core.new_state(cfg)
        order = make_order(side, code, qty, price)
        order.fee, order.tax, order.transfer_fee = core.calc_fees(side, price, qty, cfg.fee)
        plan = core.fill_plan(state, order, cfg.margin, cfg.fee)
        state = core.apply_fill(state, order, cfg.margin, cfg.fee)
        assert approx(state.net_assets, 500000.0 - plan["fees"]), side


def test_I2_pnl_equals_mtm_plus_interest(cfg):
    """I2：net - initial == 多头浮盈(不含费) + 空头浮盈 - 累计费用 - 累计利息。"""
    state = core.new_state(cfg)
    fees = 0.0
    raw_long_basis = 0.0
    order = make_order(Side.BUY.value, "600519", 100, 1000.0)
    order.fee, order.tax, order.transfer_fee = core.calc_fees(order.side, order.price, order.qty, cfg.fee)
    plan = core.fill_plan(state, order, cfg.margin, cfg.fee)
    fees += plan["fees"]
    raw_long_basis += 100 * 1000.0
    state = core.apply_fill(state, order, cfg.margin, cfg.fee)
    state = core.settle_t1(state, "2025-10-10")

    order = make_order(Side.MARGIN_BUY.value, "600519", 200, 900.0, "2025-10-10")
    order.fee, order.tax, order.transfer_fee = core.calc_fees(order.side, order.price, order.qty, cfg.fee)
    plan = core.fill_plan(state, order, cfg.margin, cfg.fee)
    fees += plan["fees"]
    raw_long_basis += 200 * 900.0
    state = core.apply_fill(state, order, cfg.margin, cfg.fee)

    order = make_order(Side.SHORT_SELL.value, "000001", 500, 12.0, "2025-10-10")
    order.fee, order.tax, order.transfer_fee = core.calc_fees(order.side, order.price, order.qty, cfg.fee)
    plan = core.fill_plan(state, order, cfg.margin, cfg.fee)
    fees += plan["fees"]
    state = core.apply_fill(state, order, cfg.margin, cfg.fee)

    state = core.mark_to_market(state, {"600519": 950.0, "000001": 11.0})
    state = core.accrue_interest(state, cfg.margin, days=5)

    lhs = state.net_assets - state.initial_cash
    rhs = (
        (state.long_market_value - raw_long_basis)
        + sum(pos.short_pnl for pos in state.positions.values())
        - fees
        - state.interest_accrued
    )
    assert approx(lhs, rhs)


def test_I3_maintenance_ratio_matches_exchange_formula(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=1000, price=10.0, cfg=cfg,)
    state = core.mark_to_market(state, {"600519": 90.0, "000001": 12.0})
    expected = (state.available_cash + state.frozen_collateral + state.long_market_value) / (
        state.financing_debt + state.short_market_value + state.interest_accrued
    )
    assert approx(state.maintenance_ratio, expected)
    assert approx(state.collateral_value, state.available_cash + state.frozen_collateral + state.long_market_value)
    assert approx(state.total_assets, state.available_cash + state.frozen_cash + state.long_market_value)


def test_I3_excludes_frozen_t1_from_numerator(cfg, state):
    """frozen_t1（未交收卖出款）不得计入担保物。"""
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    state, _ = fill(state, Side.SELL.value, qty=100, price=1000.0, trade_date="2025-10-10", cfg=cfg)
    assert state.frozen_t1 > 0
    assert approx(state.collateral_value, state.available_cash + state.long_market_value)
    assert state.total_debt == 0
    assert state.maintenance_ratio == math.inf


def test_I4_financing_capacity_anchor(cfg):
    """I4 数值锚点（**纯函数口径**，引擎路径下保证金额度会被仓位限制先卡住）：
    融资 N=625000 → ratio 1.80；跌 20%/50%/60% → 1.60 / 1.30 / 1.20。"""
    state = core.new_state(cfg)
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=6250, price=100.0, cfg=cfg)
    assert approx(state.collateral_value, 1125000.0)
    assert approx(state.financing_debt, 625000.0)
    assert approx(state.net_assets, 500000.0)
    assert approx(state.maintenance_ratio, 1.80)
    assert approx(state.leverage, 1.25)  # 625000 / 500000

    down20 = core.mark_to_market(state, {"600519": 80.0})
    assert approx(down20.long_market_value, 500000.0)
    assert approx(down20.maintenance_ratio, 1.60)

    down50 = core.mark_to_market(state, {"600519": 50.0})
    assert approx(down50.long_market_value, 312500.0)
    assert approx(down50.maintenance_ratio, 1.30)  # 精确命中平仓线

    down60 = core.mark_to_market(state, {"600519": 40.0})
    assert approx(down60.long_market_value, 250000.0)
    assert approx(down60.maintenance_ratio, 1.20)


# --------------------------------------------------------------------------- #
# INV-5 / INV-6 / INV-8 / INV-9 / INV-10
# --------------------------------------------------------------------------- #
def test_inv5_margin_required_not_in_total_assets(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    baseline = state.total_assets
    assert state.margin_required > 0
    # total_assets 恒等于「现金 + 冻结 + 多头市值」，与 margin_required 无关
    assert approx(state.total_assets, state.available_cash + state.frozen_cash + state.long_market_value)
    assert approx(state.total_assets, baseline)
    assert approx(state.margin_available, state.net_assets - state.margin_required)
    assert approx(state.margin_required, 200000.0 * cfg.margin.margin_ratio_long)


def test_inv6_financing_debt_is_full_amount(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    assert approx(state.financing_debt, 200000.0)  # 全额，不是 200000*(1-0.8)=40000


def test_inv8_short_collateral_not_released_by_settle(cfg, state):
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=2000, price=100.0, cfg=cfg)
    available = state.available_cash
    collateral = state.frozen_collateral
    qty_debt = dict(state.short_qty_debt)
    state = core.settle_t1(state, "2025-10-10")
    assert approx(state.available_cash, available)
    assert approx(state.frozen_collateral, collateral)
    assert state.short_qty_debt == qty_debt
    assert state.positions["000001"].short_available == 2000


def test_inv9_frozen_t1_flips_to_available(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    state, _ = fill(state, Side.SELL.value, qty=100, price=1000.0, trade_date="2025-10-10", cfg=cfg)
    cash_before = state.available_cash
    collateral_before = state.frozen_collateral
    assert approx(state.frozen_t1, 100000.0)
    state = core.settle_t1(state, "2025-10-13")
    assert approx(state.available_cash, cash_before + 100000.0)
    assert approx(state.frozen_t1, 0.0)
    assert approx(state.frozen_collateral, collateral_before)


def test_inv10_frozen_cash_is_sum_of_two_buckets(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    state, _ = fill(state, Side.SELL.value, qty=100, price=1000.0, trade_date="2025-10-10", cfg=cfg)
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=2000, price=100.0,
                    trade_date="2025-10-10", cfg=cfg)
    assert approx(state.frozen_t1, 100000.0)
    assert approx(state.frozen_collateral, 200000.0)
    assert approx(state.frozen_cash, state.frozen_t1 + state.frozen_collateral)


# --------------------------------------------------------------------------- #
# 纯函数性：不修改入参
# --------------------------------------------------------------------------- #
def test_apply_fill_does_not_mutate_inputs(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    before_state = copy.deepcopy(state)
    order = make_order(Side.SELL.value, qty=100, price=1010.0, trade_date="2025-10-10")
    before_order = copy.deepcopy(order)
    core.apply_fill(state, order, cfg.margin, cfg.fee)
    assert core.snapshot(state) == core.snapshot(before_state)
    assert core.order_to_dict(order) == core.order_to_dict(before_order)


def test_fill_plan_is_pure(cfg, state):
    before = core.snapshot(state)
    core.fill_plan(state, make_order(Side.BUY.value, qty=100, price=1000.0), cfg.margin, cfg.fee)
    assert core.snapshot(state) == before


def test_settle_and_mark_and_interest_are_pure(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    before = core.snapshot(state)
    core.settle_t1(state, "2025-10-10")
    core.mark_to_market(state, {"600519": 90.0})
    core.accrue_interest(state, cfg.margin, days=3)
    core.refresh_risk_flags(state, cfg.risk, cfg.margin)
    assert core.snapshot(state) == before


# --------------------------------------------------------------------------- #
# 拒单/异常路径
# --------------------------------------------------------------------------- #
def test_rejected_order_is_noop(cfg, state):
    order = make_order(Side.BUY.value, qty=100, price=1000.0)
    order.status = core.REJECTED
    order.error_code = ErrorCode.INSUFFICIENT_CASH.value
    plan = core.fill_plan(state, order, cfg.margin)
    assert plan["error"] == "NOT_FILLED"
    after = core.apply_fill(state, order, cfg.margin)
    assert core.snapshot(after) == core.snapshot(state)


@pytest.mark.parametrize("qty", [0, -100])
def test_invalid_qty_reports_error_without_touching_state(cfg, state, qty):
    plan = core.fill_plan(state, make_order(Side.BUY.value, qty=qty, price=100.0), cfg.margin)
    assert plan["error"] == ErrorCode.INVALID_LOT.value
    assert core.snapshot(core.apply_fill(state, make_order(Side.BUY.value, qty=qty, price=100.0), cfg.margin)) == core.snapshot(state)


def test_invalid_price_reports_error(cfg, state):
    plan = core.fill_plan(state, make_order(Side.BUY.value, qty=100, price=0.0), cfg.margin)
    assert plan["error"] == ErrorCode.NO_PRICE.value


@pytest.mark.parametrize("bad_price", [float("inf"), float("-inf"), float("nan"), 1e400, -1e400])
def test_H1_non_finite_price_rejected(cfg, state, bad_price):
    """H1：inf/nan/1e400 委托价必须走 NO_PRICE，绝不污染账户、绝不产生裸 NaN。"""
    for side in (Side.BUY.value, Side.SELL.value, Side.MARGIN_BUY.value, Side.SHORT_SELL.value):
        order = make_order(side, code="600519", qty=100, price=bad_price)
        plan = core.fill_plan(state, order, cfg.margin, cfg.fee)
        assert plan["error"] == ErrorCode.NO_PRICE.value, (side, plan["error"])
        after = core.apply_fill(state, order, cfg.margin, cfg.fee)
        assert core.snapshot(after) == core.snapshot(state)
    # 账户干净 → 严格 JSON 必须可序列化（不允许裸 NaN / Infinity）
    json.dumps(core.state_document(state), ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    "side,code,qty,price",
    [(Side.BUY.value, "600519", 100, 1000.0), (Side.SELL.value, "600519", 100, 1000.0),
     (Side.MARGIN_BUY.value, "600519", 2000, 100.0), (Side.SHORT_SELL.value, "000001", 2000, 100.0),
     (Side.BUY_TO_COVER.value, "000001", 2000, 100.0), (Side.REPAY_MARGIN.value, "600519", 2000, 100.0)],
)
def test_no_bare_nan_in_persisted_document(cfg, state, side, code, qty, price):
    """落盘自检：六种方向走一遍后 state.json 文档必须能 allow_nan=False 严格序列化。"""
    if side in (Side.SELL.value, Side.REPAY_MARGIN.value):
        state, _ = fill(state, Side.MARGIN_BUY.value if side == Side.REPAY_MARGIN.value else Side.BUY.value,
                        code=code, qty=qty, price=price, cfg=cfg)
        state = core.settle_t1(state, "2025-10-10")
        trade_date = "2025-10-10"
    elif side == Side.BUY_TO_COVER.value:
        state, _ = fill(state, Side.SHORT_SELL.value, code=code, qty=qty, price=price, cfg=cfg)
        state = core.settle_t1(state, "2025-10-10")
        trade_date = "2025-10-10"
    else:
        trade_date = DATE
    state, _ = fill(state, side, code=code, qty=qty, price=price, trade_date=trade_date, cfg=cfg, fees=True)
    state = core.mark_to_market(state, {code: price})
    text = json.dumps(core.state_document(state), ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text


def test_t1_locked_shares_cannot_be_sold_by_core(cfg, state):
    """core 层保护：当日买入（available=0）无法被消耗；engine 归口 T1_NOT_SETTLED。"""
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    assert state.positions["600519"].long_available == 0
    plan = core.fill_plan(state, make_order(Side.SELL.value, qty=100, price=1000.0), cfg.margin)
    assert plan["error"] == ErrorCode.INSUFFICIENT_POSITION.value
    assert core.snapshot(core.apply_fill(state, make_order(Side.SELL.value, qty=100, price=1000.0), cfg.margin)) == core.snapshot(state)


def test_unsupported_side_reports_error(cfg, state):
    plan = core.fill_plan(state, make_order("teleport", qty=100, price=10.0), cfg.margin)
    assert plan["error"] == ErrorCode.UNSUPPORTED.value


def test_over_selling_available_position_is_rejected(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=200, price=100.0, cfg=cfg)
    state = core.settle_t1(state, "2025-10-10")
    plan = core.fill_plan(state, make_order(Side.SELL.value, qty=300, price=100.0, trade_date="2025-10-10"), cfg.margin)
    assert plan["error"] == ErrorCode.INSUFFICIENT_POSITION.value


# --------------------------------------------------------------------------- #
# 结算 / 计息 / 盯市
# --------------------------------------------------------------------------- #
def test_settle_t1_unlocks_and_updates_date(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    lot = state.positions["600519"].lots[0]
    assert lot.available_qty == 0
    state = core.settle_t1(state, "2025-10-10")
    assert state.trade_date == "2025-10-10"
    assert state.positions["600519"].lots[0].available_qty == 100
    assert state.positions["600519"].long_available == 100


def test_settle_t1_same_day_does_not_unlock(cfg, state):
    """unlock_date 记为成交日；同日 settle 不得解冻（防御）。"""
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)
    state = core.settle_t1(state, DATE)
    assert state.positions["600519"].long_available == 0


def test_accrue_interest_formula(cfg, state):
    """融资利息以 financing_debt 为基数；融券费用以 **short_market_value** 为基数（v3.1）。"""
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=1000, price=10.0, cfg=cfg)
    state = core.accrue_interest(state, cfg.margin, days=1)
    expected = (
        state.financing_debt * cfg.margin.financing_rate_annual / 365
        + state.short_market_value * cfg.margin.lending_rate_annual / 365
    )
    assert approx(state.interest_accrued, expected)


def test_inv15_lending_fee_base_is_short_market_value(cfg, state):
    """INV-15：融券费用基数 == short_market_value（市值口径），不是 short_cost_debt。"""
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=1000, price=100.0, cfg=cfg)
    state = core.mark_to_market(state, {"000001": 50.0})  # 市值 50000 ≠ 成本 100000
    after = core.accrue_interest(state, cfg.margin, days=1)
    charged = after.interest_accrued - state.interest_accrued
    assert approx(charged, state.short_market_value * cfg.margin.lending_rate_annual / 365)
    assert not approx(charged, state.short_cost_debt["000001"] * cfg.margin.lending_rate_annual / 365)
    assert approx(state.total_debt, state.financing_debt + state.short_market_value + state.interest_accrued)


def test_accrue_interest_zero_days(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    assert core.accrue_interest(state, cfg.margin, days=0).interest_accrued == 0.0


def test_mark_to_market_updates_positions_and_prices(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=100.0, cfg=cfg)
    state = core.mark_to_market(state, {"600519": 120.0})
    assert state.last_prices["600519"] == 120.0
    assert state.positions["600519"].last_price == 120.0
    assert state.positions["600519"].lots[0].last_price == 120.0
    assert approx(state.long_market_value, 12000.0)
    assert approx(state.positions["600519"].long_pnl, 2000.0)


@pytest.mark.parametrize("bad", [None, 0.0, -5.0, float("nan"), "abc"])
def test_mark_to_market_ignores_invalid_prices(cfg, state, bad):
    state, _ = fill(state, Side.BUY.value, qty=100, price=100.0, cfg=cfg)
    after = core.mark_to_market(state, {"600519": bad})
    assert approx(after.positions["600519"].last_price, 100.0)


# --------------------------------------------------------------------------- #
# 派生属性
# --------------------------------------------------------------------------- #
def test_position_derived_properties():
    pos = Position(code="600519", name="x", long_qty=100, long_cost=10.0, short_qty=50,
                   short_cost=20.0, last_price=15.0)
    assert approx(pos.long_value, 1500.0)
    assert approx(pos.short_value, 750.0)
    assert approx(pos.long_pnl, 500.0)
    assert approx(pos.short_pnl, 250.0)
    assert approx(pos.market_value, 2250.0)
    assert pos.is_active


def test_leverage_and_exposure_helpers(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=cfg)
    assert approx(state.leverage, state.total_debt / state.net_assets)
    assert approx(state.gross_exposure, state.long_market_value / state.net_assets)
    assert approx(state.short_exposure, 0.0)


def test_leverage_infinite_when_net_assets_nonpositive():
    state = AccountState(initial_cash=1000.0, available_cash=0.0, financing_debt=1000.0,
                         positions={"600519": Position(code="600519", long_qty=0)})
    assert state.leverage == math.inf
    assert state.net_assets < 0


# --------------------------------------------------------------------------- #
# 风险告警与状态门禁
# --------------------------------------------------------------------------- #
def test_check_risk_includes_stop_loss_and_position_pct(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=100.0, cfg=cfg)
    state = core.mark_to_market(state, {"600519": 90.0})  # -10% < -7%
    alerts = core.check_risk(state, cfg.risk, cfg.margin)
    codes = {a["code"] for a in alerts}
    assert core.ALERT_STOP_LOSS in codes
    assert any(a["level"] == "critical" for a in alerts if a["code"] == core.ALERT_STOP_LOSS)


def test_check_risk_reports_position_limit_pct(cfg):
    state = core.new_state(cfg)
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg)  # 100000 < 20% * 500000 = 100000
    state = core.mark_to_market(state, {"600519": 1200.0})  # 120000 > 100000
    alerts = core.check_risk(state, cfg.risk, cfg.margin)
    hit = [a for a in alerts if a["code"] == core.ALERT_POSITION_LIMIT_PCT]
    assert hit and hit[0]["level"] == "warn"


def test_check_risk_reports_position_count(cfg):
    state = core.new_state(cfg)
    for i, code in enumerate(["600519", "600036", "601398", "601288", "600030", "601318"]):
        state, _ = fill(state, Side.BUY.value, code=code, qty=100, price=10.0, cfg=cfg)
    alerts = core.check_risk(state, cfg.risk, cfg.margin)
    assert any(a["code"] == core.ALERT_POSITION_LIMIT_COUNT for a in alerts)


def test_leverage_exposed_but_not_in_frozen_alert_codes(cfg, state):
    """v3.1 冻结了 check_risk 的 code 集合；杠杆信息由 state.leverage / risk_report 暴露。"""
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=12500, price=100.0, cfg=cfg)
    assert state.leverage > cfg.risk.max_leverage
    alerts = core.check_risk(state, cfg.risk, cfg.margin)
    assert ErrorCode.LEVERAGE_LIMIT.value not in {a["code"] for a in alerts}


def test_refresh_risk_flags_margin_call_and_forced(cfg):
    state = core.new_state(cfg)
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=6250, price=100.0, cfg=cfg)
    flags = core.refresh_risk_flags(state, cfg.risk, cfg.margin)
    assert not flags.margin_call and not flags.forced_liquidation  # ratio 1.80

    warn = core.refresh_risk_flags(core.mark_to_market(state, {"600519": 55.0}), cfg.risk, cfg.margin)
    assert warn.margin_call and not warn.forced_liquidation  # ratio 1.35

    at_line = core.refresh_risk_flags(core.mark_to_market(state, {"600519": 50.0}), cfg.risk, cfg.margin)
    assert at_line.margin_call and not at_line.forced_liquidation  # ratio == 1.30 不 < 1.30

    below = core.refresh_risk_flags(core.mark_to_market(state, {"600519": 40.0}), cfg.risk, cfg.margin)
    assert below.forced_liquidation and below.margin_call  # ratio 1.20


def test_risk_status_mapping(cfg, state):
    """v3.1 归口：forced / margin_call / safe。"""
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=6250, price=100.0, cfg=cfg)
    assert core.risk_status(state, cfg.risk, cfg.margin) == "safe"
    warn = core.mark_to_market(state, {"600519": 55.0})  # ratio 1.35
    assert core.risk_status(warn, cfg.risk, cfg.margin) == "margin_call"
    forced = core.mark_to_market(state, {"600519": 40.0})  # ratio 1.20
    assert core.risk_status(forced, cfg.risk, cfg.margin) == "forced"


def test_alert_codes_are_frozen_set(cfg, state):
    """冻结集合：check_risk 只允许产出这 9 个 code。"""
    allowed = {
        core.ALERT_MAINTENANCE_FORCED, core.ALERT_MAINTENANCE_WARN,
        core.ALERT_MAINTENANCE_EXTRACTABLE, core.ALERT_POSITION_LIMIT_PCT,
        core.ALERT_POSITION_LIMIT_COUNT, core.ALERT_EXPOSURE_LIMIT,
        core.ALERT_SHORT_EXPOSURE_LIMIT, core.ALERT_STOP_LOSS, core.ALERT_TAKE_PROFIT,
    }
    assert len(allowed) == 9
    for side, code, qty, price in ((Side.BUY.value, "600519", 100, 1000.0),
                                   (Side.MARGIN_BUY.value, "600519", 6250, 100.0),
                                   (Side.SHORT_SELL.value, "000001", 5000, 10.0)):
        state2 = core.new_state(cfg)
        state2, _ = fill(state2, side, code=code, qty=qty, price=price, cfg=cfg)
        state2 = core.mark_to_market(state2, {code: price * 0.5})
        for alert in core.check_risk(state2, cfg.risk, cfg.margin):
            assert alert["code"] in allowed, alert


def test_maintenance_extractable_info_alert(cfg, state):
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=1000, price=100.0, cfg=cfg)  # ratio 6.0
    alerts = core.check_risk(state, cfg.risk, cfg.margin)
    hit = [a for a in alerts if a["code"] == core.ALERT_MAINTENANCE_EXTRACTABLE]
    assert hit and hit[0]["level"] == "info"


def test_refresh_risk_flags_clear_when_debtless(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=100.0, cfg=cfg)
    flags = core.refresh_risk_flags(state, cfg.risk, cfg.margin)
    assert not flags.margin_call and not flags.forced_liquidation


def test_check_risk_reports_maintenance_ratio_crossing(cfg):
    state = core.new_state(cfg)
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=6250, price=100.0, cfg=cfg)
    state = core.mark_to_market(state, {"600519": 45.0})  # ratio 1.25
    alerts = core.check_risk(state, cfg.risk, cfg.margin)
    hit = [a for a in alerts if a["code"] == core.ALERT_MAINTENANCE_FORCED]
    assert hit and hit[0]["level"] == "critical"
    assert core.risk_status(state, cfg.risk, cfg.margin) == "forced"


# --------------------------------------------------------------------------- #
# ID 生成 / 序列化
# --------------------------------------------------------------------------- #
def test_next_ids(cfg, state):
    assert core.next_order_id(state) == "O0001"
    assert core.next_lot_id(state) == "L0001"
    for index in range(1, 4):
        order = make_order(Side.BUY.value, qty=100, price=100.0)
        order.order_id = f"O{index:04d}"
        state = core.apply_fill(state, order, cfg.margin)
        state.history.append(order)
    assert core.next_order_id(state) == "O0004"
    assert core.next_lot_id(state) == "L0004"


def test_snapshot_restore_roundtrip(cfg):
    state = core.new_state(cfg)
    state, _ = fill(state, Side.BUY.value, qty=100, price=1000.0, cfg=cfg, fees=True)
    state, _ = fill(state, Side.SHORT_SELL.value, code="000001", qty=2000, price=100.0, cfg=cfg, fees=True)
    state = core.settle_t1(state, "2025-10-10")
    state = core.mark_to_market(state, {"600519": 1010.0, "000001": 90.0})
    doc = json.loads(json.dumps(core.state_document(state), ensure_ascii=False))
    restored = core.state_from_document(doc, cfg)
    assert core.snapshot(restored) == core.snapshot(state)
    assert approx(restored.maintenance_ratio, state.maintenance_ratio)


def test_state_document_schema_version_is_3(state):
    doc = core.state_document(state)
    assert doc["schema_version"] == 3
    assert doc["app"]["initial_cash"] == 500000.0
    assert "state" in doc


@pytest.mark.parametrize("version", [1, 2, None, "3"])
def test_state_from_document_rejects_other_versions(version, cfg):
    doc = {"schema_version": version, "app": {"initial_cash": 1.0}, "state": {}}
    with pytest.raises(ValueError):
        core.state_from_document(doc, cfg)


def test_state_from_document_requires_cfg(cfg):
    """**`state_from_document` 的 `cfg` 也是必填** —— 与 `restore` 的 v3.1 决定对称。

    ★ 修前签名写的是 `cfg: AppConfig | None = None`，但它**无条件**把 `cfg` 传给 `restore`
      ⇒ 不传 cfg 会炸 `AttributeError: 'NoneType' object has no attribute 'margin'`
      （「签名说可选、实际必需」）。这个瑕疵由 engine-builder 在做两融 E2E 矩阵时发现并上报。
    ★ **为什么不改成「None 时 `load_config()`」**：那会重新引入 `restore` 明令禁止的危害 ——
      自定义 root 的账户会被**当前目录的**默认配置解析，`margin_ratio_long` 一变
      `margin_required` 就算错。所以正解是**让签名如实**。
    """
    doc = {"schema_version": core.SCHEMA_VERSION, "app": {"initial_cash": 1.0}, "state": {}}
    with pytest.raises(TypeError):
        core.state_from_document(doc)          # type: ignore[call-arg]

    # 正对照：传了 cfg 就必须能解析（证明上一条不是「怎么都抛 TypeError」）
    st = core.state_from_document(doc, cfg)
    assert isinstance(st, core.AccountState)
    assert st.initial_cash == 1.0


def test_restore_requires_cfg():
    """v3.1：restore 的 cfg 必填，不得静默用默认参数。"""
    with pytest.raises(TypeError):
        core.restore({})


def test_restore_tolerates_missing_keys(cfg):
    state = core.restore({}, cfg)
    assert state.available_cash == 0.0
    assert state.positions == {}
    assert state.short_qty_debt == {}


def test_order_dict_roundtrip():
    order = make_order(Side.BUY.value, qty=100, price=12.34)
    order.fee, order.tax, order.transfer_fee = 5.0, 0.0, 0.12
    back = core.order_from_dict(core.order_to_dict(order))
    assert core.order_to_dict(back) == core.order_to_dict(order)


def test_reporting_helpers(cfg, state):
    state, _ = fill(state, Side.BUY.value, qty=100, price=100.0, cfg=cfg)
    assert core.available_long(state, "600519") == 0
    assert core.position_of(state, "sh600519") is not None  # 前缀归一
    assert list(core.active_positions(state)) == ["600519"]
    state = core.settle_t1(state, "2025-10-10")
    assert core.available_long(state, "600519") == 100
    assert core.available_short(state, "600519") == 0


def test_bind_config_supplies_margin_ratios(tmp_path):
    from ptrade.config import AppConfig, AccountConfig, CANSLIMConfig, MarginConfig, RiskConfig

    custom = AppConfig(
        account=AccountConfig(),
        fee=FeeConfig(),
        margin=MarginConfig(margin_ratio_long=0.5, margin_ratio_short=1.2),
        risk=RiskConfig(),
        canslim=CANSLIMConfig(),
        root=str(tmp_path),
    )
    state = core.new_state(custom)
    state, _ = fill(state, Side.MARGIN_BUY.value, qty=2000, price=100.0, cfg=custom)
    assert approx(state.financing_debt, 200000.0)  # 全额，与 m_long 无关
    assert approx(state.margin_required, 100000.0)  # 200000 * 0.5（取自绑定配置）
    assert approx(core.margin_required_of(state, custom.margin), 100000.0)
