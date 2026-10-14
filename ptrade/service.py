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
from typing import Any, Iterable

from . import core, store
from .config import AppConfig
from .core import AccountState
from .datasource import DataSource, NetworkForbiddenInLock

__all__ = [
    "jsonable",
    "state_path",
    "ledger_path",
    "state_summary",
    "make_ds",
    "resolve_asof",
    "consistency",
    "risk_from_core",
    "prewarm_for_write",
    "prewarm_before_write",
    "MARGIN_SIDES",
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

    语义与原 ``cli._resolve_asof`` 一致（含错误类型与消息前缀）：

    * 缺省 = **``ds.latest_trade_asof()``**（见下方"缺省值"小节）；
    * 传未来日期 → ``RuntimeError("NO_MARKET_DATA: ...")``；
    * 显式传入当天、当前 < 15:30 **且当天是交易日** → 回退上一交易日并在 ``notes`` 标注。

    **缺省值修正（2026-10-13，P3-2 —— 第三轮独立复核指出，今天就在发作）**：
    原实现用 ``ds.latest_trade_date()``，那是**交易日历**上的最近一天，
    而行情只到已收盘的最近一天。两者相差时（实测：日历给 2026-10-12、日线止于 2026-10-09）
    缺省路径会拿到一个**没有行情**的日期，于是：

    * 引擎 ``quote()`` 标 ``suspended=True`` → **缺省下单全部被 `SUSPENDED` 误拒**
      （实测 ``order --code 000001 --side buy --qty 100 --offline``（不传 ``--price``）
      → ``rejected / SUSPENDED 停牌或无有效行情``）；
    * 测试夹具里一批无关用例集体变红。

    改用 ``latest_trade_asof()`` 后，缺省落在**真有行情**的那一天。
    第二版修正：我第一版按想象写成「用 ``datetime.now()`` 比 15:30、抛 ValueError」，
    与真实实现（用 ``ds._today()`` 做上界、抛 ``RuntimeError`` 且消息以 ``NO_MARKET_DATA``
    开头）不符 —— 那会让上层的错误分类与既有测试一起漂移。
    """
    if asof:
        day = str(asof)[:10]
    else:
        try:
            day = ds.latest_trade_asof()
        except NetworkForbiddenInLock:
            raise
        except Exception:  # noqa: BLE001 - 拿不到「有行情的日期」就退回日历语义
            day = ds.latest_trade_date()
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


# --------------------------------------------------------------------------- #
# 锁外预热（架构评审 BLOCK-1）
#
# 放在 service 而不是 cli：**网页端与 CLI 都需要**。第一版只修了 CLI，
# 独立复核实测 `POST /api/order` 仍会在锁内取数 3 次、并饿死并发写者
# （注入 0.6s/次延迟 + timeout=1.0 → StateLockTimeout），而 CLI 侧同一实验 0.02s 拿到锁。
# --------------------------------------------------------------------------- #
MARGIN_SIDES = frozenset({"margin_buy", "short_sell", "buy_to_cover", "repay_margin"})


def prewarm_for_write(
    ds: DataSource,
    state: AccountState | None,
    trade_date: str | None,
    extra_codes: Iterable[str] = (),
    *,
    side: str | None = None,
) -> list[str]:
    """在**进入写锁之前**把写路径会用到的数据预热进缓存（BLOCK-1）。

    为什么必须这么做：写命令把「读 → 改 → 写」整体放进 ``store.locked_update``，
    而引擎在这个临界区内会取数。**离线**时是本地夹具读（锁持有 ≈0.12s，无害）；
    但**在线 + 冷缓存**时是网络取数 —— 架构评审实测单段持锁 **24.97s**（另一次 >180s），
    而网页端 ``DEFAULT_LOCK_TIMEOUT`` 是 **10s** → 网页下单稳定 503，而账户毫无问题。

    ``DataSource`` 缓存落在磁盘（``_read_entry`` 先读盘），所以锁外预热一次后，
    锁内同类调用会命中新鲜缓存而**不再联网**。

    **预热键来自实测**（``.omx/probe/discover_lock_keys.py`` 记录锁内真实请求）：
    ``trade_dates`` 恒需；``spot`` 与 ``margin_targets`` 按需。
    不要凭想象加键 —— 第一版预热过 ``daily(c, None, asof)``，而引擎从不请求这个键
    （缓存键含实际的起止日期），于是那是**空预热**、还写出垃圾缓存文件，
    却被计进返回值让「预热有效」的断言空过（独立复核 N8）。

    参数：
      * ``state``：取**全部持仓**代码 —— 引擎的盯市会遍历所有持仓，不只本次下单那只；
      * ``extra_codes``：本次操作涉及的代码，与持仓代码合并去重；
      * ``trade_date``：**必须在锁外算好**。传 ``None`` 时几乎预不到东西
        （第一版就栽在这里：``init`` 后 ``state.trade_date`` 为空，交易日由锁内的
        ``resolve_asof`` 才确定 → 预热全被跳过，锁内仍是 6 次取数）；
      * ``side``：融资/融券方向需要额外预热 ``margin_targets``（2 个 akshare 接口，
        最坏 ~4.9s/个 —— 架构评审 S-1 点名过）。

    本函数**幂等**、**不抛**（best-effort）：真正的失败仍由锁内路径按原语义报错。
    返回**成功预热**的键列表（供测试与非空守卫断言「确实做了事」）。
    """
    warmed: list[str] = []
    codes = [str(c) for c in (getattr(state, "positions", {}) or {})]
    all_codes = list(dict.fromkeys([*codes, *(str(c) for c in extra_codes)]))

    def _try(label: str, fn) -> None:
        try:
            fn()
            warmed.append(label)
        except Exception:  # noqa: BLE001 - 预热尽力而为，失败留给锁内按原语义报错
            pass

    _try("trade_dates", lambda: ds.trade_dates())
    _try("spot", lambda: ds.spot())
    # **必须同时预热「按代码」的快照**（独立复核第二轮指出）：
    # `engine._float_shares` 在 `quote()` 没有 `float_mv` 时会走 `ds.spot([code])`
    # → `_enrich_spot`，那里是**另一个取数点**（会调 `_ak()`）。
    # 只预热 `ds.spot()`（codes=None）覆盖不到它 —— 守卫开火后 `source_limit` 变 0，
    # 而调用点判定是 `0 < source_limit` → **券源上限检查被静默跳过**（风控静默失效）。
    if all_codes:
        _try("spot:codes", lambda: ds.spot(list(all_codes)))
    # 融资/融券方向：锁内会查两融标的白名单（两个独立接口，冷缓存时很慢）
    if side and str(side) in MARGIN_SIDES:
        _try("margin_targets", lambda: ds.margin_targets())
    if trade_date:
        _try("next_trade_date", lambda: ds.next_trade_date(trade_date))
        _try("is_trade_date", lambda: ds.is_trade_date(trade_date))
        for code in all_codes:
            _try(f"quote:{code}", lambda c=code: ds.quote(c, trade_date))
            _try(f"last_close:{code}", lambda c=code: ds.last_close(c, trade_date))
    return warmed


def prewarm_before_write(
    cfg: AppConfig,
    ds: DataSource,
    explicit_date: str | None,
    extra_codes: Iterable[str] = (),
    *,
    side: str | None = None,
) -> list[str]:
    """锁外预热助手：用**与锁内完全相同的逻辑**解析交易日，再预热。

    为什么必须「相同逻辑」：第一版预热直接用请求里的 ``trade_date``，而 ``init`` 后
    ``state.trade_date`` 为空、交易日是锁内 ``resolve_asof`` 才定下来的
    → 预热时 ``trade_date=None`` → 几乎什么都没预到，锁内取数不变（实测修复无效）。

    读 state 用的是**不取锁**的 ``store.read_state``：这里只做「看一眼决定预热什么」，
    真正的一致性仍由锁内的 RMW 保证。
    """
    try:
        peek = store.read_state(cfg)
    except Exception:  # noqa: BLE001 - 读不到就让锁内按原语义报错
        peek = None
    date = (explicit_date or "").strip() or (peek.trade_date if peek else "") or None
    if not date:
        try:
            date = resolve_asof(ds, None)
        except Exception:  # noqa: BLE001 - 失败留给锁内按原语义报错
            date = None
    return prewarm_for_write(ds, peek, date, extra_codes=extra_codes, side=side)


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
