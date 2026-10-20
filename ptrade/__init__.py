"""ptrade —— A 股 CANSLIM 模拟盘（T+1 / 融资融券 / 风控 / 打分）。

包结构（INTERFACES.md v1）：
    config.py      配置数据类与加载
    core.py        领域模型 + 账户数学（纯函数，无 IO）
    datasource.py  akshare 适配 + 缓存 + 离线夹具
    engine.py      T+1 / 两融 / 风控 / 下单（T2）
    strategy.py    CANSLIM 七因子打分（T3）
    cli.py         命令行入口

入口：``python -m ptrade <cmd>``。
"""

from __future__ import annotations

__version__ = "1.9.0"
__all__ = ["__version__"]
