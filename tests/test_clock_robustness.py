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


# --------------------------------------------------------------------------- #
# N4（独立复核第二轮）：`latest_trade_date()` 是**日历**日期，不是「有行情的日期」
#
# 复核者用自己的 clockjump 插件实测：2026-12-09 → 18 passed；**2026-12-10 → 2 failed**，
# 根因是「窗口 36→1→0 根日线 → last=NaN → 有数据的票被判 no_data」。
#
# **已更正**（第三轮独立复核）：我原先在这里写「生产侧同机制：离线模式下
# `/api/watchlist/score` 会从某天起把整个池子报成无数据」——**这句不成立**。
# 复核者用真实 `WebApp.dispatch` 跑 7 个时钟证明该端点**不走** `score_watchlist`，
# 全部 `200 / count=1 / no_data=0 / 真实分数`。受影响的是**调用 `score_watchlist`
# 的路径**（CLI/测试）与**缺省 asof 的日期选择**本身。
#
# 修法：新增 `DataSource.latest_trade_asof(probe_codes)` —— **两段式**：
# 先找「那一天本身有行情」（`quote().suspended is False`，引擎下单要的就是这个），
# 找不到再退到「回看窗口里真的有日线」（`score_watchlist` 这类窗口消费者）。
# 第一版只用了后者 → 返回了 2026-10-12（本身无行情）→ **缺省下单全被判停牌**（P3-2）。
# --------------------------------------------------------------------------- #
def _ds_with_clock(cfg, fake_today: str | None):
    """建一个 DataSource，可选地把「今天」钉住。

    **用实例属性**（`ds._today = ...`）遮蔽类的 staticmethod，而不是替换类属性：
    实测 Python 3.14 上把 staticmethod 对象赋回类属性**恢复不了**，实例调用会多传 self
    （`DataSource._today() takes 0 positional arguments but 1 was given`）。
    """
    from ptrade.datasource import DataSource

    ds = DataSource(cfg, offline=True)
    if fake_today is not None:
        ds._today = lambda: fake_today
    return ds


@pytest.mark.parametrize("fake_today", [
    "2026-10-13", "2026-12-09", "2026-12-10", "2026-12-31", "2027-06-30", "2030-01-01",
])
def test_latest_trade_asof_stays_on_data_boundary(offline_cfg, fake_today):
    """**N4 核心断言**：`latest_trade_asof` 返回的日期，其回看窗口里**真的有日线**。"""
    ds = _ds_with_clock(offline_cfg, fake_today)
    asof = ds.latest_trade_asof(["600519"])
    window = ds.daily("600519", ds._offset(asof, -60), asof)
    assert window is not None and len(window) > 0, (
        f"「今天」={fake_today} 时 latest_trade_asof 返回 {asof}，"
        f"但该日回看 60 天窗口里**一根日线都没有** —— 日期仍漂到无数据区（N4 复发）"
    )


def test_latest_trade_asof_is_not_the_calendar_date(offline_cfg):
    """**鉴别力对照**：`latest_trade_asof` 必须与 `latest_trade_date` **可区分**。

    若两者总相等，上面那条断言可能只是碰巧通过（日历日期恰好有数据）。
    注意：**不能**断言「两者一定不等」—— 数据与日历对齐时它们本就应该相等。
    这里断言的是「至少在一个时钟下两者不等」，从而证明差异确实存在。
    """
    from ptrade.datasource import DataSource

    diffs = 0
    for fake in ("2026-12-10", "2026-12-31", "2027-06-30"):
        ds = DataSource(offline_cfg, offline=True)
        ds._today = lambda f=fake: f                                   # noqa: B023
        if ds.latest_trade_asof(["600519"]) != ds.latest_trade_date():
            diffs += 1
    assert diffs > 0, (
        "在所有被测时钟下 latest_trade_asof 都等于 latest_trade_date —— "
        "判据没有鉴别力（那个新方法可能根本没有生效）"
    )


def test_watchlist_score_survives_clock_jump(offline_cfg, monkeypatch):
    """**N4 端到端**：时钟推到未来，`score_watchlist` 不得把有数据的票判成「无数据」。"""
    from ptrade import watchlist

    watchlist.add_to_watchlist(offline_cfg, "600519", "贵州茅台")

    def fake_ds(cfg, offline=True):                                    # noqa: ARG001
        ds = _ds_with_clock(cfg, "2026-12-10")
        return ds

    monkeypatch.setattr(watchlist, "DataSource", fake_ds)
    rows = watchlist.score_watchlist(offline_cfg, offline=True)
    assert rows, "自选池有票却返回空"
    oks = [r["ok"] for r in rows]
    assert all(oks), (
        f"时钟=2026-12-10 时有数据的票被判「无数据」：{[(r['code'], r['reason']) for r in rows]} "
        f"—— N4 复发（生产侧同样会让整个池子报 no_data）"
    )
    assert all(r["score"] is not None for r in rows)


# --------------------------------------------------------------------------- #
# M6（第四轮复核）：`latest_trade_asof` 的**第二段**会返回「本身没行情」的日期
#
# 触发条件：**所有探针在该日期都没有行情**（复核用合成 `daily_999001.json` 复现：
# `asof=2026-08-28, suspended=true`）。
# 第二段对「窗口消费者」（`score_watchlist`）够用，对「下单消费者」不够 ——
# 边缘标的（长期停牌 / B 股 / 新上市）仍可能复发 P3-2 的同类症状。
#
# 本函数**无法**在第二段里再找出「本身有行情」的日期（那正是第一段找不到的），
# 所以修法是**不假装成功**：留一条可见的痕。
#
# **用什么留痕**：先用了 `RuntimeWarning`，但实测它在离线夹具下**频繁触发**
# （自选池打分就走第二段 → 全量测试报出 9 条 warnings、CLI/网页端输出被污染）。
# 改为 **logger.info**：`caplog` 可断言、`--verbose` 可见，但不打扰正常使用。
# --------------------------------------------------------------------------- #
def test_m6_phase2_fallback_is_logged(offline_cfg, caplog):
    """**M6 断言**：退到第二段（窗口判据）时必须**留痕**，不得静默返回次优日期。"""
    import logging

    import pandas as pd

    from ptrade.datasource import DataSource

    class _Phase2Only(DataSource):
        """`quote()` 永远 suspended（第一段必失败），`daily()` 窗口非空（第二段命中）。"""

        def quote(self, code, asof):                    # noqa: ARG002
            return {"code": code, "name": code, "last": float("nan"), "prev_close": float("nan"),
                    "limit_up": float("nan"), "limit_down": float("nan"), "suspended": True}

        def daily(self, code, start, end):              # noqa: ARG002
            return pd.DataFrame([{"date": "2020-01-02", "close": 1.0}])

    ds = _Phase2Only(offline_cfg, offline=True)
    with caplog.at_level(logging.INFO, logger="ptrade.datasource"):
        got = ds.latest_trade_asof(["600519"])
    assert got, "第二段应返回一个日期（即使它本身没行情）"
    # 注意用 `r.getMessage()`（已做惰性 ``%`` 格式化），**不要**自己写 `r.message % r.args` ——
    # 日志里的 ``%s`` 与 args 个数不总对应，会抛 `TypeError`（我第一版就这么错了）。
    texts = [r.getMessage() for r in caplog.records]
    assert any("退到「回看窗口非空」判据" in t for t in texts), (
        f"退到第二段时必须留痕（不得静默返回次优日期）；实测日志={texts}"
    )


def test_m6_phase1_hit_does_not_log(offline_cfg, caplog):
    """**对照**：第一段命中时**不该**留痕（否则上面那条没有鉴别力）。"""
    import logging

    ds = _ds_with_clock(offline_cfg, None)
    with caplog.at_level(logging.INFO, logger="ptrade.datasource"):
        ds.latest_trade_asof(["600519"])
    texts = [r.getMessage() for r in caplog.records]
    assert not any("退到「回看窗口非空」判据" in t for t in texts), (
        f"第一段命中时不该留痕：{texts}"
    )
