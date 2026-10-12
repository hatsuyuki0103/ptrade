"""tests/test_chart.py — task-7 K 线图：指标数学 / 重采样 / 离线自包含渲染。

全部离线（conftest 的 ``_no_network`` 为 autouse，任何 socket.connect 会当场失败），
数据来自 ``data/fixture``（``offline_ds``）。
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ptrade import chart as C  # noqa: E402

MID = "002594"
BIG = "600519"
ETF = "588000"  # 用户截图里的华夏科创50ETF


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def make_bars(n: int = 60, start: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    """构造确定性 K 线：close = start + i×step，high/low = close ± 1。"""
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-06", periods=n, freq="B").strftime("%Y-%m-%d"),
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000.0 + i for i in range(n)],
            "amount": [1.0e6 + i for i in range(n)],
        }
    )


def ema_ref(values: list[float], n: int) -> list[float]:
    """独立算式：EMA 递推 ``e[i] = a·x[i] + (1-a)·e[i-1]``，``a = 2/(n+1)``，``e[0]=x[0]``。"""
    alpha = 2.0 / (n + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


# =========================================================================== #
# 1. 指标纯函数（独立算式核对）
# =========================================================================== #
def test_ema_matches_recursive_formula():
    values = [float(100 + i) for i in range(40)]
    got = C.ema(pd.Series(values), 12)
    ref = ema_ref(values, 12)
    assert len(got) == len(ref)
    for i in (0, 1, 5, 20, 39):
        assert got.iloc[i] == pytest.approx(ref[i], abs=1e-12)


def test_ma_periods_and_values():
    frame = make_bars(120)
    enriched = C.add_indicators(frame)
    for n in C.MA_PERIODS:
        assert f"ma{n}" in enriched
    closes = frame["close"].astype(float)
    for n in C.MA_PERIODS:
        expected = closes.rolling(n).mean().iloc[-1]
        assert enriched[f"ma{n}"].iloc[-1] == pytest.approx(expected, abs=1e-9)
    # 前 n-1 根不足窗口 → NaN
    assert math.isnan(enriched["ma96"].iloc[94])
    assert not math.isnan(enriched["ma96"].iloc[95])


def test_macd_matches_independent_recursion():
    frame = make_bars(60)
    enriched = C.add_indicators(frame)
    closes = [float(x) for x in frame["close"]]

    ema12 = ema_ref(closes, C.MACD_FAST)
    ema26 = ema_ref(closes, C.MACD_SLOW)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = ema_ref(dif, C.MACD_SIGNAL)
    hist = [2.0 * (d - s) for d, s in zip(dif, dea)]

    assert enriched["macd_dif"].iloc[-1] == pytest.approx(dif[-1], abs=1e-10)
    assert enriched["macd_dea"].iloc[-1] == pytest.approx(dea[-1], abs=1e-10)
    assert enriched["macd_hist"].iloc[-1] == pytest.approx(hist[-1], abs=1e-10)


def test_boll_uses_population_std_ddof0():
    """BOLL 的 STD 必须是**总体标准差**（ddof=0，通达信/同花顺口径），不是样本标准差。"""
    frame = make_bars(60)
    enriched = C.add_indicators(frame)
    window = [float(x) for x in frame["close"].iloc[-C.BOLL_N :]]
    mid = sum(window) / len(window)
    var0 = sum((x - mid) ** 2 for x in window) / len(window)  # ddof=0
    var1 = sum((x - mid) ** 2 for x in window) / (len(window) - 1)  # ddof=1
    upper0 = mid + C.BOLL_K * math.sqrt(var0)
    upper1 = mid + C.BOLL_K * math.sqrt(var1)

    assert enriched["boll_mid"].iloc[-1] == pytest.approx(mid, abs=1e-9)
    assert enriched["boll_upper"].iloc[-1] == pytest.approx(upper0, abs=1e-9)
    assert enriched["boll_lower"].iloc[-1] == pytest.approx(mid - C.BOLL_K * math.sqrt(var0), abs=1e-9)
    # 反例哨兵：若误用 ddof=1，上轨会是 upper1（两值差异必须显著）
    assert abs(upper1 - upper0) > 1e-3
    assert enriched["boll_upper"].iloc[-1] != pytest.approx(upper1, abs=1e-9)


def test_boll_default_params():
    assert (C.BOLL_N, C.BOLL_K) == (20, 2.0)
    assert (C.MACD_FAST, C.MACD_SLOW, C.MACD_SIGNAL) == (12, 26, 9)
    assert C.MA_PERIODS == (12, 24, 48, 96)
    assert C.VOL_MA_PERIODS == (5, 10)
    assert (C.SAR_AF_START, C.SAR_AF_STEP, C.SAR_AF_MAX) == (0.02, 0.02, 0.20)


# =========================================================================== #
# 2. SAR 三例
# =========================================================================== #
def test_sar_monotonic_up_is_long_and_below_lows():
    highs = pd.Series([10.0 + i for i in range(40)])
    lows = pd.Series([9.0 + i for i in range(40)])
    out = C.sar(highs, lows)

    assert set(out["trend"]) == {1}, "单调上涨必须全程多头"
    assert (out["sar"].diff().dropna() >= -1e-12).all(), "SAR 不得回落"
    # 第 0 根按两日极值播种（等于 low[0]），此后必须严格低于当日最低价
    for i in range(1, len(out)):
        assert out["sar"].iloc[i] < lows.iloc[i], f"bar {i}: SAR 未低于 low"


def test_sar_monotonic_down_is_short_and_above_highs():
    highs = pd.Series([60.0 - i for i in range(40)])
    lows = pd.Series([59.0 - i for i in range(40)])
    out = C.sar(highs, lows)

    assert set(out["trend"]) == {-1}, "单调下跌必须全程空头"
    assert (out["sar"].diff().dropna() <= 1e-12).all(), "SAR 不得抬升"
    for i in range(1, len(out)):
        assert out["sar"].iloc[i] > highs.iloc[i], f"bar {i}: SAR 未高于 high"


def test_sar_single_reversal():
    """30 根上涨 + 15 根急跌 → 恰好一次状态切换（多转空），之后不再翻回。"""
    highs = [10.0 + i for i in range(30)] + [39.0 - 2.0 * i for i in range(15)]
    lows = [h - 1.0 for h in highs]
    out = C.sar(pd.Series(highs), pd.Series(lows))

    flips = [i for i in range(1, len(out)) if out["trend"].iloc[i] != out["trend"].iloc[i - 1]]
    assert len(flips) == 1, f"应恰好一次反转，实际 {len(flips)} 次: {flips}"
    flip = flips[0]
    assert out["trend"].iloc[flip - 1] == 1 and out["trend"].iloc[flip] == -1
    assert set(out["trend"].iloc[flip:]) == {-1}, "反转后不得再翻回"


def test_sar_edge_cases():
    assert len(C.sar(pd.Series([], dtype=float), pd.Series([], dtype=float))) == 0
    one = C.sar(pd.Series([10.0]), pd.Series([9.0]))
    assert list(one["trend"]) == [1] and one["sar"].iloc[0] == 9.0


# =========================================================================== #
# 3. 周 / 月线重采样
# =========================================================================== #
def test_resample_weekly_ohlc_and_sums():
    """2025-01-06(一) ~ 2025-01-17(五) 共 10 个交易日 → 2 个 ISO 周。"""
    dates = pd.date_range("2025-01-06", periods=10, freq="B")
    frame = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": [10.0 + i for i in range(10)],
            "high": [20.0 + i for i in range(10)],
            "low": [5.0 + i for i in range(10)],
            "close": [15.0 + i for i in range(10)],
            "volume": [100.0 * (i + 1) for i in range(10)],
            "amount": [1000.0 * (i + 1) for i in range(10)],
        }
    )
    weekly = C.resample_ohlc(frame, "weekly")
    assert len(weekly) == 2

    first = weekly.iloc[0]
    assert first["date"] == "2025-01-10"
    assert first["open"] == 10.0  # 首日 open
    assert first["close"] == 19.0  # 末日 close
    assert first["high"] == 24.0  # 区间 max
    assert first["low"] == 5.0  # 区间 min
    assert first["volume"] == sum(100.0 * (i + 1) for i in range(5))
    assert first["amount"] == sum(1000.0 * (i + 1) for i in range(5))

    second = weekly.iloc[1]
    assert second["date"] == "2025-01-17"
    assert second["open"] == 15.0 and second["close"] == 24.0
    assert second["high"] == 29.0 and second["low"] == 10.0
    assert second["volume"] == sum(100.0 * (i + 1) for i in range(5, 10))


def test_resample_weekly_keeps_partial_last_week():
    """跨周的 3 个交易日（周三/周四/周五 + 下周一）→ 最后不足一周也保留。"""
    dates = ["2025-01-08", "2025-01-09", "2025-01-10", "2025-01-13"]
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [2.0, 3.0, 4.0, 5.0],
            "low": [0.5, 1.5, 2.5, 3.5],
            "close": [1.5, 2.5, 3.5, 4.5],
            "volume": [10.0, 20.0, 30.0, 40.0],
            "amount": [100.0, 200.0, 300.0, 400.0],
        }
    )
    weekly = C.resample_ohlc(frame, "weekly")
    assert len(weekly) == 2
    assert weekly.iloc[-1]["date"] == "2025-01-13"
    assert weekly.iloc[-1]["open"] == 4.0 and weekly.iloc[-1]["close"] == 4.5
    assert weekly.iloc[-1]["volume"] == 40.0


def test_resample_monthly_ohlc_and_sums():
    dates = ["2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04"]
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0, 11.0, 20.0, 21.0],
            "high": [15.0, 16.0, 25.0, 26.0],
            "low": [8.0, 9.0, 18.0, 19.0],
            "close": [12.0, 13.0, 22.0, 23.0],
            "volume": [1.0, 2.0, 3.0, 4.0],
            "amount": [10.0, 20.0, 30.0, 40.0],
        }
    )
    monthly = C.resample_ohlc(frame, "monthly")
    assert len(monthly) == 2
    jan = monthly.iloc[0]
    assert (jan["open"], jan["high"], jan["low"], jan["close"]) == (10.0, 16.0, 8.0, 13.0)
    assert (jan["volume"], jan["amount"]) == (3.0, 30.0)
    feb = monthly.iloc[1]
    assert (feb["open"], feb["close"], feb["volume"]) == (20.0, 23.0, 7.0)


def test_resample_daily_identity_and_unknown_period():
    frame = make_bars(10)
    same = C.resample_ohlc(frame, "daily")
    assert len(same) == 10
    assert same["close"].tolist() == frame["close"].tolist()
    with pytest.raises(ValueError):
        C.resample_ohlc(frame, "quarterly")


def test_volume_ma_and_bar_colors():
    frame = make_bars(30)
    frame.loc[3, "close"] = frame.loc[3, "open"] - 5.0  # 造一根阴线
    enriched = C.add_indicators(frame)
    for n in C.VOL_MA_PERIODS:
        expected = frame["volume"].astype(float).rolling(n).mean().iloc[-1]
        assert enriched[f"vol_ma{n}"].iloc[-1] == pytest.approx(expected, abs=1e-9)
    assert bool(enriched["bar_up"].iloc[3]) is False, "收 < 开 应为绿（bar_up=False）"
    assert bool(enriched["bar_up"].iloc[4]) is True


# =========================================================================== #
# 3b. 离线自包含谓词的非空洞性（CR-18：空守卫比没有守卫更糟）
# =========================================================================== #
def test_external_refs_detects_real_cdn_script():
    """喂**真 CDN** ``<script src>`` → 必须非空 / ``no_external_refs()`` 必须 False。

    CR-18 回归哨兵：旧实现会把整个 ``<script …>…</script>``（连同 src 属性）抹成
    ``<script></script>``，导致此例**假绿**。
    """
    html = (
        '<html><head><meta charset="utf-8">'
        '<script src="https://cdn.example.com/echarts.min.js"></script>'
        "</head><body><p>hi</p></body></html>"
    )
    refs = C.external_refs(html)
    assert refs, "真 CDN <script src> 必须被抓到（否则守卫是空的）"
    assert C.no_external_refs(html) is False, "含真外链时必须判 False"
    # 开标签属性必须被保留（根因）
    assert "src=" in C._strip_script_bodies(html), "剥内容时不得连开标签属性一起抹掉"
    assert "echarts.min.js" in " ".join(refs)


@pytest.mark.parametrize(
    "snippet",
    [
        '<script src="//cdn.example.com/echarts.min.js"></script>',
        '<script src="/static/echarts.min.js"></script>',
        '<script src="vendor/echarts.min.js"></script>',
        '<link rel="stylesheet" href="https://cdn.example.com/x.css">',
        '<link rel="stylesheet" href="style.css">',
        '<img src="https://cdn.example.com/logo.png">',
        '<iframe src="https://example.com/embed"></iframe>',
        '<a href="https://example.com/doc">doc</a>',
        '@import url("https://cdn.example.com/x.css");',
        "body { background: url(https://cdn.example.com/bg.png); }",
        'fetch("https://api.example.com/data")',
    ],
)
def test_external_refs_detects_each_reference_kind(snippet):
    """各类外链形态都必须被抓到（含协议相对 / 相对路径 / JS fetch）。"""
    html = f'<html><head><meta charset="utf-8"></head><body>{snippet}</body></html>'
    refs = C.external_refs(html)
    assert refs, f"未抓到外链: {snippet}"
    assert C.no_external_refs(html) is False


@pytest.mark.parametrize(
    "snippet",
    [
        '<img src="data:image/png;base64,iVBORw0KGgo=">',
        '<img src="data:image/svg+xml;utf8,<svg></svg>">',
        '<a href="#panel-3">跳到第 3 格</a>',
        '<a href="javascript:void(0)">noop</a>',
        '<script>var inline = "<p>not a ref</p>";</script>',
        "<style>body{background:#fff}</style>",
    ],
)
def test_external_refs_does_not_flag_inline_resources(snippet):
    """``data:`` / ``#`` / ``javascript:`` / 纯内联脚本**不得**误报为外链。"""
    html = f'<html><head><meta charset="utf-8"></head><body>{snippet}</body></html>'
    refs = C.external_refs(html)
    assert refs == [], f"误报外链: {refs}（片段 {snippet}）"
    assert C.no_external_refs(html) is True


def test_external_refs_predicate_is_not_vacuous_on_real_artifacts(tmp_path, offline_cfg, offline_ds):
    """真实产物：``external_refs() == []``（保持绿），且谓词对篡改版**确实会变红**。"""
    path = C.build_chart(
        offline_cfg, offline_ds, MID, offline_ds.latest_trade_date(),
        out=str(tmp_path / "vacuity.html"),
    )
    html = Path(path).read_text(encoding="utf-8")
    assert C.external_refs(html) == [], "真实产物必须自包含"
    assert C.no_external_refs(html) is True

    tampered = html.replace(
        "<head>",
        '<head><script src="https://cdn.example.com/echarts.min.js"></script>',
        1,
    )
    assert tampered != html, "篡改必须生效（否则该哨兵无效）"
    assert C.external_refs(tampered), "把内联 ECharts 换成 CDN 后必须被抓到（这才是守卫的意义）"
    assert C.no_external_refs(tampered) is False


# =========================================================================== #
# 4. chart_payload（夹具，离线）
# =========================================================================== #
def test_payload_fixture_600519(offline_cfg, offline_ds):
    asof = offline_ds.latest_trade_date()
    payload = C.chart_payload(offline_cfg, offline_ds, BIG, asof)

    assert payload["code"] == BIG
    assert payload["asof"] == asof
    assert payload["adjust"] == "前复权"
    assert payload["latest"]["close"] is not None
    for period in C.PERIODS:
        frame = payload["periods"][period]
        assert len(frame["dates"]) > 0, f"{period} 无数据"
        assert len(frame["ohlc"]) == len(frame["dates"])
        assert set(frame) >= {
            "dates", "ohlc", "volume", "amount", "bar_up", "ma", "vol_ma", "boll", "macd", "sar",
        }
        assert sorted(frame["ma"]) == ["12", "24", "48", "96"]
        assert sorted(frame["vol_ma"]) == ["10", "5"]
        assert sorted(frame["boll"]) == ["lower", "mid", "upper"]
        assert sorted(frame["macd"]) == ["dea", "dif", "hist"]
        assert sorted(frame["sar"]) == ["trend", "value"]
        assert all(len(v) == len(frame["dates"]) for v in frame["ma"].values())
        assert all(len(v) == len(frame["dates"]) for v in frame["macd"].values())
        assert len(frame["sar"]["value"]) == len(frame["dates"])
        assert all(len(x) == 4 for x in frame["ohlc"]), "ECharts K 线须为 [open, close, low, high]"
        assert all(t in (-1, 1) for t in frame["sar"]["trend"])
    assert payload["meta"]["bars"]["daily"] >= payload["meta"]["bars"]["weekly"]
    assert payload["meta"]["bars"]["weekly"] >= payload["meta"]["bars"]["monthly"]


def test_payload_fixture_588000_etf(offline_cfg, offline_ds):
    """用户截图标的：科创50ETF 588000（ETF 兜底路径）。"""
    asof = offline_ds.latest_trade_date()
    payload = C.chart_payload(offline_cfg, offline_ds, ETF, asof)
    assert payload["periods"]["daily"]["dates"], "ETF 也必须有日线"
    assert payload["latest"]["close"] is not None
    for period in C.PERIODS:
        assert payload["periods"][period]["ma"]["12"][-1] is not None


def test_payload_is_strict_json(offline_cfg, offline_ds):
    payload = C.chart_payload(offline_cfg, offline_ds, MID, offline_ds.latest_trade_date())
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)  # 不得有 NaN/Infinity
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["code"] == MID


def test_payload_qfq_vs_unadjust_differ(offline_cfg, offline_ds):
    asof = offline_ds.latest_trade_date()
    qfq = C.chart_payload(offline_cfg, offline_ds, BIG, asof, use_qfq=True)
    raw = C.chart_payload(offline_cfg, offline_ds, BIG, asof, use_qfq=False)
    assert qfq["adjust"] == "前复权" and raw["adjust"] == "不复权"
    assert qfq["periods"]["daily"]["ohlc"][0] != raw["periods"]["daily"]["ohlc"][0]


def test_payload_unknown_code_raises(offline_cfg, offline_ds):
    """未知代码 → 明确可捕获的错误（datasource 的 DataUnavailable 或 ValueError），不得静默返回空图。"""
    from ptrade.datasource import DataUnavailable

    with pytest.raises((ValueError, DataUnavailable)) as excinfo:
        C.chart_payload(offline_cfg, offline_ds, "999999", offline_ds.latest_trade_date())
    assert "999999" in str(excinfo.value)


# =========================================================================== #
# 5. build_chart（离线自包含 HTML）
# =========================================================================== #
def test_payload_amount_series_present_and_aligned(offline_cfg, offline_ds):
    """用户明确要求看「成交额」：每个周期的 amount 序列必须非空且与 dates 等长。"""
    for code in (BIG, ETF):
        payload = C.chart_payload(offline_cfg, offline_ds, code, offline_ds.latest_trade_date())
        for period in C.PERIODS:
            frame = payload["periods"][period]
            assert frame["amount"], f"{code}/{period} amount 为空"
            assert len(frame["amount"]) == len(frame["dates"]), f"{code}/{period} 长度不一致"
            assert len(frame["volume"]) == len(frame["dates"])
            finite = [x for x in frame["amount"] if x is not None]
            assert finite, f"{code}/{period} amount 全为 None"
            assert all(x > 0 for x in finite), f"{code}/{period} 成交额应为正值"


def test_build_chart_renders_amount_panel(tmp_path, offline_cfg, offline_ds):
    """成交额面板必须真的渲染出来（含「成交额」字样、右轴亿元格式、amount 系列接线）。"""
    out = tmp_path / "amount.html"
    path = C.build_chart(
        offline_cfg, offline_ds, ETF, offline_ds.latest_trade_date(), out=str(out)
    )
    html = Path(path).read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", html)
    # 只取本页 yAxis 块：内联 ECharts 源码里也有 position:"right" 等干扰项
    yaxis_block = flat.split("const yAxis = [", 1)[1].split("];", 1)[0]

    checks = {
        "页面含「成交额」": "成交额" in flat,
        "独立面板标题④": "④ 成交额（亿元）" in flat,
        "成交额挂独立 grid/yAxis(3)": bool(
            re.search(r'name:"成交额".{0,220}xAxisIndex:3, yAxisIndex:3', flat)
        ),
        "成交量留在 grid/yAxis(2)": bool(
            re.search(r'name:"成交量".{0,260}xAxisIndex:2, yAxisIndex:2', flat)
        ),
        "上一轮双轴标题已删除": "成交量 / 成交额（右轴，亿元）" not in flat,
        "本页 yAxis 无右轴副轴": "position:" not in yaxis_block,
        "本页恰 6 个独立 yAxis": yaxis_block.count("gridIndex:") == 6,
        "成交额轴为亿元刻度": bool(re.search(r'gridIndex:3.{0,160}toFixed\(0\) \+ "亿"', flat)),
        "成交额引用 payload.amount": "d.amount" in flat,
        "无外链引用": C.no_external_refs(html),
    }
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}


def test_kline_panel_has_no_boll_overlay(tmp_path, offline_cfg, offline_ds):
    """task-8 核心诉求：BOLL 三个系列**不得**叠加在主图 gridIndex/xAxisIndex 0 上。"""
    out = tmp_path / "no_boll_overlay.html"
    path = C.build_chart(
        offline_cfg, offline_ds, BIG, offline_ds.latest_trade_date(), out=str(out)
    )
    html = Path(path).read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", html)

    checks = {}
    for name in ("BOLL上轨", "BOLL中轨", "BOLL下轨"):
        checks[f"{name} 存在"] = f'name:"{name}"' in flat
        checks[f"{name} 在独立 BOLL 面板"] = bool(
            re.search(rf'name:"{name}".{{0,220}}xAxisIndex:1, yAxisIndex:1', flat)
        )
        checks[f"{name} 不在主图 grid0"] = not bool(
            re.search(rf'name:"{name}".{{0,220}}(xAxisIndex:0|gridIndex:0)', flat)
        )
    checks["K线在主图"] = bool(re.search(r'name:"K线".{0,160}xAxisIndex:0, yAxisIndex:0', flat))
    for n in (12, 24, 48, 96):
        checks[f"MA{n} 在主图"] = bool(
            re.search(rf'name:"MA{n}".{{0,180}}xAxisIndex:0, yAxisIndex:0', flat)
        )
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}


def test_seven_panels_explicit_titles_and_linked_axes(tmp_path, offline_cfg, offline_ds):
    """7 个面板标题显式 + 6 个图表面板 X 轴联动 + 默认显示最近 ~120 根。"""
    out = tmp_path / "panels.html"
    path = C.build_chart(
        offline_cfg, offline_ds, MID, offline_ds.latest_trade_date(), out=str(out)
    )
    html = Path(path).read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", html)

    # 只取本页 title:[...] 块（内联 ECharts 里也有 text:"loading" 等干扰项）
    title_block = flat.split("title:[", 1)[1].split("], legend:", 1)[0]
    titles = re.findall(r'text:"([^"]+)"', title_block)
    assert len(titles) == 6, f"应有 6 个图表标题，实际 {len(titles)}: {titles}"
    for expected in (
        "① K线 + MA12/24/48/96",
        "② BOLL(20,2) 上/中/下轨",
        "③ 成交量（股）+ VOL MA5/MA10",
        "④ 成交额（亿元）",
        "⑤ MACD(12,26,9)",
        "⑥ SAR(0.02/0.02/0.20)",
    ):
        assert expected in titles, f"缺少面板标题: {expected}（实际 {titles}）"
    assert "⑦ 分析区（CANSLIM 七因子）" in html, "第 7 格分析区标题必须显式"

    grids = re.findall(r'\{left:66, right:26, top:"[^"]+", height:"[^"]+"\}', flat)
    assert len(grids) == 6, f"应有 6 个图表 grid，实际 {len(grids)}: {grids}"

    assert "xAxisIndex:[0,1,2,3,4,5]" in flat, "6 个面板必须 X 轴联动"
    assert re.search(r"const startPct = d0 > 120 \?", flat), "默认显示最近 ~120 根"
    assert "start:startPct, end:100" in flat
    assert C.no_external_refs(html)


def test_build_chart_selfcontained(tmp_path, offline_cfg, offline_ds):
    asof = offline_ds.latest_trade_date()
    out = tmp_path / "chart_test.html"
    path = C.build_chart(offline_cfg, offline_ds, BIG, asof, out=str(out))

    assert Path(path).is_absolute() and Path(path).is_file()
    html = Path(path).read_text(encoding="utf-8")
    assert html.lower().startswith("<!doctype html") and "<html" in html.lower()
    assert 'charset="utf-8"' in html
    assert "MA12" in html and "ma12" in html.lower()
    assert C.VENDOR_ECHARTS.read_text(encoding="utf-8")[:200] in html, "ECharts 必须内联"
    assert len(html) > 900_000

    refs = C.external_refs(html)
    assert refs == [], f"不得有外链引用: {refs}"
    assert C.no_external_refs(html) is True
    # 「无 http 字样」不是判据：ECharts 内含许可注释/命名空间 URI
    assert re.search(r"https?://", html), "ECharts 内的许可/命名空间 URL 属于正常内容"

    # 四个面板 + 三周期切换 + 分析区
    for token in ('id="chart"', 'id="btn-daily"', 'id="btn-weekly"', 'id="btn-monthly"',
                  "candlestick", "MACD", "SAR", "BOLL", "成交量"):
        assert token in html, token
    assert re.search(r'<div class="(analysis|warn)"', html), "必须有分析区"
    for period in C.PERIODS:
        assert f'"{period}":' in html, f"{period} 数据必须内联"


def test_build_chart_default_output_path(offline_cfg, offline_ds):
    asof = offline_ds.latest_trade_date()
    path = C.build_chart(offline_cfg, offline_ds, MID, asof, period="weekly")
    expected = Path(offline_cfg.root) / "reports" / f"chart_{MID}_{asof}.html"
    assert Path(path) == expected.resolve() or Path(path).samefile(expected)
    assert Path(path).is_file()
    html = Path(path).read_text(encoding="utf-8")
    assert 'window.__INITIAL_PERIOD__ = CHART_DATA.initial_period' in html


def test_build_chart_rejects_unknown_period(offline_cfg, offline_ds):
    with pytest.raises(ValueError):
        C.build_chart(offline_cfg, offline_ds, MID, offline_ds.latest_trade_date(), period="yearly")


def test_build_chart_accepts_external_analysis(tmp_path, offline_cfg, offline_ds):
    analysis = {
        "ok": True,
        "total": 88.5,
        "signal": "buy",
        "factors": {"C": 90, "A": 80, "N": 70, "S": 60, "L": 85, "I": 75, "M": 65},
        "reasons": ["当季净利润同比 +82.5%，且较上季 +31.0% 加速"],
        "risks": ["相对强度不足"],
        "recommend": {"entry": 100.0, "stop_loss": 93.0, "take_profit": 125.0},
    }
    out = tmp_path / "external.html"
    path = C.build_chart(
        offline_cfg, offline_ds, MID, offline_ds.latest_trade_date(),
        out=str(out), analysis=analysis,
    )
    html = Path(path).read_text(encoding="utf-8")
    assert "88.5" in html and "买入" in html
    assert "93.000" in html and "125.000" in html
    assert "当季净利润同比 +82.5%" in html


def test_build_chart_degrades_when_analysis_fails(tmp_path, offline_cfg, offline_ds, monkeypatch):
    """策略不可用时分析区降级为明确提示，绝不崩栈。"""
    monkeypatch.setattr(C, "_score_analysis", lambda *a, **k: {"ok": False, "error": "strategy 数据缺失"})
    out = tmp_path / "degraded.html"
    path = C.build_chart(offline_cfg, offline_ds, MID, offline_ds.latest_trade_date(), out=str(out))
    html = Path(path).read_text(encoding="utf-8")
    assert "⑦ 分析区（CANSLIM 七因子）" in html, "第 7 格标题仍需在位"
    assert "数据缺失" in html and "strategy 数据缺失" in html
    assert C.no_external_refs(html)


def test_analysis_from_strategy_is_rendered(offline_cfg, offline_ds):
    """真跑 strategy（若已就绪）：七因子齐全时分析区必须渲染出总分与信号。"""
    analysis = C._score_analysis(offline_cfg, offline_ds, BIG, offline_ds.latest_trade_date())
    assert analysis.get("ok") is True, analysis.get("error")
    assert analysis["total"] is not None
    assert set(analysis["factors"]) >= {"C", "A", "N", "S", "L", "I", "M"}
    html = C._analysis_html(analysis)
    assert "总分" in html
    assert "止损" in html and "止盈" in html
