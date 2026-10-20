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
from .datasource import DataSource, DataUnavailable, NetworkForbiddenInLock

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


def _emit(
    args: argparse.Namespace,
    command: str,
    data: Any = None,
    error: str | None = None,
    *,
    exit_code: int | None = None,
) -> int:
    """统一输出：``--json`` → JSON 到 stdout；否则中文可读；返回退出码。

    ``exit_code``：**仅**给「命令成功、但有必须被脚本看见的问题」用
    （目前唯一用途是 `status` 检出**账实分叉**，见 N7）。
    默认仍是「有 error → 1，否则 0」。
    注意此时 JSON 里 ``ok`` **仍为 true**（命令确实成功了）而退出码为 1 ——
    这是**有意**的：`ok` 描述命令是否执行成功，退出码描述「是否需要人工处理」。
    脚本侧按退出码分流、按 ``ok`` 判断命令本身，两者不混。
    """
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
    if exit_code is not None:
        return int(exit_code)
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


def _period_choices() -> list[str]:
    """周期清单的**单一真源**：`chart.PERIODS_WITH_INTRADAY`（PRD F3 / `INV-56`）。

    为什么做成函数：`chart` 的导入保留**惰性**（非 chart 子命令不必加载它），
    但 `argparse` 的 `choices` 在 `build_parser()` 时就要值 —— 于是在该时点惰性取一次。

    历史上这里与 `webapp.VALID_PERIODS`、`chart.PERIODS` **各写一份**，
    改一处漏一处（Architect 第 1 轮实测指出；PRD F3 要求收敛为单一真源）。
    """
    from .chart import PERIODS_WITH_INTRADAY

    return list(PERIODS_WITH_INTRADAY)


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
    """锁外预热 —— 转发到 :func:`ptrade.service.prewarm_for_write`。

    真源已上移到 ``service``：**网页端与 CLI 都需要它**，而第一版只修了 CLI，
    独立复核实测网页写端点仍会在锁内取数并饿死并发写者（BLOCK-1 只修一半）。
    """
    return service.prewarm_for_write(ds, state, trade_date, extra_codes)


def _prewarm_before_write(
    cfg: AppConfig,
    ds: DataSource,
    explicit_date: str | None,
    extra_codes: Iterable[str] = (),
    *,
    side: str | None = None,
) -> list[str]:
    """锁外预热助手 —— 转发到 :func:`ptrade.service.prewarm_before_write`。"""
    return service.prewarm_before_write(cfg, ds, explicit_date, extra_codes, side=side)



def _resolve_asof(ds: DataSource, asof: str | None, notes: list[str] | None = None) -> str:
    """``--asof`` 未来函数防护 —— 转发到 :func:`ptrade.service.resolve_asof`。

    语义逐字一致（含 ``RuntimeError("NO_MARKET_DATA: ...")`` 与 15:30 回退规则）。
    """
    return service.resolve_asof(ds, asof, notes)


def _coerce_price_arg(raw: Any) -> Any:
    """把 ``--price`` 的原始字符串转成引擎要的形式（B，2026-10-14）。

    * ``None`` → ``None``（市价）
    * ``"live"``（大小写/空白不敏感）→ 原样返回该字符串，由引擎识别
      （``Engine.LIVE_PRICE_SENTINEL``）
    * 其它 → ``float()``；非法值给**明确**错误而不是 ``ValueError`` 崩栈

    **为什么不能只写 `type=float`**：argparse 的 `float("live")` 会抛
    `ValueError: could not convert string to float: 'live'`，用户看到的是 argparse 的
    内部报错，而不是「live 是什么」的说明。

    ``Engine`` 在 cli 里是**惰性导入**（函数内 `from .engine import Engine`），
    所以这里用同值常量而不是 ``Engine.LIVE_PRICE_SENTINEL``；
    `tests/test_cli.py` 有一条断言两者一致，防止哪天漂移。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() == "live":
        return "live"
    try:
        return float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"--price 必须是数字或 'live'（按当前快照价成交），收到 {raw!r}"
        ) from exc


def _is_live_price_arg(raw: Any) -> bool:
    """``--price`` 是否为「按实时快照价成交」（B）。

    与 ``Engine._is_live_price`` 同义；cli 里 `Engine` 是惰性导入，所以在这里独立判断。
    两者的**取值**由 `tests/test_live_price.py` 的一致性断言守住，防止漂移。
    """
    return isinstance(raw, str) and raw.strip().lower() == "live"


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

    # **N3 修复（独立复核）**：`--trade-date` 的交易日校验必须在**进锁前**做完。
    # 它要读 `is_trade_date` → 交易日历；在线 + 冷缓存时那是网络取数，
    # 放进临界区就等于「为了校验一个日期而持锁联网」（架构评审 line 163 要求外移）。
    #
    # 保持原有**错误优先级**：账户已存在时，「账户已存在」优先于「不是交易日」
    # （原本两条检查都在锁内，存在性在前）。所以这里先做一次**不取锁**的存在性预判：
    # 若会走「账户已存在」分支，就不做日期校验、直接交给锁内报那个错。
    start_date = getattr(args, "trade_date", None)
    _will_report_exists = os.path.isfile(path) and not args.reset
    if start_date and not _will_report_exists:
        start_date = str(start_date)[:10]
        try:
            _ds = _make_ds(_cfg_with_cash(cfg, args.cash), bool(getattr(args, "offline", False)))
            if not _ds.is_trade_date(start_date):
                return _emit(args, "init", {"path": path},
                             error=f"{start_date} 不是交易日（可用 --offline 读本地日历校验）")
        except Exception:  # noqa: BLE001 - 日历不可用时不阻塞建账
            pass
    elif start_date:
        start_date = str(start_date)[:10]

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
        # 校验已在锁外完成（见上），这里**不再取数**。
        if start_date:
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
    """账户状态。**M3 修复**：同时给出账实一致性，让分叉真的被用户看见。

    **N7**：检出**已知**账实分叉（``diverged is True``）时退出码为 **1** ——
    修前「分叉了但 rc=0」，脚本侧的 `if ! ptrade status` 完全看不出来，
    而分叉意味着「重试同一笔下单可能重复成交」。

    三态的处置（**不把「不知道」当成「没问题」**）：

    * ``diverged is True``  → rc **1**（必须人工处理）
    * ``diverged is None``  → rc **0**（**无法判定**：通常是正有写事务在跑。
      命令本身没失败，不该让脚本以为「坏了」；但 JSON 里 ``diverged=null``
      与 ``consistent=false`` 如实反映「没查成」）
    * ``diverged is False`` → rc 0

    JSON 里 ``ok`` **仍为 true**（命令确实成功了）—— ``ok`` 描述命令本身，
    退出码描述「是否需要人工处理」，两者不混。
    """
    state = _load_state(cfg)
    data = _state_summary(state)
    data["state_path"] = _state_path(cfg)
    consistency = service.consistency(cfg)
    data["ledger_consistency"] = consistency
    return _emit(
        args, "status", data,
        exit_code=1 if consistency.get("diverged") is True else None,
    )


def cmd_sync(args: argparse.Namespace, cfg: AppConfig) -> int:
    ds = _make_ds(cfg, bool(args.offline))
    end = args.end or ds.latest_trade_date()
    start = args.start or ds._offset(end, -400)
    result: dict[str, Any] = {"start": start, "end": end, "codes": {}, "failed": {}}
    for code in _split_codes(args.codes):
        try:
            frame = ds.daily(code, start, end, adjust=args.adjust, unadjust=bool(args.unadjust))
            result["codes"][code] = int(len(frame))
        except NetworkForbiddenInLock:
            # ★ **守卫错误必须上抛**（INV-42/45）—— 这是 `tests/test_wiring_invariants_v2.py::W1` 抓到的：
            # `NetworkForbiddenInLock` 是 `DataUnavailable` 的**子类**，若让它掉进下面的
            # `except DataUnavailable`，就会被**降级成「这条票没数据」**写进 `failed`，
            # 然后**继续下一条**，最后可能 `return _emit(...)` 给出**退出码 0 + 只带部分结果** ——
            # 用户看到「成功」，而数据是缺的。这比直接报错危险得多。
            raise
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
    _prewarm_before_write(cfg, ds, args.trade_date, extra_codes=(args.code,), side=args.side)

    def _mutate(state: AccountState | None) -> AccountState:
        if state is None:
            raise FileNotFoundError(f"未找到账户文件 {_state_path(cfg)}，请先执行 `python -m ptrade init`")
        price_arg = _coerce_price_arg(args.price)
        trade_date = args.trade_date or state.trade_date or _resolve_asof(ds, None)
        # **`--price live` 的交易日取自快照**（B，2026-10-14）：`resolve_asof` 在 15:30 前
        # 会回退到上一交易日，而引擎的停牌检查看的是 `td` 那天的行情 ——
        # 两者不一致会让 live 下单被判 SUSPENDED（实测），于是必须**同时**给
        # `--trade-date=今天` 才能用。以快照日期为准就一步到位。
        if _is_live_price_arg(price_arg) and not args.trade_date:
            trade_date = service.live_trade_date(ds, args.code)
        engine = _engine_for(Engine, cfg, ds, state, source="cli")
        if state.trade_date != trade_date:
            state = engine.roll_to(trade_date)
        request = OrderRequest(
            code=args.code, side=args.side, qty=int(args.qty),
            # `--price live` 原样传给引擎（它认 LIVE_PRICE_SENTINEL）；
            # 其它值仍是数字。**不要**在这里 float() —— 那会把 "live" 变成 ValueError。
            price=price_arg, trade_date=trade_date,
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
    _prewarm_before_write(cfg, ds, args.trade_date, extra_codes=(args.code,), side="close")

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
    """渲染 K 线/分时图。

    ★ **看盘路径**（`INV-52`）：asof 走 :func:`ptrade.service.viewer_asof`（**含今天**）。

    与其他子命令不同，看盘**必须**看到今天正在形成的那根 bar（用户明确要求：
    「交易日的时候看我也是要看到最新的」）。策略本身已抽到中立层
    （`service.viewer_asof`，architect R4）—— 本函数**只负责取值**：

    * 显式 ``--asof`` → 那里走 `resolve_asof`（守未来函数防护）；
    * 未给 ``--asof`` → 那里走 `DataSource.chart_asof`（今天**确有数据**时才是今天），并写明 notes。

    ⚠️ **本函数不再直接调用 `chart_asof`** —— 收敛后，`INV-52` 的白名单应以
    `service.viewer_asof` 为**唯一**入口（`webapp.api_chart` 是待收敛的第二处，见 architect R3/R4）。
    """
    build_chart = _import_chart()
    ds = _make_ds(cfg, bool(args.offline))
    notes: list[str] = []
    # ★ 看盘 asof 策略**只有一处实现**（`service.viewer_asof`，architect R4）：
    #   它内部对「显式 --asof」走 `_resolve_asof`（守未来函数防护），
    #   对「未给 --asof」走 `ds.chart_asof(code)`（数据驱动、含今天）并写明 notes。
    #   抽出来之前这段策略在 `cli.cmd_chart` 与 `webapp.api_chart` **各写一遍且已漂移**。
    asof = service.viewer_asof(ds, args.code, args.asof, notes)
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
    sp.add_argument(
        "--price",
        dest="price",
        default=None,
        metavar="PRICE|live",
        help="委托价；缺省 = 市价（用行情 last ± 滑点）；**live = 按当前快照价成交**"
             "（要求快照新鲜且当天有效，否则明确报错，不退回陈旧价）",
    )
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
    sp.add_argument("--period", default="daily", choices=_period_choices())
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
    sp.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1；**非回环地址默认拒绝**，需再加 --allow-remote）")
    sp.add_argument(
        "--allow-remote", action="store_true", default=False,
        help="显式放行非回环监听地址（**危险**：本服务无鉴权，同网段任何人都能查看账户、下单、平仓与结算）",
    )
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

    默认绑 ``127.0.0.1``（**无鉴权，这是唯一边界**）。
    ★ **非回环地址默认拒绝**（硬约束「仅本地监听」）：``--host 0.0.0.0`` 会**直接报错**，
    必须**显式**再加 ``--allow-remote`` 才放行 —— 修前只打印一次警告就照常绑定
    （ultraqa F-2 实测 `0.0.0.0` 绑成功、`('0.0.0.0', 60886)`）。

    .. note::
       这是**阻塞式**长驻命令，因此它返回服务退出码（``0`` 正常、``130`` Ctrl+C），
       而不是 ``--json`` 信封 —— 把长驻服务包进一次性 JSON 输出没有意义。
    """
    from . import webapp

    host = getattr(args, "host", None) or webapp.DEFAULT_HOST
    port = int(getattr(args, "port", None) or webapp.DEFAULT_PORT)
    offline = bool(getattr(args, "offline", False))
    lock_timeout = float(getattr(args, "lock_timeout", None) or webapp.DEFAULT_LOCK_TIMEOUT)
    allow_remote = bool(getattr(args, "allow_remote", False))
    if not webapp._is_loopback(host) and not allow_remote:      # noqa: SLF001 - 同一个包的守卫
        # 在**解析参数后立刻**给出可操作的错误（而不是等到绑定那一刻抛 ValueError 栈）
        print(
            f"拒绝绑定非回环地址 {host!r}：本服务**无鉴权**，同网段的任何人都可以查看账户、"
            f"下单、平仓与结算。\n"
            f"若确实要暴露到局域网，请显式再加 `--allow-remote`（自行承担风险）。",
            file=sys.stderr,
        )
        return 2
    try:
        return webapp.serve(cfg, host=host, port=port, offline=offline,
                            lock_timeout=lock_timeout, allow_remote=allow_remote)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


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
