"""ptrade.config —— 配置数据类与加载（INTERFACES.md v1 §2）。

配置文件 `<root>/config.json` 存在时覆盖默认值；缺失字段一律用默认值。
不引入第三方依赖。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from typing import Any

__all__ = [
    "FeeConfig",
    "MarginConfig",
    "RiskConfig",
    "CANSLIMConfig",
    "AccountConfig",
    "AppConfig",
    "load_config",
    "save_default_config",
    "package_root",
    "default_root",
]


@dataclass(frozen=True)
class FeeConfig:
    """费用配置。金额单位：元；费率：比例。"""

    commission_rate: float = 0.00025  # 佣金费率（双边）
    commission_min: float = 5.0  # 单笔最低佣金
    stamp_tax_rate: float = 0.0005  # 印花税（仅卖出）
    transfer_fee_rate: float = 0.00001  # 过户费（沪深双边）
    slippage_bps: float = 5.0  # 市价单滑点（基点，1bp = 0.01%）


@dataclass(frozen=True)
class MarginConfig:
    """融资融券配置。"""

    min_maintenance_ratio: float = 1.30  # 平仓线
    warn_maintenance_ratio: float = 1.50  # 警戒线
    max_maintenance_ratio: float = 3.00  # 可提取线
    margin_ratio_long: float = 0.80  # 融资保证金比例
    margin_ratio_short: float = 1.00  # 融券保证金比例
    financing_rate_annual: float = 0.065  # 融资年化利率
    lending_rate_annual: float = 0.080  # 融券年化费率
    short_source_limit_ratio: float = 0.02  # 单票可融券量 <= 流通股本的 2%


@dataclass(frozen=True)
class RiskConfig:
    """风控配置。"""

    max_position_pct: float = 0.20  # 单票市值（多头+融券）/ 净资产（**非总资产**，见 §5 强约束 6）
    max_positions: int = 5  # 同时持仓标的数
    stop_loss_pct: float = -0.07  # CANSLIM 止损线（-7%，相对成本价）
    take_profit_pct: float = 0.25  # 止盈提示线
    max_gross_exposure: float = 2.0  # 总敞口（多头市值+融券市值）/ 净资产
    max_short_exposure: float = 0.50  # 融券市值 / 净资产
    max_leverage: float = 2.0  # 【v3 新增】总负债 / 净资产 上限；防止「开仓即低于平仓线」


@dataclass(frozen=True)
class CANSLIMConfig:
    """CANSLIM 七因子权重与阈值，权重和 = 1.0。"""

    weight_c: float = 0.18  # 当季 EPS 增长
    weight_a: float = 0.16  # 年度 EPS 增长
    weight_n: float = 0.14  # 新产品/新管理层/新高
    weight_s: float = 0.12  # 供给（流通盘 + 量能）
    weight_l: float = 0.16  # 相对强度（龙头/落后）
    weight_i: float = 0.12  # 机构认同度
    weight_m: float = 0.12  # 市场方向
    buy_threshold: float = 75.0  # 买入候选门槛（0-100）
    short_threshold: float = 40.0  # 做空候选门槛（<= 此值）
    c_accel_bonus: float = 1.25  # 增长加速加成倍数

    @property
    def weights(self) -> dict[str, float]:
        """{"C": .., "A": .., ...} 便于策略层加权。"""
        return {
            "C": self.weight_c,
            "A": self.weight_a,
            "N": self.weight_n,
            "S": self.weight_s,
            "L": self.weight_l,
            "I": self.weight_i,
            "M": self.weight_m,
        }


@dataclass(frozen=True)
class AccountConfig:
    """账户配置。"""

    initial_cash: float = 500000.0


@dataclass(frozen=True)
class AppConfig:
    """聚合配置。``root`` 为数据根目录（绝对路径）。"""

    account: AccountConfig
    fee: FeeConfig
    margin: MarginConfig
    risk: RiskConfig
    canslim: CANSLIMConfig
    root: str


def package_root() -> str:
    """`ptrade/` 的父目录（源码检出时的仓库根，如 `D:\\Ai投资`）。

    **仅作后备**：包被 pip 安装进 `site-packages` 后该目录只读，
    因此不再作为数据根默认值（见 :func:`default_root`）。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_root() -> str:
    """解析数据根目录（三级优先，v1.1 起）。

    1. 环境变量 ``PTRADE_HOME`` —— 显式指定，最高优先；
    2. 源码检出：若 :func:`package_root` 下存在 ``data/`` 目录（说明是从仓库直接跑），
       沿用它，保持「clone 即用」体验不变；
    3. 否则落到用户主目录的 ``~/.ptrade``（pip 安装后的默认，**可写**）。

    目录不存在时按需创建。返回绝对路径。
    """
    env = os.environ.get("PTRADE_HOME")
    if env and env.strip():
        root = os.path.abspath(os.path.expanduser(env.strip()))
    else:
        pkg = package_root()
        if os.path.isdir(os.path.join(pkg, "data")):
            root = pkg
        else:
            root = os.path.abspath(os.path.join(os.path.expanduser("~"), ".ptrade"))
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        # 只读文件系统等：仍返回路径，由调用方在首次写入时报出清晰错误
        pass
    return root


def _section(cls: type, raw: Any) -> Any:
    """从 raw 中取出 cls 的已知字段（未知字段忽略，缺失用默认值）。"""
    if not isinstance(raw, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


def load_config(root: str | None = None) -> AppConfig:
    """加载配置。``root`` 缺省时按 :func:`default_root` 的三级优先解析。"""
    root = os.path.abspath(root) if root else default_root()
    path = os.path.join(root, "config.json")
    raw: dict[str, Any] = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            raw = data
    return AppConfig(
        account=_section(AccountConfig, raw.get("account")),
        fee=_section(FeeConfig, raw.get("fee")),
        margin=_section(MarginConfig, raw.get("margin")),
        risk=_section(RiskConfig, raw.get("risk")),
        canslim=_section(CANSLIMConfig, raw.get("canslim")),
        root=root,
    )


def _defaults() -> dict[str, Any]:
    return {
        "account": {
            "initial_cash": AccountConfig().initial_cash,
        },
        "fee": {
            "commission_rate": FeeConfig().commission_rate,
            "commission_min": FeeConfig().commission_min,
            "stamp_tax_rate": FeeConfig().stamp_tax_rate,
            "transfer_fee_rate": FeeConfig().transfer_fee_rate,
            "slippage_bps": FeeConfig().slippage_bps,
        },
        "margin": {
            "min_maintenance_ratio": MarginConfig().min_maintenance_ratio,
            "warn_maintenance_ratio": MarginConfig().warn_maintenance_ratio,
            "max_maintenance_ratio": MarginConfig().max_maintenance_ratio,
            "margin_ratio_long": MarginConfig().margin_ratio_long,
            "margin_ratio_short": MarginConfig().margin_ratio_short,
            "financing_rate_annual": MarginConfig().financing_rate_annual,
            "lending_rate_annual": MarginConfig().lending_rate_annual,
            "short_source_limit_ratio": MarginConfig().short_source_limit_ratio,
        },
        "risk": {
            "max_position_pct": RiskConfig().max_position_pct,
            "max_positions": RiskConfig().max_positions,
            "stop_loss_pct": RiskConfig().stop_loss_pct,
            "take_profit_pct": RiskConfig().take_profit_pct,
            "max_gross_exposure": RiskConfig().max_gross_exposure,
            "max_short_exposure": RiskConfig().max_short_exposure,
            "max_leverage": RiskConfig().max_leverage,
        },
        "canslim": {
            "weight_c": CANSLIMConfig().weight_c,
            "weight_a": CANSLIMConfig().weight_a,
            "weight_n": CANSLIMConfig().weight_n,
            "weight_s": CANSLIMConfig().weight_s,
            "weight_l": CANSLIMConfig().weight_l,
            "weight_i": CANSLIMConfig().weight_i,
            "weight_m": CANSLIMConfig().weight_m,
            "buy_threshold": CANSLIMConfig().buy_threshold,
            "short_threshold": CANSLIMConfig().short_threshold,
            "c_accel_bonus": CANSLIMConfig().c_accel_bonus,
        },
    }


def save_default_config(path: str) -> AppConfig:
    """写出默认配置。

    - `path` 以 `.json` 结尾 → 视为配置文件；
    - 否则视为数据根目录 → 写 `<path>/config.json`。

    返回加载后的 :class:`AppConfig`（常量恒定，无随机数）。
    """
    if path.lower().endswith(".json"):
        file_path = os.path.abspath(path)
        root = os.path.dirname(file_path)
    else:
        root = os.path.abspath(path)
        file_path = os.path.join(root, "config.json")
    os.makedirs(root, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as fh:
        json.dump(_defaults(), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return load_config(root)
