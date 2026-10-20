# -*- coding: utf-8 -*-
"""tests/test_perf_analysis.py —— PN（首屏性能 + 分析缓存）：**chart 侧**判据。

数据来源（强制声明）：全部走 `offline_cfg`（tmp root，复制 `data/fixture`）——
**不依赖仓库 `data/cache`**（那里有在线活动灌进去的新鲜缓存，会让判据随环境漂移）。

分工（PRD R8）：本文件只钉 **chart 侧**：
* `T-PN-7` 缓存往返 == `svc.jsonable(fresh)`（含「换 asof 不串味」）
* `T-PN-7b` ★ 缓存路径与现算路径渲染出的 **HTML 逐字节相等**（比 payload 深度相等更强）
* `T-PN-1`（chart 侧 · **结构性门**）占位路径**不得**触发任何计算；6 面板齐 + `pending` marker
* `T-PN-2`（chart 侧）缓存命中后第二次渲染 = `ready` + 七因子齐
* 失败短 TTL（120s）· pending 不得落盘 · marker 三态契约 · F3 名称与 asof 解耦

`/api/chart` 的 HTTP 端到端版本（T-PN-1/2/3/9/10 的 webapp 部分）由 W-P2 负责。
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ptrade import chart as C
from ptrade import service as svc
from ptrade.datasource import DataSource, DataUnavailable

BIG = "600519"
SPOT_DAY = "2026-10-09"          # 夹具 spot 的 asof（也是夹具日线的末日）
OTHER_DAY = "2026-10-13"         # ≠ SPOT_DAY：F3 要证明名称与 asof 解耦
LATE_DAY = "2026-10-15"          # 夹具日历的末日（晚于日线末日 → 帧里没有当天 bar）


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _analysis(cfg, ds, code=BIG, asof=None) -> dict:
    return C._score_analysis(cfg, ds, code, asof or ds.latest_trade_asof([code]))


def _cache_path(cfg, code: str, asof: str) -> Path:
    return Path(C._analysis_cache_dir(cfg)) / f"{C.analysis_cache_key(code, asof)}.json"


def _backdate(cache_path: Path, seconds: float) -> None:
    """把缓存信封的 `fetched_at` 往前挪（TTL 判据必须真的过期，不能靠 sleep）。"""
    from datetime import datetime, timedelta, timezone

    entry = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "fetched_at" in entry, "信封必须带 fetched_at（否则 _expired 直接判过期，判据会假绿）"
    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    entry["fetched_at"] = stamp.isoformat()
    cache_path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")


def _body_tag(html: str) -> str:
    import re

    found = re.search(r"<body[^>]*>", html)
    assert found, "页面必须有 <body>"
    return found.group(0)


# --------------------------------------------------------------------------- #
# T-PN-7 缓存往返深度相等 + 换 asof 不串味
# --------------------------------------------------------------------------- #
def test_tpn7_cached_analysis_roundtrip_equals_fresh(offline_cfg, offline_ds):
    """缓存往返 == `svc.jsonable(fresh)`；**换 asof 不得串味**。

    ★ R5：必须先把 fresh 过一遍 `svc.jsonable` 再比 —— 原始写法会**假红**
    （`tuple ≠ list`、以及非 JSON 原生对象）。
    """
    asof = offline_ds.latest_trade_asof([BIG])
    fresh = _analysis(offline_cfg, offline_ds, asof=asof)
    assert fresh.get("ok") is True, f"前置：分析必须成功，否则本用例无意义：{fresh}"
    jsonable = svc.jsonable(fresh)

    # 非空洞前置：写之前缓存必须不存在
    assert C.read_cached_analysis(offline_cfg, BIG, asof) is None, "前置：缓存应为冷"

    C.write_cached_analysis(offline_cfg, BIG, asof, jsonable)
    assert _cache_path(offline_cfg, BIG, asof).is_file(), "缓存文件必须真的落盘"
    got = C.read_cached_analysis(offline_cfg, BIG, asof)
    assert got is not None, "刚写的缓存必须能读回"
    assert got == jsonable, "缓存往返后 payload 变了（丢字段 / 改值 / tuple→list 未归一）"
    assert sorted(got) == sorted(jsonable), f"字段集合不得变化：{sorted(got)}"

    # ★ 换 asof 不得串味（key 里必须含 asof）
    assert C.read_cached_analysis(offline_cfg, BIG, "1999-01-01") is None
    assert C.analysis_cache_key(BIG, asof) != C.analysis_cache_key(BIG, "1999-01-01")


def test_tpn7b_cached_path_html_is_byte_identical(offline_cfg, offline_ds):
    """★ **T-PN-7b**：缓存路径与现算路径渲染出的 HTML **逐字节相等**（INV-62）。

    比 payload 深度相等更强：它同时钉住「渲染消费的每个字段都完整往返」。
    """
    asof = offline_ds.latest_trade_asof([BIG])
    fresh = _analysis(offline_cfg, offline_ds, asof=asof)
    assert fresh.get("ok") is True, f"前置：分析必须成功：{fresh}"

    html_fresh, payload_fresh = C.render_chart(offline_cfg, offline_ds, BIG, asof, analysis=fresh)

    C.write_cached_analysis(offline_cfg, BIG, asof, svc.jsonable(fresh))
    cached = C.read_cached_analysis(offline_cfg, BIG, asof)
    assert cached is not None, "前置：缓存必须可读（否则比较的是 None）"
    html_cached, payload_cached = C.render_chart(offline_cfg, offline_ds, BIG, asof, analysis=cached)

    # 同一份行情载荷（否则差异可能来自行情而不是分析缓存）
    assert payload_cached["periods"]["daily"]["dates"] == payload_fresh["periods"]["daily"]["dates"]
    assert html_cached == html_fresh, (
        "缓存路径与现算路径的 HTML 不一致（INV-62）—— "
        f"长度 {len(html_cached)} vs {len(html_fresh)}"
    )
    print(f"  T-PN-7b: HTML 逐字节相等 ✓  {len(html_cached):,} 字节（sha 一致）")


# --------------------------------------------------------------------------- #
# T-PN-1（chart 侧 · 结构性门）首屏不被分析区阻塞
# --------------------------------------------------------------------------- #
def test_tpn1_placeholder_path_never_computes(offline_cfg, offline_ds, monkeypatch):
    """★ **结构性门**（真门）：传占位时 `render_chart` **不得**调用 `_score_analysis`。

    为什么不用墙钟：墙钟受机器负载影响（Critic R18：CI 上 15s 易假红）。这里钉的是**机制** ——
    「未命中缓存 → 立即返回 pending 图」这件事不允许依赖「这次跑得快」。
    墙钟另设 60s **护栏**（不是门槛）；体感门槛 ≤5s 仅作打印。
    """
    asof = offline_ds.latest_trade_asof([BIG])
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("占位路径不得触发分析计算（T-PN-1 结构性门）")

    monkeypatch.setattr(C, "_score_analysis", _boom)

    started = time.time()
    html, payload = C.render_chart(offline_cfg, offline_ds, BIG, asof, analysis=C.ANALYSIS_PENDING)
    elapsed = time.time() - started

    assert calls["n"] == 0, "★占位路径调用了 _score_analysis —— 首屏又被分析区阻塞了"
    assert elapsed <= 60.0, f"首屏墙钟 {elapsed:.1f}s > 60s 护栏（结构性门已过，这是异常信号）"
    print(f"  T-PN-1: 占位路径 0 次计算 · 墙钟 {elapsed:.2f}s（体感门槛 ≤5s）")

    # 6 个面板齐全 + 行情图真的在
    for title in ("① K线", "② BOLL", "③ 成交量", "④ 成交额", "⑤ MACD", "⑥ SAR"):
        assert title in html, f"缺面板 {title}"
    assert payload["periods"]["daily"]["dates"], "图必须有数据"
    # marker 三态契约：占位必须是 pending（前端据此**继续**轮询）
    assert _body_tag(html) == '<body data-analysis-state="pending">'
    assert "计算中" in html, "占位要给人看得到「计算中」"


def test_tpn2_second_render_is_ready_with_seven_factors(offline_cfg, offline_ds):
    """T-PN-2（chart 侧）：缓存命中后第二次渲染 = `ready` + 七因子齐、且**不再计算**。"""
    asof = offline_ds.latest_trade_asof([BIG])
    fresh = _analysis(offline_cfg, offline_ds, asof=asof)
    assert fresh.get("ok") is True
    C.write_cached_analysis(offline_cfg, BIG, asof, svc.jsonable(fresh))

    cached = C.read_cached_analysis(offline_cfg, BIG, asof)
    assert cached is not None
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof, analysis=cached)

    assert _body_tag(html) == '<body data-analysis-state="ready">'
    assert "计算中" not in html, "ready 页面不得再显示「计算中」"
    for key in ("C", "A", "N", "S", "L", "I", "M"):
        assert f"<b>{key}</b>" in html, f"七因子缺 {key}"


# --------------------------------------------------------------------------- #
# 失败短 TTL / pending 不得落盘 / marker 三态
# --------------------------------------------------------------------------- #
def test_failed_analysis_uses_short_ttl_but_success_survives(offline_cfg, offline_ds):
    """★ 失败也写缓存（INV-60：不得让分析静默消失），但**短 TTL 120s**；成功仍是 6h。

    判据做成「同一时刻、两种载荷」的对照：把两个缓存信封都倒退 3 分钟 →
    失败必须**已过期**（可重试），成功必须**仍可读**。
    """
    asof = offline_ds.latest_trade_asof([BIG])
    failed = C.ANALYSIS_FAILED("模拟：数据源不可用")
    ok_payload = svc.jsonable(_analysis(offline_cfg, offline_ds, asof=asof))

    C.write_cached_analysis(offline_cfg, BIG, asof, failed)
    C.write_cached_analysis(offline_cfg, "000001", asof, ok_payload)

    # 刚写完：两者都能读到（失败**不是**「不缓存」）
    assert C.read_cached_analysis(offline_cfg, BIG, asof) == failed
    assert C.read_cached_analysis(offline_cfg, "000001", asof) is not None

    _backdate(_cache_path(offline_cfg, BIG, asof), 3 * 60)          # 失败：180s > 120s
    _backdate(_cache_path(offline_cfg, "000001", asof), 3 * 60)     # 成功：180s < 6h

    assert C.read_cached_analysis(offline_cfg, BIG, asof) is None, \
        "失败载荷超过 120s 必须过期（否则「数据缺失」会长期压住重试）"
    assert C.read_cached_analysis(offline_cfg, "000001", asof) is not None, \
        "成功载荷 180s 就过期说明 TTL 被写短了（应为 6h）"
    assert C.ANALYSIS_FAIL_TTL == 120 and C.ANALYSIS_TTL == 6 * 3600


def test_failed_payload_renders_as_missing_with_reason(offline_cfg, offline_ds):
    """失败载荷 → `failed` marker + 「数据缺失 —— <原因>」（不得静默消失、不得空窗）。"""
    asof = offline_ds.latest_trade_asof([BIG])
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof,
                             analysis=C.ANALYSIS_FAILED("模拟：快照不可用"))
    assert _body_tag(html) == '<body data-analysis-state="failed">'
    assert "数据缺失" in html and "模拟：快照不可用" in html


def test_pending_must_not_be_cached(offline_cfg, offline_ds):
    """★ `pending` 占位**不得落盘**：否则前端把「还在算」误读成「数据缺失」而停止轮询。"""
    asof = offline_ds.latest_trade_asof([BIG])
    with pytest.raises(ValueError) as excinfo:
        C.write_cached_analysis(offline_cfg, BIG, asof, C.ANALYSIS_PENDING)
    assert "pending" in str(excinfo.value)
    assert C.read_cached_analysis(offline_cfg, BIG, asof) is None, "被拒的写入不得留下缓存文件"
    with pytest.raises(ValueError):
        C.write_cached_analysis(offline_cfg, BIG, asof, ["not", "a", "dict"])


def test_analysis_state_contract_and_body_marker(offline_cfg, offline_ds):
    """marker 契约：三态封闭 + 必须挂在 `<body>` 上（前端读 DOM 属性，禁止文本扫描）。"""
    assert C.analysis_state(C.ANALYSIS_PENDING) == "pending"
    assert C.analysis_state(C.ANALYSIS_FAILED("x")) == "failed"
    assert C.analysis_state({"ok": True, "factors": {}}) == "ready"
    assert C.analysis_state(None) == "failed"          # 兜底：不得崩、不得谎报 ready

    asof = offline_ds.latest_trade_asof([BIG])
    for analysis, expected in ((C.ANALYSIS_PENDING, "pending"),
                               (C.ANALYSIS_FAILED("x"), "failed")):
        html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof, analysis=analysis)
        assert _body_tag(html) == f'<body data-analysis-state="{expected}">'
        assert html.count("data-analysis-state=") == 1, "marker 只能出现一次（取 DOM 属性的语义）"


def test_rendering_does_not_mutate_module_placeholders(offline_cfg, offline_ds):
    """模块级占位常量不得被渲染**就地改写**（否则第一个请求会污染后续所有请求）。"""
    before = copy.deepcopy(C.ANALYSIS_PENDING)
    asof = offline_ds.latest_trade_asof([BIG])
    C.render_chart(offline_cfg, offline_ds, BIG, asof, analysis=C.ANALYSIS_PENDING)
    assert C.ANALYSIS_PENDING == before, "ANALYSIS_PENDING 被就地改写了"


# --------------------------------------------------------------------------- #
# F3（chart 侧）：分时名称与 asof 解耦
# --------------------------------------------------------------------------- #
def test_f3_intraday_name_survives_day_mismatch(offline_cfg, offline_ds, monkeypatch):
    """★ F3 / INV-61：分时的 `day` ≠ 快照日、且该日快照**不可得**时，名称**不得**退化为代码。

    ★★ **自备数据，不依赖外部状态**（Lead 排查顺序敏感失败后的纪律）：
    本用例**自己注入** spot 缓存（写死 asof 与名称哨兵），因此
    「分时日 ≠ 快照日」这个前提与「名称是哪一个」都由用例**显式构造**，
    既不依赖「夹具 spot 恰好是 2026-10-09」，也不受同会话内别的用例写缓存的影响。

    为什么**注入**失败条件而不是靠环境：实测（离线夹具）`ds.quote(code, 任意 day)` **总能**拿到名称，
    所以「换个 day 就退化」在这套夹具上**复现不出来**（那样会写成一条恒绿判据）。
    线上真正的退化条件是「该日快照分支不命中 → quote 取不到名称」—— 用 `DataUnavailable` 精确注入，
    **不联网**、确定性。

    ★ 同时是**结构性**判据：名称路径**根本不许**再碰 `ds.quote`（asof 相关）。
    """
    sentinel = "哨兵名称X"
    DataSource._write_entry(
        str(offline_ds.cache_dir), "spot",
        {"asof": SPOT_DAY, "rows": [{"code": BIG, "name": sentinel, "last": 1263.0,
                                     "prev_close": 1263.0, "volume": 1_000_000,
                                     "amount": 1.0e9}]},
    )
    assert offline_ds.name_of(BIG) == sentinel, "前置：注入的 spot 缓存必须能给出哨兵名称"

    calls = {"n": 0}

    def _no_quote(*args, **kwargs):
        calls["n"] += 1
        raise DataUnavailable("模拟：该日无快照（分时日 ≠ 快照日）")

    monkeypatch.setattr(offline_ds, "quote", _no_quote)
    data = {
        "day": OTHER_DAY, "asof": OTHER_DAY, "source": "sina", "complete": True,
        "fetched_at": "1970-01-01T00:00:00+00:00",
        "points": [{"t": "09:30", "price": 10.0, "avg": 10.0, "vol": 1.0, "amount": 10.0}],
    }
    payload = C._intraday_payload(offline_cfg, offline_ds, BIG, data)
    assert payload["day"] == OTHER_DAY != SPOT_DAY
    assert calls["n"] == 0, "名称又走了 asof 相关的 ds.quote —— 与 INV-61「与 asof 解耦」相反"
    assert payload["name"] != BIG, "名称退化为代码（INV-61 违规）"
    assert payload["name"] == sentinel, "名称必须来自 name_of（与 asof 无关的那条路）"


def test_f3_intraday_payload_prefers_name_of(offline_cfg, offline_ds, monkeypatch):
    """接线证明（比结果更强）：`_intraday_payload` 的名称**来源**必须是 `ds.name_of`。"""
    seen = {"code": None}

    def _spy(code):
        seen["code"] = code
        return "哨兵名称"

    monkeypatch.setattr(offline_ds, "name_of", _spy)
    data = {"day": OTHER_DAY, "asof": OTHER_DAY, "source": "sina", "complete": True,
            "fetched_at": "x", "points": []}
    payload = C._intraday_payload(offline_cfg, offline_ds, BIG, data)
    assert seen["code"] == BIG, "没有调用 ds.name_of"
    assert payload["name"] == "哨兵名称"


# --------------------------------------------------------------------------- #
# T-PN-13 ★ 查看路径不得触发**全市场**快照（ultraqa FAIL：冷启动首屏 80.3–83.4s）
# --------------------------------------------------------------------------- #
def _cold_spot_root(cfg) -> None:
    """把夹具与缓存里的 spot **都**清掉 = 「冷 spot」（真冷启动的等价夹具场景）。"""
    for path in (Path(cfg.root) / "data" / "fixture" / "spot.json",
                 Path(cfg.root) / "data" / "cache" / "spot.json"):
        if path.exists():
            path.unlink()


def test_tpn13_chart_payload_never_fetches_full_market_spot(offline_cfg, monkeypatch):
    """★ **T-PN-13（夹具可判定版）**：冷 spot 下 `chart_payload` 的 `spot()` 调用次数必须 = **0**。

    为什么钉这条：`quote()` 缺省 `allow_fetch=True`，在「快照日 ≠ asof」的回退分支会调
    `spot([code])` —— 那是**东财全市场 59 页**快照（ultraqa 实测冷启动首屏 **80.3–83.4s**），
    而查看路径**只是要一个名称/涨跌停价**。这是**结构性质**（不受机器负载影响），
    比端到端墙钟更可靠（墙钟版由 Lead 在最终 QA 用「空根 + 真网络」跑）。

    ★ 修前实测为 **1 次**（`chart.py:524`），修后 **0 次**（`allow_fetch=False`）。
    """
    _cold_spot_root(offline_cfg)
    ds = DataSource(offline_cfg, offline=True)

    # ---- 非空洞前置：spot 必须真的是冷的（否则「0 次」可能只是因为快照分支命中了）----
    frame, spot_asof = ds._spot_frame_raw()
    assert len(frame) == 0 and spot_asof == "", (
        f"场景没造冷：spot 仍有 {len(frame)} 行 / asof={spot_asof!r} → 本判据会失去意义"
    )
    # asof 在**计数窗口之外**解析：`latest_trade_asof` 自己会走 `_has_quote → quote`（另一条相，
    # 属 W-P3 的 `chart_asof` 缓存范围，不在本判据口径内）。
    asof = ds.latest_trade_asof([BIG])

    calls = {"n": 0}
    real_spot = DataSource.spot

    def counting(self, codes=None):
        calls["n"] += 1
        return real_spot(self, codes)

    monkeypatch.setattr(DataSource, "spot", counting)
    payload = C.chart_payload(offline_cfg, ds, BIG, asof)

    assert calls["n"] == 0, (
        f"★查看路径触发了 {calls['n']} 次全市场 snapshot（`spot()`）—— "
        "`chart_payload` 里的 `ds.quote(...)` 必须传 `allow_fetch=False`"
        f"（冷启动会因此多花约 80s）。asof={asof}"
    )
    # 修好之后图仍然必须完整（不得靠「什么都取不到」来达标）
    assert len(payload["periods"]["daily"]["dates"]) > 0
    assert payload["latest"]["close"] is not None
    assert payload["code"] == BIG


def test_tpn13_include_today_also_never_fetches_spot(offline_cfg, monkeypatch):
    """★ T-PN-13 补充：**网页端真实路径**（`include_today=True`）同样不得抓全市场。

    `with_today_bar` 在冷快照下**本来就 early-return**（`if not spot_asof or spot_asof != asof`）
    → 它不该引入任何 `spot()` 调用；本用例把这条判断**实测**钉住（修前 1 次、修后 0 次）。
    """
    _cold_spot_root(offline_cfg)
    ds = DataSource(offline_cfg, offline=True)
    frame, spot_asof = ds._spot_frame_raw()
    assert len(frame) == 0 and spot_asof == "", "场景没造冷"
    # ★ asof 取**日历末日**（晚于日线末日 2026-10-09）→ 帧里**没有**当天 bar
    #   ⇒ 真的走到 `with_today_bar` 的快照分支 → 才能验证它「冷快照 early-return 且不抓 spot」。
    asof = LATE_DAY

    calls = {"n": 0}
    real_spot = DataSource.spot

    def counting(self, codes=None):
        calls["n"] += 1
        return real_spot(self, codes)

    monkeypatch.setattr(DataSource, "spot", counting)
    payload = C.chart_payload(offline_cfg, ds, BIG, asof, include_today=True)

    assert calls["n"] == 0, f"include_today 路径触发了 {calls['n']} 次 spot()"
    # 冷快照 ⇒ 不得合成当天 bar，且必须**显式标注原因**（INV-57）
    assert payload["today_bar"] is False
    assert payload["today_bar_reason"] == "no-fresh-snapshot"


# --------------------------------------------------------------------------- #
# T-PN-14 ★ 查看路径的 asof 解析与图必须**共用同一个日线窗口**（否则同源数据取两遍）
# --------------------------------------------------------------------------- #
def _span_days(start: str, end: str) -> int:
    """窗口跨度（自然日）= `end - start`。"""
    from datetime import date

    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _record_daily(monkeypatch) -> list[tuple[str, str, str, str]]:
    """记录 `DataSource.daily` 的 (code, start, end, 调用者函数名)。"""
    import traceback

    records: list[tuple[str, str, str, str]] = []
    real_daily = DataSource.daily

    def recording(self, code, start, end, *args, **kwargs):
        # 本包装函数自身在 test 文件里 → 已被过滤掉，故 `stack[-1]` 就是**调用方**。
        stack = [f for f in traceback.extract_stack()
                 if "test_perf_analysis" not in f.filename and "pandas" not in f.filename]
        caller = stack[-1].name if stack else "?"
        records.append((str(code), str(start)[:10], str(end)[:10], caller))
        return real_daily(self, code, start, end, *args, **kwargs)

    monkeypatch.setattr(DataSource, "daily", recording)
    return records


def test_tpn14_chart_window_span_comes_from_single_source(offline_cfg, monkeypatch):
    """★ 图的日线主查询跨度必须来自 **`datasource.VIEWER_DAILY_DAYS`**（单一真源）。

    为什么：`daily()` 的缓存 key 含 ``(start, end)``。asof 解析（`_latest_bar_day`）与图
    若各拿各的跨度，**同源数据会在同一个请求里被取两遍**（ultraqa 线程级实测 1.29s + 3.93s）。
    两边共用同一常量后**才有**命中的可能（是否真命中还取决于锚点，见下一条用例的说明）。

    ★ 判据做法：把 `datasource.VIEWER_DAILY_DAYS` **改掉**（730 → 500），
    两边的窗口跨度都必须**跟着变** —— 这才是「单一真源」（而不是「两边恰好都写 730」）。
    **修前必红**：修前图用自己的 `chart.LOOKBACK_DAYS`，改了真源也不会变。
    """
    from ptrade import datasource as datasource_mod

    ds = DataSource(offline_cfg, offline=True)
    for span in (730, 500, 640):
        monkeypatch.setattr(datasource_mod, "VIEWER_DAILY_DAYS", span)
        records = _record_daily(monkeypatch)
        asof = ds.chart_asof(BIG)
        records.clear()
        C.chart_payload(offline_cfg, ds, BIG, asof)

        chart_spans = {_span_days(s, e) for c, s, e, caller in records if caller == "chart_payload"}
        assert chart_spans == {span}, (
            f"真源改成 {span} 后，图的主查询跨度是 {sorted(chart_spans)} —— "
            "说明图没有读 `datasource.VIEWER_DAILY_DAYS`（两边会各取一遍）"
        )
        asof_records = [r for r in records if r[3] == "_latest_bar_day"]
        if asof_records:                      # 便宜分支下 asof 相不取数
            spans = {_span_days(s, e) for c, s, e, _ in asof_records}
            assert spans == {span}, f"`_latest_bar_day` 的跨度没跟随真源：{sorted(spans)}"


def test_tpn14_shared_key_when_anchors_coincide(offline_cfg, monkeypatch):
    """★ **机制判据**：两边**锚点相同**时，图与 asof 相的 `daily()` 窗口必须**完全相同** ⇒ 共享缓存 key。

    锚点 = 取数窗口的**右端**：asof 相用 ``latest_trade_date()``（日历末日），图用它的 `asof`。
    本用例刻意让两者相等（把图的 asof 取成 ``latest_trade_date()``）→ 此时
    ``daily(code, x - V, x)`` 两边字面相同 ⇒ 第二次是**命中**。

    ⚠️ **诚实标注（本轮实测的残余问题，已上报 Lead）**：在真实查看路径上两者锚点**不同** ——
    日历末日（2026-10-16）**晚于**有数据的那天（2026-10-09）。所以当前**这条 1 次取数还没达成**，
    仍需 Lead/W-P3 裁决锚点；本用例只钉住「机制正确」（锚点一旦一致就必然共享 key）。
    """
    from ptrade import datasource as datasource_mod

    ds = DataSource(offline_cfg, offline=True)
    records = _record_daily(monkeypatch)
    ds.chart_asof(BIG)
    asof_phase = [(c, s, e) for c, s, e, caller in records if caller == "_latest_bar_day"]
    assert asof_phase, "asof 相没有 `_latest_bar_day` 取数（判据口径失效？）"
    records.clear()

    anchor = ds.latest_trade_date()            # ★ 让图与 asof 相用同一个锚点
    C.chart_payload(offline_cfg, ds, BIG, anchor)
    chart_main = {(c, s, e) for c, s, e, caller in records if caller == "chart_payload"}

    assert len(chart_main) == 1, f"图应恰好一次主查询：{sorted(chart_main)}"
    assert chart_main == set(asof_phase), (
        f"锚点相同却窗口不同 —— 图 {sorted(chart_main)} vs asof 相 {sorted(asof_phase)}；"
        f"跨度真源={datasource_mod.VIEWER_DAILY_DAYS}"
    )


# --------------------------------------------------------------------------- #
# V3-W1 ★ K 线 option 唯一真源（`ptrade/webui_assets/chart_option.js`）
#
# 背景：`buildOption` 原先内联在 `chart.py` 的 `_APP_JS` 里，前端(W2)要复用它就必须**抽出去**。
# ★★ Critic C3：`__MINI_BOLL__`/`__MINI_SAR__` 占位符原本**就在 buildOption 体内**、
#   靠 Python `.replace()` 注入；抽函数时必须改成**参数**（`opts.miniCandlestick`），
#   否则前端没有替换机制 ⇒ BOLL/SAR 的迷你蜡烛**静默消失**。
# 判据口径：**求值运行时 option**（node），而不是在 HTML 里数字面量 —— 参数化之后
#   迷你蜡烛是**运行时**生成的，字面量里本来就不该有它们（那样等于又复制一份）。
# --------------------------------------------------------------------------- #
NODE = shutil.which("node")

#: 扫描「K 线 option 构建器」时要排除的目录（第三方/打包产物，防误红）
_EXCLUDE_DIRS = ("node_modules", "webui_dist", "vendor", "__pycache__")


def _scan_js_files(root: Path):
    for path in root.rglob("*.js"):
        if any(part in _EXCLUDE_DIRS for part in path.parts):
            continue
        yield path


def _node_json(source: str, *, module: bool = False) -> dict:
    """在 node 里跑一段 JS，返回它写到 stdout 的 JSON。"""
    suffix = ".mjs" if module else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8",
                                     newline="\n") as fh:
        fh.write(source)
        path = fh.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        assert proc.returncode == 0, f"node 执行失败：\n{proc.stderr[:1500]}"
        return json.loads(proc.stdout)
    finally:
        Path(path).unlink(missing_ok=True)


def _page_scripts(html: str) -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


def _render_option_via_page(html: str) -> dict:
    """**render_chart 路径**：把页面里**最后一个** `<script>`（= 内联 `chart_option.js` + 页面胶水）
    配上页面 `CHART_DATA`（`raw_decode` 取）+ `echarts/document/window` stub，在 node 里跑一遍，
    用 stub 接住 `chart.setOption` —— 得到的正是**页面真正会渲染的 option**。

    ★ 为什么不在 HTML 里数 series 字面量：迷你蜡烛由 JS 工厂 `miniCandlestick(1/5)` **运行时**
    构造（结构收口后这是**唯一**实现），字面量里本来就不该有 —— 有反而说明又复制了一份。
    """
    marker = "<script>const CHART_DATA = "
    assert html.count(marker) == 1
    data_js, _end = json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker):])
    scripts = [s for s in _page_scripts(html) if s.strip()]
    wrapper = scripts[-1]
    assert "function buildOption(" in wrapper and "switchPeriod" in wrapper, (
        "最后一个 <script> 应同时含内联真源与页面胶水"
    )
    stubs = (
        "let __captured = null;\n"
        "const window = { addEventListener: function () {}, __INITIAL_PERIOD__: undefined };\n"
        "const document = { getElementById: function () { return { className: '' }; } };\n"
        "const echarts = { init: function () { return {\n"
        "  setOption: function (o) { __captured = o; },\n"
        "  resize: function () {},\n"
        "}; } };\n"
        "const CHART_DATA = " + json.dumps(data_js, ensure_ascii=False) + ";\n"
    )
    source = stubs + wrapper + "\nprocess.stdout.write(JSON.stringify(__captured));\n"
    return _node_json(source)


def _frontend_option(payload: dict, period: str = "daily") -> dict:
    """**前端路径**：直接 import `chart_option.js`（ESM）→ 同一份真源、同一工厂。"""
    with tempfile.TemporaryDirectory(prefix="v3opt-", dir=r"D:\\pytest-bt") as work:
        target = Path(work) / "chart_option.mjs"
        target.write_text(C.CHART_OPTION_JS.read_text(encoding="utf-8"), encoding="utf-8")
        entry = Path(work) / "entry.mjs"
        entry.write_text(
            "import { buildOption, miniCandlestick } from './chart_option.mjs';\n"
            f"const payload = {json.dumps(payload, ensure_ascii=False)};\n"
            f"const opt = buildOption(payload, {json.dumps(period)}, {{\n"
            "  miniCandlestick: { boll: miniCandlestick(1), sar: miniCandlestick(5) },\n"
            "});\n"
            "process.stdout.write(JSON.stringify(opt));\n",
            encoding="utf-8",
        )
        proc = subprocess.run([NODE, str(entry)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        assert proc.returncode == 0, f"node(前端路径) 执行失败：\n{proc.stderr[:1500]}"
        return json.loads(proc.stdout)


def _panel_series(option: dict, axis: int) -> list[dict]:
    return [s for s in option["series"] if s.get("xAxisIndex") == axis]


@pytest.mark.skipif(NODE is None, reason="需要 node 求值运行时 option")
def test_v3_19_kline_option_builder_is_single_source(offline_cfg, offline_ds):
    """★ **T-V3-19**：K 线 option 构建器**全仓只有一份**；分时那份**具名**且不被误算。

    * 唯一真源 = `ptrade/webui_assets/chart_option.js`（`buildOption(payload, period, opts)`）；
    * `chart.py` **内联**它（`_load_chart_option_js()` 经**唯一一个** `_inline_js()` 变换）；
    * 分时那份 = `buildOptionIntraday`（在 `_INTRADAY_JS` 里），**不是** K 线构建器；
    * glob **排除** `node_modules`/`webui_dist`/`vendor`（防误红）。
    """
    asof = offline_ds.latest_trade_asof([BIG])
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)

    # ① 全仓只有一个「K 线 option 构建器」定义点
    pattern = re.compile(r"function\s+buildOption\s*\(\s*payload\s*,\s*period\s*,\s*opts\s*\)")
    hits = [p for p in _scan_js_files(Path(C.__file__).parent) if pattern.search(
        p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == [C.CHART_OPTION_JS], f"K 线 option 构建器应只有 {C.CHART_OPTION_JS}，实际 {hits}"

    # ② chart.py 里**不得**再有 K 线 buildOption 定义；分时构建器也已移到共享文件
    src = Path(C.__file__).read_text(encoding="utf-8")
    assert "function buildOption(period)" not in src, "chart.py 里残留旧的 K 线 buildOption(period)"
    # ★ X2 结构收口：分时构建器 = 共享文件里的 `buildIntradayOption`（见 T-V3-27），
    #   chart.py 里不得再残留任何分时 option 构建器（否则又分叉）
    assert "function buildOptionIntraday" not in src, "chart.py 里残留分时 option 构建器"
    assert "function buildIntradayOption(" in C.CHART_OPTION_JS.read_text(encoding="utf-8")

    # ③ 内联内容 == 文件经**唯一一个** `_inline_js()` 后的结果（只允许这一处变换）
    inline_file = C._load_chart_option_js()
    assert inline_file in html, "render_chart 必须内联 chart_option.js（经 _inline_js）"
    assert html.count("export function") == 0, "内联产物里不得残留 `export`（经典 <script> 不认）"
    # `_inline_js` 只做一件事：削掉行首 `export `
    raw = C.CHART_OPTION_JS.read_text(encoding="utf-8")
    assert C._inline_js(raw) == re.sub(r"^export\s+", "", raw, flags=re.M)
    assert src.count("def _inline_js(") == 1, "只允许一个内联变换函数"

    # ④ 两条路径都能真的求值出 option（否则「唯一真源」只是文件摆放位置）
    via_page = _render_option_via_page(html)
    assert len(via_page["series"]) >= 16 and via_page["series"], "页面路径求值不出 series"
    _frontend = _frontend_option(_render_data_from_html(html))
    assert len(_frontend["series"]) == len(via_page["series"]), (
        "前端路径与页面路径的 series 数不一致 —— 两条路径没走同一份真源"
    )


def _render_data_from_html(html: str) -> dict:
    import re as _re
    found = _re.search(r"<script>const CHART_DATA = (.*?);</script>", html, _re.S)
    assert found, "页面里找不到 CHART_DATA"
    return json.loads(found.group(1))


@pytest.mark.skipif(NODE is None, reason="需要 node 求值运行时 option")
def test_v3_20_chart_data_is_deep_equal(offline_cfg, offline_ds):
    """★ **T-V3-20**：页面内联的 `CHART_DATA` 与 Python 侧构造的载荷**深度相等**。

    ★ 用 `json.JSONDecoder().raw_decode` 解析（**不用正则**）—— 正则截断会在
    「数据里含 `};`」时静默截错。
    """
    asof = offline_ds.latest_trade_asof([BIG])
    payload = C.chart_payload(offline_cfg, offline_ds, BIG, asof)
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)

    marker = "const CHART_DATA = "
    assert html.count(marker) == 1
    start = html.index(marker) + len(marker)
    decoded, end = json.JSONDecoder().raw_decode(html[start:])
    assert end > 0
    expected = {
        "periods": payload["periods"],
        "initial_period": "daily",
        "code": payload["code"],
        "legend": C.legend_groups(payload),
    }
    assert decoded == json.loads(json.dumps(expected, ensure_ascii=False)), (
        "页面内联的 CHART_DATA 与 Python 载荷不深等"
    )


@pytest.mark.skipif(NODE is None, reason="需要 node 求值运行时 option")
def test_v3_13_seven_panels(offline_cfg, offline_ds):
    """★ **T-V3-13**：7 个面板都在（6 个图表面板 + 第 7 格分析区）。"""
    asof = offline_ds.latest_trade_asof([BIG])
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)
    option = _render_option_via_page(html)

    assert len(option["grid"]) == 6, f"应有 6 个图表面板，实际 {len(option['grid'])}"
    titles = [t["text"] for t in option["title"]]
    for token in ("① K线", "② BOLL", "③ 成交量", "④ 成交额", "⑤ MACD", "⑥ SAR"):
        assert any(token in t for t in titles), f"面板标题缺 {token}：{titles}"
    assert "⑦ 分析区（CANSLIM 七因子）" in html, "第 7 格（分析区）必须在页面上"


@pytest.mark.skipif(NODE is None, reason="需要 node 求值运行时 option")
def test_v3_15_volume_and_amount_are_separate_panels(offline_cfg, offline_ds):
    """★ **T-V3-15**：成交量与成交额是**两个**面板（gridIndex/yAxisIndex 不同）。"""
    asof = offline_ds.latest_trade_asof([BIG])
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)
    option = _render_option_via_page(html)

    volume = _panel_series(option, 2)
    amount = _panel_series(option, 3)
    assert any(s["name"] == "成交量" for s in volume), "成交量面板(2)缺 series"
    assert any(s["name"] == "成交额" for s in amount), "成交额面板(3)缺 series"
    assert 2 != 3
    assert all(s.get("yAxisIndex") == 2 for s in volume)
    assert all(s.get("yAxisIndex") == 3 for s in amount)


@pytest.mark.skipif(NODE is None, reason="需要 node 求值运行时 option")
def test_v3_24_boll_and_sar_panels_have_candles_both_paths(offline_cfg, offline_ds):
    """★★ **T-V3-24**：**两条路径**（render_chart 页面 / 前端复用同一份 JS）
    的 BOLL 与 SAR 面板里**各有 1 个 candlestick**（迷你蜡烛，用户显式需求）。

    ★ 变异：把 `opts.miniCandlestick` 置空 ⇒ 两条判据都必须红（**参数化**不是装饰）。
    """
    asof = offline_ds.latest_trade_asof([BIG])
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)

    page_option = _render_option_via_page(html)
    front_option = _frontend_option(_render_data_from_html(html))

    for label, option in (("render_chart 页面路径", page_option),
                          ("前端复用 chart_option.js 路径", front_option)):
        for axis, panel in ((1, "BOLL"), (5, "SAR")):
            candles = [s for s in _panel_series(option, axis) if s.get("type") == "candlestick"]
            assert len(candles) == 1, (
                f"★{label}: {panel} 面板(xAxisIndex={axis}) 的 candlestick 数应为 1，"
                f"实际 {len(candles)} —— 迷你蜡烛丢了？"
            )
            assert candles[0].get("data"), f"{label}: {panel} 迷你蜡烛缺 data（未按周期填 d.ohlc）"
            assert candles[0].get("barWidth") == "62%", f"{label}: 迷你蜡烛 barWidth 应为工厂默认"
    # 主图那颗必须在（3 颗 = 主图 + BOLL + SAR）
    all_candles = [s for s in page_option["series"] if s.get("type") == "candlestick"]
    assert len(all_candles) == 3, f"candlestick 总数应为 3，实际 {len(all_candles)}"


def test_v3_24c_mini_candlestick_has_single_implementation():
    """★★ **T-V3-24c**（结构级，**取代** T-V3-24b 的跨语言全字段深等）：迷你蜡烛**只有一份实现**。

    ★ 为什么升级（Architect）：跨语言深等只证明两份**相同**，不证明两份**都对** ——
    BOLL/SAR 曾因 `yAxis` 缺 `scale:true` **完全不可见**；两份一起错的话 `T-V3-24b` 照样绿。
    ⇒ 结构收口：**删掉 Python 那份工厂**，只留 `ptrade/webui_assets/chart_option.js` 的
    `miniCandlestick()`；`chart.py` 的页面胶水直接调它（同一 `<script>` 作用域）。
    这样「**按构造**不存在漂移」，跨语言判据随之退役。

    三条断言（缺一即失去意义）：
    1. `chart.py` 里**不存在** `def _miniCandlestick`，且 `_miniCandlestick` / `_MINI_BAR_WIDTH` /
       `__CHART_OPTS__` 字样出现 **0** 次（旧机制彻底退役 —— 防「偷偷加回来」）；
    2. 全仓（排除 node_modules / webui_dist / vendor）**只有一处** `function miniCandlestick(`；
    3. **非空洞**：那一处必须是 `chart_option.js`，且 `chart.py` 里仍有 `miniCandlestick(1)` /
       `miniCandlestick(5)` 的真实**调用**（否则「没有实现」也会让本判据通过 = 空守卫）。
    """
    chart_src = Path(C.__file__).read_text(encoding="utf-8")
    assert "def _miniCandlestick" not in chart_src, "Python 那份工厂又回来了（应只保留 JS 一份）"
    for token in ("_miniCandlestick", "_MINI_BAR_WIDTH", "__CHART_OPTS__"):
        assert chart_src.count(token) == 0, f"chart.py 里残留旧机制字样 {token}（应 0 次）"

    hits = [p for p in _scan_js_files(Path(C.__file__).parent)
            if "function miniCandlestick(" in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == [C.CHART_OPTION_JS], f"迷你蜡烛工厂应只有 {C.CHART_OPTION_JS} 一处，实际 {hits}"

    called = sorted(set(re.findall(r"miniCandlestick\((\d)\)", chart_src)))
    assert called == ["1", "5"], f"chart.py 页面胶水应调用 miniCandlestick(1)/(5)，实际 {called}"


def test_T_V3_25_chart_data_carries_non_empty_legend(offline_cfg, offline_ds):
    """★★ **T-V3-25**（X1 真缺陷的防回归判据）：**图例必须非空、且两条路径同源**。

    ### 事故（真事，不是假设）

    `chart_option.js` 的 `buildOption(payload, period)` 从 **`payload.legend`** 取图例条目
    （`:55-58` 展平 → `:168-169` 的 `legend.data`）。

    * **HTML/CLI 路径**注入的 `CHART_DATA` 是「瘦身版」`{"periods","initial_period","code","legend"}` ⇒ **有图例** ✓
    * **Web 路径**（`/api/chart/data`）曾把 `chart_payload(...)` **原样返回** —— 它**没有** `legend`
      ⇒ 前端 `legend.data = []` ⇒ **图例 0 条目、渲染不出、点不了**
      ⇒ **用户明确要求的「图例点击隐藏」实际不可用** ✗

    ★ **为什么之前所有判据都是绿的**：`T-V3-24` 查的是「面板/蜡烛」、`T-V3-20` 查的是「数值深等」，
      **没有任何一条查「图例有没有条目」**；当时只确认了 `!!opt.legend.length`（**配置存在**）⇒
      正是 `INTERFACES.md §8.3` **形态④「只验配置存在不验语义」**。

    ### 本判据

    1. **HTML 路径**：`render_chart` 内嵌的 `CHART_DATA.legend` 非空；
    2. **Web 路径**：`chart.option_data(payload, period).legend` 非空（`/api/chart/data` 用的就是它）；
    3. ★ **同源**：两条路径的 legend **都 == `chart.legend_groups(payload)`**，且**逐项相等**；
    4. ★ **非空洞**：展平后 **>= 6** 条，且含真实序列名 `K线` / `MA12` / `SAR`（防「塞个占位串也算非空」）。
    """
    import json

    asof = offline_ds.latest_trade_asof([BIG])
    html, payload = C.render_chart(offline_cfg, offline_ds, BIG, asof)

    # ── 1) HTML 路径：CHART_DATA.legend
    dec = json.JSONDecoder()
    m = re.search(r"const CHART_DATA = ", html)
    assert m, "判据前提不成立：页面里找不到 CHART_DATA"
    embedded, _end = dec.raw_decode(html, m.end())
    legend_embedded = embedded.get("legend")
    assert legend_embedded, f"HTML 路径的 CHART_DATA 没有 legend：{legend_embedded!r}"
    flat_embedded = [n for group in legend_embedded for n in group]
    assert len(flat_embedded) >= 6, f"HTML 路径图例条目太少：{flat_embedded}"
    for must in ("K线", "MA12", "SAR"):
        assert must in flat_embedded, f"HTML 路径图例缺真实序列名 {must!r}：{flat_embedded}"

    # ── 2) Web 路径：option_data(...).legend（/api/chart/data 返回的就是它）
    served = C.option_data(payload, "daily")
    legend_served = served.get("legend")
    assert legend_served, (
        "★ X1 回归：Web 路径的图例为空 ⇒ 前端 legend.data=[] ⇒ 图例渲染不出、点不了"
    )
    flat_served = [n for group in legend_served for n in group]
    assert len(flat_served) >= 6, f"Web 路径图例条目太少：{flat_served}"
    for must in ("K线", "MA12", "SAR"):
        assert must in flat_served, f"Web 路径图例缺真实序列名 {must!r}：{flat_served}"

    # ── 3) 两条路径**同源**（X1 的结构性根因：形状只在一边有 legend）
    expect = C.legend_groups(payload)
    assert legend_embedded == expect, (
        f"HTML 路径的 legend 与 legend_groups() 不同源：\n{legend_embedded!r}\n!=\n{expect!r}"
    )
    assert legend_served == expect, (
        f"Web 路径的 legend 与 legend_groups() 不同源：\n{legend_served!r}\n!=\n{expect!r}"
    )
    assert legend_embedded == legend_served, (
        "两条路径的图例不一致 —— option 输入形状又分叉了"
    )


# --------------------------------------------------------------------------- #
# X2 ★ 分时在 Web 端「点了报 TypeError、图停日线」的真缺陷修复
# 根因：前端所有周期都走 `buildOption(payload, period)`，但 minute 的 payload 是
# `render_intraday` 的形状（**没有 `periods`**）⇒ `payload.periods[period]` 炸。
# 修法（结构级）：分时 option 也抽到 `chart_option.js` 的 `buildIntradayOption(payload)`，
# `render_intraday` 的 HTML 路径**内联同一份**，Web 端按周期分派 —— 每个图型仍**只有一份**构建器。
# --------------------------------------------------------------------------- #
def test_v3_27_intraday_option_has_single_builder():
    """★ **T-V3-27（分时不分叉）**：分时 option 构建器全仓**只有一处**（`chart_option.js`）。

    三条断言（缺一即失去意义）：
    1. `chart.py` 里**没有**第二份分时 option（旧 `buildOptionIntraday` 及其 option 字面量已移出）；
    2. 全仓（排除 node_modules / webui_dist / vendor）**只有一处** `function buildIntradayOption(`，
       且必须是 `chart_option.js`；
    3. **非空洞**：`chart.py` 的页面胶水必须真的**调用**它（`buildIntradayOption(INTRADAY)`）——
       否则「谁都没用它」也会让本判据通过。
    """
    chart_src = Path(C.__file__).read_text(encoding="utf-8")
    assert "function buildOptionIntraday" not in chart_src, "chart.py 里还有第二份分时 option 构建器"
    assert "② 分时量" not in chart_src, "chart.py 里还有分时 option 字面量（面板标题）"

    hits = [p for p in _scan_js_files(Path(C.__file__).parent)
            if "function buildIntradayOption(" in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == [C.CHART_OPTION_JS], (
        f"分时 option 构建器应只有 {C.CHART_OPTION_JS} 一处，实际 {hits}"
    )

    # 非空洞：HTML 分时路径必须真的调用它（Web 端由 W2 用同名函数分派）
    assert "buildIntradayOption(INTRADAY)" in chart_src, "分时页面胶水没有调用 buildIntradayOption"


@pytest.mark.skipif(NODE is None, reason="需要 node 求值运行时 option")
def test_v3_27b_intraday_option_builds_two_panels(offline_cfg, offline_ds):
    """★ 分时页面（render_intraday）的**运行时 option**：2 个 grid + 价格/均价/分时量三个 series。

    ★ 验收的是「分时 payload + 共享构建器」这条真实链路（W2 主页面走的就是它）：
    若 `buildIntradayOption` 改名/返回 null，本用例与 T-V3-27 都会红。
    """
    from tests.test_chart_v2 import _write_intraday_cache

    _write_intraday_cache(offline_ds, BIG, {
        "2026-10-09": [("09:30", 10.0, 100.0, 1000.0),
                       ("09:31", 10.2, 120.0, 1224.0),
                       ("09:32", 10.1, 80.0, 808.0)],
    })
    html, payload = C.render_intraday(offline_cfg, offline_ds, BIG)
    assert payload["kind"] == "intraday" and payload["times"], "前置：分时载荷必须非空"

    marker = "<script>const INTRADAY = "
    assert html.count(marker) == 1
    data_js, _ = json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker):])
    scripts = [s for s in _page_scripts(html) if s.strip()]
    wrapper = scripts[-1]
    assert "buildIntradayOption(" in wrapper, "分时页面胶水必须调用共享构建器"
    stubs = (
        "let __captured = null;\n"
        "const window = { addEventListener: function () {} };\n"
        "const document = { getElementById: function () { return { className: '' }; } };\n"
        "const echarts = { init: function () { return {\n"
        "  setOption: function (o) { __captured = o; },\n"
        "  resize: function () {},\n"
        "}; } };\n"
        "const INTRADAY = " + json.dumps(data_js, ensure_ascii=False) + ";\n"
    )
    option = _node_json(stubs + wrapper + "\nprocess.stdout.write(JSON.stringify(__captured));\n")

    assert len(option["grid"]) == 2, f"分时应 2 个 grid，实际 {len(option['grid'])}"
    names = [s["name"] for s in option["series"]]
    assert names == ["价格", "均价", "分时量"], f"分时 series 不对：{names}"
    assert option["series"][2]["type"] == "bar" and option["series"][2]["barWidth"] == "60%"
    assert len(option["xAxis"][0]["data"]) == len(payload["times"])


# --------------------------------------------------------------------------- #
# V4-W1（纯视觉）：grid/title 百分比预算 + 图例 6 项
# --------------------------------------------------------------------------- #
KEEP_LEGEND = ("MA12", "MA24", "MA48", "MA96", "VOL MA5", "VOL MA10")


def test_v3_25_legend_same_source_and_filtered(offline_cfg, offline_ds):
    """★★ **T-V3-25**（V4 改法，MEDIUM-1）图例**同源 + 过滤到 6 项 + 两路径一致**。

    ★ 不是「改成 6 项白名单」那么粗：`expect` 仍**从 `chart.legend_groups(payload)` 派生**
    （保住「同源」维度）再按 `KEEP` 过滤，然后两路径都 `== expect` 且 `set(expect)==set(KEEP) and len==6`。
    ⇒ 比原来「== legend_groups 逐项」**更强**：既钉同源、又钉过滤结果。
    ★ **series 完整性由 `V4-L4` 承担**（按 gridIndex 分组计数），本判据只管图例。
    """
    asof = offline_ds.latest_trade_date()
    payload = C.chart_payload(offline_cfg, offline_ds, BIG, asof)
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)

    expect = [n for g in C.legend_groups(payload) for n in g if n in KEEP_LEGEND]
    assert set(expect) == set(KEEP_LEGEND) and len(expect) == 6, f"过滤结果不对：{expect}"

    from tests.test_perf_analysis import _frontend_option, _render_data_from_html

    legend_embedded = _render_option_via_page(html)["legend"]["data"]
    legend_served = _frontend_option(_render_data_from_html(html))["legend"]["data"]
    assert legend_embedded == expect, f"HTML 路径图例 {legend_embedded} != expect {expect}"
    assert legend_served == expect, f"Web 路径图例 {legend_served} != expect {expect}"
    assert legend_embedded == legend_served, "两条路径图例不一致（不是同一份实现）"


def test_v4_l3b_panel_titles_live_in_chart_option_js():
    """★ **V4-L3b**：面板标题（含 ②⑤⑥ 的说明）在 **`chart_option.js` 的 ECharts `title`** 里。

    ★ 说明文字是用户认线/认指标的**唯一依据** —— 图例精简到 6 项时**不许**顺手删它们。
    """
    js = C.CHART_OPTION_JS.read_text(encoding="utf-8")
    for text in ("① K线 + MA12/24/48/96", "② BOLL(20,2) 上/中/下轨",
                 "③ 成交量（股）+ VOL MA5/MA10", "④ 成交额（亿元）",
                 "⑤ MACD(12,26,9)", "⑥ SAR(0.02/0.02/0.20)"):
        assert f'text:"{text}"' in js, f"chart_option.js 的 title 缺少面板标题：{text}"


def test_v4_l5_grid_budget_title_no_overlap(offline_cfg, offline_ds):
    """★ **V4-L5 + R3-E**：grid 高度是 `%` 串、`top+height<=100`、合计 **≈76%**、
    875px 下 **K 面板 >= 240px**（旧 21% ⇒ 184px 恒红），且 **`title.top` 不叠到上一个面板**。
    """
    asof = offline_ds.latest_trade_date()
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)
    option = _render_option_via_page(html)

    def pct(s):
        assert isinstance(s, str) and s.endswith("%"), f"必须是 % 串，实际 {s!r}"
        return float(s[:-1])

    grids = option["grid"]
    assert len(grids) == 6
    tops = [pct(g["top"]) for g in grids]
    heights = [pct(g["height"]) for g in grids]
    for top, height in zip(tops, heights):
        assert top + height <= 100, f"grid 越界：top={top} height={height}"
    total = sum(heights)
    assert 75 <= total <= 77, f"grid 高度合计应 ≈76%，实际 {total}%"
    assert round(875 * heights[0] / 100) >= 240, f"K 面板 {(875*heights[0]/100):.0f}px < 240"
    assert round(1328 * heights[0] / 100) >= 240

    titles = option["title"]
    assert len(titles) == 6
    for i, (title, top, height) in enumerate(zip(titles, tops, heights)):
        title_top = pct(title["top"])
        assert title_top < top, f"title[{i}] 在 grid 顶之下：{title_top} >= {top}"
        if i:
            prev_bottom = tops[i - 1] + heights[i - 1]
            assert title_top >= prev_bottom, (
                f"★title[{i}] 叠到上一个面板：{title_top}% < 上一面板底 {prev_bottom}%"
            )


