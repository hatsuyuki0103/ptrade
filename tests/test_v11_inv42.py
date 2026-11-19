"""V11-18 · INV-42 行为判据（code-review S3 点名要补的那条）。

## ★ 第一版写错了（我的错，记下来）
我一开始断言「**锁内不许调 DS**」⇒ 立刻假红：
```
★ INV-42 违规：锁内取数 ['trade_dates', 'quote']
```
**真相**：`engine.roll_to` 在锁内**确实**会调 `ds.trade_dates()`（`engine.py:109`）与
`_mark_prices → ds.quote`（`:131`）—— 但它们是**缓存命中**（`prewarm_before_write` 已填好）
⇒ **不联网** ✓
⇒ **INV-42 管的是「锁内不联网」，不是「锁内不调 DS」** ✗ 我的口径搞错了

## 正确的判据（也是评审真正担心的那个回归）
评审原话：
> 「若将来 engine 新增锁内取数、或持仓在 peek→取锁 之间变化（另一进程成交），
>   冷缓存会让 `_mark_prices` 在锁内联网」

⇒ 断言：**锁内调用的 DS 方法必须全部落在"预热白名单"里**
   （`trade_dates` / `is_trade_date` / `prev_trade_date` / `next_trade_date` /
    `spot` / `quote` / `last_close` / `margin_targets` …）
★ 一旦 engine 新增一个**没被预热**的锁内取数 ⇒ 这条立刻红 ✓
"""

from __future__ import annotations

import pathlib
from datetime import datetime

from ptrade import core, service, store
from ptrade.config import load_config
from ptrade.datasource import DataUnavailable

ROOT = pathlib.Path(__file__).resolve().parent.parent

CAL = [
    "2026-11-06",
    "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
    "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20",
]

#: ★ **预热白名单**：`service.prewarm_for_write` 会把这些填进缓存
#:   （review 逐键核过：`service.py:229-298`）
PREWARMED = frozenset({
    "trade_dates",
    "is_trade_date",
    "prev_trade_date",
    "next_trade_date",
    "spot",
    "quote",
    "last_close",
    "margin_targets",
    "latest_trade_date",
    "latest_trade_asof",
    "universe",
})


class _SpyDS:
    """最小 DS 桩 + **记录每个取数"当时在不在临界区"**。"""

    offline = False

    def __init__(self):
        self.inside: list[tuple[str, bool]] = []

    def _record(self, name: str) -> None:
        try:
            in_lock = bool(store.in_critical_section())
        except Exception:  # noqa: BLE001 - 探测不到就当"不在锁内"（宁可漏报不假红）
            in_lock = False
        self.inside.append((name, in_lock))

    def is_trade_date(self, d):
        self._record("is_trade_date")
        return str(d)[:10] in set(CAL)

    def prev_trade_date(self, d):
        self._record("prev_trade_date")
        earlier = [x for x in CAL if x < str(d)[:10]]
        if not earlier:
            raise DataUnavailable("no earlier")
        return earlier[-1]

    def next_trade_date(self, d):
        self._record("next_trade_date")
        later = [x for x in CAL if x > str(d)[:10]]
        return later[0] if later else None

    def trade_dates(self):
        self._record("trade_dates")
        return list(CAL)

    def spot(self, codes=None):
        self._record("spot")
        return None

    def quote(self, code, trade_date=None, **kw):
        self._record("quote")
        return {"last": 3.20, "name": code}

    def last_close(self, code, trade_date=None, **kw):
        self._record("last_close")
        return 3.20

    def margin_targets(self, *a, **k):
        self._record("margin_targets")
        return None

    def __getattr__(self, name):
        def _benign(*a, **k):
            self._record(f"other:{name}")
            return None

        return _benign


def _root_with_account(tmp_path, trade_date="2026-11-09"):
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True, exist_ok=True)
    cfg = load_config(str(root))
    st = core.new_state(cfg)
    st.trade_date = trade_date
    st.initial_cash = 500000.0
    st.available_cash = 499961.96
    st.frozen_collateral = 50000.0
    pos = core.Position(code="600594", name="益佰制药")
    pos.short_qty = 15700
    pos.short_cost = 3.19
    pos.last_price = 3.19
    st.positions = {"600594": pos}
    store.save_state(cfg, st, source="v11-test")
    return cfg, root


def test_v11_18_locked_reads_are_all_prewarmed(tmp_path):
    """★ V11-18（INV-42）：**锁内调用的 DS 方法必须全在预热白名单里**。

    ★ 这是评审真正担心的回归面（不是"锁内零调用"）：
      engine 新增一个**没被预热**的锁内取数 ⇒ 冷缓存 ⇒ 锁内真联网 ⇒ INV-42 ✗
    """
    cfg, _ = _root_with_account(tmp_path, "2026-11-09")
    ds = _SpyDS()

    out = service.auto_settle_to_today(cfg, ds, now=datetime(2026, 11, 19, 12, 8))
    assert out["advanced"] is True, out
    assert out["to"] == "2026-11-19"
    assert store.read_state(cfg).trade_date == "2026-11-19", "账户没推进 ✗"

    # ★ 正向：取数真的发生过（否则空守卫）
    assert len(ds.inside) > 0, "DS 取数一次都没发生 ⇒ 判据是空的 ✗"

    locked = [name for name, in_lock in ds.inside if in_lock]
    # 锁内**应当**有调用（`roll_to` 会读 trade_dates / quote 等缓存），所以不断言为空
    assert locked, "锁内一次 DS 都没调 ⇒ 说明 `_mutate` 没真的跑？（判据可能失效）"

    unexpected = sorted({n for n in locked if n not in PREWARMED})
    assert unexpected == [], (
        f"★ 锁内出现了**没被预热**的取数 {unexpected} ⇒ 冷缓存时会锁内联网（INV-42 ✗）。"
        f" 若这是新增的合法取数，请同时把它加进 `prewarm_for_write` 与 `PREWARMED` 白名单。"
    )

    # 锁外也必须覆盖到关键取数（预热真的做了）
    outside = {n for n, in_lock in ds.inside if not in_lock}
    for need in ("trade_dates",):
        assert need in outside, f"`{need}` 没在锁外预热 ⇒ 锁内那次就是冷缓存联网 ✗"
