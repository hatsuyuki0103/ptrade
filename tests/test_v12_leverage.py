"""v12 判据：融资/融券保证金 50%（2 倍杠杆）+ 风控上限放宽。

真源：`.omx/specs/deep-interview-ptrade-v12.md` · `.omx/plans/prd-ptrade-v12.md`

## 用户原话
「我的盈利是不是不对。因为我让你做空是**加了杠杆**的，现在 **300869**，
 从我买的时候 **9.81** 到现在 **8.92**，我应该赚了 **9000 多**才对吧？」

## ★★ 根因（v12 复审 S1 纠正 —— 原来的说法是错的）
**错的说法**：「`margin_ratio_short = 1.00` ⇒ 杠杆 1 倍 ⇒ 盈利少一半」。

**正确的事实**：本仓融券模型里 `margin_ratio_short` **不放大盈亏**。
盈亏恒为 `(short_cost − last_price) × short_qty` —— **只由股数决定**，
`m_short` 只影响两处（`core.py:660-669` / `core.py:353-365`）：
* `margin_used = amount × m_short`（**记录字段**，不入账）
* `margin_required` 里融券部分 `max(0, Σ short_mv × m_short − frozen_collateral)`（本账户恒为 0）

⇒ 真正让用户从 **4,539 → 9,078** 的是**迁移把股数翻倍**（5,100 → 10,200），
   **不是**改 `margin_ratio_short`（改它只是把 `margin_used` 记录口径对齐真实市场）。

## 用户裁决
融资/融券保证金**都改 50%**（口径对齐真实 A 股 + 用户明确要求）·
现有三笔**重放为 2 倍规模**（这才是盈利翻倍的原因）
"""

from __future__ import annotations

import dataclasses
import inspect

from ptrade import core


def _cfg():
    from ptrade.config import AppConfig, load_config

    return load_config(str(__import__("pathlib").Path(__file__).resolve().parent.parent))


# ══════════════════════════════════════════════════════════════════════ #
# 配置口径（V12-1 … V12-4）
# ══════════════════════════════════════════════════════════════════════ #
def test_v12_1_margin_ratio_long_is_half():
    """★ V12-1：融资保证金 0.80 → **0.50**（2 倍杠杆）。"""
    assert _cfg().margin.margin_ratio_long == 0.50


def test_v12_2_margin_ratio_short_is_half():
    """★ V12-2：融券保证金 1.00 → **0.50**（口径对齐真实 A 股 2 倍）。

    ★ 复审核正：这**不是**用户盈利偏少的根因 —— `margin_ratio_short` 不参与盈亏计算
      （见文件头「根因」段）。改它只是让 `margin_used` 记录口径接近真实券商。
    """
    assert _cfg().margin.margin_ratio_short == 0.50


def test_v12_3_max_short_exposure_raised():
    """★ V12-3：空头暴露上限 0.50 → **0.65**（2 倍仓位后合计 ~59%，旧上限会拦）。"""
    assert _cfg().risk.max_short_exposure == 0.65


def test_v12_4_max_position_pct_raised():
    """★ V12-4：单票上限 0.20 → **0.25**（2 倍仓位后单票 ~10 万，旧上限 10 万差 62 元被拒）。"""
    assert _cfg().risk.max_position_pct == 0.25


def test_v12_4b_leverage_caps_untouched():
    """V12-4b：`max_leverage` / `min_maintenance_ratio` **不许**顺手改（用户没要求）。"""
    cfg = _cfg()
    assert cfg.risk.max_leverage == 2.0
    assert cfg.margin.min_maintenance_ratio == 1.30


# ══════════════════════════════════════════════════════════════════════ #
# 行为：50% 保证金 ⇒ 5 万保证金可控制 10 万名义（V12-5 / V12-6）
# ══════════════════════════════════════════════════════════════════════ #
def _short_plan(cfg, qty: int, price: float):
    """用**纯函数** `core.fill_plan` 算一笔融券卖出的记账影响（无需 DS/锁）。"""
    st = core.new_state(cfg)
    order = core.Order(
        code="300869", name="康泰医学", side=core.Side.SHORT_SELL.value,
        qty=qty, price=price, trade_date="2026-11-09",
        status="filled", error_code=core.ErrorCode.OK.value,
    )
    return st, order, core.fill_plan(st, order, cfg.margin, cfg.fee)


def test_v12_5_half_margin_gives_2x_notional():
    """★ V12-5（行为）：`margin_used == 名义金额 × 0.50`（保证金记录口径）。

    ★ 复审核正：**这不是用户「9,000 多」的来源**。用户盈利翻倍来自**股数翻倍**
      （5,100 → 10,200），不是保证金比例 —— 盈亏 = (成本−现价)×股数，与 m_short 无关。
      这条只钉住「`margin_used` 确实按配置比例计算」，防止字段被写死。
    """
    cfg = _cfg()
    qty, px = 10200, 9.81
    notional = qty * px
    _, _, plan = _short_plan(cfg, qty, px)
    assert plan["margin_used"] == notional * 0.50, (
        f"保证金={plan['margin_used']} · 名义={notional} ⇒ 比例 {plan['margin_used']/notional:.2f}"
    )
    # ★ 券款全额冻结（不是保证金）—— 融券的「券款即担保物」口径不变
    assert plan["frozen_collateral_delta"] == notional
    assert plan["debt_delta"] == notional


def test_v12_6_old_ratio_would_require_full_notional():
    """★ V12-6（反证）：旧口径 `m_short = 1.0` 下，保证金 = 名义金额 ⇒ **1 倍，无杠杆**。

    ★ 这条钉住「比例真的参与计算」，防止有人把 `margin_used` 写死或忽略配置。
    """
    cfg = _cfg()
    old = dataclasses.replace(cfg, margin=dataclasses.replace(cfg.margin, margin_ratio_short=1.00))
    qty, px = 10200, 9.81
    notional = qty * px
    st_old = core.new_state(old)
    order_old = core.Order(
        code="300869", name="康泰医学", side=core.Side.SHORT_SELL.value,
        qty=qty, price=px, trade_date="2026-11-09",
        status="filled", error_code=core.ErrorCode.OK.value,
    )
    plan_old = core.fill_plan(st_old, order_old, old.margin, old.fee)
    assert plan_old["margin_used"] == notional * 1.00
    assert plan_old["margin_used"] == 2 * plan_old["frozen_collateral_delta"] / 2  # 名义 == 保证金


# ══════════════════════════════════════════════════════════════════════ #
# 账户实际状态（V12-7 / V12-8）—— 这是用户真正会看的数字
# ══════════════════════════════════════════════════════════════════════ #
_MIGRATED_QTY = {"600594": 31400, "300869": 10200, "601238": 20400}


def _account_snapshot_or_skip():
    """读真实账户；若已不是「迁移后那一刻」的状态，**skip 并说明原因**。

    ★ 复审 S3：原来这两条判据写死 `short_qty == 31400` / `ledger == 6`，
      用户**下一次正常交易或结算后必然变红** ⇒ 会把「全量测试门禁」搞坏 ✗
      这两条本质是**一次性数据审计**（迁移是否做对），不是回归判据
      ⇒ 账户变了就 skip（附原因），回归保护交给 test_v12_1..6（纯配置/纯函数）。
    """
    import pathlib

    import pytest

    from ptrade import store
    from ptrade.config import load_config

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = load_config(str(root))
    st = store.read_state(cfg)
    if st is None:
        pytest.skip("没有账户（干净环境）—— 这条是数据审计，不是回归判据")
    actual = {c: int(p.short_qty) for c, p in st.positions.items()}
    if actual != _MIGRATED_QTY:
        pytest.skip(
            f"账户已变化（当前 {actual}）—— v12 迁移审计只在迁移后那一刻有意义；"
            "回归保护由 test_v12_1..6 承担"
        )
    return cfg, st, root


def test_v12_7_account_positions_doubled():
    """★ V12-7（**数据审计**，账户变化时 skip）：三笔空头已重放为 2 倍。

    期望（迁移当时，现价 2.95 / 8.92 / 4.67）：
      600594: 31,400 @3.19 ⇒ +7,536
      300869: 10,200 @9.81 ⇒ **+9,078**  ← 用户预期的「9,000 多」
      601238: 20,400 @4.92 ⇒ +5,100
    """
    cfg, st, _ = _account_snapshot_or_skip()

    # 成本价不变（= 原成交价）
    assert abs(float(st.positions["600594"].short_cost) - 3.19) < 1e-9
    assert abs(float(st.positions["300869"].short_cost) - 9.81) < 1e-9
    assert abs(float(st.positions["601238"].short_cost) - 4.92) < 1e-9


def test_v12_8_ledger_consistent_and_risk_ok():
    """★ V12-8（**数据审计**，账户变化时 skip）：账实一致（N7）+ 风控全部在线内。

    重放后 `ledger` 应有 6 行（原 3 + 加开 3）。
    """
    cfg, st, root = _account_snapshot_or_skip()

    n = len([
        ln for ln in (root / "data" / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()
    ])
    assert n == 6, f"ledger 应为 6 行（3 原 + 3 加开），实际 {n}"

    assert st.maintenance_ratio >= cfg.margin.min_maintenance_ratio, (
        f"担保比例 {st.maintenance_ratio} 低于平仓线 {cfg.margin.min_maintenance_ratio}"
    )
    assert st.leverage <= cfg.risk.max_leverage
    assert st.short_market_value <= st.net_assets * cfg.risk.max_short_exposure + 1.0
    for pos in st.positions.values():
        notional = pos.short_qty * pos.short_cost
        assert notional <= st.net_assets * cfg.risk.max_position_pct + 1.0
    assert st.margin_call is False
    assert st.forced_liquidation is False
