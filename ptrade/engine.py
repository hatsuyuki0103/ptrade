"""ptrade/engine.py — T2 引擎层（INTERFACES.md **v2**）。

职责：交易生命周期（``roll_to``）、下单校验与成交（``validate`` / ``submit``）、
建仓建议（``position_sizing`` / ``build_plan``）、平仓（``close_position``）、
风险报告（``risk_report``）。

只依赖 ``ptrade.core`` 与 ``ptrade.datasource``，不引入任何第三方依赖。

校验顺序严格遵循 ``INTERFACES.md`` v2 §5 强约束 10（确定性，先命中先返回）::

    参数合法性 → 停牌 → 涨跌停 → 风险门禁 → 白名单 → 手数 → T+1
    → 现金 → 保证金 → 券源 → 仓位/敞口

v2 关键口径（§3.2 / §3.3）：
* ``total_assets = available_cash + frozen_cash + long_market_value``（不含 margin_required）；
* ``total_debt = short_market_value + financing_debt + interest_accrued``；
* 融资负债 = ``N × (1 - m_long)``；融券卖出券款 ``+N`` 冻结；
* 开仓额度用 ``margin_required`` / ``margin_available``，**不参与总资产**。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .core import (
    AccountState,
    ErrorCode,
    Order,
    Side,
    accrue_interest,
    apply_fill,
    bind_config,
    calc_fees,
    check_risk,
    fill_plan,
    mark_to_market,
    next_order_id,
    risk_status,
    round_lot,
    settle_t1,
)
from .datasource import DataSource

__all__ = ["OrderRequest", "Engine"]

# ---- 方向分组 ---------------------------------------------------------------

#: 买入方向（委托价 >= 涨停价 时拒绝）
BUY_SIDES: tuple[Side, ...] = (Side.BUY, Side.MARGIN_BUY, Side.BUY_TO_COVER)
#: 卖出方向（委托价 <= 跌停价 时拒绝）
SELL_SIDES: tuple[Side, ...] = (Side.SELL, Side.SHORT_SELL, Side.REPAY_MARGIN)
#: 加仓方向（风险门禁：追保 / 平仓状态下一律拒绝）
ADD_SIDES: tuple[Side, ...] = (Side.BUY, Side.MARGIN_BUY, Side.SHORT_SELL)

_EPS = 1e-9


@dataclass
class OrderRequest:
    """下单请求。``price=None`` 表示市价（用最新价 ± 滑点）。"""

    code: str
    side: str
    qty: int
    price: float | None = None
    trade_date: str = ""


class Engine:
    """交易引擎：把 ``OrderRequest`` 变成账户状态变化，或给出确定的拒绝原因。"""

    def __init__(self, cfg: AppConfig, ds: DataSource, state: AccountState):
        self.cfg = cfg
        self.ds = ds
        # 绑定 cfg.margin / cfg.risk，使 AccountState.margin_required 用本账户的比例
        # （bind_config 只设置私有绑定属性，不是记账字段）
        self.state = bind_config(state, cfg)

    # ==================================================================
    # 生命周期
    # ==================================================================
    def roll_to(self, trade_date: str) -> AccountState:
        """推进到新交易日：结算 T+1 → 按日计息 → mark-to-market → 风险评估。

        * ``trade_date`` 早于当前交易日 → ``ValueError``
        * ``trade_date`` 不是交易日 → ``ValueError``
        * ``trade_date`` 等于当前交易日 → 幂等（不重复结算 / 不重复计息）
        """
        if not isinstance(trade_date, str) or not trade_date.strip():
            raise ValueError("trade_date 必须是非空 'YYYY-MM-DD' 字符串")
        trade_date = trade_date.strip()
        cur = self.state.trade_date

        if cur and trade_date < cur:
            raise ValueError(f"不能回退到更早的交易日：{trade_date} < {cur}")

        dates = list(self.ds.trade_dates() or [])
        if trade_date not in dates:
            raise ValueError(f"{trade_date} 不是交易日")

        if trade_date == cur:
            return self.state  # 幂等

        days = sum(1 for d in dates if cur < d <= trade_date)

        st = settle_t1(self.state, trade_date)
        if days > 0:
            st = accrue_interest(st, self.cfg.margin, days=days)
        st = mark_to_market(st, self._mark_prices(st, trade_date))
        st = self._refresh_risk_flags(st)
        self.state = st
        return self.state

    def _mark_prices(self, state: AccountState, trade_date: str) -> dict[str, float]:
        """取最新价做市值重估；取不到则沿用旧价（绝不抛异常）。"""
        prices: dict[str, float] = {}
        for code, pos in state.positions.items():
            px = None
            quote = self._quote(code, trade_date)
            if quote:
                last = self._as_float(quote.get("last"))
                if last is not None and last > 0:
                    px = last
            if px is None:
                try:
                    last_close = self._as_float(self.ds.last_close(code, trade_date))
                except Exception:
                    last_close = None
                if last_close is not None and last_close > 0:
                    px = last_close
            if px is None or px <= 0:
                px = pos.last_price
            if px:
                prices[code] = float(px)
        return prices

    def _refresh_risk_flags(self, state: AccountState) -> AccountState:
        """按维持担保比例刷新 ``margin_call`` / ``forced_liquidation``（v2 §3.2）。"""
        if float(state.total_debt) <= 0:
            state.margin_call = False
            state.forced_liquidation = False
            return state
        mr = self._as_float(state.maintenance_ratio)
        if mr is None:
            mr = math.inf
        state.forced_liquidation = bool(mr < self.cfg.margin.min_maintenance_ratio)
        state.margin_call = bool(mr < self.cfg.margin.warn_maintenance_ratio)
        return state

    # ==================================================================
    # 校验 / 下单
    # ==================================================================
    def validate(self, req: OrderRequest) -> Order:
        """纯校验：返回 status='filled' 的**预案**（含费用与资金影响）或 status='rejected'。

        绝不修改 ``self.state``。
        """
        return self._evaluate(req)

    def submit(self, req: OrderRequest) -> Order:
        """校验 → 成交（或拒单）→ apply_fill → 追加 history → 落 ledger.jsonl。

        拒单不抛异常；只有 IO 错误会抛出。
        """
        order = self._evaluate(req)
        if order.status == "filled":
            self.state = apply_fill(self.state, order, self.cfg.margin, self.cfg.fee)
            self._backfill_unlock_dates(order)
        self.state.history.append(order)
        self._append_ledger(order)
        return order

    def _backfill_unlock_dates(self, order: Order) -> None:
        """把本笔成交生成的批次 ``unlock_date`` 回填为**真实次日交易日**。

        core 无交易日历，只能把 ``unlock_date`` 记为成交日；引擎负责补上真实解冻日，
        以便 ``snapshot``/``restore`` 语义正确（``settle_t1`` 判据为
        ``unlock_date <= new_trade_date``）。
        """
        trade_date = order.trade_date or self.state.trade_date
        if not trade_date:
            return
        try:
            unlock = self.ds.next_trade_date(trade_date)
        except Exception:
            return
        if not unlock:
            return
        code = str(order.code)
        pos = self.state.positions.get(code)
        if pos is None:
            # core 内部会做 sh/sz 前缀归一，兜底按去前缀匹配
            for key, item in self.state.positions.items():
                if key == code.lstrip("shsz") or key in code:
                    pos = item
                    break
        if pos is None:
            return
        for lot in pos.lots:
            if lot.unlock_date == trade_date and lot.available_qty < lot.qty:
                lot.unlock_date = unlock

    # ------------------------------------------------------------------
    # 校验主体（顺序即契约，见模块 docstring）
    # ------------------------------------------------------------------
    def _evaluate(self, req: OrderRequest) -> Order:
        if not isinstance(req, OrderRequest):
            raise TypeError("req 必须是 OrderRequest")

        # ---------- 1. 参数合法性 ----------
        try:
            side = Side(req.side)
        except (ValueError, KeyError):
            return self._reject(req, ErrorCode.UNSUPPORTED, f"未知交易方向: {req.side!r}")

        code = str(req.code or "").strip()
        if not code:
            return self._reject(req, ErrorCode.NO_MARKET_DATA, "证券代码为空")

        td = (req.trade_date or self.state.trade_date or "").strip()
        if td != self.state.trade_date:
            return self._reject(
                req,
                ErrorCode.UNSUPPORTED,
                f"trade_date={td!r} 与账户交易日 {self.state.trade_date!r} 不一致，请先 roll_to",
            )

        if req.price is not None:
            px_in = self._as_float(req.price)
            if px_in is None or px_in <= 0:
                return self._reject(req, ErrorCode.NO_PRICE, f"委托价非法: {req.price!r}")

        # ---------- 2. 停牌 ----------
        quote = self._quote(code, td)
        if quote is None:
            return self._reject(req, ErrorCode.NO_MARKET_DATA, f"无 {code} 的行情数据")
        name = str(quote.get("name") or code)
        if quote.get("suspended"):
            return self._reject(
                req, ErrorCode.SUSPENDED, f"{name}({code}) 停牌或无有效行情", name=name
            )

        last = self._as_float(quote.get("last"))
        limit_up = self._as_float(quote.get("limit_up"))
        limit_down = self._as_float(quote.get("limit_down"))

        # ---------- 3. 涨跌停 ----------
        if req.price is not None:
            px = float(self._as_float(req.price))
        else:
            if last is None or last <= 0:
                return self._reject(
                    req, ErrorCode.NO_PRICE, f"{name}({code}) 无最新价，无法市价委托", name=name
                )
            px = self._market_price(last, side)

        if side in BUY_SIDES and limit_up and px >= limit_up:
            return self._reject(
                req,
                ErrorCode.LIMIT_UP_NO_BUY,
                f"{name}({code}) 涨停（委托价 {px:.4f} >= 涨停价 {limit_up:.4f}），不可买入",
                name=name,
                price=px,
            )
        if side in SELL_SIDES and limit_down and px <= limit_down:
            return self._reject(
                req,
                ErrorCode.LIMIT_DOWN_NO_SELL,
                f"{name}({code}) 跌停（委托价 {px:.4f} <= 跌停价 {limit_down:.4f}），不可卖出",
                name=name,
                price=px,
            )

        # ---------- 4. 风险门禁（账户状态优先于标的状态） ----------
        if side in ADD_SIDES:
            mr = self._as_float(self.state.maintenance_ratio)
            if mr is None:
                mr = math.inf
            forced = bool(self.state.forced_liquidation) or (
                float(self.state.total_debt) > 0
                and mr < self.cfg.margin.min_maintenance_ratio
            )
            margin_call = bool(self.state.margin_call) or (
                float(self.state.total_debt) > 0
                and mr < self.cfg.margin.warn_maintenance_ratio
            )
            if forced:
                return self._reject(
                    req,
                    ErrorCode.BELOW_MAINTENANCE,
                    f"维持担保比例 {mr:.4f} 低于平仓线 "
                    f"{self.cfg.margin.min_maintenance_ratio:.2f}，只允许减仓",
                    name=name,
                    price=px,
                )
            if margin_call:
                return self._reject(
                    req,
                    ErrorCode.MARGIN_CALL_BLOCKED,
                    f"维持担保比例 {mr:.4f} 低于警戒线 "
                    f"{self.cfg.margin.warn_maintenance_ratio:.2f}，追保期间禁止加仓",
                    name=name,
                    price=px,
                )

        # ---------- 5. 两融白名单 ----------
        if side in (Side.SHORT_SELL, Side.BUY_TO_COVER) and not self._is_short_target(code, td):
            return self._reject(
                req,
                ErrorCode.NOT_SHORT_TARGET,
                f"{name}({code}) 不是融券标的，不可融券卖出/买券还券",
                name=name,
                price=px,
            )
        if side == Side.MARGIN_BUY and not self._is_margin_target(code, td):
            return self._reject(
                req,
                ErrorCode.NOT_MARGIN_TARGET,
                f"{name}({code}) 不是融资融券标的，不可融资买入",
                name=name,
                price=px,
            )

        # ---------- 6. 手数 ----------
        qty = self._coerce_qty(req.qty)
        if qty is None or qty <= 0 or qty % 100 != 0:
            return self._reject(
                req,
                ErrorCode.INVALID_LOT,
                f"数量必须是 100 股的正整数倍，收到 {req.qty!r}",
                name=name,
                price=px,
            )

        amount = px * qty
        commission, tax, transfer_fee = calc_fees(side.value, px, qty, self.cfg.fee)
        fees = commission + tax + transfer_fee
        m_long = float(self.cfg.margin.margin_ratio_long)
        m_short = float(self.cfg.margin.margin_ratio_short)
        st = self.state
        key = self._key(code)
        pos = st.positions.get(key)

        # ---------- 7. T+1 / 持仓可用 ----------
        if side in (Side.SELL, Side.REPAY_MARGIN):
            err = self._check_long_available(req, pos, qty, name, px)
            if err is not None:
                return err
        elif side == Side.BUY_TO_COVER:
            err = self._check_short_available(req, pos, qty, name, px)
            if err is not None:
                return err

        # ---------- 8. 现金（v3：融资买入自有资金不出账，只扣费用） ----------
        cash = float(st.available_cash)
        if side == Side.BUY:
            need = amount + fees
        elif side == Side.MARGIN_BUY:
            need = fees
        elif side == Side.BUY_TO_COVER:
            need = (amount + fees) - self._released_short_proceeds(pos, qty)
        elif side == Side.SHORT_SELL:
            need = fees + max(0.0, m_short - 1.0) * amount
        elif side == Side.REPAY_MARGIN:
            # v3.2：只支持全额还款；卖出净额不足全额时，缺口由可用现金补 → 现金不足则拒
            debt = float(st.financing_debt)
            proceeds = amount - fees
            need = max(0.0, debt - proceeds)
        else:
            need = 0.0
        if need > cash + _EPS:
            return self._reject(
                req,
                ErrorCode.INSUFFICIENT_CASH,
                f"可用资金 {cash:.2f} 不足，需 {need:.2f}",
                name=name,
                price=px,
            )

        # ---------- 9. 保证金额度（v3：margin_required 不得越过净资产） ----------
        if side == Side.MARGIN_BUY:
            margin_add = amount * m_long
        elif side == Side.SHORT_SELL:
            margin_add = max(0.0, amount * m_short - amount)
        else:
            margin_add = 0.0

        if side in (Side.MARGIN_BUY, Side.SHORT_SELL):
            margin_required_new = float(st.margin_required) + margin_add
            if margin_required_new > float(st.net_assets) + _EPS:
                return self._reject(
                    req,
                    ErrorCode.INSUFFICIENT_MARGIN_SPACE,
                    f"保证金空间不足：占用后 {margin_required_new:.2f} > 净资产 "
                    f"{float(st.net_assets):.2f}（本次新增 {margin_add:.2f}）",
                    name=name,
                    price=px,
                )

        # ---------- 10. 杠杆 / 维持保证金（v3 强约束 5b） ----------
        if side in ADD_SIDES:
            projected = apply_fill(
                self.state,
                self._candidate_order(code, name, side, qty, px, td),
                self.cfg.margin,
                self.cfg.fee,
            )
            lev_after = self._as_float(projected.leverage)
            if lev_after is None:  # 净资产 <= 0 且仍有负债 → 无穷杠杆
                lev_after = math.inf
            mr_after = self._as_float(projected.maintenance_ratio)
            if mr_after is None:  # 无负债 → 无穷维持担保比例
                mr_after = math.inf
            max_leverage = float(self.cfg.risk.max_leverage)
            min_mr = float(self.cfg.margin.min_maintenance_ratio)
            if lev_after is not None and lev_after > max_leverage + _EPS:
                return self._reject(
                    req,
                    ErrorCode.LEVERAGE_LIMIT,
                    f"杠杆超限：开仓后 {lev_after:.4f} > 上限 {max_leverage:.2f}"
                    f"（负债 {float(projected.total_debt):.2f} / 净资产 "
                    f"{float(projected.net_assets):.2f}）",
                    name=name,
                    price=px,
                )
            if mr_after is not None and mr_after < min_mr - _EPS:
                return self._reject(
                    req,
                    ErrorCode.LEVERAGE_LIMIT,
                    f"开仓后维持担保比例 {mr_after:.4f} 低于平仓线 {min_mr:.2f}，禁止开仓",
                    name=name,
                    price=px,
                )

        # ---------- 11. 券源 ----------
        if side == Side.SHORT_SELL:
            source_limit = self._short_source_limit(quote, code, td)
            cumulative = int(st.short_qty_debt.get(key, 0)) + qty
            if 0 < source_limit < cumulative:
                return self._reject(
                    req,
                    ErrorCode.SHORT_SOURCE_LIMIT,
                    f"券源不足：{name}({code}) 累计欠券 {cumulative} 股 > 流通股本 "
                    f"{self.cfg.margin.short_source_limit_ratio:.2%} 上限 {source_limit} 股",
                    name=name,
                    price=px,
                )

        # ---------- 12. 仓位 / 敞口 ----------
        if side in ADD_SIDES:
            err = self._check_limits(req, code, name, side, amount, px)
            if err is not None:
                return err

        return self._order(
            req,
            status="filled",
            error_code=ErrorCode.OK,
            reason="校验通过",
            qty=qty,
            name=name,
            price=px,
            fee=commission,
            tax=tax,
            transfer_fee=transfer_fee,
        )

    # ------------------------------------------------------------------
    def _check_limits(
        self,
        req: OrderRequest,
        code: str,
        name: str,
        side: Side,
        amount: float,
        px: float,
    ) -> Order | None:
        """仓位与敞口限制（顺序：持仓数 → 单票仓位 → 总敞口 → 融券敞口）。"""
        risk = self.cfg.risk
        st = self.state
        net = float(st.net_assets)
        pos = st.positions.get(code)
        cur_value = (pos.long_value + pos.short_value) if pos is not None else 0.0
        long_mv = float(st.long_market_value)
        short_mv = float(st.short_market_value)

        if pos is None and len([p for p in st.positions.values() if p.is_active]) >= risk.max_positions:
            return self._reject(
                req,
                ErrorCode.POSITION_LIMIT_COUNT,
                f"持仓标的数已达上限 {risk.max_positions} 只，无法新建仓 {name}({code})",
                name=name,
                price=px,
            )

        if net <= 0:
            return self._reject(
                req,
                ErrorCode.EXPOSURE_LIMIT,
                f"净资产 {net:.2f} <= 0，禁止开仓",
                name=name,
                price=px,
            )

        cap = risk.max_position_pct * net
        if cur_value + amount > cap + _EPS:
            return self._reject(
                req,
                ErrorCode.POSITION_LIMIT_PCT,
                f"单票仓位超限：{name}({code}) 持仓 {cur_value:.2f} + 本次 {amount:.2f} "
                f"> 上限 {cap:.2f}（{risk.max_position_pct:.0%} x 净资产 {net:.2f}）",
                name=name,
                price=px,
            )

        gross = long_mv + short_mv + amount
        gross_cap = risk.max_gross_exposure * net
        if gross > gross_cap + _EPS:
            return self._reject(
                req,
                ErrorCode.EXPOSURE_LIMIT,
                f"总敞口超限：{gross:.2f} > 上限 {gross_cap:.2f}"
                f"（{risk.max_gross_exposure:.2f} x 净资产 {net:.2f}）",
                name=name,
                price=px,
            )

        if side == Side.SHORT_SELL:
            short_after = short_mv + amount
            short_cap = risk.max_short_exposure * net
            if short_after > short_cap + _EPS:
                return self._reject(
                    req,
                    ErrorCode.EXPOSURE_LIMIT,
                    f"融券敞口超限：{short_after:.2f} > 上限 {short_cap:.2f}"
                    f"（{risk.max_short_exposure:.2f} x 净资产 {net:.2f}）",
                    name=name,
                    price=px,
                )
        return None

    def _check_long_available(
        self, req: OrderRequest, pos, qty: int, name: str, px: float
    ) -> Order | None:
        held = int(pos.long_qty) if pos is not None else 0
        avail = int(pos.long_available) if pos is not None else 0
        if held < qty:
            return self._reject(
                req,
                ErrorCode.INSUFFICIENT_POSITION,
                f"{name} 多头持仓 {held} 股，不足卖出 {qty} 股",
                name=name,
                price=px,
            )
        if avail < qty:
            return self._reject(
                req,
                ErrorCode.T1_NOT_SETTLED,
                f"{name} 多头可卖 {avail} 股（T+1 未解冻），不足卖出 {qty} 股",
                name=name,
                price=px,
            )
        return None

    def _check_short_available(
        self, req: OrderRequest, pos, qty: int, name: str, px: float
    ) -> Order | None:
        held = int(pos.short_qty) if pos is not None else 0
        avail = int(pos.short_available) if pos is not None else 0
        if held < qty:
            return self._reject(
                req,
                ErrorCode.INSUFFICIENT_POSITION,
                f"{name} 融券未还 {held} 股，不足买券还券 {qty} 股",
                name=name,
                price=px,
            )
        if avail < qty:
            return self._reject(
                req,
                ErrorCode.T1_NOT_SETTLED,
                f"{name} 可买券还券 {avail} 股（T+1 未解冻），不足 {qty} 股",
                name=name,
                price=px,
            )
        return None

    # ==================================================================
    # 建仓建议
    # ==================================================================
    def position_sizing(self, code: str, price: float, score: float) -> dict:
        """按账户约束给出建议买入股数。

        返回 ``{"qty","amount","margin_required","binding_constraint","reason"}``，
        ``binding_constraint`` ∈ {"cash","position_pct","position_count","exposure","score","ok"}。
        ``margin_required`` = 本笔占用的**开仓额度**（现金买入即金额本身，融资买入为
        ``N × m_long``，融券卖出为 ``max(0, N × m_short - N)``）。
        """
        return self._size(code, float(price), float(score), Side.BUY)

    def _size(self, code: str, price: float, score: float, side: Side) -> dict:
        cfg = self.cfg
        st = self.state
        m_long = float(cfg.margin.margin_ratio_long)
        m_short = float(cfg.margin.margin_ratio_short)

        def out(qty: int, constraint: str, reason: str, margin_used: float = 0.0) -> dict:
            amount = max(qty, 0) * price if price > 0 else 0.0
            return {
                "qty": int(max(qty, 0)),
                "amount": round(amount, 2),
                "margin_required": round(margin_used, 2),
                "binding_constraint": constraint,
                "reason": reason,
            }

        if not (price > 0):
            return out(0, "score", f"价格非法（{price}），无法计算仓位")

        if side == Side.SHORT_SELL:
            if score > cfg.canslim.short_threshold:
                return out(
                    0,
                    "score",
                    f"得分 {score:.1f} 高于做空门槛 {cfg.canslim.short_threshold:.1f}，不做空",
                )
        elif score < cfg.canslim.buy_threshold:
            return out(
                0,
                "score",
                f"得分 {score:.1f} 低于买入门槛 {cfg.canslim.buy_threshold:.1f}",
            )

        if self._key(code) not in st.positions and len(
            [p for p in st.positions.values() if p.is_active]
        ) >= cfg.risk.max_positions:
            return out(0, "position_count", f"持仓标的数已达上限 {cfg.risk.max_positions} 只")

        net = float(st.net_assets)
        if net <= 0:
            return out(0, "exposure", f"净资产 {net:.2f} <= 0，无可用仓位额度")

        pos = st.positions.get(self._key(code))
        cur_value = (pos.long_value + pos.short_value) if pos is not None else 0.0
        long_mv = float(st.long_market_value)
        short_mv = float(st.short_market_value)
        cash = float(st.available_cash)

        room_pct = max(0.0, cfg.risk.max_position_pct * net - cur_value)
        room_gross = max(0.0, cfg.risk.max_gross_exposure * net - (long_mv + short_mv))
        debt = float(st.total_debt)
        collateral = float(st.collateral_value)
        room_leverage = max(0.0, cfg.risk.max_leverage * net - debt)
        min_mr = float(cfg.margin.min_maintenance_ratio)
        # 融资开仓与融券开仓都会同时让「担保物 +N、负债 +N」，
        # 故 ratio_after = (C+a)/(D+b) >= m  ⇔  N <= (C - m·D)/(m - 1)
        if min_mr > 1.0 + _EPS:
            room_maintenance = max(0.0, (collateral - min_mr * debt) / (min_mr - 1.0))
        else:
            room_maintenance = math.inf
        if side == Side.MARGIN_BUY:
            # v3：自有资金不出账（只扣费用），真正的约束是开仓额度 N×m_long、杠杆与维持担保比例
            room_margin = max(0.0, float(st.margin_available) / max(m_long, _EPS))
            rooms = {
                "position_pct": room_pct,
                "cash": min(room_margin, room_leverage, room_maintenance),
                "exposure": room_gross,
            }
        elif side == Side.SHORT_SELL:
            # 券款即保证金：m_short <= 1 时不额外占用额度
            extra = max(0.0, m_short - 1.0)
            room_margin = (
                max(0.0, float(st.margin_available)) / extra if extra > _EPS else math.inf
            )
            room_short = max(0.0, cfg.risk.max_short_exposure * net - short_mv)
            rooms = {
                "position_pct": room_pct,
                "exposure": min(room_gross, room_short, room_margin, room_leverage, room_maintenance),
            }
        else:
            rooms = {
                "position_pct": room_pct,
                "cash": cash,
                "exposure": room_gross,
            }

        binding = min(rooms, key=lambda k: rooms[k])
        budget = rooms[binding]
        if budget <= 0 or not math.isfinite(budget):
            if not math.isfinite(budget):
                budget = 0.0
            if budget <= 0:
                return out(0, binding, f"{binding} 额度已用尽（可用 {budget:.2f}）")

        qty = round_lot(int(budget / price))
        # 预留手续费 / 额度占用，避免「算得出但下不进去」
        while qty > 0:
            amount = qty * price
            c, _, t = calc_fees(side.value, price, qty, cfg.fee)
            fees = c + t
            if side == Side.BUY:
                ok = amount + fees <= cash + _EPS
            elif side == Side.MARGIN_BUY:
                # v3：现金只出费用；额度受开仓保证金空间约束
                ok = (
                    fees <= cash + _EPS
                    and float(st.margin_required) + amount * m_long <= net + _EPS
                )
            else:
                extra = max(0.0, m_short - 1.0) * amount
                ok = fees + extra <= cash + _EPS
            if ok:
                break
            qty -= 100
        if qty <= 0:
            return out(0, binding, f"{binding} 额度不足以买入 1 手（可用 {budget:.2f}，价格 {price:.2f}）")

        amount = qty * price
        if side == Side.MARGIN_BUY:
            margin_used = amount * m_long
        elif side == Side.SHORT_SELL:
            margin_used = max(0.0, amount * m_short - amount)
        else:
            margin_used = amount
        return {
            "qty": int(qty),
            "amount": round(amount, 2),
            "margin_required": round(margin_used, 2),
            "binding_constraint": binding,
            "reason": (
                f"按 {binding} 约束可开 {qty} 股（金额 {amount:.2f}，"
                f"占用额度 {margin_used:.2f}，净资产 {net:.2f}）"
            ),
        }

    def build_plan(self, recommendations: list[dict]) -> dict:
        """把策略层推荐转成可执行计划。"""
        st = self.state
        actions: list[dict] = []
        skipped: list[dict] = []
        notes: list[str] = []

        for rec in recommendations or []:
            code = str(rec.get("code") or "")
            name = str(rec.get("name") or code)
            act = str(rec.get("action") or "watch")
            side = str(rec.get("side") or "none")
            try:
                score = float(rec.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            reason = self._rec_reason(rec)

            if not code:
                skipped.append({"code": code, "action": act, "reason": "缺少 code 字段"})
                continue
            if act == "watch":
                actions.append(
                    self._action(code, name, "watch", side, 0, 0.0, 0.0, "观察，不产生交易", score)
                )
                continue
            if act not in ("open_long", "add_long", "open_short", "close"):
                # 未知 action：无方向信息的当观察处理，其余记入 skipped
                if side == "none":
                    actions.append(
                        self._action(
                            code, name, "watch", side, 0, 0.0, 0.0, "观察，不产生交易", score
                        )
                    )
                else:
                    skipped.append({"code": code, "action": act, "reason": f"未知 action: {act}"})
                continue

            pos = st.positions.get(code)
            if act in ("open_long", "add_long"):
                price = self._rec_price(rec, code)
                if price is None or price <= 0:
                    skipped.append({"code": code, "action": act, "reason": "无有效价格"})
                    continue
                sizing = self._size(code, float(price), score, Side.BUY)
                if sizing["qty"] <= 0:
                    skipped.append(
                        {
                            "code": code,
                            "action": act,
                            "reason": sizing["reason"],
                            "binding_constraint": sizing["binding_constraint"],
                        }
                    )
                    continue
                plan_act = "add_long" if (pos is not None and pos.long_qty > 0) else "open_long"
                actions.append(
                    self._action(
                        code, name, plan_act, "long", sizing["qty"], float(price),
                        sizing["amount"], reason, score,
                    )
                )

            elif act == "open_short":
                price = self._rec_price(rec, code)
                if price is None or price <= 0:
                    skipped.append({"code": code, "action": act, "reason": "无有效价格"})
                    continue
                if not self._is_short_target(code, st.trade_date):
                    skipped.append(
                        {"code": code, "action": act, "reason": f"{name}({code}) 不是融券标的"}
                    )
                    continue
                sizing = self._size(code, float(price), score, Side.SHORT_SELL)
                if sizing["qty"] <= 0:
                    skipped.append(
                        {
                            "code": code,
                            "action": act,
                            "reason": sizing["reason"],
                            "binding_constraint": sizing["binding_constraint"],
                        }
                    )
                    continue
                actions.append(
                    self._action(
                        code, name, "open_short", "short", sizing["qty"], float(price),
                        sizing["amount"], reason, score,
                    )
                )

            elif act == "close":
                price = self._rec_price(rec, code)
                px = float(price) if price else 0.0
                emitted = False
                if pos is not None and pos.short_qty > 0:
                    actions.append(
                        self._action(code, name, "close_short", "short", int(pos.short_qty), px,
                                     0.0, reason, score)
                    )
                    emitted = True
                if pos is not None and pos.long_qty > 0:
                    actions.append(
                        self._action(code, name, "close_long", "long", int(pos.long_qty), px,
                                     0.0, reason, score)
                    )
                    emitted = True
                if not emitted:
                    skipped.append({"code": code, "action": "close", "reason": "当前无持仓"})
            else:
                skipped.append({"code": code, "action": act, "reason": f"未知 action: {act}"})

        if not actions and not skipped:
            notes.append("无推荐输入")
        if st.forced_liquidation:
            notes.append("账户已触及平仓线：计划中的加仓动作会被拒绝，仅可减仓")
        elif st.margin_call:
            notes.append("账户已触及警戒线：追保期间禁止加仓")

        return {
            "generated_for": st.trade_date,
            "actions": actions,
            "skipped": skipped,
            "notes": notes,
        }

    @staticmethod
    def _action(
        code: str,
        name: str,
        action: str,
        side: str,
        qty: int,
        price: float,
        amount: float,
        reason: str,
        score: float,
    ) -> dict:
        return {
            "code": code,
            "name": name,
            "action": action,
            "side": side,
            "qty": int(qty),
            "price": round(float(price), 4),
            "amount": round(float(amount), 2),
            "margin_required": round(float(amount), 2),
            "reason": reason,
            "score": round(float(score), 4),
        }

    @staticmethod
    def _rec_reason(rec: dict) -> str:
        reasons = rec.get("reasons") or rec.get("reason") or []
        if isinstance(reasons, str):
            return reasons
        parts = [str(r) for r in reasons if str(r).strip()]
        return "；".join(parts) if parts else "策略推荐"

    def _rec_price(self, rec: dict, code: str) -> float | None:
        px = self._as_float(rec.get("entry"))
        if px is not None and px > 0:
            return px
        quote = self._quote(code, self.state.trade_date)
        if quote:
            last = self._as_float(quote.get("last"))
            if last is not None and last > 0:
                return last
        pos = self.state.positions.get(code)
        if pos is not None and pos.last_price > 0:
            return float(pos.last_price)
        return None

    # ==================================================================
    # 平仓
    # ==================================================================
    def close_position(self, code: str, trade_date: str, ratio: float = 1.0) -> list[Order]:
        """按 ``ratio`` 平掉多头与空头：先买券还券（空头），再卖券还款/卖出（多头）。"""
        td = trade_date or self.state.trade_date
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio <= 0:
            return []

        pos = self.state.positions.get(self._key(code))
        if pos is None:
            return [
                self._order(
                    OrderRequest(code=code, side=Side.SELL.value, qty=0, trade_date=td),
                    status="rejected",
                    error_code=ErrorCode.NOT_FOUND,
                    reason=f"无 {code} 持仓",
                )
            ]

        def _leg_qty(total: int) -> int:
            if total <= 0:
                return 0
            if ratio >= 1.0:
                return int(total)
            qty = round_lot(int(total * ratio))
            if qty < 100 and total >= 100:
                qty = 100  # 至少平 1 手
            return min(qty, int(total))

        orders: list[Order] = []

        # 1) 空头：买券还券
        short_qty = _leg_qty(int(pos.short_qty))
        if short_qty > 0:
            orders.append(
                self.submit(
                    OrderRequest(
                        code=code,
                        side=Side.BUY_TO_COVER.value,
                        qty=short_qty,
                        price=None,
                        trade_date=td,
                    )
                )
            )

        # 2) 多头：有融资负债 → 卖券还款；否则普通卖出
        pos = self.state.positions.get(self._key(code)) or pos
        long_qty = _leg_qty(int(pos.long_qty))
        if long_qty > 0:
            side = Side.REPAY_MARGIN if float(self.state.financing_debt) > 0 else Side.SELL
            orders.append(
                self.submit(
                    OrderRequest(
                        code=code,
                        side=side.value,
                        qty=long_qty,
                        price=None,
                        trade_date=td,
                    )
                )
            )
        return orders

    # ==================================================================
    # 风险报告
    # ==================================================================
    def risk_report(self) -> dict:
        """账户风险快照：维持担保比例、状态、告警、敞口、持仓明细。"""
        st = self.state
        cfg = self.cfg
        mr = self._as_float(st.maintenance_ratio)
        if mr is None:
            mr = math.inf
        net = float(st.net_assets)
        long_mv = float(st.long_market_value)
        short_mv = float(st.short_market_value)
        gross = long_mv + short_mv
        debt = float(st.total_debt)

        alerts: list[dict] = []
        try:
            alerts = [dict(a) for a in (check_risk(st, cfg.risk, cfg.margin) or [])]
            for a in alerts:
                a["level"] = self._text(a.get("level"))
                a["code"] = self._text(a.get("code"))
        except Exception as exc:  # 风险报告本身不得因为单条规则失败而崩
            alerts = [
                {
                    "level": "warn",
                    "code": "RISK_CHECK_ERROR",
                    "message": f"风险规则计算失败: {exc}",
                }
            ]

        # status 归口（v3.1/v3.3）：以 core.risk_status 的冻结告警码为准；
        # 账户上的 margin_call / forced_liquidation 标志只用于「加严」，不会放宽。
        try:
            base_status = risk_status(st, cfg.risk, cfg.margin)
        except Exception:
            base_status = "safe"
        if base_status == "forced" or st.forced_liquidation:
            status = "forced"
        elif base_status == "margin_call" or st.margin_call:
            status = "margin_call"
        elif any(a.get("level") in ("warn", "critical") for a in alerts):
            status = "warn"
        else:
            status = "safe"

        # MAINTENANCE_EXTRACTABLE（§3.3 冻结码，只读提示）：给出可提取金额读数。
        # 本版本不实现资金划出（L11），该读数仅用于展示。
        if debt > 0:
            extractable = max(
                0.0,
                float(st.collateral_value)
                - float(cfg.margin.max_maintenance_ratio) * debt,
            )
        else:
            extractable = 0.0
        for alert in alerts:
            if alert.get("code") == "MAINTENANCE_EXTRACTABLE":
                alert["extractable"] = round(extractable, 2)

        def ratio(value: float) -> float:
            return (value / net) if net > 0 else math.inf

        def ratio_json(value: float) -> float | None:
            """比例字段的 JSON 形态：非有限（无负债 / 净资产 ≤ 0）→ ``None``。

            CR-9：绝不能输出裸 ``Infinity`` —— 那会让 ``risk --json`` 变成非严格 JSON，
            且与 ``order`` 的 ``null`` 口径不一致。
            """
            num = float(value)
            if math.isnan(num) or math.isinf(num):
                return None
            return round(num, 6)

        positions: list[dict] = []
        for code, pos in sorted(st.positions.items()):
            value = pos.long_value + pos.short_value
            entry: dict = {
                "code": code,
                "name": pos.name,
                "long_qty": int(pos.long_qty),
                "long_available": int(pos.long_available),
                "short_qty": int(pos.short_qty),
                "short_available": int(pos.short_available),
                "long_cost": round(float(pos.long_cost or 0.0), 4),
                "short_cost": round(float(pos.short_cost or 0.0), 4),
                "last_price": round(float(pos.last_price or 0.0), 4),
                "long_value": round(float(pos.long_value), 2),
                "short_value": round(float(pos.short_value), 2),
                "long_pnl": round(float(pos.long_pnl), 2),
                "short_pnl": round(float(pos.short_pnl), 2),
                "weight": round(ratio(value), 6),
                "short_qty_debt": int(st.short_qty_debt.get(code, 0)),
                "short_cost_debt": round(float(st.short_cost_debt.get(code, 0.0)), 2),
                "stop_loss_pct": cfg.risk.stop_loss_pct,
                "take_profit_pct": cfg.risk.take_profit_pct,
            }
            if pos.long_qty > 0 and pos.long_cost > 0:
                ret = (pos.last_price - pos.long_cost) / pos.long_cost
                entry["long_return_pct"] = round(ret, 6)
                entry["stop_loss_triggered"] = bool(ret <= cfg.risk.stop_loss_pct)
                entry["take_profit_triggered"] = bool(ret >= cfg.risk.take_profit_pct)
            else:
                entry["long_return_pct"] = 0.0
                entry["stop_loss_triggered"] = False
                entry["take_profit_triggered"] = False
            positions.append(entry)

        return {
            # CR-9：无负债时维持担保比例为无穷 → 统一输出 null（严格 JSON），has_debt 区分语义
            "maintenance_ratio": ratio_json(mr),
            "status": status,
            "alerts": alerts,
            "exposure": {
                "net_assets": round(net, 2),
                "total_assets": round(float(st.total_assets), 2),
                "collateral_value": round(float(st.collateral_value), 2),
                "total_debt": round(debt, 2),
                "has_debt": bool(debt > 0),
                "leverage": ratio_json(st.leverage),
                "long_market_value": round(long_mv, 2),
                "short_market_value": round(short_mv, 2),
                "gross_exposure": round(gross, 2),
                "net_exposure": round(long_mv - short_mv, 2),
                "gross_ratio": ratio_json(ratio(gross)),
                "long_ratio": ratio_json(ratio(long_mv)),
                "short_ratio": ratio_json(ratio(short_mv)),
                "available_cash": round(float(st.available_cash), 2),
                "frozen_cash": round(float(st.frozen_cash), 2),
                "frozen_t1": round(float(st.frozen_t1), 2),
                "frozen_collateral": round(float(st.frozen_collateral), 2),
                "financing_debt": round(float(st.financing_debt), 2),
                "interest_accrued": round(float(st.interest_accrued), 2),
                "margin_required": round(float(st.margin_required), 2),
                "margin_available": round(float(st.margin_available), 2),
                "extractable": round(extractable, 2),
                "max_gross_exposure": cfg.risk.max_gross_exposure,
                "max_short_exposure": cfg.risk.max_short_exposure,
                "max_position_pct": cfg.risk.max_position_pct,
                "max_positions": cfg.risk.max_positions,
                "max_leverage": cfg.risk.max_leverage,
            },
            "positions": positions,
            "maintenance_ratio_lines": {
                "forced": cfg.margin.min_maintenance_ratio,
                "warn": cfg.margin.warn_maintenance_ratio,
                "max": cfg.margin.max_maintenance_ratio,
            },
            "trade_date": st.trade_date,
            "total_pnl": round(float(st.total_pnl), 2),
        }

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _quote(self, code: str, trade_date: str) -> dict | None:
        try:
            quote = self.ds.quote(code, trade_date)
        except Exception:
            return None
        if not quote:
            return None
        return dict(quote)

    def _is_margin_target(self, code: str, trade_date: str) -> bool:
        try:
            return bool(self.ds.is_margin_target(code, trade_date))
        except Exception:
            return False

    def _is_short_target(self, code: str, trade_date: str) -> bool:
        try:
            return bool(self.ds.is_short_target(code, trade_date))
        except Exception:
            return False

    def _short_source_limit(self, quote: dict, code: str, trade_date: str) -> int:
        """券源上限：流通股本 × ``short_source_limit_ratio``。数据缺失 → 0（不限制）。

        ``quote()`` 契约里没有 ``float_mv``，因此回退到 ``spot([code])`` 取流通市值。
        """
        float_shares = self._float_shares(quote, code, trade_date)
        if float_shares <= 0:
            return 0
        return int(float_shares * float(self.cfg.margin.short_source_limit_ratio))

    def _float_shares(self, quote: dict, code: str, trade_date: str) -> float:
        float_mv = self._as_float(quote.get("float_mv"))
        last_px = self._as_float(quote.get("last"))
        if float_mv is None or float_mv <= 0 or last_px is None or last_px <= 0:
            try:
                frame = self.ds.spot([code])
                if frame is not None and len(frame) > 0:
                    row = frame.iloc[0]
                    if float_mv is None or float_mv <= 0:
                        float_mv = self._as_float(row.get("float_mv"))
                    if last_px is None or last_px <= 0:
                        last_px = self._as_float(row.get("last"))
            except Exception:
                return 0.0
        if not float_mv or not last_px or float_mv <= 0 or last_px <= 0:
            return 0.0
        return float(float_mv) / float(last_px)

    def _released_short_proceeds(self, pos, qty: int) -> float:
        """买券还券时释放的冻结券款（按欠券比例分摊 ``short_cost_debt``）。

        单批次持仓下与 core 的 FIFO 券款消耗口径一致；多批次时按比例近似，
        且不超过 ``frozen_collateral``。
        """
        if pos is None or int(pos.short_qty) <= 0 or qty <= 0:
            return 0.0
        locked = float(self.state.short_cost_debt.get(str(pos.code), 0.0))
        ratio = min(1.0, float(qty) / float(pos.short_qty))
        return min(locked * ratio, float(self.state.frozen_collateral))

    @staticmethod
    def _key(code: str) -> str:
        """与 core 一致的标的键：去掉 sh/sz/bj 前缀、转小写。"""
        c = str(code or "").strip().lower()
        for pre in ("sh", "sz", "bj"):
            if c.startswith(pre):
                c = c[len(pre) :]
        return c

    def _market_price(self, last: float, side: Side) -> float:
        slip = float(self.cfg.fee.slippage_bps) / 10000.0
        px = last * (1.0 + slip) if side in BUY_SIDES else last * (1.0 - slip)
        return round(px, 4)

    @staticmethod
    def _as_float(value) -> float | None:
        """宽松转 float（拒 ``None`` / ``NaN`` / ``inf``）。"""
        if value is None or isinstance(value, bool):
            return None
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(out) or math.isinf(out):
            return None
        return out

    @staticmethod
    def _coerce_qty(value) -> int | None:
        """把请求里的数量规整为 int；非整数类型（含字符串）一律视为非法手数。"""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        return None

    @staticmethod
    def _text(value) -> str:
        """把 Enum / 任意对象安全转成契约字符串（注意：``str(Enum)`` 会给出 'Side.BUY'）。"""
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        return value.value if hasattr(value, "value") else str(value)

    @staticmethod
    def _side_value(side) -> str:
        return side.value if isinstance(side, Side) else str(side)

    @staticmethod
    def _code_value(code) -> str:
        return code.value if isinstance(code, ErrorCode) else str(code)

    def _candidate_order(self, code: str, name: str, side: Side, qty: int, px: float, td: str) -> Order:
        """构造一个仅用于「试算」的成交单（配合 core 的 ``apply_fill`` 投影开仓后状态）。"""
        return Order(
            order_id=next_order_id(self.state),
            ts=datetime.now().isoformat(timespec="seconds"),
            trade_date=td or self.state.trade_date,
            code=str(code),
            name=str(name or code),
            side=self._side_value(side),
            qty=int(qty),
            price=float(px),
            status="filled",
            error_code=ErrorCode.OK.value,
            reason="试算",
        )

    def _reject(
        self,
        req: OrderRequest,
        code: ErrorCode,
        reason: str,
        *,
        qty: int | None = None,
        name: str | None = None,
        price: float | None = None,
    ) -> Order:
        return self._order(
            req,
            status="rejected",
            error_code=code,
            reason=reason,
            qty=qty,
            name=name,
            price=price,
        )

    def _order(
        self,
        req: OrderRequest,
        *,
        status: str,
        error_code: ErrorCode,
        reason: str,
        qty: int | None = None,
        name: str | None = None,
        price: float | None = None,
        fee: float = 0.0,
        tax: float = 0.0,
        transfer_fee: float = 0.0,
    ) -> Order:
        if qty is None:
            qty = self._coerce_qty(req.qty) or 0
        td = (req.trade_date or self.state.trade_date or "").strip() or self.state.trade_date
        order = Order(
            order_id=next_order_id(self.state),
            ts=datetime.now().isoformat(timespec="seconds"),
            trade_date=td,
            code=str(req.code or ""),
            name=str(name or req.code or ""),
            side=self._side_value(req.side),
            qty=int(qty),
            price=float(price or 0.0),
            status=status,
            error_code=self._code_value(error_code),
            reason=reason,
            fee=float(fee),
            tax=float(tax),
            transfer_fee=float(transfer_fee),
        )
        if status == "filled":
            self._fill_audit_fields(order)
        return order

    def _fill_audit_fields(self, order: Order) -> None:
        """用 core 的权威口径（``fill_plan``）回填 Order 的审计字段。

        这样引擎的「预案」与 ``apply_fill`` 的实际入账**同源**，不可能出现口径漂移。
        """
        try:
            plan = fill_plan(self.state, order, self.cfg.margin, self.cfg.fee)
        except Exception:
            return
        if not isinstance(plan, dict):
            return
        for field in ("fee", "tax", "transfer_fee", "cash_delta", "margin_used", "realized_pnl"):
            if field in plan and plan[field] is not None:
                setattr(order, field, float(plan[field]))
        if plan.get("debt_delta") is not None:
            order.debt_delta = float(plan["debt_delta"])

    def _append_ledger(self, order: Order) -> None:
        root = Path(self.cfg.root)
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(order), ensure_ascii=False)
        with (data_dir / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
