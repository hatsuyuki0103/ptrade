"""**spot 快照的后台周期刷新器**（v5）。

## 为什么需要它（实测根因）

`/api/quote`（自选池的现价/涨跌幅）走 `DataSource._spot_frame_raw()` —— **只读缓存**。
而全仓刷新 spot 的只有两处：**启动预热** 与 **下单/结算前**。
⇒ 实测：`data/cache/spot.json` 的 `fetched_at` **228 秒不变**，连续 3 次 `/api/quote`
   **返回完全相同的价** ⇒ 用户看到的「盘中自选涨幅不刷新」**根因在服务端，不在前端轮询**。

## 设计（每条都有理由，不要随手改）

* **间隔 60s**：与 `datasource.TTL_SPOT = 60` **同源**（缓存本来就是 60s 寿命）；
  实测全市场快照（5572 行 / ~1.5 MB）抓一次要**十几秒** ⇒ 20s 间隔的占空比会到 75%，60s ⇒ 25%。
* **惰性门控**：只有**最近 ``UI_ACTIVE_WINDOW_S`` 秒内**有 UI 请求（`/api/quote` /
  `/api/watchlist` / `/api/health`）才刷 —— 没人看的时候不打上游。
* **自己的非阻塞 guard**：`DataSource._cached` 的 `_single_flight_lock` 是**按 cache key** 的
  单飞；刷新器**不去抢它**（抢了会让 UI 的 `spot()` 排队）。本模块有自己的 `Lock`，
  `acquire(blocking=False)` 失败就**跳过本轮**。
* **固定墙钟节拍**：`next_at = max(now, next_at + interval)` —— 不是 `fetch(); sleep(60)`，
  否则每次抓取的十几秒会累加，导致**越跳越久**。
* **只在锁外调用**：`_fetch_spot` 内部走 `_ak()`，而 `_ak()` 有 `store.in_critical_section()`
  守卫（锁内联网直接抛 `NetworkForbiddenInLock`）。所以「锁外」不是纪律，是**唯一可行姿势**。
* **失败即跳过**：上游 `stock_zh_a_spot_em` 实测多次 `RemoteDisconnected` ⇒
  **吞掉 `DataUnavailable`/`OSError` 后等下一个 tick**，绝不因此退出线程
  （否则一次网络抖动就永久停止刷新 = 更隐蔽的「不刷新」）。
* **非盘中 / `--offline` 绝不刷**：`--offline` 是服务端硬闸（`INV-10/35`），不得绕过。

## 判据（`tests/test_ui_v5.py` 的 V5-9 / V5-14）

* 间隔可注入（`SPOT_REFRESH_S` 可 monkeypatch/构造参数）⇒ 测试用 800ms 跑 2 个周期；
* **源码里不得出现** `state_lock` / `locked_update` / `in_critical_section`（`INV-42` 锁内禁网）；
* 非盘中 / `--offline` 下 socket 尝试数 = 0。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

#: spot 快照的后台刷新间隔（秒）。★ 与 ``datasource.TTL_SPOT`` **同源**，改一个要想另一个。
SPOT_REFRESH_S = 60.0

#: UI 活跃窗口（秒）：最近这么久内有 UI 请求才刷新（惰性门控）。
UI_ACTIVE_WINDOW_S = 120.0


class SpotRefresher:
    """后台 spot 刷新器（daemon 线程 + 可注入的取数与时钟）。"""

    def __init__(
        self,
        refresh: Callable[[], Any],
        *,
        should_run: Callable[[], bool],
        interval_s: float = SPOT_REFRESH_S,
        ui_active_window_s: float = UI_ACTIVE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._refresh = refresh
        self._should_run = should_run
        self._interval = float(interval_s)
        self._ui_window = float(ui_active_window_s)
        self._clock = clock
        self._lock = threading.Lock()          # ★ 自己的非阻塞 guard（不碰 datasource 的单飞）
        self._stop = threading.Event()
        self._last_ui = 0.0
        self._next_at = 0.0
        self._thread: threading.Thread | None = None
        #: 统计（测试/诊断用；也顺带证明「真的跑了」）
        self.ticks = 0
        self.refreshed = 0
        self.skipped_busy = 0
        self.skipped_inactive = 0
        self.skipped_closed = 0
        self.failures = 0

    # ── 外部调用 ────────────────────────────────────────────────────────
    def note_ui_activity(self) -> None:
        """UI 请求进来时调一次（`/api/quote` / `/api/watchlist` / `/api/health`）。"""
        self._last_ui = self._clock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._next_at = self._clock() + self._interval
        self._thread = threading.Thread(target=self._loop, name="ptrade-spot-refresher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    # ── 内部 ────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            # ★ **固定墙钟**：`next_at = max(now, next_at + interval)` —— 抓取耗时不会累积。
            self._next_at = max(now, self._next_at + self._interval)
            wait = max(0.0, self._next_at - now)
            if self._stop.wait(wait):
                return
            self.tick()

    def tick(self) -> bool:
        """执行**一轮**判定 + 可能的刷新。返回是否真的刷新了。测试直接调这个（不用等线程）。"""
        self.ticks += 1
        if not self._should_run():
            self.skipped_closed += 1
            return False
        if self._clock() - self._last_ui > self._ui_window:
            self.skipped_inactive += 1
            return False
        if not self._lock.acquire(blocking=False):
            self.skipped_busy += 1
            return False
        try:
            self._refresh()
            self.refreshed += 1
            return True
        except Exception:                      # noqa: BLE001 - ★ 一次网络抖动不得终止刷新
            self.failures += 1
            return False
        finally:
            self._lock.release()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "ticks": self.ticks,
            "refreshed": self.refreshed,
            "skipped_busy": self.skipped_busy,
            "skipped_inactive": self.skipped_inactive,
            "skipped_closed": self.skipped_closed,
            "failures": self.failures,
        }


def start_spot_refresher(
    refresh: Callable[[], Any],
    *,
    should_run: Callable[[], bool],
    interval_s: float = SPOT_REFRESH_S,
    ui_active_window_s: float = UI_ACTIVE_WINDOW_S,
) -> SpotRefresher:
    """建并启动刷新器（幂等：调用方自己保证只调一次；`WebApp` 在启动维护里调）。"""
    refresher = SpotRefresher(
        refresh, should_run=should_run, interval_s=interval_s, ui_active_window_s=ui_active_window_s
    )
    refresher.start()
    return refresher
