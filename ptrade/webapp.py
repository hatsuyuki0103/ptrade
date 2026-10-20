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

import gzip
import json
import math
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .chart import (  # noqa: F401 - 分析缓存/状态的**单一真源在 chart.py**（R7），这里只转发
    ANALYSIS_FAILED,
    ANALYSIS_PENDING,
    CHART_OPTION_JS,
    PERIODS_WITH_INTRADAY,
    analysis_cache_key,
    analysis_state as chart_analysis_state,
    read_cached_analysis,
    write_cached_analysis,
)
from .datasource import DataUnavailable, NetworkForbiddenInLock

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_LOCK_TIMEOUT",
    "WebApp",
    "Response",
    "analysis_cache_key",
    "analysis_status",
    "attach_ledger_source",
    "banner",
    "cleanup_stale_cache",
    "create_server",
    "discover_cached_asof",
    "read_cached_analysis",
    "serve",
    "start_startup_maintenance",
    "write_cached_analysis",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_LOCK_TIMEOUT = 10.0
#: 对外周期清单的**单一真源**：``chart.PERIODS_WITH_INTRADAY``（INV-56）。
#: 历史上 ``chart`` / ``webapp`` / ``cli`` 三处各写一份 —— 一分时入口就会漂移。
VALID_PERIODS = PERIODS_WITH_INTRADAY
_BODY_LIMIT = 8 * 1024 * 1024
#: gzip 的**下限阈值**：小于它就压缩（≈1 KB 的 JSON 压了反而更大，且没有收益）
_GZIP_MIN_BYTES = 1024

# --------------------------------------------------------------------------- #
# PN（性能：分析异步化）—— PRD R16 **最小可行子集**
#
# 背景（实测）：冷缓存下一次分析 57–153s，而 `/api/chart` 原先**同步**等它 ⇒
# 用户首屏 180s 看白屏。修法：**占位页立即返回 + 后台算 + 小 JSON 报状态**。
# --------------------------------------------------------------------------- #
#: 分析缓存 TTL 与失败短 TTL 的**真源在 chart.py**（`chart.ANALYSIS_TTL` / `ANALYSIS_FAIL_TTL`），
#: 这里不再重复定义（历史上同一常量多处各写一份就是漂移的源头）。
#: 后台分析并发上限（Critic R4）：**有界**，超出直接返 ``pending``、**不排队**
ANALYSIS_MAX_WORKERS = 2
#: 同 key 的 job 超过此时限即视为「已死」，允许重开
#: （**任何形式的 key 泄漏都不得变成永久 pending** —— T-PN-9 的变异就打这条）
ANALYSIS_JOB_STALE_S = 600.0
#: 渲染并发上限（Critic G2）：Semaphore 只限**后台**不限**渲染** ⇒ 狂刷新 = N×1 MB 整页
RENDER_MAX_CONCURRENCY = 2
RENDER_WAIT_S = 5.0
#: 启动时按 mtime 清理旧缓存（Critic G3：分析缓存每 code/asof 一个文件 → 无界增长）。
#: 两道闸（见 `cleanup_stale_cache`）：① 超龄（> CACHE_MAX_AGE_S）删；② 总数超过
#: CACHE_KEEP_NEWEST 时，超出的**最旧**那些也删 —— 于是缓存总量被**硬性**封顶。
#: ★ 1000 ≈ 2 MB（每条 ~2 KB），远大于日常用量，同时让「时钟跳变一次扫光」不可能发生。
CACHE_MAX_AGE_S = 7 * 24 * 3600.0
CACHE_KEEP_NEWEST = 1000

_ANALYSIS_SEM = threading.BoundedSemaphore(ANALYSIS_MAX_WORKERS)
_RENDER_SEM = threading.BoundedSemaphore(RENDER_MAX_CONCURRENCY)
_ANALYSIS_JOBS: dict[str, float] = {}          # job key → 启动时刻（``time.monotonic``）
_ANALYSIS_JOBS_LOCK = threading.Lock()
# ★ v5：**完成时也不删**的启动时刻表 —— 只服务于 `/api/analysis` 的 `elapsed_s`。
#   为什么不能复用 `_ANALYSIS_JOBS`：它在 `_analysis_background` 的 `finally` 里被 pop
#   （见该函数末尾），所以 `ready` 那一刻就查不到 ⇒ `elapsed_s` 永远 `null`（契约说谎）。
#   语义：`pending` = 已算秒数（会变大）· `ready` = **本次耗时**（最终值）· `null` 仅「本进程从未起过 job」。
_ANALYSIS_STARTED: dict[str, float] = {}
_ANALYSIS_STARTED_LOCK = threading.Lock()

# ★ v5：`/api/health` 下发给前端的**轮询节拍**（毫秒）。
#   * 盘中：5s（用户拍板「5 秒」；UI 只读缓存 ≈62ms，不打上游 —— 上游由 spot 后台刷新器负责）
#   * 非盘中：0（不轮询）
#   * 判不出（交易日历不可用）：15s（低频探测；**不再**退到 120s，否则用户体感「不刷新」）
POLL_CADENCE_MS: dict[bool | None, int] = {True: 5000, False: 0}
POLL_CADENCE_UNKNOWN_MS = 15000

#: G2：渲染并发已满时返回的**极小骨架页**（几百字节；不画图、不引图表库）
_BUSY_HTML = (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>渲染繁忙</title></head>"
    '<body data-analysis-state="pending">'
    '<div style="font:14px/1.6 system-ui;padding:24px;color:#c9d1d9;background:#0d1117">'
    f"服务器正在渲染其他图表（并发上限 {RENDER_MAX_CONCURRENCY}），请稍后刷新本页。"
    "</div></body></html>"
)


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


def _require_code(value: Any, field: str = "code") -> str:
    """★ **F1**（ultraqa，HIGH）：**code 必须是 6 位 ASCII 数字**，否则 **400**。

    为什么必须有：`/api/chart/data` 曾把**未校验的 code 直接拼进缓存文件名**
    （`chart_asof_<code>_<asof>.json`）⇒ 畸形值导致 **HTTP 500**：

    ==========================  =========================================
    ``code='../'``              ``FileNotFoundError``（缓存文件路径穿越）
    ``code='<script>alert('``   ``OSError WinError 123``（文件名非法）
    ``code='6'*5000``           ``FileNotFoundError``（文件名过长）
    ==========================  =========================================

    ★ 同一批畸形值在 `/api/quote` / `/api/watchlist` / `/api/order` 上都**没有 500** ⇒ 只此一处漏。
    ★ 用「逐字符白名单」而不是 `str.isdigit()`：后者对全角/其它 Unicode 数字也为真。
    """
    text = str(value or "").strip()
    if len(text) != 6 or any(ch not in "0123456789" for ch in text):
        raise _ParamError(f"{field} 必须是 6 位数字（收到 {text[:24]!r}）")
    return text


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
# PN：分析缓存 / 状态 / 后台任务
#
# ★ **单一真源**：缓存的读写与状态判定都在 `chart.py`（W-P1/task-34：`read_cached_analysis`
#   / `write_cached_analysis` / `analysis_state` / `ANALYSIS_PENDING` / `ANALYSIS_FAILED`）。
#   本模块**只做接线**，不再各写一份（同一件事两套实现正是评审反复打回过的形态）。
# --------------------------------------------------------------------------- #
def cache_dir_of(cfg) -> str:
    """分析/asof 缓存目录（与 `search.py` 读名称表同一口径：``<root>/data/cache``）。"""
    return os.path.join(str(cfg.root), "data", "cache")


def discover_cached_asof(cfg, code: str) -> str | None:
    """从缓存里**发现**该 code 已经算过的 asof（取最新那个）。

    为什么需要：前端正常会带着 asof 轮询，但拿不到时（例如它还没解析出 marker）
    `/api/analysis?code=X` 不能**永远 pending** —— 后台任务算完后，
    这里就能把结果认出来（同目录下最新的一份 ``analysis_<code>_<asof>.json``）。
    """
    directory = cache_dir_of(cfg)
    if not os.path.isdir(directory):
        return None
    prefix = f"analysis_{code}_"
    found: list[tuple[float, str]] = []
    for name in os.listdir(directory):
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        asof = name[len(prefix):-len(".json")]
        if len(asof) != 10 or asof.count("-") != 2:
            continue
        try:
            found.append((os.path.getmtime(os.path.join(directory, name)), asof))
        except OSError:
            continue
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][1]


def analysis_status(cfg, offline: bool, code: str, asof: str | None) -> dict:
    """``/api/analysis`` 的 ``data``：``pending`` / ``ready`` / ``failed``（**只读缓存**）。

    * 状态判定复用 ``chart.analysis_state``（**不另写一份**）：``ANALYSIS_PENDING`` → pending、
      ``ok is False`` → failed、其余 → ready。
    * ``asof`` 缺失时先尝试**发现**已算过的 asof；彻底没有 → 交给后台，先如实报 pending。
    * ``pending`` 时**顺带**确保有一个后台 job 在跑（有界）—— 这样即使用户刷新页面
      （新的 ``/api/chart`` 请求），状态端点自己也不会把状态停在「永远在算」。
    """
    if not asof:
        asof = discover_cached_asof(cfg, code)
    if not asof:
        _ensure_analysis_job(cfg, offline, code, None)
        return {"state": "pending", "error": None, "asof": None, "code": code,
                "elapsed_s": _elapsed_s(cfg, code)}   # ★ v5：进度（秒）
    cached = read_cached_analysis(cfg, code, asof)
    if cached is None:
        _ensure_analysis_job(cfg, offline, code, asof)
        return {"state": "pending", "error": None, "asof": asof, "code": code,
                "elapsed_s": _elapsed_s(cfg, code)}   # ★ v5：进度（秒）
    state = chart_analysis_state(cached)
    error = None
    data: dict = {"state": state, "error": None, "asof": asof, "code": code,
                  "elapsed_s": _elapsed_s(cfg, code)}
    if state == "ready":
        # ★ **V3 修复（Lead 真机发现）**：`ready` 时必须把**七因子**一起带上 ——
        #   否则前端显示「分析：就绪」但总分/因子全是 `—`（状态说就绪、数据是空的 = **契约说谎**）。
        #   这些字段本来就在缓存载荷里 ⇒ **零额外计算**，整包仍是几百字节量级。
        data["factors"] = cached.get("factors") or {}
        data["total"] = cached.get("total")
        data["signal"] = cached.get("signal")
        data["reasons"] = cached.get("reasons") or []
        data["risks"] = cached.get("risks") or []
        # ★ v5（用户诉求「只给了分数没具体分析」的**真正修法**）：
        #   `chart._score_analysis` 早把 `strategy.recommend()` 的结果写进了**分析缓存载荷**
        #   （`payload["recommend"]`），CLI 的 HTML 页面也早已渲染它 —— **只有 Web 路径漏了**。
        #   ⇒ 这里**只透传**，**绝不重算**（重算会与缓存出现两套口径，且 `test_perf_analysis`
        #   的「缓存往返深等」会打红）。
        data["recommend"] = cached.get("recommend")
    if state == "failed":
        # ★ G4：`failed` 的**原因文案必须非空**。载荷给了原因就**原样透出**（不覆盖生产路径的
        #   真实文案）；载荷没给（实测：注入的载荷只有 `ok=False`、没有 `error` 字段 →
        #   `data.error` 曾是 `null`，前端只能显示「未知原因」）→ **兜底合成**一条。
        error = str(cached.get("error") or "").strip() or (
            f"分析失败（缓存载荷未提供原因）：code={code} asof={asof}")
    data["error"] = error
    return data


def _job_key(cfg, code: str) -> str:
    """后台 job 的**去重键**。

    * 含 **root**：同一个进程里可能同时存在多个数据根（测试/多实例）；
      只按 code 去重会让 A 根的任务把 B 根的请求挡成 pending。
    * 含 **code 但不含 asof**：`/api/analysis` 未解析 asof 时与 `/api/chart` 已解析时必须
      落到**同一个**键，否则同一份工作会被起两次。
    """
    return f"analysis_job_{cfg.root}_{code}"


def _note_ui_activity(app) -> None:
    """★ v5：告诉 spot 后台刷新器「**有人在看**」—— 惰性门控的**唯一**输入。

    刷新器只在最近 :data:`..spot_refresher.UI_ACTIVE_WINDOW_S` 秒内收到过本通知时才真的抓上游；
    没人看的时候一次都不打（见 `spot_refresher` 模块 docstring）。
    未启动（例如单测直接构造 `WebApp`、或 `create_server` 还没跑到那一步）时是 **no-op**，不得抛。
    """
    refresher = getattr(app, "spot_refresher", None)
    if refresher is not None:
        try:
            refresher.note_ui_activity()
        except Exception:                                  # noqa: BLE001 - 记账失败绝不能影响请求
            pass


def _elapsed_s(cfg, code: str) -> float | None:
    """`/api/analysis` 的 `elapsed_s`：**分析开始至今的秒数**。

    * `pending`：随着轮询**变大**（前端据此显示「已算 N 秒…」）；
    * `ready`：**本次耗时**（最终值）—— 不是 `None`；
    * `None`：**只在本进程从未为这只票起过 job** 时出现（例如刚重启）。
    ★ 数据源是 `_ANALYSIS_STARTED`（**完成时不删**），不是 `_ANALYSIS_JOBS`
      （后者在后台线程的 `finally` 里被 pop ⇒ 用它会在 `ready` 瞬间返回 `None`）。
    """
    key = _job_key(cfg, code)
    with _ANALYSIS_STARTED_LOCK:
        started = _ANALYSIS_STARTED.get(key)
    if started is None:
        return None
    return round(time.monotonic() - started, 1)


def _ensure_analysis_job(cfg, offline: bool, code: str, asof: str | None) -> bool:
    """确保该 ``code|asof`` 有一个后台分析 job；**有界**且**可自愈**。

    * **有界**：``_ANALYSIS_SEM``（``ANALYSIS_MAX_WORKERS``）—— 拿不到额度就**直接返回**，
      **不排队**（Critic R4：「超出直接返 pending」）。
    * **去重**：同 key 已在跑就不重复起。注意这只是**任务级**去重，**不是**分析级
      single-flight —— 数据级同 key 单飞由 `DataSource._cached` 负责（R16 明确不要第二层）。
    * ★ **自愈**：job 记录超过 ``ANALYSIS_JOB_STALE_S`` 视为已死 → 允许重开。
      这样**任何形式的 key 泄漏都不会变成「永久 pending」**（T-PN-9 的变异正打这条）。
    """
    key = _job_key(cfg, code)
    now = time.monotonic()
    with _ANALYSIS_JOBS_LOCK:
        started_at = _ANALYSIS_JOBS.get(key)
        if started_at is not None and (now - started_at) < ANALYSIS_JOB_STALE_S:
            return False
        if started_at is not None:                     # 陈旧 → 覆盖（原线程若还活着，它的
            _ANALYSIS_JOBS.pop(key, None)              # finally 会 pop 一个不存在的 key，无害）
        if not _ANALYSIS_SEM.acquire(blocking=False):
            return False                               # 超出并发上限 → 直接返 pending
        _ANALYSIS_JOBS[key] = now
    # ★ v5：另记一份**不会被 finally 删掉**的起点（给 `elapsed_s` 用）。
    #   ★ 刻意放在 `_ANALYSIS_JOBS_LOCK` 块**之外**，避免两把锁嵌套。
    with _ANALYSIS_STARTED_LOCK:
        _ANALYSIS_STARTED[key] = now
        _stale_before = now - ANALYSIS_JOB_STALE_S
        for _k in [k for k, t in _ANALYSIS_STARTED.items() if t < _stale_before]:
            _ANALYSIS_STARTED.pop(_k, None)            # 有界：过期项顺手清掉
    threading.Thread(
        target=_analysis_background, args=(cfg, offline, code, asof),
        name=f"ptrade-analysis-{code}", daemon=True,
    ).start()
    return True


def _analysis_background(cfg, offline: bool, code: str, asof: str | None) -> None:
    """后台算一只票的分析并**原子写缓存**（daemon 线程目标）。

    * ★ 本函数只读行情、写**缓存文件**，**从不接触账户状态** ⇒ 它从头到尾都不需要写锁。
      发布前的静态判据会把这一点钉住（函数源码里出现锁原语即红）—— 因为
      「后台线程天然不在临界区」只是**线程局部**（`store.in_critical_section`）带来的
      **巧合级安心**，必须变成**被守住的**性质。
    * ★ 异常也要**写一个失败载荷**（短 TTL）：否则页面永远停在 pending
      （「不得永久 pending」，T-PN-9）。
    * ``asof=None``：先把 asof 解析出来（此时也不在请求线程里，慢也不阻塞首屏）。
    """
    from . import chart

    key = _job_key(cfg, code)
    try:
        ds = _svc().make_ds(cfg, offline)
        if asof is None:
            # 连 asof 都没解析过（前端没拿到 marker）→ 先解析。
            # `chart_asof` 自身已有**相位并入 key** 的结果缓存，这里不再另存一份。
            asof = str(_svc().viewer_asof(ds, code, None, []))[:10]
        payload = _svc().jsonable(chart._score_analysis(cfg, ds, code, asof))
        if not isinstance(payload, dict) or "ok" not in payload:
            payload = ANALYSIS_FAILED("分析结果形状异常")
        write_cached_analysis(cfg, code, asof, payload)
    except NetworkForbiddenInLock:
        # INV-45：守卫错误**不得**被这条后台宽 handler 静默降级成「分析失败」——
        # 它是「锁内联网」的**编码错误信号**，必须响亮。先写一条可见的失败载荷（不
        # 让页面永远 pending），**再原样抛出**（daemon 线程的异常会打到 stderr）。
        try:
            write_cached_analysis(cfg, code, asof or "unknown",
                                  ANALYSIS_FAILED("NETWORK_IN_LOCK：分析在写锁临界区内尝试联网"))
        except Exception:                               # noqa: BLE001
            pass
        raise
    except BaseException as exc:                        # noqa: BLE001 - 后台线程必须自己收口
        try:
            # ★ 失败也**写缓存**（`ANALYSIS_FAILED` → 短 TTL）：
            #   否则页面永远停在 pending（「不得永久 pending」）。
            write_cached_analysis(cfg, code, asof or "unknown",
                                  ANALYSIS_FAILED(f"后台分析失败：{exc}"))
        except Exception:                               # noqa: BLE001
            pass
    finally:
        with _ANALYSIS_JOBS_LOCK:
            _ANALYSIS_JOBS.pop(key, None)
        _ANALYSIS_SEM.release()


def cleanup_stale_cache(cfg, *, max_age_s: float = CACHE_MAX_AGE_S,
                        keep_newest: int = CACHE_KEEP_NEWEST) -> int:
    """按 mtime 清理旧的分析/asof 缓存（Critic G3：每 code/asof 一个文件 → 无界增长）。

    ★ **只碰两类可再生的缓存文件**：``analysis_*``（分析缓存）与 ``chart_asof_*``
      （asof 结果缓存，同样是每 code 一个文件）。其它缓存条目（``stock_list`` /
      ``trade_dates`` / 日线 / spot …）**一律不动**，**不做任何通配删除**。
    ★ **两道闸**：① 超龄（mtime 早于 ``now - max_age_s``）→ 删；
      ② 但若**全部**候选都超龄（疑似时钟跳变 / 首次清理），只删到**剩最新
      ``keep_newest`` 个**为止 —— 防「一次跑偏把缓存全扫光」。
    """
    directory = cache_dir_of(cfg)
    if not os.path.isdir(directory):
        return 0
    candidates = [
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(".json")
        and name.startswith(("analysis_", "chart_asof_"))
    ]
    if not candidates:
        return 0
    candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)   # 新的在前
    cutoff = time.time() - max_age_s
    stale: list[str] = []
    for path in candidates:
        try:
            if os.path.getmtime(path) < cutoff:
                stale.append(path)
        except OSError:
            continue
    if len(stale) == len(candidates):        # 闸②：全都超龄 → 保留最新 keep_newest 个
        victims = stale[keep_newest:]
    else:
        victims = stale
    removed = 0
    for path in victims:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed


def start_startup_maintenance(cfg, offline: bool) -> threading.Thread:
    """启动时的**唯一**后台任务（R11）：清旧缓存 + 预热 spot + 预热名称表 —— **顺序**执行。

    * ★ **不阻塞端口就绪**：本函数 `start()` 后立刻返回；调用方不等它完成 ⇒
      监听端口先可用（T-PN-12 ②）。
    * ★★ **跟随 ``--offline``**（Critic G1）：预热用的数据源就是 ``make_ds(cfg, offline)``，
      ``offline=True`` 时数据源自身**拒绝联网** ⇒ 不可能在 `serve --offline` 下偷偷联网。
      这正是「把纪律交给机制，而不是交给记性」。
    * ★ **只有一条任务**（R11）：spot 与名称表由**同一个**线程**顺序**做，
      **不得**两处各起一个线程。
    * 预热是**尽力而为**：任何异常都吞掉（预热失败绝不能让启动崩）。
    """
    def _work() -> None:
        try:
            cleanup_stale_cache(cfg)
        except NetworkForbiddenInLock:
            raise                                        # INV-45：守卫错误不得被吞（见下）
        except Exception:                                # noqa: BLE001 - 清理是尽力而为
            pass
        try:
            ds = _svc().make_ds(cfg, offline)
            ds.spot()                                    # 预热 spot 快照（后续 asof 走便宜分支）
        except NetworkForbiddenInLock:
            # ★ INV-42/45：守卫错误**不是**「数据不可得」。`ds.spot()` 是已知会抛守卫的函数，
            #   宽 handler 会把它降级成「预热失败」而**静默吞掉** —— 本项目已有 30+ 处同款纪律。
            #   这里先放行再降级。**后台线程里当下不会发生**，但这条纪律的价值是**结构性**的：
            #   不依赖「后台线程恰好不在临界区」（那是线程局部的巧合，不是被守住的保证）。
            raise
        except Exception:                                # noqa: BLE001 - 预热尽力而为
            pass
        try:
            from .search import load_name_table

            load_name_table(cfg, offline=offline)         # R9：名称表预热（同一线程内顺序做）
        except NetworkForbiddenInLock:
            raise                                        # INV-45：同上，先放行
        except Exception:                                # noqa: BLE001
            pass

    thread = threading.Thread(target=_work, daemon=True, name="ptrade-startup-maintenance")
    thread.start()
    return thread


def vendor_asset(name: str) -> Response:
    """V3-A5：**本地** vendored 静态资源 —— 零 CDN（`INV-59` 同族；`T-V3-10c` 的落点）。

    ★ **白名单**（防路径穿越）：只允许下表的文件名，任何 `..`/绝对路径一律 404。
    ★ 用 `pathlib` 精确拼接 + `is_file()` 复核，不信任调用方传入的字符串。
    """
    allowed = {
        "echarts.min.js": Path(__file__).with_name("vendor") / "echarts.min.js",
        # ★ V3-W1（task-36）：K 线 option 的**唯一真源**（ESM，带 `export`）——
        #   原样返回，**不复制内容、不改写**（Critic C3：不许前端再写第二份构建器）。
        "chart_option.js": Path(CHART_OPTION_JS),
    }
    target = allowed.get(name)
    if target is None or not target.is_file():
        return Response(404, _envelope("vendor", None, error=f"未知静态资源 {name!r}"))
    return Response(200, body=target.read_bytes(),
                    content_type="application/javascript; charset=utf-8")


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
    def _ds(self, offline: bool | None = None):
        """构造数据源 —— **唯一**的数据源缝隙（**参数化**）。

        ``offline=None``（默认）→ 用进程级设置；调用方若已在请求级收紧过，直接把
        **有效值**传进来即可：``self._ds(effective_offline)``。

        R5（architect，task-28）：``api_chart`` 原先有一条「先判断请求级是否更严、
        再决定走 ``self._ds()`` 还是自建数据源」的分支，注释还自认「既有测试会替换它」——
        那是**为测试而存在的生产分支**。参数化之后生产路径不再需要那条判断，
        而测试缝仍是同一个：``WebApp._ds`` 依旧可被替换（如
        ``tests/test_webapp.py::test_dataunavailable_still_maps_to_200_business_state``
        用 ``_boom(*a, **k)`` 覆盖它）。
        """
        return _svc().make_ds(self.cfg, self.offline if offline is None else bool(offline))

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
    def _cached_trade_dates(self) -> list[str] | None:
        """只读**缓存条目**里的交易日历；缓存缺失/为空 → ``None``（不可判定）。

        ★ **绝不调 ``ds.trade_dates()``** —— 那在冷缓存时会联网，于是「探活」变成
        「取数」：监控/前端轮询会持续触发抓取，且违反 `market_open` 的纯计算契约。
        照 `search.py:169` 的跨模块直读先例：``DataSource._read_entry(cache_dir, key)``。
        """
        from .datasource import DataSource

        cache_dir = os.path.join(str(self.cfg.root), "data", "cache")
        entry = DataSource._read_entry(cache_dir, "trade_dates")
        payload = entry.get("payload") if isinstance(entry, dict) else entry
        if isinstance(payload, dict):
            payload = payload.get("rows") or payload.get("trade_dates") or []
        if not isinstance(payload, (list, tuple)) or not payload:
            return None
        return [str(item)[:10] for item in payload]

    def api_health(self, query, body) -> Response:
        """探活 + 真实监听地址 + **账实分叉**（N7）。

        地址不能写死 ``DEFAULT_HOST:DEFAULT_PORT`` —— 那样 ``--port`` 改变监听端口后
        ``/api/health`` 仍报默认值，判据就失效了（实测：``--port 8791`` 时它报 8765）。
        真实地址由 :func:`create_server` 回填到 ``app.bound_address``；
        ``port=0``（系统分配）时同样正确。

        **为什么探活要报分叉**（N7）：``/api/health`` 是**监控与被脚本轮询的入口**。
        一个只看 ``status == "ok"`` 的监控方，在账实分叉时**完全看不出来** ——
        而分叉意味着「重试同一笔下单可能重复成交」。所以：

        * ``diverged`` 扁平化到顶层（脚本一条 ``jq .data.diverged`` 就能判）；
        * ``diverged is True`` 时 ``status`` 变为 **``"diverged"``** 且 HTTP **503**
          （健康检查语义上一律如此：不健康就该非 2xx，否则 ``curl -f`` 判不出来）；
        * ``diverged is None``（**无法判定**）→ 仍 **200**，但 ``status`` 为
          ``"unknown"`` —— 「没查成」不是「不健康」，但**也绝不当成 ok**。
        """
        addr = self.bound_address
        host, port = addr if addr else (DEFAULT_HOST, DEFAULT_PORT)
        # ★ v5：算一次、两处用（`market_open` 与 `poll_cadence_ms` 必须来自**同一读数**，
        #   否则两次 `datetime.now()` 跨过 9:30/15:00 边界时会出现自相矛盾的响应）。
        _note_ui_activity(self)                            # ★ v5：惰性门控的输入之一
        _market_open = _svc().market_open(datetime.now(), self._cached_trade_dates())
        consistency = _svc().consistency(self.cfg)
        diverged = consistency.get("diverged")
        if diverged is True:
            status, http = "diverged", 503
        elif diverged is None:
            status, http = "unknown", 200
        else:
            status, http = "ok", 200
        return Response(http, _envelope("health", {
            "status": status,
            "root": self.cfg.root,
            "offline": self.offline,
            "bind": f"{host}:{port}",
            "source": "web",
            # F5：盘中状态三态（True 盘中 / False 交易日但非盘中 / None 不可判定）。
            # **纯计算不取数**：日历只读缓存条目（`_cached_trade_dates`），
            # 前端据此决定自动刷新（True=轮询 / False=停 / None=固定低频探测）。
            "market_open": _market_open,
            # ★ v5：**前端轮询节拍由服务端决定**（单一真源）。
            #   为什么不让前端用本地时钟算交易时段：本地时钟**不懂 A 股节假日**
            #   （节假日的工作日会被误判成盘中 ⇒ 空转轮询）。服务端 `market_open` 是**纯计算**。
            "poll_cadence_ms": POLL_CADENCE_MS.get(_market_open, POLL_CADENCE_UNKNOWN_MS),
            # 扁平化顶层：便于脚本判定（不必知道 ledger_consistency 的内部结构）
            "diverged": diverged,
            "consistent": consistency.get("consistent"),
            "ledger_consistency": consistency,
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

    def api_analysis(self, query, body) -> Response:
        """PN（R16 第 6 项）：**几百字节 JSON** 报告分析状态，供前端**有界轮询**。

        ``{"state": "pending|ready|failed", "error": ..., "asof": ..., "code": ...}``

        ★ 为什么不让 ``/api/chart`` 自己**重渲染 1 MB 页面**来表达「算好了」：
          Critic 明确反对「marker 驱动 1 MB HTML 重渲染」—— 小 JSON 就够。
        ★ **失败态必须带原因文案**（Critic G4），否则用户只看到「永远在算」。
        ★ 本端点**不阻塞**：只读缓存；未命中时起一个**有界**后台任务；
          连 asof 都没解析出来时，也**不在请求线程里**跑解析（那要几十秒），
          而是交给后台，先如实报 ``pending``。
        """
        code = (query.get("code") or [""])
        code = (code[0] if isinstance(code, list) else code) or ""
        if not str(code).strip():
            raise _ParamError("缺少必填参数 code")
        offline = _as_bool(query.get("offline"), self.offline) or self.offline
        asof_query = query.get("asof")
        asof_query = (asof_query[0] if isinstance(asof_query, list) else asof_query) or None
        asof = str(asof_query)[:10] if asof_query else None
        # 前端正常会带着 asof 轮询；拿不到时由 `analysis_status` 从缓存里**发现**已算过的 asof
        # （否则「不带 asof」这条路径会永远 pending —— 那是 T-PN-9 的同族缺陷）。
        return Response(200, _envelope("analysis",
                                       analysis_status(self.cfg, offline, code, asof)))

    def api_chart(self, query, body) -> Response:
        """W9：**内联返回**单文件 HTML（不得返回路径字符串冒充）。

        **W-6**：改用 `chart.render_chart` 拿**内存里的** html，
        不再 `write_text` 500 KB 再 `open().read()` 读回来（原先一次请求两趟盘）。
        落盘仍然做（`chart.write_chart`，**原子写**）—— 用户要能在
        `reports/` 下直接拿到文件，也要能「另存为」。

        F5（v2）三条：
        * **请求级 ``?offline=``**（Critic N2）：语义与 ``/api/search`` 一致 ——
          进程级 ``offline`` 是**下界**，请求级只能更严（``--offline`` 起服务时
          ``?offline=0`` **不得**解除）。修前这里走的是进程级 ``self._ds()``，
          于是网页上的「离线」勾选对图表**完全无效**。
        * ``period == "minute"`` → **先路由到** ``chart.render_intraday``（分时是独立分支：
          ``minute`` **不在** ``chart.PERIODS``，交给 ``render_chart`` 会抛 ValueError）。
        * **看盘 asof**：**一律**走 ``service.viewer_asof(ds, code, asof_query, notes)``
          —— **唯一**的看盘 asof 策略入口（`INV-52` / architect R4）。
          它内部：显式 ``?asof=`` → ``resolve_asof``（守未来函数）；未给 → ``DataSource.chart_asof``
          （**数据驱动、含今天**）并写明 notes。
          ★ 修前这里与 ``cli.cmd_chart`` **各写了一份**同一策略，且可观测行为已经漂移
          （CLI 那份追加 notes、这份不追加）→ 已收敛到中立层，白名单也随之缩到**一个**函数。

        PN（性能，R16）三条 —— **首屏不再被分析挡住**：
        * 分析缓存（``analysis_<code>_<asof>.json``，TTL 6h）**命中 → 完整页**；
          **未命中 → 占位页立即返回** + **有界**后台 daemon 线程算完**原子写**缓存。
          冷缓存下分析 57–153s，修前首屏要等它 ⇒ 实测 **180s** 白屏。
        * 后台并发**有界**（``ANALYSIS_MAX_WORKERS``）：拿不到额度就只报 ``pending``，**不排队**。
          ❌ 不新增分析级 single-flight —— 数据级同 key 单飞已由 `DataSource._cached` 负责。
        * ``<body data-analysis-state="pending|ready|failed">`` marker（R2）：
          前端读**同源 iframe 的 DOM** 判定 + 用 ``/api/analysis`` 小 JSON 有界轮询。
        * ★ G2 取舍：渲染并发也加了上限（``_RENDER_SEM``），等不到就返几百字节骨架页 ——
          详见那段注释。
        """
        from .chart import (
            chart_out_path,
            intraday_out_path,
            render_chart,
            render_intraday,
            write_chart,
        )

        code = (query.get("code") or [""])
        code = (code[0] if isinstance(code, list) else code) or ""
        if not str(code).strip():
            raise _ParamError("缺少必填参数 code")
        # ★★ F1（ultraqa，HIGH）：**进缓存文件名之前必须校验** —— 否则 `../` / `<script>` /
        #   5000 字符会分别变成 FileNotFoundError / WinError 123 / 文件名过长 ⇒ **HTTP 500**。
        #   ★ 本文件里 `/api/chart/data` 与 `/api/chart`(HTML) **两处**都拼缓存文件名，故都修。
        code = _require_code(code)
        period = (query.get("period") or ["daily"])
        period = (period[0] if isinstance(period, list) else period) or "daily"
        if period not in VALID_PERIODS:
            raise _ParamError(f"period 必须是 {'/'.join(VALID_PERIODS)} 之一（收到 {period!r}）")

        # 请求级 offline（**进程级为下界**，照 api_search 的既有实现）
        offline = _as_bool(query.get("offline"), self.offline) or self.offline
        # R5：**参数化缝隙** —— 把「有效 offline」交给同一个 `_ds`，生产路径里不再有
        # 「判断请求级是否更严、再决定走 `_ds()` 还是自建数据源」这条**只为测试服务**的分支。
        ds = self._ds(offline)

        notes: list[str] = []
        asof_query = query.get("asof")
        asof_query = (asof_query[0] if isinstance(asof_query, list) else asof_query) or None

        # 分时：独立入口 + 独立落盘命名（INV-56：不得复用 chart_out_path，否则同日覆盖 K 线图）
        if period == "minute":
            html, payload = render_intraday(self.cfg, ds, code, day=asof_query)
            write_chart(intraday_out_path(self.cfg, code, payload["day"]), html)
            return Response(200, body=html.encode("utf-8"),
                            content_type="text/html; charset=utf-8")

        # 看盘 asof：**收敛到唯一的策略入口** `service.viewer_asof`（architect R4 第 2 轮要求）。
        # ★ 修前这里是**第二份实现**（`resolve_asof(...) if asof_query else ds.chart_asof(code)`），
        #   与 `cli.cmd_chart` 那份**已经在可观测层面不一致** —— CLI 那份会往 `notes` 追加
        #   「看盘路径：asof 取自…」，这份不追加。同一个策略两处实现、行为还不一样，
        #   正是 architect 说的「策略复制」；抽到中立层后这里只负责取值。
        asof = _svc().viewer_asof(ds, code, asof_query, notes)
        # ★ 不额外写 asof 缓存：`chart_asof` 内部已带**相位并入 key** 的结果缓存
        #   （`chart_asof_<code>_<phase>.json`）。再来一份就是同一件事两套实现。

        # ① 分析缓存：命中 → **完整页**；未命中 → **占位页立即返回** + 后台算（PRD F2）
        #   占位/失败的载荷形状由 `chart.py` 定义（`ANALYSIS_PENDING` / `ANALYSIS_FAILED`），
        #   `<body data-analysis-state=...>` 也由 chart 的模板渲染 —— 本模块**不做 HTML 后处理**。
        cached = read_cached_analysis(self.cfg, code, asof)
        if cached is not None:
            analysis = cached
        else:
            analysis = ANALYSIS_PENDING
            _ensure_analysis_job(self.cfg, offline, code, asof)      # 有界；拿不到额度就只报 pending

        # ★ G2（Critic）：`Semaphore` 只限**后台**不限**渲染** ⇒ 狂刷新 = N×1 MB 整页。
        #   这里给渲染也加并发上限并**有界等待**：等不到就返回几百字节的骨架页，
        #   宁可让用户稍后刷新一次，也不让 10 个并发请求各渲染 1 MB（内存/CPU 尖峰）。
        #   取舍：正常使用（一次看一张图）永远走不到这条路；它只挡「手动狂刷 / 脚本扫」。
        if not _RENDER_SEM.acquire(timeout=RENDER_WAIT_S):
            return Response(200, body=_BUSY_HTML.encode("utf-8"),
                            content_type="text/html; charset=utf-8")
        try:
            html, payload = render_chart(self.cfg, ds, code, asof, period=period,
                                         analysis=analysis)
        finally:
            _RENDER_SEM.release()

        # R2：`<body data-analysis-state=...>` 由 `chart.py` 的模板渲染（`__ANALYSIS_STATE__`
        # ← `chart.analysis_state(analysis)`）—— 本模块**不再注入属性**（一件事一套实现）。
        write_chart(chart_out_path(self.cfg, code, payload["asof"]), html)
        return Response(200, body=html.encode("utf-8"),
                        content_type="text/html; charset=utf-8")

    def api_chart_data(self, query, body) -> Response:
        """V3-A5：图表**数据**的 JSON 真源（唯一；与 CLI 同源 `chart.chart_payload`）。

        ★ 为什么它是「加载慢」的解药：ECharts（1,030,855 B）在主页**只加载一次**，
          每次换图只取这份 JSON（< 600 KB），不再每张图重发 1.17 MB 整页。
        ★ `T-V3-10c`：本响应**不含** `echarts` 字样（防「把库塞进数据里」）。
        """
        from .chart import PERIODS, chart_payload, option_data, render_intraday

        code = (query.get("code") or [""])
        code = (code[0] if isinstance(code, list) else code) or ""
        if not str(code).strip():
            raise _ParamError("缺少必填参数 code")
        # ★★ F1（ultraqa，HIGH）：**进缓存文件名之前必须校验** —— 否则 `../` / `<script>` /
        #   5000 字符会分别变成 FileNotFoundError / WinError 123 / 文件名过长 ⇒ **HTTP 500**。
        #   ★ 本文件里 `/api/chart/data` 与 `/api/chart`(HTML) **两处**都拼缓存文件名，故都修。
        code = _require_code(code)
        period = (query.get("period") or ["daily"])
        period = (period[0] if isinstance(period, list) else period) or "daily"
        if period not in VALID_PERIODS:
            raise _ParamError(f"period 必须是 {'/'.join(VALID_PERIODS)} 之一（收到 {period!r}）")
        offline = _as_bool(query.get("offline"), self.offline) or self.offline
        ds = self._ds(offline)
        notes: list[str] = []
        asof_query = query.get("asof")
        asof_query = (asof_query[0] if isinstance(asof_query, list) else asof_query) or None
        if period == "minute":
            # 分时是**独立渲染分支**（INV-56）：拿它的 payload，丢掉 html（主页自己画）。
            _html, payload = render_intraday(self.cfg, ds, code, day=asof_query)
            return Response(200, _envelope("chart/data", payload))
        asof = _svc().viewer_asof(ds, code, asof_query, notes)
        payload = chart_payload(self.cfg, ds, code, asof, include_today=True)
        # ★★ X1（真缺陷）：返回**瘦身版**（`option_data`），而不是裸 `chart_payload` ——
        #   后者**没有** `legend`，导致前端 `legend.data=[]` ⇒ **图例空、点不了**（用户要求的「图例点击隐藏」失效）。
        #   `option_data` 与 HTML/CLI 路径**共用**，两条路径按构造同形。
        return Response(200, _envelope("chart/data", _svc().jsonable(option_data(payload, period))))

    def api_quote(self, query, body) -> Response:
        """V3-A2：批量现价 —— 给自选池/操作条用。

        ★ **一次 `_spot_frame_raw()`**（只读缓存、不联网）供全部 code 使用：
          否则 N 只票 = N 次取数（这正是「加载慢」的那类浪费）。
        ★ **绝不在写锁内**：本端点全程只读（`/api/chart` 同理）。
        ★ 取不到就明确 `no_data: true`（INV-38：不得记 0）。
        """
        raw = (query.get("codes") or query.get("code") or [""])
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else ""
        codes = [c.strip() for c in str(raw).replace("，", ",").split(",") if c.strip()]
        if not codes:
            raise _ParamError("缺少必填参数 codes")
        _note_ui_activity(self)                            # ★ v5：自选池 5s 轮询的主入口
        ds = self._ds()
        names: dict[str, str] = {}
        try:
            from . import watchlist as watchlist_mod

            for item in watchlist_mod.list_watchlist(self.cfg).get("items", []):
                names[str(item.get("code"))] = str(item.get("name") or "")
        except Exception:                        # noqa: BLE001 - 名称只是锦上添花
            names = {}
        frame = None                             # ★ 只取一次快照
        try:
            frame, _spot_asof = ds._spot_frame_raw()
        except Exception:                        # noqa: BLE001 - 无快照 → 全部 no_data
            frame = None
        items: list[dict] = []
        for code in codes:
            item: dict = {"code": code}
            row = None
            if frame is not None and len(frame):
                try:
                    matched = frame[frame["code"].astype(str).str.zfill(6) == code.zfill(6)]
                    if len(matched):
                        row = matched.iloc[0]
                except Exception:                # noqa: BLE001
                    row = None
            if row is not None:
                def _f(key):
                    value = row.get(key)
                    try:
                        return None if value is None or value != value else float(value)
                    except (TypeError, ValueError):
                        return None

                last, prev = _f("last"), _f("prev_close")
                item["name"] = str(row.get("name") or names.get(code, "") or "")
                item["last"] = last
                item["pct_chg"] = (round((last - prev) / prev * 100.0, 2)
                                   if last and prev else None)
                item["no_data"] = last is None
            else:
                item["name"] = names.get(code, "")
                item["last"] = None
                item["pct_chg"] = None
                item["no_data"] = True
            items.append(item)
        return Response(200, _envelope("quote", {"items": items, "count": len(items)}))

    def api_name_refresh(self, query, body) -> Response:
        """V3：强制刷新名称表（**W3 的 `search.refresh_name_table`**）。

        ★★ **`--offline` 下必须拒绝**（INV-64 服务端一侧）：否则「离线」被一个按钮破坏。
        拒绝用 **200 业务态 + 明确文案**（前端能直接显示，不必猜状态码含义）。
        """
        from .search import refresh_name_table

        # ★ F4（ultraqa，LOW）：请求级 `?offline=true` 必须能**收紧**（与其它端点同一模式）；
        #   `or self.offline` 保证**永远不能放宽**进程级 `--offline`。
        offline = _as_bool(query.get("offline"), self.offline) or self.offline
        try:
            result = refresh_name_table(self.cfg, offline=offline)
        except NetworkForbiddenInLock:
            # ★★ **必须排在 `DataUnavailable` 之前**：`NetworkForbiddenInLock` 是它的**子类**，
            #   反过来写会让守卫错误被下面的宽 handler 接住 → 变成 200 业务态 = **静默吞掉守卫错误**
            #   （违 INV-42/45；本项目已出现过多次同款事故，`test_W1` 的「一跳静态规则」正是为此）。
            raise
        except DataUnavailable as exc:                    # 含 offline 拒绝 / 取数失败
            return Response(200, _envelope("name/refresh", None, error=str(exc)))
        return Response(200, _envelope("name/refresh", result))

    def api_vendor(self, query, body) -> Response:
        """内部占位：静态资源由 `_route` 直接调 `vendor_asset`（不走这里）。"""
        raise _ParamError("内部端点：静态资源由路由直接处理（见 `vendor_asset`）")

    def api_root(self, query, body) -> Response:
        """`GET /`：返回**同一份** `webui.APP_HTML`（V3：判据要断言真实响应体）。"""
        from .webui import APP_HTML

        return Response(200, body=str(APP_HTML).encode("utf-8"),
                        content_type="text/html; charset=utf-8")

    # ==================================================================
    # 自选池
    # ==================================================================
    def api_watchlist(self, query, body, method: str) -> Response:
        from . import watchlist
        from .store import StateLockTimeout

        if method == "GET":
            _note_ui_activity(self)                        # ★ v5：自选池轮询（GET）是主入口
            items = watchlist.list_watchlist(self.cfg)
            return Response(200, _envelope("watchlist", {"items": items, "count": len(items)}))
        if method == "POST":
            code = _require_text(body, "code")
            # ★ F5（ultraqa P5）：**格式校验必须在入池之前**。
            #   实测过 `{"code": "../"}` / `"<script>alert(1)</script>"` / 全角 / 5000 字符
            #   曾 **HTTP 200 并真的写进自选池** —— 不崩，但脏数据会一路污染渲染与批量取价。
            code = _require_code(code)
            given_name = str(body.get("name") or "").strip()
            note = str(body.get("note") or "")
            # ★ P0-4（Architect）：**名称解析必须在写锁外**。
            #   W3 已把解析放在 `add_to_watchlist` **内部、且严格在它自己的写锁之前**
            #   （默认路径就是 cache-only 兜底，绝不联网）。所以正确接法是：
            #   **把这次调用整体移出 `with self._state_lock()`** —— 它自己管锁，
            #   我们再套一层反而会把「名称解析」包进我们的临界区（INV-42 的温床），
            #   而且同线程可重入的嵌套锁会**掩盖**真实边界。
            #   ★ 暂**不依赖** `resolve_name=` 注入的语义（core-builder 自认该钩子当前未兑现）。
            #
            #   ★★ INV-45（v3 补齐）：`add_to_watchlist` **自己取写锁**，所以它抛出的
            #   `StateLockTimeout` 是**真·取锁超时**（不是「块内 bug」）⇒ **必须映射成 503**。
            #   这里**只**兜 `StateLockTimeout`、不动其它异常：
            #   `_state_lock` 那条「块内异常走 500」的纪律仍然成立（本分支没有外层块）。
            try:
                doc = watchlist.add_to_watchlist(
                    self.cfg, code, name=given_name, note=note,
                    # ★ M4（code-review）：传**可配置**的写锁超时 ——
                    #   此前本函数内部写死 10s，`WebApp(lock_timeout=…)` 对 POST 形同虚设。
                    lock_timeout=self.lock_timeout,
                )
            except StateLockTimeout as exc:
                raise _LockTimeout(f"自选池写锁超时（INV-45）：{exc}") from exc
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
            with self._state_lock():
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
        # ★ F5（ultraqa P5）：**格式校验必须在入池/下单之前**。
        #   实测过：`{"code": "../"}` / `"<script>alert(1)</script>"` / 全角 / 5000 字符
        #   曾 **HTTP 200 并真的写进自选池** —— 不崩，但脏数据会一路污染渲染与批量取价。
        code = _require_code(code)
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
        # ★ F5（ultraqa P5）：**格式校验必须在入池/下单之前**。
        #   实测过：`{"code": "../"}` / `"<script>alert(1)</script>"` / 全角 / 5000 字符
        #   曾 **HTTP 200 并真的写进自选池** —— 不崩，但脏数据会一路污染渲染与批量取价。
        code = _require_code(code)
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
        """临界区入口：把**取锁阶段**的 ``StateLockTimeout`` 映射为 503（W7）。

        **第五轮复核 L3 的结构性收口**：原先这里调 `store.locked_update`，
        而它把 `mutate` 也包在 `with` 里 → **块体**抛出的 `StateLockTimeout`
        同样会被转成 503，把「块体自己的 bug」伪装成「锁忙，请重试」。
        当时复核**逐条排除了可达性**（`mutate` 内唯一取锁点是 `append_jsonl`，
        同线程可重入不等待），但那是**推理**而非保障。

        改用 `store.locked_update_split`：取锁阶段与块体分离，
        只有**取锁**超时才转 503，块内异常原样上抛（→ 500）。
        与 `_state_lock()`（INV-45）形态一致，不再依赖「当前不可达」的判断。
        """
        store = _store()
        try:
            return store.locked_update_split(
                self.cfg, mutate, source="web", timeout=self.lock_timeout
            )
        except store.StateLockTimeout as exc:
            raise _LockTimeout(str(exc)) from exc

    @contextmanager
    def _state_lock(self):
        """只取锁不改 state 的临界区（自选池增删用）。

        **为什么单独一层**（第四轮复核 M5，MEDIUM）：`/api/watchlist` 的 POST/DELETE
        原先**直接**用 `store.state_lock`，锁超时抛的是原始 `store.StateLockTimeout` ——
        它**不在 `map_exception` 的映射表里** → 落进兜底 **HTTP 500**，
        而 INV-45 契约要求 **503 + `STATE_LOCK_TIMEOUT`**。

        **为什么手动分离 `__enter__` 与块体**（我自己的实证发现，见
        `.omx/probe/verify_m5_contextmanager.py`）：直觉写法

            with state_lock(...):   # 异常在此抛出
                try: yield
                except StateLockTimeout -> _LockTimeout

        会把「**块内**抛出的 `StateLockTimeout`」也误转成 503 —— 实测确认会发生。
        块内的同类型异常是**块体自己的 bug**，该走 500，不该伪装成「锁忙、请重试」。
        所以：只把 **`__enter__` 阶段**的异常转成 `_LockTimeout`，块内的原样上抛。

        `__exit__` 会收到**真实的** `exc_info`（而不是 `(None, None, None)`）——
        这样若将来 `state_lock` 换成会**抑制异常**的 CM，抑制不会被悄悄丢掉；
        当前 `store.state_lock` 永不抑制（`_locked_file` 的 `finally` 只做清理），
        所以返回值可以忽略，但**不该断言它一定是假**（第五轮复核 L4）。
        """
        store = _store()
        cm = store.state_lock(self.cfg.root, self.lock_timeout)
        try:
            cm.__enter__()                    # ← 只在这里可能因锁超时抛错
        except store.StateLockTimeout as exc:
            raise _LockTimeout(str(exc)) from exc
        try:
            yield
        except BaseException:
            # 把真实异常告知 CM（而不是 `(None, None, None)`），然后**无条件上抛**：
            # 本方法要的是「块内异常 → 500」，若因 CM 抑制而静默消失就与本意相反。
            # 当前 `store.state_lock` 永不抑制，所以返回值只作告知用。
            cm.__exit__(*sys.exc_info())
            raise
        else:
            cm.__exit__(None, None, None)

    # ==================================================================
    # 路由
    # ==================================================================
    def dispatch(self, method: str, path: str, query: dict, body: dict | None) -> Response:
        """路由 + **异常映射**。

        映射逻辑抽到模块级纯函数 :func:`map_exception`（2026-10-14）：这样
        `NetworkForbiddenInLock → 409` 这类分支可以被**直接单测**（喂异常对象即可，
        不必跑整条路由）—— 此前它在变异实验里会卡死，给不出「违反时会红」的证据。
        """
        method = method.upper()
        try:
            return self._route(method, path, query or {}, body)
        except Exception as exc:                           # noqa: BLE001 - 统一映射
            mapped = map_exception(path, exc)
            if mapped is not None:
                return mapped
            # 未预期异常 → 500（映射表不管这一类）
            return Response(
                500,
                _envelope(_command_of(path), None,
                          error=f"{type(exc).__name__}: {exc}"),
            )


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
        if path == "/api/analysis" and method == "GET":
            return self.api_analysis(query, body)
        if path == "/api/chart/data" and method == "GET":
            return self.api_chart_data(query, body)
        if path == "/api/chart" and method == "GET":
            return self.api_chart(query, body)
        if path == "/api/quote" and method == "GET":
            return self.api_quote(query, body)
        if path == "/api/name/refresh" and method == "POST":
            return self.api_name_refresh(query, body)
        if path.startswith("/vendor/") and method == "GET":
            return vendor_asset(path[len("/vendor/"):])
        if path == "/static/chart_option.js" and method == "GET":
            return vendor_asset("chart_option.js")
        return Response(404, _envelope(_command_of(path), None, error=f"未知端点 {method} {path}"))


class _LockTimeout(RuntimeError):
    """写锁超时 → HTTP 503（内部信号）。"""


# --------------------------------------------------------------------------- #
# 异常 → HTTP 响应 的**纯函数**映射（2026-10-14）
#
# 抽出来的理由：这些分支是**契约**（400 / 409 / 503 / 200业务态 / 500），
# 抽成纯函数后才能「喂异常对象 → 断言状态码」地单测，不必跑整条路由 ——
# 后者在变异实验里会卡死，导致 `NetworkForbiddenInLock → 409` 长期拿不到变异证据。
# --------------------------------------------------------------------------- #
def map_exception(path: str, exc: BaseException) -> Response | None:
    """把已知异常映射为 ``Response``；**不认识就返回 `None`**（交给调用方兜底 500）。

    **顺序即契约**（子类必须在前）：

    ============================================  ==========================================
    异常                                          映射
    ============================================  ==========================================
    ``_ParamError``                               400（客户端参数非法）
    ``_LockTimeout``                              503 + ``STATE_LOCK_TIMEOUT``
    ``NetworkForbiddenInLock``                    **409 + ``NETWORK_IN_LOCK``**（INV-42）
    ``DataUnavailable``                           **200 业务态**（「拿不到数据」不是服务端故障）
    ``ValueError``                                400（非法交易日/未来日期/非法代码等）
    其它                                          ``None`` → 调用方 500
    ============================================  ==========================================

    ``NetworkForbiddenInLock`` 是 ``DataUnavailable`` 的**子类**，所以它必须排在前面；
    这也正是原实现「按**类名字符串**匹配」漏掉子类的根因（守卫错误曾被判成 500）。
    """
    command = _command_of(path)
    if isinstance(exc, _ParamError):
        return Response(400, _envelope(command, None, error=f"{exc}"))
    if isinstance(exc, _LockTimeout):
        return Response(503, _envelope(command, None, error=f"STATE_LOCK_TIMEOUT: {exc}"))
    if isinstance(exc, NetworkForbiddenInLock):
        # 写锁临界区内试图联网：**服务端编码错误**，不是客户端数据问题、
        # 也不是「暂时拿不到数据」。409 并给出可执行的 `NETWORK_IN_LOCK` 提示
        # （架构 S-1 ③ 要求），而不是混进 200 业务态（那会让用户以为「稍后重试就好」）。
        return Response(409, _envelope(command, None, error=f"NETWORK_IN_LOCK: {exc}"))
    if isinstance(exc, DataUnavailable):
        # 「拿不到数据」是**业务态**而非服务端故障（search/chart 早已这样处理）。
        return Response(200, _envelope(command, None, error=str(exc)))
    if isinstance(exc, ValueError):
        # 客户端数据问题：非法交易日、未来日期、回退到更早日期、非法代码格式等。
        return Response(400, _envelope(command, None, error=f"{exc}"))
    return None


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
            encoding = ""
            # ★ F-GZIP（传输层，可选但有真收益）：整页 HTML ≈ **1.17 MB**（图表库与数据全内联），
            #   gzip 后 ≈15% ⇒ 「打开网页」的下行时间对用户是实打实的（ultraqa：8 路并发 ≈9 MB）。
            #   两条纪律：① **只压大响应**（<1 KB 的 JSON 压了反而更大，且小响应无需压）；
            #             ② **客户端没请求就不压**（`Accept-Encoding` 里没有 gzip → 原样返回），
            #   这样既有客户端（urllib 默认 identity）行为一字不变。
            accept = (self.headers.get("Accept-Encoding") or "").lower()
            if len(body) >= _GZIP_MIN_BYTES and "gzip" in accept:
                packed = gzip.compress(body, compresslevel=6)
                if len(packed) < len(body):
                    body, encoding = packed, "gzip"
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
                self.send_header("Vary", "Accept-Encoding")
            # ★ 静态 JS（1 MB 的 ECharts）允许浏览器**跨页面**缓存 ⇒ 「pin 一次加载」才成立；
            #   动态响应继续 no-store（账户数据绝不落缓存）。
            if response.content_type.startswith("application/javascript"):
                self.send_header("Cache-Control", "public, max-age=86400")
            else:
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


#: 允许**无需显式放行**即可绑定的监听地址（「仅本地监听」是本项目的硬约束）。
#: ★ 由 ultraqa（`gate-verifier-3`）实测发现：修前 `create_server(host="0.0.0.0")`
#:   **真的绑成功**（`('0.0.0.0', 60886)`），只有启动横幅**警告一次** ——
#:   而网页端**无鉴权**（横幅自己写着「同网段的任何人都可以查看账户、下单、平仓与结算」）
#:   ⇒ 「仅本地监听」**没有硬闸**，只有一句提示。现改为**默认拒绝**。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _is_loopback(host: str) -> bool:
    """host 是否**确定**是回环地址。

    ★ **保守**：数字形态用 `ipaddress` 判（`127.0.0.0/8` 整段算回环），
      **名字形态只认白名单** —— **不做 DNS 解析**：一个名字能不能解析到回环，
      由系统解析器说了算，而它随后会被交给 `getaddrinfo` 去真的解析，
      所以「以 `127.` 开头」**不能**当作回环的证据。

    ★★ 第一版的 `h.startswith("127.")` **把主机名也判成回环**，而 docstring 却写着
      「只认白名单、不做 DNS 解析」—— **文档说 A、代码做 B**（architect 第 3 轮实测：
      `_is_loopback("127.evil.com") is True`、`"127.0.0.1.nip.io" is True`、
      `"127.0.0.1.attacker.net" is True`）。随后 `create_server` 把 host 交给
      `ThreadingHTTPServer` 让 `getaddrinfo` 解析 ⇒ **能解析到非回环的名字穿过了硬闸**。
      → 现在数字形态走 `ipaddress`（`127.1.2.3` 仍是 True，`127.evil.com` → False），
      名字形态只认 `LOOPBACK_HOSTS`。
    """
    import ipaddress

    h = str(host).strip().lower()
    if h in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False          # 是**名字**而不是 IP 字面量 → 一律不认（保守）


def create_server(
    cfg,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    offline: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    allow_remote: bool = False,
) -> ThreadingHTTPServer:
    """创建（并绑定）HTTP 服务器；``port=0`` 时由系统分配空闲端口。

    ★ **非回环地址默认拒绝**（硬约束「仅本地监听」）。要暴露到局域网必须**显式**
    ``allow_remote=True``（对应 CLI 的 ``--allow-remote``）—— 因为本服务**无鉴权**，
    同网段的任何人都能查看账户、下单、平仓与结算。
    修前只打印一次警告就照常绑定（ultraqa F-2 实测 `0.0.0.0` 绑成功）。
    """
    if not _is_loopback(host) and not allow_remote:
        raise ValueError(
            f"拒绝绑定非回环地址 {host!r}：本服务**无鉴权**，同网段的任何人都可以查看账户、"
            f"下单、平仓与结算。若确实要暴露到局域网，请显式加 --allow-remote（自行承担风险）。"
        )
    app = WebApp(cfg, offline=offline, lock_timeout=lock_timeout)
    server = ThreadingHTTPServer((host, int(port)), _make_handler(app))
    server.daemon_threads = True
    server.app = app                                        # type: ignore[attr-defined]
    # 回填真实监听地址（port=0 时由系统分配，必须从中读出而非回显入参）
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    app.bound_address = (str(bound_host), int(bound_port))
    # ★ PN（R11 + G1 + G3）：**一条**后台任务做「清旧缓存 → 预热 spot → 预热名称表」。
    #   `start_startup_maintenance` 只 `start()` 线程就返回 ⇒ **不阻塞端口就绪**（T-PN-12）。
    #   挂到 `app` 上是为了让判据能拿到它（线程计数器 + 「端口早于预热完成」）。
    app.startup_thread = start_startup_maintenance(cfg, offline)      # type: ignore[attr-defined]
    # ★ v5：spot 快照的**周期刷新器**（用户诉求「盘中自选涨幅不刷新」的**真根因修法**）。
    #   ★ 为什么是**独立**线程：`start_startup_maintenance` 是**一次性预热**（做完就退出），
    #     而周期刷新必须**活过**它 ⇒ 生命周期不同。R11 的「不得两处各起一个线程」约束的是
    #     **预热**（spot 与名称表别各起一个），不适用于这里 —— 别把它误判成违规。
    #   ★ 在 `create_server` 里起的原因：① 拿得到 `app`（`should_run` 要用它的交易日历缓存）；
    #     ② 端口已 `listen`（刷新器晚于端口就绪，不影响 `T-PN-12`「端口早于预热完成」）。
    #   ★ 全部失败都不许影响启动（刷新器是**尽力而为**的后台服务）。
    try:
        from .spot_refresher import start_spot_refresher

        def _spot_should_run() -> bool:
            """★ 三重门控（服务端权威）：离线不刷 · 非盘中不刷 · 判不出交给惰性门控。"""
            if offline:
                return False                               # `--offline` 是硬闸（INV-10/35）
            try:
                mo = _svc().market_open(datetime.now(), app._cached_trade_dates())  # noqa: SLF001
            except Exception:                              # noqa: BLE001 - 判不出就让它走惰性门控
                return True
            return mo is not False                         # True/None ⇒ 可以刷；False ⇒ 不刷

        def _spot_refresh() -> None:
            """★ **只在锁外**调 `_refresh_spot_cache()`（`_fetch_spot`→`_ak()` 有锁内禁网守卫）。"""
            ds = _svc().make_ds(cfg, offline)
            ds._refresh_spot_cache()                       # noqa: SLF001

        app.spot_refresher = start_spot_refresher(          # type: ignore[attr-defined]
            _spot_refresh, should_run=_spot_should_run
        )
    except Exception:                                      # noqa: BLE001 - 刷新器起不来也要能 serve
        app.spot_refresher = None                          # type: ignore[attr-defined]
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
    allow_remote: bool = False,
) -> int:
    """阻塞式启动网页服务（Ctrl+C 退出）。供 ``cli.py serve`` 子命令调用。

    ``allow_remote`` 透传给 :func:`create_server`（非回环地址默认为**拒绝**）。
    """
    server = create_server(cfg, host=host, port=port, offline=offline,
                           lock_timeout=lock_timeout, allow_remote=allow_remote)
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
