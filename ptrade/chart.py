"""ptrade/chart.py — K 线图：指标纯函数 + 周/月线重采样 + 离线自包含 HTML（task-7）。

设计原则
--------
* **纯函数指标**：``ema`` / ``macd`` / ``boll`` / ``sar`` / ``moving_average`` / ``vol_ma``
  输入 Series → 输出 Series/DataFrame，无 IO，可独立单测。
* **重采样**：``resample_ohlc(df, "weekly"|"monthly")``，口径固定
  ``open=first, high=max, low=min, close=last, volume/amount=sum``，ISO 周 + 自然月。
* **载荷**：``chart_payload()`` 只产出可 JSON 化数据（严格 JSON：``NaN/inf → None``）。
* **渲染**：``build_chart()`` 生成**单个自包含 HTML**（ECharts 源码内联、数据内联、
  三周期页内切换、分析区），默认 ``<root>/reports/chart_<code>_<asof>.html``。

依赖限制：只用 ``pandas``（ECharts 为 vendored 静态资源，不计入 Python 依赖）。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from .config import AppConfig
from .datasource import DataSource

__all__ = [
    "ema",
    "moving_average",
    "macd",
    "boll",
    "sar",
    "vol_ma",
    "add_indicators",
    "resample_ohlc",
    "chart_payload",
    "build_chart",
    "MA_PERIODS",
    "VOL_MA_PERIODS",
]

MA_PERIODS: tuple[int, ...] = (12, 24, 48, 96)
VOL_MA_PERIODS: tuple[int, ...] = (5, 10)
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BOLL_N, BOLL_K = 20, 2.0
SAR_AF_START, SAR_AF_STEP, SAR_AF_MAX = 0.02, 0.02, 0.20
PERIODS: tuple[str, ...] = ("daily", "weekly", "monthly")
PERIOD_LABEL = {"daily": "日线", "weekly": "周线", "monthly": "月线"}
VENDOR_ECHARTS = Path(__file__).resolve().parent / "vendor" / "echarts.min.js"

#: 取多少自然日的历史（保证 MA96 / BOLL20 / SAR 有足够预热）
LOOKBACK_DAYS = 730


# =========================================================================== #
# 指标纯函数
# =========================================================================== #
def ema(series: pd.Series, n: int) -> pd.Series:
    """指数移动平均：``alpha = 2/(n+1)``，首值取第一个观测值（``adjust=False``）。"""
    return series.astype(float).ewm(span=int(n), adjust=False).mean()


def moving_average(close: pd.Series, n: int) -> pd.Series:
    """简单移动平均 ``MA(n) = close.rolling(n).mean()``。"""
    return close.astype(float).rolling(int(n)).mean()


def macd(
    close: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> pd.DataFrame:
    """MACD(12,26,9)：``DIF = EMA12 - EMA26``；``DEA = EMA(DIF,9)``；``柱 = 2×(DIF-DEA)``。"""
    close = close.astype(float)
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": 2.0 * (dif - dea)})


def boll(close: pd.Series, n: int = BOLL_N, k: float = BOLL_K) -> pd.DataFrame:
    """BOLL(20,2)：``MID = MA20``，``UPPER/LOWER = MID ± k×STD20``。

    **STD 用总体标准差 ``ddof=0``**（通达信/同花顺口径）。
    """
    close = close.astype(float)
    mid = close.rolling(int(n)).mean()
    std = close.rolling(int(n)).std(ddof=0)
    return pd.DataFrame(
        {"mid": mid, "upper": mid + float(k) * std, "lower": mid - float(k) * std}
    )


def vol_ma(volume: pd.Series, n: int) -> pd.Series:
    """成交量均线 ``MA(n)``。"""
    return volume.astype(float).rolling(int(n)).mean()


def sar(
    high: pd.Series,
    low: pd.Series,
    af_start: float = SAR_AF_START,
    af_step: float = SAR_AF_STEP,
    af_max: float = SAR_AF_MAX,
) -> pd.DataFrame:
    """抛物线 SAR（Wilder）。返回 ``{"sar", "trend"}``，``trend``：``1`` 多头 / ``-1`` 空头。

    规则：``SAR = SAR_prev + AF × (EP - SAR_prev)``；创新高/新低则更新 ``EP`` 且
    ``AF += af_step``（上限 ``af_max``）；``SAR`` 被前两日极值夹紧后若被当日价格穿越，
    则反转趋势、``AF`` 重置为 ``af_start``。纯函数，不做任何 IO。
    """
    highs = [float(x) for x in high]
    lows = [float(x) for x in low]
    n = len(highs)
    if n == 0:
        return pd.DataFrame({"sar": [], "trend": []})
    if n == 1:
        return pd.DataFrame({"sar": [lows[0]], "trend": [1]})

    af = float(af_start)
    trend = 1 if (highs[1] >= highs[0] and lows[1] >= lows[0]) else -1
    if highs[1] == highs[0] and lows[1] == lows[0]:
        trend = 1
    if trend == 1:
        ep = max(highs[0], highs[1])
        value = min(lows[0], lows[1])
    else:
        ep = min(lows[0], lows[1])
        value = max(highs[0], highs[1])

    out_sar = [0.0] * n
    out_trend = [0] * n
    out_sar[0] = value
    out_trend[0] = trend

    for i in range(1, n):
        cur = value + af * (ep - value)
        if trend == 1:
            cur = min(cur, lows[i - 1])
            if i >= 2:
                cur = min(cur, lows[i - 2])
            if lows[i] < cur:  # 反转 → 空头
                trend = -1
                cur = ep
                ep = lows[i]
                af = float(af_start)
            elif highs[i] > ep:
                ep = highs[i]
                af = min(af + float(af_step), float(af_max))
        else:
            cur = max(cur, highs[i - 1])
            if i >= 2:
                cur = max(cur, highs[i - 2])
            if highs[i] > cur:  # 反转 → 多头
                trend = 1
                cur = ep
                ep = highs[i]
                af = float(af_start)
            elif lows[i] < ep:
                ep = lows[i]
                af = min(af + float(af_step), float(af_max))
        value = cur
        out_sar[i] = cur
        out_trend[i] = trend

    return pd.DataFrame({"sar": out_sar, "trend": out_trend})


# =========================================================================== #
# 组合指标 / 重采样
# =========================================================================== #
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """给 OHLCV 序列附加全部指标列（MA12/24/48/96、MACD、BOLL、SAR、量均线、涨跌色）。"""
    out = df.copy().reset_index(drop=True)
    if out.empty:
        return out
    close = out["close"].astype(float)

    for n in MA_PERIODS:
        out[f"ma{n}"] = moving_average(close, n)

    macd_frame = macd(close)
    out["macd_dif"] = macd_frame["dif"]
    out["macd_dea"] = macd_frame["dea"]
    out["macd_hist"] = macd_frame["hist"]

    boll_frame = boll(close)
    out["boll_mid"] = boll_frame["mid"]
    out["boll_upper"] = boll_frame["upper"]
    out["boll_lower"] = boll_frame["lower"]

    sar_frame = sar(out["high"], out["low"])
    out["sar"] = sar_frame["sar"]
    out["sar_trend"] = sar_frame["trend"]

    volume = out["volume"].astype(float)
    for n in VOL_MA_PERIODS:
        out[f"vol_ma{n}"] = vol_ma(volume, n)
    # 收 ≥ 开 → 红（上涨），否则绿
    out["bar_up"] = (out["close"].astype(float) >= out["open"].astype(float)).astype(bool)
    return out


def resample_ohlc(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """日线 → 周线/月线。``period ∈ {"daily","weekly","monthly"}``。

    聚合口径（固定）：``open=首, high=max, low=min, close=末, volume=sum, amount=sum``；
    周期键用 **ISO 周（年-W周号）** 与 **自然月（年-月）**；不足一个完整周期也保留。
    """
    if period == "daily":
        return df.copy().reset_index(drop=True)
    if period not in ("weekly", "monthly"):
        raise ValueError(f"未知周期: {period!r}（可选 daily/weekly/monthly）")
    if df.empty:
        return df.copy().reset_index(drop=True)

    work = df.copy().reset_index(drop=True)
    stamps = pd.to_datetime(work["date"].astype(str), errors="coerce")
    if period == "weekly":
        iso = stamps.dt.isocalendar()
        keys = (
            iso["year"].astype("Int64").astype(str)
            + "-W"
            + iso["week"].astype("Int64").astype(str).str.zfill(2)
        )
    else:
        keys = stamps.dt.strftime("%Y-%m")

    grouped = work.groupby(keys, sort=True)
    agg = grouped.agg(
        date=("date", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    )
    agg = agg.reset_index(drop=True)
    agg["pct_chg"] = agg["close"].astype(float).pct_change() * 100.0
    return agg


# =========================================================================== #
# 载荷
# =========================================================================== #
def _finite(value: Any) -> float | None:
    """转 float；``NaN`` / ``inf`` / 非法 → ``None``（严格 JSON 安全）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _num_list(series: pd.Series, digits: int = 4) -> list:
    return [
        None if (v := _finite(x)) is None else round(v, digits) for x in series.tolist()
    ]


def _frame_payload(frame: pd.DataFrame) -> dict:
    """把一帧（含指标）转成纯 JSON 数据（ECharts 友好）。"""
    return {
        "dates": [str(x)[:10] for x in frame["date"].tolist()],
        # ECharts candlestick 约定顺序：[open, close, low, high]
        "ohlc": [
            [
                _finite(o),
                _finite(c),
                _finite(l),
                _finite(h),
            ]
            for o, c, l, h in zip(frame["open"], frame["close"], frame["low"], frame["high"])
        ],
        "pct_chg": _num_list(frame["pct_chg"]) if "pct_chg" in frame else [],
        "volume": _num_list(frame["volume"], 2),
        "amount": _num_list(frame["amount"], 2),
        "bar_up": [bool(x) for x in frame["bar_up"].tolist()],
        "ma": {str(n): _num_list(frame[f"ma{n}"]) for n in MA_PERIODS},
        "vol_ma": {str(n): _num_list(frame[f"vol_ma{n}"], 2) for n in VOL_MA_PERIODS},
        "boll": {
            "mid": _num_list(frame["boll_mid"]),
            "upper": _num_list(frame["boll_upper"]),
            "lower": _num_list(frame["boll_lower"]),
        },
        "macd": {
            "dif": _num_list(frame["macd_dif"]),
            "dea": _num_list(frame["macd_dea"]),
            "hist": _num_list(frame["macd_hist"]),
        },
        "sar": {
            "value": _num_list(frame["sar"]),
            "trend": [int(x) for x in frame["sar_trend"].tolist()],
        },
    }


def _start_of(asof: str, days: int = LOOKBACK_DAYS) -> str:
    from datetime import date, timedelta

    try:
        start = date.fromisoformat(str(asof)[:10]) - timedelta(days=int(days))
    except ValueError:
        return "1990-01-01"
    return start.isoformat()


def chart_payload(
    cfg: AppConfig,
    ds: DataSource,
    code: str,
    asof: str,
    use_qfq: bool = True,
) -> dict:
    """三周期（日/周/月）K 线数据 + 全部指标，纯 JSON 可序列化。

    ``use_qfq=True``（默认）→ 前复权价；``False`` → 不复权真实价（``unadjust=True``）。
    """
    asof = str(asof)[:10]
    start = _start_of(asof)
    if use_qfq:
        daily = ds.daily(code, start, asof, adjust="qfq")
        adjust_label = "前复权"
    else:
        daily = ds.daily(code, start, asof, adjust="", unadjust=True)
        adjust_label = "不复权"

    if daily is None or len(daily) == 0:
        raise ValueError(f"{code} 在 {start} ~ {asof} 无日线数据")

    quote: dict = {}
    try:
        quote = dict(ds.quote(code, asof) or {})
    except Exception:
        quote = {}

    periods: dict[str, dict] = {}
    for period in PERIODS:
        frame = resample_ohlc(daily, period)
        enriched = add_indicators(frame)
        periods[period] = _frame_payload(enriched)

    daily_dates = periods["daily"]["dates"]
    return {
        "code": str(code),
        "name": str(quote.get("name") or code),
        "asof": asof,
        "adjust": adjust_label,
        "use_qfq": bool(use_qfq),
        "latest": {
            "date": daily_dates[-1] if daily_dates else asof,
            "close": _finite(daily["close"].iloc[-1]),
            "pct_chg": _finite(daily["pct_chg"].iloc[-1]) if "pct_chg" in daily else None,
            "prev_close": _finite(quote.get("prev_close")),
            "limit_up": _finite(quote.get("limit_up")),
            "limit_down": _finite(quote.get("limit_down")),
            "suspended": bool(quote.get("suspended")) if quote else False,
        },
        "periods": periods,
        "meta": {
            "window_start": daily_dates[0] if daily_dates else start,
            "window_end": daily_dates[-1] if daily_dates else asof,
            "bars": {p: len(periods[p]["dates"]) for p in PERIODS},
            "ma_periods": list(MA_PERIODS),
            "vol_ma_periods": list(VOL_MA_PERIODS),
            "macd": [MACD_FAST, MACD_SLOW, MACD_SIGNAL],
            "boll": [BOLL_N, BOLL_K],
            "sar": [SAR_AF_START, SAR_AF_STEP, SAR_AF_MAX],
        },
    }


# =========================================================================== #
# 分析区
# =========================================================================== #
def _score_analysis(cfg: AppConfig, ds: DataSource, code: str, asof: str) -> dict:
    """只读调用 ``ptrade.strategy``；任何失败都降级为「数据缺失」提示，绝不抛异常。"""
    try:
        from . import strategy  # 延迟导入：strategy 未就绪时不影响画图
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"strategy 模块不可用：{exc}"}

    try:
        scorer = strategy.CanslimScorer(cfg, ds, offline=True)
        result = scorer.score(code, asof)
        factors = {str(f.key): _finite(f.score) for f in result.factors}
        payload: dict = {
            "ok": True,
            "total": _finite(result.total),
            "signal": str(result.signal),
            "factors": factors,
            "notes": {str(f.key): str(f.note) for f in result.factors},
            "reasons": [str(r) for r in result.reasons],
            "risks": [str(r) for r in result.risks],
        }
        try:
            recs = strategy.recommend([result], cfg)
            payload["recommend"] = recs[0] if recs else None
        except Exception as exc:  # noqa: BLE001
            payload["recommend"] = None
            payload["recommend_error"] = str(exc)
        return payload
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"打分失败：{exc}"}


def _factor_bars(factors: dict) -> str:
    if not factors:
        return ""
    cells = []
    for key in ("C", "A", "N", "S", "L", "I", "M"):
        score = factors.get(key)
        if score is None:
            cells.append(f'<div class="f"><b>{key}</b><span class="na">缺失</span></div>')
            continue
        width = max(0.0, min(100.0, float(score)))
        cells.append(
            f'<div class="f"><b>{key}</b><span class="v">{width:.0f}</span>'
            f'<i style="width:{width:.0f}%"></i></div>'
        )
    return '<div class="factors">' + "".join(cells) + "</div>"


def _analysis_html(analysis: dict | None) -> str:
    heading = '<h3 class="panel-title">⑦ 分析区（CANSLIM 七因子）</h3>'
    if not analysis:
        return f'<div class="analysis">{heading}<div class="warn">未提供分析数据</div></div>'
    if not analysis.get("ok"):
        return (
            f'<div class="analysis">{heading}<div class="warn">数据缺失 —— '
            f'{_escape(str(analysis.get("error") or "未知原因"))}</div></div>'
        )
    total = analysis.get("total")
    signal = str(analysis.get("signal") or "-")
    signal_cn = {"buy": "买入", "watch": "观察", "avoid": "回避", "short": "做空"}.get(
        signal, signal
    )
    rec = analysis.get("recommend") or {}
    entry = _fmt(rec.get("entry"))
    stop = _fmt(rec.get("stop_loss"))
    take = _fmt(rec.get("take_profit"))
    reasons = "".join(f"<li>{_escape(str(r))}</li>" for r in (analysis.get("reasons") or []))
    risks = "".join(f"<li>{_escape(str(r))}</li>" for r in (analysis.get("risks") or []))
    return f"""
<div class="analysis">
  {heading}
  <div class="score">
    <div class="total">{_fmt(total)}<small>总分</small></div>
    <div class="signal s-{_escape(signal)}">{_escape(signal_cn)}</div>
  </div>
  {_factor_bars(analysis.get("factors") or {})}
  <div class="plan">
    <span>建议入场 <b>{entry}</b></span>
    <span>止损 <b class="down">{stop}</b></span>
    <span>止盈 <b class="up">{take}</b></span>
  </div>
  <div class="lists">
    <div><h4>理由</h4><ul>{reasons or "<li>—</li>"}</ul></div>
    <div><h4>风险</h4><ul>{risks or "<li>—</li>"}</ul></div>
  </div>
</div>"""


def _fmt(value: Any) -> str:
    num = _finite(value)
    return "—" if num is None else f"{num:.3f}"


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# =========================================================================== #
# 渲染
# =========================================================================== #
_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light; }
  body { margin:0; padding:16px 18px 28px; font:13px/1.5 "Microsoft YaHei",-apple-system,Segoe UI,Arial,sans-serif;
         background:#f7f8fa; color:#1f2329; }
  h1 { font-size:18px; margin:0 0 6px; }
  .infobar { display:flex; flex-wrap:wrap; gap:14px; align-items:baseline; margin-bottom:10px;
             padding:10px 14px; background:#fff; border:1px solid #e5e6eb; border-radius:8px; }
  .infobar .code { font-size:16px; font-weight:700; }
  .infobar .price { font-size:20px; font-weight:700; }
  .up { color:#ef232a; } .down { color:#14b143; }
  .tag { padding:1px 8px; border-radius:10px; background:#f0f2f5; color:#4e5969; font-size:12px; }
  .toolbar { display:flex; gap:8px; margin:10px 0; }
  .toolbar button { padding:5px 14px; border:1px solid #d9d9d9; background:#fff; border-radius:6px;
                    cursor:pointer; font-size:13px; }
  .toolbar button.active { background:#1f6feb; border-color:#1f6feb; color:#fff; font-weight:600; }
  #chart { width:100%; height:1180px; background:#fff; border:1px solid #e5e6eb; border-radius:8px; }
  .analysis { margin-top:14px; padding:14px 16px; background:#fff; border:1px solid #e5e6eb; border-radius:8px; }
  .analysis .score { display:flex; align-items:center; gap:16px; margin-bottom:10px; }
  .panel-title { margin:0 0 8px; font-size:13px; color:#4e5969; font-weight:600; }
  .analysis .total { font-size:30px; font-weight:700; }
  .analysis .total small { font-size:12px; font-weight:400; color:#86909c; margin-left:6px; }
  .analysis .signal { padding:3px 12px; border-radius:12px; font-weight:600; background:#f0f2f5; }
  .analysis .signal.s-buy { background:#ffece8; color:#ef232a; }
  .analysis .signal.s-short { background:#e8ffea; color:#14b143; }
  .analysis .signal.s-watch { background:#fff7e8; color:#d25f00; }
  .factors { display:flex; gap:10px; flex-wrap:wrap; margin:8px 0 12px; }
  .factors .f { min-width:74px; border:1px solid #e5e6eb; border-radius:6px; padding:6px 8px; }
  .factors .f b { margin-right:6px; } .factors .f .v { font-weight:600; }
  .factors .f i { display:block; height:4px; margin-top:5px; background:#1f6feb; border-radius:2px; }
  .factors .f .na { color:#86909c; }
  .plan { display:flex; gap:22px; margin-bottom:10px; }
  .lists { display:flex; gap:26px; }
  .lists ul { margin:4px 0 0; padding-left:18px; }
  .lists h4 { margin:0; font-size:13px; color:#4e5969; }
  .warn { padding:12px 14px; background:#fff7e8; border:1px solid #ffd591; border-radius:8px; margin-top:14px; }
  footer { margin-top:12px; color:#86909c; font-size:12px; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="infobar">
  <span class="code">__CODE__</span>
  <span>__NAME__</span>
  <span class="price __DIR__">__PRICE__</span>
  <span class="__DIR__">__PCT__</span>
  <span class="tag">__ADJUST__</span>
  <span class="tag">asof __ASOF__</span>
  <span class="tag">区间 __WIN_START__ ~ __WIN_END__</span>
  <span class="tag">日线 __BARS_DAILY__ 根 / 周线 __BARS_WEEKLY__ / 月线 __BARS_MONTHLY__</span>
</div>
<div class="toolbar">
  <button id="btn-daily" class="active" onclick="switchPeriod('daily')">日线</button>
  <button id="btn-weekly" onclick="switchPeriod('weekly')">周线</button>
  <button id="btn-monthly" onclick="switchPeriod('monthly')">月线</button>
  <span class="tag" style="align-self:center">① K线+MA12/24/48/96 · ② BOLL(20,2) · ③ 成交量(股,MA5/MA10) · ④ 成交额(亿元) · ⑤ MACD(12,26,9) · ⑥ SAR(0.02/0.02/0.20) · ⑦ CANSLIM 分析</span>
</div>
<div id="chart"></div>
__ANALYSIS__
<footer>数据来源：本地夹具/缓存（offline，无外链） · 指标由 pandas 纯函数计算 · 仅供研究/模拟，非投资建议</footer>
<script>__ECHARTS__</script>
<script>const CHART_DATA = __DATA__;</script>
<script>__APP_JS__</script>
</body>
</html>
"""

_APP_JS = r"""
const LABEL = {daily:"日线", weekly:"周线", monthly:"月线"};
const UP = "#ef232a", DOWN = "#14b143";
let active = window.__INITIAL_PERIOD__ || "daily";
const chart = echarts.init(document.getElementById("chart"), null, {renderer:"canvas"});

function num(v) { return (v === null || v === undefined) ? "-" : v; }

function buildOption(period) {
  const d = CHART_DATA.periods[period];
  const d0 = d.dates.length;
  /* 默认只显示最近 ~120 根（可缩放/平移） */
  const startPct = d0 > 120 ? (1 - 120 / d0) * 100 : 0;
  /* 6 个图表面板：K线 / BOLL / 成交量 / 成交额 / MACD / SAR（第 7 格是下方 HTML 分析区） */
  const grids = [
    {left:66, right:26, top:"3%",  height:"21%"},
    {left:66, right:26, top:"29%", height:"11%"},
    {left:66, right:26, top:"45%", height:"9%"},
    {left:66, right:26, top:"58%", height:"9%"},
    {left:66, right:26, top:"71%", height:"9%"},
    {left:66, right:26, top:"84%", height:"8%"}
  ];
  const xAxis = grids.map(function (g, i) {
    return {
      type:"category", gridIndex:i, data:d.dates, boundaryGap:true,
      axisLine:{onZero:false, lineStyle:{color:"#c9cdd4"}},
      axisTick:{show:i === 5}, axisLabel:{show:i === 5, color:"#86909c"},
      splitLine:{show:false}, min:"dataMin", max:"dataMax",
      axisPointer:{label:{show:i === 0}}
    };
  });
  const yAxis = [
    {scale:true, gridIndex:0, splitNumber:5, axisLabel:{color:"#86909c"}, splitLine:{lineStyle:{color:"#f2f3f5"}}},
    {scale:true, gridIndex:1, splitNumber:3, axisLabel:{color:"#86909c"}, splitLine:{lineStyle:{color:"#f2f3f5"}}},
    {gridIndex:2, splitNumber:2, axisLabel:{show:false}, splitLine:{show:false}},
    /* 成交额独立面板：亿元刻度 */
    {gridIndex:3, splitNumber:3, splitLine:{show:false},
     axisLabel:{color:"#1f6feb", formatter:function (v) { return (v / 1e8).toFixed(0) + "亿"; }}},
    {gridIndex:4, splitNumber:2, axisLabel:{show:false}, splitLine:{show:false}},
    {gridIndex:5, splitNumber:2, axisLabel:{show:false}, splitLine:{show:false}}
  ];
  const series = [
    /* ① K线面板：只放 K线 + MA12/24/48/96（BOLL 已移到独立面板） */
    {name:"K线", type:"candlestick", data:d.ohlc, xAxisIndex:0, yAxisIndex:0,
     itemStyle:{color:UP, color0:DOWN, borderColor:UP, borderColor0:DOWN}},
    {name:"MA12", type:"line", data:d.ma["12"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#1f2329"}},
    {name:"MA24", type:"line", data:d.ma["24"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#f2c200"}},
    {name:"MA48", type:"line", data:d.ma["48"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#ff7ac6"}},
    {name:"MA96", type:"line", data:d.ma["96"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#14b143"}},
    /* ② BOLL(20,2) 独立面板 */
    {name:"BOLL上轨", type:"line", data:d.boll.upper, xAxisIndex:1, yAxisIndex:1, showSymbol:false,
     lineStyle:{width:1, color:"#8c8c8c", type:"dashed"}},
    {name:"BOLL中轨", type:"line", data:d.boll.mid, xAxisIndex:1, yAxisIndex:1, showSymbol:false,
     lineStyle:{width:1.2, color:"#ff9f43"}},
    {name:"BOLL下轨", type:"line", data:d.boll.lower, xAxisIndex:1, yAxisIndex:1, showSymbol:false,
     lineStyle:{width:1, color:"#8c8c8c", type:"dashed"}},
    /* ③ 成交量面板：只放量 */
    {name:"成交量", type:"bar", data:d.volume.map(function (v, i) {
       return {value:v, itemStyle:{color: d.bar_up[i] ? UP : DOWN}};
     }), xAxisIndex:2, yAxisIndex:2, barWidth:"60%"},
    {name:"VOL MA5", type:"line", data:d.vol_ma["5"], xAxisIndex:2, yAxisIndex:2, showSymbol:false,
     lineStyle:{width:1, color:"#f2c200"}},
    {name:"VOL MA10", type:"line", data:d.vol_ma["10"], xAxisIndex:2, yAxisIndex:2, showSymbol:false,
     lineStyle:{width:1, color:"#1f6feb"}},
    /* ④ 成交额独立面板：亿元刻度，与成交量彻底分家 */
    {name:"成交额", type:"line", data:d.amount, xAxisIndex:3, yAxisIndex:3, showSymbol:false,
     lineStyle:{width:1.2, color:"#0e9f9f"}, itemStyle:{color:"#0e9f9f"},
     areaStyle:{opacity:0.12}, z:3},
    /* ⑤ MACD 面板 */
    {name:"MACD柱", type:"bar", data:d.macd.hist.map(function (v) {
       return {value:v, itemStyle:{color: (v !== null && v >= 0) ? UP : DOWN}};
     }), xAxisIndex:4, yAxisIndex:4, barWidth:"50%"},
    {name:"DIF", type:"line", data:d.macd.dif, xAxisIndex:4, yAxisIndex:4, showSymbol:false,
     lineStyle:{width:1.1, color:"#1f2329"}},
    {name:"DEA", type:"line", data:d.macd.dea, xAxisIndex:4, yAxisIndex:4, showSymbol:false,
     lineStyle:{width:1.1, color:"#f2c200"}},
    /* ⑥ SAR 面板 */
    {name:"SAR", type:"scatter", data:d.sar.value.map(function (v, i) {
       return {value:v, itemStyle:{color: d.sar.trend[i] > 0 ? UP : DOWN}};
     }), xAxisIndex:5, yAxisIndex:5, symbolSize:4}
  ];
  return {
    animation:false,
    backgroundColor:"#fff",
    /* 7 个面板标题全部显式（第 7 格为下方 HTML 分析区） */
    title:[
      {text:"① K线 + MA12/24/48/96", left:70, top:"0.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"② BOLL(20,2) 上/中/下轨", left:70, top:"26.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"③ 成交量（股）+ VOL MA5/MA10", left:70, top:"42.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"④ 成交额（亿元）", left:70, top:"55.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"⑤ MACD(12,26,9)", left:70, top:"68.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"⑥ SAR(0.02/0.02/0.20)", left:70, top:"81.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}}
    ],
    legend:{top:"0.5%", right:26, itemWidth:14, itemHeight:8, textStyle:{fontSize:11},
            data:["K线","MA12","MA24","MA48","MA96"]},
    tooltip:{trigger:"axis", axisPointer:{type:"cross", link:[{xAxisIndex:"all"}]},
      backgroundColor:"rgba(255,255,255,0.96)", borderColor:"#e5e6eb", textStyle:{color:"#1f2329", fontSize:12},
      formatter:function (ps) {
        if (!ps || !ps.length) return "";
        const i = ps[0].dataIndex, dd = CHART_DATA.periods[active];
        const rows = ["<b>" + dd.dates[i] + "</b>"];
        const ohlc = dd.ohlc[i] || [];
        if (ohlc.length === 4) {
          const up = ohlc[1] >= ohlc[0];
          rows.push("开 " + num(ohlc[0]) + " 收 <b style='color:" + (up ? UP : DOWN) + "'>" + num(ohlc[1]) + "</b>");
          rows.push("高 " + num(ohlc[3]) + " 低 " + num(ohlc[2]));
        }
        if (dd.pct_chg && dd.pct_chg[i] !== null) rows.push("涨跌幅 " + num(dd.pct_chg[i]) + "%");
        const vol = dd.volume[i], amt = dd.amount[i];
        if (vol !== null) rows.push("量 " + (vol / 1e4).toFixed(2) + " 万  MA5 " + ((dd.vol_ma["5"][i] || 0) / 1e4).toFixed(2) + " 万  MA10 " + ((dd.vol_ma["10"][i] || 0) / 1e4).toFixed(2) + " 万");
        if (amt !== null) rows.push("成交额 " + (amt / 1e8).toFixed(3) + " 亿");
        rows.push("MA12 " + num(dd.ma["12"][i]) + " / MA24 " + num(dd.ma["24"][i]) + " / MA48 " + num(dd.ma["48"][i]) + " / MA96 " + num(dd.ma["96"][i]));
        rows.push("BOLL " + num(dd.boll.lower[i]) + " / " + num(dd.boll.mid[i]) + " / " + num(dd.boll.upper[i]));
        rows.push("DIF " + num(dd.macd.dif[i]) + " DEA " + num(dd.macd.dea[i]) + " 柱 " + num(dd.macd.hist[i]));
        rows.push("SAR " + num(dd.sar.value[i]) + (dd.sar.trend[i] > 0 ? " (多)" : " (空)"));
        return rows.join("<br>");
      }},
    axisPointer:{link:[{xAxisIndex:"all"}], label:{backgroundColor:"#1f6feb"}},
    grid:grids, xAxis:xAxis, yAxis:yAxis, series:series,
    dataZoom:[
      {type:"inside", xAxisIndex:[0,1,2,3,4,5], start:startPct, end:100},
      {type:"slider", xAxisIndex:[0,1,2,3,4,5], bottom:2, height:16, start:startPct, end:100}
    ]
  };
}

function switchPeriod(period) {
  active = period;
  ["daily","weekly","monthly"].forEach(function (p) {
    const el = document.getElementById("btn-" + p);
    if (el) el.className = (p === period) ? "active" : "";
  });
  chart.setOption(buildOption(period), true);
  window.__ACTIVE_PERIOD__ = period;
}

window.__INITIAL_PERIOD__ = CHART_DATA.initial_period || "daily";
switchPeriod(window.__INITIAL_PERIOD__);
window.addEventListener("resize", function () { chart.resize(); });
"""


def _load_echarts() -> str:
    if not VENDOR_ECHARTS.is_file():
        raise FileNotFoundError(
            f"缺少 vendored ECharts：{VENDOR_ECHARTS}（需从 unpkg 下载 echarts.min.js 入库）"
        )
    return VENDOR_ECHARTS.read_text(encoding="utf-8", errors="replace")


def _render_html(payload: dict, analysis: dict | None, period: str) -> str:
    latest = payload.get("latest") or {}
    close = _finite(latest.get("close"))
    pct = _finite(latest.get("pct_chg"))
    direction = "up" if (pct is not None and pct >= 0) else "down"
    price_text = "—" if close is None else f"{close:.3f}"
    pct_text = "—" if pct is None else f"{pct:+.2f}%"
    meta = payload.get("meta") or {}
    bars = meta.get("bars") or {}

    # 内联 JSON 需防 </script> 提前闭合
    data_json = json.dumps(
        {"periods": payload["periods"], "initial_period": period, "code": payload["code"]},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).replace("</", "<\\/")

    title = f"{payload['code']} {payload['name']} K线图（{payload['adjust']}）"
    html = _PAGE
    for token, value in (
        ("__TITLE__", _escape(title)),
        ("__CODE__", _escape(payload["code"])),
        ("__NAME__", _escape(str(payload.get("name") or ""))),
        ("__PRICE__", price_text),
        ("__PCT__", pct_text),
        ("__DIR__", direction),
        ("__ADJUST__", _escape(str(payload.get("adjust") or ""))),
        ("__ASOF__", _escape(str(payload.get("asof") or ""))),
        ("__WIN_START__", _escape(str(meta.get("window_start") or ""))),
        ("__WIN_END__", _escape(str(meta.get("window_end") or ""))),
        ("__BARS_DAILY__", str(bars.get("daily", 0))),
        ("__BARS_WEEKLY__", str(bars.get("weekly", 0))),
        ("__BARS_MONTHLY__", str(bars.get("monthly", 0))),
        ("__ANALYSIS__", _analysis_html(analysis)),
        ("__ECHARTS__", _load_echarts()),
        ("__DATA__", data_json),
        ("__APP_JS__", _APP_JS),
    ):
        html = html.replace(token, value)
    return html


def build_chart(
    cfg: AppConfig,
    ds: DataSource,
    code: str,
    asof: str,
    period: str = "daily",
    out: str | None = None,
    use_qfq: bool = True,
    analysis: dict | None = None,
) -> str:
    """渲染 K 线图 HTML 并返回**绝对路径**。``period ∈ {"daily","weekly","monthly"}``。"""
    if period not in PERIODS:
        raise ValueError(f"未知周期: {period!r}（可选 {'/'.join(PERIODS)}）")

    payload = chart_payload(cfg, ds, code, asof, use_qfq=use_qfq)
    if analysis is None:
        analysis = _score_analysis(cfg, ds, code, payload["asof"])

    html = _render_html(payload, analysis, period)

    target = out or os.path.join(
        str(cfg.root), "reports", f"chart_{code}_{payload['asof']}.html"
    )
    path = Path(target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return str(path.resolve())


def _strip_script_bodies(html: str) -> str:
    """只剥掉 ``<script>`` 的**内容**，**保留开标签属性**。

    CR-18（HIGH）：早期实现用 ``re.sub(r"<script[^>]*>.*?</script>", "<script></script>")``，
    把 ``src`` 属性一起抹掉了 → 真外链 ``<script src="https://cdn...">`` 在后续正则跑之前就已消失，
    ``no_external_refs()`` 恒为 True（**空守卫**）。必须保留 ``<script ...>`` 开标签。
    """
    import re

    return re.sub(r"(<script\b[^>]*>).*?(</script>)", r"\1\2", html, flags=re.I | re.S)


#: 不产生网络请求的 URL 方案（``data:`` 内联资源 / ``javascript:`` / 页内锚点）
_INTERNAL_URL_PREFIXES = ("data:", "javascript:", "about:", "blob:")

#: 资源引用模式：命中后还要过 :func:`_is_external_url`（排除 data:/javascript:/# ）
_RESOURCE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"<script\b[^>]*\ssrc\s*=\s*[\"']?([^\"'\s>]+)", "script src"),
    (r"<link\b[^>]*\shref\s*=\s*[\"']?([^\"'\s>]+)", "link href"),
    (r"<(?:img|iframe|source|video|audio|embed|object|track)\b[^>]*\s(?:src|data)\s*=\s*[\"']?([^\"'\s>]+)", "media src"),
    (r"<a\b[^>]*\shref\s*=\s*[\"']?([^\"'\s>]+)", "anchor href"),
    (r"@import\s+(?:url\()?\s*[\"']?([^\"')\s;]+)", "@import"),
    (r"url\(\s*[\"']?([^\"')\s]+)", "css url()"),
)

#: JS 里对 http(s) 的显式取数调用（内联库自身的 XMLHttpRequest 实现不算，要有具体 URL）
_JS_NETWORK_PATTERN = (
    r"(?:fetch|XMLHttpRequest|WebSocket|importScripts|EventSource)\s*\(?\s*[\"'](https?://[^\"'\s)]+)"
)


def _is_external_url(url: str) -> bool:
    """是否构成**离线自包含**意义上的外链引用。

    * ``data:`` / ``javascript:`` / ``about:`` / ``blob:`` / ``#`` → **不算**（零请求）
    * 其余（含 ``http(s)://``、协议相对 ``//cdn/...``、以及相对/绝对路径资源）→ **算**
      （单文件产物一旦出现任何 ``src``/``href`` 资源引用就不是自包含）
    """
    text = str(url or "").strip().strip("\"'")
    if not text or text.startswith("#"):
        return False
    low = text.lower()
    return not low.startswith(_INTERNAL_URL_PREFIXES)


def external_refs(html: str) -> list[str]:
    """列出页面里的**外链引用**（空列表 ⇒ 离线自包含）。

    * 只剥 ``<script>`` 的**内容**，保留其开标签属性 → ``<script src="https://...">`` 会被抓到；
    * ``data:`` 图片等内联资源**不误报**；
    * 内联 ECharts 源码里的 ``'<img src='`` 运行时字符串与 Apache 许可/命名空间 URL 不算外链。
    """
    import re

    markup = _strip_script_bodies(html)
    found: list[str] = []
    for pattern, label in _RESOURCE_PATTERNS:
        for match in re.finditer(pattern, markup, re.I | re.S):
            url = match.group(1) if match.groups() else match.group(0)
            if _is_external_url(url):
                found.append(f"{label}: {str(url).strip()[:140]}")
    for match in re.finditer(_JS_NETWORK_PATTERN, html, re.I):
        found.append(f"js network call: {match.group(1).strip()[:140]}")
    return found


def no_external_refs(html: str) -> bool:
    """离线自包含判定：无任何资源外链引用（``<script src>`` / ``<link href>`` / ``<img src>`` /
    ``@import`` / ``url(http…)`` / JS 内 ``fetch("http…")``）。

    **不要**用「HTML 里没有 ``http`` 字样」判定 —— 内联的 ECharts 源码含 6 处 http 字符串
    （Apache 许可注释 + XML 命名空间 URI），不会发起请求，按「无 http 字样」判会误红。
    """
    return not external_refs(html)
