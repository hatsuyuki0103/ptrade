# -*- coding: utf-8 -*-
"""ptrade.webapp —— 本地网页服务（HTTP + ``/api/*``）。

契约真源：``.omx/plans/prd-ptrade-web.md`` §5.4（端点表 + 响应契约）。

设计要点
--------
1. **写路径全部经 ``store``**（INV-39）：``/api/order``、``/api/close``、``/api/settle`` 的
   read→mutate→write 临界区一律走 ``store.locked_update``，与 CLI 跨进程互斥；watchlist 的
   变更走 ``store.state_lock``（等价锁 + watchlist 自身的原子写）。
   **只读路径不取写锁**（INV-34）：``/api/state``、``/api/positions``、``/api/risk``、
   ``/api/search``、``/api/chart`` 先尝试直接读；读不到再退回锁内读一次（避免与写入者撕裂）。
2. **INV-40：不自己写第二份序列化器**。``/api/state`` 与 ``/api/risk`` 分别复用
   ``cli._state_summary``（+ ``state_path``，与 ``cmd_status`` 同构造）与 ``cmd_risk`` 的
   ``engine.risk_report()`` / ``service.risk_from_core`` 口径，字段与 ``status --json`` /
   ``risk --json`` 的 ``data`` 逐字段一致。JSON 序列化统一走 ``cli._jsonable``。
3. **INV-41：ledger 带 ``source``**。``core.Order`` 无 ``source`` 字段，故本模块用一个只覆盖
   ``_append_ledger`` 的 ``Engine`` 子类（``_sourced_engine_class()``）把 ``source="web"``
   写进同一份 ``<root>/data/ledger.jsonl``。**这是 W3 侧的适配层**：一旦 engine 原生支持
   ``source``，删掉该子类即可（``attach_ledger_source(engine, source)`` 亦可供 CLI 复用）。

HTTP 语义
---------
* ``200``：成功；**业务拒单也是 200**（``ok=true`` 且 ``data.orders[].status == "rejected"``）；
  以及"业务上暂时做不了"（如尚未 ``init``、数据源不可用）→ ``ok=false`` + ``error``。
* ``400``：请求参数非法（缺 ``code``、``qty`` 非整数 / 非 100 整数倍、``side`` 非法、``period`` 非法…）。
* ``503``：写锁超时（``error`` 含 ``STATE_LOCK_TIMEOUT``）。
* ``500``：未预期异常。
* ``GET /api/chart`` 是**唯一**不返回 JSON 信封的端点：直接内联返回单文件 HTML（W9）。

启动
----
``serve(cfg, host="127.0.0.1", port=8765, offline=False)``；``host != 127.0.0.1`` 时启动横幅
必须打印警告（R-2：网页端无鉴权，误绑公网 = 同网段任何人可下单）。
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .datasource import DataUnavailable, NetworkForbiddenInLock

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_LOCK_TIMEOUT",
    "WebApp",
    "Response",
    "attach_ledger_source",
    "banner",
    "create_server",
    "serve",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_LOCK_TIMEOUT = 10.0
VALID_PERIODS = ("daily", "weekly", "monthly")
_BODY_LIMIT = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 依赖注入缝隙（便于测试；也是 INV-40「同一序列化器」的落点）
# --------------------------------------------------------------------------- #
def _store():
    """惰性取 ``ptrade.store``（W1 地基）。测试可替换本函数注入假 store。"""
    from . import store

    return store


def _svc():
    """惰性取 ``ptrade.service``（W-7：网页端依赖**中立服务层**，不再依赖 cli 的私有助手）。

    此前这里通过 ``_cli()`` 调 ``cli`` 的私有函数（``_make_ds`` / ``_state_path`` /
    ``_state_summary`` / ``_jsonable`` / ``_resolve_asof``，共 12 处），属于**分层倒置**：
    一个 UI 层反向依赖另一个 UI 层的内部实现。现在这些助手的真源在 ``ptrade.service``，
    ``cli`` 只是转发的薄包装。
    """
    from . import service

    return service




def attach_ledger_source(engine, source: str) -> None:
    """把 ``Engine`` 实例的 ledger 落账钩子换成带 ``source`` 标注的版本（INV-41）。

    供网页侧（``source="web"``）与 CLI 侧（``source="cli"``）复用；不改变 engine 的其它行为。
    """

    def _append(order) -> None:
        row = asdict(order)
        row["source"] = source
        path = os.path.join(engine.cfg.root, "data", "ledger.jsonl")
        _store().append_jsonl(path, row)

    engine._append_ledger = _append          # 实例级覆盖：只影响本实例，不改 engine.py


# --------------------------------------------------------------------------- #
# 请求 / 响应
# --------------------------------------------------------------------------- #
class _ParamError(ValueError):
    """请求参数非法 → HTTP 400。"""


@dataclass
class Response:
    """统一的响应载体。``payload`` 非空时按 JSON 信封输出。"""

    status: int
    payload: dict | None = None
    body: bytes | None = None
    content_type: str = "application/json; charset=utf-8"

    def json_bytes(self) -> bytes:
        assert self.payload is not None
        return json.dumps(self.payload, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def to_bytes(self) -> bytes:
        return self.body if self.body is not None else self.json_bytes()


def _envelope(command: str, data: Any = None, *, error: str | None = None) -> dict:
    """与 CLI ``--json`` 同构的信封：``{"ok","command","data","error"}``。"""
    return {
        "ok": error is None,
        "command": command,
        "data": _svc().jsonable(data) if data is not None else {},
        "error": error,
    }


# --------------------------------------------------------------------------- #
# 参数解析（HTTP 层：结构性问题 → 400）
# --------------------------------------------------------------------------- #
def _as_bool(value: Any, default: bool) -> bool:
    """宽松布尔解析。

    **必须处理 list**：``parse_qs`` 返回的查询值永远是 list（``{'offline': ['true']}``），
    直接 ``str(['true'])`` 得到 ``"['true']"`` → 匹配失败 → **静默回落默认值**。
    那会让请求级 ``?offline=`` 无声失效（Critic 的 W-5 / V1 担心的正是这条路径）。
    """
    if isinstance(value, (list, tuple)):
        if not value:
            return default
        value = value[0]
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


def _parse_int(value: Any, field: str) -> int:
    """严格整数：拒绝 bool、小数、非数字字符串（W4：``qty`` 非整数 → 400）。"""
    if isinstance(value, bool) or value is None:
        raise _ParamError(f"{field} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise _ParamError(f"{field} 必须是整数（收到 {value!r}）")
        return int(value)
    text = str(value).strip()
    if text.startswith("-"):
        digits = text[1:]
    else:
        digits = text
    if not digits.isdigit():
        raise _ParamError(f"{field} 必须是整数（收到 {value!r}）")
    return int(text)


def _parse_float(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _ParamError(f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _ParamError(f"{field} 必须是数字（收到 {value!r}）") from None
    if not math.isfinite(number) or number <= 0:
        raise _ParamError(f"{field} 必须是正有限数（收到 {value!r}）")
    return number


def _require_text(body: dict, field: str) -> str:
    value = body.get(field)
    text = "" if value is None else str(value).strip()
    if not text:
        raise _ParamError(f"缺少必填参数 {field}")
    return text


def _codes_from(body: dict) -> list[str]:
    """接受 ``codes``（列表 / 逗号串）、``code``（单个）。"""
    raw = body.get("codes", body.get("code"))
    if raw is None:
        raise _ParamError("缺少必填参数 codes")
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        items = [part.strip() for part in str(raw).replace("，", ",").split(",")]
    codes = [c for c in items if c]
    if not codes:
        raise _ParamError("codes 为空")
    return codes


# --------------------------------------------------------------------------- #
# WebApp
# --------------------------------------------------------------------------- #
class WebApp:
    """无状态路由层：``dispatch(method, path, query, body) -> Response``。

    既不监听端口也不持有连接，便于直接单测；HTTP 层（``create_server``）只做协议适配。
    """

    def __init__(
        self,
        cfg,
        *,
        offline: bool = False,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ):
        self.cfg = cfg
        self.offline = bool(offline)
        self.lock_timeout = float(lock_timeout)
        # 真实监听地址，由 create_server 在 bind 之后回填（W-10：A1 判据要能机械读出）。
        # 不能写死 DEFAULT_HOST:DEFAULT_PORT —— --port 改变后 /api/health 会报错地址。
        self.bound_address: tuple[str, int] | None = None

    # ---- 依赖构造 ----
    def _ds(self):
        return _svc().make_ds(self.cfg, self.offline)

    def _read_state(self):
        """只读账户状态；不存在返回 ``None``（不取写锁 —— INV-34）。"""
        return _store().read_state(self.cfg)

    def _state_missing(self) -> Response:
        return Response(200, _envelope(
            "state", None, error=f"未找到账户文件 {_svc().state_path(self.cfg)}，请先执行 `python -m ptrade init`"
        ))

    # ==================================================================
    # 只读端点
    # ==================================================================
    def api_health(self, query, body) -> Response:
        """探活 + **回报真实监听地址**（PRD §9.3 W-10：A1 要可机械判定）。

        不能写死 ``DEFAULT_HOST:DEFAULT_PORT`` —— 那样 ``--port`` 改变监听端口后
        ``/api/health`` 仍报默认值，判据就失效了（实测：``--port 8791`` 时它报 8765）。
        真实地址由 :func:`create_server` 回填到 ``app.bound_address``；
        ``port=0``（系统分配）时同样正确。
        """
        addr = self.bound_address
        host, port = addr if addr else (DEFAULT_HOST, DEFAULT_PORT)
        return Response(200, _envelope("health", {
            "status": "ok",
            "root": self.cfg.root,
            "offline": self.offline,
            "bind": f"{host}:{port}",
            "source": "web",
        }))

    def api_state(self, query, body) -> Response:
        """INV-40：``data`` 与 ``ptrade status --json`` 的 ``data`` 逐字段一致。"""
        state = self._read_state()
        if state is None:
            return self._state_missing()
        data = _svc().state_summary(state)              # 同一序列化器（INV-40）
        data["state_path"] = _svc().state_path(self.cfg)  # 与 cmd_status 同构造
        # M3：与 `ptrade status --json` **同样**给出账实一致性（INV-40 要求逐字段一致，
        # 所以两处必须一起加；只加一边就会打红既有的「逐字段比对」测试）
        data["ledger_consistency"] = _svc().consistency(self.cfg)
        return Response(200, _envelope("status", data))

    def api_positions(self, query, body) -> Response:
        state = self._read_state()
        if state is None:
            return self._state_missing()
        positions = _svc().state_summary(state)["positions"]
        return Response(200, _envelope("positions", {
            "positions": positions,
            "count": len(positions),
            "trade_date": state.trade_date,
            "state_path": _svc().state_path(self.cfg),
        }))

    def api_risk(self, query, body) -> Response:
        """与 ``risk --json`` 同构（engine.risk_report 优先，core 兜底）。"""
        state = self._read_state()
        if state is None:
            return self._state_missing()
        report: dict
        try:
            from .engine import Engine

            report = Engine(self.cfg, self._ds(), state).risk_report()
            report["source"] = "engine"
        except RuntimeError:                              # engine 不可用 → core 兜底
            report = _svc().risk_from_core(state, self.cfg)
        return Response(200, _envelope("risk", _svc().jsonable(report)))

    def api_search(self, query, body) -> Response:
        from .search import search_stocks

        q = (query.get("q") or query.get("query") or [""])
        text = (q[0] if isinstance(q, list) else q) or ""
        # ``parse_qs`` 的值永远是 list —— 必须解包。否则 _parse_int 收到 ['2']，
        # 报「limit 必须是整数（收到 ['2']）」，使 limit 参数**完全不可用**（实测）。
        limit_raw = query.get("limit", 30)
        if isinstance(limit_raw, list):
            limit_raw = limit_raw[0] if limit_raw else 30
        limit = _parse_int(limit_raw, "limit")
        if limit <= 0:
            raise _ParamError("limit 必须为正整数")
        # **进程级 offline 是下界**（V1 硬闸）：`--offline` 起服务时，请求级
        # `?offline=false` 不得解除它（实测修前可解除：offline 返回 False、照常联网）。
        # 请求级只能**更严**（进程在线时可用 ?offline=true 单次收紧）。
        offline = _as_bool(query.get("offline"), self.offline) or self.offline
        try:
            rows = search_stocks(self.cfg, text, offline=offline, limit=limit)
        except NetworkForbiddenInLock as exc:
            # 守卫错误 → 409（同 dispatch 的映射；见那里的说明）
            return Response(409, _envelope("search", None, error=f"NETWORK_IN_LOCK: {exc}"))
        except DataUnavailable as exc:                    # 业务态（非 500）
            return Response(200, _envelope("search", None, error=str(exc)))
        except Exception:
            raise
        return Response(200, _envelope("search", {
            "query": text, "offline": offline, "count": len(rows), "results": rows,
        }))

    def api_chart(self, query, body) -> Response:
        """W9：**内联返回**单文件 HTML（不得返回路径字符串冒充）。"""
        from .chart import build_chart

        code = (query.get("code") or [""])
        code = (code[0] if isinstance(code, list) else code) or ""
        if not str(code).strip():
            raise _ParamError("缺少必填参数 code")
        period = (query.get("period") or ["daily"])
        period = (period[0] if isinstance(period, list) else period) or "daily"
        if period not in VALID_PERIODS:
            raise _ParamError(f"period 必须是 {'/'.join(VALID_PERIODS)} 之一（收到 {period!r}）")
        ds = self._ds()
        notes: list[str] = []
        asof_query = query.get("asof")
        asof_query = (asof_query[0] if isinstance(asof_query, list) else asof_query) or None
        asof = _svc().resolve_asof(ds, asof_query, notes)
        out = os.path.join(self.cfg.root, "reports", f"chart_{code}_{asof}.html")
        path = os.path.abspath(str(build_chart(self.cfg, ds, code, asof, period=period, out=out)))
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
        return Response(200, body=html.encode("utf-8"),
                        content_type="text/html; charset=utf-8")

    def api_root(self, query, body) -> Response:
        from .webui import APP_HTML

        return Response(200, body=str(APP_HTML).encode("utf-8"),
                        content_type="text/html; charset=utf-8")

    # ==================================================================
    # 自选池
    # ==================================================================
    def api_watchlist(self, query, body, method: str) -> Response:
        from . import watchlist

        if method == "GET":
            items = watchlist.list_watchlist(self.cfg)
            return Response(200, _envelope("watchlist", {"items": items, "count": len(items)}))
        if method == "POST":
            code = _require_text(body, "code")
            with _store().state_lock(self.cfg.root, self.lock_timeout):
                doc = watchlist.add_to_watchlist(
                    self.cfg, code, name=str(body.get("name") or ""), note=str(body.get("note") or "")
                )
            return Response(200, _envelope("watchlist", {"code": code, "items": doc.get("items", []),
                                                         "count": len(doc.get("items", []))}))
        if method == "DELETE":
            # ``body`` 在无请求体时是 None（DELETE 通常无 body）—— 直接 ``.get`` 会空指针 500。
            # 另外必须**先解包再转字符串**：直接 str(list) 会得到 "['600519']" 这种垃圾值，
            # 「不崩」不等于「正确」（第一版修复就踩了这个）。
            raw_code = (body or {}).get("code")
            if raw_code is None:
                raw_code = query.get("code")
            if isinstance(raw_code, (list, tuple)):
                raw_code = raw_code[0] if raw_code else None
            code = str(raw_code).strip() if raw_code is not None else ""
            if not code:
                raise _ParamError("缺少必填参数 code")
            with _store().state_lock(self.cfg.root, self.lock_timeout):
                doc = watchlist.remove_from_watchlist(self.cfg, code)
            return Response(200, _envelope("watchlist", {"code": code, "items": doc.get("items", []),
                                                         "count": len(doc.get("items", []))}))
        raise _ParamError(f"不支持的方法 {method}")

    def api_watchlist_score(self, query, body) -> Response:
        """INV-38：池内**缺数据**的票标注 ``no_data``，**不得**记为 0 分。"""
        from . import watchlist
        from . import strategy

        items = watchlist.list_watchlist(self.cfg)
        codes = [str(item.get("code")) for item in items if item.get("code")]
        asof = self._resolve_asof_query(query, body)
        if not codes:
            return Response(200, _envelope("score", {
                "asof": asof, "count": 0, "results": [], "no_data": [], "rank": None,
            }))
        scorer = strategy.CanslimScorer(self.cfg, self._ds(), offline=self.offline)
        results = scorer.score_many(codes, asof)
        rows: list[dict] = []
        no_data: list[dict] = []
        for item in results:
            if not item.factors or item.total <= 0.0:      # 取数失败兜底 → 「无数据」
                no_data.append({
                    "code": item.code,
                    "name": item.name,
                    "no_data": True,
                    "error": (item.risks[0] if item.risks else "无数据"),
                })
            else:
                rows.append({**_svc().jsonable(item), "no_data": False})
        ranked = strategy.rank([r for r in results if r.factors], self.cfg)
        return Response(200, _envelope("score", {
            "asof": asof,
            "count": len(rows),
            "results": rows,
            "no_data": no_data,
            "rank": _svc().jsonable(ranked),
            "recommend": _svc().jsonable(strategy.recommend([r for r in results if r.factors], self.cfg)),
        }))

    # ==================================================================
    # 打分
    # ==================================================================
    def _resolve_asof_query(self, query, body) -> str:
        raw = (body or {}).get("asof")
        if raw is None:
            raw = query.get("asof")
            raw = (raw[0] if isinstance(raw, list) else raw) or None
        ds = self._ds()
        return _svc().resolve_asof(ds, raw, [])

    def api_score(self, query, body) -> Response:
        from . import strategy

        codes = _codes_from(body or {})
        asof = self._resolve_asof_query(query, body)
        scorer = strategy.CanslimScorer(self.cfg, self._ds(), offline=self.offline)
        results = scorer.score_many(codes, asof)
        good = [r for r in results if r.factors]
        return Response(200, _envelope("scan", {
            "asof": asof,
            "adjust": "qfq",
            "count": len(results),
            "results": [_svc().jsonable(r) for r in results],
            "rank": _svc().jsonable(strategy.rank(good, self.cfg)),
        }))

    # ==================================================================
    # 写端点（全部经 store 锁 + 原子写 —— INV-39）
    # ==================================================================
    def api_order(self, query, body) -> Response:
        from .engine import Engine, OrderRequest

        body = body or {}
        code = _require_text(body, "code")
        side = _require_text(body, "side")
        try:
            from .core import Side

            valid_sides = {item.value for item in Side}
        except Exception:                                  # pragma: no cover - 理论不可达
            valid_sides = set()
        if side not in valid_sides:
            raise _ParamError(f"side 非法：{side!r}（可选 {'/'.join(sorted(valid_sides))}）")
        qty = _parse_int(body.get("qty"), "qty")
        if qty <= 0:
            raise _ParamError("qty 必须为正整数")
        if qty % 100 != 0:
            raise _ParamError(f"qty 必须是 100 的整数倍（收到 {qty}）")
        # `price` 允许字符串 "live"（B/D，2026-10-14）：按当前快照价成交。
        # 不能直接 `_parse_float` —— `float("live")` 会变成参数错误，
        # 而这里它是**合法的哨兵值**。
        raw_price = body.get("price")
        if isinstance(raw_price, str) and raw_price.strip().lower() == "live":
            price: Any = "live"
        else:
            price = _parse_float(raw_price, "price")
        ds = self._ds()
        trade_date = str(body.get("trade_date") or "").strip() or None

        holder: dict[str, Any] = {}

        def _mutate(state):
            if state is None:
                raise _ParamError("尚未初始化账户，请先执行 `python -m ptrade init`")
            engine = Engine(self.cfg, ds, state)
            effective = trade_date or state.trade_date or _svc().resolve_asof(ds, None, [])
            if state.trade_date != effective:
                engine.roll_to(effective)
            attach_ledger_source(engine, "web")            # INV-41
            req = OrderRequest(code=code, side=side, qty=qty, price=price, trade_date=effective)
            order = engine.submit(req)
            holder["order"] = order
            holder["trade_date"] = effective
            holder["state"] = engine.state
            return engine.state

        # BLOCK-1：**必须在进锁前**把锁内要用的数据预热进缓存。
        # 引擎在临界区内会取数（实测锁内键：trade_dates / spot，融资融券还含
        # margin_targets —— 后者是两个 akshare 接口，冷缓存最坏 ~4.9s/个）。
        # 在线 + 冷缓存时不预热会把锁持有到几十秒，而 DEFAULT_LOCK_TIMEOUT 是 10s
        # → 并发写者全部 503 / StateLockTimeout，而账户本身毫无问题。
        _svc().prewarm_before_write(self.cfg, ds, trade_date,
                                    extra_codes=(code,), side=side)
        new_state = self._locked_update(_mutate, "order")
        state = holder.get("state") or new_state
        return Response(200, _envelope("order", {
            "trade_date": holder.get("trade_date") or (state.trade_date if state else ""),
            "orders": [_svc().jsonable(holder.get("order"))] if holder.get("order") else [],
            "state": _svc().state_summary(state) if state is not None else {},
        }))

    def api_close(self, query, body) -> Response:
        from .engine import Engine

        body = body or {}
        code = _require_text(body, "code")
        ratio = _parse_float(body.get("ratio"), "ratio")
        ratio = 1.0 if ratio is None else ratio
        if ratio > 1.0:
            raise _ParamError("ratio 必须在 (0, 1] 区间内")
        ds = self._ds()
        trade_date = str(body.get("trade_date") or "").strip() or None
        holder: dict[str, Any] = {}

        def _mutate(state):
            if state is None:
                raise _ParamError("尚未初始化账户，请先执行 `python -m ptrade init`")
            engine = Engine(self.cfg, ds, state)
            effective = trade_date or state.trade_date or _svc().resolve_asof(ds, None, [])
            if state.trade_date != effective:
                engine.roll_to(effective)
            attach_ledger_source(engine, "web")            # INV-41
            orders = engine.close_position(code, effective, ratio)
            holder["orders"] = orders
            holder["trade_date"] = effective
            holder["state"] = engine.state
            return engine.state

        # BLOCK-1：锁外预热（见 api_order 的说明）。平仓会遍历持仓并盯市。
        _svc().prewarm_before_write(self.cfg, ds, trade_date,
                                    extra_codes=(code,), side="close")
        new_state = self._locked_update(_mutate, "close")
        state = holder.get("state") or new_state
        return Response(200, _envelope("close", {
            "trade_date": holder.get("trade_date") or (state.trade_date if state else ""),
            "orders": _svc().jsonable(holder.get("orders") or []),
            "state": _svc().state_summary(state) if state is not None else {},
        }))

    def api_settle(self, query, body) -> Response:
        from .engine import Engine

        body = body or {}
        trade_date = _require_text(body, "trade_date")
        ds = self._ds()
        holder: dict[str, Any] = {}

        def _mutate(state):
            if state is None:
                raise _ParamError("尚未初始化账户，请先执行 `python -m ptrade init`")
            engine = Engine(self.cfg, ds, state)
            rolled = engine.roll_to(trade_date)
            holder["state"] = rolled
            return rolled

        # BLOCK-1：锁外预热（见 api_order 的说明）。结算会滚动交易日并盯市。
        _svc().prewarm_before_write(self.cfg, ds, trade_date)
        new_state = self._locked_update(_mutate, "settle")
        state = holder.get("state") or new_state
        return Response(200, _envelope("settle", {
            "trade_date": state.trade_date if state is not None else trade_date,
            "orders": [],
            "state": _svc().state_summary(state) if state is not None else {},
        }))

    def _locked_update(self, mutate: Callable, command: str):
        """临界区入口：把 ``StateLockTimeout`` 映射为 503（W7）。"""
        store = _store()
        try:
            return store.locked_update(self.cfg, mutate, source="web", timeout=self.lock_timeout)
        except store.StateLockTimeout as exc:
            raise _LockTimeout(str(exc)) from exc

    # ==================================================================
    # 路由
    # ==================================================================
    def dispatch(self, method: str, path: str, query: dict, body: dict | None) -> Response:
        method = method.upper()
        try:
            return self._route(method, path, query or {}, body)
        except _ParamError as exc:
            command = _command_of(path)
            return Response(400, _envelope(command, None, error=f"{exc}"))
        except _LockTimeout as exc:
            command = _command_of(path)
            return Response(503, _envelope(command, None, error=f"STATE_LOCK_TIMEOUT: {exc}"))
        except NetworkForbiddenInLock as exc:
            # **守卫错误**（INV-42）：写锁临界区内试图联网。这是**服务端编码错误**，不是
            # 客户端数据问题，也不是「暂时拿不到数据」—— 它意味着某条写在锁内取数了。
            # 返回 **409 Conflict** 并给出**可执行**的提示（架构 S-1 ③ 要求），
            # 而不是混进 200 业务态（那会让用户以为「稍后重试就好」）。
            command = _command_of(path)
            return Response(409, _envelope(
                command, None,
                error=f"NETWORK_IN_LOCK: {exc}",
            ))
        except DataUnavailable as exc:
            # 「拿不到数据」是**业务态**而非服务端故障（search/chart 早已这样处理）。
            # **按类型匹配**，不用 `type(exc).__name__ == "DataUnavailable"` ——
            # 后者漏掉子类（`NetworkForbiddenInLock` 就是子类，实测曾被判成 HTTP 500）。
            command = _command_of(path)
            return Response(200, _envelope(command, None, error=str(exc)))
        except ValueError as exc:
            # **客户端数据问题**归 400：非法交易日、未来日期、回退到更早日期、
            # 非法代码格式等都抛 ValueError（实测曾有 7 处报 500）。
            # 500 只留给真正的服务端故障（未预期异常）。
            command = _command_of(path)
            return Response(400, _envelope(command, None, error=f"{exc}"))
        except Exception as exc:                           # noqa: BLE001 - 统一 500
            command = _command_of(path)
            return Response(500, _envelope(command, None, error=f"{type(exc).__name__}: {exc}"))

    def _route(self, method: str, path: str, query: dict, body: dict | None) -> Response:
        if path in ("/", "/index.html") and method == "GET":
            return self.api_root(query, body)
        if path == "/api/health":
            return self.api_health(query, body)
        if path == "/api/state":
            return self.api_state(query, body)
        if path == "/api/positions":
            return self.api_positions(query, body)
        if path == "/api/risk":
            return self.api_risk(query, body)
        if path == "/api/search":
            return self.api_search(query, body)
        if path == "/api/watchlist":
            return self.api_watchlist(query, body, method)
        if path == "/api/watchlist/score" and method == "POST":
            return self.api_watchlist_score(query, body)
        if path == "/api/score" and method == "POST":
            return self.api_score(query, body)
        if path == "/api/order" and method == "POST":
            return self.api_order(query, body)
        if path == "/api/close" and method == "POST":
            return self.api_close(query, body)
        if path == "/api/settle" and method == "POST":
            return self.api_settle(query, body)
        if path == "/api/chart" and method == "GET":
            return self.api_chart(query, body)
        return Response(404, _envelope(_command_of(path), None, error=f"未知端点 {method} {path}"))


class _LockTimeout(RuntimeError):
    """写锁超时 → HTTP 503（内部信号）。"""


def _command_of(path: str) -> str:
    if path.startswith("/api/"):
        return path[len("/api/"):].split("/")[0] or "api"
    return "index"


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
def _make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ptrade-web/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):                # 静音（测试/CLI 输出干净）
            pass

        # ---- 协议适配 ----
        def _respond(self, response: Response) -> None:
            body = response.to_bytes()
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_body(self) -> tuple[dict | None, Response | None]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None, None
            if length > _BODY_LIMIT:
                return None, Response(400, _envelope("api", None, error="请求体过大"))
            raw = self.rfile.read(length)
            if not raw.strip():
                return None, None
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None, Response(400, _envelope("api", None, error="请求体不是合法 JSON"))
            if parsed is not None and not isinstance(parsed, dict):
                return None, Response(400, _envelope("api", None, error="请求体必须是 JSON 对象"))
            return parsed, None

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            body, early = self._read_body()
            if early is not None:
                self._respond(early)
                return
            self._respond(app.dispatch(method, parsed.path, query, body))

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def create_server(
    cfg,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    offline: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> ThreadingHTTPServer:
    """创建（并绑定）HTTP 服务器；``port=0`` 时由系统分配空闲端口。"""
    app = WebApp(cfg, offline=offline, lock_timeout=lock_timeout)
    server = ThreadingHTTPServer((host, int(port)), _make_handler(app))
    server.daemon_threads = True
    server.app = app                                        # type: ignore[attr-defined]
    # 回填真实监听地址（port=0 时由系统分配，必须从中读出而非回显入参）
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    app.bound_address = (str(bound_host), int(bound_port))
    return server


def banner(host: str, port: int, *, offline: bool = False, root: str = "") -> str:
    """启动横幅。``host != 127.0.0.1`` → **必须**含警告（R-2：网页端无鉴权）。"""
    url_host = host if ":" not in host else f"[{host}]"
    lines = [
        "=" * 68,
        f"ptrade 网页端已启动：http://{url_host}:{port}",
        f"数据根：{root or '(默认)'}    模式：{'离线（不联网）' if offline else '在线（可联网）'}",
    ]
    if host != DEFAULT_HOST:
        lines += [
            "-" * 68,
            "⚠️  警告：未绑定 127.0.0.1 —— 本服务【没有鉴权】，同网段的任何人都可以查看账户、",
            "    下单、平仓与结算！仅在受信任的内网/本机演示时使用，用完请立即停止服务。",
        ]
    lines.append("=" * 68)
    return "\n".join(lines)


def serve(
    cfg,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    offline: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    print_banner: bool = True,
) -> int:
    """阻塞式启动网页服务（Ctrl+C 退出）。供 ``cli.py serve`` 子命令调用。"""
    server = create_server(cfg, host=host, port=port, offline=offline, lock_timeout=lock_timeout)
    if print_banner:
        bound_host, bound_port = server.server_address[0], server.server_address[1]
        print(banner(bound_host, bound_port, offline=offline, root=cfg.root), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:                               # pragma: no cover - 手工退出
        print("\n已停止。", file=sys.stderr)
    finally:
        server.server_close()
    return 0
