"""时间炸弹回归测试（N4/N5）：告警「夹具日期依赖系统时钟」。

**为什么需要这些测试**：2026-10-13 早上全量出现 8 个失败（前一天全绿），
根因是夹具用 ``latest_trade_date()``（读交易日历，覆盖到 2026-12-31）当 asof，
而日线夹具止于 2026-10-09 —— 系统日期一推进，就有一批无关用例因 ``SUSPENDED`` 集体变红。

独立复核还发现两处残留（N4/N5）：
  * ``test_watchlist.find_no_data_code`` 仍用 ``latest_trade_date()``，且只 catch 异常
    → **2026-12-10 起必然变红**；
  * ``price_asof`` 的 60 步预算 fail-open，会退回**已知无数据**的日期。

本文件把这两条钉住：用**伪造的「今天」**把时钟推到未来，断言结果不变。
"""

from __future__ import annotations

import pytest

from tests._guards import price_asof


def _warp_today(ds, monkeypatch, fake: str) -> None:
    """把 DataSource 的「今天」推到 ``fake``（不影响磁盘上的夹具数据）。"""
    monkeypatch.setattr(ds, "_today", lambda: fake, raising=False)


@pytest.mark.parametrize("fake_today", [
    "2026-10-13",     # 触发过真实失败的日期
    "2026-12-10",     # 复核者实测 test_watchlist 在此日期变红
    "2026-12-31",     # 日历末尾
    "2027-06-30",     # 远超日历
    "2030-01-01",
])
def test_price_asof_ignores_system_clock(offline_ds, monkeypatch, fake_today):
    """**N4/N5 核心断言**：无论「今天」是哪天，都返回**有数据**的同一个日期。"""
    baseline = price_asof(offline_ds)
    _warp_today(offline_ds, monkeypatch, fake_today)
    warped = price_asof(offline_ds)

    assert warped == baseline, (
        f"「今天」={fake_today} 时 price_asof 返回 {warped}，基线是 {baseline} —— "
        f"夹具日期又依赖系统时钟了（时间炸弹复发）"
    )
    # 关键：返回的日期必须**真的有数据**（否则等于退回炸弹日期）
    assert offline_ds.quote("600519", warped)["suspended"] is False, (
        f"price_asof 返回了无数据的日期 {warped}（suspended=True）"
    )


def test_price_asof_fails_loudly_when_nothing_has_data(offline_ds, monkeypatch):
    """**N5 非空守卫**：找不到任何有数据的日期时，必须**响亮失败**，不得退回炸弹日期。

    做法：让 ``quote()`` 永远报 suspended（模拟「夹具全无数据」），
    断言抛 AssertionError 而不是返回一个 ``latest_trade_date()``。
    这条守卫的意义：证明上面那条「不得 fail-open」不是空话。
    """
    monkeypatch.setattr(
        type(offline_ds), "quote",
        lambda self, code, asof: {"suspended": True, "last": float("nan")},
        raising=True,
    )
    with pytest.raises(AssertionError, match="不得.*退回无数据的日期"):
        price_asof(offline_ds)


def test_watchlist_no_data_code_is_actually_suspended(offline_cfg, monkeypatch):
    """**N4 断言**：``find_no_data_code`` 返回的代码必须**真的取不到行情**。

    修前它只 catch 异常，而缺数据时 ``quote()`` 返回 ``suspended=True`` 而**不抛异常**
    → 会把无效代码当有效候选返回，使 INV-38 用例失去前提
    （复核者实测症状：``_today=2026-12-10 → 2 failed``）。
    """
    from ptrade.datasource import DataSource

    from tests.test_watchlist import find_no_data_code

    code = find_no_data_code(offline_cfg)
    ds = DataSource(offline_cfg, offline=True)
    asof = price_asof(ds)
    # 「取不到行情」有两种形态：抛 `DataUnavailable`（缺日线夹具）
    # 或返回 `suspended=True` / 非正 last（有夹具但无有效价）。
    # `find_no_data_code` 必须把**两种**都认成「取不到」—— 它修前只 catch 异常，
    # 所以会漏掉第二种，把无效代码当有效候选返回。这里两种都接受。
    try:
        quote = ds.quote(code, asof)
    except Exception:  # noqa: BLE001 - 抛错即「取不到」，符合条件
        return
    last = quote.get("last")
    no_data = quote.get("suspended") is True or last is None or float(last) <= 0
    assert no_data, (
        f"find_no_data_code 返回了 {code}，但它在 {asof} 有行情 {quote} —— "
        f"INV-38 用例的前提不成立（判据失效）"
    )
