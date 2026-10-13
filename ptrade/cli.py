"""ptrade.cli —— 命令行入口（INTERFACES.md §7）。

约定：

* ``python -m ptrade <cmd> ...``，``--json`` 输出 ``{"ok","command","data","error"}`` 到 stdout；
  **退出码 0/1**（业务拒单不算错误：``ok=true`` 且 ``data.orders[].status == "rejected"``）。
* 全局 ``--root <path>`` 可覆盖数据根目录；``--json`` / ``--root`` 在子命令前后均可写。
* ``--offline`` 透传到 ``DataSource(offline=True)``，**任何命令在 ``--offline`` 下不得联网**。
* ``engine`` / ``strategy`` / ``chart`` 均为**延迟导入**：模块未就绪时给明确错误而不是崩栈。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any, Iterable

from . import core
from . import service
from . import store
from .config import AccountConfig, AppConfig, load_config
from .core import AccountState, Side
from .datasource import DataSource, DataUnavailable

__all__ = ["main", "build_parser"]

STATE_DIR = "data"
STATE_FILE = "state.json"
LEDGER_FILE = "ledger.jsonl"


# --------------------------------------------------------------------------- #
# 输出 / 序列化
# --------------------------------------------------------------------------- #
def _jsonable(obj: Any) -> Any:
    """递归转 JSON 安全结构 —— 转发到 :func:`ptrade.service.jsonable`（W-7：消除分层倒置）。

    保留同名私有函数是为了**零改动**既有调用点与测试；真实实现只有一处（service）。
    """
    return service.jsonable(obj)

def _want_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _emit(args: argparse.Namespace, command: str, data: Any = None, error: str | None = None) -> int:
    """统一输出：``--json`` → JSON 到 stdout；否则中文可读；返回退出码。"""
    if _want_json(args):
        payload = {
            "ok": error is None,
            "command": command,
            "data": _jsonable(data) if data is not None else {},
            "error": error,
        }
        # allow_nan=False：非有限值已在 _jsonable 里转 null；若将来漏了会立刻报错而不是产出非法 JSON
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    elif error is not None:
        print(f"[错误] {error}", file=sys.stderr)
    else:
        _print_human(command, data or {})
    return 0 if error is None else 1


def _print_human(command: str, data: dict[str, Any]) -> None:
    if command in ("init", "status"):
        print(f"账户日期      : {data.get('trade_date') or '(未结算)'}")
        print(f"初始资金      : {data.get('initial_cash', 0):,.2f} 元")
        print(f"可用现金      : {data.get('available_cash', 0):,.2f} 元")
        print(f"冻结(待交收)  : {data.get('frozen_t1', 0):,.2f} 元")
        print(f"冻结(融券担保): {data.get('frozen_collateral', 0):,.2f} 元")
        print(f"融资负债      : {data.get('financing_debt', 0):,.2f} 元")
        print(f"多头市值      : {data.get('long_market_value', 0):,.2f} 元")
        print(f"融券市值      : {data.get('short_market_value', 0):,.2f} 元")
        print(f"总资产        : {data.get('total_assets', 0):,.2f} 元")
        print(f"总负债        : {data.get('total_debt', 0):,.2f} 元")
        print(f"净资产        : {data.get('net_assets', 0):,.2f} 元")
        ratio = data.get("maintenance_ratio")
        print(f"维持担保比例  : {'inf（无负债）' if ratio is None else f'{ratio:.4f}'}")
        leverage = data.get("leverage")
        print(f"杠杆          : {'inf' if leverage is None else f'{leverage:.4f}'}")
        print(f"累计盈亏      : {data.get('total_pnl', 0):,.2f} 元")
        positions = data.get("positions") or []
        if positions:
            print("\n持仓：")
            print(f"{'代码':<8}{'名称':<12}{'多股数':>10}{'可卖':>10}{'空股数':>10}{'成本':>12}{'现价':>12}")
            for pos in positions:
                print(
                    f"{pos.get('code', ''):<8}{str(pos.get('name', '')):<12}"
                    f"{pos.get('long_qty', 0):>10}{pos.get('long_available', 0):>10}"
                    f"{pos.get('short_qty', 0):>10}{pos.get('long_cost', 0):>12.3f}"
                    f"{pos.get('last_price', 0):>12.3f}"
                )
    elif command == "ledger":
        orders = data.get("orders") or []
        print(f"账本（最近 {len(orders)} 条 / 共 {data.get('total', 0)} 条）：")
        for order in orders:
            print(
                f"{order.get('ts', '')[:19]:<20}{order.get('code', ''):<8}"
                f"{order.get('side', ''):<12}{order.get('qty', 0):>8}"
                f"{order.get('price', 0):>10.3f}  {order.get('status', ''):<9}{order.get('error_code', '')}"
            )
    elif command == "risk":
        ratio = data.get("maintenance_ratio")
        leverage = data.get("leverage")
        print(f"状态          : {data.get('status')}")
        print(f"维持担保比例  : {'inf（无负债）' if ratio is None else ratio}")
        print(f"杠杆          : {'inf' if leverage is None else leverage}")
        alerts = data.get("alerts") or []
        print(f"告警 {len(alerts)} 条：")
        for alert in alerts:
            print(f"  [{alert.get('level')}] {alert.get('code')} {alert.get('message')}")
    elif command == "sync":
        print(f"已同步 {data.get('start')} ~ {data.get('end')}：")
        for code, rows in (data.get("codes") or {}).items():
            print(f"  {code}: {rows} 行")
    elif command in ("scan", "recommend"):
        for row in data.get("results") or data.get("recommendations") or []:
            print(
                f"{row.get('code', ''):<8}{str(row.get('name', '')):<12}"
                f"总分 {row.get('total', row.get('score', 0)):>6.2f}  {row.get('signal', row.get('action', ''))}"
            )
    elif command == "plan":
        for action in data.get("actions") or []:
            print(
                f"{action.get('code', ''):<8}{action.get('action', ''):<12}"
                f"{action.get('qty', 0):>8}  {action.get('reason', '')}"
            )
        for skipped in data.get("skipped") or []:
            print(f"[跳过] {skipped}")
    elif command in ("order", "settle", "close"):
        for row in data.get("orders") or []:
            print(
                f"{row.get('order_id', ''):<8}{row.get('code', ''):<8}{row.get('side', ''):<12}"
                f"{row.get('qty', 0):>8}{row.get('price', 0):>12.3f}  "
                f"{row.get('status', '')} {row.get('error_code', '')} {row.get('reason', '')}"
            )
    elif command == "chart":
        print(f"K 线图已生成：{data.get('path')}")
        print(f"标的 {data.get('code')} / 周期 {data.get('period')} / 基准日 {data.get('asof')}")
    else:
        print(json.dumps(_jsonable(data), ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# 状态与账本 IO
# --------------------------------------------------------------------------- #
def _state_path(cfg: AppConfig) -> str:
    """转发到 :func:`ptrade.service.state_path`。"""
    return service.state_path(cfg)

def _ledger_path(cfg: AppConfig) -> str:
    """转发到 :func:`ptrade.service.ledger_path`。"""
    return service.ledger_path(cfg)

def _load_state(cfg: AppConfig) -> AccountState:
    """读账户状态。**只读路径**：不取写锁（INV-26）。

    文件不存在时抛出与既有语义一致的可读错误（`status`/`scan` 等命令依赖它）。
    """
    state = store.read_state(cfg)
    if state is None:
        raise FileNotFoundError(f"未找到账户文件 {_state_path(cfg)}，请先执行 `python -m ptrade init`")
    return state


# `_save_state` 已删除（Critic P3 裁决）：它零调用点，而**保留它就是保留一个陷阱** ——
# 任何新写命令顺手调用它，就会退化成「锁外写」（丢掉读-改-写的原子性，即 Architect B1），
# 而且不会有任何测试变红。写路径的唯一入口是
# `store.locked_update(cfg, mutate, source="cli")`，见 cmd_init / cmd_order / cmd_settle / cmd_close。


# 注意：ledger.jsonl 的**写**归 engine.submit（§5），CLI 只读不写，
# 否则同一笔委托会被落账两次（已实测复现并修复）。


def _read_ledger(cfg: AppConfig, limit: int) -> tuple[list[dict[str, Any]], int]:
    path = _ledger_path(cfg)
    if not os.path.isfile(path):
        return [], 0
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    total = len(rows)
    return (rows[-limit:] if limit > 0 else rows), total


def _state_summary(state: AccountState) -> dict[str, Any]:
    """账户快照序列化 —— 转发到 :func:`ptrade.service.state_summary`。

    已逐字段比对（24 键、0 差异、JSON 字节级一致、持仓按 ``is_active`` 过滤），
    见 ``.omx/probe/verify_service_parity.py``。
    """
    return service.state_summary(state)

def _import_engine():
    try:
        from .engine import Engine, OrderRequest
    except ImportError as exc:  # pragma: no cover - 下游未就绪时的清晰报错
        raise RuntimeError(f"engine 模块不可用（{exc}）") from exc
    return Engine, OrderRequest


def _import_strategy():
    try:
        from . import strategy
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"strategy 模块不可用（{exc}）") from exc
    return strategy


def _import_chart():
    try:
        from .chart import build_chart
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"chart 模块不可用（{exc}）") from exc
    return build_chart


def _make_ds(cfg: AppConfig, offline: bool) -> DataSource:
    """转发到 :func:`ptrade.service.make_ds`。"""
    return service.make_ds(cfg, offline)

def _engine_for(engine_cls, cfg: AppConfig, ds: DataSource, state: AccountState, *, source: str = "cli"):
    """构造 Engine 并把 ledger 落账标注来源（**Critic W8**）。

    为什么需要：``core.Order`` **没有** ``source`` 字段，所以 CLI 下单落进
    ``ledger.jsonl`` 的行原本 18 个字段、**不含 ``source``**；而网页侧用
    ``webapp.attach_ledger_source`` 加了 ``source="web"``。结果同一份 ledger 里
    网页行有来源、CLI 行没有 —— 「谁下的单」不可审计。

    这里做与网页侧**同构**的钩子（CLI 不能 import ``webapp``：那是分层倒置，见 Critic W-7）。
    待 ``ptrade/service.py`` 落地后，两边应改为共同依赖 service 里的单一实现。
    """
    engine = engine_cls(cfg, ds, state)

    def _append_with_source(order) -> None:
        row = asdict(order)
        row["source"] = source
        store.append_jsonl(store.ledger_path(cfg), row)

    engine._append_ledger = _append_with_source  # noqa: SLF001 - 有意的落账钩子替换
    return engine


def prewarm_for_write(
    ds: DataSource,
    state: AccountState | None,
    trade_date: str | None,
    extra_codes: Iterable[str] = (),
) -> list[str]:
    """在**进入写锁之前**把写路径会用到的数据预热进缓存（架构评审 BLOCK-1）。

    为什么必须这么做：写命令把「读 → 改 → 写」整体放进 ``store.locked_update``，
    而引擎在这个临界区内会调 ``trade_dates`` / ``next_trade_date`` / ``quote`` /
    ``last_close`` / ``daily`` / ``spot``（实测 **6 次**）。**离线**时这些是本地夹具读
    （锁持有 ≈0.12s，无害）；但**在线 + 冷缓存**时它们是网络取数 —— 架构评审实测
    单段持锁 **24.97s**（另一次 >180s），而网页端 ``DEFAULT_LOCK_TIMEOUT`` 是 **10s**
    → 网页下单稳定 503，且**账户本身没有任何问题**。

    ``DataSource`` 的缓存落在磁盘（``_read_entry`` 先读盘），所以这里预热一次之后，
    锁内的同类调用会命中新鲜缓存而**不再联网**。

    参数：
      * ``state``：用于取出**全部持仓**代码 —— 引擎的 mark-to-market 会遍历所有持仓，
        不只本次下单的那只；
      * ``extra_codes``：本次操作涉及的代码（下单标的等），与持仓代码合并去重；
      * ``trade_date``：**必须在锁外算好**。若传 ``None``，几乎预不到东西（第一版就栽在这里：
        ``init`` 后 ``state.trade_date`` 为空 → 交易日由锁内的 ``_resolve_asof`` 才确定 →
        预热全被跳过，锁内仍是 6 次数据访问）。

    本函数**幂等**且**不抛**（best-effort）：真正的失败仍由锁内路径按原语义报错，
    这里只负责尽量让锁内不联网。返回成功预热的键列表（供测试与非空守卫断言「确实做了事」）。
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
    if trade_date:
        _try("next_trade_date", lambda: ds.next_trade_date(trade_date))
        _try("is_trade_date", lambda: ds.is_trade_date(trade_date))
        for code in all_codes:
            _try(f"quote:{code}", lambda c=code: ds.quote(c, trade_date))
            _try(f"last_close:{code}", lambda c=code: ds.last_close(c, trade_date))
            _try(f"daily:{code}", lambda c=code: ds.daily(c, None, trade_date))
        if all_codes:
            _try("spot", lambda: ds.spot())
    return warmed


def _prewarm_before_write(
    cfg: AppConfig,
    ds: DataSource,
    explicit_date: str | None,
    extra_codes: Iterable[str] = (),
) -> list[str]:
    """锁外预热助手：用**与锁内完全相同的逻辑**解析交易日，再预热。

    为什么必须「相同逻辑」：第一版预热直接用 ``args.trade_date``，而 ``init`` 后
    ``state.trade_date`` 为空、交易日是锁内 ``_resolve_asof`` 才定下来的 →
    预热时 ``trade_date=None`` → ``quote``/``last_close``/``daily`` 全被跳过，
    锁内仍是 6 次数据访问（实测：修复尝试无效）。这里改成先按锁内同款规则算出日期。
    """
    peek = store.read_state(cfg)
    date = explicit_date or (peek.trade_date if peek else "") or None
    if not date:
        try:
            date = _resolve_asof(ds, None)
        except Exception:  # noqa: BLE001 - 失败留给锁内按原语义报错
            date = None
    return prewarm_for_write(ds, peek, date, extra_codes=extra_codes)


def _resolve_asof(ds: DataSource, asof: str | None, notes: list[str] | None = None) -> str:
    """``--asof`` 未来函数防护 —— 转发到 :func:`ptrade.service.resolve_asof`。

    语义逐字一致（含 ``RuntimeError("NO_MARKET_DATA: ...")`` 与 15:30 回退规则）。
    """
    return service.resolve_asof(ds, asof, notes)

def _split_codes(text: str | None) -> list[str]:
    if not text:
        return []
    return [part.strip() for part in str(text).replace("，", ",").split(",") if part.strip()]


def _cfg_with_cash(cfg: AppConfig, cash: float | None) -> AppConfig:
    if cash is None:
        return cfg
    return dataclasses.replace(cfg, account=AccountConfig(initial_cash=float(cash)))


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace, cfg: AppConfig) -> int:
    """初始化账户。

    **B1 修复**：整个「存在性检查 → 建账 → 落盘」周期放进 ``store.locked_update``
    临界区。此前是裸的 read-modify-write，与网页端并发时会丢更新。
    """
    path = _state_path(cfg)
    os.makedirs(os.path.join(cfg.root, STATE_DIR), exist_ok=True)
    started: dict[str, Any] = {}

    def _mutate(_existing: AccountState | None) -> Any:
        # 存在性检查必须在锁内：否则两个并发 init 可能都通过检查后互相覆盖
        if os.path.isfile(path) and not args.reset:
            started["error"] = "账户已存在（使用 --reset 重建）"
            # **NO_WRITE**（不是 None）：`locked_update` 把 `None` 解释成「写回入参状态」，
            # 于是错误路径也会落盘 —— 而 `save_state` 会顺手刷新 ledger 水位，
            # 一次失败的 init 就能抹掉已检出的账实分叉（code-review H2，已复现）。
            return store.NO_WRITE
        built = core.new_state(_cfg_with_cash(cfg, args.cash))
        # trade_date 缺省**留空**（账户"未落定"）：首笔 order/settle 的 --trade-date 决定起点，
        # 因此既能从最近交易日开跑，也能从任意历史交易日复盘。
        # 若显式给了 --trade-date，则用它作为起点（复盘历史某日的推荐用法）。
        # 两种情况下 init 都不联网（日期校验仅在可用时用本地日历做，失败则放行）。
        start_date = getattr(args, "trade_date", None)
        if start_date:
            start_date = str(start_date)[:10]
            try:
                ds = _make_ds(_cfg_with_cash(cfg, args.cash), bool(getattr(args, "offline", False)))
                if not ds.is_trade_date(start_date):
                    started["error"] = f"{start_date} 不是交易日（可用 --offline 读本地日历校验）"
                    return store.NO_WRITE      # 同上：校验失败不得落盘
            except Exception:  # noqa: BLE001 - 日历不可用时不阻塞建账
                pass
            built.trade_date = start_date
        started["start"] = start_date
        return built

    state = store.locked_update(cfg, _mutate, source="cli")
    if "error" in started or not isinstance(state, AccountState):
        return _emit(args, "init", {"path": path}, error=started.get("error", "账户初始化未完成"))
    data = _state_summary(state)
    data["path"] = path
    data["reset"] = bool(args.reset)
    data["hint"] = (
        f"起点交易日 = {state.trade_date}" if started.get("start")
        else "trade_date 为空（未落定）：首笔 order/settle 的 --trade-date 决定起点"
    )
    return _emit(args, "init", data)

def cmd_status(args: argparse.Namespace, cfg: AppConfig) -> int:
    """账户状态。**M3 修复**：同时给出账实一致性，让分叉真的被用户看见。"""
    state = _load_state(cfg)
    data = _state_summary(state)
    data["state_path"] = _state_path(cfg)
    data["ledger_consistency"] = service.consistency(cfg)
    return _emit(args, "status", data)


def cmd_sync(args: argparse.Namespace, cfg: AppConfig) -> int:
    ds = _make_ds(cfg, bool(args.offline))
    end = args.end or ds.latest_trade_date()
    start = args.start or ds._offset(end, -400)
    result: dict[str, Any] = {"start": start, "end": end, "codes": {}, "failed": {}}
    for code in _split_codes(args.codes):
        try:
            frame = ds.daily(code, start, end, adjust=args.adjust, unadjust=bool(args.unadjust))
            result["codes"][code] = int(len(frame))
        except DataUnavailable as exc:
            result["failed"][code] = str(exc)
    if result["failed"] and not result["codes"]:
        detail = "; ".join(f"{code}: {msg}" for code, msg in result["failed"].items())
        return _emit(args, "sync", result, error=f"没有可用数据（离线模式下必须命中缓存或夹具）：{detail}")
    return _emit(args, "sync", result)


def cmd_scan(args: argparse.Namespace, cfg: AppConfig) -> int:
    strategy = _import_strategy()
    ds = _make_ds(cfg, bool(args.offline))
    notes: list[str] = []
    asof = _resolve_asof(ds, args.asof, notes)
    codes = _split_codes(args.codes) or list(ds.universe())
    scorer = strategy.CanslimScorer(cfg, ds, offline=bool(args.offline))
    results = scorer.score_many(codes, asof)
    ranked = strategy.rank(results, cfg)
    data = {
        "asof": asof,
        "adjust": "qfq",
        "asof_notes": notes,
        "count": len(results),
        "results": [_jsonable(r) for r in results],
        "rank": _jsonable(ranked),
    }
    return _emit(args, "scan", data)


def cmd_recommend(args: argparse.Namespace, cfg: AppConfig) -> int:
    strategy = _import_strategy()
    ds = _make_ds(cfg, bool(args.offline))
    notes: list[str] = []
    asof = _resolve_asof(ds, args.asof, notes)
    codes = _split_codes(args.codes) or list(ds.universe())
    scorer = strategy.CanslimScorer(cfg, ds, offline=bool(args.offline))
    results = scorer.score_many(codes, asof)
    recommendations = strategy.recommend(results, cfg)
    return _emit(args, "recommend", {
        "asof": asof, "adjust": "qfq", "asof_notes": notes,
        "recommendations": _jsonable(recommendations),
    })


def cmd_plan(args: argparse.Namespace, cfg: AppConfig) -> int:
    Engine, _ = _import_engine()
    strategy = _import_strategy()
    ds = _make_ds(cfg, bool(args.offline))
    notes: list[str] = []
    asof = _resolve_asof(ds, args.asof, notes)
    state = _load_state(cfg)
    if not state.trade_date:
        state.trade_date = asof
    codes = _split_codes(args.codes) or list(ds.universe())
    scorer = strategy.CanslimScorer(cfg, ds, offline=bool(args.offline))
    results = scorer.score_many(codes, asof)
    recommendations = strategy.recommend(results, cfg)
    engine = Engine(cfg, ds, state)
    plan = engine.build_plan(recommendations)
    data = _jsonable(plan)
    if isinstance(data, dict):
        data.setdefault("asof", asof)
        data["asof_notes"] = notes
    return _emit(args, "plan", data)


def cmd_order(args: argparse.Namespace, cfg: AppConfig) -> int:
    """下单。**B1 修复**：读-改-写整体进临界区。**BLOCK-1 修复**：临界区前预热数据。"""
    Engine, OrderRequest = _import_engine()
    ds = _make_ds(cfg, bool(args.offline))
    generated: dict[str, Any] = {}
    # 锁外预热（BLOCK-1）：交易日按锁内同款逻辑解析，并带上本次下单标的
    _prewarm_before_write(cfg, ds, args.trade_date, extra_codes=(args.code,))

    def _mutate(state: AccountState | None) -> AccountState:
        if state is None:
            raise FileNotFoundError(f"未找到账户文件 {_state_path(cfg)}，请先执行 `python -m ptrade init`")
        trade_date = args.trade_date or state.trade_date or _resolve_asof(ds, None)
        engine = _engine_for(Engine, cfg, ds, state, source="cli")
        if state.trade_date != trade_date:
            state = engine.roll_to(trade_date)
        request = OrderRequest(
            code=args.code, side=args.side, qty=int(args.qty),
            price=(None if args.price is None else float(args.price)), trade_date=trade_date,
        )
        order = engine.submit(request)
        generated["order"] = order
        generated["trade_date"] = trade_date
        return engine.state

    state = store.locked_update(cfg, _mutate, source="cli")
    # ledger.jsonl 由 engine.submit 负责落账（§5），CLI **不得**重复追加
    return _emit(
        args, "order",
        {
            "trade_date": generated.get("trade_date"),
            "orders": [_jsonable(generated["order"])],
            "state": _state_summary(state),
        },
    )


def cmd_settle(args: argparse.Namespace, cfg: AppConfig) -> int:
    """结算。**B1 修复**：读-改-写整体进临界区。**BLOCK-1 修复**：临界区前预热数据。"""
    Engine, _ = _import_engine()
    ds = _make_ds(cfg, bool(args.offline))
    _prewarm_before_write(cfg, ds, args.trade_date)

    def _mutate(state: AccountState | None) -> AccountState:
        if state is None:
            raise FileNotFoundError(f"未找到账户文件 {_state_path(cfg)}，请先执行 `python -m ptrade init`")
        engine = _engine_for(Engine, cfg, ds, state, source="cli")
        return engine.roll_to(args.trade_date)

    state = store.locked_update(cfg, _mutate, source="cli")
    return _emit(args, "settle", {"trade_date": state.trade_date, "orders": [], "state": _state_summary(state)})


def _risk_from_core(state: AccountState, cfg: AppConfig) -> dict[str, Any]:
    """engine 未就绪时的 core 兜底 —— 转发到 :func:`ptrade.service.risk_from_core`。"""
    return service.risk_from_core(state, cfg)

def cmd_risk(args: argparse.Namespace, cfg: AppConfig) -> int:
    state = _load_state(cfg)
    offline = bool(getattr(args, "offline", False))
    try:
        Engine, _ = _import_engine()
        ds = _make_ds(cfg, offline)
        report = Engine(cfg, ds, state).risk_report()
        report["source"] = "engine"
    except RuntimeError:
        report = _risk_from_core(state, cfg)
    return _emit(args, "risk", _jsonable(report))


def cmd_close(args: argparse.Namespace, cfg: AppConfig) -> int:
    """平仓。**B1 修复**：读-改-写整体进临界区。**BLOCK-1 修复**：临界区前预热数据。"""
    Engine, _ = _import_engine()
    ds = _make_ds(cfg, bool(getattr(args, "offline", False)))
    generated: dict[str, Any] = {}
    _prewarm_before_write(cfg, ds, args.trade_date, extra_codes=(args.code,))

    def _mutate(state: AccountState | None) -> AccountState:
        if state is None:
            raise FileNotFoundError(f"未找到账户文件 {_state_path(cfg)}，请先执行 `python -m ptrade init`")
        trade_date = args.trade_date or state.trade_date or _resolve_asof(ds, None)
        engine = _engine_for(Engine, cfg, ds, state, source="cli")
        if state.trade_date != trade_date:
            state = engine.roll_to(trade_date)
        orders = engine.close_position(args.code, trade_date, float(args.ratio))
        generated["orders"] = orders
        generated["trade_date"] = trade_date
        return engine.state

    state = store.locked_update(cfg, _mutate, source="cli")
    # 平仓内部走 submit → ledger 已由引擎落账，CLI 不重复追加
    return _emit(
        args, "close",
        {
            "trade_date": generated.get("trade_date"),
            "orders": _jsonable(generated["orders"]),
            "state": _state_summary(state),
        },
    )


def cmd_ledger(args: argparse.Namespace, cfg: AppConfig) -> int:
    rows, total = _read_ledger(cfg, int(args.limit))
    return _emit(args, "ledger", {"orders": rows, "total": total, "path": _ledger_path(cfg)})


def cmd_chart(args: argparse.Namespace, cfg: AppConfig) -> int:
    build_chart = _import_chart()
    ds = _make_ds(cfg, bool(args.offline))
    notes: list[str] = []
    asof = _resolve_asof(ds, args.asof, notes)
    out = args.out or os.path.join(cfg.root, "reports", f"chart_{args.code}_{asof}.html")
    path = build_chart(cfg, ds, args.code, asof, period=args.period, out=out)
    path = os.path.abspath(str(path))
    return _emit(args, "chart", {
        "path": path, "code": args.code, "asof": asof, "period": args.period,
        "adjust": "qfq", "asof_notes": notes,
    })


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptrade", description="A 股 CANSLIM 模拟盘（T+1 / 融资融券 / 风控 / 打分 / K 线图）"
    )
    parser.add_argument("--root", default=None, help="数据根目录（优先 PTRADE_HOME，其次源码检出根，否则 ~/.ptrade）")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON 输出")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--root", default=argparse.SUPPRESS)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return sp

    sp = add("init", "初始化模拟账户")
    sp.add_argument("--cash", type=float, default=None, help="初始资金（默认 500000）")
    sp.add_argument("--reset", action="store_true", help="覆盖已有账户")
    sp.add_argument(
        "--trade-date", dest="trade_date", default=None,
        help="起始交易日 YYYY-MM-DD（默认留空：由首笔 order/settle 的 --trade-date 决定，便于复盘历史某日）",
    )
    sp.add_argument("--offline", action="store_true")

    add("status", "查看账户状态")

    sp = add("sync", "预取日线数据到缓存")
    sp.add_argument("--codes", required=True)
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--adjust", default="qfq")
    sp.add_argument("--unadjust", action="store_true", help="取不复权真实价")
    sp.add_argument("--offline", action="store_true")

    for name, help_text in (("scan", "CANSLIM 打分"), ("recommend", "生成交易建议")):
        sp = add(name, help_text)
        sp.add_argument("--codes", default=None)
        sp.add_argument("--asof", default=None)
        sp.add_argument("--offline", action="store_true")

    sp = add("plan", "生成建仓/调仓计划")
    sp.add_argument("--codes", default=None)
    sp.add_argument("--asof", default=None)
    sp.add_argument("--offline", action="store_true")

    sp = add("order", "下单")
    sp.add_argument("--code", required=True)
    sp.add_argument("--side", required=True, choices=[s.value for s in Side])
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--price", type=float, default=None, help="缺省 = 市价（收盘价 ± 滑点）")
    sp.add_argument("--trade-date", dest="trade_date", default=None)
    sp.add_argument("--offline", action="store_true")

    sp = add("settle", "推进到新交易日（T+1 结算 / 计息 / mark-to-market）")
    sp.add_argument("--trade-date", dest="trade_date", required=True)
    sp.add_argument("--offline", action="store_true")

    sp = add("risk", "风险报告")
    sp.add_argument("--offline", action="store_true")

    sp = add("close", "平仓")
    sp.add_argument("--code", required=True)
    sp.add_argument("--ratio", type=float, default=1.0)
    sp.add_argument("--trade-date", dest="trade_date", default=None)
    sp.add_argument("--offline", action="store_true")

    sp = add("ledger", "查看成交流水")
    sp.add_argument("--limit", type=int, default=20)

    sp = add("chart", "生成离线单文件 K 线图（HTML）")
    sp.add_argument("--code", required=True)
    sp.add_argument("--period", default="daily", choices=["daily", "weekly", "monthly"])
    sp.add_argument("--asof", default=None)
    sp.add_argument("--out", default=None)
    sp.add_argument("--offline", action="store_true")

    # ---- W5 集成：搜索 / 自选池 / 网页端 ----
    sp = add("search", "搜索股票（代码前缀或中文名）")
    sp.add_argument("query", help="代码片段（如 600519）或名称（如 茅台）")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--refresh", action="store_true", help="强制刷新全市场名称表（需联网）")
    sp.add_argument("--offline", action="store_true")

    sp = add("watchlist", "自选池增删查")
    # 用**扁平**参数而不是嵌套子解析器：嵌套的 add/remove/list 子解析器拿不到全局
    # `--root/--json/--offline`，于是 `watchlist add X --json` 会报 unrecognized arguments。
    # 扁平 `--action` 与既有 `close --ratio` 的写法一致。
    sp.add_argument("code", nargs="?", default=None,
                    help="股票代码（--action add/remove 时必填）")
    sp.add_argument("--action", choices=["add", "remove", "list"], default="list")
    sp.add_argument("--name", default="")
    sp.add_argument("--note", default="")
    # 自选池本身只读写 ``<root>/data/watchlist.json``、不联网，但全局 ``--offline``
    # 必须被接受：否则 `watchlist list --offline` 会以退出码 2 报 unrecognized arguments，
    # 而调用方（脚本/网页/用户）没有理由知道「这个命令恰好不需要它」。
    sp.add_argument("--offline", action="store_true")

    sp = add("serve", "启动本地网页端（默认绑 127.0.0.1，无鉴权）")
    sp.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1；改成其它值会让同网段可下单）")
    sp.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    sp.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=None)
    sp.add_argument("--offline", action="store_true",
                    help="硬闸：网页端一律不联网（请求级 ?offline= 只能更严不能更松）")

    return parser


# --------------------------------------------------------------------------- #
# 网页端 / 搜索 / 自选池（W5 集成）
# --------------------------------------------------------------------------- #
def cmd_search(args: argparse.Namespace, cfg: AppConfig) -> int:
    """搜索股票（代码前缀 / 中文名子串）。

    ``--offline`` 只读本地缓存；无缓存则**明确报错**（沿用 ``DataUnavailable`` 语义，
    不得静默返回空 —— INV-28：区分「没搜到」与「拿不到数据」）。
    """
    from . import search as search_mod

    offline = bool(getattr(args, "offline", False))
    limit = int(getattr(args, "limit", 30) or 30)
    rows = search_mod.search_stocks(
        cfg, args.query, offline=offline, limit=limit,
        force_refresh=bool(getattr(args, "refresh", False)),
    )
    return _emit(args, "search", {
        "query": args.query,
        "count": len(rows),
        "results": _jsonable(rows),
        "offline": offline,
    })


def cmd_watchlist(args: argparse.Namespace, cfg: AppConfig) -> int:
    """自选池增删查（幂等；删不存在的 code 不报错）。"""
    from . import watchlist as wl

    action = getattr(args, "action", None) or "list"
    code = getattr(args, "code", None)
    if action in ("add", "remove") and not code:
        return _emit(args, f"watchlist.{action}", None, error=f"--action {action} 需要提供股票代码")
    if action == "add":
        doc = wl.add_to_watchlist(cfg, code, name=getattr(args, "name", "") or "",
                                  note=getattr(args, "note", "") or "")
        return _emit(args, "watchlist.add", {
            "count": len(doc.get("items", [])), "items": _jsonable(doc.get("items", [])),
        })
    if action == "remove":
        doc = wl.remove_from_watchlist(cfg, code)
        return _emit(args, "watchlist.remove", {
            "count": len(doc.get("items", [])), "items": _jsonable(doc.get("items", [])),
        })
    items = wl.list_watchlist(cfg)
    return _emit(args, "watchlist.list", {"count": len(items), "items": _jsonable(items)})


def cmd_serve(args: argparse.Namespace, cfg: AppConfig) -> int:
    """启动本地网页端（``python -m ptrade serve``）。

    默认绑 ``127.0.0.1``（**无鉴权，这是唯一边界**）；``--host`` 改变时由
    ``webapp.banner`` 打印警告。

    .. note::
       这是**阻塞式**长驻命令，因此它返回服务退出码（``0`` 正常、``130`` Ctrl+C），
       而不是 ``--json`` 信封 —— 把长驻服务包进一次性 JSON 输出没有意义。
    """
    from . import webapp

    host = getattr(args, "host", None) or webapp.DEFAULT_HOST
    port = int(getattr(args, "port", None) or webapp.DEFAULT_PORT)
    offline = bool(getattr(args, "offline", False))
    lock_timeout = float(getattr(args, "lock_timeout", None) or webapp.DEFAULT_LOCK_TIMEOUT)
    return webapp.serve(cfg, host=host, port=port, offline=offline,
                        lock_timeout=lock_timeout)


_HANDLERS = {
    "init": cmd_init,
    "status": cmd_status,
    "sync": cmd_sync,
    "scan": cmd_scan,
    "recommend": cmd_recommend,
    "plan": cmd_plan,
    "order": cmd_order,
    "settle": cmd_settle,
    "risk": cmd_risk,
    "close": cmd_close,
    "ledger": cmd_ledger,
    "chart": cmd_chart,
    "search": cmd_search,
    "watchlist": cmd_watchlist,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None)
    if not command:
        parser.print_help()
        return 0
    try:
        cfg = load_config(getattr(args, "root", None))
        return _HANDLERS[command](args, cfg)
    except Exception as exc:  # noqa: BLE001 - 统一转成结构化错误 + 退出码 1
        return _emit(args, command, None, error=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
