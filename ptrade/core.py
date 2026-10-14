"""ptrade.core —— 领域模型与账户数学（INTERFACES.md **v3** §3）。

设计约束
--------
* **纯函数、无 IO**：只依赖标准库与 :mod:`ptrade.config`，不读文件、不联网、
  不 import akshare / pandas。
* **不变更入参**：所有变换函数（``apply_fill`` / ``settle_t1`` / ``mark_to_market`` /
  ``accrue_interest`` / ``refresh_risk_flags``）都 ``deepcopy`` 后返回新状态。
* ``snapshot`` / ``restore`` / ``state_document`` / ``state_from_document`` 是唯一的序列化出口。

v3 记账模型（四条不变量）
-------------------------
* ``total_assets = available_cash + frozen_t1 + frozen_collateral + long_market_value``（会计恒等式口径）
* ``frozen_cash = frozen_t1 + frozen_collateral``（派生量）：``frozen_t1`` 随 ``settle_t1`` 解冻，
  ``frozen_collateral``（融券券款）锁到买券还券
* ``collateral_value = available_cash + frozen_collateral + long_market_value``
  （**担保物**；仅排除 ``frozen_t1``——未交收卖出款不是保证金资金）
* ``total_debt = financing_debt + short_market_value + interest_accrued``
  （``financing_debt`` 为**全额** ``Σ N``，交易所口径；融券负债用**市值**口径）
* ``net_assets = total_assets - total_debt``
* ``maintenance_ratio = collateral_value / total_debt``（**交易所口径**，分母 <= 0 → ``math.inf``）
* ``leverage = total_debt / net_assets``
* ``margin_required`` **只是开仓额度指标，不参与 total_assets / 维持担保比例**

.. note::
   ``margin_required`` 需要 ``margin_ratio_long`` / ``margin_ratio_short``；
   ``new_state(cfg)`` / ``restore(doc, cfg)`` 会把 ``cfg.margin`` 绑定到状态对象
   （私有属性 ``_margin``，``deepcopy`` 会保留），未绑定时退化为 :class:`MarginConfig` 默认值。
   需要显式传入的场合请用 :func:`margin_required_of`。
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import AppConfig, FeeConfig, MarginConfig, RiskConfig

__all__ = [
    "Side",
    "ErrorCode",
    "Lot",
    "Position",
    "Order",
    "AccountState",
    "FILLED",
    "REJECTED",
    "SCHEMA_VERSION",
    "ALERT_MAINTENANCE_FORCED",
    "ALERT_MAINTENANCE_WARN",
    "ALERT_MAINTENANCE_EXTRACTABLE",
    "ALERT_POSITION_LIMIT_PCT",
    "ALERT_POSITION_LIMIT_COUNT",
    "ALERT_EXPOSURE_LIMIT",
    "ALERT_SHORT_EXPOSURE_LIMIT",
    "ALERT_STOP_LOSS",
    "ALERT_TAKE_PROFIT",
    "risk_status",
    "round_lot",
    "calc_fees",
    "fill_plan",
    "apply_fill",
    "accrue_interest",
    "mark_to_market",
    "settle_t1",
    "check_risk",
    "refresh_risk_flags",
    "new_state",
    "next_order_id",
    "next_lot_id",
    "snapshot",
    "restore",
    "state_document",
    "state_from_document",
    "order_to_dict",
    "order_from_dict",
    "position_of",
    "active_positions",
    "available_long",
    "available_short",
    "margin_required_of",
    "bind_config",
]

FILLED = "filled"
REJECTED = "rejected"

# 风险告警 code 集合（v3.1 冻结，供 check_risk / risk_report / CLI 共用）
ALERT_MAINTENANCE_FORCED = "MAINTENANCE_FORCED"            # critical  ratio < 平仓线
ALERT_MAINTENANCE_WARN = "MAINTENANCE_WARN"                # warn      ratio < 警戒线
ALERT_MAINTENANCE_EXTRACTABLE = "MAINTENANCE_EXTRACTABLE"  # info      ratio >= 可提取线
ALERT_POSITION_LIMIT_PCT = "POSITION_LIMIT_PCT"            # warn
ALERT_POSITION_LIMIT_COUNT = "POSITION_LIMIT_COUNT"        # warn
ALERT_EXPOSURE_LIMIT = "EXPOSURE_LIMIT"                    # warn
ALERT_SHORT_EXPOSURE_LIMIT = "SHORT_EXPOSURE_LIMIT"        # warn
ALERT_STOP_LOSS = "STOP_LOSS"                              # critical
ALERT_TAKE_PROFIT = "TAKE_PROFIT"                          # info

LONG = "long"
SHORT = "short"

KIND_CASH = "cash"
KIND_MARGIN = "margin"
KIND_SHORT = "short"

SCHEMA_VERSION = 3


# --------------------------------------------------------------------------- #
# 3.1 枚举与错误码（v3）
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    """交易方向。值即接口契约中 `side` 字段的字符串。"""

    BUY = "buy"  # 普通买入（自有资金）
    SELL = "sell"  # 普通卖出（卖出持仓）
    MARGIN_BUY = "margin_buy"  # 融资买入（做多，产生负债）
    SHORT_SELL = "short_sell"  # 融券卖出（做空开仓）
    BUY_TO_COVER = "buy_to_cover"  # 买券还券（做空平仓）
    REPAY_MARGIN = "repay_margin"  # 卖券还款 / 直接还款


class ErrorCode(str, Enum):
    """统一错误码（v3：``INSUFFICIENT_MARGIN`` → ``INSUFFICIENT_MARGIN_SPACE``，新增 ``SHORT_SOURCE_LIMIT`` / ``LEVERAGE_LIMIT``）。"""

    OK = "OK"
    T1_NOT_SETTLED = "T1_NOT_SETTLED"  # T+1 未解冻
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    NOT_MARGIN_TARGET = "NOT_MARGIN_TARGET"  # 非两融标的
    NOT_SHORT_TARGET = "NOT_SHORT_TARGET"  # 非融券标的
    INSUFFICIENT_MARGIN_SPACE = "INSUFFICIENT_MARGIN_SPACE"  # 保证金空间不足
    LEVERAGE_LIMIT = "LEVERAGE_LIMIT"  # 【v3】杠杆超限 / 开仓后维持担保比例低于平仓线
    MARGIN_CALL_BLOCKED = "MARGIN_CALL_BLOCKED"  # 追保状态禁止加仓
    BELOW_MAINTENANCE = "BELOW_MAINTENANCE"  # 触发平仓线，仅允许减仓
    POSITION_LIMIT_PCT = "POSITION_LIMIT_PCT"  # 单票仓位超限
    POSITION_LIMIT_COUNT = "POSITION_LIMIT_COUNT"  # 持仓数超限
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"  # 总敞口/空头敞口超限
    SHORT_SOURCE_LIMIT = "SHORT_SOURCE_LIMIT"  # 券源不足
    LIMIT_UP_NO_BUY = "LIMIT_UP_NO_BUY"  # 涨停不可买
    LIMIT_DOWN_NO_SELL = "LIMIT_DOWN_NO_SELL"  # 跌停不可卖
    NO_PRICE = "NO_PRICE"
    NO_MARKET_DATA = "NO_MARKET_DATA"
    INVALID_LOT = "INVALID_LOT"  # 非 100 股整数倍
    SUSPENDED = "SUSPENDED"  # 停牌/无行情
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"


# --------------------------------------------------------------------------- #
# 3.2 数据模型（v3）
# --------------------------------------------------------------------------- #
@dataclass
class Lot:
    """持仓批次。买入 / 融资买入 / 融券开仓各生成一条。"""

    lot_id: str = ""
    code: str = ""
    open_date: str = ""  # YYYY-MM-DD
    unlock_date: str = ""  # T+1 解冻交易日；open_date 当日 locked
    qty: int = 0
    available_qty: int = 0  # 可卖数量（T+1 后 = qty）
    cost_price: float = 0.0
    """含费用的摊薄成本（long）；融券批次为**开仓均价（券款口径）**，
    这样买券还券时释放的券款恰好等于当初冻结的金额。"""
    side: str = LONG  # "long" | "short"
    kind: str = KIND_CASH  # "cash" | "margin" | "short"
    last_price: float = 0.0


@dataclass
class Position:
    """单标的的多空合并持仓。"""

    code: str = ""
    name: str = ""
    long_qty: int = 0
    long_available: int = 0
    short_qty: int = 0  # 融券未还数量
    short_available: int = 0  # 可买券还券数量（T+1）
    long_cost: float = 0.0  # 多头摊薄成本
    short_cost: float = 0.0  # 空头开仓均价
    last_price: float = 0.0
    lots: list[Lot] = field(default_factory=list)

    # ---- 派生属性 ----
    @property
    def long_value(self) -> float:
        """多头市值 = long_qty * last_price。"""
        return float(self.long_qty) * float(self.last_price)

    @property
    def short_value(self) -> float:
        """空头市值 = short_qty * last_price（市值口径）。"""
        return float(self.short_qty) * float(self.last_price)

    @property
    def long_pnl(self) -> float:
        """多头浮动盈亏 = (last_price - long_cost) * long_qty。"""
        return (float(self.last_price) - float(self.long_cost)) * float(self.long_qty)

    @property
    def short_pnl(self) -> float:
        """空头浮动盈亏 = (short_cost - last_price) * short_qty。"""
        return (float(self.short_cost) - float(self.last_price)) * float(self.short_qty)

    @property
    def market_value(self) -> float:
        """多空合计市值（用于单票仓位占比）。"""
        return self.long_value + self.short_value

    @property
    def is_active(self) -> bool:
        return self.long_qty > 0 or self.short_qty > 0


@dataclass
class Order:
    """委托 / 成交记录。拒绝单也生成 Order（status='rejected'）。"""

    order_id: str = ""
    ts: str = ""  # ISO8601
    trade_date: str = ""  # YYYY-MM-DD
    code: str = ""
    name: str = ""
    side: str = ""  # Side 的值
    qty: int = 0
    price: float = 0.0
    status: str = FILLED  # "filled" | "rejected"
    error_code: str = ErrorCode.OK.value
    reason: str = ""
    fee: float = 0.0
    tax: float = 0.0
    transfer_fee: float = 0.0
    cash_delta: float = 0.0  # 对 available_cash 的影响
    margin_used: float = 0.0  # 本笔占用保证金（自有资金部分，仅审计用，不入账）
    debt_delta: float = 0.0  # 对总负债的影响（融资=元；融券=券款元额）
    realized_pnl: float = 0.0  # 平仓本笔实现盈亏
    # ---- 成交价审计（D，2026-10-14）----
    price_source: str = ""      # "request"（用户给的价）/ "live"（实时快照）/ ""（市价推导）
    price_asof: str = ""        # 该价对应的行情日期（快照 payload.asof）
    price_fetched_at: str = ""  # 该快照的抓取时刻（缓存 fetched_at）—— 真正回答「哪一刻的价」

    @property
    def total_fee(self) -> float:
        return float(self.fee) + float(self.tax) + float(self.transfer_fee)

    @property
    def filled(self) -> bool:
        return self.status == FILLED


@dataclass
class AccountState:
    """账户状态（v3：无 ``margin_used`` / ``lending_debt_value`` / ``lending_debt_qty``；``frozen_cash`` 为派生属性）。"""

    trade_date: str = ""
    initial_cash: float = 0.0
    available_cash: float = 0.0  # 账户可用现金
    frozen_t1: float = 0.0  # 【T+1 待交收】普通卖出待交收款；settle_t1 转入 available_cash
    frozen_collateral: float = 0.0  # 【融券担保】融券卖出券款；不随交易日解冻，仅买券还券时释放
    financing_debt: float = 0.0  # 融资负债 = Σ 融资买入金额 N（**全额**，交易所口径）+ 已资本化利息
    short_cost_debt: dict[str, float] = field(default_factory=dict)  # 融券负债（成本口径）
    short_qty_debt: dict[str, int] = field(default_factory=dict)  # 融券欠券数量
    interest_accrued: float = 0.0  # 累计利息与融券费用
    positions: dict[str, Position] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    margin_call: bool = False  # 是否触及警戒线
    forced_liquidation: bool = False  # 是否触及平仓线
    history: list[Order] = field(default_factory=list)

    # ---- 账户数学（派生属性，无缓存） ----
    @property
    def long_market_value(self) -> float:
        return sum(p.long_value for p in self.positions.values())

    @property
    def short_market_value(self) -> float:
        """Σ position.short_value（市值口径，用于负债与维持担保比例分母）。"""
        return sum(p.short_value for p in self.positions.values())

    @property
    def frozen_cash(self) -> float:
        """**派生量** ``frozen_t1 + frozen_collateral``（不单独存储，避免三者不一致）。

        ``frozen_t1``（T+1 待交收卖出款）会随 ``settle_t1`` 解冻；
        ``frozen_collateral``（融券卖出券款）必须一直锁到买券还券。
        """
        return float(self.frozen_t1) + float(self.frozen_collateral)

    @property
    def total_assets(self) -> float:
        """``available_cash + frozen_t1 + frozen_collateral + long_market_value``（会计恒等式口径）。

        **不是**维持担保比例的分子（见 :attr:`collateral_value`）。
        """
        return (
            float(self.available_cash)
            + float(self.frozen_t1)
            + float(self.frozen_collateral)
            + float(self.long_market_value)
        )

    @property
    def collateral_value(self) -> float:
        """【v3.1】担保物价值 = ``available_cash + frozen_collateral + long_market_value``。

        融券卖出所得资金属**可充抵保证金的资金**（深交所投教口径），故 ``frozen_collateral`` 计入分子；
        ``frozen_t1``（尚未交收的卖出款）**不计入**——它不是保证金资金。
        """
        return (
            float(self.available_cash)
            + float(self.frozen_collateral)
            + float(self.long_market_value)
        )

    @property
    def total_debt(self) -> float:
        """``financing_debt + short_market_value + interest_accrued``。

        【v3】``financing_debt`` 用**全额**口径；融券负债用**市值**口径。
        """
        return float(self.financing_debt) + float(self.short_market_value) + float(self.interest_accrued)

    @property
    def net_assets(self) -> float:
        return self.total_assets - self.total_debt

    @property
    def maintenance_ratio(self) -> float:
        """【v3，交易所口径】维持担保比例 = ``collateral_value / total_debt``；分母 <= 0 → ``math.inf``。

        **不得**把 ``frozen_t1`` / ``frozen_collateral`` 计入分子。
        """
        debt = self.total_debt
        if debt <= 0:
            return math.inf
        return self.collateral_value / debt

    @property
    def leverage(self) -> float:
        """【v3】总负债 / 净资产。开仓后不得超过 ``RiskConfig.max_leverage``。"""
        net = self.net_assets
        if net <= 0:
            return math.inf if self.total_debt > 0 else 0.0
        return self.total_debt / net

    @property
    def margin_required(self) -> float:
        """当前已占用开仓额度（**不参与 total_assets**）。

        * 融资部分 = ``Σ 融资买入金额 × m_long`` = ``financing_debt × m_long``
          （v3 下 ``financing_debt = Σ N`` 全额口径）
        * 融券部分 = ``max(0, Σ short_market_value × m_short - frozen_collateral)``

        比例取自 :func:`bind_config` 绑定的 ``MarginConfig``，未绑定时用默认值。
        """
        return margin_required_of(self, _bound_margin(self))

    @property
    def margin_available(self) -> float:
        """``net_assets - margin_required``。"""
        return self.net_assets - self.margin_required

    @property
    def total_pnl(self) -> float:
        return self.net_assets - float(self.initial_cash)

    @property
    def gross_exposure(self) -> float:
        """总敞口 / 净资产 = (多头市值 + 融券市值) / net_assets。"""
        gross = self.long_market_value + self.short_market_value
        net = self.net_assets
        if net <= 0:
            return math.inf if gross > 0 else 0.0
        return gross / net

    @property
    def short_exposure(self) -> float:
        """融券市值 / 净资产。"""
        net = self.net_assets
        if net <= 0:
            return math.inf if self.short_market_value > 0 else 0.0
        return self.short_market_value / net

    def position(self, code: str) -> Position | None:
        return self.positions.get(_pid(code))


# --------------------------------------------------------------------------- #
# 3.3 纯函数
# --------------------------------------------------------------------------- #
def round_lot(qty: int) -> int:
    """向下取整到 100 股（负数向负无穷取整）。"""
    return int(qty // 100) * 100


def calc_fees(
    side: str,
    price: float,
    qty: int,
    fee: FeeConfig,
    transfer_fee: bool = True,
) -> tuple[float, float, float]:
    """返回 ``(commission, stamp_tax, transfer_fee)``。

    * ``commission = max(amount * commission_rate, commission_min)``，仅 ``amount > 0`` 时收；
    * 印花税仅卖出方向收：``SELL`` / ``SHORT_SELL``（``BUY_TO_COVER`` 与买入类不收）；
    * ``transfer_fee = amount * transfer_fee_rate``（``transfer_fee=True`` 时）。
    """
    amount = float(price) * int(qty)
    if amount <= 0:
        return 0.0, 0.0, 0.0
    commission = max(amount * float(fee.commission_rate), float(fee.commission_min))
    stamp = 0.0
    if side in (Side.SELL.value, Side.SHORT_SELL.value):
        stamp = amount * float(fee.stamp_tax_rate)
    transfer = amount * float(fee.transfer_fee_rate) if transfer_fee else 0.0
    return float(commission), float(stamp), float(transfer)


def bind_config(state: AccountState, cfg: AppConfig) -> AccountState:
    """把 ``cfg.margin`` / ``cfg.risk`` 绑定到状态对象（供 ``margin_required`` 使用）。

    绑定的是私有属性 ``_margin`` / ``_risk``，**不是记账字段**，不影响序列化与不变量。
    返回同一对象（便于链式使用）。
    """
    object.__setattr__ if False else None  # 保持 dataclass 非 frozen，直接赋值
    state._margin = cfg.margin  # type: ignore[attr-defined]
    state._risk = cfg.risk  # type: ignore[attr-defined]
    return state


def _bound_margin(state: AccountState) -> MarginConfig:
    got = state.__dict__.get("_margin")
    return got if isinstance(got, MarginConfig) else MarginConfig()


def margin_required_of(state: AccountState, margin: MarginConfig | None = None) -> float:
    """显式计算 ``margin_required``（可指定比例，不依赖绑定）。"""
    margin = margin if margin is not None else _bound_margin(state)
    m_long = float(margin.margin_ratio_long)
    m_short = float(margin.margin_ratio_short)

    long_part = float(state.financing_debt) * m_long

    short_part = max(
        0.0,
        float(state.short_market_value) * m_short - float(state.frozen_collateral),
    )
    return long_part + short_part


# ---- 内部工具 -------------------------------------------------------------- #
def _pid(code: str) -> str:
    """统一的标的键（去掉 sh/sz/bj 前缀，保留 6 位数字）。"""
    c = str(code).strip().lower()
    for pre in ("sh", "sz", "bj"):
        if c.startswith(pre):
            c = c[len(pre) :]
    return c


def _ensure_position(state: AccountState, code: str, name: str = "", last_price: float = 0.0) -> Position:
    code = _pid(code)
    pos = state.positions.get(code)
    if pos is None:
        pos = Position(code=code, name=name or "", last_price=float(last_price))
        state.positions[code] = pos
    if name and not pos.name:
        pos.name = name
    if last_price and not pos.last_price:
        pos.last_price = float(last_price)
    return pos


def _recompute_long(pos: Position) -> None:
    pos.lots = [l for l in pos.lots if l.qty > 0]
    longs = [l for l in pos.lots if l.side == LONG]
    pos.long_qty = sum(int(l.qty) for l in longs)
    pos.long_available = sum(int(l.available_qty) for l in longs)
    pos.long_cost = (
        sum(float(l.cost_price) * int(l.qty) for l in longs) / pos.long_qty if pos.long_qty else 0.0
    )


def _recompute_short(pos: Position) -> None:
    pos.lots = [l for l in pos.lots if l.qty > 0]
    shorts = [l for l in pos.lots if l.side == SHORT]
    pos.short_qty = sum(int(l.qty) for l in shorts)
    pos.short_available = sum(int(l.available_qty) for l in shorts)
    pos.short_cost = (
        sum(float(l.cost_price) * int(l.qty) for l in shorts) / pos.short_qty
        if pos.short_qty
        else 0.0
    )


def _consume_lots(pos: Position | None, side: str, qty: int) -> tuple[int, float]:
    """按 FIFO 消耗批次，返回 ``(已消耗股数, 已消耗成本)``。只消耗 ``available_qty``。"""
    if pos is None or qty <= 0:
        return 0, 0.0
    remaining = int(qty)
    cost = 0.0
    for lot in pos.lots:
        if remaining <= 0:
            break
        if lot.side != side or lot.available_qty <= 0:
            continue
        take = min(int(lot.available_qty), remaining)
        cost += float(lot.cost_price) * take
        lot.qty -= take
        lot.available_qty -= take
        remaining -= take
    if side == LONG:
        _recompute_long(pos)
    else:
        _recompute_short(pos)
    return int(qty) - remaining, cost


def _fill(
    state: AccountState,
    order: Order,
    margin: MarginConfig | None = None,
    fee_cfg: FeeConfig | None = None,
) -> tuple[AccountState, dict[str, Any]]:
    """apply_fill / fill_plan 的共同实现，返回 ``(新状态, plan)``。"""
    margin = margin if margin is not None else _bound_margin(state)
    new = copy.deepcopy(state)
    code = _pid(order.code)
    name = order.name or ""
    qty = int(order.qty)
    price = float(order.price)
    amount = price * qty
    m_long = float(margin.margin_ratio_long)
    m_short = float(margin.margin_ratio_short)

    plan: dict[str, Any] = {
        "side": order.side,
        "code": code,
        "qty": qty,
        "price": price,
        "amount": amount,
        "fee": 0.0,
        "tax": 0.0,
        "transfer_fee": 0.0,
        "fees": 0.0,
        "cash_delta": 0.0,
        "frozen_t1_delta": 0.0,
        "frozen_collateral_delta": 0.0,
        "financing_debt_delta": 0.0,
        "short_cost_delta": 0.0,
        "short_qty_delta": 0,
        "margin_used": 0.0,  # Order.margin_used（审计口径）
        "debt_delta": 0.0,  # Order.debt_delta
        "realized_pnl": 0.0,
        "released_proceeds": 0.0,
        "filled_qty": 0,
        "error": None,
    }

    if order.status != FILLED:
        plan["error"] = "NOT_FILLED"
        return new, plan

    if fee_cfg is not None:
        comm, tax, transfer = calc_fees(order.side, price, qty, fee_cfg)
    else:
        comm, tax, transfer = float(order.fee), float(order.tax), float(order.transfer_fee)
    fees = comm + tax + transfer
    plan["fee"], plan["tax"], plan["transfer_fee"] = comm, tax, transfer
    plan["fees"] = fees

    if qty <= 0:
        plan["error"] = ErrorCode.INVALID_LOT.value
        return new, plan
    # H1 加固：非有限价格（inf / -inf / nan / 1e400）一律拒，语义归 NO_PRICE。
    # 否则 amount 变 inf/nan → 账户被 NaN 污染（风控比较恒 False）且落盘出现裸 NaN（RFC8259 非法）。
    if not math.isfinite(price) or price <= 0:
        plan["error"] = ErrorCode.NO_PRICE.value
        return new, plan

    side = order.side
    trade_date = order.trade_date or state.trade_date

    # 减仓方向先验证可用数量（错误码由 engine 判定；core 只做状态完整性保护）
    if side in (Side.SELL.value, Side.REPAY_MARGIN.value) and qty > 0:
        if available_long(new, code) < qty:
            plan["error"] = ErrorCode.INSUFFICIENT_POSITION.value
            return new, plan
    if side == Side.BUY_TO_COVER.value and qty > 0:
        if available_short(new, code) < qty:
            plan["error"] = ErrorCode.INSUFFICIENT_POSITION.value
            return new, plan

    if side == Side.BUY.value:
        # 普通买入：-(N + F)
        plan["cash_delta"] = -(amount + fees)
        pos = _ensure_position(new, code, name, price)
        pos.lots.append(
            Lot(
                lot_id=_next_lot_id(new),
                code=code,
                open_date=trade_date,
                unlock_date=trade_date,
                qty=qty,
                available_qty=0,
                cost_price=(amount + fees) / qty,
                side=LONG,
                kind=KIND_CASH,
                last_price=price,
            )
        )
        _recompute_long(pos)
        pos.last_price = float(price)
        new.last_prices.setdefault(code, price)
        plan["filled_qty"] = qty

    elif side == Side.SELL.value:
        # 普通卖出：款项 T+1 到账（先进 frozen_t1）
        pos = new.positions.get(code)
        filled_qty, cost_basis = _consume_lots(pos, LONG, qty)
        plan["frozen_t1_delta"] = amount - fees
        plan["realized_pnl"] = (amount - fees) - cost_basis
        plan["filled_qty"] = filled_qty
        if pos is not None:
            pos.last_price = float(price)

    elif side == Side.MARGIN_BUY.value:
        # 融资买入：自有资金不出账（留在信用账户作担保物），只扣费用；负债为全额 N
        plan["cash_delta"] = -fees
        plan["financing_debt_delta"] = amount
        plan["debt_delta"] = amount
        plan["margin_used"] = amount * m_long
        pos = _ensure_position(new, code, name, price)
        pos.lots.append(
            Lot(
                lot_id=_next_lot_id(new),
                code=code,
                open_date=trade_date,
                unlock_date=trade_date,
                qty=qty,
                available_qty=0,
                cost_price=(amount + fees) / qty,
                side=LONG,
                kind=KIND_MARGIN,
                last_price=price,
            )
        )
        _recompute_long(pos)
        pos.last_price = float(price)
        new.last_prices.setdefault(code, price)
        plan["filled_qty"] = qty

    elif side == Side.SHORT_SELL.value:
        # 融券卖出：券款即保证金（进 frozen_collateral）；费用从 available_cash 扣（v3 修洞）；
        # m_short > 1 时从 available_cash 额外冻结 (m_short-1)*N
        extra = max(0.0, m_short - 1.0) * amount
        plan["cash_delta"] = -(fees + extra)
        plan["frozen_collateral_delta"] = amount + extra
        plan["short_qty_delta"] = qty
        plan["short_cost_delta"] = amount
        plan["debt_delta"] = amount
        plan["margin_used"] = amount * m_short
        pos = _ensure_position(new, code, name, price)
        pos.lots.append(
            Lot(
                lot_id=_next_lot_id(new),
                code=code,
                open_date=trade_date,
                unlock_date=trade_date,
                qty=qty,
                available_qty=0,
                cost_price=amount / qty,  # 券款口径：开仓均价
                side=SHORT,
                kind=KIND_SHORT,
                last_price=price,
            )
        )
        _recompute_short(pos)
        pos.last_price = float(price)
        new.last_prices.setdefault(code, price)
        plan["filled_qty"] = qty

    elif side == Side.BUY_TO_COVER.value:
        # 买券还券：释放该笔券款抵债（frozen_collateral），不足部分从 available_cash 补
        pos = new.positions.get(code)
        filled_qty, released = _consume_lots(pos, SHORT, qty)
        plan["released_proceeds"] = released
        plan["cash_delta"] = -(amount + fees) + released
        plan["frozen_collateral_delta"] = -released
        plan["short_qty_delta"] = -qty
        plan["short_cost_delta"] = -released
        plan["debt_delta"] = -released
        plan["margin_used"] = -(amount * m_short)
        plan["realized_pnl"] = (released - amount) - fees
        plan["filled_qty"] = filled_qty
        if pos is not None:
            pos.last_price = float(price)

    elif side == Side.REPAY_MARGIN.value:
        # 卖券还款（qty>0）或直接还款（qty==0）；v3.2：只支持全额还款
        proceeds = 0.0
        if qty > 0:
            pos = new.positions.get(code)
            filled_qty, cost_basis = _consume_lots(pos, LONG, qty)
            proceeds = amount - fees
            plan["realized_pnl"] = proceeds - cost_basis
            plan["filled_qty"] = filled_qty
            if pos is not None:
                pos.last_price = float(price)
        # v3.2：只支持**全额还款**（repay = financing_debt）；净额不足由 engine 判 INSUFFICIENT_CASH
        repay = max(float(new.financing_debt), 0.0)
        plan["cash_delta"] = proceeds - repay
        plan["financing_debt_delta"] = -repay
        plan["debt_delta"] = -repay
        plan["repay"] = repay

    else:
        plan["error"] = ErrorCode.UNSUPPORTED.value
        return new, plan

    _apply(new, plan, code)
    return new, plan


def _apply(state: AccountState, plan: dict[str, Any], code: str) -> None:
    """把 fill_plan 的结果作用到（已深拷贝的）state 上（v3 记账）。"""
    state.available_cash += plan["cash_delta"]
    state.frozen_t1 += plan["frozen_t1_delta"]
    state.frozen_collateral += plan["frozen_collateral_delta"]
    state.financing_debt += plan["financing_debt_delta"]
    if plan["short_qty_delta"]:
        state.short_qty_debt[code] = int(state.short_qty_debt.get(code, 0)) + int(plan["short_qty_delta"])
    if plan["short_cost_delta"]:
        state.short_cost_debt[code] = float(state.short_cost_debt.get(code, 0.0)) + float(
            plan["short_cost_delta"]
        )
    # 清理已还清的劵债键
    for book in (state.short_qty_debt, state.short_cost_debt):
        for key in [k for k, v in book.items() if abs(float(v)) < 1e-9]:
            del book[key]
    # 数值卫生：消除浮点残差（不改动契约金额）
    for attr in ("available_cash", "frozen_t1", "frozen_collateral", "financing_debt"):
        val = float(getattr(state, attr))
        if abs(val) < 1e-9:
            setattr(state, attr, 0.0)


def fill_plan(
    state: AccountState,
    order: Order,
    margin: MarginConfig | None = None,
    fee_cfg: FeeConfig | None = None,
) -> dict[str, Any]:
    """计算一笔成交对账户的影响（**纯函数**，不改动 state）。

    返回 dict（供 ``engine.submit`` 回填 Order 的审计字段）::

        {"side","code","qty","price","amount","fee","tax","transfer_fee","fees",
         "cash_delta","frozen_t1_delta","frozen_collateral_delta","financing_debt_delta",
         "short_cost_delta","short_qty_delta",
         "margin_used","debt_delta","realized_pnl","released_proceeds",
         "filled_qty","error"}

    ``fee_cfg`` 给定时按 :func:`calc_fees` **重算**费用（权威值）；否则沿用
    ``order.fee/tax/transfer_fee``。
    """
    _, plan = _fill(state, order, margin, fee_cfg)
    return plan


def apply_fill(
    state: AccountState,
    order: Order,
    margin: MarginConfig | None = None,
    fee_cfg: FeeConfig | None = None,
) -> AccountState:
    """把已成交订单应用到账户，返回**新状态**，不修改 ``state`` / ``order``。

    账户数学（v3）逐条对齐 INTERFACES.md §3.3 表格，``N = price*qty``、``F = fee+tax+transfer``：

    ============  ==============================  ==========================  ==============================
    操作           available_cash                  frozen_t1 / collateral      financing_debt / 券债
    ============  ==============================  ==========================  ==============================
    普通买入       ``-(N+F)``                      0 / 0                       0
    普通卖出       0（T+1 到账）                   ``+(N-F)`` / 0              0
    融资买入       ``-F``（自有资金不出账）        0 / 0                       ``+N``（**全额**）
    融券卖出       ``-F``（``m_short>1`` 再 ``-(m_short-1)N``） 0 / ``+N``    ``short_qty_debt+N``、``short_cost_debt+N``
    买券还券       ``-(N+F)+该笔券款``             0 / ``-该笔券款``           ``short_qty_debt-qty``（券债清零删键）
    卖券还款       ``+(N-F)-repay``（``repay=financing_debt`` 全额） 0 / 0       ``-repay``
    ============  ==============================  ==========================  ==============================

    * 三类开仓（BUY / MARGIN_BUY / SHORT_SELL）都扣费用 ``F``，口径一致。
    * ``frozen_cash`` 是派生量 ``frozen_t1 + frozen_collateral``；``settle_t1`` 只解冻 ``frozen_t1``。
    * ``unlock_date`` 记为成交日 —— core 无交易日历（纯函数），engine 可用真实次日交易日回填；
      :func:`settle_t1` 只要 ``unlock_date <= new_trade_date`` 即解冻。
    * ``status != 'filled'``、``qty <= 0``、``price <= 0``、可用数量不足 → 返回状态副本并在
      :func:`fill_plan` 的 ``error`` 中给出原因（不抛异常，不做部分成交）。
    * 本函数**不追加 history**，也不落盘 —— 那是 ``engine.submit`` 的职责。
    """
    new, _ = _fill(state, order, margin, fee_cfg)
    return new


def accrue_interest(state: AccountState, margin: MarginConfig, days: int = 1) -> AccountState:
    """按日计提融资利息 + 融券费用，累加到 ``interest_accrued``。

    * 融资利息 = ``financing_debt × financing_rate_annual / 365 × days``
    * 融券费用 = ``short_market_value（市值口径）× lending_rate_annual / 365 × days``
      （v3.1 冻结：基数用**市值**，不是 ``short_cost_debt``）
    * ``financing_debt`` **只记本金**，利息与费用全部走 ``interest_accrued``（无双重计提）
    """
    new = copy.deepcopy(state)
    if days <= 0:
        return new
    interest = float(new.financing_debt) * float(margin.financing_rate_annual) / 365.0 * int(days)
    lending_base = float(new.short_market_value)  # v3.1：市值口径
    fee = lending_base * float(margin.lending_rate_annual) / 365.0 * int(days)
    new.interest_accrued += interest + fee
    return new


def mark_to_market(state: AccountState, prices: dict[str, float]) -> AccountState:
    """按最新价刷新持仓 ``last_price`` / ``last_prices``。

    v3 下 ``short_market_value`` 由持仓派生（派生属性），无需额外重算负债字段。
    """
    new = copy.deepcopy(state)
    for code, px in (prices or {}).items():
        if px is None:
            continue
        try:
            price = float(px)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        key = _pid(code)
        new.last_prices[key] = price
        pos = new.positions.get(key)
        if pos is not None:
            pos.last_price = price
            for lot in pos.lots:
                lot.last_price = price
    return new


def settle_t1(state: AccountState, new_trade_date: str) -> AccountState:
    """推进结算日：解冻 T+1 批次、``frozen_t1`` 转回 ``available_cash``、更新 ``trade_date``。

    **绝不触碰 ``frozen_collateral``** —— 融券券款必须一直锁到买券还券为止。

    解冻条件：``lot.unlock_date <= new_trade_date`` 且 ``lot.open_date < new_trade_date``
    （后半条是防御：core 把 ``unlock_date`` 记为成交日，避免"当日 settle 当日解冻"）。
    """
    new = copy.deepcopy(state)
    for pos in new.positions.values():
        for lot in pos.lots:
            if lot.qty <= 0 or lot.available_qty >= lot.qty:
                continue
            unlock = lot.unlock_date or lot.open_date
            if unlock and unlock <= new_trade_date and (not lot.open_date or lot.open_date < new_trade_date):
                lot.available_qty = int(lot.qty)
        _recompute_long(pos)
        _recompute_short(pos)
    new.available_cash += new.frozen_t1
    new.frozen_t1 = 0.0
    new.trade_date = str(new_trade_date)
    return new


def check_risk(state: AccountState, cfg: RiskConfig, margin: MarginConfig) -> list[dict]:
    """风险评估，返回告警列表（纯函数，不改状态）。

    告警项 ``{"level": "warn"|"critical"|"info", "code": "...", "message": "..."}``。
    **code 集合已冻结（v3.1）**：``MAINTENANCE_FORCED`` / ``MAINTENANCE_WARN`` /
    ``MAINTENANCE_EXTRACTABLE`` / ``POSITION_LIMIT_PCT`` / ``POSITION_LIMIT_COUNT`` /
    ``EXPOSURE_LIMIT`` / ``SHORT_EXPOSURE_LIMIT`` / ``STOP_LOSS`` / ``TAKE_PROFIT``。
    状态归口请用 :func:`risk_status`。
    """
    alerts: list[dict] = []
    net = state.net_assets
    debt = state.total_debt

    if debt > 0:
        ratio = state.maintenance_ratio
        if ratio < float(margin.min_maintenance_ratio):
            alerts.append(
                {
                    "level": "critical",
                    "code": ALERT_MAINTENANCE_FORCED,
                    "message": (
                        f"维持担保比例 {ratio:.4f} 低于平仓线 "
                        f"{margin.min_maintenance_ratio:.2f}，仅允许减仓"
                    ),
                }
            )
        elif ratio < float(margin.warn_maintenance_ratio):
            alerts.append(
                {
                    "level": "warn",
                    "code": ALERT_MAINTENANCE_WARN,
                    "message": (
                        f"维持担保比例 {ratio:.4f} 低于警戒线 "
                        f"{margin.warn_maintenance_ratio:.2f}，禁止加仓"
                    ),
                }
            )
        elif ratio >= float(margin.max_maintenance_ratio):
            alerts.append(
                {
                    "level": "info",
                    "code": ALERT_MAINTENANCE_EXTRACTABLE,
                    "message": (
                        f"维持担保比例 {ratio:.4f} 达到可提取线 "
                        f"{margin.max_maintenance_ratio:.2f}，可提取担保物"
                    ),
                }
            )

    active = active_positions(state)
    if len(active) > int(cfg.max_positions):
        alerts.append(
            {
                "level": "warn",
                "code": ALERT_POSITION_LIMIT_COUNT,
                "message": f"持仓标的数 {len(active)} 超过上限 {cfg.max_positions}",
            }
        )

    if net > 0:
        for code, pos in sorted(active.items()):
            pct = pos.market_value / net
            if pct > float(cfg.max_position_pct):
                alerts.append(
                    {
                        "level": "warn",
                        "code": ALERT_POSITION_LIMIT_PCT,
                        "message": f"{code} 单票仓位 {pct:.2%} 超过上限 {cfg.max_position_pct:.2%}",
                    }
                )
        gross = state.gross_exposure
        if math.isfinite(gross) and gross > float(cfg.max_gross_exposure):
            alerts.append(
                {
                    "level": "warn",
                    "code": ALERT_EXPOSURE_LIMIT,
                    "message": f"总敞口 {gross:.2f} 倍净资产 超过上限 {cfg.max_gross_exposure:.2f}",
                }
            )
        short_exp = state.short_exposure
        if math.isfinite(short_exp) and short_exp > float(cfg.max_short_exposure):
            alerts.append(
                {
                    "level": "warn",
                    "code": ALERT_SHORT_EXPOSURE_LIMIT,
                    "message": (
                        f"融券敞口 {short_exp:.2f} 倍净资产 超过上限 {cfg.max_short_exposure:.2f}"
                    ),
                }
            )

    for code, pos in sorted(active.items()):
        if pos.long_qty > 0 and pos.long_cost > 0:
            pnl_pct = pos.last_price / pos.long_cost - 1.0
            if pnl_pct <= float(cfg.stop_loss_pct):
                alerts.append(
                    {
                        "level": "critical",
                        "code": ALERT_STOP_LOSS,
                        "message": f"{code} 浮动盈亏 {pnl_pct:.2%} 触发止损线 {cfg.stop_loss_pct:.2%}",
                    }
                )
            elif pnl_pct >= float(cfg.take_profit_pct):
                alerts.append(
                    {
                        "level": "info",
                        "code": ALERT_TAKE_PROFIT,
                        "message": f"{code} 浮动盈亏 {pnl_pct:.2%} 达到止盈线 {cfg.take_profit_pct:.2%}",
                    }
                )
    return alerts


def risk_status(state: AccountState, cfg: RiskConfig, margin: MarginConfig) -> str:
    """``risk_report().status`` 归口（v3.1）：``forced`` / ``margin_call`` / ``safe``。"""
    codes = {a["code"] for a in check_risk(state, cfg, margin)}
    if ALERT_MAINTENANCE_FORCED in codes:
        return "forced"
    if ALERT_MAINTENANCE_WARN in codes:
        return "margin_call"
    return "safe"


def refresh_risk_flags(state: AccountState, cfg: RiskConfig, margin: MarginConfig) -> AccountState:
    """按维持担保比例刷新 ``margin_call`` / ``forced_liquidation`` 标志（返回新状态）。

    * ``forced_liquidation``：比例 < ``min_maintenance_ratio``（平仓线）且总负债 > 0；
    * ``margin_call``：比例 < ``warn_maintenance_ratio``（警戒线）且总负债 > 0。
    """
    new = copy.deepcopy(state)
    if new.total_debt <= 0:
        new.margin_call = False
        new.forced_liquidation = False
        return new
    ratio = new.maintenance_ratio
    new.forced_liquidation = ratio < float(margin.min_maintenance_ratio)
    new.margin_call = ratio < float(margin.warn_maintenance_ratio)
    return new


def new_state(cfg: AppConfig) -> AccountState:
    """构造初始账户状态（现金 = ``cfg.account.initial_cash``），并绑定配置比例。"""
    cash = float(cfg.account.initial_cash)
    state = AccountState(
        trade_date="",
        initial_cash=cash,
        available_cash=cash,
        frozen_t1=0.0,
        frozen_collateral=0.0,
        financing_debt=0.0,
        short_cost_debt={},
        short_qty_debt={},
        interest_accrued=0.0,
        positions={},
        last_prices={},
        margin_call=False,
        forced_liquidation=False,
        history=[],
    )
    return bind_config(state, cfg)


def _next_lot_id(state: AccountState) -> str:
    top = 0
    for pos in state.positions.values():
        for lot in pos.lots:
            lid = str(lot.lot_id)
            if lid.startswith("L") and lid[1:].isdigit():
                top = max(top, int(lid[1:]))
    return f"L{top + 1:04d}"


def next_lot_id(state: AccountState) -> str:
    """下一个批次号（例如 ``L0001``），不修改状态。"""
    return _next_lot_id(state)


def next_order_id(state: AccountState) -> str:
    """下一个委托号（例如 ``O0001``），不修改状态。"""
    top = 0
    for order in state.history:
        oid = str(order.order_id)
        if oid.startswith("O") and oid[1:].isdigit():
            top = max(top, int(oid[1:]))
    return f"O{top + 1:04d}"


# ---- 查询辅助（engine / strategy / cli 共用） ------------------------------- #
def position_of(state: AccountState, code: str) -> Position | None:
    return state.positions.get(_pid(code))


def active_positions(state: AccountState) -> dict[str, Position]:
    """有持仓的标的（多头或空头数量 > 0）。"""
    return {code: pos for code, pos in state.positions.items() if pos.is_active}


def available_long(state: AccountState, code: str) -> int:
    pos = position_of(state, code)
    return int(pos.long_available) if pos else 0


def available_short(state: AccountState, code: str) -> int:
    pos = position_of(state, code)
    return int(pos.short_available) if pos else 0


# --------------------------------------------------------------------------- #
# 3.4 持久化（dict 互转，真正的文件 IO 在 cli）
# --------------------------------------------------------------------------- #
def _lot_to_dict(lot: Lot) -> dict[str, Any]:
    return {
        "lot_id": lot.lot_id,
        "code": lot.code,
        "open_date": lot.open_date,
        "unlock_date": lot.unlock_date,
        "qty": int(lot.qty),
        "available_qty": int(lot.available_qty),
        "cost_price": float(lot.cost_price),
        "side": lot.side,
        "kind": lot.kind,
        "last_price": float(lot.last_price),
    }


def _position_to_dict(pos: Position) -> dict[str, Any]:
    return {
        "code": pos.code,
        "name": pos.name,
        "long_qty": int(pos.long_qty),
        "long_available": int(pos.long_available),
        "short_qty": int(pos.short_qty),
        "short_available": int(pos.short_available),
        "long_cost": float(pos.long_cost),
        "short_cost": float(pos.short_cost),
        "last_price": float(pos.last_price),
        "lots": [_lot_to_dict(l) for l in pos.lots],
    }


def order_to_dict(order: Order) -> dict[str, Any]:
    """Order -> JSON 可序列化 dict（ledger 每行一条）。"""
    return {
        "order_id": order.order_id,
        "ts": order.ts,
        "trade_date": order.trade_date,
        "code": order.code,
        "name": order.name,
        "side": order.side,
        "qty": int(order.qty),
        "price": float(order.price),
        "status": order.status,
        "error_code": order.error_code,
        "reason": order.reason,
        "fee": float(order.fee),
        "tax": float(order.tax),
        "transfer_fee": float(order.transfer_fee),
        "cash_delta": float(order.cash_delta),
        "margin_used": float(order.margin_used),
        "debt_delta": float(order.debt_delta),
        "realized_pnl": float(order.realized_pnl),
    }


_ORDER_FIELDS = tuple(order_to_dict(Order()).keys())
_INT_ORDER_FIELDS = ("qty",)
_FLOAT_ORDER_FIELDS = (
    "price",
    "fee",
    "tax",
    "transfer_fee",
    "cash_delta",
    "margin_used",
    "debt_delta",
    "realized_pnl",
)


def order_from_dict(d: dict[str, Any]) -> Order:
    """dict -> Order（忽略未知键，缺失用默认值）。"""
    data = {k: v for k, v in (d or {}).items() if k in _ORDER_FIELDS}
    for key in _INT_ORDER_FIELDS:
        if key in data:
            data[key] = int(data[key])
    for key in _FLOAT_ORDER_FIELDS:
        if key in data:
            data[key] = float(data[key])
    return Order(**data)


def snapshot(state: AccountState) -> dict[str, Any]:
    """AccountState -> JSON 可序列化 dict（``restore`` 可无损还原）。"""
    return {
        "trade_date": state.trade_date,
        "initial_cash": float(state.initial_cash),
        "available_cash": float(state.available_cash),
        "frozen_t1": float(state.frozen_t1),
        "frozen_collateral": float(state.frozen_collateral),
        "financing_debt": float(state.financing_debt),
        "short_cost_debt": {str(k): float(v) for k, v in state.short_cost_debt.items()},
        "short_qty_debt": {str(k): int(v) for k, v in state.short_qty_debt.items()},
        "interest_accrued": float(state.interest_accrued),
        "positions": {code: _position_to_dict(pos) for code, pos in state.positions.items()},
        "last_prices": {str(k): float(v) for k, v in state.last_prices.items()},
        "margin_call": bool(state.margin_call),
        "forced_liquidation": bool(state.forced_liquidation),
        "history": [order_to_dict(o) for o in state.history],
    }


def _float(d: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, default))
    except (TypeError, ValueError):
        return default


def _int(d: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(d.get(key, default))
    except (TypeError, ValueError):
        return default


def restore(d: dict[str, Any], cfg: AppConfig) -> AccountState:
    """dict -> AccountState。容忍缺失键（用默认值）与未知键（忽略）。

    **``cfg`` 必填**（v3.1）：照 ``cfg.margin`` / ``cfg.risk`` 校验并绑定，
    不得静默使用默认参数——否则自定义 ``margin_ratio_long`` 下 ``margin_required`` 会算错。
    """
    data = d or {}
    positions: dict[str, Position] = {}
    for code, raw in (data.get("positions") or {}).items():
        if not isinstance(raw, dict):
            continue
        lots = []
        for raw_lot in raw.get("lots") or []:
            if not isinstance(raw_lot, dict):
                continue
            lots.append(
                Lot(
                    lot_id=str(raw_lot.get("lot_id", "")),
                    code=str(raw_lot.get("code", code)),
                    open_date=str(raw_lot.get("open_date", "")),
                    unlock_date=str(raw_lot.get("unlock_date", "")),
                    qty=_int(raw_lot, "qty"),
                    available_qty=_int(raw_lot, "available_qty"),
                    cost_price=_float(raw_lot, "cost_price"),
                    side=str(raw_lot.get("side", LONG)),
                    kind=str(raw_lot.get("kind", KIND_CASH)),
                    last_price=_float(raw_lot, "last_price"),
                )
            )
        key = _pid(str(code))
        positions[key] = Position(
            code=key,
            name=str(raw.get("name", "")),
            long_qty=_int(raw, "long_qty"),
            long_available=_int(raw, "long_available"),
            short_qty=_int(raw, "short_qty"),
            short_available=_int(raw, "short_available"),
            long_cost=_float(raw, "long_cost"),
            short_cost=_float(raw, "short_cost"),
            last_price=_float(raw, "last_price"),
            lots=lots,
        )

    short_qty_debt: dict[str, int] = {}
    for key, val in (data.get("short_qty_debt") or {}).items():
        qty = _int({"v": val}, "v")
        if qty > 0:
            short_qty_debt[str(key)] = qty
    short_cost_debt: dict[str, float] = {}
    for key, val in (data.get("short_cost_debt") or {}).items():
        amt = _float({"v": val}, "v")
        if abs(amt) > 1e-9:
            short_cost_debt[str(key)] = amt

    history = [order_from_dict(o) for o in (data.get("history") or []) if isinstance(o, dict)]
    state = AccountState(
        trade_date=str(data.get("trade_date", "")),
        initial_cash=_float(data, "initial_cash"),
        available_cash=_float(data, "available_cash"),
        frozen_t1=_float(data, "frozen_t1"),
        frozen_collateral=_float(data, "frozen_collateral"),
        financing_debt=_float(data, "financing_debt"),
        short_cost_debt=short_cost_debt,
        short_qty_debt=short_qty_debt,
        interest_accrued=_float(data, "interest_accrued"),
        positions=positions,
        last_prices={str(k): float(v) for k, v in (data.get("last_prices") or {}).items()},
        margin_call=bool(data.get("margin_call", False)),
        forced_liquidation=bool(data.get("forced_liquidation", False)),
        history=history,
    )
    bind_config(state, cfg)
    return state


def state_document(state: AccountState, initial_cash: float | None = None) -> dict[str, Any]:
    """``<root>/data/state.json`` 的完整文档（v3）。

    ``{"schema_version": 3, "app": {"initial_cash": ...}, "state": {...}}``
    """
    cash = float(state.initial_cash if initial_cash is None else initial_cash)
    return {
        "schema_version": SCHEMA_VERSION,
        "app": {"initial_cash": cash},
        "state": snapshot(state),
    }


def state_from_document(doc: dict[str, Any], cfg: AppConfig | None = None) -> AccountState:
    """解析 state.json 文档；``schema_version != 3`` 一律 ``ValueError``（不做静默迁移）。"""
    if not isinstance(doc, dict):
        raise ValueError("state.json 非法：顶层不是对象")
    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"state.json 的 schema_version={version!r} 不受支持，当前实现要求 {SCHEMA_VERSION}；"
            "v1/v2 账户文件不做静默迁移，请用 `python -m ptrade init --reset` 重建"
        )
    payload = doc.get("state")
    if not isinstance(payload, dict):
        raise ValueError("state.json 非法：缺少 state 对象")
    app = doc.get("app") if isinstance(doc.get("app"), dict) else {}
    state = restore(payload, cfg)
    if "initial_cash" in app:
        state.initial_cash = _float(app, "initial_cash", state.initial_cash)
    return state
