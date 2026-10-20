"""测试前置守卫：夹具 / 剧本缺失时**显式跳过**，而不是静默跑出错数据。

夹具与剧本都不入 git（见 README「离线夹具与测试」）。**脚本的真实位置**（W6 修正）：

    python data/fixture/build_fixtures.py     # 用 akshare 现拉重建（需联网）
    python data/scenario/build_scenario.py    # 重建剧本（SYNTHETIC）

⚠️ **本文件原先指向 `tools/fetch-fixtures.py` / `tools/build_fixtures.py`，`tools/` 目录并不存在**
（Architect 第 1 轮实测、Critic 复核确认）。那段指引会让读日志的人**跑一条不存在的命令**，
所以在此改正 —— 并保留这条注释，说明「陈旧的路径指引」在本项目真实发生过。

之所以要在测试里显式守卫，而不是让夹具缺失自然失败：夹具缺失时数据源返回空，
断言会崩在**与夹具无关的位置**（例如「浮盈未进现金」这类不变量测试），
读起来像是实现有 bug。跳过并给出获取命令，比一份误导性的红更有用。
"""

from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "data", "fixture")
SCENARIO_DIR = os.path.join(ROOT, "data", "scenario")

_FIX_HINT = (
    "先跑 `python data/fixture/build_fixtures.py`（用 akshare 重建，需联网）"
    "或 `python data/scenario/build_scenario.py`（重建剧本）"
)


def _has_json(directory: str) -> bool:
    return os.path.isdir(directory) and any(
        f.endswith(".json") for f in os.listdir(directory)
    )


def price_asof(ds, code: str = "600519") -> str:
    """返回**日线数据真正覆盖到**的最近交易日（可安全下单/取价）。

    为什么不能直接用 ``ds.latest_trade_date()``（2026-10-13 暴露的时间炸弹）：
    它读的是 ``trade_dates.json``，那张日历覆盖到 2026-12-31；而 32 只票的
    **日线夹具全部止于 2026-10-09**。于是只要系统日期往前走，它就会返回一个
    **日历上有、却没有任何行情**的日期：依赖它的夹具会建仓在无数据的日期 ——
    ``quote()`` 因「日线末值日期 != asof」标 ``suspended=True``，引擎按 ``SUSPENDED``
    拒单，一批无关用例集体变红（实测 2026-10-13 早上 **8 个**失败，前一天全绿）。

    ``--asof`` 纪律要求历史序列**不得依赖系统时钟**；测试夹具同理：
    唯一真源是「数据实际覆盖到哪里」。

    判定用 ``quote()['suspended']`` —— 那正是引擎下单时的判据。
    **不能用** ``last_close()``：它按「≤asof 的最后一行」取值，越界会静默回落到更早日期，
    因此探测不出「当天无数据」（第一版就栽在这里，返回了无数据的 2026-10-12）。

    本函数放在 ``_guards.py`` 而非 ``conftest.py``，是为了**可被测试模块导入**。

    **预算不能 fail-open**（独立复核 N5）：第一版跑满 60 步后 ``return ds.latest_trade_date()``
    —— 那恰好是**已知无数据**的日期（就是时间炸弹本身），等于「找不到就退回炸弹」，
    而调用方无从分辨。实测：截短夹具（末日 2026-09-15）+ 时钟跳 2027-06-30 →
    它返回 ``2026-12-31``、``suspended=True``。
    改成**响亮失败**：夹具应有 420 天日线，最多回溯 400 步足够覆盖；
    真跑满说明夹具异常或日历与数据严重脱节，此时宁可直接报错，不要静默给出坏日期。
    """
    day = ds.latest_trade_date()
    for _ in range(400):                            # 夹具含 420 天日线，400 步足够
        if not ds.quote(code, day)["suspended"]:
            return day
        day = ds.prev_trade_date(day)
    raise AssertionError(
        f"price_asof 在 400 个交易日里都没找到有数据的日期（起始 {ds.latest_trade_date()}）—— "
        f"夹具缺失或交易日历与日线严重脱节。**不得**退回无数据的日期（那正是时间炸弹）。"
    )


def require_fixtures() -> None:
    """真实夹具（`data/fixture/`）缺失 → skip。"""
    if not _has_json(FIXTURE_DIR):
        pytest.skip(f"缺少 data/fixture —— {_FIX_HINT}")


def require_scenario() -> None:
    """剧本夹具（`data/scenario/`，SYNTHETIC）缺失 → skip。

    剧本标定的是**人为构造**的形态。缺它时若退回真实数据，
    那些断言会得到毫无意义的成败 —— 必须跳过而不是放行。
    """
    if not _has_json(SCENARIO_DIR):
        pytest.skip(
            "缺少 data/scenario —— 先跑 `python data/scenario/build_scenario.py`"
            "（重建剧本，SYNTHETIC）"
        )
