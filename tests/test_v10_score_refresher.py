"""A4 判据：周末全市场分数刷新（窗口判定 / 范围 / 幂等台账 / 单 scorer / 守卫）。

真源：`.omx/plans/prd-ptrade-v10-r2.md` §3（M3）+ §4（A4）
评审要点对应：
* **Architect B3**：「周五收盘」判定**不能只看 `weekday()==4`**（节假日调休把周四变成最后交易日）
* **Critic B3 修正 1**：★ **必须排除「周一盘中」**（否则周一 10:00 也 True ⇒ 盘中触发全市场 ✗）
* **Critic B3 修正 2**：`next_trade_date` 依赖**历史**日历 ⇒ 取不到要回退
* **Critic B3 修正 3**：**ISO 周幂等台账**（一周最多一轮，判定宽松也不会重复烧时间）
* **Critic M3**：★ **必须复用一个 scorer**（每票新建是 103h 的根因）
* **Critic B5**：范围 = spot **过滤 6 位 A 股**（`UNIVERSE_CODES` 只有 32 只且含 3 只 ETF ✗）
* **Critic B4**：**单趟循环**，不是 `spot_refresher` 的 per-tick
* **Architect B8**：后台单票循环**允许**宽 except，但**必须先 re-raise 守卫**
* **Architect B2**：台账是**独立文件**，不能拿 analysis 缓存当进度（会被 TTL/清理打掉）
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from datetime import datetime

import pytest

from ptrade import score_refresher as sr

CAL = [
    "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",           # 周一到周四（周五 11-06 休市）
    "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
]
_TD = set(CAL)


def _w(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute)


def _win(now: datetime, cal=None) -> bool:
    dates = CAL if cal is None else cal
    return sr.is_weekend_window(now=now, trade_dates=dates, is_trade_date=lambda d: d in set(dates))


# ── V10-A4a · 窗口判定（纯函数，注入日历 + now）
@pytest.mark.parametrize(
    "name,now,expect",
    [
        ("周四收盘后（周五休市 ⇒ 周四=本周最后交易日）", _w(5, 16), True),
        ("★ 周四盘中", _w(5, 10), False),
        ("周一盘前 08:00", _w(9, 8), True),
        ("★★ 周一盘中 10:00（Critic B3 修正 1：绝不能触发全市场）", _w(9, 10), False),
        ("周一收盘后 16:00", _w(9, 16), False),
        ("周二 16:00", _w(10, 16), False),
        ("周三盘中", _w(11, 11), False),
        ("周五收盘后 11-13 16:00", _w(13, 16), True),
    ],
)
def test_v10_a4a_weekend_window(name, now, expect):
    """V10-A4a：窗口判定必须覆盖**周四当最后交易日**与**周一盘中**这两个易错点。"""
    assert _win(now) is expect, f"{name}: 期望 {expect}"


def test_v10_a4a_no_future_calendar_fallback():
    """V10-A4a'（Critic B3 修正 2）：日历无未来日期时，回退到「日历最大日 == 最近已收盘交易日」。"""
    # 日历只到 11-05（周四），now = 11-05 16:00 ⇒ 之后无交易日 ⇒ 应判 True（跨周缺口）
    assert _win(_w(5, 16), cal=CAL[:4]) is True
    # 而 11-04 16:00（日历里还有 11-05）⇒ prev=11-04 next=11-05 同周 ⇒ False
    assert _win(_w(4, 16), cal=CAL[:4]) is False


def test_v10_a4a_empty_calendar():
    """空日历 ⇒ False（不猜，不乱跑）。"""
    assert _win(_w(5, 16), cal=[]) is False


# ── V10-A4b · 单趟循环（不是 per-tick）
def test_v10_a4b_single_pass_loop_not_per_tick():
    """V10-A4b（Critic B4）：批量必须是**单趟 for 循环**，不是「每 tick 一只票」。

    ★ `spot_refresher` 的 per-tick 模型 + 惰性门控会让 4–8h 的任务**永远跑不完** ✗
    """
    src = inspect.getsource(sr.ScoreRefresher._run_batch)
    assert "for code in codes" in src, "缺单趟循环"
    # ★ 不得出现按 tick 推进单票的痕迹
    assert "self._tick()" not in src
    # ★ 必须有台账落盘（可断点续）
    assert "write_ledger" in src


def test_v10_a4b_offline_never_runs():
    """V10-A4b'：`--offline` 是硬闸 —— `_tick` 必须直接返回 False。"""
    src = inspect.getsource(sr.ScoreRefresher._tick)
    assert "offline" in src
    assert "return False" in src


# ── V10-A4c · 范围：周末全市场 / 平时持仓∪自选
def test_v10_a4c_filter_a_shares_only():
    """V10-A4c（Critic B5）：必须**只留 6 位 A 股**，排除 ETF/LOF/B 股。"""
    assert sr._is_a_share("600594") is True
    assert sr._is_a_share("300869") is True
    assert sr._is_a_share("688371") is True
    # ★ 这三只是 `UNIVERSE_CODES` 里的 ETF —— 绝不能被当股票打分 ✗
    assert sr._is_a_share("588000") is False
    assert sr._is_a_share("510050") is False
    assert sr._is_a_share("159915") is False
    assert sr._is_a_share("") is False
    assert sr._is_a_share("60059") is False


def test_v10_a4c_weekday_scope_is_positions_union_watchlist():
    """V10-A5：平时范围 = 持仓 ∪ 自选池（≤40 只），不得是全市场。"""

    class _Pos:
        pass

    class _St:
        positions = {"600594": _Pos(), "300869": _Pos()}

    items = [{"code": "601857"}, {"code": "600594"}, {"code": "588000"}]
    codes = sr.weekday_codes(None, _St(), items)
    assert codes == ["600594", "300869", "601857"], f"结果 {codes}"
    assert "588000" not in codes, "ETF 混进来了"
    assert len(codes) <= 40


# ── V10-A4d · ISO 周幂等台账
def test_v10_a4d_ledger_roundtrip_and_idempotency(tmp_path):
    """V10-A4d（Architect B2 + Critic B3 修正 3）：台账能原子读写，且**同一 ISO 周幂等**。"""
    cfg = type("C", (), {"root": str(tmp_path)})()
    week = sr.iso_week_key(_w(13, 16))
    assert week == "2026-W46", week
    assert sr.read_ledger(cfg, week) is None
    doc = {"asof": "2026-11-13", "iso_week": week, "total": 3,
           "done": ["600594"], "failed": {}, "status": "running"}
    sr.write_ledger(cfg, week, doc)
    got = sr.read_ledger(cfg, week)
    assert got is not None and got["done"] == ["600594"] and got["status"] == "running"
    # ★ 台账是**独立文件**，不在 analysis_/chart_asof_ 白名单里 ⇒ 不会被 cleanup 清
    names = [p.name for p in (tmp_path / "data" / "cache").iterdir()]
    assert any(n.startswith("score_refresh_") for n in names), names
    assert not any(n.startswith("analysis_") for n in names), "台账不许混进 analysis_ 命名空间"


def test_v10_a4d_done_week_is_skipped(tmp_path):
    """V10-A4d'：本周 status=done ⇒ `_tick` 不再跑（**一周最多一轮**）。"""
    cfg = type("C", (), {"root": str(tmp_path)})()

    class _DS:
        offline = False
        def trade_dates(self):
            return CAL
        def latest_trade_asof(self, codes):
            return "2026-11-06"

    r = sr.ScoreRefresher(cfg, _DS(), now_fn=lambda: _w(13, 16))
    week = sr.iso_week_key(_w(13, 16))
    sr.write_ledger(cfg, week, {"status": "done", "done": ["600594"], "total": 1})
    assert r._tick() is False, "本周已跑完却仍触发"


# ── V10-A4e · 单 scorer（Critic M3 —— 可交付性的核心）
def test_v10_a4e_reuses_single_scorer():
    """V10-A4e（Critic M3）：★ `_run_batch` 里 **`CanslimScorer` 只许构造 1 次**。

    ★ 每票新建 scorer ⇒ 丢掉 `strategy.py:592-602` 的全部实例级缓存 ⇒ **103h** ✗
    复用一个 ⇒ 每票 1.5–4.5s ⇒ 5024 只 ≈ **4–8h** ⇒ 一个周末够 ✓
    """
    src = inspect.getsource(sr.ScoreRefresher._run_batch)
    assert src.count("CanslimScorer(") == 1, (
        f"CanslimScorer 构造了 {src.count('CanslimScorer(')} 次 —— 必须只有 1 次（复用实例）"
    )
    # 且构造位置**在循环之前**
    i_ctor = src.index("CanslimScorer(")
    i_loop = src.index("for code in codes")
    assert i_ctor < i_loop, "scorer 必须在循环外构造（复用一个实例）"


# ── V10-A4f · 后台单票循环允许宽 except，但必须 re-raise 守卫（Architect B8）
def test_v10_a4f_guard_reraised_in_loop():
    """V10-A4f（Architect B8）：单票循环允许宽 `except`，但**必须先 re-raise 守卫**。"""
    for fn in (sr.ScoreRefresher._tick, sr.ScoreRefresher._run_batch):
        src = inspect.getsource(fn)
        assert "except NetworkForbiddenInLock" in src, f"{fn.__name__} 缺守卫 re-raise"
        assert "raise" in src


def test_v10_a4f_no_guard_swallowed_by_broad_handler():
    """V10-A4f'（INV-42/45 语义）：逐函数 AST 检查 —— 宽 handler 前必须有 guard re-raise。"""
    tree = ast.parse(pathlib.Path(sr.__file__).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        has_guard = any("NetworkForbiddenInLock" in (ast.unparse(h.type) if h.type else "")
                        for h in node.handlers)
        if has_guard:
            continue
        broad = [
            h for h in node.handlers
            if h.type is None or (isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException"))
        ]
        if broad:
            called = [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]
            if any("trade_dates" in c or "latest_trade_asof" in c for c in called):
                bad.append(f"line {node.lineno}")
    assert not bad, f"这些 try 会吞守卫错误：{bad}"


# ── V10-A4g · 端点
def test_v10_a4g_status_endpoint_shape():
    """V10-A4g：`/api/score_refresh_status` 必须返回 `status/total/done/failed` 四要素。"""
    from ptrade import webapp as webapp_mod

    src = inspect.getsource(webapp_mod.WebApp.api_score_refresh_status)
    for k in ("status", "total", "done", "failed"):
        assert f'"{k}"' in src, f"缺 {k}"
    assert "read_ledger" in src, "必须读台账（重启后进度不丢）"
