"""v11 判据：模拟盘「交易日」自动推进到当天。

真源：`.omx/plans/test-spec-ptrade-v11.md`（V11-1 … V11-17）

## 修的是什么（用户原话）
> 「为什么现在 还是 交易日 **2026-11-09**。现在已经是 **2026年11月19日12:08:35** 了」

根因：`engine.roll_to()` 是**唯一**推进 `trade_date` 的入口，全仓**没有自动推进逻辑** ✗
"""

from __future__ import annotations

import inspect
import json
import pathlib
from datetime import datetime

import pytest

from ptrade import core, service, store
from ptrade.config import load_config
from ptrade.datasource import DataUnavailable, NetworkForbiddenInLock

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 覆盖 11-09 … 11-19 的交易日（周一~周五；11-14/11-15 周末不入表）
CAL = [
    "2026-11-06",
    "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
    "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20",
]


class _FakeDS:
    """最小 DS 桩：只实现 `auto_settle` 真的会用到的方法。

    ★ `__getattr__` 兜底：`prewarm_before_write` 会调一堆 DS 方法，
      这里统统返回 benign 空实现（**不让测试因为缺方法而假红**）
    """

    offline = False

    def __init__(self, cal=None, prices=None, fail_is_trade_date=False):
        self.cal = list(cal if cal is not None else CAL)
        self.prices = dict(prices or {"600594": 3.20})
        self.fail_is_trade_date = fail_is_trade_date
        self.calls: list[str] = []

    def is_trade_date(self, d):
        self.calls.append(f"is_trade_date({d})")
        if self.fail_is_trade_date:
            raise OSError("boom")
        return str(d)[:10] in set(self.cal)

    def prev_trade_date(self, d):
        earlier = [x for x in self.cal if x < str(d)[:10]]
        if not earlier:
            raise DataUnavailable(f"日历里 {d} 之前没有交易日")
        return earlier[-1]

    def trade_dates(self):
        return list(self.cal)

    def quote(self, code, trade_date=None, **kw):
        px = self.prices.get(code)
        return {"last": px, "name": code} if px is not None else None

    def last_close(self, code, trade_date=None, **kw):
        return self.prices.get(code)

    def __getattr__(self, name):
        def _benign(*a, **k):
            return None

        return _benign


def _root_with_account(tmp_path, trade_date="2026-11-09"):
    """建一个临时 root + 一个空头持仓账户（**绝不碰用户 data/**）。"""
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True, exist_ok=True)
    cfg = load_config(str(root))
    st = core.new_state(cfg)
    st.trade_date = trade_date
    st.initial_cash = 500000.0
    st.available_cash = 499961.96
    st.frozen_collateral = 50000.0
    pos = core.Position(code="600594", name="益佰制药")
    pos.short_qty = 15700
    pos.short_cost = 3.19
    pos.last_price = 3.19
    st.positions = {"600594": pos}
    store.save_state(cfg, st, source="v11-test")
    return cfg, root


# ══════════════════════════════════════════════════════════════════════ #
# R1 · account_target_trade_date
# ══════════════════════════════════════════════════════════════════════ #
def test_v11_1_target_is_today_when_today_is_trade_date():
    """★ V11-1：今天是交易日 ⇒ 返回**今天**（盘中也是今天，**不是昨天**）。

    ★★ 这是与 `latest_trade_date()` 的**关键语义差异**：
       后者返回"最近已收盘交易日"，盘中会回退到昨天（v3.1 未来函数防护）。
       但**账户的交易日**盘中就该是今天 —— 今天下的单 `trade_date` 也是今天。
       （用户 11-19 12:08 看到账户停在 11-09，正是缺了这一环）
    """
    ds = _FakeDS()
    got = service.account_target_trade_date(ds, now=datetime(2026, 11, 19, 12, 8))
    assert got == "2026-11-19", f"应返回今天，实际 {got}"


def test_v11_2_target_is_prev_trade_date_on_weekend():
    """V11-2：今天不是交易日（周六/周日）⇒ 返回之前最近的交易日。"""
    ds = _FakeDS()
    assert service.account_target_trade_date(ds, now=datetime(2026, 11, 21, 10)) == "2026-11-20"
    assert service.account_target_trade_date(ds, now=datetime(2026, 11, 22, 10)) == "2026-11-20"


def test_v11_3_target_none_when_calendar_unavailable():
    """V11-3：日历拿不到 ⇒ `None`（**不猜**）。"""
    ds = _FakeDS(cal=[])
    ds.fail_is_trade_date = True
    assert service.account_target_trade_date(ds, now=datetime(2026, 11, 19, 12, 0)) is None


def test_v11_4_target_never_exceeds_today():
    """★ V11-4：**永不 > 今天** —— 日历里塞未来日期也返回 ≤ 今天。"""
    ds = _FakeDS(cal=CAL + ["2026-12-31", "2027-01-04"])
    for now in (datetime(2026, 11, 19, 9, 0), datetime(2026, 11, 19, 16, 0)):
        got = service.account_target_trade_date(ds, now=now)
        assert got is not None and got <= "2026-11-19", f"{now} ⇒ {got} 超过今天"


def test_v11_5_latest_trade_date_semantics_unchanged():
    """★ V11-5：**不得**改 `latest_trade_date()` 的语义（防"顺手把未来函数防护改掉"）。

    真 DS 盘中调用必须仍返回**昨天**（11-18），不是今天。
    """
    from ptrade.datasource import DataSource

    src = inspect.getsource(DataSource.latest_trade_date)
    # 保留盘中回退逻辑
    assert "15 * 60 + 30" in src, "latest_trade_date 的 15:30 盘中回退被删了 ✗"
    assert "earlier[-1]" in src, "latest_trade_date 的盘中回退被改 ✗"
    # 且它**不是**我们新加的那个函数（语义必须分开）
    assert "account_target_trade_date" not in src


# ══════════════════════════════════════════════════════════════════════ #
# R2 · auto_settle_to_today
# ══════════════════════════════════════════════════════════════════════ #
def test_v11_6_advances_account_to_target(tmp_path):
    """★ V11-6：state 在 11-09 + target 11-19 ⇒ `trade_date` **真的**变 11-19。"""
    cfg, _ = _root_with_account(tmp_path, "2026-11-09")
    ds = _FakeDS()
    out = service.auto_settle_to_today(cfg, ds, now=datetime(2026, 11, 19, 12, 8))
    assert out["error"] is None, out
    assert out["advanced"] is True
    assert out["from"] == "2026-11-09" and out["to"] == "2026-11-19"
    assert store.read_state(cfg).trade_date == "2026-11-19", "账户没真的推进 ✗"


def test_v11_7_idempotent_no_double_interest(tmp_path):
    """★ V11-7：**幂等** —— 连跑两次，第二次 `advanced=False`，**利息不重复计**。"""
    cfg, _ = _root_with_account(tmp_path, "2026-11-09")
    ds = _FakeDS()
    first = service.auto_settle_to_today(cfg, ds, now=datetime(2026, 11, 19, 12, 8))
    interest1 = store.read_state(cfg).interest_accrued
    second = service.auto_settle_to_today(cfg, ds, now=datetime(2026, 11, 19, 12, 30))
    interest2 = store.read_state(cfg).interest_accrued
    assert first["advanced"] is True
    assert second["advanced"] is False, "幂等失败：第二次又推进了 ✗"
    assert float(interest1) == float(interest2), f"利息被重复计了：{interest1} → {interest2} ✗"
    assert store.read_state(cfg).trade_date == "2026-11-19"


def test_v11_8_fast_path_does_not_take_write_lock(tmp_path, monkeypatch):
    """★ V11-8：已同步时走**快路径** —— `locked_update_split` **一次都不被调用**。

    ★ 页面每次打开都会调这个端点 ⇒ 快路径是性能关键（不能每次都抢写锁）。
    """
    cfg, _ = _root_with_account(tmp_path, "2026-11-19")
    calls: list = []
    real = store.locked_update_split

    def _spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(store, "locked_update_split", _spy)
    out = service.auto_settle_to_today(cfg, _FakeDS(), now=datetime(2026, 11, 19, 12, 8))
    assert out["advanced"] is False
    assert calls == [], f"快路径仍然取了写锁 {len(calls)} 次 ✗"


def test_v11_9_days_counts_trade_days(tmp_path):
    """V11-9：`days` = 交易日历里 `(from, to]` 的天数（11-09→11-19 = **8**）。"""
    cfg, _ = _root_with_account(tmp_path, "2026-11-09")
    out = service.auto_settle_to_today(cfg, _FakeDS(), now=datetime(2026, 11, 19, 12, 8))
    assert out["days"] == 8, f"day 数算错：{out['days']}（应为 8）"


def test_v11_10_target_unavailable_returns_error_not_raise(tmp_path):
    """★ V11-10：target 取不到 ⇒ `advanced=False` + `error` 非空，**不抛**。"""
    cfg, _ = _root_with_account(tmp_path, "2026-11-09")
    ds = _FakeDS(cal=[])
    ds.fail_is_trade_date = True
    out = service.auto_settle_to_today(cfg, ds, now=datetime(2026, 11, 19, 12, 0))
    assert out["advanced"] is False
    assert out["error"], "应该给出 error 说明"


def test_v11_10b_never_advances_backwards(tmp_path):
    """★ V11-10b：目标早于当前交易日 ⇒ **拒绝**（不推进到过去）。"""
    cfg, _ = _root_with_account(tmp_path, "2026-11-19")
    ds = _FakeDS()
    # 造一个"目标更早"的场景：现在时刻是 11-06（日历里 11-06 是交易日）
    out = service.auto_settle_to_today(cfg, ds, now=datetime(2026, 11, 6, 12, 0))
    assert out["advanced"] is False
    assert out["error"] and "早于" in out["error"]
    assert store.read_state(cfg).trade_date == "2026-11-19", "被回退了 ✗"


# ══════════════════════════════════════════════════════════════════════ #
# R3 · 端点
# ══════════════════════════════════════════════════════════════════════ #
def test_v11_11_endpoint_shape():
    """★ V11-11：`POST /api/auto_settle` 返回五要素。"""
    from ptrade import webapp as webapp_mod

    src = inspect.getsource(webapp_mod.WebApp.api_auto_settle)
    for k in ("advanced", "from", "to", "days", "error", "offline"):
        assert f'"{k}"' in src, f"缺 {k}"
    assert "_envelope(\"auto_settle\"" in src


def test_v11_12_endpoint_failure_is_not_fatal():
    """★ V11-12：**失败也 200** —— 端点必须捕获业务异常并转成 `advanced=False`。"""
    from ptrade import webapp as webapp_mod

    src = inspect.getsource(webapp_mod.WebApp.api_auto_settle)
    # 先放行守卫错误
    assert "except NetworkForbiddenInLock" in src
    # 业务异常被转成 200 的降级结果
    assert "(OSError, ValueError, RuntimeError)" in src
    assert "Response(200" in src


def test_v11_13_endpoint_calls_ds_once():
    """★ V11-13：`_ds()` 只被调一次（`test_r5_ds_seam_is_parameterized` 同款约束）。"""
    from ptrade import webapp as webapp_mod

    src = inspect.getsource(webapp_mod.WebApp.api_auto_settle)
    assert src.count("self._ds(") == 1, f"`_ds` 调了 {src.count('self._ds(')} 次 ✗"


# ══════════════════════════════════════════════════════════════════════ #
# 回归
# ══════════════════════════════════════════════════════════════════════ #
def test_v11_14_api_state_untouched():
    """★ V11-14（INV-40）：`/api/state` **不得**被 v11 改动（仍与 `status --json` 一致）。"""
    from ptrade import webapp as webapp_mod

    src = inspect.getsource(webapp_mod.WebApp.api_state)
    for bad in ("auto_settle", "roll_to", "account_target_trade_date"):
        assert bad not in src, f"api_state 不该出现 {bad} ✗"


def test_v11_15_guard_sites_registered():
    """★ V11-15：新守卫站点已登记 `REGISTERED_GUARD_SITES`。"""
    from tests.test_wiring_invariants_v2 import REGISTERED_GUARD_SITES

    for site in (
        "service::account_target_trade_date",
        "service::auto_settle_to_today",
        "webapp::api_auto_settle",
    ):
        assert site in REGISTERED_GUARD_SITES, f"{site} 未登记 ✗"


def test_v11_16_frontend_calls_auto_settle_without_new_dom():
    """★ V11-16：前端调 `autoSettle`，且**不新增任何 DOM**（右列 0px 布局预算）。"""
    from ptrade import webui as webui_mod

    html = webui_mod.APP_HTML
    assert 'autoSettle: "/api/auto_settle"' in html
    assert "function autoSettleOnLoad()" in html
    assert "autoSettleOnLoad()" in html
    # ★ 反证：不得为此新增 DOM 元素
    assert 'data-testid="auto-settle' not in html, "加了 DOM ⇒ 布局预算会红 ✗"


def test_v11_17_no_future_advance_and_offline_flag(tmp_path):
    """★ V11-17：响应里带 `offline`，且**永不**把账户推到未来。"""
    cfg, _ = _root_with_account(tmp_path, "2026-11-09")
    ds = _FakeDS()
    out = service.auto_settle_to_today(cfg, ds, now=datetime(2026, 11, 19, 12, 8))
    assert "offline" in out and out["offline"] is False
    assert out["to"] <= "2026-11-19"
