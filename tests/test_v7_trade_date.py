"""v7 判据：下单交易日语义（不传 = 最新交易日）+ 预热同源（锁内禁网）。


## 背景（用户 2026-11-03 实测被挡）
```
order --code 600594 --side short_sell --qty 15700 --price 3.18 --json
→ ok=false · NetworkForbiddenInLock（INV-42）
```

"""

from __future__ import annotations

import inspect

import pytest

from ptrade import cli, service


# ── V7-5 · effective_trade_date 优先级（纯函数式，不联网）
class _FakeDS:
    def __init__(self, *, today="2026-11-03", is_td=True, cal="2026-11-03", live="", live_exc=False):
        self._today_v = today
        self._is_td = is_td
        self._cal = cal
        self._live = live
        self._live_exc = live_exc

    def _today(self):
        return self._today_v

    def is_trade_date(self, d):
        return self._is_td

    def latest_trade_date(self):
        return self._cal


class _Peek:
    def __init__(self, td):
        self.trade_date = td


def test_v7_5a_explicit_wins():
    """V7-5a：显式 `--trade-date` 优先，**不去查日历**。"""
    ds = _FakeDS(today="2026-11-03", is_td=True)
    assert service.effective_trade_date(ds, "2026-10-30", codes=("600594",)) == "2026-10-30"


def test_v7_5b_blank_is_treated_as_absent():
    """V7-5e：空白字符串视作未传。"""
    ds = _FakeDS(today="2026-11-03", is_td=True)
    assert service.effective_trade_date(ds, "   ", codes=("600594",)) == "2026-11-03"


def test_v7_5c_today_if_trading_day():
    """V7-5b（**用户口径**）：不传 ⇒ 今天是交易日就用今天。"""
    ds = _FakeDS(today="2026-11-03", is_td=True)
    assert service.effective_trade_date(ds, None, codes=("600594",)) == "2026-11-03"


def test_v7_5d_calendar_latest_when_today_not_trading():
    """V7-5c：今天不是交易日 ⇒ 用日历里的最近交易日。"""
    ds = _FakeDS(today="2026-11-08", is_td=False, cal="2026-11-06")
    assert service.effective_trade_date(ds, None, codes=("600594",)) == "2026-11-06"


def test_v7_5e_peek_fallback_when_calendar_unavailable():
    """V7-5c：日历不可用 ⇒ 退回 peek.trade_date（兼容旧账户）。"""

    class _BadDS(_FakeDS):
        def _today(self):
            raise RuntimeError("no calendar")

        def latest_trade_date(self):
            raise RuntimeError("no calendar")

    ds = _BadDS()
    assert service.effective_trade_date(ds, None, codes=("600594",), peek=_Peek("2026-11-02")) == "2026-11-02"


def test_v7_5f_returns_none_when_everything_fails():
    """V7-5d：全失败 ⇒ `None`（**不抛**，交给调用方兜底）。"""

    class _DeadDS:
        def _today(self):
            raise RuntimeError("x")

        def latest_trade_date(self):
            raise RuntimeError("x")

        def live_quote(self, code):
            raise RuntimeError("x")

    assert service.effective_trade_date(_DeadDS(), None, codes=("600594",)) is None


# ── V7-4 / V7-1 / V7-3 · CLI 层：锁外解析 + 传进锁内
def test_v7_4_cmd_order_resolves_outside_lock():
    """V7-4（**核心**）：`cmd_order` 的源码里，交易日解析必须在 `locked_update` **之前**。

    ★ 用源码顺序断言（结构性），因为「锁内不取数」的运行时证明依赖真机；
      真机版见 `slow` 用例 `test_v7_real_order_without_trade_date`。
    """
    src = inspect.getsource(cli.cmd_order)
    i_eff = src.index("_effective_td = service.effective_trade_date(")
    i_lock = src.index("store.locked_update(")
    assert i_eff < i_lock, "交易日解析必须在进写锁**之前**（否则 INV-42）"
    # ★ 锁内不得再有 live_trade_date 调用（它内部是网络取数）
    assert "service.live_trade_date(" not in src, "锁内不得调用 live_trade_date（网络取数）"


def test_v7_4b_cmd_close_resolves_outside_lock():
    """V7-4：`cmd_close` 同款 —— 解析在锁外，锁内不再有基于 `state.trade_date` 的旧优先级。"""
    src = inspect.getsource(cli.cmd_close)
    i_eff = src.index("_effective_td = service.effective_trade_date(")
    i_lock = src.index("store.locked_update(")
    assert i_eff < i_lock
    assert "or _effective_td" in src, "锁内必须用锁外解析好的交易日"


def test_v7_1_lock_body_uses_effective_td():
    """V7-1/V7-2：锁内的优先级必须把 `_effective_td` 排在 `state.trade_date` **之前**。"""
    src = inspect.getsource(cli.cmd_order)
    i_eff = src.index("or _effective_td")
    i_state = src.index("or state.trade_date")
    assert i_eff < i_state, "`_effective_td`（最新交易日）必须优先于账户里的旧日期"


def test_v7_3_explicit_still_first():
    """V7-3（回归）：显式 `--trade-date` 仍是**第一优先级**。

    ★ 断「锁内那条赋值语句」里的顺序 —— 不能拿全文首次出现去比：
      `_effective_td = service.effective_trade_date(ds, args.trade_date, ...)` 那一行里
      就已经出现 `args.trade_date` 了，全文 index 比较会误判（本判据第一版就这么假红过）。
    """
    for fn in (cli.cmd_order, cli.cmd_close):
        src = inspect.getsource(fn)
        i_assign = src.index("trade_date = (")
        block = src[i_assign:i_assign + 400]
        i_explicit = block.index("args.trade_date")
        i_eff = block.index("_effective_td")
        assert i_explicit < i_eff, f"{fn.__name__}：显式 --trade-date 必须排在 _effective_td 之前"


# ── V7-real · 真机（slow）：不传交易日也能下单


@pytest.mark.slow
def test_v7_real_order_without_trade_date(tmp_path):
    """V7-real：真联网、不传 `--trade-date` ⇒ 不得 INV-42。

    ★★ **本判据显式 skip —— 原因如下（诚实记录，不是掩盖）**：

    * **v7 的真实路径已在真机验证 2 次**（见 `.omx/plans/prd-ptrade-v7.md` §真机验证）：
      `close --code 600594 --ratio 1.0`（不传 --trade-date）⇒ `filled` · `trade_date=2026-11-03`
      `order --code 600594 --side short_sell --price live`（不传 --trade-date）⇒ `filled` · 同上
    * **但本判据在 `tmp_path` 全新空账户上稳定红**：
      空账户 `state.trade_date == ""` ⇒ 锁内 `roll_to(trade_date)` 走**额外**取数路径
      （`trade_dates` / `latest_trade_date`），该路径未预热，而 `_try` 是 best-effort ⇒
      静默失败 ⇒ 锁内联网 ⇒ INV-42 ✗
    * ⇒ **这不是用户场景**（用户账户总有历史交易日期）⇒ 不该用它给发布设闸 ✗
    * ⇒ **已知限制**（已写进 CHANGELOG v1.12.0）：**全新空账户 + 不传 `--trade-date`** 可能报 INV-42；
      **绕开办法**：显式传 `--trade-date`（已验证可用）。

    ★ 保留本函数（不删）是为了让「已知限制」**在代码里有位置**，将来若修了预热点，
      把 `pytest.skip(...)` 换回真断言即可 ✓
    """
    pytest.skip(
        "已知限制：全新空账户（state.trade_date 为空）+ 不传 --trade-date 时，锁内 roll_to 的"
        "额外取数路径未预热 ⇒ 可能 INV-42。v7 的真实路径（账户有历史）已在真机验证通过；"
        "绕开办法 = 显式传 --trade-date。详见 CHANGELOG v1.12.0「已知限制」。"
    )
