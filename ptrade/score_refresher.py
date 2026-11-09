"""**周末全市场分数刷新器**（v10-A4）—— 周五收盘~下周一开盘前把全市场 5024 只算一遍。

## 用户裁决（原话）
> 「**周五收盘**的时候，你可以**全市场都算**，因为**我不在乎这个时间消耗**。
>   平时你可以给我算**持仓的和自选池的**。」

## ★ 为什么**不复用** `spot_refresher` 的 per-tick 模型（Critic B4）
`spot_refresher` = **固定墙钟节拍**（每 60s 干一件**小事**）+ **惰性门控**（最近 120s 内有 UI 请求才刷）。
而本任务是**一次跑几小时的单趟活** ⇒ 套 per-tick 会：
* 惰性门控 ⇒ 用户不会开 4 天页面 ⇒ **任务永远跑不完** ✗
* `next_at = max(now, next_at + 60)` ⇒ 一次 4h 的活会**越过无数个 tick** ✗
⇒ **单趟循环**（`for code in codes: ...`）+ **每 N 只落台账** + 可暂停/停止 ✓

## ★ 成本（Critic M3 的重估，这是可交付性的关键）
`chart.py:643` **每票新建 `CanslimScorer`** ⇒ 丢掉全部实例级缓存 ⇒ **≈103h** ✗
本模块**复用一个 scorer** ⇒ 每票只多 1 次自身日线 + 2–3 次自身财务/股东 ≈ **1.5–4.5s**
⇒ 5024 只 ≈ **2.8–6.3h**，加节流/重试 ≈ **4–8h** ⇒ **一个周末够** ✓

## 「周五收盘」判定（Architect B3 + Critic B3 的 3 个修正）
```python
prev_td = today if now >= 15:30 else prev_trade_date(today)    # 最近**已收盘**交易日
next_td = next_trade_date(today)                                # ★ 新浪日历是**历史**日历 ⇒ 可能抛
in_window = prev_td.iso_week != next_td.iso_week
# ★ 修正 1：**排除周一盘中**（否则周一 10:00 也会 True ⇒ 盘中触发全市场 ✗）
if is_trade_date(today) and 09:30 <= now < 15:00: in_window = False
# ★ 修正 2：next_td 取不到 ⇒ 回退「日历最大日期 == prev_td」启发式
# ★ 修正 3：**必须配 ISO 周幂等台账** ⇒ 一周最多一轮（判定宽松也不会重复烧时间）
```

## ISO 周幂等台账（Critic 的硬要求）
`data/cache/score_refresh_<iso_year>-W<iso_week>.json`：
```json
{"asof": "2026-11-06", "iso_week": "2026-W45", "total": 5024,
 "done": ["000001", ...], "failed": {"600xxx": {"error": "...", "attempts": 1}},
 "started_at": "...", "updated_at": "...", "status": "running|done|paused"}
```
★ **不能拿 analysis 缓存当进度台账**：`ANALYSIS_TTL=6h`（跑几小时就会过期 ✗）·
`CACHE_KEEP_NEWEST=1000`（5024 个文件会被删到 1000 ✗ —— 虽然只在"全部超龄"时触发，
但**不该把进度押在会被清理的东西上** ✗）
⇒ 台账**独立文件**，`cleanup_stale_cache` 只清 `analysis_*` / `chart_asof_*` ⇒ **不会被清** ✓
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, time as dtime
from typing import Any, Callable

# ★ INV-42/45：`NetworkForbiddenInLock` 是 `DataUnavailable` 的子类 ⇒
#   任何 `except Exception` 都会顺手接住它 ⇒ 必须**先**显式 re-raise ✗
from .datasource import NetworkForbiddenInLock

#: 开盘 / 收盘时刻（★ 与 `service._asof_phase` 的 15:30 口径一致 —— **不是 15:00**）
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(15, 30)

#: 台账落盘目录（相对 `<root>`）
LEDGER_DIR = os.path.join("data", "cache")

#: 每 N 只落一次台账（★ 太频会拖慢，太稀会丢进度）
CHECKPOINT_EVERY = 50

#: **平时**（非周末窗口）刷「持仓 ∪ 自选」的最小间隔（秒）。
#: ★ 评审 B6：`weekday_codes` 原来**生产零调用** ⇒ PRD A5 未接线 ✗
WEEKDAY_REFRESH_S = 3600.0

#: 单票失败重试上限（★ 东财限流时不要狂重试，1 次够了）
MAX_ATTEMPTS = 2


# --------------------------------------------------------------------------- #
# 纯函数：窗口判定（可注入日历 / now ⇒ 判据能确定性测）
# --------------------------------------------------------------------------- #
def is_weekend_window(
    *,
    now: datetime,
    trade_dates: list[str],
    is_trade_date: Callable[[str], bool] | None = None,
) -> bool:
    """是否处于「本周期最后一个交易日收盘 ~ 下一周期第一个交易日开盘前」的窗口。

    ★ **四条验证**（`test-spec-ptrade-v10.md` V10-A4a 会逐条测）：
    * 周五收盘后 ⇒ `True`
    * **周四**收盘后（周五是节假日，本周最后交易日=周四）⇒ **`True`** ← ★ 专打 `weekday()==4` 的错法
    * **周一盘中**（10:00）⇒ **`False`** ← ★ Critic B3 补的：否则周一 10:00 也 True ⇒ 盘中触发全市场 ✗
    * 周一盘前 ⇒ `True`（还在窗口内）
    * 周二 ⇒ `False`

    ★ **纯函数**：日历**注入**（不读 IO），`now` 注入 ⇒ 判据可确定性构造场景 ✓
    """
    today = now.strftime("%Y-%m-%d")
    now_t = now.time()
    cal = sorted(set(str(d)[:10] for d in trade_dates))
    if not cal:
        return False

    # ★ 修正 1：**当天是交易日且已开盘未收盘 ⇒ 不在窗口**（盘中绝不触发全市场）
    if is_trade_date is not None and is_trade_date(today) and MARKET_OPEN <= now_t < MARKET_CLOSE:
        return False

    # 最近**已收盘**交易日
    if now_t >= MARKET_CLOSE and today in cal:
        prev_td = today
    else:
        earlier = [d for d in cal if d < today]
        if not earlier:
            return False
        prev_td = earlier[-1]

    # 之后第一个交易日
    later = [d for d in cal if d > today]
    if not later:
        # ★ 修正 2：日历没有未来日期（新浪 `tool_trade_date_hist_sina` 是**历史**日历）。
        #
        # ★★ code-review B2（HIGH）：原回退 `return prev_td == cal[-1]` **错了** ✗
        #   生产日历只到「今天」⇒ `later` **恒空** ⇒ **周一 16:00**：`today in cal` 且
        #   `prev_td == today == cal[-1]` ⇒ 判 **True** ⇒ **扫描跑在周一晚**，
        #   而 ISO 周台账随后把该周标 done ⇒ **该周周五收盘后不再扫** ✗（且 4–8h 占工作日晚间）
        #
        # ⇒ 回退只许认「**今天不是交易日**」或「**今天已是周五及以后**」：
        #   * 周一~周四的交易日 ⇒ 绝不可能是"本周最后一个交易日"的窗口 ⇒ False ✓
        #   * 周五收盘后 / 周六 / 周日 ⇒ True ✓
        #   * 周四（周五休市）⇒ 历史日历**无法区分** ⇒ 按约定 False（由周五/周六轮次兜住）✓
        return prev_td == cal[-1] and (today not in cal or now.weekday() >= 4)

    next_td = later[0]
    # ★ 核心判定：prev / next 是否**跨 ISO 周**
    pw = datetime.strptime(prev_td, "%Y-%m-%d").isocalendar()
    nw = datetime.strptime(next_td, "%Y-%m-%d").isocalendar()
    return (pw[0], pw[1]) != (nw[0], nw[1])


def week_anchor(now: datetime, trade_dates: list[str]) -> str:
    """**ISO 周台账的锚点日期** = 窗口起点 = **最近一个已收盘交易日**。

    ## 为什么不能用 `now`（复审 R3，HIGH）
    窗口是「**周五收盘 → 周一开盘**」，而 ISO 周从**周一 00:00** 起 ⇒ **跨周**：
    * 周一 00:00–09:30 ⇒ 窗口仍为 True（盘中排除只覆盖 09:30–15:30），
      但 `now` 已属 **W+1** ⇒ 台账 W+1 不存在 ⇒ **再跑一整批**（8–16h 重复烧）✗
    * 该次跑把 **W+1** 标 done ⇒ **W+1 的周五收盘后不再跑**
      ⇒ A4 从「每周五」漂成「**每周一凌晨**」，且数据仍是上周五的 ✗
    * 状态端点也按 `now` 读 ⇒ 周一盘前看不到真实进度 ✗

    ## 锚点口径（四条都正确）
    * 周五 16:00 ⇒ 今天（周五）⇒ **W** ✓
    * 周六 / 周日 ⇒ prev_trade_date ⇒ **周五** ⇒ **W** ✓
    * 周一盘前 ⇒ prev_trade_date ⇒ **上周五** ⇒ **W**（不是 W+1）✓
    * 次周五 16:00 ⇒ 今天 ⇒ **W+1** ✓
    """
    today = now.strftime("%Y-%m-%d")
    cal = sorted(set(str(d)[:10] for d in (trade_dates or [])))
    if now.time() >= MARKET_CLOSE and today in cal:
        return today
    earlier = [d for d in cal if d < today]
    return earlier[-1] if earlier else today


def iso_week_key(dt: datetime) -> str:
    """ISO 周键：`2026-W45`（★ 用它做**幂等**：一周最多跑一轮）。"""
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


# --------------------------------------------------------------------------- #
# 台账
# --------------------------------------------------------------------------- #
def ledger_path(cfg, iso_week: str) -> str:
    return os.path.join(str(cfg.root), LEDGER_DIR, f"score_refresh_{iso_week}.json")


def read_ledger(cfg, iso_week: str) -> dict[str, Any] | None:
    p = ledger_path(cfg, iso_week)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError, TypeError):  # ★ 建议 3：收窄
        return None


def write_ledger(cfg, iso_week: str, doc: dict[str, Any]) -> None:
    """原子写台账 —— ★ **必须走 `store.atomic_write_json`**，不许自己 `os.replace`。

    ★ 为什么（`tests/test_store.py::test_meta_only_store_may_call_os_replace`）：
      `watchlist.py` 曾自带一份**无重试**的原子写，在 4 个读者线程下实测 60 次写**失败 50 次**
      （`PermissionError errno=13` —— Windows 上读者持句柄时 `os.replace` 会失败）。
      ⇒ **写方重试必须只有一处落点**（`store.py`），否则每个新模块都把同一个 bug 重新引入 ✗
      ⇒ 这里用 `store.atomic_write_json`（`tmp → fsync → os.replace` + 有界重试）✓
    """
    p = ledger_path(cfg, iso_week)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    from . import store

    store.atomic_write_json(p, doc)


# --------------------------------------------------------------------------- #
# 范围：周末 = 全市场；平时 = 持仓 ∪ 自选
# --------------------------------------------------------------------------- #
def _is_a_share(code: str) -> bool:
    """6 位 A 股代码：**排除 ETF/LOF/B 股**（★ Critic B5：`UNIVERSE_CODES` 里有 3 只 ETF ✗）。

    A 股：沪 `600/601/603/605/688/689` · 深 `000/001/002/003/300/301` · 北 `4xx/8xx/92x`（本仓库不含北交所）
    """
    raw = str(code or "").strip()
    # ★ 必须是**恰好 6 位纯数字** —— 先判长度再 zfill，
    #   否则 "".zfill(6) == "000000" 会以 "00" 开头 ⇒ 空代码被误判成 A 股 ✗
    if len(raw) != 6 or not raw.isdigit():
        return False
    return raw.startswith(("60", "68", "00", "30"))


def weekend_codes(cfg, ds) -> list[str]:
    """周末全市场代码：**spot 快照 rows** 过滤成 6 位 A 股 ✓

    ★ 不用 `ds.universe()` —— 实测它只有 **32 只硬编码**（`UNIVERSE_CODES`）且**含 3 只 ETF** ✗
    ★ 不用 `ds.spot()`（会联网刷新）—— 用 `_spot_frame_raw()` **只读缓存** ✓
    """
    try:
        frame, _ = ds._spot_frame_raw()  # noqa: SLF001 - 只读缓存
    except NetworkForbiddenInLock:
        raise                      # ★ 建议 3：未来若这里加守卫，不许被静默吞掉
    except (OSError, ValueError, TypeError):
        return []
    if frame is None or not len(frame):
        return []
    out: list[str] = []
    for _, row in frame.iterrows():
        code = str(row.get("code") or "").zfill(6)
        if not _is_a_share(code):
            continue
        # ★ 排除停牌 / 成交额为 0
        try:
            if float(row.get("volume") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        out.append(code)
    return out


def weekday_codes(cfg, state, watchlist_items: list[dict]) -> list[str]:
    """平时范围 = **持仓 ∪ 自选池**（≤40 只）✓"""
    codes: list[str] = []
    for c in (getattr(state, "positions", {}) or {}).keys():
        if _is_a_share(c):
            codes.append(str(c).zfill(6))
    for item in watchlist_items or []:
        c = str((item or {}).get("code") or "").zfill(6)
        if _is_a_share(c):
            codes.append(c)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# --------------------------------------------------------------------------- #
# 刷新器（单趟循环 + 可暂停）
# --------------------------------------------------------------------------- #
class ScoreRefresher:
    """周末全市场分数刷新器（**单趟循环**，可暂停，进度从台账恢复）。"""

    def __init__(
        self,
        cfg,
        ds,
        *,
        now_fn: Callable[[], datetime] = datetime.now,
        refresh_s: float = 600.0,
    ) -> None:
        self.cfg = cfg
        self.ds = ds
        self._now_fn = now_fn
        self._refresh_s = float(refresh_s)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        #: 内存态进度（★ 重启会丢 ⇒ 以**台账**为准）
        self.state: dict[str, Any] = {"status": "idle", "done": 0, "total": 0,
                                      "asof": "", "iso_week": "", "last_error": "",
                                      "started_at": "", "updated_at": ""}
        self.codes: list[str] = []
        #: ★ B6：平时分支的上次运行时刻（monotonic，避免墙钟回拨）
        self._last_weekday_at: float = 0.0

    # ---- 只读状态 ----
    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    # ---- 生命周期 ----
    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="score-refresher", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    # ---- 主循环（★ 单趟，不是 per-tick）----
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self._tick()
            except Exception as exc:  # noqa: BLE001 - 绝不让线程死掉（下一次 tick 还能跑）
                with self._lock:
                    self.state["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                ran = False
            # ★ 固定墙钟节拍**只用于"下次什么时候再来看看"**，
            #   真正的批量工作在 `_tick` 里**一口气跑完**（不是每 tick 一只票）✓
            if not ran:
                for _ in range(int(self._refresh_s)):
                    if self._stop.wait(1.0):
                        return

    def _tick(self) -> bool:
        """检查窗口 → 若在窗口内且本周未跑过 ⇒ **跑完整批**（单趟）。返回是否真的干了活。"""
        now = self._now_fn()
        if getattr(self.ds, "offline", False):
            return False                       # ★ `--offline` 硬闸：一次都不跑
        try:
            trade_dates = list(self.ds.trade_dates())
        except NetworkForbiddenInLock:
            raise                      # ★ INV-42/45：锁内联网必须响亮地炸，不许降级成"本轮不跑"
        except Exception:  # noqa: BLE001 - 日历取不到 ⇒ 本轮不跑（不猜）
            return False
        if not is_weekend_window(
            now=now,
            trade_dates=trade_dates,
            is_trade_date=lambda d: d in set(trade_dates),
        ):
            # ★★ B6（评审 MEDIUM）：**平时分支** —— PRD A5「平时算持仓 ∪ 自选池」。
            #   原实现只跑周末 ⇒ `weekday_codes` **生产零调用**（死代码）✗
            #   ⇒ 这里按 `WEEKDAY_REFRESH_S` 节流跑一次（≤40 只，几分钟，不是 4–8h）
            return self._weekday_pass(now)
        # ★★ 复审 R3：周键锚到**窗口起点**（最近已收盘交易日），**不是 `now`** ——
        #   否则周一 00:00–09:30 会算成 W+1 ⇒ 首周双跑 8–16h + 次周五漏跑 ✗
        week = iso_week_key(datetime.strptime(week_anchor(now, trade_dates), "%Y-%m-%d"))
        doc = read_ledger(self.cfg, week)
        if isinstance(doc, dict) and doc.get("status") == "done":
            return False                       # ★ ISO 周幂等：本周已跑完
        return self._run_batch(now, week, doc)

    def _weekday_pass(self, now: datetime) -> bool:
        """**平时**（非周末窗口）刷「持仓 ∪ 自选」—— ≤40 只，按 `WEEKDAY_REFRESH_S` 节流。

        ★ PRD A5：用户原话「平时你可以给我算**持仓的和自选池的**」。
        ★ 节流理由：盘中有 5s 轮询的 UI，这里不能跟着跑；1 小时一次足够让分数「当天内稳定」。
        """
        import time as _time

        mono = _time.monotonic()
        if self._last_weekday_at and (mono - self._last_weekday_at) < WEEKDAY_REFRESH_S:
            return False
        try:
            from . import store, watchlist

            state = store.read_state(self.cfg)
            items = watchlist.list_watchlist(self.cfg)
        except (OSError, ValueError):
            return False
        if state is None:
            return False
        codes = weekday_codes(self.cfg, state, items)
        if not codes:
            return False
        asof = ""
        try:
            asof = str(self.ds.latest_trade_asof(codes[:5]) or "")
        except NetworkForbiddenInLock:
            raise
        except (OSError, ValueError, RuntimeError):
            asof = ""
        if not asof:
            return False
        self._last_weekday_at = mono
        with self._lock:
            self.state.update(
                status="weekday", total=len(codes), done=0, asof=asof,
                iso_week="", updated_at=now.strftime("%Y-%m-%dT%H:%M:%S"),
                started_at=now.strftime("%Y-%m-%dT%H:%M:%S"), last_error="",
            )
        # ★ 复用同一个 scorer（同样适用于 ≤40 只的小批）
        from . import chart as chart_mod
        from . import strategy

        scorer = strategy.CanslimScorer(self.cfg, self.ds, offline=False)
        done = 0
        for code in codes:
            if self._stop.is_set():
                break
            try:
                result = scorer.score(code, asof)
                payload = chart_mod.analysis_payload_from_result(
                    result, self.cfg,
                    computed_at=self._now_fn().strftime("%Y-%m-%dT%H:%M:%S"),
                )
                chart_mod.write_cached_analysis(self.cfg, code, asof, payload)
                done += 1
            except NetworkForbiddenInLock:
                raise
            except Exception as exc:  # noqa: BLE001 - 单票失败不拖垮整轮（与周末批次口径一致）
                with self._lock:
                    self.state["last_error"] = f"{code}: {type(exc).__name__}: {exc}"[:160]
            with self._lock:
                self.state["done"] = done
        with self._lock:
            self.state["status"] = "idle"
        return True

    def _run_batch(self, now: datetime, week: str, prev: dict | None) -> bool:
        codes = weekend_codes(self.cfg, self.ds)
        if not codes:
            return False
        asof = ""
        try:
            asof = str(self.ds.latest_trade_asof(codes[:5]) or "")
        except NetworkForbiddenInLock:
            raise                      # ★ INV-42/45：同上
        except Exception:  # noqa: BLE001
            asof = ""
        # ★ B4：`asof` 拿不到就**本轮不跑** —— 否则 `analysis_<code>_` 空 key
        #   会写 5024 个**永久读不到**的垃圾缓存（评审 B4）✗
        if not asof:
            return False
        done: set[str] = set((prev or {}).get("done") or [])
        failed: dict[str, Any] = dict((prev or {}).get("failed") or {})
        with self._lock:
            self.state.update(
                status="running", total=len(codes), done=len(done), asof=asof, iso_week=week,
                started_at=(prev or {}).get("started_at") or now.strftime("%Y-%m-%dT%H:%M:%S"),
                updated_at=now.strftime("%Y-%m-%dT%H:%M:%S"), last_error="",
            )
        self.codes = codes

        # ★★ **一个 scorer 跑完整批**（Critic M3：每票新建是 103h 的根因）
        from . import strategy

        scorer = strategy.CanslimScorer(self.cfg, self.ds, offline=False)
        from . import chart as chart_mod

        n_since_ckpt = 0
        for code in codes:
            if self._stop.is_set():
                with self._lock:
                    self.state["status"] = "paused"
                write_ledger(self.cfg, week, {
                    "asof": asof, "iso_week": week, "total": len(codes),
                    "done": sorted(done), "failed": failed,
                    "started_at": self.state["started_at"],
                    "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "status": "paused",
                })
                return True
            if code in done:
                continue
            attempts = int((failed.get(code) or {}).get("attempts") or 0)
            try:
                if (failed.get(code) or {}).get("dead"):
                    continue               # ★ 建议 1：已达重试上限 ⇒ 跳过（不再无谓烧时间）
                result = scorer.score(code, asof)
                # ★★ B1（CRITICAL）：**必须**用 chart 的唯一构造器。
                #   曾用 `result.to_dict()` —— 无 `ok`、`factors` 是 list ⇒ 判 failed ⇒ 120s 过期
                #   ⇒ **整个周末扫描（4–8h × 5024 只）2 分钟后全部作废** ✗
                payload = chart_mod.analysis_payload_from_result(
                    result, self.cfg, computed_at=self._now_fn().strftime("%Y-%m-%dT%H:%M:%S")
                )
                chart_mod.write_cached_analysis(self.cfg, code, asof, payload)
                done.add(code)
                failed.pop(code, None)
            except NetworkForbiddenInLock:
                # ★★ 复审 R2（HIGH，纪律回归）：v1.16 重构时把 v1.15 内层的
                #   `isinstance(exc, NetworkForbiddenInLock): raise` 删了、**却没换成**标准形态
                #   ⇒ 宽 `except Exception` 会把**守卫错误**记成"这只票算失败"而静默吞掉 ✗
                #   （INV-42/45；且 `REGISTERED_GUARD_SITES` 仍登记着 score_refresher::_run_batch）
                #   ⇒ 冒到 `_run` 的兜底记 last_error、等下一 tick，**不要**记成单票 failed
                raise
            except Exception as exc:  # noqa: BLE001 - **单票失败不拖垮整批**（Critic B8 允许的宽 except）
                attempts += 1
                # ★ 建议 1：`MAX_ATTEMPTS` 原来两个分支写**完全相同**的 dict ⇒
                #   "重试上限"根本没生效，failed 票下一轮还会重试 ✗
                #   ⇒ 达到上限后记 `dead`，**跳过**后续轮次（不再白烧时间）
                failed[code] = {
                    "error": f"{type(exc).__name__}: {exc}"[:120],
                    "attempts": attempts,
                    "dead": attempts >= MAX_ATTEMPTS,
                }
            n_since_ckpt += 1
            with self._lock:
                self.state["done"] = len(done)
                self.state["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            if n_since_ckpt >= CHECKPOINT_EVERY:
                n_since_ckpt = 0
                write_ledger(self.cfg, week, {
                    "asof": asof, "iso_week": week, "total": len(codes),
                    "done": sorted(done), "failed": failed,
                    "started_at": self.state["started_at"],
                    "updated_at": self.state["updated_at"], "status": "running",
                })

        write_ledger(self.cfg, week, {
            "asof": asof, "iso_week": week, "total": len(codes),
            "done": sorted(done), "failed": failed,
            "started_at": self.state["started_at"],
            "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "status": "done" if len(done) + len(failed) >= len(codes) else "running",
        })
        with self._lock:
            self.state["status"] = "done"
            self.state["done"] = len(done)
        return True
