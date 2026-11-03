"""v7 判据：下单交易日语义（不传 = 最新交易日）+ 预热同源（锁内禁网）。

真源：`.omx/plans/prd-ptrade-v7.md` · `.omx/plans/test-spec-ptrade-v7.md`

## 背景（用户 2026-11-03 实测被挡）
```
order --code 600594 --side short_sell --qty 15700 --price 3.18 --json
→ ok=false · NetworkForbiddenInLock（INV-42）
```
根因两条：
1. `cli.cmd_order` 的 `trade_date = args.trade_date or state.trade_date or ...` ⇒ 账户停在旧日期时用旧日期；
   而 `live` 分支的 `live_trade_date()` 是**锁内**网络取数；
2. `service.prewarm_before_write` 把 `peek.trade_date` 排在快照之前 ⇒ 预热键是**旧日期** ⇒ 锁内未命中 ⇒ 联网。

修法：新增 `service.effective_trade_date()`（锁外、日历优先），CLI 的 `order`/`close` 都改用它。
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
    """V7-real：真联网、不传 `--trade-date`，`order` 必须**不触发 INV-42**。

    ★ 数据根用 `tmp_path`（**隔离**）—— 绝不能用仓库根：
      镜像里没有 `data/state.json`，用仓库根会 `FileNotFoundError`（第一版就这么假红过），
      而且写测试绝不该碰真实账户（`.omx/NOTES.md` 铁律 2）。
    ★ 先 `init` 建账户，再下一笔**平仓单**（`buy_to_cover` 无持仓 ⇒ 业务拒单也可接受，
      本判据只钉「**不得是 INV-42**」）。
    """
    import socket as _socket
    import subprocess
    import sys as _sys

    from ptrade.datasource import DataSource

    real_connect = _socket.socket.connect
    real_create = _socket.create_connection
    real_ak = DataSource._ak

    mp = pytest.MonkeyPatch()
    mp.setattr(_socket.socket, "connect", real_connect, raising=False)
    mp.setattr(_socket, "create_connection", real_create, raising=False)
    mp.setattr(DataSource, "_ak", real_ak, raising=True)
    try:
        root = str(tmp_path)
        # 建账户（--offline 也行：init 只写 state.json，不取行情）
        subprocess.run(
            [_sys.executable, "-m", "ptrade", "init", "--root", root, "--cash", "500000", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        p = subprocess.run(
            [_sys.executable, "-m", "ptrade", "order", "--root", root, "--code", "600594",
             "--side", "buy_to_cover", "--qty", "100", "--price", "3.23", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
        out = (p.stdout or "") + (p.stderr or "")
        assert "NetworkForbiddenInLock" not in out, f"不传 --trade-date 不得触发 INV-42：{out[:600]}"
        assert '"ok": true' in out or '"ok":true' in out, f"应成功（或业务拒单），实际: {out[:600]}"
    finally:
        mp.undo()
