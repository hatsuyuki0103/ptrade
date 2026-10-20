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
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from . import datasource
from . import store
from .config import AppConfig
from .datasource import DataSource, DataUnavailable, NetworkForbiddenInLock

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
    "render_chart",
    "chart_out_path",
    "write_chart",
    "MA_PERIODS",
    "VOL_MA_PERIODS",
    "PERIODS",
    "PERIODS_WITH_INTRADAY",
    "render_intraday",
    "build_intraday",
    "intraday_out_path",
    "legend_groups",
    "legend_budget",
]

MA_PERIODS: tuple[int, ...] = (12, 24, 48, 96)
VOL_MA_PERIODS: tuple[int, ...] = (5, 10)
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BOLL_N, BOLL_K = 20, 2.0
SAR_AF_START, SAR_AF_STEP, SAR_AF_MAX = 0.02, 0.02, 0.20

#: ★ **K 线管线**可选周期 —— **不含 ``minute``**（``resample_ohlc`` 对非日/周/月抛 ValueError）。
#: 分时走**独立入口** :func:`render_intraday`，绝不进这条管线。
PERIODS: tuple[str, ...] = ("daily", "weekly", "monthly")

#: ★ **对外周期清单的单一真源**（webapp 的 ``VALID_PERIODS`` / cli 的 ``--period choices``
#: 都必须从这里导入，不得各写一份 —— 否则「分时」在两个入口的口径迟早漂移）。
PERIODS_WITH_INTRADAY: tuple[str, ...] = PERIODS + ("minute",)

PERIOD_LABEL = {"daily": "日线", "weekly": "周线", "monthly": "月线", "minute": "分时"}
VENDOR_ECHARTS = Path(__file__).resolve().parent / "vendor" / "echarts.min.js"

#: 取多少自然日的历史（保证 MA96 / BOLL20 / SAR 有足够预热）。
#:
#: ★ **单一真源**：值来自 :data:`ptrade.datasource.VIEWER_DAILY_DAYS` —— 查看路径的 asof 解析
#: （``latest_trade_asof._latest_bar_day``）用**同一个跨度**取日线，这样 ``daily()``
#: 的缓存 key（含窗口 ``(start, end)``）**才有命中可能**（ultraqa 实测：同源数据被取两遍）。
#: ⚠️ 读的是**模块属性**（调用时解析），所以改真源会立刻传导过来，便于判据用 monkeypatch 验证。
#:
#: ★★ **fail-safe（有意设计，务必保留）**：若两边窗口对不上（例如取数**锚点**不同、
#: 或将来图的预热需求变了），后果**只是退回 2 次取数（变慢）**，**绝不会出错数据** ——
#: key 不同 ⇒ 各自独立取数 ⇒ 每个调用方拿到的仍是自己那份完整数据。
#: 「赚不到（省不下那一次取数）就退回原状，绝不为了省一次取数去动语义」是这条改动的原则。
#: ⚠️ **已知且有意保留的残余**：两边的**锚点**不同（图用 `asof` = 有数据的那天；
#: asof 解析用 `latest_trade_date()` = 日历末日，可能在**未来**）⇒ 当前**仍是 2 次取数**。
#: 改锚点会砍掉窗口前段的 K 线并改变 MA/BOLL 早期值 ⇒ 明确**不做**（见 `chart_payload` 里的长注释）。
LOOKBACK_DAYS = datasource.VIEWER_DAILY_DAYS

#: 图例分组（**面板序 == yAxis 面板序**）—— 页面图例由 Python 生成后注入，
#: 因此「Python 视图」与「页面实际图例」不可能漂移。``微型K线`` 是 BOLL/SAR 面板的
#: 背景参考 K 线（装饰性、``silent``），仍计入面板 series 数。
LEGEND_BY_PANEL: tuple[tuple[str, ...], ...] = (
    ("K线", "MA12", "MA24", "MA48", "MA96"),
    ("微型K线", "BOLL上轨", "BOLL中轨", "BOLL下轨"),
    ("成交量", "VOL MA5", "VOL MA10"),
    ("成交额",),
    ("MACD柱", "DIF", "DEA"),
    ("微型K线", "SAR"),
)

#: 图例标签的单字符近似宽度（px）：``label_width`` 缺省时按 ``12 × max(len(name))`` 推导，
#: 因此**从根上不可能取 0**（名字非空）。
_LEGEND_LABEL_CHAR_PX = 12


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


def _start_of(asof: str, days: int | None = None) -> str:
    """``asof`` 往前 ``days`` 个自然日；``days=None`` → **调用时**取真源
    :data:`ptrade.datasource.VIEWER_DAILY_DAYS`（而不是导入时冻结的值 ——
    后者会让「改真源」不再传导，是单一真源最隐蔽的失效形态）。
    """
    from datetime import date, timedelta

    if days is None:
        days = datasource.VIEWER_DAILY_DAYS
    try:
        start = date.fromisoformat(str(asof)[:10]) - timedelta(days=int(days))
    except ValueError:
        return "1990-01-01"
    return start.isoformat()


# =========================================================================== #
# 图例（**Python 两层纯函数**，不用 node）
# =========================================================================== #
#: 图例项 → payload 里的数据路径（用于剔除「空系列」，让图例不出现空条目）
_SERIES_SOURCE: dict[str, tuple[str, ...]] = {
    "K线": ("ohlc",),
    "微型K线": ("ohlc",),
    "MA12": ("ma", "12"),
    "MA24": ("ma", "24"),
    "MA48": ("ma", "48"),
    "MA96": ("ma", "96"),
    "BOLL上轨": ("boll", "upper"),
    "BOLL中轨": ("boll", "mid"),
    "BOLL下轨": ("boll", "lower"),
    "成交量": ("volume",),
    "VOL MA5": ("vol_ma", "5"),
    "VOL MA10": ("vol_ma", "10"),
    "成交额": ("amount",),
    "MACD柱": ("macd", "hist"),
    "DIF": ("macd", "dif"),
    "DEA": ("macd", "dea"),
    "SAR": ("sar", "value"),
}


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _has_data(container: Any) -> bool:
    if container is None:
        return False
    if isinstance(container, (list, tuple)):
        return any(item is not None for item in container)
    return True


def legend_groups(payload: dict) -> list[list[str]]:
    """把图例项按 **yAxis 面板序**分组（纯函数，无 IO）。

    返回 ``list[list[str]]``：外层下标 == 面板号（gridIndex 0..5），内层是该面板的 series 名。
    页面图例**由本函数的返回值注入**（见 :func:`_render_html`），所以「Python 视图」与
    「页面实际图例」不可能各自漂移。

    某个项在 ``payload``（日线帧）里**没有任何数据**时会被剔除 —— 否则图例会列出空系列。
    """
    frame = ((payload.get("periods") or {}).get("daily") or {})
    groups: list[list[str]] = []
    for names in LEGEND_BY_PANEL:
        kept = [
            name for name in names
            if _has_data(_dig(frame, _SERIES_SOURCE.get(name, ())))
        ]
        groups.append(kept)
    return groups


def _legend_label_width(group: list[str]) -> int:
    """标签宽度推导：``12 × max(len(name))``（名字非空 → **从根上不可能取 0**）。"""
    longest = max((len(str(name)) for name in group), default=1)
    return _LEGEND_LABEL_CHAR_PX * max(1, longest)


def legend_budget(
    item_width: int = 14,
    item_gap: int = 10,
    label_width: int | None = None,
    nominal_panel_width: int = 900,
) -> bool:
    """图例**单行宽度预算**（纯函数）：每个分组是否都能在一行内排下。

    判据：对每个分组 ``n × (item_width + item_gap + label_width) <= nominal_panel_width``，
    **全部**满足才返回 True。

    * ``label_width=None``（默认）→ 按该分组的 ``12 × max(len(name))`` 推导
      → **从根上不可能取 0**（标签才是真实重叠的驱动项）；
    * 显式传 ``label_width <= 0`` → **ValueError**：``0`` 会让预算恒绿
      （``30 × 24 = 720 <= 900``），等于把守卫拆掉，明令禁止。

    ⚠️ **诚实标注**：这是**近似模型**（ECharts 图例会换行，换行本身不违规；面板宽度与视口相关），
    只用于「防止图例越改越乱」，**不保证**真实浏览器里不重叠。
    """
    if label_width is not None and int(label_width) <= 0:
        raise ValueError(
            "label_width 必须为正：0 会让 legend_budget 恒绿（预算守卫失效），禁止传入"
        )
    nominal = float(nominal_panel_width)
    for group in LEGEND_BY_PANEL:
        if not group:
            continue
        width = int(label_width) if label_width is not None else _legend_label_width(list(group))
        if len(group) * (int(item_width) + int(item_gap) + width) > nominal:
            return False
    return True


def _provisional_flag(ds: DataSource, asof: str) -> bool:
    """``provisional = (asof == 今天) and (now < 15:30)``。

    ``with_today_bar`` **不**贴「未收盘」标签（它会给已收盘日贴错标签），由调用方按此式计算。
    ``datetime`` 是**模块属性** → 测试可注入时钟（test-spec §0.5）；``ds._today`` 同样可注入。
    """
    asof = str(asof)[:10]
    try:
        today = str(ds._today())[:10]
    except Exception:  # noqa: BLE001 - 时钟不可得 → 不贴标签（宁缺勿错）
        return False
    if asof != today:
        return False
    now = datetime.now()
    return (now.hour, now.minute) < (15, 30)


def chart_payload(
    cfg: AppConfig,
    ds: DataSource,
    code: str,
    asof: str,
    use_qfq: bool = True,
    *,
    include_today: bool = False,
) -> dict:
    """三周期（日/周/月）K 线数据 + 全部指标，纯 JSON 可序列化。

    ``use_qfq=True``（默认）→ 前复权价；``False`` → 不复权真实价（``unadjust=True``）。

    ``include_today=True`` → 尝试把**当天实时快照**合成一根 bar（``ds.with_today_bar``）：
    当天 bar 必须在 :func:`add_indicators` **之前**并入 frame（否则 MA/MACD/SAR 各数组长度
    与 ``dates`` 不等），且 :func:`_render_html` 的 ``latest`` 必须从**合成后的** frame 重算
    （否则图头显示昨天收盘价、图上却多一根今天的 bar，自相矛盾）。

    ★ **默认必须是 False**：``chart_payload`` 的既有 10 个调用点（``render_chart`` + 8 处测试）
    都依赖「不加当天 bar」的确定性（夹具窗口 = 历史窗口）。
    """
    asof = str(asof)[:10]
    # ★ 跨度取**真源常量**（调用时解析模块属性，便于判据 monkeypatch 验证单一真源）；
    #   锚点仍是 `asof`（与 `datasource` 的契约注释一致：`daily(code, asof - V, asof)`）。
    #
    # ★★ **锚点必须保持 `asof`（= 有数据的那一天）—— 不要为了共享缓存 key 改成日历末日！**
    #
    #   背景（2026-10-16 实测，Lead 裁决「接受诚实的 2 次取数」）：`daily()` 的缓存 key 是
    #   **整对 `(start, end)`**，所以「图与 asof 解析共用一次取数」要求**跨度与锚点都相同**。
    #   而 asof 解析那一侧（`latest_trade_asof._latest_bar_day`）锚在 ``latest_trade_date()``
    #   （**日历**末日，可能还在**未来**：sina 日历覆盖到 2026-12-31，而行情只到已收盘的最近一天）。
    #   实测两条窗口：图 `(2024-10-09, 2026-10-09)` vs asof 相 `(2024-10-16, 2026-10-16)` ——
    #   **跨度已同源（730）、锚点差 7 天 ⇒ key 仍不同 ⇒ 仍是 2 次取数**。
    #
    #   ★ 把图锚到日历末日「看起来」能让 key 相同、省下那一次取数，**但代价是窗口前移**：
    #     前面被砍掉 (日历末日 − 有数据日) 根（夹具实测 **486 → 481**；生产上日历跑到未来时
    #     可能少几十根）⇒ **改变 MA/BOLL 的早期值** ⇒ 违反本目标的硬约束「不改任何指标/打分数字」。
    #   ⇒ **锚点分歧是已知的、有意的取舍（记入 backlog），不在这里修**。
    #     要么等 `daily()` 的缓存键与窗口解耦（core-builder 已推演：窄请求先到时**不达成** 2→1），
    #     要么由 Lead 在「省一次取数」与「前段根数不变」之间另行裁决。
    start = _start_of(asof, datasource.VIEWER_DAILY_DAYS)
    if use_qfq:
        daily = ds.daily(code, start, asof, adjust="qfq")
        adjust_label = "前复权"
    else:
        daily = ds.daily(code, start, asof, adjust="", unadjust=True)
        adjust_label = "不复权"

    if daily is None or len(daily) == 0:
        raise ValueError(f"{code} 在 {start} ~ {asof} 无日线数据")

    # ---- 当天 bar：**在 add_indicators 之前**并入（指标随之重算）----
    today_bar = False
    provisional = False
    #: INV-57 要求「不加 bar 时**显式标注原因**」——前端据此区分
    #: 「没请求 / 快照新鲜已合成 / 真实日线已含当天 / 快照陈旧·停牌·不可得」。
    if not include_today:
        today_bar_reason = "not-requested"
    else:
        last_before = str(daily["date"].iloc[-1])[:10] if len(daily) and "date" in daily else ""
        daily, today_bar = ds.with_today_bar(daily, code, asof)
        if today_bar:
            today_bar_reason = "added-fresh-snapshot"
            provisional = _provisional_flag(ds, asof)
        elif last_before and last_before >= asof:
            today_bar_reason = "already-in-frame"      # 真实日线已含当天（收盘后），无需合成
        else:
            # 快照陈旧 / 停牌 / 不可得 —— 更细的子原因在 ``ds.with_today_bar`` 内部，
            # 它当前只返回 ``(frame, added)``；要细化需 W1 把 reason 一并返回。
            today_bar_reason = "no-fresh-snapshot"

    quote: dict = {}
    try:
        # ★ **查看路径不得触发全市场快照**（ultraqa FAIL 的修复，T-PN-13）：
        #   缺省 `allow_fetch=True` 时，`quote()` 在「快照日 ≠ asof」的回退分支会调 `spot([code])`
        #   —— 那是**东财全市场 59 页**快照（ultraqa 实测冷启动首屏 **80.3–83.4s**），
        #   而这里**只为了一个 code 的名称与涨跌停价**。传 `False` 后该分支改用 cache-only 的
        #   `name_of`（取不到回退 code），**不改任何数字**（冷缓存下 `with_today_bar` 本来也
        #   early-return，不会合成当天 bar）。
        #   ⚠️ **下单/引擎路径仍用默认 True**，那一行不动。
        quote = dict(ds.quote(code, asof, allow_fetch=False) or {})
    except NetworkForbiddenInLock:
        # ★ `ds.quote()` **会**抛守卫（它走 `_cached`/`daily`/`spot`）。
        #   没有这一行，下面的 `except Exception` 会把守卫错误**静默降级成「没有行情」** ——
        #   违反 INV-42/45（守卫错误必须上抛，不得变成业务的「正常结果」）。
        #   由 `tests/test_wiring_invariants_v2.py::W1` 的「一跳静态规则」抓到（architect R3 的建议）。
        raise
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
        "include_today": bool(include_today),
        "today_bar": bool(today_bar),
        "today_bar_reason": today_bar_reason,
        "provisional": bool(provisional),
        # ★ latest 从**合成后的** frame 重算（daily 已在上面被 with_today_bar 替换）
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


# =========================================================================== #
# 分析缓存 + 状态（PN R16/R7/R2）—— 落盘、原子、**失败短 TTL**、相位并 key
# =========================================================================== #
#: 分析缓存 TTL（成功）：6h
ANALYSIS_TTL = 6 * 3600
#: 分析缓存 TTL（**失败**）：120s —— 失败也要缓存（INV-60 / spec A-4「不得让分析静默消失」），
#: 但**短** TTL 保证「下次渲染显示数据缺失」之后很快又能重试。
ANALYSIS_FAIL_TTL = 120

#: 「分析还没算完」的占位载荷 —— `/api/chart` 未命中缓存时**立即**返回它（不得被分析阻塞）。
#: ★ 前端判状态请读 `<body data-analysis-state="...">`，**禁止**用 `indexOf("计算中")` 文本扫描（PRD §8.3 形态⑤）。
ANALYSIS_PENDING: dict[str, Any] = {
    "ok": False, "pending": True, "error": "",
    "total": None, "signal": "", "factors": {}, "notes": {},
    "reasons": [], "risks": [], "recommend": None,
}


def ANALYSIS_FAILED(error: str) -> dict[str, Any]:
    """分析**明确失败**的占位载荷（带原因文案）—— 渲染为「数据缺失 —— <原因>」。

    与 :data:`ANALYSIS_PENDING` 的区别是**终态**：前端看到它应停止轮询（INV-60）。
    """
    return {
        "ok": False, "pending": False, "error": str(error),
        "total": None, "signal": "", "factors": {}, "notes": {},
        "reasons": [], "risks": [], "recommend": None,
    }


def analysis_state(analysis: dict | None) -> str:
    """``pending | ready | failed`` —— 渲染进 `<body data-analysis-state="...">`（PRD **R2 冻结契约**）。

    这是前端判断「继续轮询 / 停止」的**唯一**依据（DOM 属性，不是文本扫描）。
    """
    if not isinstance(analysis, dict):
        return "failed"
    if analysis.get("pending"):
        return "pending"
    return "ready" if analysis.get("ok") else "failed"


def analysis_cache_key(code: str, asof: str) -> str:
    """分析缓存 key：``analysis_<code>_<asof>``（**asof 必须进 key** —— 换日不得串味）。"""
    return f"analysis_{str(code)}_{str(asof)[:10]}"


def _analysis_cache_dir(cfg: AppConfig) -> str:
    """分析缓存的落地目录（与 `DataSource.cache_dir` 同一处：``<root>/data/cache``）。"""
    return os.path.join(str(cfg.root), "data", "cache")


def read_cached_analysis(cfg: AppConfig, code: str, asof: str) -> dict | None:
    """读分析缓存（**只读**：不联网、不计算）。缺失/过期/形状不对 → ``None``。

    ★ **TTL 按内容分档**（Critic 要求「相位并 key」，**不做** `{value, resolved_for, phase}` 三字段包装）：
    ``payload["ok"] is True`` → :data:`ANALYSIS_TTL`（6h）；失败 → :data:`ANALYSIS_FAIL_TTL`（120s）。
    """
    entry = DataSource._read_entry(_analysis_cache_dir(cfg), analysis_cache_key(code, asof))
    if not isinstance(entry, dict) or "payload" not in entry:
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    ttl = ANALYSIS_TTL if payload.get("ok") else ANALYSIS_FAIL_TTL
    if DataSource._expired(entry, ttl):
        return None
    return payload


def write_cached_analysis(cfg: AppConfig, code: str, asof: str, payload: Any) -> str:
    """把分析结果**原子落盘**，返回缓存文件路径。

    * 复用**既有** `DataSource._write_entry` 原子信封（先序列化→再 `os.replace`）——
      **不新增**存取函数（PRD R7）；序列化失败时目标文件原封不动。
    * ★ 缓存值就是**分析载荷本身**（相位并 key，不做三字段包装）。
    * ★ **拒绝写 `pending` 占位**：它一旦落盘，前端会把「还在算」误读成「数据缺失」而**停止轮询**。
    """
    if not isinstance(payload, dict):
        raise ValueError("分析缓存只接受 dict 载荷")
    if payload.get("pending"):
        raise ValueError(
            "pending 占位不得写缓存：落盘后前端会把「还在算」当成「数据缺失」而停止轮询"
        )
    return DataSource._write_entry(
        _analysis_cache_dir(cfg), analysis_cache_key(code, asof), payload
    )


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
    if analysis.get("pending"):
        # 首屏占位：图先出来，分析区在后台算（INV-60）。文案含「计算中」便于人工核对，
        # 但**机器判状态请用 <body data-analysis-state>**，不要扫文本（PRD §8.3 形态⑤）。
        return (
            f'<div class="analysis">{heading}'
            '<div class="warn">分析计算中…（首屏不被分析区阻塞，完成后自动刷新）</div></div>'
        )
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
  .tag-live { background:#fff7e8; color:#d25f00; border:1px solid #ffd591; font-weight:600; }
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
<body __ANALYSIS_STATE__>
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
  __PROVISIONAL__
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
/* ---------------------------------------------------------------------------
 * 页面级胶水（**K 线 option 构建器不在这里** —— 唯一真源是
 * `ptrade/webui_assets/chart_option.js`，`_render_html` 会把它内联到本段**之前**，
 * 所以同一段 <script> 里 `buildOption(...)` 直接可用；前端(W2)加载同一份文件）。
 * ------------------------------------------------------------------------- */
let active = window.__INITIAL_PERIOD__ || "daily";
const chart = echarts.init(document.getElementById("chart"), null, {renderer:"canvas"});

function currentMiniCandlestick() {
  /* ★ 迷你 K 线**只此一份实现**（V3-W1 结构收口）：`chart_option.js` 已内联在**本段之前**、
     且与本题在同一个 <script> 作用域里，所以直接调 `miniCandlestick(1/5)` 即可 ——
     **不再**由 Python 生成 series 字符串再注入（那会形成第二份实现 = 漂移源）。
     前端(W2)用同一份文件的同名函数，参数相同 ⇒ 按构造不存在两份实现。 */
  return { boll: miniCandlestick(1), sar: miniCandlestick(5) };
}

function switchPeriod(period) {
  active = period;
  ["daily","weekly","monthly"].forEach(function (p) {
    const el = document.getElementById("btn-" + p);
    if (el) el.className = (p === period) ? "active" : "";
  });
  chart.setOption(buildOption(CHART_DATA, period, {
    miniCandlestick: currentMiniCandlestick(),
    getActive: function () { return active; }
  }), true);
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


#: K 线 option 构建器的**唯一真源**（chart.py 内联它；前端 webui 也加载同一份）。
WEBUI_ASSETS = Path(__file__).resolve().parent / "webui_assets"
CHART_OPTION_JS = WEBUI_ASSETS / "chart_option.js"

#: `_inline_js()` 唯一允许削掉的前缀：ESM 的 `export `（浏览器 `<script>` 经典脚本不接受它）。
_EXPORT_PREFIX = re.compile(r"^export\s+", re.M)


def _inline_js(source: str) -> str:
    """把 `chart_option.js` 的源码变成**可内联进经典 `<script>`** 的源码。

    ★ **只允许这一处变换**：把行首的 ``export `` 去掉（前端用 ESM 必须保留它，
    内联到 `<script>` 则不能有它）。**禁止**在这里做任何别的改写（改名/拼接/替换）——
    「内联内容 == 文件经这一个变换后的结果」是 T-V3-19/T-V3-20 的判据基础。
    """
    return _EXPORT_PREFIX.sub("", source)


def _load_chart_option_js() -> str:
    """读取并内联 K 线 option 唯一真源（`ptrade/webui_assets/chart_option.js`）。"""
    if not CHART_OPTION_JS.is_file():
        raise FileNotFoundError(
            f"缺少 K 线 option 唯一真源：{CHART_OPTION_JS}"
            "（前端 W2 与 chart.py 内联共用同一份，缺了就是漂移源）"
        )
    return _inline_js(CHART_OPTION_JS.read_text(encoding="utf-8", errors="replace"))


def option_data(payload: dict, period: str) -> dict:
    """★★ **图表 option 的输入数据**（`chart_option.js` 的 `buildOption(payload, period)` 吃这个形状）。

    **为什么必须只有这一个函数**（X1，真缺陷的修法）：
    `buildOption` 从 **`payload.legend`** 取图例条目。此前 **HTML/CLI 路径**注入的是
    「瘦身版」（含 `legend`），而 **Web 路径**（`/api/chart/data`）却把 `chart_payload(...)`
    **原样**返回（**没有** `legend`）⇒ 前端 `legend.data = []` ⇒ **图例是空的、点不了**
    ⇒ 用户明确要求的「**图例点击隐藏**」**根本没实现**。

    ⇒ 抽成唯一真源：`_render_html` 与 `api_chart_data` **都**调它 ⇒ 两条路径按构造同形，
      图例不可能再只在一侧存在。（`legend` 的分组真源仍是 `legend_groups(payload)`。）
    """
    return {
        "periods": payload["periods"],
        "initial_period": period,
        "code": payload["code"],
        "legend": legend_groups(payload),
    }


def _render_html(payload: dict, analysis: dict | None, period: str) -> str:
    latest = payload.get("latest") or {}
    close = _finite(latest.get("close"))
    pct = _finite(latest.get("pct_chg"))
    direction = "up" if (pct is not None and pct >= 0) else "down"
    price_text = "—" if close is None else f"{close:.3f}"
    pct_text = "—" if pct is None else f"{pct:+.2f}%"
    meta = payload.get("meta") or {}
    bars = meta.get("bars") or {}

    # 图例：**Python 单一真源** → 注入页面（不可能与页面各自漂移）
    # ★ X1：瘦身版的构造已抽到 `option_data()`，与 `/api/chart/data` **共用同一函数**。
    legend = legend_groups(payload)
    # 「未收盘」标注：仅在真的合成了一根当天 bar 且未到 15:30 时出现（防恒标注）
    provisional_html = (
        '<span class="tag tag-live">未收盘 · 当日 bar 为实时快照合成</span>'
        if payload.get("provisional") else ""
    )

    # 内联 JSON 需防 </script> 提前闭合
    data_json = json.dumps(
        option_data(payload, period),      # ★ X1：唯一真源（与 /api/chart/data 同形）
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
        ("__PROVISIONAL__", provisional_html),
        ("__ANALYSIS_STATE__", f'data-analysis-state="{analysis_state(analysis)}"'),
        ("__ANALYSIS__", _analysis_html(analysis)),
        ("__ECHARTS__", _load_echarts()),
        ("__DATA__", data_json),
        # ★ K 线 option 的唯一真源 = 外部 `chart_option.js`（内联前经**唯一** `_inline_js()` 变换）
        ("__APP_JS__", _load_chart_option_js() + "\n" + _APP_JS),
    ):
        html = html.replace(token, value)
    return html


def render_chart(
    cfg: AppConfig,
    ds: DataSource,
    code: str,
    asof: str,
    period: str = "daily",
    use_qfq: bool = True,
    analysis: dict | None = None,
    *,
    include_today: bool = True,
) -> tuple[str, dict]:
    """渲染 K 线图 HTML，**返回 `(html, payload)`**（不落盘）。

    **为什么单独有这个函数**（W-6）：`/api/chart` 要的是**内联 HTML**，
    而 `build_chart` 只返回路径 —— 于是它必须 `write_text` 500 KB 再 `open().read()`
    读回来，**一次请求两趟盘**。这个函数让调用方直接拿到内存里的 html。

    ``include_today``（默认 True）透传给 :func:`chart_payload`；``use_qfq`` **保留**
    （5 个既有调用点全部兼容）。
    """
    if period not in PERIODS:
        raise ValueError(f"未知周期: {period!r}（可选 {'/'.join(PERIODS)}）")

    payload = chart_payload(cfg, ds, code, asof, use_qfq=use_qfq, include_today=include_today)
    if analysis is None:
        analysis = _score_analysis(cfg, ds, code, payload["asof"])
    return _render_html(payload, analysis, period), payload


def chart_out_path(cfg: AppConfig, code: str, asof: str, out: str | None = None) -> Path:
    """解析图表落盘路径（默认 `<root>/reports/chart_<code>_<asof>.html`）。

    抽出来是为了让 `build_chart`（CLI）与 `/api/chart`（网页端）用**同一套**路径规则 ——
    两边各写一份就迟早漂移。
    """
    target = out or os.path.join(str(cfg.root), "reports", f"chart_{code}_{asof}.html")
    return Path(target).expanduser()


def write_chart(path: Path, html: str) -> str:
    """原子写图表 HTML（tmp → fsync → `os.replace`），返回绝对路径。

    **W-6**：原先 `path.write_text(html)` 非原子。图表 HTML 约 500 KB，
    写到一半被杀（Ctrl+C / 断电 / 磁盘满）会留下**半截文件**，
    而文件名按「代码+asof」定址 → 下次同参数请求会**命中这个残文件**当成有效结果。
    原子写之后，读者要么看到旧内容、要么看到完整新内容。

    **不取写锁**：`reports/` 是按「代码+日期」定址的只写产物、不参与账户状态；
    并发写各自文件，`os.replace` 保证替换是原子的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write_text(str(path), html)
    return str(path.resolve())


def build_chart(
    cfg: AppConfig,
    ds: DataSource,
    code: str,
    asof: str,
    period: str = "daily",
    out: str | None = None,
    use_qfq: bool = True,
    analysis: dict | None = None,
    *,
    include_today: bool = True,
) -> str:
    """渲染 K 线图 HTML、**原子落盘**，返回**绝对路径**。``period ∈ {"daily","weekly","monthly"}``。

    返回**路径**（而不是 html）是既有契约，CLI 与 7 处测试都依赖它 ——
    要拿内存里的 html 请用 :func:`render_chart`（网页端就是这么做的，见 W-6）。

    ``include_today`` 与 ``use_qfq`` **都必须透传给** :func:`render_chart`
    （否则 CLI/网页无法关闭「含今天」的行为）。
    """
    html, payload = render_chart(cfg, ds, code, asof, period=period, use_qfq=use_qfq,
                                 analysis=analysis, include_today=include_today)
    return write_chart(chart_out_path(cfg, code, payload["asof"], out), html)


# =========================================================================== #
# 分时（F3：**独立入口 + 独立载荷形状**，绝不进 K 线管线）
# =========================================================================== #
_INTRADAY_PAGE = """<!DOCTYPE html>
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
  #chart { width:100%; height:620px; background:#fff; border:1px solid #e5e6eb; border-radius:8px; }
  footer { margin-top:12px; color:#86909c; font-size:12px; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="infobar">
  <span class="code">__CODE__</span>
  <span>__NAME__</span>
  <span class="price __DIR__">__LAST__</span>
  <span class="tag">分时 __DAY__</span>
  <span class="tag">数据源 __SOURCE__</span>
  <span class="tag">__COMPLETE__</span>
  <span class="tag">点数 __POINTS__</span>
</div>
<div id="chart"></div>
<footer>分时数据来自本地缓存/夹具（离线自包含，无外链） · 仅供研究/模拟，非投资建议</footer>
<script>__ECHARTS__</script>
<script>const INTRADAY = __DATA__;</script>
<script>__INTRADAY_JS__</script>
</body>
</html>
"""

#: 分时图 JS：价格线 + 均价线 + 分时量（独立于 K 线的 6 面板结构）
_INTRADAY_JS = r"""
/* ---------------------------------------------------------------------------
 * 分时**页面胶水**（option 构建器不在这里 —— 唯一真源是
 * `ptrade/webui_assets/chart_option.js` 的 `buildIntradayOption(payload)`，
 * `_render_intraday_html` 会把它内联到本段**之前**、同一个 <script>；Web 主页面(W2)共用同一份）。
 * ------------------------------------------------------------------------- */
const chart = echarts.init(document.getElementById("chart"), null, {renderer:"canvas"});
chart.setOption(buildIntradayOption(INTRADAY), true);
window.addEventListener("resize", function () { chart.resize(); });
"""


def intraday_out_path(cfg: AppConfig, code: str, day: str, out: str | None = None) -> Path:
    """分时落盘路径（默认 ``<root>/reports/intraday_<code>_<day>.html``）。

    ★ **绝不复用** :func:`chart_out_path`：后者是 ``chart_<code>_<asof>.html``，
    同一天生成分时会**覆盖 K 线图**（Critic N5）。
    """
    target = out or os.path.join(str(cfg.root), "reports", f"intraday_{code}_{day}.html")
    return Path(target).expanduser()


def _intraday_data(ds: DataSource, code: str, day: str | None) -> dict:
    """取分时数据；**非交易日 / 该日无数据 → 上一交易日**（不得回落到未来某天）。

    ★ **R6**：``NetworkForbiddenInLock`` ⊂ ``DataUnavailable``，所以守卫 handler 必须写在
    ``except DataUnavailable`` **之前**（Python 按顺序匹配）—— 否则守卫错误会掉进「换一天再试」
    的兜底：① 在锁内**多发起一次本不该发的取数**（INV-42 明确禁止）；② 真因被掩盖
    （错误从第二次调用冒出，堆栈指向兜底而不是原发点）。
    """
    if not day:
        return ds.intraday(code)
    requested = str(day)[:10]
    try:
        return ds.intraday(code, day=requested)
    except NetworkForbiddenInLock:
        raise      # INV-42/45：守卫错误必须上抛，不得降级成「换个交易日再试」
    except DataUnavailable:
        latest = ds.intraday(code)
        if str(latest.get("day") or "")[:10] <= requested:
            return latest
        raise


def _intraday_payload(cfg: AppConfig, ds: DataSource, code: str, data: dict) -> dict:
    """把 ``DataSource.intraday`` 的载荷转成**分时专用**的可 JSON 化结构（独立形状）。"""
    points = [p for p in (data.get("points") or []) if isinstance(p, dict)]
    times = [str(p.get("t") or "")[:5] for p in points]
    price = [_finite(p.get("price")) for p in points]
    avg = [_finite(p.get("avg")) for p in points]
    vol = [_finite(p.get("vol")) for p in points]
    amount = [_finite(p.get("amount")) for p in points]
    bar_up: list[bool] = []
    previous: float | None = None
    for value in price:
        bar_up.append(True if (value is None or previous is None) else bool(value >= previous))
        if value is not None:
            previous = value

    name = str(code)
    try:
        # ★ F3 / INV-61：名称与 **asof 解耦** —— 原先用 `ds.quote(code, day)`，
        #   而分时的 `day` 常常≠快照 asof（真实场景下必然发生在「分时日 ≠ 快照日」），
        #   于是 quote 失败 → 名称退化成代码。`name_of` **只读缓存/夹具、绝不联网**，
        #   在任意 `day` 下都能给出名称（真冷启动无 spot/无名称表时才回退 code，T-PN-4b）。
        name = str(ds.name_of(code) or code)
    except NetworkForbiddenInLock:
        # ★ 保留守卫上抛纪律（原 `ds.quote` 版本的教训，INV-42/45）：
        #   `NetworkForbiddenInLock ⊂ DataUnavailable`，若先被下面的 `except Exception`
        #   接住，就会把「锁内联网」静默降级成「拿不到名称」——名称确实非必需，
        #   但**守卫错误不是「名称不可得」**（它必须响亮地传播到 409）。
        raise
    except Exception:  # noqa: BLE001 - 名称非必需
        pass

    day = str(data.get("day") or "")
    return {
        "kind": "intraday",
        "code": str(code),
        "name": name,
        "day": day,
        "asof": str(data.get("asof") or day),
        "source": str(data.get("source") or ""),
        "complete": bool(data.get("complete")),
        "fetched_at": str(data.get("fetched_at") or ""),
        "times": times,
        "price": price,
        "avg": avg,
        "vol": vol,
        "amount": amount,
        "bar_up": bar_up,
        "meta": {
            "label": PERIOD_LABEL["minute"],
            "points": len(points),
            "window": [times[0], times[-1]] if times else [],
        },
    }


def _render_intraday_html(payload: dict) -> str:
    price = [x for x in payload.get("price", []) if x is not None]
    last = price[-1] if price else None
    first = price[0] if price else None
    direction = "up" if (last is not None and first is not None and last >= first) else "down"
    title = f"{payload['code']} {payload['name']} 分时图（{payload['day']}）"
    data_json = json.dumps(
        {
            "times": payload["times"], "price": payload["price"], "avg": payload["avg"],
            "vol": payload["vol"], "amount": payload["amount"], "bar_up": payload["bar_up"],
            "day": payload["day"], "code": payload["code"],
        },
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).replace("</", "<\\/")
    html = _INTRADAY_PAGE
    for token, value in (
        ("__TITLE__", _escape(title)),
        ("__CODE__", _escape(payload["code"])),
        ("__NAME__", _escape(str(payload.get("name") or ""))),
        ("__LAST__", "—" if last is None else f"{last:.3f}"),
        ("__DIR__", direction),
        ("__DAY__", _escape(str(payload.get("day") or ""))),
        ("__SOURCE__", _escape(str(payload.get("source") or ""))),
        ("__COMPLETE__", "完整交易日" if payload.get("complete") else "数据不完整"),
        ("__POINTS__", str(int((payload.get("meta") or {}).get("points") or 0))),
        ("__ECHARTS__", _load_echarts()),
        ("__DATA__", data_json),
        ("__INTRADAY_JS__", _load_chart_option_js() + "\n" + _INTRADAY_JS),
    ):
        html = html.replace(token, value)
    return html


def render_intraday(
    cfg: AppConfig,
    ds: DataSource,
    code: str,
    day: str | None = None,
) -> tuple[str, dict]:
    """渲染**分时** HTML，返回 ``(html, payload)``（不落盘；要落盘用 :func:`build_intraday`）。

    **独立入口 + 独立载荷形状**（``kind=="intraday"``），**不进 K 线管线**
    （``PERIODS`` 不含 ``minute``；``resample_ohlc`` 也拒绝它）。
    """
    data = _intraday_data(ds, code, day)
    payload = _intraday_payload(cfg, ds, code, data)
    return _render_intraday_html(payload), payload


def build_intraday(
    cfg: AppConfig,
    ds: DataSource,
    code: str,
    day: str | None = None,
    out: str | None = None,
) -> str:
    """渲染分时 HTML、原子落盘，返回**绝对路径**（``reports/intraday_<code>_<day>.html``）。"""
    html, payload = render_intraday(cfg, ds, code, day)
    return write_chart(intraday_out_path(cfg, code, payload["day"], out), html)


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

