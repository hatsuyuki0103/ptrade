"""W1 数据层新契约测试（PRD F1 / F2 / F2b；test-spec T-W1-3 … T-W1-10 的数据源侧）。

## 数据从哪来（每条都声明）
* **不联网**：`urlopen` 被替换成假响应（**只替换最底层的网络原语**，
  这样 `_http_get` 内的 `_net_guard` 仍然真实生效 —— 若替换 `_http_get` 本身，
  「锁内禁网」就会因为绕过守卫而**假绿**，这正是我们踩过的坑）；
* **磁盘**：一律用 `tmp_path` / `offline_cfg` 作为数据根，**绝不写仓库 `data/`**
  （仓库根含 `data/cache`，其数字与夹具环境完全不同 —— 曾因此写出过空断言）；
* **夹具**：`conftest` 的 `offline_cfg` 会把 `data/fixture` 复制到 tmp。

## 判据的「修前必红」
见 `.omx/probe/w1_mutation_verify.py`（逐条注掉实现 → 必须红）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

import pytest

from ptrade import datasource as dsmod
from ptrade import store
from ptrade.datasource import (
    INTRADAY_MIN_POINTS,
    REPORT_MIN_ROWS,
    DataSource,
    DataUnavailable,
    NetworkForbiddenInLock,
)

# --------------------------------------------------------------------------- #
# 工具：造 JSONP + 假 urlopen
# --------------------------------------------------------------------------- #
def jsonp(points_by_day: dict[str, int]) -> str:
    """按新浪格式造 JSONP：每天 ``count`` 个 1 分钟 bar（从 09:30 起）。"""
    rows = []
    for day, count in points_by_day.items():
        for i in range(count):
            hh, mm = divmod(9 * 60 + 30 + i, 60)
            rows.append({
                "day": f"{day} {hh:02d}:{mm:02d}:00",
                "open": "10.00", "high": "10.10", "low": "9.90", "close": "10.05",
                "volume": "1000", "amount": "10050.0",
            })
    return "var _=(" + json.dumps(rows, ensure_ascii=False) + ");"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._body = text.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def patch_urlopen(monkeypatch, body: str, counter: dict[str, int]) -> None:
    """替换**最底层**网络原语（保留 `_http_get` 与 `_net_guard`）。"""

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        counter["n"] = counter.get("n", 0) + 1
        if isinstance(body, Exception):
            raise body
        return _FakeResponse(body)

    dsmod._HTTP_LAST_CALL.clear()          # 避免 1s 限频让用例变慢
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def cache_dir(root: str) -> str:
    return os.path.join(root, "data", "cache")


def inject(root: str, key: str, payload: object) -> None:
    """往 tmp 数据根注入一条**新鲜**缓存（走真实的写入口，信封格式一致）。"""
    DataSource._write_entry(cache_dir(root), key, payload)


# --------------------------------------------------------------------------- #
# T-W1-3 分时守卫（离线 / 锁内）+ 正对照
# --------------------------------------------------------------------------- #
def test_T_W1_3_offline_intraday_raises_and_never_fetches(offline_cfg, monkeypatch):
    """离线：**绝不发请求**（计数器必须为 0，且抛 DataUnavailable）。

    注：离线时 ``_cached`` 先看夹具/缓存，找不到就抛「离线模式缺少数据 …」——
    这**先于** ``_http_get`` 的 ``_net_guard`` 生效。两条都满足硬性要求 1
    （绝不发请求 + 抛 DataUnavailable），故这里不断言具体文案；
    ``kind="http"`` 的那条文案由 ``test_T_W1_3_http_get_guard_wording`` 直接验。
    """
    counter: dict[str, int] = {}
    patch_urlopen(monkeypatch, jsonp({"2026-10-14": 238}), counter)
    ds = DataSource(offline_cfg, offline=True)
    with pytest.raises(DataUnavailable) as exc:
        ds.intraday("000001")
    assert "offline" in str(exc.value) or "离线" in str(exc.value)
    assert counter.get("n", 0) == 0, "离线模式竟然发出了 HTTP 请求"


def test_T_W1_3_http_get_guard_wording(offline_cfg):
    """``_http_get`` 自身必须先过 ``_net_guard(kind="http")``（唯一裸 HTTP 出口）。"""
    ds = DataSource(offline_cfg, offline=True)
    with pytest.raises(DataUnavailable) as exc:
        ds._http_get("https://example.invalid/x")
    assert "offline=True" in str(exc.value) and "http" in str(exc.value)


def test_T_W1_3_inlock_intraday_raises_and_never_fetches(offline_cfg, monkeypatch):
    """锁内：抛 NetworkForbiddenInLock（**不吞**），且**绝不**发请求。"""
    counter: dict[str, int] = {}
    patch_urlopen(monkeypatch, jsonp({"2026-10-14": 238}), counter)
    ds = DataSource(offline_cfg, offline=False)
    with store.state_lock(offline_cfg.root, timeout=5.0):
        with pytest.raises(NetworkForbiddenInLock):
            ds.intraday("000001")
    assert counter.get("n", 0) == 0, "锁内竟然发出了 HTTP 请求"


def test_T_W1_3_positive_control_counter_can_increase(offline_cfg, monkeypatch):
    """★ **正对照**：同样的假 urlopen，在**锁外**必须真的被调用。

    没有这条，「计数器 == 0」可能恒真（例如守卫把路全堵死了），
    那两条守卫判据就是**空守卫**。
    """
    counter: dict[str, int] = {}
    patch_urlopen(monkeypatch, jsonp({"2026-10-14": 238}), counter)
    ds = DataSource(offline_cfg, offline=False)
    got = ds.intraday("000001")
    assert counter.get("n", 0) == 1, "锁外也未发请求 → 计数器恒零，正对照失败"
    assert len(got["points"]) == 238


# --------------------------------------------------------------------------- #
# T-W1-4 完整日容差判据（238 → complete；142 → 不完整）
# --------------------------------------------------------------------------- #
def test_T_W1_4_238_points_is_complete(offline_cfg):
    ds = DataSource(offline_cfg, offline=False)
    ds._http_get = lambda url, **kw: jsonp({"2026-10-14": 238})  # type: ignore[assignment]
    got = ds.intraday("000001")
    assert len(got["points"]) == 238
    assert got["complete"] is True
    assert got["source"] == "sina"
    assert got["asof"] == "2026-10-14" and got["day"] == "2026-10-14"
    assert got["fetched_at"], "缺少 fetched_at 审计字段"


def test_T_W1_4_142_points_is_not_complete(offline_cfg):
    """半日/盘中样本（142 点）**不得**被判成完整日。"""
    ds = DataSource(offline_cfg, offline=False)
    ds._http_get = lambda url, **kw: jsonp({"2026-10-13": 142})  # type: ignore[assignment]
    got = ds.intraday("000001")
    assert len(got["points"]) == 142
    assert got["complete"] is False
    assert INTRADAY_MIN_POINTS == 228, "容差判据必须是 0.95×240=228（硬阈值 240 永远判不出）"


def test_T_W1_4b_default_day_is_the_latest_even_if_partial(offline_cfg):
    """★ **`intraday(day=None)` 必须取「最新那一天」，哪怕是部分日**。

    ## 为什么必须钉住（Lead 在联网分时验证时发现的**规格缺口**）

    `datasource.py` 的选日是 ``target = str(day)[:10] if day else max(by_day)`` ——
    **`day=None` 时取 payload 里最新的那一天，不要求它完整**。这正是用户要的行为：
    > 「交易日的时候看**我也是要看到最新的**」

    **但**：`INV-55` 只规定了 ``complete`` 的容差判据，**没有规定 `day=None` 取哪一天**；
    修前夹具也只覆盖 238/142 的 ``complete``，**没有覆盖选日规则**。

    ⚠️ **风险**：若将来有人把它「顺手修」成「取最近**完整**日」，
    用户在**盘中**会**静默看到昨天**（`complete=True` 的完整日）—— 页面看起来完全正常，
    **只是数据是旧的** —— 正好违背用户的核心需求且**不会有人发现**。

    ## 判据（**互为对照**，缺一即可能恒真）

    喂一天完整（10-14: 238）+ 一天部分（10-15: 5）：
    * `intraday()`（不指定日）→ 必须是 **10-15**（最新，**部分**）且 `complete is False`；
    * `intraday(day="2026-10-14")` → 必须是 **10-14** 且 `complete is True`（证明上一条不是「怎么都取部分」）。
    """
    ds = DataSource(offline_cfg, offline=False)
    ds._http_get = lambda url, **kw: jsonp({"2026-10-14": 238, "2026-10-15": 5})  # type: ignore[assignment]

    # ① 不指定日 → **最新一天**，即使它是部分日
    latest = ds.intraday("000001")
    assert latest["day"] == "2026-10-15", (
        f"`day=None` 必须取**最新**那一天（2026-10-15），实际 {latest['day']} —— "
        f"若取成 2026-10-14，用户在盘中会**静默看到昨天**（页面正常、数据是旧的）"
    )
    assert len(latest["points"]) == 5
    assert latest["complete"] is False, "5 点的部分日不得被判成完整"

    # ② 显式指定完整日 → 必须能取到，且判为完整（正对照，防①是「怎么都取部分」）
    explicit = ds.intraday("000001", day="2026-10-14")
    assert explicit["day"] == "2026-10-14"
    assert len(explicit["points"]) == 238
    assert explicit["complete"] is True

    # ③ 窗口外的一天 → 明确报错（不是静默回退到最新）
    with pytest.raises(DataUnavailable) as exc:
        ds.intraday("000001", day="2026-10-13")
    assert "2026-10-13" in str(exc.value), "报错要点明是哪一天"


def test_T_W1_4_vol_and_amount_are_incremental_and_avg_is_vwap(offline_cfg):
    """vol/amount 语义钉死为**增量**；avg = **累计 VWAP**。"""
    ds = DataSource(offline_cfg, offline=False)
    ds._http_get = lambda url, **kw: jsonp({"2026-10-14": 3})  # type: ignore[assignment]
    points = ds.intraday("000001")["points"]
    assert [p["vol"] for p in points] == [1000.0, 1000.0, 1000.0], "vol 必须是增量而非累计"
    assert [p["amount"] for p in points] == [10050.0] * 3
    assert points[0]["avg"] == pytest.approx(10.05)      # Σamount/Σvol
    assert [p["t"] for p in points] == ["09:30", "09:31", "09:32"]


def test_T_W1_4_broken_jsonp_raises_instead_of_empty_points(offline_cfg):
    """JSONP 解析失败 → **DataUnavailable**（空 points 与用户投诉的形态一模一样）。"""
    ds = DataSource(offline_cfg, offline=False)
    ds._http_get = lambda url, **kw: "var _=这不是 JSON;"  # type: ignore[assignment]
    with pytest.raises(DataUnavailable) as exc:
        ds.intraday("000001")
    assert "JSONP" in str(exc.value)


def test_T_W1_4_unknown_day_raises(offline_cfg):
    ds = DataSource(offline_cfg, offline=False)
    ds._http_get = lambda url, **kw: jsonp({"2026-10-14": 238})  # type: ignore[assignment]
    with pytest.raises(DataUnavailable) as exc:
        ds.intraday("000001", day="2020-01-01")
    assert "缺少 2020-01-01" in str(exc.value)


# --------------------------------------------------------------------------- #
# T-W1-5 min_rows 缓存写入守卫（INV-54）
# --------------------------------------------------------------------------- #
def test_T_W1_5_below_min_rows_is_not_written_and_warns(offline_cfg, tmp_path, caplog):
    """低于阈值 → **不落盘** + warning；**返回值仍交给调用方**。"""
    cache_root = tmp_path / "root"
    (cache_root / "data").mkdir(parents=True, exist_ok=True)
    cfg = offline_cfg.__class__(**{**offline_cfg.to_dict(), "root": str(cache_root)}) \
        if hasattr(offline_cfg, "to_dict") else None
    ds = DataSource(cfg or offline_cfg, offline=False)
    payload = {"rows": [{"code": "000001", "industry": "银行"} for _ in range(16)]}
    with caplog.at_level("WARNING"):
        got = ds._cached("industry_map", 10_000.0, lambda: payload, min_rows=100)
    assert got == payload, "低行数不应改变返回值（只在落盘上设闸）"
    assert not os.path.exists(os.path.join(ds.cache_dir, "industry_map.json")), "16 行竟被写进缓存"
    assert any("min_rows" in r.message or "min_rows" in str(r.args) for r in caplog.records), \
        "低于阈值必须留 warning（否则静默丢缓存无人知）"


def test_T_W1_5_above_min_rows_is_written(offline_cfg, tmp_path):
    ds = DataSource(offline_cfg, offline=False)
    ds.cache_dir = str(tmp_path / "cache")
    payload = {"rows": [{"code": "000001"} for _ in range(120)]}
    got = ds._cached("industry_map", 10_000.0, lambda: payload, min_rows=100)
    assert got == payload
    assert os.path.exists(os.path.join(ds.cache_dir, "industry_map.json")), "达标却没落盘"


def test_T_W1_5_count_uses_rows_key_when_dict(offline_cfg, tmp_path):
    """计数口径：``payload["rows"]``（industry_map 是 ``{"rows": [...]}`` 形态）。"""
    assert DataSource._row_count({"rows": [1, 2, 3]}) == 3
    assert DataSource._row_count([1, 2]) == 2
    assert DataSource._row_count({"a": 1}) == 1
    assert DataSource._row_count("nonsense") is None      # 数不清就不猜 → 不按 min_rows 拒绝


# --------------------------------------------------------------------------- #
# T-W1-6 报告期数据驱动（≥1000 行）
# --------------------------------------------------------------------------- #
def rows_n(n: int) -> dict:
    return {"rows": [{"code": f"{i:06d}", "industry": "白酒Ⅱ"} for i in range(n)]}


def test_T_W1_6_picks_recent_period_with_enough_rows(offline_cfg):
    """注入两期：最近那期只有 16 行 → 必须**继续往前找** 1001 行那期。"""
    ds = DataSource(offline_cfg, offline=False)
    candidates = ds.report_period_candidates()
    assert len(candidates) >= 2
    inject(offline_cfg.root, f"quarterly_{candidates[0]}", rows_n(16))
    inject(offline_cfg.root, f"quarterly_{candidates[1]}", rows_n(1001))
    assert ds.latest_report_period() == candidates[1]


def test_T_W1_6_all_below_threshold_raises_not_fail_open(offline_cfg):
    """**全部候选都不达标 → 抛 DataUnavailable**（不得退回空期 —— 那是 fail-open）。"""
    ds = DataSource(offline_cfg, offline=False)
    for period in ds.report_period_candidates():
        inject(offline_cfg.root, f"quarterly_{period}", rows_n(29))
    with pytest.raises(DataUnavailable) as exc:
        ds.latest_report_period()
    assert str(REPORT_MIN_ROWS) in str(exc.value)


def test_T_W1_6_threshold_constant_is_1000():
    assert REPORT_MIN_ROWS == 1000


# --------------------------------------------------------------------------- #
# T-W1-7 残缺 industry_map（16 行）→ industry_of 返回 ''（忠实复现现场）
# --------------------------------------------------------------------------- #
def test_T_W1_7_fresh_16_row_industry_map_yields_empty(offline_cfg):
    """★ 现场复现：**新鲜的** 16 行 industry_map 仍会让 ``industry_of`` 返回 ''。

    ⚠️ 只在 tmp 数据根上做 —— 在仓库根现场断言会改写 `data/cache` 并破坏隔离。
    """
    ds = DataSource(offline_cfg, offline=False)
    inject(offline_cfg.root, "industry_map",
           {"rows": [{"code": f"{i:06d}", "industry": "银行"} for i in range(16)]})
    assert ds.industry_of("600519") == "", "16 行缓存里没有 600519 → 必须是空串（L 因子退化）"


# --------------------------------------------------------------------------- #
# T-W1-9 陈旧快照拒合成（INV-57）
# --------------------------------------------------------------------------- #
def _frame():
    import pandas as pd

    return pd.DataFrame([
        {"date": "2026-10-09", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1,
         "volume": 1000.0, "amount": 10_100.0, "pct_chg": 1.0, "turnover": 0.5},
    ])


def test_T_W1_9_stale_spot_is_not_synthesized(offline_cfg, monkeypatch):
    """快照日期 != asof（陈旧）→ **不加 bar**：不得造出「用 10-09 收盘价做的 10-15 bar」。

    ★ **必须让 `quote` 返回「看着可用且未停牌」的价**（`suspended=False, last=10.5`）——
    现场形态就是这样：`quote()` 对无数据日**返回上一收盘价**，所以它既有价、又不算停牌。
    若这里让 `quote` 走真实实现，本用例会被**另一条**守卫（suspended / 日线推导）挡住，
    于是删掉陈旧检查后**仍然通过** = 空守卫。
    （这条设计缺陷是变异 M3 实测抓出来的：第一版就是绿的。）
    """
    ds = DataSource(offline_cfg, offline=False)
    monkeypatch.setattr(ds, "_spot_frame_raw", lambda: (_frame(), "2026-10-09"))
    monkeypatch.setattr(ds, "quote", lambda code, asof: {"suspended": False, "last": 10.5,
                                                        "prev_close": 10.0})
    frame, added = ds.with_today_bar(_frame(), "000001", "2026-10-15")
    assert added is False and len(frame) == 1


def test_T_W1_9_suspended_is_not_synthesized(offline_cfg, monkeypatch):
    """快照新鲜但**停牌/无成交** → 不合成。"""
    ds = DataSource(offline_cfg, offline=False)
    monkeypatch.setattr(ds, "_spot_frame_raw", lambda: (_frame(), "2026-10-15"))
    monkeypatch.setattr(ds, "quote", lambda code, asof: {"suspended": True, "last": 10.0,
                                                        "prev_close": 10.0})
    frame, added = ds.with_today_bar(_frame(), "000001", "2026-10-15")
    assert added is False and len(frame) == 1


def test_T_W1_9_fresh_and_trading_is_synthesized(offline_cfg, monkeypatch):
    """★ 正对照：快照新鲜 + 未停牌 → 必须合成（否则上面两条「不加」可能恒真）。"""
    ds = DataSource(offline_cfg, offline=False)
    spot = _frame()
    spot.loc[0, "code"] = "000001"
    monkeypatch.setattr(ds, "_spot_frame_raw", lambda: (spot, "2026-10-15"))
    monkeypatch.setattr(ds, "quote", lambda code, asof: {"suspended": False, "last": 10.5,
                                                        "prev_close": 10.0})
    frame, added = ds.with_today_bar(_frame(), "000001", "2026-10-15")
    assert added is True and len(frame) == 2
    assert str(frame["date"].iloc[-1]) == "2026-10-15"
    assert frame["close"].iloc[-1] == pytest.approx(10.5)


def test_T_W1_9_existing_real_bar_is_not_duplicated(offline_cfg, monkeypatch):
    """真实日线已含当天（收盘后）→ 不合成（否则图上会多一根假 bar）。"""
    ds = DataSource(offline_cfg, offline=False)
    frame = _frame()
    frame.loc[0, "date"] = "2026-10-15"
    frame, added = ds.with_today_bar(frame, "000001", "2026-10-15")
    assert added is False and len(frame) == 1


# --------------------------------------------------------------------------- #
# T-W1-10 单飞 × 锁内禁网（F2b）
# --------------------------------------------------------------------------- #
def test_T_W1_10_inlock_never_waits_for_inflight_fetch(offline_cfg, monkeypatch):
    """锁内遇到「同 key 正在抓取」→ **立即**抛 NetworkForbiddenInLock，**绝不等待**。

    ★ 用两个 ``threading.Event`` 断言那个拖慢的取数**当时尚未结束** ——
    不用墙钟阈值（`<100ms` 那类断言在 CI 上会 flaky）。
    """
    ds = DataSource(offline_cfg, offline=False)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow(url, **kw):  # noqa: ANN001
        started.set()
        release.wait(10.0)
        finished.set()
        return jsonp({"2026-10-14": 238})

    ds._http_get = slow  # type: ignore[assignment]
    worker = threading.Thread(target=lambda: ds.intraday("000001"))
    worker.start()
    assert started.wait(5.0), "慢取数没有开始（前置条件不成立）"
    try:
        with store.state_lock(offline_cfg.root, timeout=5.0):
            with pytest.raises(NetworkForbiddenInLock):
                ds.intraday("000001")
            assert not finished.is_set(), "锁内竟然等到了别人的取数结果（fail-slow 未消灭）"
            assert not release.is_set()
    finally:
        release.set()
        worker.join(10.0)


def test_T_W1_10_outside_lock_eight_threads_fetch_once(offline_cfg):
    """锁外 8 路并发同 key → 真取数**恰好 1 次**（其余等单飞结果）。"""
    ds = DataSource(offline_cfg, offline=False)
    calls = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def slow(url, **kw):  # noqa: ANN001
        calls["n"] += 1
        started.set()
        release.wait(5.0)
        return jsonp({"2026-10-14": 238})

    ds._http_get = slow  # type: ignore[assignment]
    got: list = []
    threads = [threading.Thread(target=lambda: got.append(ds.intraday("000001")))
               for _ in range(8)]
    for t in threads:
        t.start()
    assert started.wait(5.0)
    time.sleep(0.3)          # 让其余 7 路都堵在单飞锁上（不是在跑各自的 fetch）
    release.set()
    for t in threads:
        t.join(15.0)
    assert calls["n"] == 1, f"同 key 并发取数 {calls['n']} 次（应恰好 1 次）"
    assert len(got) == 8 and all(len(g["points"]) == 238 for g in got)


def test_T_W1_10_failure_is_not_cached(offline_cfg):
    """失败**不缓存负结果**：一次网络失败之后，第二次仍会真的再试。"""
    ds = DataSource(offline_cfg, offline=False)
    calls = {"n": 0}

    def flaky(url, **kw):  # noqa: ANN001
        calls["n"] += 1
        raise OSError("断网")

    ds._http_get = flaky  # type: ignore[assignment]
    for _ in range(2):
        with pytest.raises(DataUnavailable):
            ds.intraday("000001")
    assert calls["n"] == 2, "失败被缓存成了负结果（第二次没有重试）"
    assert not os.path.exists(os.path.join(ds.cache_dir, "intraday_000001.json"))


# --------------------------------------------------------------------------- #
# T-W1-8 看盘 asof 与下单分离（数据源侧；调用点接线属 W4）
# --------------------------------------------------------------------------- #
def test_T_W1_8_chart_asof_returns_today_only_when_spot_is_today(offline_cfg, monkeypatch):
    """`chart_asof`：**今天只在今天确有数据时**才返回（否则回落最近有数据的交易日）。

    调用点接线（`cmd_chart`/`api_chart` → `chart_asof`）属 W4；这里只钉数据源语义。
    变异 M10：让它无条件返回 `_today()` → 第二条断言必红。
    """
    ds = DataSource(offline_cfg, offline=False)
    monkeypatch.setattr(ds, "_today", lambda: "2026-10-15")
    monkeypatch.setattr(ds, "_spot_frame_raw", lambda: (_frame(), "2026-10-15"))
    assert ds.chart_asof("000001") == "2026-10-15", "快照就是今天 → 应返回今天"

    monkeypatch.setattr(ds, "_spot_frame_raw", lambda: (_frame(), "2026-10-09"))
    monkeypatch.setattr(ds, "latest_trade_asof",
                        lambda probe_codes=None, **kw: "2026-10-09")
    got = ds.chart_asof("000001")
    assert got == "2026-10-09", "快照是上一交易日 → 不得返回今天（会构造出无数据窗口）"
    assert got != "2026-10-15"


# --------------------------------------------------------------------------- #
# G008（CRITICAL rework）：钉住**生产调用点**真的传了 min_rows
# --------------------------------------------------------------------------- #
def _cached_call_sites(source: str, key_literal: str) -> list:
    """AST：找出 ``_cached("<key_literal>", ...)`` 的调用节点。"""
    import ast

    sites = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "_cached" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == key_literal:
            sites.append(node)
    return sites


def _min_rows_violations(source: str, key_literal: str, expected: int,
                         constant_names: dict | None = None) -> list[str]:
    """检查生产调用点是否传了 ``min_rows=<expected>``；返回违规说明列表。

    ``min_rows`` 的值可以是字面量，也可以是模块常量名（用 ``constant_names`` 解析）。
    ★ 这个检测器本身也被测（``test_T_W1_5_call_site_detector_is_not_vacuous``）——
    否则「没有违规」与「检测器失效」不可区分。
    """
    import ast

    names = constant_names or {}
    sites = _cached_call_sites(source, key_literal)
    if not sites:
        return [f"没找到 _cached({key_literal!r}, …) 的生产调用点（检测器可能失效）"]
    bad: list[str] = []
    for node in sites:
        value = None
        for kw in node.keywords:
            if kw.arg == "min_rows":
                value = kw.value
        if value is None:
            bad.append(f"第 {node.lineno} 行 _cached({key_literal!r}, …) **没传 min_rows**")
            continue
        if isinstance(value, ast.Constant):
            actual: object = value.value
        elif isinstance(value, ast.Name):
            actual = names.get(value.id, f"<未解析常量 {value.id}>")
        else:
            actual = f"<不支持的表达式 {type(value).__name__}>"
        if actual != expected:
            bad.append(f"第 {node.lineno} 行 min_rows={actual!r}，期望 {expected}")
    return bad


def test_T_W1_5_production_call_sites_pass_min_rows():
    """★ G008 的核心防回归：**生产调用点**必须真的传 ``min_rows``。

    为什么必须用 AST：原来的单测**自己**调 ``_cached(..., min_rows=100)``，
    只能证明「参数有效」，**完全不能证明生产调用点传了它** ——
    INV-54 就是这么变成空守卫的（有实现、有单测、有 INV，唯独没接线）。
    """
    import pathlib

    import ptrade.datasource as ds_mod

    source = pathlib.Path(ds_mod.__file__).read_text(encoding="utf-8")
    violations = _min_rows_violations(
        source, "industry_map", 100, {"INDUSTRY_MAP_MIN_ROWS": ds_mod.INDUSTRY_MAP_MIN_ROWS}
    )
    assert not violations, "INV-54 生产调用点未接线：\n  " + "\n  ".join(violations)
    assert ds_mod.INDUSTRY_MAP_MIN_ROWS == 100, "阈值被改弱"

    # search.py 的 stock_list 侧不是 _cached 路径，钉它的 STOCK_LIST_MIN_ROWS 判断存在
    import ast

    import ptrade.search as search_mod

    search_source = pathlib.Path(search_mod.__file__).read_text(encoding="utf-8")
    guarded = False
    for node in ast.walk(ast.parse(search_source)):
        if not isinstance(node, ast.Compare):
            continue
        # ⚠️ 常量在**哪一侧**取决于写法：`len(rows) < STOCK_LIST_MIN_ROWS` 里它是 comparator
        # （第一版只看 left → 假红，本条自测就是这么抓出来的）。
        operands = [node.left, *node.comparators]
        if any(isinstance(o, ast.Name) and o.id == "STOCK_LIST_MIN_ROWS" for o in operands):
            guarded = True
    assert guarded, "search.py 的 stock_list 写路径没有 STOCK_LIST_MIN_ROWS 判断（守卫被摘了）"
    assert search_mod.STOCK_LIST_MIN_ROWS == 1000


def test_T_W1_5_call_site_detector_is_not_vacuous():
    """非空洞：检测器喂「没传」「传错值」「key 不匹配」都必须报违规。"""
    missing = 'def f(self):\n    return self._cached("industry_map", 1, g)\n'
    assert _min_rows_violations(missing, "industry_map", 100), "检测器抓不到『没传』"

    wrong = 'def f(self):\n    return self._cached("industry_map", 1, g, min_rows=5)\n'
    assert _min_rows_violations(wrong, "industry_map", 100), "检测器抓不到『传错值』"

    good = 'def f(self):\n    return self._cached("industry_map", 1, g, min_rows=K)\n'
    assert not _min_rows_violations(good, "industry_map", 100, {"K": 100}), "检测器误报正确写法"

    other = 'def f(self):\n    return self._cached("other", 1, g)\n'
    assert _min_rows_violations(other, "industry_map", 100), "key 不匹配时也必须报『找不到调用点』"
    assert _cached_call_sites(other, "industry_map") == [], "按 key 过滤失效"


def test_T_W1_5_production_industry_map_does_not_cache_sixteen_rows(offline_cfg, monkeypatch,
                                                                   caplog):
    """★ 行为化：注入 16 行 fetch → 走**生产入口** ``_industry_map()`` → **缓存不得写出**。

    数据来源：注入的 16 行（复现 ``quarterly_report('20260930')`` 只有 16 行的现场），
    全部落在 tmp 数据根，**不碰仓库 ``data/cache``**。
    """
    ds = DataSource(offline_cfg, offline=False)
    monkeypatch.setattr(
        ds, "_fetch_industry_map",
        lambda: [{"code": f"{i:06d}", "industry": "银行"} for i in range(16)],
    )
    cache_file = os.path.join(ds.cache_dir, "industry_map.json")
    assert not os.path.exists(cache_file), "前置条件：缓存必须是空的"
    with caplog.at_level("WARNING"):
        mapping = ds._industry_map()
    assert len(mapping) == 16, "低行数仍要把值交给调用方（INV-54 只在落盘上设闸）"
    assert not os.path.exists(cache_file), "16 行竟被落盘 → 会驻留 TTL 并让 industry_of 全返回 ''"
    assert any("min_rows" in str(r.args) or "min_rows" in str(r.message)
               for r in caplog.records), "低于阈值必须留 warning"


def test_T_W1_5_production_industry_map_caches_full_table(offline_cfg, monkeypatch):
    """正对照：≥1000 行**必须**落盘（否则上面那条「不落盘」可能恒真）。"""
    ds = DataSource(offline_cfg, offline=False)
    monkeypatch.setattr(
        ds, "_fetch_industry_map",
        lambda: [{"code": f"{i:06d}", "industry": "银行"} for i in range(1200)],
    )
    mapping = ds._industry_map()
    assert len(mapping) == 1200
    assert os.path.exists(os.path.join(ds.cache_dir, "industry_map.json")), "达标却没落盘"


# --------------------------------------------------------------------------- #
# T-W1-8b / T-W1-3b 的 AST 守卫已在 tests/test_frozen_interfaces_v2.py
# --------------------------------------------------------------------------- #
def test_T_W1_3b_and_8b_ast_guards_live_in_frozen_file():
    """网络原语 deny-list 与 chart_asof 白名单的 AST 守卫由冻结文件承担（避免两处实现）。"""
    import pathlib

    frozen = (pathlib.Path(__file__).resolve().parent / "test_frozen_interfaces_v2.py")
    text = frozen.read_text(encoding="utf-8")
    assert "test_network_primitives_only_inside_http_get" in text
    assert "test_chart_asof_only_in_viewer_functions" in text

# --------------------------------------------------------------------------- #
# PN 补丁（task-33）：name_of + chart_asof 结果缓存（R10/R12）
# --------------------------------------------------------------------------- #
def _cold_web_cfg(tmp_path):
    """**真冷根**：只复制**定量夹具**，`data/cache` 为空，且**不得**预置
    `trade_dates`/`spot`/`asof_*`（Architect 点名的假绿陷阱）。

    ★ Critic C1 的修法（Lead 已核实）：必须用 **`offline=True`** 跑 ——
    夹具即数据源，30 步探测才会**真的发生**；`offline=False` 时
    `conftest.block_real_network` 会让日历取数抛错 → `latest_trade_asof` 走
    「异常 → `return self._today()`」→ **一次 `daily` 都没调** → `calls=0 <= 3` **假绿**。
    """
    import pathlib
    import shutil

    from ptrade.config import load_config

    repo_fixture = pathlib.Path(__file__).resolve().parent.parent / "data" / "fixture"
    fixture = tmp_path / "data" / "fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    for name in ("trade_dates.json", "daily_600519.json", "daily_600519_unadjust.json",
                 "margin_targets.json"):
        src = repo_fixture / name
        if src.is_file():
            shutil.copy2(src, fixture / name)
    (tmp_path / "data" / "cache").mkdir(parents=True, exist_ok=True)
    assert not (tmp_path / "data" / "cache" / "spot.json").exists()
    assert not (tmp_path / "data" / "cache" / "trade_dates.json").exists()
    return load_config(str(tmp_path))


def _write_spot_cache(cfg, *, asof: str) -> None:
    """往**缓存**写一条 spot（模拟快照刷新；不走联网）。"""
    DataSource._write_entry(
        os.path.join(str(cfg.root), "data", "cache"), "spot",
        {"asof": asof, "rows": [{"code": "600519", "name": "贵州茅台",
                                 "last": 1500.0, "prev_close": 1490.0,
                                 "volume": 100000.0, "amount": 150000000.0}]},
    )


def test_T_PN_0_cold_asof_resolution_calls_daily_at_most_3(tmp_path, monkeypatch):
    """**T-PN-0**：真冷根上 asof 解析必须是 O(1)（`daily` 取数 ≤3），而不是 30 次探测。

    数据来源：夹具（`data/fixture`，**offline=True** 即数据源）+ 空 `data/cache`。
    ★ 同时记录**第二次**调用的取数次数 —— 那是 R10 结果缓存真正生效的地方。
    """
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    calls: list[str] = []
    orig = DataSource.daily

    def counting(self, code, start=None, end=None, **kw):  # noqa: ANN001
        calls.append(code)
        return orig(self, code, start, end, **kw)

    monkeypatch.setattr(DataSource, "daily", counting)
    first = ds.chart_asof("600519")
    first_calls = len(calls)
    calls.clear()
    second = ds.chart_asof("600519")
    second_calls = len(calls)
    print(f"\n  [T-PN-0] 冷根第一次：daily 调用 {first_calls} 次 → asof={first!r}")
    print(f"  [T-PN-0] 第二次（缓存应生效）：daily 调用 {second_calls} 次 → asof={second!r}")
    assert second_calls <= 3, (
        f"第二次解析仍触发 {second_calls} 次 daily 取数（应 ≤3）—— asof 结果缓存（R1/R10）未生效"
    )


def test_T_PN_0b_cold_root_really_probes(tmp_path, monkeypatch):
    """**T-PN-0 的非空洞前提**：真冷根上第一次解析**必须真的取数**（否则判据是空的）。

    ★ 这正是 Critic C1：`offline=False` 下取数会抛错并被 `except Exception: return self._today()`
    吞掉 → `calls == 0` → 「O(1)」与「取数失败」不可区分 = **空守卫**。
    """
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    calls: list[str] = []
    orig = DataSource.daily

    def counting(self, code, start=None, end=None, **kw):  # noqa: ANN001
        calls.append(code)
        return orig(self, code, start, end, **kw)

    monkeypatch.setattr(DataSource, "daily", counting)
    ds.chart_asof("600519")
    assert calls, "冷根上竟然一次 daily 都没取 → 与「取数失败被吞」不可区分（T-PN-0 会是空守卫）"


def test_T_PN_11_today_branch_is_never_cached(tmp_path, monkeypatch):
    """**T-PN-11**（防回归，R10）：spot 从昨天改成今天 → `chart_asof` **立刻**返回今天。"""
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    _write_spot_cache(cfg, asof="2026-10-15")
    monkeypatch.setattr(ds, "_today", lambda: "2026-10-16")
    monkeypatch.setattr(ds, "latest_trade_asof", lambda probe_codes=None, **kw: "2026-10-15")
    assert ds.chart_asof("600519") == "2026-10-15", "spot 是昨天 → 应回退到最近有行情日"
    _write_spot_cache(cfg, asof="2026-10-16")
    assert ds.chart_asof("600519") == "2026-10-16", (
        "spot 已含今天 → **必须立刻返回今天**；返回昨天说明「今天」分支被缓存绕过了（R10 违规）"
    )


def test_T_PN_11b_asof_cache_is_phase_aware(tmp_path, monkeypatch):
    """**T-PN-11b**（R12）：pre_close 写入的缓存**不得**在 post_close 相位被复用。"""
    from datetime import datetime as _dt

    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    _write_spot_cache(cfg, asof="2026-10-15")
    monkeypatch.setattr(ds, "_today", lambda: "2026-10-16")
    monkeypatch.setattr(ds, "_now", lambda: _dt(2026, 10, 16, 9, 0), raising=False)
    monkeypatch.setattr(ds, "latest_trade_asof", lambda probe_codes=None, **kw: "2026-10-15")
    first = ds.chart_asof("600519")
    assert first == "2026-10-15"

    monkeypatch.setattr(ds, "_now", lambda: _dt(2026, 10, 16, 16, 0), raising=False)
    monkeypatch.setattr(ds, "latest_trade_asof", lambda probe_codes=None, **kw: "2026-10-16")
    got = ds.chart_asof("600519")
    assert got == "2026-10-16", (
        f"post_close 相位复用了 pre_close 的缓存值 {first!r} ⇒ 图上不含今天（INV-52 违规，R12）"
    )


def test_T_PN_4_name_of_uses_snapshot_not_asof(tmp_path):
    """**T-PN-4**：夹具 spot asof=2026-10-09；分时 day ≠ 它时名称**仍**是名称（不是 code）。"""
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    _write_spot_cache(cfg, asof="2026-10-09")
    assert ds.name_of("600519") == "贵州茅台"
    assert ds.name_of("600519") != "600519"


def test_T_PN_4b_cold_start_falls_back_to_code_without_network(tmp_path, monkeypatch):
    """**T-PN-4b**：真冷启动（无 spot 无名称表）→ 回退为 code，**且不得因此联网**。"""
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=False)
    sockets: list[int] = []
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: sockets.append(1), raising=False)
    assert ds.name_of("600519") == "600519", "冷启动必须回退为 code（有意取舍）"
    assert sockets == [], "name_of 冷启动竟然联网了（INV-61 违规）"


def test_T_PN_5_name_of_offline_never_connects(tmp_path, monkeypatch):
    """**T-PN-5**：`offline=True` 下 `name_of` 的 socket 计数必须为 0。"""
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    _write_spot_cache(cfg, asof="2026-10-09")
    sockets: list[int] = []
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: sockets.append(1), raising=False)
    assert ds.name_of("600519") == "贵州茅台"
    assert sockets == [], "offline=True 时 name_of 联网了"


def test_T_PN_6_name_of_never_raises(tmp_path, monkeypatch):
    """**T-PN-6**：任何异常路径下 `name_of` 都**不抛**，回退为 code。"""
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)

    def boom(*a, **k):
        raise RuntimeError("注入：读缓存炸了")

    monkeypatch.setattr(ds, "_spot_frame_raw", boom)
    assert ds.name_of("600519") == "600519"
    assert ds.name_of("") == ""


def test_T_PN_0c_cheap_branch_needs_no_daily(tmp_path, monkeypatch):
    """★ **T-PN-0c**（Lead 追加）：便宜分支（`spot_asof == today`）必须 **0 次 `daily` 取数**。

    为什么必须有这条：PRD **R11** 说「**让 spot 在首屏之前就绪** ⇒ `chart_asof` 走便宜分支
    ⇒ **0 次 daily** ⇒ T-PN-0 按构造绿」—— 但在这条判据之前，那个 promise **只活在注释里**
    （没有任何判据钉住「便宜分支真的不取数」）。

    ★ 制造手法：把 `_today` 注入成**夹具 spot 的 asof**（`2026-10-09`）——
    即把「spot 就是今天」这件事在夹具环境里**造出来**。
    `_today` 已可注入（T-PN-11b 在用）⇒ 本条**零新增机制**。

    变异：把便宜分支短路（`if False:`）→ 本条必须红（会掉进昂贵分支去探测）。
    """
    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    _write_spot_cache(cfg, asof="2026-10-09")                    # 夹具 spot 的 asof
    monkeypatch.setattr(ds, "_today", lambda: "2026-10-09")      # ★ 把「spot 就是今天」造出来
    calls: list[str] = []
    orig = DataSource.daily

    def counting(self, code, start=None, end=None, **kw):  # noqa: ANN001
        calls.append(code)
        return orig(self, code, start, end, **kw)

    monkeypatch.setattr(DataSource, "daily", counting)
    got = ds.chart_asof("600519")
    assert got == "2026-10-09", f"spot 就是今天 → 应直接返回今天，实际 {got!r}"
    assert len(calls) == 0, (
        f"便宜分支（spot_asof == today）不该取数，实际 {len(calls)} 次 daily —— "
        "R11 的「预热 ⇒ 按构造绿」前提不成立"
    )


def test_T_PN_11c_ttl_cannot_cross_a_day():
    """★ **F2（code-review）结构判据**：`ASOF_CACHE_TTL` 必须**严格小于** A 股最短的非交易间隔。

    为什么：**同相位内缓存不得跨日复用**。A 股最短的非交易间隔 =
    **15:00 → 次日 09:30 = 8h30m**；若 TTL ≥ 它，16:00 写的 `post_close` 条目
    到**次日开盘后**仍算新鲜 ⇒ 返回**昨天**的 asof ⇒ 图上不含今天（`INV-52` 违规）。

    ★ 为什么必须有这条：**正确性此前靠算术、没有任何判据钉住** ——
    code-review 实测把 TTL 改成 **12h**（内存改）后 `T-PN-11`/`T-PN-11b` **照样 2 passed**，
    而行为上跨日复用**可复现**（day2 返回 `10-15`，应为 `10-16`）。
    → 将来有人为「少取数」调大 TTL，行为会悄悄坏掉而判据全绿；本条让它**立刻红**。

    ★ 为什么上界取 **8h30m** 而不是评审建议里写的 `8h`：真正的语义边界就是 8h30m；
    写 `8h` 会把 `8h15m` 这种**合法**取值也判红（假红）。
    """
    from ptrade.datasource import ASOF_CACHE_TTL

    shortest_market_gap = 8 * 3600 + 30 * 60        # 15:00 → 次日 09:30
    assert ASOF_CACHE_TTL < shortest_market_gap, (
        f"ASOF_CACHE_TTL={ASOF_CACHE_TTL}s ≥ 最短非交易间隔 {shortest_market_gap}s（8h30m）"
        " ⇒ 同相位的 asof 缓存会**跨日**复用，次日开盘后返回昨天的 asof（INV-52 违规）"
    )
    assert ASOF_CACHE_TTL == 6 * 3600, (
        f"出厂值应为 6h（余量 2h30m），实际 {ASOF_CACHE_TTL}s —— 改值请连同本判据的理由一起改"
    )


def test_T_PN_11c_backdated_envelope_is_not_reused(tmp_path, monkeypatch):
    """★ **F2（code-review）行为判据**：把缓存信封的 `fetched_at` **回拨 10h10m** → **不得**复用。

    复现评审给的路径：`post_close`（16:00）写入 `10-15` → 次日开盘后（09:30+）若 TTL 过大，
    条目仍算新鲜 ⇒ 命中缓存返回 `10-15` ⇒ 图上不含今天；**不缓存**时会重新解析并得到**今天**。

    数据来源：夹具冷根 + 注入的 spot 缓存（昨天）+ 一条**手工回拨**的 asof 缓存信封。
    ★ 6h 出厂值下本条绿（10h10m > 6h ⇒ 过期 ⇒ 重解析）；**TTL 调到 12h 时本条必红**
      （10h10m < 12h ⇒ 被复用）。
    """
    import json
    import pathlib
    from datetime import datetime as _dt
    from datetime import timedelta, timezone

    cfg = _cold_web_cfg(tmp_path)
    ds = DataSource(cfg, offline=True)
    _write_spot_cache(cfg, asof="2026-10-15")                     # spot 陈旧 → 走昂贵分支
    monkeypatch.setattr(ds, "_today", lambda: "2026-10-16")
    monkeypatch.setattr(ds, "_now", lambda: _dt(2026, 10, 16, 9, 30))
    monkeypatch.setattr(ds, "latest_trade_asof",
                        lambda probe_codes=None, **kw: "2026-10-16")   # 今天已可得

    key = f"chart_asof_600519_{ds._asof_phase()}"
    DataSource._write_entry(ds.cache_dir, key, "2026-10-15")       # 「昨天」解析出来的值
    path = pathlib.Path(ds.cache_dir) / f"{key}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["fetched_at"] = (_dt.now(timezone.utc) - timedelta(hours=10, minutes=10)).isoformat()
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    got = ds.chart_asof("600519")
    assert got == "2026-10-16", (
        f"回拨 10h10m 的缓存被复用了：返回 {got!r}（应为今天的 2026-10-16）—— "
        "TTL 过大导致同相位缓存**跨日**复用（INV-52 违规，code-review F2）"
    )


def test_T_PN_13_viewer_asof_fetches_no_full_market_snapshot(tmp_path, monkeypatch):
    """★ **T-PN-13**（ultraqa FAIL 的判据）：查看路径的 asof 解析**不得**触发全市场 `spot()`。

    数据来源：夹具冷根（**无 spot 缓存**）+ `offline=True`（夹具即数据源，探测真的发生）。

    **修前实测：`chart_asof('600519')` 触发 6 次 `spot()`** —— 每次都是一次全市场快照抓取
    （akshare 分页 59 页 × ~1.4s ≈ 80s，ultraqa 实测首屏 80.3–83.4s）。修后必须为 **0**。

    ★ **正对照**：缺省 `allow_fetch=True`（下单/引擎路径）时计数仍 > 0 ——
    否则「== 0」可能只是「根本没走到那条路」= 空守卫。
    """
    cfg = _cold_web_cfg(tmp_path / "viewer")
    ds = DataSource(cfg, offline=True)
    counts = {"n": 0, "daily": 0}
    real_spot = DataSource.spot
    real_daily = DataSource.daily

    def counting(self, codes=None):  # noqa: ANN001
        counts["n"] += 1
        return real_spot(self, codes)

    def counting_daily(self, code, start=None, end=None, **kw):  # noqa: ANN001
        counts["daily"] += 1
        return real_daily(self, code, start, end, **kw)

    monkeypatch.setattr(DataSource, "spot", counting)
    monkeypatch.setattr(DataSource, "daily", counting_daily)
    asof = ds.chart_asof("600519")
    assert counts["n"] == 0, (
        f"查看路径触发了 {counts['n']} 次 spot()（全市场快照 59 页 ≈80s）—— "
        "`allow_fetch=False` 没有生效（T-PN-13，ultraqa FAIL）"
    )
    assert asof, "查看路径仍必须给出 asof"
    # ★ T-PN-13 扩充：逐日探测 `daily(code, d, d)` 在冷缓存下**每天一次 akshare 取数**
    #   （最多 6 次 ≈ **12s**，Lead 实测真实冷启动 12.06s 的来源）。
    #   改为「一次取覆盖窗口的帧、在帧里找最近有 bar 的那天」后应为 **1** 次。
    assert counts["daily"] <= 2, (
        f"查看路径的 asof 解析用了 {counts['daily']} 次按票 daily 取数（应 ≤2）—— "
        "冷启动仍会花掉 ~12s（T-PN-13 扩充）"
    )

    # ★ 正对照：缺省路径（下单/引擎）仍会走到这些取数 ⇒ 两个计数器都不是恒零
    cfg2 = _cold_web_cfg(tmp_path / "order")
    ds2 = DataSource(cfg2, offline=True)
    counts["n"] = counts["daily"] = 0
    ds2.latest_trade_asof(probe_codes=["600519"])          # 缺省 allow_fetch=True
    assert counts["n"] > 0, (
        "正对照失败：缺省（下单）路径竟然也不调 spot() —— 本判据的「== 0」是空的"
    )
    assert counts["daily"] > 0, "正对照失败：缺省路径竟然也不取日线 —— 本判据的「≤2」是空的"


def test_T_PN_13_allow_fetch_does_not_change_the_asof(tmp_path):
    """★ **等价性判据**：同一冷根下 `allow_fetch=True/False` 必须返回**同一个 asof**。

    为什么必须有：`allow_fetch=False` 换掉了「当天有行情」的实现（`quote()` → 按票日线），
    若换了语义，查看路径的 asof 就会漂。**实测确实发生过**：
    最初那版直接改用第二段的 `_has_window`（「窗口非空」）→ asof 从 `2026-10-09`
    漂到 `2026-10-16`（因为「窗口非空」≠「当天有行情」）⇒ 未采用，
    改成语义等价的 `_has_same_day_bar` 后两端一致。
    """
    cfg_a = _cold_web_cfg(tmp_path / "a")
    cfg_b = _cold_web_cfg(tmp_path / "b")
    ds_a = DataSource(cfg_a, offline=True)
    ds_b = DataSource(cfg_b, offline=True)
    asof_fetch = ds_a.latest_trade_asof(probe_codes=["600519"])                      # 缺省 True
    asof_nofetch = ds_b.latest_trade_asof(probe_codes=["600519"], allow_fetch=False)
    assert asof_fetch == asof_nofetch, (
        f"两侧 asof 不一致：allow_fetch=True → {asof_fetch!r}，False → {asof_nofetch!r}"
        " —— 说明 `allow_fetch=False` 换了语义（查看路径的 asof 会漂），不可接受"
    )


def test_T_PN_13b_viewer_quote_fetches_nothing_and_keeps_semantics(tmp_path, monkeypatch):
    """★ **T-PN-13b**：查看路径的 `quote(..., allow_fetch=False)` **不得取数**，且**语义不变**。

    **修前实测（Lead 读码 + 我实测）：`chart_asof + quote(False)` 的 `daily()` = 2**
    —— `quote()` 里那行 `window = self.daily(c, asof-60d, asof)` 是**无条件**取数，
    漏在 `allow_fetch` 判断**之外**（它当时只管「名称」那一小段）= 契约违反。
    修后 = **1**（只剩 asof 解析那一次）。

    ★ **语义一致性（本用例核心）**：在**数据可用**（夹具/缓存）时，
    `allow_fetch=True` 与 `False` 返回的关键字段必须**相同** ——
    这正是上一轮拦住 `_has_window` 漂移的同款检查；做不到就**不要硬上**（本轮实测：
    只读缓存那一版会退化成 `last=nan/suspended=True`，加上**夹具**回退后才一致 ✓）。
    """
    cfg = _cold_web_cfg(tmp_path / "viewer")
    ds = DataSource(cfg, offline=True)
    counts = {"spot": 0, "daily": 0}
    real_spot, real_daily = DataSource.spot, DataSource.daily

    def c_spot(self, codes=None):  # noqa: ANN001
        counts["spot"] += 1
        return real_spot(self, codes)

    def c_daily(self, code, start=None, end=None, **kw):  # noqa: ANN001
        counts["daily"] += 1
        return real_daily(self, code, start, end, **kw)

    monkeypatch.setattr(DataSource, "spot", c_spot)
    monkeypatch.setattr(DataSource, "daily", c_daily)

    asof = ds.chart_asof("600519")
    viewer = ds.quote("600519", asof, allow_fetch=False)
    assert counts["spot"] == 0
    assert counts["daily"] <= 1, (
        f"查看路径（asof 解析 + quote）用了 {counts['daily']} 次按票 daily 取数（应 ≤1）—— "
        "`quote(..., allow_fetch=False)` 仍在取数（T-PN-13b）"
    )

    # ★ 语义一致性：同款冷根、走**默认**路径（允许取数）
    cfg2 = _cold_web_cfg(tmp_path / "order")
    ds2 = DataSource(cfg2, offline=True)
    full = ds2.quote("600519", ds2.chart_asof("600519"), allow_fetch=True)

    def same(a: object, b: object) -> bool:
        if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
            return True          # 双 NaN 视为相等
        return a == b

    for key in ("last", "prev_close", "suspended", "limit_up", "limit_down", "name"):
        assert same(viewer[key], full[key]), (
            f"`allow_fetch=False` 的 quote 与默认路径不一致：{key} = {viewer[key]!r} vs {full[key]!r}"
            " —— 换语义了，不可接受（数据可用时必须一致）"
        )


def test_T_PN_13c_request_thread_fetches_daily_once(tmp_path, monkeypatch):
    """★ **T-PN-13c**（ultraqa 的**精确口径**）：**请求线程**的 `_fetch_daily` 真取数 ≤ 1。

    口径（Lead 采纳 ultraqa 的纠正）：**数 `_fetch_daily` 真取数并区分线程**，
    **不数 `daily()` 调用**（后台分析线程的取数不阻塞用户 ⇒ 不计入）。

    **修前实测：请求线程 2 次** —— ultraqa 线程级 trace：50 天窗口 `2026-08-27~2026-10-16`
    ⊂ 2 年窗口 `2024-10-16~2026-10-16`，同源数据取了两遍（缓存 key 含窗口、互不命中）。
    **修后 1 次**：asof 解析直接用与图相同的窗口 —— ★ v6 起该窗口是**固定锚点** :data:`VIEWER_START_DATE`
    （A 股开市日），图随后**命中同一 key**。

    数据来源：夹具当「假 akshare」（`_fetch_daily` 按请求窗口切片夹具行）+ 冷根（cache 为空）。
    → 测的是**真取数路径与缓存 key 是否共享**；离线读夹具**不经过** `_fetch_daily`，所以必须在
      `offline=False` 下用假 `_fetch_daily` 才能复现这条 trace。
    """
    import json
    import pathlib
    import threading

    # ★ v6：窗口真源从「N 天」换成**固定锚点**（A 股开市日）—— 单一真源的**语义没变**，只是真源换了
    from ptrade.datasource import VIEWER_START_DATE

    repo = pathlib.Path(__file__).resolve().parent.parent
    payload = json.loads((repo / "data" / "fixture" / "daily_600519.json").read_text("utf-8"))
    rows = payload.get("payload") if isinstance(payload, dict) else payload
    assert isinstance(rows, list) and rows, "夹具日线不可用 → 本用例不成立"

    cfg = _cold_web_cfg(tmp_path / "viewer")
    ds = DataSource(cfg, offline=False)
    counts = {"req": 0, "bg": 0, "spot": 0}
    real_spot = DataSource._fetch_spot

    def fake_fetch_daily(self, code, adj, start, end):  # noqa: ANN001
        if threading.current_thread() is threading.main_thread():
            counts["req"] += 1
        else:  # 后台分析线程：不阻塞用户，不计入判据
            counts["bg"] += 1
        return [r for r in rows if start <= str(r.get("date"))[:10] <= end]

    def fake_fetch_trade_dates(self):  # noqa: ANN001
        cal = json.loads((repo / "data" / "fixture" / "trade_dates.json").read_text("utf-8"))
        return cal.get("payload") if isinstance(cal, dict) else cal

    def counting_spot(self):  # noqa: ANN001
        counts["spot"] += 1
        return real_spot(self)

    monkeypatch.setattr(DataSource, "_fetch_daily", fake_fetch_daily)
    monkeypatch.setattr(DataSource, "_fetch_trade_dates", fake_fetch_trade_dates)
    monkeypatch.setattr(DataSource, "_fetch_spot", counting_spot)

    # ★ 复现 ultraqa trace 的**真实冷启动形态**：`base == asof`（快照新鲜 → asof 就是今天）。
    #   ★ 为什么必须这样对齐：缓存 key 同时含**起止日**，所以「共享」要求
    #     **窗口 + 结束日**都一致；真实冷启动里 asof == 今天 == base ✓（ultraqa trace：
    #     两次取数窗口都以 `2026-10-16` 结束）。
    #   ★ 快照陈旧时（asof < base）会退化成 2 次取数 —— 这是**有意的 fail-safe**（只是变慢，
    #     不会出错数据）；本判据钉的是「**能共享的那个形态必须共享**」。
    monkeypatch.setattr(ds, "_today", lambda: "2026-10-09")
    # ★★ 修正（v3 收口时发现）：本用例要复现的是「**快照新鲜** ⇒ 便宜分支
    #   `spot_asof == today ⇒ return today` 生效」的形态（见上方 docstring 引用的
    #   ultraqa trace：两次取数都以同一天结束）。但 `_cold_web_cfg()` 按设计**不预置 spot**
    #   （那是给 `T-PN-0` 的「真冷根」用的，前提与本用例**相反**）⇒ 便宜分支永远不触发，
    #   `chart_asof` 会退到 `_latest_bar_day()` 的探测路径，返回值与「快照新鲜」形态无关。
    #   ⇒ **把前提显式造出来**（不动任何断言）：写一条 asof = 今天 的 spot 缓存。
    _write_spot_cache(cfg, asof="2026-10-09")
    asof = ds.chart_asof("600519")
    assert asof == "2026-10-09", f"前置条件：asof 应等于 base（实测 {asof!r}）"
    # ★★ 断言按**本用例标题的口径**修正为 `<= 1`（标题写的就是「请求线程真取数 ≤ 1」）：
    #   快照**新鲜**时 `chart_asof` 走**便宜分支**（`spot_asof == today ⇒ return today`）⇒ **0 次取数**
    #   （比 1 次更理想）；只有当便宜分支不成立、需要按票探测时才应恰好 1 次。
    #   ★ 这**不是放宽**：`<= 1` 仍然能抓修前的 **2 次**（那正是本判据存在的原因），
    #     而下面的「图的主查询不得再取数」仍是**严格**的 `== 1`（0 → 1 是图自己的那一次）。
    assert counts["req"] <= 1, (
        f"asof 解析的**请求线程**真取数 = {counts['req']}（应 ≤1；修前为 2）"
    )
    # ★ 模拟图的主查询：**同一窗口** ⇒ 必须命中缓存、不得再取数
    ds.daily("600519", VIEWER_START_DATE, asof)
    assert counts["req"] == 1, (
        f"图的主查询又取了一次（请求线程真取数 {counts['req']}）—— "
        "asof 解析与图的主窗口没共享缓存 key（两边都要用 VIEWER_START_DATE）"
    )
    assert counts["spot"] == 0, f"请求线程触发了 {counts['spot']} 次 _fetch_spot（全市场快照）"
    assert counts["bg"] == 0, "本用例不应有后台线程取数"

    # ★ 正对照：缺省 `allow_fetch=True`（下单/引擎）路径仍会真取数 ⇒ 计数器不是恒零
    cfg2 = _cold_web_cfg(tmp_path / "order")
    ds2 = DataSource(cfg2, offline=False)
    counts["req"] = 0
    ds2.latest_trade_asof(probe_codes=["600519"])          # 缺省 allow_fetch=True
    assert counts["req"] > 0, (
        "正对照失败：缺省路径竟然也不真取数 —— 本判据的「≤1」是空的"
    )


def test_T_PN_13d_stale_snapshot_shares_viewer_window(tmp_path, monkeypatch):
    """★ **T-PN-13d**（补 code-review **M5** 的缺口）：**陈旧快照**形态下，
    asof 探测与图的主查询必须**共享同一个窗口** —— ★ v6 起是**固定锚点 `VIEWER_START_DATE`**（只真取数 **1** 次）。

    ### 为什么必须单独一条（而不是靠 `T_PN_13c`）

    `T_PN_13c` 在本轮被改成「**快照新鲜**」形态后，`chart_asof` 走**便宜分支**
    （`spot_asof == today ⇒ return today`）⇒ **0 次取数**，随后图的主查询是冷取数 1 次 ⇒
    末尾的 `== 1` **恒成立** ⇒ **它注释里声称的「共享窗口」机制在该形态下根本没被触发**。

    本条用**陈旧快照**（spot `asof=2026-10-08` 而 `_today=2026-10-09`）强制走
    `_latest_bar_day` **按票探测**路径 —— 那才是会真取数的那个分支。

    ### 断言

    ① 探测确实发生（请求线程真取数 == 1）；
    ② ★ **图的主查询（同一窗口）命中缓存** ⇒ 仍 == 1（**这就是被 13c 丢掉的那个机制**）；
    ③ 请求线程不得触发 `_fetch_spot`；④ 无后台线程取数；
    ⑤ ★ **正对照**：窗口**改小 1 天** ⇒ 必须**再取一次**（2）⇒ 证明「命中」不是「怎么查都不取数」。
    """
    import json
    import pathlib
    import threading

    # ★ v6：窗口真源从「N 天」换成**固定锚点**（A 股开市日）—— 单一真源的**语义没变**，只是真源换了
    from ptrade.datasource import VIEWER_START_DATE

    repo = pathlib.Path(__file__).resolve().parent.parent
    payload = json.loads((repo / "data" / "fixture" / "daily_600519.json").read_text("utf-8"))
    rows = payload.get("payload") if isinstance(payload, dict) else payload
    assert isinstance(rows, list) and rows, "夹具日线不可用 → 本用例不成立"

    cfg = _cold_web_cfg(tmp_path / "stale")
    ds = DataSource(cfg, offline=False)
    counts = {"req": 0, "bg": 0, "spot": 0}
    real_spot = DataSource._fetch_spot

    def fake_fetch_daily(self, code, adj, start, end):  # noqa: ANN001
        if threading.current_thread() is threading.main_thread():
            counts["req"] += 1
        else:
            counts["bg"] += 1
        return [r for r in rows if start <= str(r.get("date"))[:10] <= end]

    def fake_fetch_trade_dates(self):  # noqa: ANN001
        cal = json.loads((repo / "data" / "fixture" / "trade_dates.json").read_text("utf-8"))
        return cal.get("payload") if isinstance(cal, dict) else cal

    def counting_spot(self):  # noqa: ANN001
        counts["spot"] += 1
        return real_spot(self)

    monkeypatch.setattr(DataSource, "_fetch_daily", fake_fetch_daily)
    monkeypatch.setattr(DataSource, "_fetch_trade_dates", fake_fetch_trade_dates)
    monkeypatch.setattr(DataSource, "_fetch_spot", counting_spot)

    # ★ 陈旧快照：spot 停在昨天 ⇒ 便宜分支不成立 ⇒ 必须走按票探测
    monkeypatch.setattr(ds, "_today", lambda: "2026-10-09")
    _write_spot_cache(cfg, asof="2026-10-08")

    asof = ds.chart_asof("600519")
    assert asof, "asof 解析不得为空"
    # ① 探测确实发生（陈旧快照下不得凭空返回今天）
    assert counts["req"] == 1, (
        f"陈旧快照下 asof 探测的请求线程真取数 = {counts['req']}（应为 1；"
        f"若为 0 说明走了便宜分支 = 本例前提不成立，asof={asof!r}）"
    )

    # ② ★ 图的主查询：**同一窗口** ⇒ 必须命中缓存
    ds.daily("600519", VIEWER_START_DATE, asof)
    assert counts["req"] == 1, (
        f"图的主查询又取了一次（请求线程真取数 {counts['req']}）—— "
        "asof 探测与图的主窗口**没有共享** VIEWER_START_DATE 缓存 key"
    )

    # ③ 请求线程不得触发全市场快照
    assert counts["spot"] == 0, f"请求线程触发了 {counts['spot']} 次 _fetch_spot"
    # ④ 无后台线程取数
    assert counts["bg"] == 0, "本用例不应有后台线程取数"

    # ⑤ ★ 正对照：窗口**改小 1 天** ⇒ 缓存 key 不同 ⇒ 必须再取一次
    ds.daily("600519", "1990-12-20", asof)   # ★ 与锚点差 1 天 ⇒ key 不同（fail-safe：退回 2 次取数，不错数据）
    assert counts["req"] == 2, (
        f"正对照失败：窗口改小 1 天后仍取数 {counts['req']} 次（应为 2）—— "
        "说明上面的「命中」不是因为「怎么查都不取数」，判据是空的"
    )
