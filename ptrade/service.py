"""ptrade.service —— 中立服务层：可被 CLI 与网页端**共同依赖**的助手。

**为什么需要这个模块**（architect 评审 W-7 / 建议 S-2）：

``webapp.py`` 曾经通过 ``_cli()`` 调用 ``cli.py`` 的**私有**助手（``_make_ds`` /
``_state_path`` / ``_state_summary`` / ``_jsonable`` / ``_resolve_asof`` 等 **12 处**）。
那是**分层倒置**：UI 层（webapp）反向依赖另一个 UI 层（cli）的内部实现，
CLI 一改内部结构就会连带打断网页端。评审判为 **WATCH**（已识别、有解药），
并要求设到期条件：**下次新增写端点或第二个消费者之前必须先抽本模块**。

本模块把那些助手搬成**公开 API**；``cli.py`` 保留同名私有函数作**薄转发**，
因此既有调用点与测试零改动，而 ``webapp.py`` 改为依赖 ``service``。

**依赖方向（单向、无环）**：
``service`` → ``core`` / ``config`` / ``datasource`` / ``store`` / ``strategy``
``cli`` → ``service``；``webapp`` → ``service``
**``service`` 不得 import ``cli`` 或 ``webapp``**（否则又成环）。
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
from datetime import datetime
from typing import Any

from . import core, store
from .config import AppConfig
from .core import AccountState
from .datasource import DataSource

__all__ = [
    "jsonable",
    "state_path",
    "ledger_path",
    "state_summary",
    "make_ds",
    "resolve_asof",
    "consistency",
    "risk_from_core",
]


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def jsonable(obj: Any) -> Any:
    """递归转成 JSON 安全结构（``to_dict()`` / dataclass / 基本类型）。

    把 NaN / Infinity 归成 ``None``：``json.dumps(..., allow_nan=False)`` 遇到它们会抛，
    而状态里出现非有限数意味着上游已经出错，不该让它变成「响应 500」。
    """
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if hasattr(obj, "to_dict"):
        return jsonable(obj.to_dict())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    return str(obj)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
STATE_DIR = "data"
STATE_FILE = "state.json"
LEDGER_FILE = "ledger.jsonl"


def state_path(cfg: AppConfig) -> str:
    return os.path.join(cfg.root, STATE_DIR, STATE_FILE)


def ledger_path(cfg: AppConfig) -> str:
    return os.path.join(cfg.root, STATE_DIR, LEDGER_FILE)


# --------------------------------------------------------------------------- #
# 账户摘要（INV-32：网页端与 CLI 必须用**同一个**序列化器）
# --------------------------------------------------------------------------- #
def state_summary(state: AccountState) -> dict[str, Any]:
    """账户快照的稳定序列化。字段顺序即契约顺序（既有消费者按需读取）。"""
    ratio = state.maintenance_ratio
    return {
        "trade_date": state.trade_date,
        "initial_cash": state.initial_cash,
        "available_cash": state.available_cash,
        "frozen_t1": state.frozen_t1,
        "frozen_collateral": state.frozen_collateral,
        "frozen_cash": state.frozen_cash,
        "financing_debt": state.financing_debt,
        "short_cost_debt": dict(state.short_cost_debt),
        "short_qty_debt": dict(state.short_qty_debt),
        "interest_accrued": state.interest_accrued,
        "long_market_value": state.long_market_value,
        "short_market_value": state.short_market_value,
        "collateral_value": state.collateral_value,
        "total_assets": state.total_assets,
        "total_debt": state.total_debt,
        "net_assets": state.net_assets,
        "maintenance_ratio": None if ratio is None or not math.isfinite(ratio) else ratio,
        "leverage": state.leverage,
        "margin_required": state.margin_required,
        "margin_available": state.margin_available,
        "total_pnl": state.total_pnl,
        "margin_call": state.margin_call,
        "forced_liquidation": state.forced_liquidation,
        "positions": [position_row(p) for p in state.positions.values() if p.is_active],
    }


def position_row(position) -> dict[str, Any]:
    """单笔持仓的稳定序列化（字段与原 ``cli._state_summary`` **逐字段一致**）。

    注意字段名是 ``long_available`` / ``short_available``（不是 ``available_qty``）——
    第一版我按想象写错了，核对 ``core.Position`` 后才改对。位置过滤由调用方负责
    （``state_summary`` 只输出 ``is_active`` 的持仓）。
    """
    return {
        "code": position.code,
        "name": position.name,
        "long_qty": position.long_qty,
        "long_available": position.long_available,
        "short_qty": position.short_qty,
        "short_available": position.short_available,
        "long_cost": position.long_cost,
        "short_cost": position.short_cost,
        "last_price": position.last_price,
        "long_value": position.long_value,
        "short_value": position.short_value,
        "long_pnl": position.long_pnl,
        "short_pnl": position.short_pnl,
    }


# --------------------------------------------------------------------------- #
# 数据源 / 交易日
# --------------------------------------------------------------------------- #
def make_ds(cfg: AppConfig, offline: bool) -> DataSource:
    """构造数据源（唯一的构造点，便于测试注入与一致性）。"""
    return DataSource(cfg, offline=bool(offline))


MARKET_CLOSE = (15, 30)


def resolve_asof(ds: DataSource, asof: str | None, notes: list[str] | None = None) -> str:
    """``--asof`` 未来函数防护（v3.1）：只允许**已收盘**交易日。

    语义与原 ``cli._resolve_asof`` **逐字一致**（含错误类型与消息前缀）：

    * 缺省 = ``ds.latest_trade_date()``（其本身已裁剪到"最近已收盘交易日"）；
    * 传未来日期 → ``RuntimeError("NO_MARKET_DATA: ...")``；
    * 显式传入当天、当前 < 15:30 **且当天是交易日** → 回退上一交易日并在 ``notes`` 标注。

    第二版修正：我第一版按想象写成「用 ``datetime.now()`` 比 15:30、抛 ValueError」，
    与真实实现（用 ``ds._today()`` 做上界、抛 ``RuntimeError`` 且消息以 ``NO_MARKET_DATA``
    开头）不符 —— 那会让上层的错误分类与既有测试一起漂移。
    """
    day = str(asof)[:10] if asof else ds.latest_trade_date()
    today = ds._today()  # noqa: SLF001 - 与原 cli 实现同款（它承载"当前可用交易日"语义）
    now = datetime.now()
    if day > today:
        raise RuntimeError(f"NO_MARKET_DATA: asof={day} 超出可用数据范围（今天 {today}）")
    closed = (now.hour, now.minute) >= MARKET_CLOSE
    if day == today and not closed and ds.is_trade_date(today):
        prev = ds.prev_trade_date(today)
        if notes is not None:
            notes.append(f"当日（{today}）尚未收盘（<15:30），已回退到上一交易日 {prev}")
        return prev
    return day


# --------------------------------------------------------------------------- #
# 账实一致性（M3：让「可检出」真的被用户看见）
# --------------------------------------------------------------------------- #
def consistency(cfg: AppConfig) -> dict[str, Any]:
    """ledger / state 分叉检查的**对外视图**（去掉内部 ``reason`` 细节键的歧义）。

    直接转发 :func:`ptrade.store.check_ledger_state_consistency`；后者已在写锁内读取
    （M4），因此在事务进行中会返回 ``diverged=None``（不可判定）而**不会误报**。
    """
    return store.check_ledger_state_consistency(cfg)


def risk_from_core(state: AccountState, cfg: AppConfig) -> dict[str, Any]:
    """``engine`` 未就绪时的 core 兜底风险报告（口径与 ``engine.risk_report`` 一致）。

    原在 ``cli.py``（``_risk_from_core``）。搬进 service 是因为**网页端也需要它**
    （``webapp.api_risk`` 的兜底分支），而它本身只依赖 ``core`` 与 ``state_summary``，
    没有理由让网页端跨越到另一个 UI 层去取它（W-7 的最后 1 处）。
    """
    state = core.refresh_risk_flags(state, cfg.risk, cfg.margin)
    ratio = state.maintenance_ratio
    return {
        "maintenance_ratio": None if ratio == float("inf") else ratio,
        "status": core.risk_status(state, cfg.risk, cfg.margin),
        "alerts": core.check_risk(state, cfg.risk, cfg.margin),
        "leverage": state.leverage,
        "exposure": {
            "long_market_value": state.long_market_value,
            "short_market_value": state.short_market_value,
            "gross_exposure": state.gross_exposure,
            "short_exposure": state.short_exposure,
            "margin_required": state.margin_required,
            "margin_available": state.margin_available,
            "max_leverage": cfg.risk.max_leverage,
        },
        "positions": state_summary(state)["positions"],
        "source": "core",
    }
