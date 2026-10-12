"""测试前置守卫：夹具 / 剧本缺失时**显式跳过**，而不是静默跑出错数据。

夹具与剧本都不入 git（见 README「离线夹具与测试」）：

    python tools/fetch-fixtures.py    # 从 release 下载（含剧本）
    python tools/build_fixtures.py    # 用 akshare 现拉重建
    python tools/build_scenario.py    # 重建剧本（SYNTHETIC）

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
    "先跑 `python tools/fetch-fixtures.py`（从 release 下载）"
    "或 `python tools/build_fixtures.py`（用 akshare 重建，需联网）"
)


def _has_json(directory: str) -> bool:
    return os.path.isdir(directory) and any(
        f.endswith(".json") for f in os.listdir(directory)
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
            "缺少 data/scenario —— 先跑 `python tools/fetch-fixtures.py`"
            "（夹具包内含剧本）或 `python tools/build_scenario.py`"
        )
