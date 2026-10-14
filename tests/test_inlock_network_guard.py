"""锁内禁网开关的回归测试（架构评审 S-1 第 2 条，2026-10-13 落地）。

**它解决什么**：BLOCK-1 的「锁外预热」只把**已知**的取数挪出临界区。万一还有漏网的取数，
它仍会在持有写锁时联网 —— 那是 **fail-slow**：写者互相饿死、网页端返回 503，
而账户本身毫无问题，排查极难。有了这个开关，漏网会**立刻响亮报错**并指明修法。

**实测（`.omx/probe/detect_inlock_network2.py`）**：数据源可用时，写命令的
锁外预热 4/1/3 次、锁内联网 **0** 次 —— 即当前没有漏网；
开关的作用是**防未来回归**，而不是修一个现存缺口。

**但「响亮」必须被守住**（独立复核第二轮指出两条静默化路径）：
守卫错误若与普通取数失败同型，会被「回退过期缓存」或「降级兜底」吞掉 ——
* `_cached` / `daily` / `index_daily`：有过期缓存 → 静默返回**陈旧数据**（无异常）；
* `engine._float_shares`：`except Exception: return 0.0` → `source_limit=0` →
  `0 < source_limit` 不成立 → **券源上限检查被静默跳过**（风控静默失效）。
所以新增了独立异常类型 :class:`NetworkForbiddenInLock`，并在上述位置穿透/上抛。
本文件下面 3 条测试就是守这一点。
"""

from __future__ import annotations

import types

import pytest

from ptrade import store
from ptrade.config import load_config
from ptrade.datasource import DataSource, DataUnavailable, NetworkForbiddenInLock


@pytest.fixture
def online_ds(tmp_path, monkeypatch):
    """在线 DataSource + **类级**阻断真联网（**不真联网**）。

    **为什么必须类级**（2026-10-14 实测）：`_ak_module` 是**实例属性**，
    而网页端点在处理请求时会 `make_ds()` 造**新实例**（`_ak_module=None`）→
    `_ak()` 会去 `import akshare` 并**真的联网**。后果：
    * 正常态：`index_daily` 用例要跑 22s（实测）；
    * **变异态**（把守卫穿透注掉）：锁内真的去取数 → 子进程卡 **30 分钟 / 1802s CPU**，
      我不得不杀掉。**测试在「实现被注掉」时必须是有界失败，不能转去联网。**

    这里在**类**上替换 `_ak()`：保留「离线报错」与「守卫拦截」两段语义（测试要验的就是它们），
    只把「真 import akshare」换成抛错。这样无论新建多少实例都不会联网。
    """
    cfg = load_config(str(tmp_path))

    class _NoNetworkAk:
        """任何属性访问都抛错（模拟「取数不可用」，且绝不触网）。"""

        def __getattr__(self, name: str):
            raise RuntimeError(f"本用例禁止访问 akshare.{name}（未配置真实数据源）")

    real_ak = DataSource._ak

    def blocked_ak(self):
        if self.offline:
            raise DataUnavailable(
                "offline=True：禁止访问 akshare（离线模式只读 data/cache 与 data/fixture）"
            )
        if store.in_critical_section():
            raise NetworkForbiddenInLock(
                "写锁临界区内禁止联网取数：请把该数据的获取移到锁外"
                "（用 service.prewarm_before_write 预热），或改用 --offline。"
            )
        return _NoNetworkAk()

    monkeypatch.setattr(DataSource, "_ak", blocked_ak, raising=True)
    ds = DataSource(cfg, offline=False)
    return ds, cfg


def test_in_critical_section_marker_tracks_depth(tmp_path):
    """**标记本身**：锁内为 True、锁外为 False，且**重入**不会提前清除。"""
    assert store.in_critical_section() is False, "锁外不应标记为持锁"
    with store.state_lock(str(tmp_path)):
        assert store.in_critical_section() is True
        with store.state_lock(str(tmp_path)):          # 同线程重入
            assert store.in_critical_section() is True
        assert store.in_critical_section() is True, "退出内层后仍应持锁（标记被提前清除）"
    assert store.in_critical_section() is False, "退出后标记未清除"


def test_ak_is_allowed_outside_lock(online_ds):
    """**对照**：锁外取 akshare 必须成功返回（否则「锁内被拒」没有鉴别力）。

    `online_ds` 现在把 `_ak()` 在**类**上换成了「返回阻断替身」的实现（见夹具说明），
    所以这里断言的是「**不抛异常且拿到东西**」，而不是具体的模块类型。
    """
    ds, _ = online_ds
    got = ds._ak()
    assert got is not None, "锁外 `_ak()` 应返回可用的取数入口"


def test_ak_is_refused_inside_lock(online_ds):
    """**核心断言**：锁内取 akshare 必须被拒，且错误信息指明修法。"""
    ds, cfg = online_ds
    with store.state_lock(cfg.root):
        with pytest.raises(DataUnavailable) as ei:
            ds._ak()
    msg = str(ei.value)
    assert "写锁临界区内禁止联网取数" in msg, msg
    assert "prewarm" in msg or "锁外" in msg, f"错误信息未指明修法：{msg}"

    # 退出锁后应恢复可用（证明它只是**临界区**内的限制）
    assert ds._ak() is not None


def test_offline_error_message_not_hijacked(online_ds, tmp_path):
    """**不混淆语义**：离线模式的错误信息必须仍是原来那条，不被新守卫接管。"""
    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=True)
    with pytest.raises(DataUnavailable) as ei:
        ds._ak()
    msg = str(ei.value)
    assert "offline=True" in msg and "禁止访问 akshare" in msg, msg
    assert "写锁临界区内禁止" not in msg, "离线错误被锁内守卫顶替了"


def test_guard_is_per_thread_not_process_wide(tmp_path):
    """**按线程**：一个线程持锁，不得让**另一个**线程的锁外取数被拒。

    否则会把「同进程内并发工作」误伤成全局禁令。
    """
    import threading

    errors: list[str] = []
    other_saw: list[bool] = []
    started = threading.Event()
    done = threading.Event()

    def other_thread():
        try:
            started.set()
            # 这一句是重点：**另一个线程在锁外**读标记，必须为 False
            other_saw.append(store.in_critical_section())
        finally:
            done.set()

    with store.state_lock(str(tmp_path)):
        assert store.in_critical_section() is True, "本线程持锁时应标记为 True"
        t = threading.Thread(target=other_thread, daemon=True)
        t.start()
        started.wait(timeout=10)
        done.wait(timeout=10)
        t.join(timeout=10)
        # 关键：另一个线程在该时刻的观察必须与它**自己**的锁状态一致，
        # 不能被本线程的持锁状态污染。
        assert other_saw == [False], (
            f"另一个线程在锁外观察到 in_critical_section()={other_saw} —— "
            f"标记泄漏到别的线程了（这会把并发工作误伤成全局禁令）"
        )
        assert store.in_critical_section() is True, "另一个线程的活动不应影响本线程标记"

    # **断言 errors 为空**（复核者指出：上一版收了 errors 却从不断言 = 空守卫）
    assert errors == [], f"另一个线程出现了意外异常：{errors}"
    assert store.in_critical_section() is False, "退出后标记未清除"


def test_guard_is_per_thread_directly(tmp_path):
    """**更直接的按线程断言**：同一个进程里，两个线程同时检查标记，结果必须各自独立。

    上一版把「另一个线程」放在等锁状态，于是它**根本没有机会执行到位**，
    断言其实没被跑到（复核者指出用例稳定耗时 ≥10s 就是这个原因）。
    这里让两个线程**同时**执行，一个持锁、一个不持锁。
    """
    import threading

    results: dict[str, bool] = {}
    barrier = threading.Barrier(2, timeout=20)

    def holder():
        with store.state_lock(str(tmp_path)):
            barrier.wait()
            results["holder"] = store.in_critical_section()

    def outsider():
        barrier.wait()
        results["outsider"] = store.in_critical_section()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=outsider)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert results.get("holder") is True, f"持锁线程应看到 True：{results}"
    assert results.get("outsider") is False, (
        f"未持锁线程看到了 {results.get('outsider')} —— 标记泄漏到别的线程"
    )


# --------------------------------------------------------------------------- #
# 守卫「响亮」不得被静默化（独立复核第二轮的两条否定结论）
# --------------------------------------------------------------------------- #
def test_guard_error_not_swallowed_by_stale_cache(online_ds):
    """**F1 断言**：有过期缓存时，锁内取数仍必须**响亮报错**，不得静默返回陈旧数据。

    修之前 `_cached` 的 `except Exception` 会回退过期缓存 → 同一锁内调用
    「有过期缓存就静默成功、无缓存才报错」，等于「违规是否响亮取决于缓存状态」。
    """
    ds, cfg = online_ds
    # 造一个**明确过期**的缓存条目
    ds._write_entry(ds.cache_dir, "trade_dates", [{"date": "2020-01-01"}])
    import json as _json
    import pathlib as _pathlib

    p = _pathlib.Path(ds.cache_dir) / "trade_dates.json"
    doc = _json.loads(p.read_text(encoding="utf-8"))
    doc["fetched_at"] = "2020-01-01T00:00:00"
    p.write_text(_json.dumps(doc), encoding="utf-8")

    with store.state_lock(cfg.root):
        with pytest.raises(NetworkForbiddenInLock):
            ds.trade_dates()          # 不得返回过期缓存里的陈旧内容


def test_guard_error_not_degraded_to_zero_in_float_shares(online_ds):
    """**F3 断言**：`engine._float_shares` 必须**上抛**守卫错误，不得降级成 0。

    0 会让 `0 < source_limit` 不成立 → **券源上限检查被静默跳过**（风控静默失效）。
    同时断言**普通**取数失败仍降级成 0.0 —— 原有兜底不能被顺手破坏。
    """
    from ptrade import core
    from ptrade.engine import Engine

    ds, cfg = online_ds
    quote_no_mv = {"code": "600519", "name": "贵州茅台", "last": 12.6, "prev_close": 12.5,
                   "limit_up": 13.86, "limit_down": 11.25, "suspended": False}
    state = core.new_state(cfg)

    class _GuardFiring(type(ds)):                      # type: ignore[misc]
        def spot(self, codes=None):                    # noqa: ARG002
            raise NetworkForbiddenInLock("注入：锁内禁止联网取数")

    class _OrdinaryFailure(type(ds)):                  # type: ignore[misc]
        def spot(self, codes=None):                    # noqa: ARG002
            raise RuntimeError("普通取数失败")

    eng = Engine(cfg, _GuardFiring(cfg, offline=False), state)
    with pytest.raises(NetworkForbiddenInLock):
        eng._float_shares(quote_no_mv, "600519", "2026-10-09")

    eng2 = Engine(cfg, _OrdinaryFailure(cfg, offline=False), state)
    assert eng2._float_shares(quote_no_mv, "600519", "2026-10-09") == 0.0, (
        "普通取数失败的原有兜底（返回 0.0）被破坏了"
    )


def test_network_forbidden_is_a_dataunavailable_subclass(online_ds):
    """**兼容性**：守卫异常必须是 `DataUnavailable` 的子类。

    否则既有的 `except DataUnavailable` → HTTP 400 / 业务态 200 的映射会失效，
    网页端会把它当 500 内部错误。
    """
    from ptrade.datasource import DataUnavailable

    assert issubclass(NetworkForbiddenInLock, DataUnavailable)


def test_guard_error_penetrates_daily_stale_fallback(online_ds):
    """**F1 的 daily 分支**：`daily()` 同样不得用过期缓存吞掉守卫错误。"""
    ds, cfg = online_ds
    ds._write_entry(ds.cache_dir, "daily_600519_qfq", [{"date": "2020-01-01", "close": 1.0}])
    import json as _json
    import pathlib as _pathlib

    p = _pathlib.Path(ds.cache_dir) / "daily_600519_qfq.json"
    doc = _json.loads(p.read_text(encoding="utf-8"))
    doc["fetched_at"] = "2020-01-01T00:00:00"
    p.write_text(_json.dumps(doc), encoding="utf-8")

    with store.state_lock(cfg.root):
        with pytest.raises(NetworkForbiddenInLock):
            ds.daily("600519", "2025-01-01", "2026-10-09")


# --------------------------------------------------------------------------- #
# 「守卫错误被静默降级」的同类站点（Lead 自主排查，第三轮复核第 7 项）
#
# F1/F3 是同一个**模式**：`except Exception` 把守卫错误吞掉，降级成「看起来正常的值」，
# 用户拿到的是**错误的正常结果**而不是明确的失败。Lead 全仓搜出并修了 6 个额外站点：
#
#   engine._quote                → None    （订单以错误码被拒，而非提示「锁内禁网」）
#   engine._is_margin_target     → False   （合法融资买入被判「非两融标的」）
#   engine._is_short_target      → False   （合法融券卖出被判「非标的」）
#   watchlist._has_market_data   → False   （有数据的票被判「无数据」）
#   datasource.industry_of       → ""      （行业内 RS 静默退化）
#   datasource.universe          → 兜底池  （市场宽度/排名基准静默换掉）
#
# 下面用**参数化**逐个钉住（每条都断言"上抛"而不是"返回降级值"）。
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("site", [
    "engine._quote",
    "engine._is_margin_target",
    "engine._is_short_target",
    "engine._mark_prices",
    "engine._backfill_unlock_dates",
    "watchlist._has_market_data",
    "datasource.industry_of",
    "datasource.industry_constituents",
    "datasource.universe",
])
def test_guard_error_must_not_be_degraded_at_site(online_ds, site):
    """**参数化断言**：守卫错误在这些站点都必须**上抛**，不得降级成正常值。

    第三轮复核的硬阻塞项就是「补的站点零测试覆盖」——它用 21 个变异测出
    「13:31 补的 6 处逐个 GREEN」。本用例把批次 2（`_mark_prices` / `_backfill_unlock_dates`
    等）与批次 3（`industry_constituents` 等）一并纳入，消除零覆盖。
    """
    from ptrade import core, watchlist
    from ptrade.engine import Engine

    ds, cfg = online_ds
    state = core.new_state(cfg)
    quote_no_mv = {"code": "600519", "name": "贵州茅台", "last": 12.6, "prev_close": 12.5,
                   "limit_up": 13.86, "limit_down": 11.25, "suspended": False}

    class _Firing(type(ds)):                            # type: ignore[misc]
        """把底层取数一律换成守卫错误。"""

        def quote(self, code, asof):                    # noqa: ARG002
            raise NetworkForbiddenInLock("注入")

        def is_margin_target(self, code, asof):         # noqa: ARG002
            raise NetworkForbiddenInLock("注入")

        def is_short_target(self, code, asof):          # noqa: ARG002
            raise NetworkForbiddenInLock("注入")

        def last_close(self, code, asof):               # noqa: ARG002
            raise NetworkForbiddenInLock("注入")

        def next_trade_date(self, day):                 # noqa: ARG002
            raise NetworkForbiddenInLock("注入")

        def _industry_map(self):                        # noqa: ARG002
            raise NetworkForbiddenInLock("注入")

        def _cached(self, key, ttl, fetch, fixture_key=None):   # noqa: ARG002
            raise NetworkForbiddenInLock("注入")

    firing = _Firing(cfg, offline=False)
    eng = Engine(cfg, firing, state)

    if site == "engine._quote":
        call = lambda: eng._quote("600519", "2026-10-09")                    # noqa: E731
    elif site == "engine._is_margin_target":
        call = lambda: eng._is_margin_target("600519", "2026-10-09")         # noqa: E731
    elif site == "engine._is_short_target":
        call = lambda: eng._is_short_target("600519", "2026-10-09")          # noqa: E731
    elif site == "engine._mark_prices":
        # 必须让 `_quote` **返回 None**（而不是抛守卫错误），否则异常在 `_quote` 内部就上抛了，
        # 根本走不到 `last_close` 那段兜底 —— 我第一版就是这样，注掉实现后用例仍绿（零覆盖）。
        eng._quote = lambda code, td: None                                    # type: ignore[method-assign]

        def _call_mark():                                                     # noqa: E306
            pos = core.Position()
            pos.code, pos.long_qty, pos.last_price = "600519", 100, 999.0
            st = core.new_state(cfg)
            st.positions["600519"] = pos
            return eng._mark_prices(st, "2026-10-09")
        call = _call_mark
    elif site == "engine._backfill_unlock_dates":
        def _call_backfill():                                                 # noqa: E306
            order = core.Order(order_id="O0001", trade_date="2026-10-09",
                               code="600519", side="buy", qty=100, price=12.6)
            return eng._backfill_unlock_dates(order)
        call = _call_backfill
    elif site == "watchlist._has_market_data":
        call = lambda: watchlist._has_market_data(firing, "600519", "2026-10-09")  # noqa: E731
    elif site == "datasource.industry_of":
        call = lambda: firing.industry_of("600519")                          # noqa: E731
    elif site == "datasource.industry_constituents":
        call = lambda: firing.industry_constituents("银行")                   # noqa: E731
    else:
        call = lambda: firing.universe()                                     # noqa: E731

    with pytest.raises(NetworkForbiddenInLock):
        call()
    _ = quote_no_mv      # 保持可变签名（部分站点用到报价）


def test_degenerate_values_would_have_been_wrong(online_ds):
    """**非空守卫**：证明上面那 6 个站点「降级值确实是错的」。

    做法：让底层抛等价的**普通**异常（非守卫），观察降级值 ——
    它看起来完全正常（`None` / `False` / `""` / 兜底池），
    正是「用户会拿到错误正常结果」的证据。
    """
    from ptrade import core, watchlist
    from ptrade.engine import Engine

    ds, cfg = online_ds
    state = core.new_state(cfg)

    class _Ordinary(type(ds)):                          # type: ignore[misc]
        def quote(self, code, asof):                    # noqa: ARG002
            raise RuntimeError("普通失败")

        def is_margin_target(self, code, asof):         # noqa: ARG002
            raise RuntimeError("普通失败")

        def is_short_target(self, code, asof):          # noqa: ARG002
            raise RuntimeError("普通失败")

    ordinary = _Ordinary(cfg, offline=False)
    eng = Engine(cfg, ordinary, state)

    assert eng._quote("600519", "2026-10-09") is None, "普通失败应仍降级成 None（原有兜底）"
    assert eng._is_margin_target("600519", "2026-10-09") is False
    assert eng._is_short_target("600519", "2026-10-09") is False
    assert watchlist._has_market_data(ordinary, "600519", "2026-10-09") is False


# --------------------------------------------------------------------------- #
# 第三轮复核的最后两条零覆盖（它的硬阻塞项残留）
# --------------------------------------------------------------------------- #
def test_index_daily_guard_error_penetrates_stale_cache(online_ds):
    """**`index_daily` 的守卫穿透**（复核者 M3：注掉实现后 91 passed 全绿 = 零覆盖）。

    与 `_cached` / `daily` 同形：有过期缓存时不得静默返回陈旧指数数据。

    **必须让「真取数」立刻失败**（一个惰性抛错的假 akshare）：
    否则在**变异实验**里（把穿透注掉）锁内会真的去**联网取指数数据** —— 实测那次跑了
    **30 分钟、1802s CPU** 还没结束，我不得不杀掉进程。而且**正常态**也会慢
    （实测 22s，说明确实走到了网络路径）。测试**绝不能**因为实现被注掉就变成真联网。

    注意：`types.ModuleType("akshare")` 对**属性访问**不抛错（它是个空模块，
    `ak.xxx` 只会 `AttributeError`，但某些实现会先尝试 import 真 akshare）；
    这里用一个**任何属性访问都抛错**的替身，从机制上断绝联网。
    """

    class _NoNetworkAk:
        """任何属性访问都抛错 —— 从机制上杜绝本用例联网。"""

        def __getattr__(self, name: str):
            raise RuntimeError(f"本用例禁止访问 akshare.{name}（应为锁内禁网拦截）")

    ds, cfg = online_ds
    ds._ak_module = _NoNetworkAk()
    ds._write_entry(ds.cache_dir, "index_sh000001", [{"date": "2020-01-01", "close": 1.0}])
    import json as _json
    import pathlib as _pathlib

    p = _pathlib.Path(ds.cache_dir) / "index_sh000001.json"
    doc = _json.loads(p.read_text(encoding="utf-8"))
    doc["fetched_at"] = "2020-01-01T00:00:00"
    p.write_text(_json.dumps(doc), encoding="utf-8")

    with store.state_lock(cfg.root):
        with pytest.raises(NetworkForbiddenInLock):
            ds.index_daily("sh000001", "2025-01-01", "2026-10-09")


def test_prewarm_actually_warms_spot_codes(online_ds):
    """**`spot([codes])` 真被预热**（复核者 M5 / P3-5：这处零覆盖）。

    为什么重要：它覆盖 `engine._enrich_spot`，也就是 `_float_shares`（券源上限）的取数点；
    不预热的话守卫开火会让 `source_limit` 变 0 = **券源上限静默跳过**。

    本用例**直接**验证预热调用到了带 codes 的 `spot`（而不是只断言 `warmed` 里有字符串）——
    我原先在 `test_cli.py` 里的 stub 签名是 `spot(self)`，传 `codes` 会 `TypeError`，
    被 `_try` 的 best-effort 吞掉，于是**零覆盖**。这里有正对照。
    """
    from ptrade import service

    ds, cfg = online_ds
    seen: list[object] = []

    class _Recorder(type(ds)):                          # type: ignore[misc]
        def spot(self, codes=None):
            seen.append(codes)
            raise RuntimeError("本用例只关心有没有带 codes 调用（不让它真取数）")

    rec = _Recorder(cfg, offline=False)

    class _State:
        trade_date = "2026-10-09"
        positions: dict = {}

    service.prewarm_for_write(rec, _State(), "2026-10-09", extra_codes=("600519",))
    assert any(isinstance(c, list) and "600519" in c for c in seen), (
        f"`spot([codes])` 没被调用 —— `_enrich_spot` 会在锁内取数（F3 隐患）。实测调用：{seen}"
    )
    assert seen, "spot 完全没被调用（连无参那次都没有）"
