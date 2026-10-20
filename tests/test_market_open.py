"""`service.market_open` 的三态与「不自取数」守卫（PRD R3 §3 F5 / W4）。

为什么单独一个文件：`market_open` 的**「日历陈旧 → None 而非 False」**是
「用日历/理想值推最新」这类错误的**最新一道防线** —— 本项目已经栽过四次
（下单 asof → 报告期 → 图表今天 → 完整日阈值）。它值得一组专门的判据。

纪律：
  * 每条判据都声明**数据从哪来**（全部本地构造，不依赖仓库 data/）；
  * 「不取数」类判据**必须有正对照** —— 否则「计数 == 0」可能是恒真（空守卫）。
"""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from ptrade import service


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# --------------------------------------------------------------------------- #
# 1. 签名被冻结（A12 表 / test_frozen_interfaces_v2.py 也会查，这里就近再钉一道）
# --------------------------------------------------------------------------- #

def test_signature_is_frozen():
    sig = inspect.signature(service.market_open)
    assert list(sig.parameters) == ["now", "trade_dates"], "形参名被改了"
    # 契约（PRD R3 §3 F5）：`trade_dates` 是**必填**位置参数，**没有默认值**。
    # （注解 `list[str] | None` 是说「值可以是 None」，不是说有默认值 —— 我第一版就在这里写错了。）
    assert sig.parameters["trade_dates"].default is inspect.Parameter.empty, \
        "trade_dates 变成有默认值了 —— 与冻结契约不一致"
    # 返回注解必须是 `bool | None`（三态）。注意 datasource/service 有
    # `from __future__ import annotations` → 注解是**字符串**，不能与类型对象比。
    assert "bool" in str(sig.return_annotation) and "None" in str(sig.return_annotation)


# --------------------------------------------------------------------------- #
# 2. 三态
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "label, now, dates, want",
    [
        # True：盘中（交易日）
        ("盘中 10:30", dt.datetime(2026, 10, 15, 10, 30), ["2026-10-14", "2026-10-15"], True),
        ("边界 09:30 整", dt.datetime(2026, 10, 15, 9, 30), ["2026-10-15"], True),
        ("边界 11:30 整", dt.datetime(2026, 10, 15, 11, 30), ["2026-10-15"], True),
        ("边界 13:00 整", dt.datetime(2026, 10, 15, 13, 0), ["2026-10-15"], True),
        ("边界 15:00 整", dt.datetime(2026, 10, 15, 15, 0), ["2026-10-15"], True),
        # False：日历覆盖今天，但不在盘中 / 不是交易日
        ("午休 12:00", dt.datetime(2026, 10, 15, 12, 0), ["2026-10-15"], False),
        ("盘后 16:00", dt.datetime(2026, 10, 15, 16, 0), ["2026-10-15"], False),
        ("开盘前 09:00", dt.datetime(2026, 10, 15, 9, 0), ["2026-10-15"], False),
        ("11:31 刚过 morning", dt.datetime(2026, 10, 15, 11, 31), ["2026-10-15"], False),
        ("12:59 未到 afternoon", dt.datetime(2026, 10, 15, 12, 59), ["2026-10-15"], False),
        ("15:01 刚收盘", dt.datetime(2026, 10, 15, 15, 1), ["2026-10-15"], False),
        # ★ 日历覆盖今天、但今天不在日历里（周末）→ False（**不是** None）
        ("周末（日历覆盖到今天）", dt.datetime(2026, 10, 17, 10, 30),
         ["2026-10-14", "2026-10-20"], False),
        # None：不可判定
        ("日历为空 list", dt.datetime(2026, 10, 15, 10, 30), [], None),
        ("日历为 None", dt.datetime(2026, 10, 15, 10, 30), None, None),
        # ★★ 最重要的一条：日历陈旧 → None（不是 False）
        ("★ 日历陈旧（max 在上月）", dt.datetime(2026, 12, 1, 10, 30), ["2026-10-14"], None),
    ],
)
def test_three_states(label, now, dates, want):
    assert service.market_open(now, dates) is want, f"{label} 判定错误"


def test_stale_calendar_is_none_not_false():
    """★ 本文件最重要的一条：日历不覆盖今天时**必须** None。

    若返回 False，前端会停掉轮询 → 用户第 6 项「自动刷新」**静默失效**。
    """
    got = service.market_open(dt.datetime(2026, 12, 1, 10, 30), ["2026-10-14"])
    assert got is None, "日历陈旧时返回了非 None —— 前端会停轮询、自动刷新静默失效"
    assert got is not False, "日历陈旧被误判成「已闭市」"


# --------------------------------------------------------------------------- #
# 3. ★ 不自取数（含**正对照**，否则「计数 == 0」可能是恒真）
# --------------------------------------------------------------------------- #

def test_market_open_never_fetches(monkeypatch):
    """`market_open` 是纯函数：调用它**不得**触发任何取数。"""
    calls = {"n": 0}

    class _Counting:
        def __init__(self, *a, **k):
            pass

        def trade_dates(self, *a, **k):
            calls["n"] += 1
            return ["2026-10-15"]

        def __getattr__(self, name):        # 其它任何属性被访问也算「取数企图」
            calls["n"] += 1
            raise AttributeError(name)

    fake = _Counting()

    # ★ 正对照：先证明计数器**能涨**（否则下面的 == 0 是恒真 = 空守卫）
    fake.trade_dates()
    assert calls["n"] == 1, "正对照失败：计数器根本没连上，下面的「计数==0」将毫无意义"

    calls["n"] = 0
    for _ in range(5):
        service.market_open(dt.datetime(2026, 10, 15, 10, 30), ["2026-10-15"])
    assert calls["n"] == 0, "market_open 试图取数 —— 它必须是纯计算"


def test_market_open_does_not_touch_datasource_module_attr(monkeypatch):
    """更强的版本：把 `DataSource.trade_dates` 换成会爆炸的实现，`market_open` 仍应正常。"""
    from ptrade import datasource

    def _boom(*a, **k):
        raise AssertionError("market_open 不得调用 DataSource.trade_dates()")

    monkeypatch.setattr(datasource.DataSource, "trade_dates", _boom, raising=False)

    # ★ 正对照：证明这个炸弹真的会炸（否则上面的 monkeypatch 可能没生效）
    with pytest.raises(AssertionError):
        datasource.DataSource.trade_dates(object())

    assert service.market_open(dt.datetime(2026, 10, 15, 10, 30), ["2026-10-15"]) is True


# --------------------------------------------------------------------------- #
# 4. 鲁棒性
# --------------------------------------------------------------------------- #

def test_tolerates_datetime_like_strings():
    """日历里若混入带时间的 ISO 串，应只取日期部分（按前 10 字符比较）。"""
    assert service.market_open(
        dt.datetime(2026, 10, 15, 10, 30),
        ["2026-10-14T00:00:00", "2026-10-15T00:00:00"],
    ) is True


def test_empty_and_none_are_none():
    assert service.market_open(dt.datetime(2026, 10, 15, 10, 30), []) is None
    assert service.market_open(dt.datetime(2026, 10, 15, 10, 30), None) is None


def test_exported_in_all():
    assert "market_open" in service.__all__


def test_market_open_has_docstring_stating_none_rule():
    """契约必须写在 docstring 里 —— 后来者不该靠猜才知道 None 的含义。"""
    doc = service.market_open.__doc__ or ""
    assert "None" in doc
    assert "陈旧" in doc or "覆盖今天" in doc
