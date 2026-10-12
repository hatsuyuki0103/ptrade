"""tests/test_engine.py — T2 引擎层测试（INTERFACES.md **v3**）。

覆盖 task-2 的 11 条硬要求 + v3 新增（§5 强约束 5b / LEVERAGE_LIMIT / §3.2 基准表 /
§8.1 不变量 INV-1..INV-10）。全部离线（``DataSource(offline=True)``），绝不联网。
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许直接 `pytest tests/test_engine.py`
    sys.path.insert(0, str(ROOT))

from ptrade.config import load_config  # noqa: E402
from ptrade.core import (  # noqa: E402
    ErrorCode,
    Order,
    Side,
    apply_fill,
    calc_fees,
    mark_to_market,
    new_state,
    round_lot,
)
from ptrade.datasource import DataSource  # noqa: E402
from ptrade.engine import Engine, OrderRequest  # noqa: E402

FIXTURE_DIR = ROOT / "data" / "fixture"

BIG = "600519"      # 样例强票
MID = "002594"      # 样例中性票
SMALL = "000001"    # 样例弱票 / 做空候选
FIN_ONLY = "601398"  # 两融标的但 can_short=False（负例）

AP = pytest.approx
ABS = 1e-6

#: 零费用 / 零利率配置（用于断言 §3.2 基准表与不变量 I1 的精确等式）
ZERO_FEE = dict(
    commission_rate=0.0,
    commission_min=0.0,
    stamp_tax_rate=0.0,
    transfer_fee_rate=0.0,
)


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #
class Env:
    def __init__(self, root: Path, cfg, ds: DataSource, usable: list[str]):
        self.root = root
        self.cfg = cfg
        self.ds = ds
        #: 日线夹具实际覆盖到的交易日（升序）——quote() 只在这些日子有行情
        self.usable = usable


def _daily_dates(ds: DataSource, code: str) -> list[str]:
    try:
        frame = ds.daily(code, "2025-01-01", "2030-12-31")
        dates = sorted({str(x)[:10] for x in frame["date"].tolist()})
        if dates:
            return dates
    except Exception:  # noqa: BLE001 - 夹具不可用时退化为交易日历
        pass
    return list(ds.trade_dates())


@pytest.fixture
def env(tmp_path):
    """把 data/fixture 复制到 tmp root（隔离 ledger/state，且 offline 可读）。"""
    if not FIXTURE_DIR.is_dir():
        pytest.skip("data/fixture 缺失（T1 生成）")
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DIR, root / "data" / "fixture")
    (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
    cfg = load_config(str(root))
    ds = DataSource(cfg, offline=True)
    return Env(root, cfg, ds, _daily_dates(ds, MID))


def make_engine(
    env: Env,
    *,
    zero_fee: bool = False,
    zero_rate: bool = False,
    risk_kw: dict | None = None,
    margin_kw: dict | None = None,
    account_kw: dict | None = None,
) -> Engine:
    cfg = env.cfg
    if zero_fee:
        cfg = replace(cfg, fee=replace(cfg.fee, **ZERO_FEE))
    if zero_rate:
        cfg = replace(
            cfg,
            margin=replace(
                cfg.margin, financing_rate_annual=0.0, lending_rate_annual=0.0
            ),
        )
    if risk_kw:
        cfg = replace(cfg, risk=replace(cfg.risk, **risk_kw))
    if margin_kw:
        cfg = replace(cfg, margin=replace(cfg.margin, **margin_kw))
    if account_kw:
        cfg = replace(cfg, account=replace(cfg.account, **account_kw))
    ds = DataSource(cfg, offline=True)
    state = new_state(cfg)
    # 锚定在「日线夹具覆盖的倒数第 3 个交易日」，保证 quote() 有行情且可连续 roll 两次
    usable = list(env.usable)
    state.trade_date = usable[max(0, len(usable) - 3)]
    engine = Engine(cfg, ds, state)
    engine.usable = usable  # 供 roll_next 判断可用的下一交易日
    return engine


# 常用放宽配置（仅放宽与「本条被测约束」无关的上限，避免误判）
def relaxed(**kw) -> dict:
    base = dict(max_position_pct=0.6, max_gross_exposure=10.0, max_leverage=10.0)
    base.update(kw)
    return base


def last(eng: Engine, code: str) -> float:
    """当前交易日该标的最新价（夹具值）。"""
    return float(eng.ds.quote(code, eng.state.trade_date)["last"])


def band(eng: Engine, code: str) -> dict:
    return dict(eng.ds.quote(code, eng.state.trade_date))


def req(code: str, side: str, qty: int, price: float | None = None) -> OrderRequest:
    return OrderRequest(code=code, side=side, qty=qty, price=price)


def roll_next(eng: Engine) -> str:
    """推进到下一个「日线夹具有行情」的交易日（这样 quote() 不会误判停牌）。"""
    cur = eng.state.trade_date
    nxt = eng.ds.next_trade_date(cur)
    usable = getattr(eng, "usable", None)
    if usable and nxt not in usable:
        later = [d for d in usable if d > cur]
        if later:
            nxt = later[0]
    eng.roll_to(nxt)
    return nxt


def buy_qty(eng: Engine, code: str, frac: float = 0.5) -> int:
    """按净资产比例算出可下手的整手数量。"""
    px = last(eng, code)
    qty = round_lot(int(frac * float(eng.state.net_assets) / px))
    assert qty >= 100, f"{code} 1 手 = {px * 100:.2f} 元，超出预算"
    return qty


def settle_long(eng: Engine, code: str, qty: int) -> None:
    """买入并跨到次一交易日，使多头可卖。"""
    order = eng.submit(req(code, "buy", qty, last(eng, code)))
    assert order.status == "filled", order.reason
    roll_next(eng)


def daily_codes() -> list[str]:
    """夹具里带日线（因此 quote() 有行情）的标的代码。"""
    return sorted(
        {
            path.stem.split("_")[1]
            for path in FIXTURE_DIR.glob("daily_*.json")
            if "_unadjust" not in path.stem
        }
    )


def code_with_flag(ds: DataSource, want: str) -> str | None:
    """从夹具的两融名单里找标的：

    * ``"non_margin"``  —— 有日线但**不在**两融名单（用于 NOT_MARGIN_TARGET）
    * ``"margin_only"`` —— 是融资标的但 ``can_short=False``（用于 NOT_SHORT_TARGET）

    注：当前夹具把 601398 标成 ``can_short=True``（§4 要求含一个 ``can_short=False`` 的
    *股票* 负例，实际只有 ETF 且无行情），故 ``"margin_only"`` 通常取不到，测试会退化为
    对 ``is_short_target`` 打桩（已报 Lead）。
    """
    try:
        frame = ds.margin_targets()
    except Exception:
        return None
    listed = {str(c).zfill(6): row for c, row in zip(frame["code"], frame.to_dict("records"))}
    for code in daily_codes():
        row = listed.get(code)
        if want == "non_margin" and row is None:
            return code
        if want == "margin_only" and row is not None and row["can_finance"] and not row["can_short"]:
            return code
    return None


def one_share_ratio(env: Env, code: str) -> float:
    """构造一个「券源上限恰为 1 股」的 short_source_limit_ratio（依赖夹具流通市值）。"""
    ds = DataSource(env.cfg, offline=True)
    try:
        frame = ds.spot([code])
        row = frame.iloc[0]
        float_shares = float(row["float_mv"]) / float(row["last"])
    except Exception:
        pytest.skip("夹具缺少 float_mv，无法构造券源上限")
    if not float_shares > 0:
        pytest.skip("夹具 float_mv 非法")
    return 1.0 / float_shares


# =========================================================================== #
# 1. T+1 双向
# =========================================================================== #
def test_t1_same_day_sell_is_t1_not_settled(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID)

    bought = eng.submit(req(MID, "buy", qty, px))
    assert bought.status == "filled", bought.reason
    pos = eng.state.positions[MID]
    assert pos.long_qty == qty
    assert pos.long_available == 0, "当日买入必须全部 locked"

    same_day = eng.submit(req(MID, "sell", qty, px))
    assert same_day.status == "rejected"
    assert same_day.error_code == ErrorCode.T1_NOT_SETTLED.value
    assert eng.state.positions[MID].long_qty == qty, "拒单不得改变持仓"


def test_t1_sell_succeeds_after_roll(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID)
    settle_long(eng, MID, qty)
    assert eng.state.positions[MID].long_available == qty

    sold = eng.submit(req(MID, "sell", qty, px))
    assert sold.status == "filled", sold.reason
    assert eng.state.positions[MID].long_qty == 0
    assert eng.state.frozen_t1 > 0


def test_t1_short_cover_same_day_rejected_then_ok(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, SMALL)
    qty = buy_qty(eng, SMALL, frac=0.1)

    opened = eng.submit(req(SMALL, "short_sell", qty, px))
    assert opened.status == "filled", opened.reason
    assert eng.state.positions[SMALL].short_available == 0

    same_day = eng.submit(req(SMALL, "buy_to_cover", qty, px))
    assert same_day.error_code == ErrorCode.T1_NOT_SETTLED.value

    roll_next(eng)
    assert eng.state.positions[SMALL].short_available == qty
    covered = eng.submit(req(SMALL, "buy_to_cover", qty, px))
    assert covered.status == "filled", covered.reason
    assert eng.state.positions[SMALL].short_qty == 0


def test_sell_beyond_holding_is_insufficient_position(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID)
    settle_long(eng, MID, qty)
    order = eng.submit(req(MID, "sell", qty + 100, px))
    assert order.error_code == ErrorCode.INSUFFICIENT_POSITION.value


def test_cover_beyond_short_is_insufficient_position(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, SMALL)
    qty = buy_qty(eng, SMALL, frac=0.1)
    assert eng.submit(req(SMALL, "short_sell", qty, px)).status == "filled"
    roll_next(eng)
    order = eng.submit(req(SMALL, "buy_to_cover", qty + 100, px))
    assert order.error_code == ErrorCode.INSUFFICIENT_POSITION.value


# =========================================================================== #
# 2. 两融白名单
# =========================================================================== #
def test_short_sell_non_short_target_rejected(env):
    """§5 强约束 2：非融券标的 SHORT_SELL → NOT_SHORT_TARGET。

    夹具里的 601398 实测 ``can_short=True``（§4 要求的 ``can_short=False`` 股票负例缺失，
    只有无行情的 ETF），故对名单接口打桩以确定性地覆盖该分支；已报 Lead。
    """
    eng = make_engine(env, risk_kw=relaxed())
    eng.ds.is_short_target = lambda code, asof=None: False
    order = eng.submit(req(FIN_ONLY, "short_sell", 1000, last(eng, FIN_ONLY)))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.NOT_SHORT_TARGET.value


def test_buy_to_cover_non_short_target_rejected(env):
    eng = make_engine(env, risk_kw=relaxed())
    eng.ds.is_short_target = lambda code, asof=None: False
    order = eng.validate(req(FIN_ONLY, "buy_to_cover", 100, last(eng, FIN_ONLY)))
    assert order.error_code == ErrorCode.NOT_SHORT_TARGET.value


def test_margin_buy_non_margin_target_rejected(env):
    eng = make_engine(env, risk_kw=relaxed())
    code = code_with_flag(eng.ds, "non_margin")
    if code is None:  # 夹具未含「有行情但非两融」的标的 → 用契约 API 桩定
        code = MID
        eng.ds.is_margin_target = lambda c, asof=None: False
    order = eng.submit(req(code, "margin_buy", 100, last(eng, code)))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.NOT_MARGIN_TARGET.value


def test_margin_buy_on_margin_target_filled(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    order = eng.submit(req(MID, "margin_buy", 100, px))
    assert order.status == "filled", order.reason


# =========================================================================== #
# 3. 涨跌停 / 停牌 / 无行情
# =========================================================================== #
def test_limit_up_blocks_buy(env):
    eng = make_engine(env, risk_kw=relaxed())
    quote = band(eng, MID)
    order = eng.submit(req(MID, "buy", 100, quote["limit_up"]))
    assert order.error_code == ErrorCode.LIMIT_UP_NO_BUY.value


def test_limit_down_blocks_sell(env):
    eng = make_engine(env, risk_kw=relaxed())
    qty = buy_qty(eng, MID)
    settle_long(eng, MID, qty)
    order = eng.submit(req(MID, "sell", qty, band(eng, MID)["limit_down"]))
    assert order.error_code == ErrorCode.LIMIT_DOWN_NO_SELL.value


def test_limit_down_blocks_repay_margin(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    assert eng.submit(req(MID, "margin_buy", 100, px)).status == "filled"
    roll_next(eng)
    order = eng.validate(req(MID, "repay_margin", 100, band(eng, MID)["limit_down"]))
    assert order.error_code == ErrorCode.LIMIT_DOWN_NO_SELL.value


def test_suspended_is_blocked(env):
    eng = make_engine(env, risk_kw=relaxed())
    quote = band(eng, MID)
    quote["suspended"] = True
    eng.ds.quote = lambda code, asof: dict(quote) if code == MID else None
    order = eng.submit(req(MID, "buy", 100, last(eng, MID)))
    assert order.error_code == ErrorCode.SUSPENDED.value


def test_unknown_code_no_market_data(env):
    eng = make_engine(env, risk_kw=relaxed())
    order = eng.submit(req("999999", "buy", 100, 10.0))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.NO_MARKET_DATA.value


def test_market_order_applies_slippage(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    slip = eng.cfg.fee.slippage_bps / 10000.0
    bought = eng.submit(OrderRequest(MID, "buy", 100, None))  # 市价
    assert bought.status == "filled", bought.reason
    assert bought.price == AP(round(px * (1 + slip), 4), abs=1e-9)


# =========================================================================== #
# 4. 风险状态门禁
# =========================================================================== #
@pytest.mark.parametrize("side", ["buy", "margin_buy", "short_sell"])
def test_forced_liquidation_blocks_all_adds(env, side):
    eng = make_engine(env, risk_kw=relaxed())
    code = MID if side != "short_sell" else SMALL
    eng.state.forced_liquidation = True
    order = eng.submit(req(code, side, 100, last(eng, code)))
    assert order.error_code == ErrorCode.BELOW_MAINTENANCE.value


@pytest.mark.parametrize("side", ["buy", "margin_buy", "short_sell"])
def test_margin_call_blocks_all_adds(env, side):
    eng = make_engine(env, risk_kw=relaxed())
    code = MID if side != "short_sell" else SMALL
    eng.state.margin_call = True
    order = eng.submit(req(code, side, 100, last(eng, code)))
    assert order.error_code == ErrorCode.MARGIN_CALL_BLOCKED.value


def test_forced_liquidation_still_allows_reduction(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID)
    settle_long(eng, MID, qty)
    eng.state.forced_liquidation = True
    order = eng.submit(req(MID, "sell", qty, px))
    assert order.status == "filled", order.reason


def test_margin_call_still_allows_cover(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, SMALL)
    qty = buy_qty(eng, SMALL, frac=0.1)
    assert eng.submit(req(SMALL, "short_sell", qty, px)).status == "filled"
    roll_next(eng)
    eng.state.margin_call = True
    order = eng.submit(req(SMALL, "buy_to_cover", qty, px))
    assert order.status == "filled", order.reason


# =========================================================================== #
# 5. 手数
# =========================================================================== #
@pytest.mark.parametrize("qty", [0, -100, 150, 99, 100.5, "100"])
def test_invalid_lot(env, qty):
    eng = make_engine(env, risk_kw=relaxed())
    order = eng.submit(req(MID, "buy", qty, last(eng, MID)))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.INVALID_LOT.value


# =========================================================================== #
# 6. 校验顺序确定性（§5 强约束 10）
# =========================================================================== #
def test_order_param_validity_first(env):
    eng = make_engine(env, risk_kw=relaxed())
    assert eng.validate(req(MID, "bogus_side", 100)).error_code == ErrorCode.UNSUPPORTED.value
    assert eng.validate(req("", "buy", 100)).error_code == ErrorCode.NO_MARKET_DATA.value
    assert eng.validate(req(MID, "buy", 100, -1.0)).error_code == ErrorCode.NO_PRICE.value
    assert eng.validate(req(MID, "buy", 100, float("nan"))).error_code == ErrorCode.NO_PRICE.value


def test_suspended_beats_limit_and_lot(env):
    eng = make_engine(env, risk_kw=relaxed())
    quote = band(eng, MID)
    quote["suspended"] = True
    eng.ds.quote = lambda code, asof: dict(quote) if code == MID else None
    # 停牌 > 涨跌停
    assert eng.validate(req(MID, "buy", 100, quote["limit_up"])).error_code == ErrorCode.SUSPENDED.value
    # 停牌 > 手数
    assert eng.validate(req(MID, "buy", 150)).error_code == ErrorCode.SUSPENDED.value


def test_limit_beats_risk_gate_and_lot(env):
    eng = make_engine(env, risk_kw=relaxed())
    up = band(eng, MID)["limit_up"]
    eng.state.forced_liquidation = True
    # 涨跌停 > 风险门禁
    assert eng.validate(req(MID, "buy", 100, up)).error_code == ErrorCode.LIMIT_UP_NO_BUY.value
    # 涨跌停 > 手数
    assert eng.validate(req(MID, "buy", 150, up)).error_code == ErrorCode.LIMIT_UP_NO_BUY.value


def test_risk_gate_beats_whitelist_and_margin(env):
    eng = make_engine(env, risk_kw=relaxed())
    eng.state.forced_liquidation = True
    # 风险门禁 > 白名单（600519 是两融标的，但账户状态优先）
    code = code_with_flag(eng.ds, "non_margin") or MID
    assert eng.validate(req(code, "margin_buy", 100, last(eng, code))).error_code == (
        ErrorCode.BELOW_MAINTENANCE.value
    )
    eng.state.forced_liquidation = False
    eng.state.margin_call = True
    assert eng.validate(req(code, "margin_buy", 100, last(eng, code))).error_code == (
        ErrorCode.MARGIN_CALL_BLOCKED.value
    )


def test_whitelist_beats_lot_and_cash(env):
    eng = make_engine(env, risk_kw=relaxed())
    eng.ds.is_short_target = lambda code, asof=None: False
    # 白名单 > 手数
    order = eng.validate(req(FIN_ONLY, "short_sell", 150, last(eng, FIN_ONLY)))
    assert order.error_code == ErrorCode.NOT_SHORT_TARGET.value
    # 白名单 > 现金（把可用现金压到 0，仍应报白名单）
    eng.state.available_cash = 0.0
    order = eng.validate(req(FIN_ONLY, "short_sell", 100, last(eng, FIN_ONLY)))
    assert order.error_code == ErrorCode.NOT_SHORT_TARGET.value


def test_lot_beats_t1(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID)
    assert eng.submit(req(MID, "buy", qty, px)).status == "filled"
    # 当日买入（T+1 未解冻）+ 手数非法 → 手数先命中
    assert eng.validate(req(MID, "sell", 150, px)).error_code == ErrorCode.INVALID_LOT.value
    # 手数合法 → T+1 命中
    assert eng.validate(req(MID, "sell", qty, px)).error_code == ErrorCode.T1_NOT_SETTLED.value


def test_t1_is_checked_before_position_limits(env):
    """T+1 在「仓位/敞口」之前：把持仓上限调成 0，当日卖出仍先报 T+1。"""
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID)
    assert eng.submit(req(MID, "buy", qty, px)).status == "filled"
    eng.cfg = replace(eng.cfg, risk=replace(eng.cfg.risk, max_positions=0, max_position_pct=0.0001))
    assert eng.validate(req(MID, "sell", qty, px)).error_code == ErrorCode.T1_NOT_SETTLED.value


def test_cash_beats_margin_space(env):
    eng = make_engine(env, account_kw={"initial_cash": 100.0}, risk_kw=relaxed())
    px = last(eng, MID)
    qty = round_lot(int(2_000_000 / px)) or 100
    order = eng.validate(req(MID, "margin_buy", qty, px))
    assert order.error_code == ErrorCode.INSUFFICIENT_CASH.value


def test_margin_space_beats_leverage(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = round_lot(int(1_500_000 / px)) or 100
    order = eng.validate(req(MID, "margin_buy", qty, px))
    assert order.error_code == ErrorCode.INSUFFICIENT_MARGIN_SPACE.value


def test_leverage_beats_short_source(env):
    eng = make_engine(
        env,
        margin_kw={"short_source_limit_ratio": one_share_ratio(env, SMALL)},
        risk_kw=relaxed(max_position_pct=10.0, max_gross_exposure=100.0, max_leverage=1.0),
    )
    px = last(eng, SMALL)
    qty = round_lot(int(1_500_000 / px)) or 100
    order = eng.validate(req(SMALL, "short_sell", qty, px))
    assert order.error_code == ErrorCode.LEVERAGE_LIMIT.value


def test_short_source_beats_position_limits(env):
    eng = make_engine(
        env,
        margin_kw={"short_source_limit_ratio": one_share_ratio(env, SMALL)},
        risk_kw=relaxed(max_position_pct=0.0001, max_gross_exposure=0.0001),
    )
    order = eng.validate(req(SMALL, "short_sell", 100, last(eng, SMALL)))
    assert order.error_code == ErrorCode.SHORT_SOURCE_LIMIT.value


def test_position_limits_are_last(env):
    eng = make_engine(env, risk_kw=relaxed(max_position_pct=0.2))
    px = last(eng, MID)
    qty = round_lot(int(0.5 * float(eng.state.net_assets) / px))
    order = eng.validate(req(MID, "buy", max(qty, 100), px))
    assert order.error_code == ErrorCode.POSITION_LIMIT_PCT.value


# =========================================================================== #
# 7. 账户数学（§3.3 v3 表格逐条）
# =========================================================================== #
def test_math_cash_buy(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    amount = px * qty
    comm, tax, transfer = calc_fees(Side.BUY.value, px, qty, eng.cfg.fee)
    order = eng.submit(req(MID, "buy", qty, px))
    assert order.status == "filled"
    st = eng.state
    assert st.available_cash == AP(500000.0 - amount - (comm + tax + transfer), abs=1e-6)
    assert st.frozen_collateral == 0.0 and st.frozen_t1 == 0.0
    assert st.financing_debt == 0.0
    pos = st.positions[MID]
    assert pos.long_qty == qty and pos.long_available == 0
    assert pos.lots[0].kind == "cash"
    assert pos.long_value == AP(amount, abs=1e-6)


def test_math_cash_sell_goes_to_frozen_t1(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    settle_long(eng, MID, qty)
    cash_before = eng.state.available_cash
    amount = px * qty
    comm, tax, transfer = calc_fees(Side.SELL.value, px, qty, eng.cfg.fee)
    order = eng.submit(req(MID, "sell", qty, px))
    assert order.status == "filled"
    st = eng.state
    assert st.available_cash == AP(cash_before, abs=1e-6), "普通卖出当日不动可用现金"
    assert st.frozen_t1 == AP(amount - (comm + tax + transfer), abs=1e-6)
    assert st.frozen_collateral == 0.0


def test_math_margin_buy_v3(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    amount = px * qty
    comm, tax, transfer = calc_fees(Side.MARGIN_BUY.value, px, qty, eng.cfg.fee)
    order = eng.submit(req(MID, "margin_buy", qty, px))
    assert order.status == "filled", order.reason
    st = eng.state
    assert st.available_cash == AP(500000.0 - (comm + tax + transfer), abs=1e-6), "v3：只扣费用"
    assert st.financing_debt == AP(amount, abs=1e-6), "v3：融资负债 = 全额 N"
    assert st.positions[MID].lots[0].kind == "margin"
    assert st.margin_required == AP(amount * eng.cfg.margin.margin_ratio_long, abs=1e-6)
    assert float(st.leverage) == AP(float(st.total_debt) / float(st.net_assets), abs=1e-9)


def test_math_short_sell_v3(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, SMALL)
    qty = buy_qty(eng, SMALL, 0.1)
    amount = px * qty
    comm, tax, transfer = calc_fees(Side.SHORT_SELL.value, px, qty, eng.cfg.fee)
    order = eng.submit(req(SMALL, "short_sell", qty, px))
    assert order.status == "filled", order.reason
    st = eng.state
    assert st.available_cash == AP(500000.0 - (comm + tax + transfer), abs=1e-6), "v3：融券卖出扣费用"
    assert st.frozen_collateral == AP(amount, abs=1e-6), "券款冻结"
    assert st.frozen_t1 == 0.0
    assert st.short_qty_debt[SMALL] == qty
    assert st.short_cost_debt[SMALL] == AP(amount, abs=1e-6)
    assert st.financing_debt == 0.0
    assert st.short_market_value == AP(amount, abs=1e-6)


def test_math_buy_to_cover_releases_collateral(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, SMALL)
    qty = buy_qty(eng, SMALL, 0.1)
    settle_short = eng.submit(req(SMALL, "short_sell", qty, px))
    assert settle_short.status == "filled"
    roll_next(eng)
    cash_before = eng.state.available_cash
    comm, tax, transfer = calc_fees(Side.BUY_TO_COVER.value, px, qty, eng.cfg.fee)
    order = eng.submit(req(SMALL, "buy_to_cover", qty, px))
    assert order.status == "filled", order.reason
    st = eng.state
    assert st.frozen_collateral == AP(0.0, abs=1e-6)
    assert st.available_cash == AP(cash_before - (comm + tax + transfer), abs=1e-6)
    assert SMALL not in st.short_qty_debt, "还清后应删除键"
    assert st.positions[SMALL].short_qty == 0


def test_math_repay_margin_full_repayment_only(env):
    """v3.2：REPAY_MARGIN 只支持全额还款 → financing_debt 清零，缺口由可用现金补。"""
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    roll_next(eng)
    amount = px * qty
    comm, tax, transfer = calc_fees(Side.REPAY_MARGIN.value, px, qty, eng.cfg.fee)
    debt_before = eng.state.financing_debt
    cash_before = eng.state.available_cash
    order = eng.submit(req(MID, "repay_margin", qty, px))
    assert order.status == "filled", order.reason
    st = eng.state
    proceeds = amount - (comm + tax + transfer)
    assert debt_before == AP(amount, abs=1e-6)
    assert st.financing_debt == AP(0.0, abs=1e-6), "全额还款后负债清零"
    assert st.available_cash == AP(cash_before + proceeds - debt_before, abs=1e-6)
    assert st.positions[MID].long_qty == 0


def test_repay_margin_rejects_when_cash_cannot_cover_shortfall(env):
    """v3.2：卖出净额 < 全额负债且可用现金补不上 → INSUFFICIENT_CASH。"""
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    roll_next(eng)
    eng.state.available_cash = 1.0  # 现金枯竭（卖出净额仍差一笔费用）
    order = eng.submit(req(MID, "repay_margin", qty, px))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.INSUFFICIENT_CASH.value
    assert eng.state.financing_debt == AP(px * qty, abs=1e-6), "拒单不得改动负债"


# =========================================================================== #
# 8. v3 数值基准表（INV-4）
# =========================================================================== #
ZERO = dict(zero_fee=True, zero_rate=True)
WIDE = relaxed(max_position_pct=1.0, max_gross_exposure=10.0, max_short_exposure=10.0, max_leverage=100.0)


def test_baseline_row1_initial(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    st = eng.state
    assert (st.available_cash, st.frozen_collateral, st.long_market_value, st.financing_debt) == (
        500000.0, 0.0, 0.0, 0.0,
    )
    assert (st.collateral_value, st.total_debt, st.net_assets) == (500000.0, 0.0, 500000.0)
    assert math.isinf(st.maintenance_ratio)


def test_baseline_row2_margin_buy_200k(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, MID)
    qty = round_lot(int(200000 / px))
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    st = eng.state
    notional = qty * px
    assert (st.available_cash, st.frozen_collateral, st.long_market_value, st.financing_debt) == (
        500000.0, 0.0, notional, notional,
    )
    assert st.collateral_value == AP(500000.0 + notional, abs=1e-6)
    assert st.total_debt == AP(notional, abs=1e-6)
    assert st.net_assets == AP(500000.0, abs=1e-6)
    assert st.maintenance_ratio == AP((500000.0 + notional) / notional, abs=1e-9)


def test_baseline_row3_price_drop_20pct(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, MID)
    qty = round_lot(int(200000 / px))
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    notional = qty * px
    crashed = px * 0.8
    eng.state = mark_to_market(eng.state, {MID: crashed})
    st = eng.state
    assert st.long_market_value == AP(notional * 0.8, abs=1e-6)
    assert st.collateral_value == AP(500000.0 + notional * 0.8, abs=1e-6)
    assert st.total_debt == AP(notional, abs=1e-6)
    assert st.net_assets == AP(500000.0 - notional * 0.2, abs=1e-6)
    assert st.maintenance_ratio == AP((500000.0 + notional * 0.8) / notional, abs=1e-9)


def test_baseline_row4_short_sell_200k(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, SMALL)
    qty = round_lot(int(200000 / px))
    assert eng.submit(req(SMALL, "short_sell", qty, px)).status == "filled"
    st = eng.state
    notional = qty * px
    assert (st.available_cash, st.frozen_collateral, st.long_market_value, st.financing_debt) == (
        500000.0, notional, 0.0, 0.0,
    )
    # v3.1：融券券款计入担保物（仅排除 frozen_t1）
    assert st.collateral_value == AP(500000.0 + notional, abs=1e-6)
    assert st.total_debt == AP(notional, abs=1e-6)
    assert st.net_assets == AP(500000.0, abs=1e-6)
    assert st.maintenance_ratio == AP((500000.0 + notional) / notional, abs=1e-9)


def test_baseline_row5_short_price_drop_20pct(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, SMALL)
    qty = round_lot(int(200000 / px))
    assert eng.submit(req(SMALL, "short_sell", qty, px)).status == "filled"
    notional = qty * px
    eng.state = mark_to_market(eng.state, {SMALL: px * 0.8})
    st = eng.state
    assert st.short_market_value == AP(notional * 0.8, abs=1e-6)
    assert st.total_debt == AP(notional * 0.8, abs=1e-6)
    assert st.net_assets == AP(500000.0 + notional * 0.2, abs=1e-6)
    assert st.maintenance_ratio == AP((500000.0 + notional) / (notional * 0.8), abs=1e-9)


def test_v31_short_collateral_counts_in_numerator(env):
    """v3.1 基准：融券 20 万含费用 → ratio 3.49924（不带零费用口径）。"""
    eng = make_engine(env, risk_kw=WIDE)
    px = last(eng, SMALL)
    qty = round_lot(int(200000 / px))
    assert eng.submit(req(SMALL, "short_sell", qty, px)).status == "filled"
    st = eng.state
    notional = qty * px
    comm, tax, transfer = calc_fees(Side.SHORT_SELL.value, px, qty, eng.cfg.fee)
    fees = comm + tax + transfer
    assert st.available_cash == AP(500000.0 - fees, abs=1e-6)
    assert st.frozen_collateral == AP(notional, abs=1e-6)
    assert st.maintenance_ratio == AP((500000.0 - fees + notional) / notional, abs=1e-9)


# =========================================================================== #
# 9. 不变量 INV-1/2/3/5/6/8/9/10 + leverage
# =========================================================================== #
def test_inv1_open_keeps_net_assets_buy(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(MID, "buy", 1000, last(eng, MID))).status == "filled"
    assert eng.state.net_assets == AP(500000.0, abs=1e-6)


def test_inv1_open_keeps_net_assets_margin_buy(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(MID, "margin_buy", 1000, last(eng, MID))).status == "filled"
    assert eng.state.net_assets == AP(500000.0, abs=1e-6)


def test_inv1_open_keeps_net_assets_short_sell(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(SMALL, "short_sell", 1000, last(eng, SMALL))).status == "filled"
    assert eng.state.net_assets == AP(500000.0, abs=1e-6)


def test_inv1_close_legs_keep_net_assets_zero_fee(env):
    px = last(make_engine(env, **ZERO, risk_kw=WIDE), MID)
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(MID, "buy", 1000, px)).status == "filled"
    roll_next(eng)
    assert eng.submit(req(MID, "sell", 1000, px)).status == "filled"
    assert eng.state.net_assets == AP(500000.0, abs=1e-6)

    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(MID, "margin_buy", 1000, px)).status == "filled"
    roll_next(eng)
    assert eng.submit(req(MID, "repay_margin", 1000, px)).status == "filled"
    assert eng.state.net_assets == AP(500000.0, abs=1e-6)

    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    spx = last(eng, SMALL)
    assert eng.submit(req(SMALL, "short_sell", 1000, spx)).status == "filled"
    roll_next(eng)
    assert eng.submit(req(SMALL, "buy_to_cover", 1000, spx)).status == "filled"
    assert eng.state.net_assets == AP(500000.0, abs=1e-6)


def test_inv2_price_move_equals_pnl_minus_interest(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, MID)
    qty = 1000
    assert eng.submit(req(MID, "buy", qty, px)).status == "filled"
    eng.state = mark_to_market(eng.state, {MID: px * 1.2})
    st = eng.state
    pnl = st.positions[MID].long_pnl
    assert st.net_assets - st.initial_cash == AP(pnl - st.interest_accrued, abs=1e-6)


def test_inv2_short_price_move_equals_pnl(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, SMALL)
    assert eng.submit(req(SMALL, "short_sell", 1000, px)).status == "filled"
    eng.state = mark_to_market(eng.state, {SMALL: px * 0.8})
    st = eng.state
    pnl = st.positions[SMALL].short_pnl
    assert st.net_assets - st.initial_cash == AP(pnl - st.interest_accrued, abs=1e-6)


def test_inv3_maintenance_ratio_formula(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(MID, "margin_buy", 1000, last(eng, MID))).status == "filled"
    st = eng.state
    assert st.maintenance_ratio == AP(st.collateral_value / st.total_debt, abs=1e-12)
    assert st.collateral_value == AP(st.available_cash + st.long_market_value, abs=1e-9)


def test_inv5_margin_required_not_in_total_assets(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(MID, "margin_buy", 1000, last(eng, MID))).status == "filled"
    st = eng.state
    assert st.margin_required > 0
    assert st.total_assets == AP(
        st.available_cash + st.frozen_t1 + st.frozen_collateral + st.long_market_value, abs=1e-9
    )
    assert abs(st.total_assets - (st.total_assets + st.margin_required)) > 1.0  # v1 回归哨兵


def test_inv6_financing_debt_is_full_notional(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, MID)
    qty = 1000
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    st = eng.state
    assert st.financing_debt == AP(px * qty, abs=1e-6)
    assert abs(st.financing_debt - px * qty * (1 - eng.cfg.margin.margin_ratio_long)) > 1.0


def test_inv8_short_collateral_survives_roll(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, SMALL)
    qty = 1000
    assert eng.submit(req(SMALL, "short_sell", qty, px)).status == "filled"
    cash = eng.state.available_cash
    collateral = eng.state.frozen_collateral
    roll_next(eng)
    assert eng.state.available_cash == AP(cash, abs=1e-6), "券款绝不随交易日解冻"
    assert eng.state.frozen_collateral == AP(collateral, abs=1e-6)


def test_inv9_sell_proceeds_released_by_roll(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, MID)
    settle_long(eng, MID, 1000)
    cash = eng.state.available_cash
    assert eng.submit(req(MID, "sell", 1000, px)).status == "filled"
    frozen = eng.state.frozen_t1
    assert frozen > 0
    roll_next(eng)
    assert eng.state.frozen_t1 == AP(0.0, abs=1e-9)
    assert eng.state.available_cash == AP(cash + frozen, abs=1e-6)


def test_inv10_frozen_cash_is_sum(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    settle_long(eng, MID, 1000)
    assert eng.submit(req(MID, "sell", 1000, last(eng, MID))).status == "filled"
    assert eng.submit(req(SMALL, "short_sell", 1000, last(eng, SMALL))).status == "filled"
    st = eng.state
    assert st.frozen_t1 > 0 and st.frozen_collateral > 0
    assert st.frozen_cash == AP(st.frozen_t1 + st.frozen_collateral, abs=1e-9)


def test_leverage_property(env):
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.submit(req(MID, "margin_buy", 1000, last(eng, MID))).status == "filled"
    st = eng.state
    assert float(st.leverage) == AP(float(st.total_debt) / float(st.net_assets), abs=1e-12)


# =========================================================================== #
# 10. 杠杆 / 维持保证金（§5 强约束 5b）
# =========================================================================== #
def test_leverage_limit_blocks_250k_margin_buy_and_proof(env):
    """TC-E-33（v3.1）：把 m_long 调低到 0.30 让「保证金额度」不再先卡死，
    此时 `max_leverage` 作为回退保护接管 → LEVERAGE_LIMIT。
    """
    px = last(make_engine(env, risk_kw=WIDE), MID)
    qty = round_lot(int(1_666_666 / px))
    notional = qty * px

    eng = make_engine(
        env,
        zero_fee=True,
        risk_kw=relaxed(max_position_pct=100.0, max_gross_exposure=1000.0, max_leverage=2.0),
        margin_kw={"margin_ratio_long": 0.30},
    )
    # 保证金额度：N × 0.30 <= 500000 → N <= 1666666，故这笔能过额度闸
    assert notional * 0.30 <= 500000.0 + 1.0
    order = eng.submit(req(MID, "margin_buy", qty, px))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.LEVERAGE_LIMIT.value
    assert eng.state.financing_debt == 0.0

    # 证明：用 core 的权威投影（绕过引擎校验）看这笔开仓的真实后果
    probe = make_engine(
        env,
        zero_fee=True,
        risk_kw=relaxed(max_position_pct=100.0, max_gross_exposure=1000.0, max_leverage=2.0),
        margin_kw={"margin_ratio_long": 0.30},
    )
    candidate = Order(
        order_id="O9001",
        trade_date=probe.state.trade_date,
        code=MID,
        name="probe",
        side=Side.MARGIN_BUY.value,
        qty=qty,
        price=px,
        status="filled",
    )
    projected = apply_fill(probe.state, candidate, probe.cfg.margin, probe.cfg.fee)
    assert projected.leverage > probe.cfg.risk.max_leverage
    assert projected.leverage == AP(notional / 500000.0, abs=1e-9)
    assert projected.maintenance_ratio == AP((500000.0 + notional) / notional, abs=1e-9)


def test_leverage_limit_blocks_open_below_maintenance(env):
    """即使杠杆未超，开仓后 ratio < 1.30 也必须拒（同样 LEVERAGE_LIMIT）。"""
    eng = make_engine(
        env,
        zero_fee=True,
        risk_kw=relaxed(max_position_pct=100.0, max_gross_exposure=1000.0, max_leverage=100.0),
    )
    px = last(eng, SMALL)
    qty = round_lot(int(2_000_000 / px))  # ratio_after = (500000+N)/N < 1.30 ⇔ N > 1666666
    order = eng.submit(req(SMALL, "short_sell", qty, px))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.LEVERAGE_LIMIT.value


def test_v31_margin_capacity_boundaries(env):
    """v3.1：默认参数下融资容量由保证金额度绑定 —— 625000 可建、1000000 被拒。"""
    px = last(make_engine(env, risk_kw=WIDE), MID)
    wide = relaxed(max_position_pct=100.0, max_gross_exposure=1000.0, max_leverage=2.0)

    ok_qty = round_lot(int(625_000 / px))
    eng = make_engine(env, zero_fee=True, risk_kw=wide)
    opened = eng.submit(req(MID, "margin_buy", ok_qty, px))
    assert opened.status == "filled", opened.reason
    notional = ok_qty * px
    assert notional <= 625_000.0 + 1.0
    assert eng.state.financing_debt == AP(notional, abs=1e-6)
    assert eng.state.maintenance_ratio == AP((500000.0 + notional) / notional, abs=1e-9)

    for target in (1_000_000, 2_500_000):
        eng2 = make_engine(env, zero_fee=True, risk_kw=wide)
        qty = round_lot(int(target / px))
        order = eng2.submit(req(MID, "margin_buy", qty, px))
        assert order.status == "rejected"
        assert order.error_code == ErrorCode.INSUFFICIENT_MARGIN_SPACE.value, (
            f"N≈{target}: {order.error_code}"
        )


def test_v31_full_margin_then_crash_crosses_130pct(env):
    """纯函数口径（v3.2 §3.2 注）：N≈625000 的开仓 ratio 与 1.30 临界跌幅。

    该仓位**不是引擎可达**的（引擎路径下 `POSITION_LIMIT_PCT` 单票 20% 会先拒），
    故这里放宽仓位上限以验证 maintain ratio 的数学；引擎可达的穿仓场景见
    ``test_engine_reachable_forced_liquidation_5_positions``。
    """
    eng = make_engine(
        env,
        zero_fee=True,
        zero_rate=True,
        risk_kw=relaxed(max_position_pct=10.0, max_gross_exposure=100.0, max_leverage=2.0),
    )
    px = last(eng, MID)
    qty = round_lot(int(625_000 / px))
    notional = qty * px
    opened = eng.submit(req(MID, "margin_buy", qty, px))
    assert opened.status == "filled", opened.reason
    assert eng.state.maintenance_ratio == AP((500000.0 + notional) / notional, abs=1e-9)

    # 精确求解 ratio == 1.30 的临界价：(500000 + qty·p) / notional = 1.30
    critical = (1.30 * notional - 500000.0) / qty
    assert 0 < critical < px

    base_quote = eng.ds.quote
    eng.ds.quote = lambda code, asof: (
        {**base_quote(code, asof), "last": critical} if code == MID else base_quote(code, asof)
    )
    roll_next(eng)
    assert eng.state.maintenance_ratio == AP(1.30, abs=1e-9), "恰好 1.30"
    # core 用严格「<」判定平仓线：恰好 130% 属追保，不算强制平仓
    assert eng.state.forced_liquidation is False
    assert eng.risk_report()["status"] == "margin_call"

    # 再跌 → 穿仓
    eng.ds.quote = lambda code, asof: (
        {**base_quote(code, asof), "last": critical * 0.99} if code == MID else base_quote(code, asof)
    )
    roll_next(eng)
    assert eng.state.maintenance_ratio < 1.30
    assert eng.state.forced_liquidation is True
    assert eng.risk_report()["status"] == "forced"
    assert eng.validate(req(BIG, "buy", 100, last(eng, BIG))).error_code == (
        ErrorCode.BELOW_MAINTENANCE.value
    )


def test_engine_reachable_forced_liquidation_5_positions(env):
    """引擎可达的穿仓场景（v3.2 必测）：默认参数下 5 票各 ~20% 融资建仓，
    开仓 ratio ≈ 2.01；标的整体下跌跨过 1.30 → forced_liquidation=True。
    """
    eng = make_engine(env, zero_fee=True, zero_rate=True)
    cap = eng.cfg.risk.max_position_pct * float(eng.state.net_assets)

    # 选 5 个「有日线行情 + 两融标的 + 1 手不超单票上限 + 当前不在涨跌停」的标的
    codes: list[str] = []
    for code in daily_codes():
        try:
            info = band(eng, code)
            px = float(info["last"])
        except Exception:
            continue
        if not (px > 0) or info.get("suspended"):
            continue
        if px * 100 > cap:
            continue  # 例如 600519：1 手就超过单票 20% 上限
        if px >= float(info["limit_up"]) or px <= float(info["limit_down"]):
            continue
        if not eng.ds.is_margin_target(code):
            continue
        codes.append(code)
        if len(codes) == eng.cfg.risk.max_positions:
            break
    assert len(codes) == eng.cfg.risk.max_positions, f"可用标的不足: {codes}"

    for code in codes:
        px = last(eng, code)
        qty = round_lot(int(cap / px))
        assert qty >= 100, f"{code} 1 手超过单票上限"
        order = eng.submit(req(code, "margin_buy", qty, px))
        assert order.status == "filled", f"{code}: {order.error_code} {order.reason}"

    st = eng.state
    long_mv = float(st.long_market_value)
    debt = float(st.financing_debt)
    assert debt > 0
    opening_ratio = float(st.maintenance_ratio)
    assert opening_ratio == AP((float(st.available_cash) + long_mv) / debt, abs=1e-9)
    assert opening_ratio > 1.30
    assert float(st.leverage) <= eng.cfg.risk.max_leverage

    # 求解 ratio == 1.30 的统一跌幅系数 f：(avail + long_mv·f) / debt = 1.30
    avail = float(st.available_cash)
    factor = (1.30 * debt - avail) / long_mv
    assert 0 < factor < 1
    assert factor < 0.5, "受集中度约束限制，实盘穿仓需要极端跌幅"

    base_quote = eng.ds.quote
    prices = {c: last(eng, c) for c in codes}

    def patched(scale: float):
        def _quote(code, asof):
            data = base_quote(code, asof)
            if code in prices:
                return {**data, "last": prices[code] * scale}
            return data

        return _quote

    eng.ds.quote = patched(factor)
    roll_next(eng)
    assert eng.state.maintenance_ratio == AP(1.30, abs=1e-6)
    assert eng.state.forced_liquidation is False  # 恰好 130% → 仅追保

    eng.ds.quote = patched(factor * 0.99)
    roll_next(eng)
    assert eng.state.maintenance_ratio < 1.30
    assert eng.state.forced_liquidation is True
    report = eng.risk_report()
    assert report["status"] == "forced"
    assert report["maintenance_ratio"] < 1.30
    assert eng.validate(req(MID, "buy", 100, last(eng, MID))).error_code == (
        ErrorCode.BELOW_MAINTENANCE.value
    )


def test_inf_price_is_rejected_as_no_price(env):
    """v3.2 ⑳㉓：``price=inf`` 必须拒（否则 frozen/现金被 NaN 污染，风控失效）。"""
    eng = make_engine(env, risk_kw=relaxed())
    for bad in (float("inf"), float("-inf"), float("nan")):
        order = eng.validate(req(MID, "buy", 100, bad))
        assert order.status == "rejected"
        assert order.error_code == ErrorCode.NO_PRICE.value
    # 四种方向都要挡住
    for side in ("buy", "sell", "margin_buy", "short_sell", "buy_to_cover", "repay_margin"):
        order = eng.validate(req(MID, side, 100, float("inf")))
        assert order.error_code == ErrorCode.NO_PRICE.value, side
    # 账户不得被污染
    st = eng.state
    assert st.available_cash == 500000.0
    assert st.frozen_t1 == 0.0 and st.frozen_collateral == 0.0
    assert st.financing_debt == 0.0


def test_risk_report_uses_frozen_alert_codes(env):
    """§3.3 冻结告警码 + v3.3 严格 `<` 边界（1.300 → margin_call，跌破才 forced）。"""
    # 1) 无负债 → safe
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    assert eng.risk_report()["status"] == "safe"

    # 2) ratio >= 3.00 → info 级 MAINTENANCE_EXTRACTABLE（只读提示，不影响 status）
    eng = make_engine(env, **ZERO, risk_kw=WIDE)
    px = last(eng, MID)
    qty = round_lot(int(100_000 / px))
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    report = eng.risk_report()
    assert report["maintenance_ratio"] >= 3.0
    codes = {a["code"] for a in report["alerts"]}
    assert "MAINTENANCE_EXTRACTABLE" in codes
    info = next(a for a in report["alerts"] if a["code"] == "MAINTENANCE_EXTRACTABLE")
    assert info["level"] == "info"
    expected = float(eng.state.collateral_value) - 3.0 * float(eng.state.total_debt)
    assert info["extractable"] == AP(round(expected, 2), abs=0.02)
    assert report["status"] == "safe"
    assert report["exposure"]["extractable"] == info["extractable"]

    # 3) ratio 恰好 1.300 → margin_call（MAINTENANCE_WARN，非 FORCED）
    eng = make_engine(
        env, **ZERO, risk_kw=relaxed(max_position_pct=10.0, max_gross_exposure=100.0, max_leverage=2.0)
    )
    px = last(eng, MID)
    qty = round_lot(int(625_000 / px))
    notional = qty * px
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    critical = (1.30 * notional - 500000.0) / qty
    base_quote = eng.ds.quote
    eng.ds.quote = lambda c, a: (
        {**base_quote(c, a), "last": critical} if c == MID else base_quote(c, a)
    )
    roll_next(eng)
    report = eng.risk_report()
    assert report["maintenance_ratio"] == AP(1.30, abs=1e-9)
    codes = {a["code"] for a in report["alerts"]}
    assert "MAINTENANCE_WARN" in codes and "MAINTENANCE_FORCED" not in codes
    assert report["status"] == "margin_call"

    # 4) 跌破 1.30 → forced（critical 级 MAINTENANCE_FORCED）
    eng.ds.quote = lambda c, a: (
        {**base_quote(c, a), "last": critical * 0.99} if c == MID else base_quote(c, a)
    )
    roll_next(eng)
    report = eng.risk_report()
    assert report["maintenance_ratio"] < 1.30
    forced_alert = next(
        (a for a in report["alerts"] if a["code"] == "MAINTENANCE_FORCED"), None
    )
    assert forced_alert is not None and forced_alert["level"] == "critical"
    assert report["status"] == "forced"


def test_inv22_margin_reuse_with_unrealized_profit(env):
    """§3.2.1「融上再融」算例逐格 + 浮盈不入现金（INV-22）。

    默认比例（m_long=0.80、单票 20%、max_leverage=2.0）、零费用零利率：
    融资买入 N≈10 万 → 标的涨 60% → 净资产 50 万 → 56 万，单票上限 10 万 → 11.2 万，
    可再融资额度随浮盈上升，但 ``available_cash`` 一分不动。
    """
    eng = make_engine(env, **ZERO)  # 默认 risk/margin 配置
    px = last(eng, MID)
    cap0 = eng.cfg.risk.max_position_pct * float(eng.state.net_assets)
    qty = round_lot(int(cap0 / px))
    order = eng.submit(req(MID, "margin_buy", qty, px))
    assert order.status == "filled", order.reason
    notional = qty * px
    st = eng.state

    # —— §3.2.1 算例「开仓后」行 ——
    assert st.available_cash == AP(500000.0, abs=1e-6), "融资买入自有资金不出账"
    assert st.financing_debt == AP(notional, abs=1e-6)
    assert st.long_market_value == AP(notional, abs=1e-6)
    assert st.net_assets == AP(500000.0, abs=1e-6), "I1：开仓不产生盈亏"
    assert eng.cfg.risk.max_position_pct * float(st.net_assets) == AP(cap0, abs=1e-6)
    assert st.margin_required == AP(notional * 0.8, abs=1e-6)
    assert st.margin_available == AP(500000.0 - notional * 0.8, abs=1e-6)

    # —— 标的涨 60% ——
    base_quote = eng.ds.quote
    eng.ds.quote = lambda c, a: (
        {**base_quote(c, a), "last": px * 1.6} if c == MID else base_quote(c, a)
    )
    roll_next(eng)
    st = eng.state
    assert st.available_cash == AP(500000.0, abs=1e-6), "规则 2：浮盈不得进入 available_cash"
    assert st.long_market_value == AP(notional * 1.6, abs=1e-6)
    assert st.net_assets == AP(500000.0 + 0.6 * notional, abs=1e-6)
    assert st.margin_required == AP(notional * 0.8, abs=1e-6), "额度占用只由负债决定"
    assert st.margin_available == AP(500000.0 + 0.6 * notional - notional * 0.8, abs=1e-6)

    cap1 = eng.cfg.risk.max_position_pct * float(st.net_assets)
    assert cap1 > cap0, "单票上限必须随浮盈上升（实时 net_assets 派生）"

    # —— 融上再融：用放大后的额度再开一仓（N2 > 原上限 cap0）——
    px2 = last(eng, SMALL)
    qty2 = round_lot(int(cap1 * 0.99 / px2))
    notional2 = qty2 * px2
    assert notional2 > cap0, (notional2, cap0)
    order2 = eng.submit(req(SMALL, "margin_buy", qty2, px2))
    assert order2.status == "filled", f"{order2.error_code}: {order2.reason}"
    st = eng.state
    assert st.available_cash == AP(500000.0, abs=1e-6)
    assert st.financing_debt == AP(notional + notional2, abs=1e-6)
    assert float(st.leverage) <= eng.cfg.risk.max_leverage

    # —— 规则 4：利息按日计提侵蚀净资产 → 反向压缩额度 ——
    eng2 = make_engine(env, zero_fee=True)  # 默认融资利率 6.5%
    px3 = last(eng2, MID)
    qty3 = round_lot(int(cap0 / px3))
    assert eng2.submit(req(MID, "margin_buy", qty3, px3)).status == "filled"
    available_before = float(eng2.state.margin_available)
    roll_next(eng2)
    interest = float(eng2.state.interest_accrued)
    assert interest > 0
    pnl = float(eng2.state.positions[MID].long_pnl)
    # 净资产 = 本金 + 浮动盈亏 − 累计利息（跨日时 mark-to-market 与计息同时发生）
    assert float(eng2.state.net_assets) == AP(500000.0 + pnl - interest, abs=1e-6)
    assert float(eng2.state.margin_available) == AP(
        float(eng2.state.net_assets) - float(eng2.state.margin_required), abs=1e-9
    )
    # 规则 4：利息反向压缩额度（低于「只算浮盈」的口径）
    assert float(eng2.state.margin_available) < available_before + pnl
    assert eng2.state.available_cash == AP(500000.0, abs=1e-6)


def test_v31_crash_60pct_gives_ratio_120(env):
    """跌 60% → ratio 1.20 → 强制平仓（数值逐格断言）。"""
    eng = make_engine(
        env,
        zero_fee=True,
        zero_rate=True,
        risk_kw=relaxed(max_position_pct=10.0, max_gross_exposure=100.0, max_leverage=2.0),
    )
    px = last(eng, MID)
    qty = round_lot(int(625_000 / px))
    notional = qty * px
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    eng.state = mark_to_market(eng.state, {MID: px * 0.4})
    st = eng.state
    assert st.long_market_value == AP(notional * 0.4, abs=1e-6)
    assert st.maintenance_ratio == AP((500000.0 + notional * 0.4) / notional, abs=1e-9)
    eng._refresh_risk_flags(eng.state)
    assert st.forced_liquidation is True
    assert eng.risk_report()["status"] == "forced"


def test_margin_space_limits_margin_buy_under_defaults(env):
    """默认参数下，融资买入受「开仓额度 N×m_long ≤ 净资产」约束。"""
    eng = make_engine(env, zero_fee=True, risk_kw=relaxed(max_position_pct=100.0, max_gross_exposure=100.0))
    px = last(eng, MID)
    ok_qty = round_lot(int(0.95 * 500000.0 / eng.cfg.margin.margin_ratio_long / px))
    assert eng.submit(req(MID, "margin_buy", ok_qty, px)).status == "filled"
    eng2 = make_engine(env, zero_fee=True, risk_kw=relaxed(max_position_pct=100.0, max_gross_exposure=100.0))
    bad_qty = round_lot(int(1.5 * 500000.0 / eng2.cfg.margin.margin_ratio_long / px))
    order = eng2.submit(req(MID, "margin_buy", bad_qty, px))
    assert order.error_code == ErrorCode.INSUFFICIENT_MARGIN_SPACE.value


# =========================================================================== #
# 11. 仓位 / 敞口 / 券源
# =========================================================================== #
def test_position_limit_pct(env):
    eng = make_engine(env, risk_kw=relaxed(max_position_pct=0.2))
    px = last(eng, MID)
    qty = round_lot(int(0.5 * float(eng.state.net_assets) / px))
    order = eng.submit(req(MID, "buy", max(qty, 100), px))
    assert order.error_code == ErrorCode.POSITION_LIMIT_PCT.value


def test_position_limit_count(env):
    eng = make_engine(env, risk_kw=relaxed(max_positions=1))
    assert eng.submit(req(MID, "buy", 100, last(eng, MID))).status == "filled"
    order = eng.submit(req(SMALL, "buy", 100, last(eng, SMALL)))
    assert order.error_code == ErrorCode.POSITION_LIMIT_COUNT.value


def test_exposure_limit_gross(env):
    eng = make_engine(
        env, risk_kw=relaxed(max_position_pct=1.0, max_gross_exposure=0.5, max_leverage=10.0)
    )
    px = last(eng, MID)
    first = round_lot(int(200000 / px))
    assert eng.submit(req(MID, "margin_buy", first, px)).status == "filled"
    # 第二票把总敞口推过 0.5 × 净资产
    spx = last(eng, SMALL)
    big_qty = round_lot(int(300000 / spx))
    order = eng.submit(req(SMALL, "margin_buy", big_qty, spx))
    assert order.error_code == ErrorCode.EXPOSURE_LIMIT.value


def test_short_exposure_limit(env):
    eng = make_engine(env, risk_kw=relaxed(max_position_pct=1.0, max_short_exposure=0.3))
    px = last(eng, SMALL)
    qty = round_lot(int(0.4 * float(eng.state.net_assets) / px))
    order = eng.submit(req(SMALL, "short_sell", max(qty, 100), px))
    assert order.error_code == ErrorCode.EXPOSURE_LIMIT.value


def test_short_source_limit(env):
    eng = make_engine(
        env,
        margin_kw={"short_source_limit_ratio": one_share_ratio(env, SMALL)},
        risk_kw=relaxed(max_position_pct=10.0, max_gross_exposure=100.0),
    )
    order = eng.submit(req(SMALL, "short_sell", 100, last(eng, SMALL)))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.SHORT_SOURCE_LIMIT.value


# =========================================================================== #
# 12. position_sizing / build_plan / close_position / risk_report
# =========================================================================== #
def test_position_sizing_score_gate(env):
    eng = make_engine(env, risk_kw=relaxed())
    below = eng.position_sizing(MID, last(eng, MID), eng.cfg.canslim.buy_threshold - 1)
    assert below["qty"] == 0 and below["binding_constraint"] == "score"
    ok = eng.position_sizing(MID, last(eng, MID), eng.cfg.canslim.buy_threshold + 1)
    assert ok["qty"] > 0 and ok["qty"] % 100 == 0


def test_position_sizing_pct_and_cash_bindings(env):
    eng = make_engine(env, risk_kw=relaxed(max_position_pct=0.2))
    px = last(eng, MID)
    pct = eng.position_sizing(MID, px, 90.0)
    assert pct["binding_constraint"] == "position_pct"
    assert pct["amount"] <= 0.2 * float(eng.state.net_assets) + 1.0

    wide = make_engine(env, risk_kw=relaxed(max_position_pct=2.0, max_leverage=10.0))
    cash = wide.position_sizing(MID, px, 90.0)
    assert cash["binding_constraint"] == "cash"
    assert cash["amount"] <= float(wide.state.available_cash)


def test_position_sizing_count_binding(env):
    eng = make_engine(env, risk_kw=relaxed(max_positions=0))
    assert eng.position_sizing(MID, last(eng, MID), 90.0)["binding_constraint"] == "position_count"


def test_position_sizing_is_executable(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    sizing = eng.position_sizing(MID, px, 90.0)
    assert set(sizing) == {"qty", "amount", "margin_required", "binding_constraint", "reason"}
    order = eng.submit(req(MID, "buy", sizing["qty"], px))
    assert order.status == "filled", f"{sizing} -> {order.error_code}: {order.reason}"


def test_build_plan_structure(env):
    eng = make_engine(env, risk_kw=relaxed())
    # 夹具的两融名单把 601398 标为 can_short=True（§4 的股票负例缺失）→ 打桩构造 skip 场景
    eng.ds.is_short_target = lambda code, asof=None: code != FIN_ONLY
    plan = eng.build_plan(
        [
            {"code": MID, "name": "中性", "action": "open_long", "side": "long",
             "score": 80.0, "entry": last(eng, MID), "reasons": ["总分达标"]},
            {"code": BIG, "name": "强票", "action": "watch", "side": "none",
             "score": 60.0, "reasons": ["观望"]},
            {"code": FIN_ONLY, "name": "融资标的", "action": "open_short", "side": "short",
             "score": 20.0, "entry": last(eng, FIN_ONLY), "reasons": ["弱"]},
        ]
    )
    assert set(plan) == {"generated_for", "actions", "skipped", "notes"}
    assert plan["generated_for"] == eng.state.trade_date
    acts = {a["action"] for a in plan["actions"]}
    assert "open_long" in acts and "watch" in acts
    for action in plan["actions"]:
        assert set(action) == {
            "code", "name", "action", "side", "qty", "price",
            "amount", "margin_required", "reason", "score",
        }
    assert any(s["code"] == FIN_ONLY for s in plan["skipped"]), "非融券标的不得进 open_short"


def test_build_plan_close_with_side_none_not_swallowed(env):
    """回归：action='close' 且 side='none' 必须进 close 分支，不能被当成 watch。"""
    eng = make_engine(env, risk_kw=relaxed())
    qty = 100
    settle_long(eng, MID, qty)
    plan = eng.build_plan(
        [{"code": MID, "name": "中性", "action": "close", "side": "none",
          "score": 30.0, "reasons": ["止损"]}]
    )
    assert [a["action"] for a in plan["actions"]] == ["close_long"]
    assert plan["skipped"] == []


def test_build_plan_close_without_position_is_skipped(env):
    eng = make_engine(env, risk_kw=relaxed())
    plan = eng.build_plan(
        [{"code": MID, "name": "中性", "action": "close", "side": "none", "score": 30.0}]
    )
    assert plan["actions"] == []
    assert plan["skipped"][0]["code"] == MID


def test_close_position_long(env):
    eng = make_engine(env, risk_kw=relaxed())
    qty = 300
    settle_long(eng, MID, qty)
    orders = eng.close_position(MID, eng.state.trade_date)
    assert len(orders) == 1 and orders[0].status == "filled", orders
    assert eng.state.positions[MID].long_qty == 0


def test_close_position_short(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, SMALL)
    qty = buy_qty(eng, SMALL, 0.1)
    assert eng.submit(req(SMALL, "short_sell", qty, px)).status == "filled"
    roll_next(eng)
    orders = eng.close_position(SMALL, eng.state.trade_date)
    assert len(orders) == 1 and orders[0].status == "filled", orders
    assert orders[0].side == Side.BUY_TO_COVER.value
    assert eng.state.positions[SMALL].short_qty == 0


def test_close_position_uses_repay_margin_when_indebted(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    roll_next(eng)
    orders = eng.close_position(MID, eng.state.trade_date)
    assert orders and orders[0].status == "filled"
    assert orders[0].side == Side.REPAY_MARGIN.value


def test_close_position_ratio_zero_is_noop(env):
    eng = make_engine(env, risk_kw=relaxed())
    settle_long(eng, MID, 100)
    assert eng.close_position(MID, eng.state.trade_date, ratio=0.0) == []


def test_close_position_unknown_code_not_found(env):
    eng = make_engine(env, risk_kw=relaxed())
    orders = eng.close_position(MID, eng.state.trade_date)
    assert len(orders) == 1
    assert orders[0].error_code == ErrorCode.NOT_FOUND.value


def test_risk_report_structure_and_safe(env):
    eng = make_engine(env, risk_kw=relaxed())
    report = eng.risk_report()
    assert set(["maintenance_ratio", "status", "alerts", "exposure", "positions"]) <= set(report)
    assert report["status"] == "safe"
    # CR-9：无负债 → 维持担保比例为无穷 → 序列化为 None（绝不输出裸 Infinity）
    assert report["maintenance_ratio"] is None
    assert report["exposure"]["has_debt"] is False
    for key in (
        "net_assets", "total_assets", "total_debt", "collateral_value", "leverage",
        "long_market_value", "short_market_value", "gross_exposure", "net_exposure",
        "available_cash", "frozen_cash", "frozen_t1", "frozen_collateral",
        "financing_debt", "interest_accrued", "margin_required", "margin_available",
        "max_gross_exposure", "max_short_exposure", "max_position_pct", "max_positions",
        "max_leverage", "has_debt",
    ):
        assert key in report["exposure"], key


def test_risk_report_is_strict_json(env):
    """CR-9：``risk_report()`` 必须可严格 JSON 序列化（无裸 NaN/Infinity）。

    无负债（比例无穷）与有杠杆两种情形都要过。
    """
    fresh = make_engine(env, risk_kw=relaxed())
    text = json.dumps(fresh.risk_report(), ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["maintenance_ratio"] is None

    eng = make_engine(env, **ZERO, risk_kw=relaxed())
    assert eng.submit(req(MID, "margin_buy", 1000, last(eng, MID))).status == "filled"
    report = eng.risk_report()
    assert report["exposure"]["has_debt"] is True
    assert report["maintenance_ratio"] is not None
    text = json.dumps(report, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text


def test_risk_report_position_detail(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    assert eng.submit(req(MID, "buy", qty, px)).status == "filled"
    report = eng.risk_report()
    assert len(report["positions"]) == 1
    row = report["positions"][0]
    assert row["code"] == MID and row["long_qty"] == qty
    assert row["long_value"] == AP(px * qty, abs=1e-2)
    assert row["stop_loss_triggered"] is False


# =========================================================================== #
# 13. roll_to / submit 的边界与副作用
# =========================================================================== #
def test_roll_to_rejects_earlier_date(env):
    eng = make_engine(env)
    earlier = eng.ds.prev_trade_date(eng.state.trade_date)
    with pytest.raises(ValueError):
        eng.roll_to(earlier)


def test_roll_to_rejects_non_trade_date(env):
    eng = make_engine(env)
    dates = set(eng.ds.trade_dates())
    from datetime import date, timedelta

    cursor = date.fromisoformat(eng.ds.next_trade_date(eng.state.trade_date))
    gap = None
    for _ in range(40):
        cursor = cursor + timedelta(days=1)
        if cursor.isoformat() not in dates:
            gap = cursor.isoformat()
            break
    if gap is None:
        pytest.skip("夹具交易日连续 40 天，无法构造非交易日")
    with pytest.raises(ValueError):
        eng.roll_to(gap)


def test_roll_to_rejects_empty(env):
    eng = make_engine(env)
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            eng.roll_to(bad)


def test_roll_to_same_date_is_idempotent(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    assert eng.submit(req(MID, "margin_buy", 100, px)).status == "filled"
    before = (eng.state.interest_accrued, eng.state.available_cash, eng.state.trade_date)
    returned = eng.roll_to(eng.state.trade_date)
    assert returned is eng.state
    assert (eng.state.interest_accrued, eng.state.available_cash, eng.state.trade_date) == before
    # 再跨日：只计息一次
    roll_next(eng)
    once = eng.state.interest_accrued
    eng.roll_to(eng.state.trade_date)
    assert eng.state.interest_accrued == once


def test_roll_to_accrues_interest_per_trading_day(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    qty = buy_qty(eng, MID, 0.3)
    assert eng.submit(req(MID, "margin_buy", qty, px)).status == "filled"
    debt = eng.state.financing_debt
    dates = eng.ds.trade_dates()
    idx = dates.index(eng.state.trade_date)
    target = dates[idx + 2]
    eng.roll_to(target)
    expected = debt * eng.cfg.margin.financing_rate_annual / 365.0 * 2
    assert eng.state.interest_accrued == AP(expected, abs=1e-9)


def test_roll_to_marks_to_market(env):
    eng = make_engine(env, risk_kw=relaxed())
    qty = buy_qty(eng, MID, 0.3)
    assert eng.submit(req(MID, "buy", qty, last(eng, MID))).status == "filled"
    roll_next(eng)
    assert eng.state.positions[MID].last_price == AP(last(eng, MID), abs=1e-9)


def test_validate_does_not_mutate_state(env):
    eng = make_engine(env, risk_kw=relaxed())
    eng.submit(req(MID, "buy", 100, last(eng, MID)))
    st = eng.state
    snapshot = (
        st.available_cash,
        st.frozen_t1,
        st.frozen_collateral,
        st.financing_debt,
        st.interest_accrued,
        len(st.history),
        dict(st.short_qty_debt),
        {c: (p.long_qty, p.long_available, p.short_qty) for c, p in st.positions.items()},
    )
    for _ in range(3):
        eng.validate(req(MID, "buy", 100, last(eng, MID)))
        eng.validate(req(MID, "sell", 100, last(eng, MID)))
        eng.validate(req(SMALL, "short_sell", 100, last(eng, SMALL)))
    after = (
        st.available_cash,
        st.frozen_t1,
        st.frozen_collateral,
        st.financing_debt,
        st.interest_accrued,
        len(st.history),
        dict(st.short_qty_debt),
        {c: (p.long_qty, p.long_available, p.short_qty) for c, p in st.positions.items()},
    )
    assert snapshot == after


def test_submit_appends_history_and_ledger(env):
    eng = make_engine(env, risk_kw=relaxed())
    px = last(eng, MID)
    first = eng.submit(req(MID, "buy", 100, px))
    second = eng.submit(req(MID, "sell", 100, px))  # 拒单也应留痕
    assert first.order_id == "O0001" and second.order_id == "O0002"
    assert [o.order_id for o in eng.state.history] == ["O0001", "O0002"]
    assert second.status == "rejected"

    ledger = env.root / "data" / "ledger.jsonl"
    assert ledger.is_file()
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 2
    assert rows[0]["code"] == MID
    assert rows[0]["side"] == "buy", "side 必须是契约值 'buy'，不是 'Side.BUY'"
    assert rows[0]["error_code"] == "OK"
    assert rows[1]["status"] == "rejected"
    assert set(rows[0]) == {
        "order_id", "ts", "trade_date", "code", "name", "side", "qty", "price",
        "status", "error_code", "reason", "fee", "tax", "transfer_fee",
        "cash_delta", "margin_used", "debt_delta", "realized_pnl",
    }


def test_one_lot_above_position_cap_is_rejected(env):
    """边界：一手即超过单票 20% 上限（例如 600519，50 万账户买不起一手）→ POSITION_LIMIT_PCT。

    这是真实约束的正确体现，不是缺陷：`max_position_pct × net_assets` 先于资金约束生效。
    """
    eng = make_engine(env)  # 默认 max_position_pct = 0.20
    px = last(eng, BIG)
    cap = eng.cfg.risk.max_position_pct * float(eng.state.net_assets)
    if px * 100 <= cap:
        pytest.skip(f"夹具里 {BIG} 一手 {px * 100:.0f} 元未超过上限 {cap:.0f} 元")
    order = eng.submit(req(BIG, "buy", 100, px))
    assert order.status == "rejected"
    assert order.error_code == ErrorCode.POSITION_LIMIT_PCT.value
    assert order.reason  # 中文原因可读
    # 拒单不得产生任何资金/持仓变动
    assert eng.state.available_cash == 500000.0
    assert eng.state.positions == {}
    assert eng.state.history[-1].status == "rejected"


def test_rejected_order_does_not_touch_money(env):
    eng = make_engine(env)
    px = last(eng, MID)
    before = (eng.state.available_cash, eng.state.frozen_collateral, eng.state.financing_debt)
    order = eng.submit(req(MID, "buy", 100_000_000 // 100 * 100, px))
    assert order.status == "rejected"
    after = (eng.state.available_cash, eng.state.frozen_collateral, eng.state.financing_debt)
    assert before == after


# =========================================================================== #
# 14. 与 T1 conftest 夹具的互操作（INTERFACES §8）
# =========================================================================== #
def test_conftest_engine_offline_interop(engine_offline):
    """T1 的 ``engine_offline``（600519 已建仓且已结算）必须能直接驱动 T2 的引擎。"""
    eng = engine_offline
    assert isinstance(eng, Engine)
    st = eng.state
    assert "600519" in st.positions
    pos = st.positions["600519"]
    assert pos.long_qty > 0 and pos.long_available == pos.long_qty, "conftest 夹具应为已结算持仓"
    px = float(eng.ds.quote("600519", st.trade_date)["last"])
    order = eng.submit(OrderRequest("600519", "sell", min(pos.long_qty, 100), px))
    assert order.status == "filled", f"{order.error_code}: {order.reason}"
    assert order.side == Side.SELL.value
    report = eng.risk_report()
    assert report["status"] in {"safe", "warn", "margin_call", "forced"}
    assert {"maintenance_ratio", "status", "alerts", "exposure", "positions"} <= set(report)
