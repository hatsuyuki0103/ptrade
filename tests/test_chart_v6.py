"""**V6（K 线从上市日算起）判据** —— 真源 `.omx/plans/test-spec-ptrade-v6.md`（R3 修订版）。

★ 关键前提（**Critic C2 的教训**）：**「体积达标」只有在「起点正确」的前提下才有意义** ——
  一个「窗口 = 5000 天」的假实现体积也达标，而老票仍然不是从上市日画。
⇒ 本文件里所有「上限/性能」类断言都**排在「起点正确」断言之后**。

## 改动前必红依据（实测）
* `_start_of("2026-10-19")` = `"2024-10-19"`（`asof - 730d`）
* `600519` 日线 484 根、起点 2024-10-21
* 三个周期共用一个 `startPct`（≈最近 120 根，与周期无关）
* `datasource.VIEWER_DAILY_DAYS` / `chart.LOOKBACK_DAYS` 都存在
"""

from __future__ import annotations

import gzip
import json
import pathlib
import shutil
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ★★ 在**模块导入时**捕获「真的」socket / akshare 入口 —— 那时 `tests/conftest.py` 的
#    `_no_network`（autouse，封 `socket.connect`）与 `block_real_network`（封 `DataSource._ak`）
#    **还没运行**。slow 判据要真联网，必须在用例里把它们恢复回去。
#    （照 `tests/test_webapp.py:45-46` 的既有先例；非 slow 判据仍全程断网，不受影响。）
import socket as _socket  # noqa: E402

_REAL_SOCKET_CONNECT = _socket.socket.connect
_REAL_CREATE_CONNECTION = _socket.create_connection
try:                                    # `_ak` 是 DataSource 的取数入口（conftest 会换成兜底桩）
    from ptrade.datasource import DataSource as _DS

    _REAL_AK = _DS._ak                  # noqa: SLF001
except Exception:                       # pragma: no cover - 极端情况下不阻断收集
    _REAL_AK = None


# ══════════════════════════════════════════════════════════════════════════ #
# V6-1 固定锚点（源码级，秒级）
# ══════════════════════════════════════════════════════════════════════════ #
def test_v6_1_fixed_anchor_date():
    """★ 窗口起点 = **A 股开市日**（固定锚），不是「asof − N 天」（会天天漂移）。"""
    from ptrade import chart, datasource

    assert datasource.VIEWER_START_DATE == "1990-12-19", \
        f"A 股开市日必须是锚点，实际 {datasource.VIEWER_START_DATE!r}"
    got = chart._start_of("2026-10-19")
    assert got == "1990-12-19", \
        f"图窗口起点必须是锚点（改动前 = asof-730d = 2024-10-19），实际 {got!r}"
    # ★ 显式传 days 时仍按 days（兼容老调用点，见 test-spec V6-1）
    assert chart._start_of("2026-10-19", 30) == "2026-09-19"


# ══════════════════════════════════════════════════════════════════════════ #
# V6-8 死别名与旧常量必须删除（R3-A：原写法 `or True` 是恒真）
# ══════════════════════════════════════════════════════════════════════════ #
def test_v6_8_no_dead_lookback_alias():
    """★ `LOOKBACK_DAYS` 是**导入时冻结**的死别名（全仓零使用）⇒ 必须删；
    `VIEWER_DAILY_DAYS` 留着就是**陷阱常量** ⇒ 也必须删。"""
    from ptrade import chart, datasource

    assert not hasattr(chart, "LOOKBACK_DAYS"), "死别名 LOOKBACK_DAYS 必须删除（导入时冻结的陷阱）"
    assert not hasattr(datasource, "VIEWER_DAILY_DAYS"), \
        "VIEWER_DAILY_DAYS 必须删除（留 730 兼容名 = 陷阱常量）"


# ══════════════════════════════════════════════════════════════════════════ #
# V6-7 成对改：asof 探针与图用**同一个锚点**（R3-B：AST 判据，不是字符串包含）
# ══════════════════════════════════════════════════════════════════════════ #
def test_v6_7_latest_bar_day_uses_anchor():
    """★ `_latest_bar_day` 的起点必须来自 `VIEWER_START_DATE`（漏改会让图与 asof 发现各取一遍）。

    ★ 它是**嵌套函数**（8 空格缩进，包在某个方法里）—— 既不是 module 属性、也不是 `DataSource` 属性，
      所以用 **AST 扫整个模块**把那个 `FunctionDef` 找出来。
    ★ 用 AST 而不是 `in src`：**注释里提到 `VIEWER_START_DATE` 不算数**（Critic 指出的形态⑤）。
    """
    import ast

    from ptrade import datasource

    tree = ast.parse(pathlib.Path(datasource.__file__).read_text(encoding="utf-8"))
    targets = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_latest_bar_day"]
    assert targets, "找不到 `_latest_bar_day`（判据失效，需人工核）"
    fn = targets[0]

    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    # ① 不得再出现已删除的常量（AST 级：Name 或 Attribute）
    assert "VIEWER_DAILY_DAYS" not in (names | attrs), \
        "`_latest_bar_day` 不得再用已删除的 VIEWER_DAILY_DAYS"
    # ② 必须引用锚点（AST 级，不看注释）
    assert ("VIEWER_START_DATE" in (names | attrs)), \
        "`_latest_bar_day` 的起点必须来自 VIEWER_START_DATE（AST 级，注释里写不算）"
    # ③ 必须真的取数（有 .daily(...) 调用）
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "daily"]
    assert calls, "`_latest_bar_day` 里找不到 .daily(...) 调用"


# ══════════════════════════════════════════════════════════════════════════ #
# V6-6 指标预热诚实（纯 Python，秒级）
# ══════════════════════════════════════════════════════════════════════════ #
def test_v6_6_ma96_warmup_is_null(tmp_path):
    """★ `MA96` 的前 95 个位置必须是 `None`（不得用上市前数据前向填充）。

    ★ 用**合成日线**（R2-D：不放 5MB 静态夹具）—— 窗口够长 ⇒ 预热段真的落在数据里、测得到。
    """
    from ptrade import chart
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    _write_synthetic_daily(tmp_path, code="600519", start="1990-12-19", rows=400)
    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=True)
    payload = chart.chart_payload(cfg, ds, "600519", "1991-12-31", include_today=False)
    # ★ 真实路径是 `periods.daily.ma["96"]`（不是 `indicators.ma96`）
    ma96 = payload["periods"]["daily"]["ma"]["96"]
    assert ma96[0] is None, f"MA96[0] 必须是 None（实际 {ma96[0]!r}）"
    assert ma96[94] is None, f"MA96[94] 必须是 None（实际 {ma96[94]!r}）"
    assert isinstance(ma96[95], (int, float)), f"MA96[95] 必须有值（实际 {ma96[95]!r}）"


# ══════════════════════════════════════════════════════════════════════════ #
# V6-9 ★★ 体积/压缩 —— **必须排在「起点正确」之后**（C2）
# ══════════════════════════════════════════════════════════════════════════ #
def test_v6_9_payload_size_only_after_correct_start(tmp_path):
    """★ 「体积达标」只有在「起点正确」的前提下才有意义。

    否则一个「窗口 = 5000 天」的实现体积也达标，而老票**仍然不是从上市日画**（= 需求没达成）。
    ★ 用 6023 行合成日线（起点 1990-12-19），断言：
      ① 起点 == 1990-12-19（**先断这条**）
      ② 根数 == 6023
      ③ 体积 / gzip 上界（**后断**）
    """
    from ptrade import chart
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    _write_synthetic_daily(tmp_path, code="600519", start="1990-12-19", rows=6023)
    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=True)
    asof = _last_synthetic_date(tmp_path, "600519")
    payload = chart.chart_payload(cfg, ds, "600519", asof, include_today=False)
    dates = payload["periods"]["daily"]["dates"]

    # ① **先断起点**（需求的实质）
    assert dates[0][:10] == "1990-12-19", f"起点 {dates[0][:10]} ≠ 锚点（窗口太短？）"
    # ② 根数
    assert len(dates) >= 6000, f"根数 {len(dates)} < 6000（应 ≈6023）"
    # ③ **后断体积**（防"为了体积把窗口缩回去"）
    raw = json.dumps(chart.option_data(payload, "daily"), ensure_ascii=False).encode("utf-8")
    assert len(raw) <= 2_400_000, f"payload {len(raw):,} B > 2.4 MB"
    assert len(gzip.compress(raw, 6)) <= 800_000, f"gzip 后 {len(gzip.compress(raw, 6)):,} B > 800 KB"


# ══════════════════════════════════════════════════════════════════════════ #
# V6-11 payload 必须带 coverage（诚实标注的可观测来源）
# ══════════════════════════════════════════════════════════════════════════ #
def test_v6_11_payload_carries_coverage(tmp_path):
    """★ `coverage = {first, last, requested_start}` —— 前端据此说「数据从 X 开始」。

    ★ **刻意没有 `complete` 布尔**：从 `daily()` 的返回无法区分「这只票上市就晚」与「取数被截断」，
      给假布尔比如实给三个可观测值更糟（这是有意的诚实取舍，写在这里防后人"补"成假布尔）。
    """
    from ptrade import chart
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    _write_synthetic_daily(tmp_path, code="600519", start="1990-12-19", rows=300)
    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=True)
    asof = _last_synthetic_date(tmp_path, "600519")
    payload = chart.chart_payload(cfg, ds, "600519", asof, include_today=False)
    cov = payload.get("coverage")
    assert isinstance(cov, dict), f"payload 必须带 coverage，实际 {cov!r}"
    assert cov.get("first") and cov.get("last"), f"coverage 缺 first/last：{cov!r}"
    assert cov.get("requested_start") == "1990-12-19", f"requested_start 必须是锚点：{cov!r}"
    assert "complete" not in cov, "★ 不得给假的 complete 布尔（见 docstring）"
    # 透传到 option_data（前端从它取）
    slim = chart.option_data(payload, "daily")
    assert slim.get("coverage") == cov, "option_data 必须透传 coverage（否则前端拿不到）"


# ══════════════════════════════════════════════════════════════════════════ #
# V6-13 slow marker 必须注册（C3：未注册会 warning，且 CI skip 会让 slow 判据无人验）
# ══════════════════════════════════════════════════════════════════════════ #
def test_v6_13_slow_marker_registered():
    import tomllib

    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = cfg.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    assert any("slow" in m for m in markers), f"slow marker 未注册（会 warning 且 CI skip 无人验）：{markers}"


# ══════════════════════════════════════════════════════════════════════════ #
# 慢判据：真实长历史（需网络；CI 显式 skip 不得算通过）
# ══════════════════════════════════════════════════════════════════════════ #
@pytest.mark.slow
def test_v6_2_3_first_bar_is_listing_day_real():
    """★ **数据源事实**：`daily` 第一根 == 该票上市日（需真实长历史 ⇒ `PTRADE_RUN_SLOW=1` 才跑）。

    实测（2026-10-20，真联网）：
      600519 → 6023 根、首根 **2001-08-27**
      601857 → 4596 根、首根 **2007-11-05**
      688825 →   55 根、首根 2026-07-27（新股，自然从 IPO）
    """
    from datetime import date

    # ★★ 恢复**真** socket 与**真** `_ak`：`conftest` 的全局断网是给其余 1183 条判据用的，
    #    这条 slow 判据要**真联网**取 35 年历史（铁律 9：每条 slow 判据必须真的能跑）。
    import socket as _sock

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_sock.socket, "connect", _REAL_SOCKET_CONNECT, raising=False)
    monkeypatch.setattr(_sock, "create_connection", _REAL_CREATE_CONNECTION, raising=False)
    if _REAL_AK is not None:
        monkeypatch.setattr(_DS, "_ak", _REAL_AK, raising=True)
    try:
        # 网络真的不通时**明确 skip 并说明**（不得静默当通过）
        import urllib.request as _u

        try:
            _u.urlopen("https://www.baidu.com", timeout=8)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"真实网络不可用（{type(exc).__name__}）⇒ 本判据不成立，**skip 不算通过**")
        _assert_real_listing_days()
    finally:
        monkeypatch.undo()


def _assert_real_listing_days() -> None:
    """真实长历史：`daily` 第一根 == 该票上市日（本函数内**已恢复真网络**）。"""
    from datetime import date

    from ptrade import chart
    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="v6-real-"))
    cfg = load_config(str(tmp))
    ds = DataSource(cfg, offline=False)
    for code, expect_first, min_bars in (("600519", "2001-08-27", 5500), ("601857", "2007-11-05", 4200)):
        payload = chart.chart_payload(cfg, ds, code, "2026-10-19", include_today=False)
        dates = payload["periods"]["daily"]["dates"]
        assert len(dates) >= min_bars, f"{code} 只有 {len(dates)} 根（应 ≥{min_bars}）"
        got = dates[0][:10]
        delta = abs((date.fromisoformat(got) - date.fromisoformat(expect_first)).days
                    - (date(2026, 10, 19) - date(2026, 10, 19)).days)
        assert delta <= 7, f"{code} 第一根 {got} ≠ 上市日 {expect_first}（±7 天）"


# ══════════════════════════════════════════════════════════════════════════ #
# 合成日线工具（R2-D：不提交 5MB 静态夹具，测试时现造）
# ══════════════════════════════════════════════════════════════════════════ #
def _write_synthetic_daily(root: pathlib.Path, *, code: str, start: str, rows: int) -> None:
    """在**隔离 root** 的 `data/fixture` 下造一条确定性日线（起点 `start`、`rows` 行）。

    ★ 为什么放 `data/fixture`：`DataSource(offline=True)` 的夹具兜底会读它，
      这样判据**不需要网络也不需要 5MB 静态文件**（R2-D 的裁决）。
    """
    from datetime import date, timedelta

    fix = root / "data" / "fixture"
    fix.mkdir(parents=True, exist_ok=True)
    d = date.fromisoformat(start)
    out = []
    day = d
    i = 0
    # ★ 生成 **`rows` 个交易日**（跳周末 ⇒ 日历上要多走 ≈ rows×7/5 天）
    while len(out) < rows:
        if day.weekday() < 5:           # 只在工作日产出（保持"交易日"形状）
            base = 10.0 + i * 0.01
            out.append({
                "date": day.isoformat(), "open": base, "high": base * 1.01, "low": base * 0.99,
                "close": base * 1.005, "volume": 1_000_000 + i, "amount": 1e7 + i,
                "pct_chg": 0.5, "turnover": 1.0,
            })
            i += 1
        day += timedelta(days=1)
    # （下面的旧循环体已删；保留 `out` 供写入）
    if False:  # pragma: no cover - 只为让下面的旧语句保持语法位置
        base = 10.0 + i * 0.01
        pass
    (fix / f"daily_{code}.json").write_text(
        json.dumps({"payload": out}, ensure_ascii=False), encoding="utf-8"
    )


def _last_synthetic_date(root: pathlib.Path, code: str) -> str:
    doc = json.loads((root / "data" / "fixture" / f"daily_{code}.json").read_text(encoding="utf-8"))
    rows = doc.get("payload") or []
    return str(rows[-1]["date"])[:10]
