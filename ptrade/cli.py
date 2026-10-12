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
from datetime import datetime
from typing import Any

from . import core
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
    """递归转 JSON 安全结构（CanslimResult/FactorScore 用 to_dict()，dataclass 用 asdict）。"""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        # CR-9：非有限浮点一律序列化为 null（JSON 无 Infinity/NaN；禁止裸 Infinity）
        return obj if math.isfinite(obj) else None
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return _jsonable(obj.to_dict())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    item = getattr(obj, "item", None)  # numpy 标量
    if callable(item):
        try:
            return _jsonable(item())
        except Exception:  # noqa: BLE001
            pass
    return str(obj)


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
    return os.path.join(cfg.root, STATE_DIR, STATE_FILE)


def _ledger_path(cfg: AppConfig) -> str:
    return os.path.join(cfg.root, STATE_DIR, LEDGER_FILE)


def _load_state(cfg: AppConfig) -> AccountState:
    path = _state_path(cfg)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"未找到账户文件 {path}，请先执行 `python -m ptrade init`")
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return core.state_from_document(doc, cfg)


def _save_state(cfg: AppConfig, state: AccountState) -> str:
    path = _state_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(core.state_document(state), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


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
        "maintenance_ratio": None if ratio == float("inf") else ratio,
        "leverage": state.leverage,
        "margin_required": state.margin_required,
        "margin_available": state.margin_available,
        "total_pnl": state.total_pnl,
        "margin_call": state.margin_call,
        "forced_liquidation": state.forced_liquidation,
        "positions": [
            {
                "code": pos.code,
                "name": pos.name,
                "long_qty": pos.long_qty,
                "long_available": pos.long_available,
                "short_qty": pos.short_qty,
                "short_available": pos.short_available,
                "long_cost": pos.long_cost,
                "short_cost": pos.short_cost,
                "last_price": pos.last_price,
                "long_value": pos.long_value,
                "short_value": pos.short_value,
                "long_pnl": pos.long_pnl,
                "short_pnl": pos.short_pnl,
            }
            for pos in state.positions.values()
            if pos.is_active
        ],
    }


# --------------------------------------------------------------------------- #
# 延迟导入（模块未就绪时给明确错误）
# --------------------------------------------------------------------------- #
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
    return DataSource(cfg, offline=offline)


MARKET_CLOSE = (15, 30)


def _resolve_asof(ds: DataSource, asof: str | None, notes: list[str] | None = None) -> str:
    """``--asof`` 未来函数防护（v3.1）：只允许**已收盘**交易日。

    * 缺省 = ``ds.latest_trade_date()``（其本身已裁剪到"最近已收盘交易日"）；
    * 显式传入当天且当前时间 < 15:30 → 自动回退到上一交易日，并在 ``notes`` 里标注；
    * 传入未来日期 → 报 ``NO_MARKET_DATA``。
    """
    day = str(asof)[:10] if asof else ds.latest_trade_date()
    today = ds._today()
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
    path = _state_path(cfg)
    if os.path.isfile(path) and not args.reset:
        return _emit(args, "init", {"path": path}, error="账户已存在（使用 --reset 重建）")
    cfg = _cfg_with_cash(cfg, args.cash)
    state = core.new_state(cfg)
    # trade_date 缺省**留空**（账户"未落定"）：首笔 order/settle 的 --trade-date 决定起点，
    # 因此既能从最近交易日开跑，也能从任意历史交易日复盘。
    # 若显式给了 --trade-date，则用它作为起点（复盘历史某日的推荐用法）。
    # 两种情况下 init 都不联网（日期校验仅在可用时用本地日历做，失败则放行）。
    start = getattr(args, "trade_date", None)
    if start:
        start = str(start)[:10]
        try:
            ds = _make_ds(cfg, bool(getattr(args, "offline", False)))
            if not ds.is_trade_date(start):
                return _emit(args, "init", {"path": path}, error=f"{start} 不是交易日（可用 --offline 读本地日历校验）")
        except Exception:  # noqa: BLE001 - 日历不可用时不阻塞建账
            pass
        state.trade_date = start
    _save_state(cfg, state)
    data = _state_summary(state)
    data["path"] = path
    data["reset"] = bool(args.reset)
    data["hint"] = (
        f"起点交易日 = {state.trade_date}" if start
        else "trade_date 为空（未落定）：首笔 order/settle 的 --trade-date 决定起点"
    )
    return _emit(args, "init", data)


def cmd_status(args: argparse.Namespace, cfg: AppConfig) -> int:
    state = _load_state(cfg)
    data = _state_summary(state)
    data["state_path"] = _state_path(cfg)
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
    Engine, OrderRequest = _import_engine()
    ds = _make_ds(cfg, bool(args.offline))
    state = _load_state(cfg)
    trade_date = args.trade_date or state.trade_date or _resolve_asof(ds, None)
    engine = Engine(cfg, ds, state)
    if state.trade_date != trade_date:
        state = engine.roll_to(trade_date)
    request = OrderRequest(
        code=args.code, side=args.side, qty=int(args.qty),
        price=(None if args.price is None else float(args.price)), trade_date=trade_date,
    )
    order = engine.submit(request)
    state = engine.state
    _save_state(cfg, state)
    # ledger.jsonl 由 engine.submit 负责落账（§5），CLI **不得**重复追加
    return _emit(
        args, "order",
        {"trade_date": trade_date, "orders": [_jsonable(order)], "state": _state_summary(state)},
    )


def cmd_settle(args: argparse.Namespace, cfg: AppConfig) -> int:
    Engine, _ = _import_engine()
    ds = _make_ds(cfg, bool(args.offline))
    state = _load_state(cfg)
    engine = Engine(cfg, ds, state)
    state = engine.roll_to(args.trade_date)
    _save_state(cfg, state)
    return _emit(args, "settle", {"trade_date": state.trade_date, "orders": [], "state": _state_summary(state)})


def _risk_from_core(state: AccountState, cfg: AppConfig) -> dict[str, Any]:
    """engine 未就绪时的 core 兜底（口径与 engine.risk_report 一致）。"""
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
        "positions": _state_summary(state)["positions"],
        "source": "core",
    }


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
    Engine, _ = _import_engine()
    ds = _make_ds(cfg, bool(getattr(args, "offline", False)))
    state = _load_state(cfg)
    trade_date = args.trade_date or state.trade_date or _resolve_asof(ds, None)
    engine = Engine(cfg, ds, state)
    if state.trade_date != trade_date:
        state = engine.roll_to(trade_date)
    orders = engine.close_position(args.code, trade_date, float(args.ratio))
    state = engine.state
    _save_state(cfg, state)
    # 平仓内部走 submit → ledger 已由引擎落账，CLI 不重复追加
    return _emit(
        args, "close",
        {"trade_date": trade_date, "orders": _jsonable(orders), "state": _state_summary(state)},
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

    return parser


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
