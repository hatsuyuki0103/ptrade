# -*- coding: utf-8 -*-
"""ptrade/strategy.py — CANSLIM 七因子打分与买卖/做空信号（[T3]）。

接口真源：``INTERFACES.md`` v1 §6（七因子口径表 §6.1 + 信号判定顺序 §6.2）。

设计要点
--------
1. **只依赖 config / datasource**，本模块自身不做任何网络访问；一切数据经 ``DataSource``，
   ``offline=True`` 时由数据层保证不联网。
2. **纯打分函数与 IO 分离**：§6.1 表格的每一行都对应一个 ``map_*`` 纯函数（无 IO、可逐点单测），
   便于边界值断言；``CanslimScorer`` 只负责取数与组装。
3. **缺失数据一律降级**：某因子主数据不可得 → 该因子 50 分（中性），并在 ``risks`` 中标注
   ``"<因子>数据缺失"``（如 ``"C数据缺失"``），**绝不抛异常**。
4. **完全无行情数据**（离线夹具缺该标的、且快照也没有）→ ``score()`` 抛 ``StrategyDataError``；
   ``score_many()`` 捕获后给该票 ``total=0.0``、``factors=[]``、``signal="avoid"``、``risks`` 非空。
5. **禁止未来函数**：日线/指数/业绩报表/年报/股东户数一律按 ``asof`` 过滤（年报按法定披露截止日
   = 次年 4-30 判断，见 :func:`report_periods`）。**已知契约层残留**：§6.1 规定 S 因子取
   ``spot()`` 的 ``float_mv``/``turnover``，而 §4 的 ``spot()`` 签名**没有 asof 参数** → 回溯历史
   ``asof`` 时这两个值来自"最新快照"而非当日，属接口层面的前视偏差（权重 0.12）。本模块按契约
   取数不做私改，该风险已在交付报告中上报 Lead/verifier。
5. **单位一律按 INTERFACES.md §4 的强制单位表直接取值，不做自适应探测**：
   ``float_mv`` 为**元**（按 §6.1 的「亿元」阈值消费时 ÷1e8）、``ratio``/``turnover``/``roe``/
   ``holders_chg_pct``/``*_yoy`` 均为**百分数**（``12.5`` 表示 12.5%）。量纲由数据层与夹具保证；
   一旦夹具量纲有误，会立刻在 TC-S-22 上暴露，而不是被启发式探测掩盖。

INTERFACES.md 未明确规定处的实现口径（已显式记录，便于评审核对）
--------------------------------------------------------------
* ``total`` 为**精确加权和**（不做四舍五入）；阈值比较带 ``_EPS = 1e-9`` 容差，避免浮点误差跨档。
* ``score_many`` 的**失败兜底结果不套用 §6.2 分支**（契约 §6.2 明文）：取数失败≠看空，故
  ``signal="avoid"``；``rank()`` 亦把失败票排除出 ``shorts``。
* ``recommend()`` 的 ``entry`` 从打分结果的 ``value["close"]/["last"]`` 取（§6 冻结签名不带
  DataSource，故不在 recommend 内二次取数）；**取不到价格时降级为 ``action="watch"``** 并记入
  ``risks``，避免下游 ``engine.build_plan`` 拿着 ``entry=0`` 算出 qty=0 或除零。
* 缺失（``missing=True``）的因子给中性 50 分；``to_dict()`` 为 CLI JSON 序列化提供便利（附加方法，不改契约）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from .config import AppConfig, CANSLIMConfig
from .datasource import DataSource

__all__ = [
    "FACTORS",
    "FactorScore",
    "CanslimResult",
    "CanslimScorer",
    "StrategyDataError",
    "rank",
    "recommend",
    "report_periods",
    "map_c_growth",
    "map_a_growth",
    "map_n_position",
    "map_s_float_mv",
    "map_l_percentile",
    "map_l_absolute",
    "map_i_ratio",
    "map_m_breadth",
]

# ---------------------------------------------------------------------------
# 契约常量（INTERFACES.md §6）
# ---------------------------------------------------------------------------

FACTORS: tuple[str, ...] = ("C", "A", "N", "S", "L", "I", "M")

NEUTRAL_SCORE = 50.0            # 缺失数据的中性分
EPS = 1e-9                      # 阈值比较容差
SCORE_MIN = 0.0
SCORE_MAX = 100.0

N_WINDOW = 250                  # N 因子：250 日区间位置
N_VOLUME_WINDOW = 50            # N 因子：50 日均量
N_BREAKOUT_VOLUME_RATIO = 1.5   # N 因子：「突破 + 放量」的量能倍数
N_NEW_HIGH_DISCOUNT = 0.98      # N 因子：收盘价 ≥ 250 日最高 × 0.98 视为「创新高」

L_WINDOW_DAYS = 182             # L 因子：近 6 月（自然日）
L_MIN_COMPETITORS = 10          # §6.1 门槛：行业**可比较对手**数（= 可算 RS 成分股数 − 自身）< 10
                                # → 强制降级为绝对 RS 分档。口径依据：百分位分辨率 = 比较次数 = n−1，
                                # 计自身等于让候选股给自己凑样本（verifier CR-16 裁定）。
L_MAX_INDUSTRY_PEERS = 50       # 行业内取数上限（**截断前按代码升序**，保证子集可复现）

S_TURNOVER_LOW, S_TURNOVER_HIGH = 3.0, 15.0   # S 因子：换手率 3%-15% → +10

I_ROE_HURDLE = 17.0             # A 因子：ROE ≥ 17% 额外 +10

M_MA50_BONUS, M_MA200_BONUS = 20.0, 20.0
M_LIMIT_UP_STRONG, M_LIMIT_UP_MILD = 50, 20

DEFAULT_INDEX_CANDIDATES: tuple[str, ...] = ("sh000001", "000001", "csi931151", "sh000300")

# 报告期披露截止日（period 月/日 → 披露截止 月/日；年报次年披露）
_PERIOD_DEADLINES: tuple[tuple[int, int, int, int], ...] = (
    (12, 31, 4, 30),
    (9, 30, 10, 31),
    (6, 30, 8, 31),
    (3, 31, 4, 30),
)

# 机构股东识别关键词（先排除自然人，再匹配机构）
_NON_INSTITUTION_KEYWORDS = ("个人", "自然人", "职工", "员工", "高管", "管理层", "内部")
_INSTITUTION_KEYWORDS = (
    "机构", "基金", "保险", "社保", "养老", "年金", "券商", "证券", "信托", "银行",
    "资管", "理财", "私募", "qfii", "投资", "资本", "控股", "集团", "公司", "合伙",
    "大学", "研究院", "资产", "法人",
)


class StrategyDataError(RuntimeError):
    """标的完全无行情数据，无法评分（``score_many`` 会捕获并降级为 total=0）。"""


# ---------------------------------------------------------------------------
# 通用小工具（纯函数）
# ---------------------------------------------------------------------------


def _parse_date(value: str | date | datetime) -> date:
    """解析 ``YYYY-MM-DD`` / ``YYYYMMDD`` / ``date`` / ``datetime`` → ``datetime.date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text[:10])


def _num(value: Any) -> float | None:
    """宽松数值转换：``None`` / ``NaN`` / ``inf`` / 空串 / ``"--"`` → ``None``。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("%", "").replace(",", "")
        if text in {"", "-", "--", "nan", "none", "null", "NaN"}:
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _norm_code(value: Any) -> str:
    """``sh600519`` / ``600519.SH`` / ``600519`` → ``600519``。"""
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else text


def _clamp(score: float, low: float = SCORE_MIN, high: float = SCORE_MAX) -> float:
    return max(low, min(high, score))


def _to_yi(value: float | None) -> float | None:
    """元 → 亿元（INTERFACES.md §4 强制单位表：``float_mv`` 为**元**，§6.1 阈值按亿元）。"""
    if value is None:
        return None
    return value / 1e8


def _to_ratio(value: float | None) -> float | None:
    """百分数 → 小数（INTERFACES.md §4 强制单位表：``ratio`` 为**百分数**，如 12.5 表示 12.5%）。"""
    if value is None:
        return None
    return value / 100.0


def _is_institution(holder_type: Any, holder_name: Any = None) -> bool:
    """判断十大股东行是否属于「机构」（先排除自然人，再匹配机构关键词）。"""
    for field in (holder_type, holder_name):
        text = str(field or "").strip().lower()
        if not text:
            continue
        if any(kw in text for kw in _NON_INSTITUTION_KEYWORDS):
            return False
        if any(kw in text for kw in _INSTITUTION_KEYWORDS):
            return True
    return False


def report_periods(asof: str | date, years_back: int = 3) -> list[str]:
    """返回 ``asof`` 时点**已披露**的报告期（``YYYYMMDD``），按由新到旧排序。

    依据各报告期的法定披露截止日（Q1/年报 4-30、H1 8-31、Q3 10-31）过滤。
    例：``asof="2025-10-09"`` → ``["20250630", "20250331", "20241231", ...]``。
    """
    day = _parse_date(asof)
    periods: list[str] = []
    for year in range(day.year, day.year - years_back - 1, -1):
        for month, dom, deadline_month, deadline_day in _PERIOD_DEADLINES:
            deadline_year = year + 1 if month == 12 else year
            if date(deadline_year, deadline_month, deadline_day) <= day:
                periods.append(f"{year}{month:02d}{dom:02d}")
    periods.sort(reverse=True)
    return periods


def _norm_daily(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """归一化日线：``date`` 为 ``YYYY-MM-DD`` 字符串、价格/量列转数值、按日期升序。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    out = df.copy()
    if "date" not in out.columns:
        return None
    out["date"] = out["date"].astype(str).str.slice(0, 10)
    for column in ("open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"):
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return out if len(out) else None


def _frame_rows(df: pd.DataFrame | None) -> list[dict]:
    """DataFrame → ``list[dict]``（空 / 非 DataFrame → 空列表）。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    return [{str(k): v for k, v in record.items()} for record in df.to_dict(orient="records")]


def _has_market_info(quote: dict | None, spot: dict | None) -> bool:
    """是否至少拿到一条可用的行情信息（名称或最新价），用于判定「该标的是否存在」。"""
    for source in (quote, spot):
        if not isinstance(source, dict):
            continue
        if str(source.get("name") or "").strip():
            return True
        price = _num(source.get("last"))
        if price is not None and price > 0:
            return True
    return False


def _pick_row(df: pd.DataFrame | None, code: str) -> dict | None:
    """从含 ``code`` 列的 DataFrame 中取该标的首行（代码按 6 位数字归一化匹配）。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty or "code" not in df.columns:
        return None
    try:
        target = _norm_code(code)
        matched = df[df["code"].map(_norm_code) == target]
        if matched.empty:
            return None
        return {str(k): v for k, v in matched.iloc[0].to_dict().items()}
    except Exception:  # pragma: no cover - 防御性：脏数据不得影响评分
        return None


# ---------------------------------------------------------------------------
# §6.1 纯打分函数（逐条对应口径表，无 IO，可逐点单测）
# ---------------------------------------------------------------------------


def map_c_growth(yoy: float) -> float:
    """C 因子基础分：``>=100%→100；>=50%→90；>=30%→80；>=20%→70；>=10%→55；>=0→40；<0→15``。"""
    if yoy >= 100.0:
        return 100.0
    if yoy >= 50.0:
        return 90.0
    if yoy >= 30.0:
        return 80.0
    if yoy >= 20.0:
        return 70.0
    if yoy >= 10.0:
        return 55.0
    if yoy >= 0.0:
        return 40.0
    return 15.0


def map_a_growth(yoy: float) -> float:
    """A 因子单一年度基础分：``>=50→100；>=25→85；>=15→70；>=5→55；>=0→40；<0→20``。"""
    if yoy >= 50.0:
        return 100.0
    if yoy >= 25.0:
        return 85.0
    if yoy >= 15.0:
        return 70.0
    if yoy >= 5.0:
        return 55.0
    if yoy >= 0.0:
        return 40.0
    return 20.0


def map_n_position(
    distance: float,
    vol_ratio: float | None = None,
    *,
    breakout: bool = False,
) -> float:
    """N 因子：250 日区间位置 + 「突破且放量」加成。

    :param distance: ``(250日最高 - 收盘价) / 250日最高``（≥0；≤0.02 即「创新高」）
    :param vol_ratio: 当日成交量 / 50 日均量
    :param breakout: 是否**放量突破**（收盘价创出新高，或突破前 250 日最高价）
    """
    if distance <= 0.02:
        score = 100.0
    elif distance <= 0.05:
        score = 85.0
    elif distance <= 0.15:
        score = 65.0
    elif distance <= 0.30:
        score = 45.0
    else:
        score = 25.0
    if breakout and vol_ratio is not None and vol_ratio > N_BREAKOUT_VOLUME_RATIO:
        score = max(score, 90.0)
    return score


def map_s_float_mv(float_mv_yi: float) -> float:
    """S 因子流通市值分档（单位：亿元）：``<50→80；<200→90；<500→75；<1000→60；否则 45``。"""
    if float_mv_yi < 50.0:
        return 80.0
    if float_mv_yi < 200.0:
        return 90.0
    if float_mv_yi < 500.0:
        return 75.0
    if float_mv_yi < 1000.0:
        return 60.0
    return 45.0


def map_l_percentile(percentile: float) -> float:
    """L 因子行业排名分（``percentile`` = 行业内相对强度百分位 0-1，越大越强）。"""
    if percentile >= 0.90:
        return 100.0
    if percentile >= 0.75:
        return 85.0
    if percentile >= 0.50:
        return 65.0
    if percentile >= 0.25:
        return 45.0
    return 25.0


def map_l_absolute(rs: float) -> float:
    """L 因子退化分档（``rs`` 为百分数，例如 32.5 表示 +32.5%）。"""
    if rs > 30.0:
        return 90.0
    if rs > 15.0:
        return 75.0
    if rs > 0.0:
        return 60.0
    if rs > -15.0:
        return 40.0
    return 20.0


def map_i_ratio(ratio: float) -> float:
    """I 因子机构合计持股分档（``ratio`` 为小数）：``10%-60%→85；60%-80%→70；>80%→55；3%-10%→60；<3%→35``。"""
    if ratio > 0.80:
        return 55.0
    if ratio > 0.60:
        return 70.0
    if ratio >= 0.10:
        return 85.0
    if ratio >= 0.03:
        return 60.0
    return 35.0


def map_m_breadth(breadth: dict | None) -> float:
    """M 因子：基础 20 分 + 均线/20 日涨幅/涨跌家数/涨停家数分项，上限 100。"""
    data = breadth or {}
    score = 20.0
    if data.get("above_ma50"):
        score += M_MA50_BONUS
    if data.get("above_ma200"):
        score += M_MA200_BONUS

    change_20d = _num(data.get("pct_chg_20d"))
    if change_20d is not None:
        if change_20d > 5.0:
            score += 25.0
        elif change_20d > 0.0:
            score += 15.0
        elif change_20d > -5.0:
            score += 5.0

    advancers = _num(data.get("advancers"))
    decliners = _num(data.get("decliners"))
    ratio: float | None = None
    if advancers is not None and decliners is not None:
        if decliners > 0:
            ratio = advancers / decliners
        elif advancers > 0:
            ratio = math.inf
        else:
            ratio = 1.0
    if ratio is not None:
        if ratio > 2.0:
            score += 20.0
        elif ratio > 1.0:
            score += 12.0
        elif ratio >= 0.5:
            score += 5.0

    limit_up = _num(data.get("limit_up_count"))
    if limit_up is not None:
        if limit_up > M_LIMIT_UP_STRONG:
            score += 15.0
        elif limit_up >= M_LIMIT_UP_MILD:
            score += 8.0

    return _clamp(score)


# ---------------------------------------------------------------------------
# 结果模型（INTERFACES.md §6 冻结字段）
# ---------------------------------------------------------------------------


@dataclass
class FactorScore:
    key: str            # "C".."M"
    score: float        # 0-100
    weight: float
    value: dict         # 原始输入值（JSON 可序列化，用于解释）
    note: str           # 中文解释（引用具体数值）

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "score": self.score,
            "weight": self.weight,
            "value": dict(self.value),
            "note": self.note,
        }


@dataclass
class CanslimResult:
    code: str
    name: str
    asof: str
    total: float
    factors: list[FactorScore]
    signal: str
    reasons: list[str]
    risks: list[str]

    def factor(self, key: str) -> FactorScore | None:
        """按 key 取因子（不存在返回 ``None``）。"""
        for item in self.factors:
            if item.key == key:
                return item
        return None

    def score_of(self, key: str) -> float | None:
        item = self.factor(key)
        return None if item is None else item.score

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "asof": self.asof,
            "total": self.total,
            "factors": [item.to_dict() for item in self.factors],
            "signal": self.signal,
            "reasons": list(self.reasons),
            "risks": list(self.risks),
        }


@dataclass
class _Ctx:
    """单票评分上下文：一次取数、多因子复用。"""

    code: str
    asof: str
    daily: pd.DataFrame | None
    spot: dict | None
    quote: dict | None


# ---------------------------------------------------------------------------
# §6.2 信号判定（顺序敏感，先命中先返回）
# ---------------------------------------------------------------------------


def _decide_signal(
    total: float,
    m_score: float,
    l_score: float,
    cfg: CANSLIMConfig,
    *,
    m_missing: bool = False,
    abs_rs: float | None = None,
) -> tuple[str, str]:
    """按 §6.2（v3.5）的 1→5 顺序判定信号，返回 ``(signal, 中文理由)``。

    ========== ==========================================================
    分支        条件
    ========== ==========================================================
    1          ``total <= short_threshold`` → ``short``
    2          ``total >= buy_threshold`` 且 ``M >= 50`` 且 ``L >= 60``
               且 ``abs_rs > 0`` → ``buy``
    3          ``total >= buy_threshold`` 但（``M < 50`` 或 ``L < 60``
               或 ``abs_rs <= 0``）→ ``watch``
    4          ``total >= 50`` → ``watch``
    5          其他 → ``avoid``
    ========== ==========================================================

    两条**独立前置**（都不钳制因子分值，只作用于 §6.2 判定）：

    - ``m_missing``：M 因子数据缺失的**显式标记**（不是 ``m_score == 50`` 推断）——中性 50 恰好会
      通过分支 2 的 ``M >= 50``，等于「取数失败反而更容易放行买入」（INV-14 / A12）；
    - ``abs_rs``：近 6 月**绝对** RS（百分数）。**严格 ``> 0``，不加 epsilon**（``abs_rs == 0`` 不通过）；
      取不到（``None``）→ **fail-closed**，不得判 ``buy``。它是独立前置，**不钳制 L 分值** ——
      L 仍严格等于 §6.1 分档值（板块内相对强度），绝对强度由本前置把守。

    分支 3 用「三条件或」穷尽所有「总分够但前置不全」的输入，保证任意输入**恰好命中 1..5 中的一条**。
    """
    buy_line = cfg.buy_threshold
    short_line = cfg.short_threshold
    rs_ok = abs_rs is not None and abs_rs > 0.0        # 严格 > 0，不加 epsilon

    # 1. 总分 ≤ 做空门槛 → short
    if total <= short_line + EPS:
        return "short", (
            f"总分 {total:.2f} ≤ 做空门槛 {short_line:.1f}，七因子综合显著弱于市场，具备融券做空候选资格"
        )
    # 2. 总分 ≥ 买入门槛 且 M ≥ 50 且 L ≥ 60 且 绝对 RS > 0 → buy
    if total >= buy_line - EPS and m_score >= 50.0 and l_score >= 60.0 and rs_ok and not m_missing:
        return "buy", (
            f"总分 {total:.2f} ≥ 买入门槛 {buy_line:.1f}，且市场方向 M={m_score:.0f} ≥ 50、"
            f"相对强度 L={l_score:.0f} ≥ 60、绝对 RS {abs_rs:+.2f}% > 0，四项前置全部满足，满足买入条件"
        )
    # 3. 总分 ≥ 买入门槛 但（M < 50 或 L < 60 或 绝对 RS ≤ 0）→ watch
    if total >= buy_line - EPS:
        failed: list[str] = []
        if m_missing:
            failed.append("市场方向(M)数据缺失")
        elif m_score < 50.0:
            failed.append(f"市场方向 M={m_score:.0f} < 50")
        if l_score < 60.0:
            failed.append(f"相对强度 L={l_score:.0f} < 60")
        if abs_rs is None:
            failed.append("绝对 RS 不可得（fail-closed）")
        elif abs_rs <= 0.0:
            failed.append(f"绝对 RS {abs_rs:+.2f}% ≤ 0（未跑赢指数）")
        return "watch", (
            f"总分 {total:.2f} ≥ 买入门槛 {buy_line:.1f}，但" + "、".join(failed) + "，买入前置不齐，暂列观察"
        )
    # 4. 总分 ≥ 50 → watch
    if total >= 50.0 - EPS:
        return "watch", (
            f"总分 {total:.2f} 介于 50.0 与买入门槛 {buy_line:.1f} 之间，方向待确认，暂列观察"
        )
    # 5. 其他 → avoid
    return "avoid", (
        f"总分 {total:.2f} 低于 50.0 且高于做空门槛 {short_line:.1f}，多空方向均不明确，建议规避"
    )


# ---------------------------------------------------------------------------
# 打分器
# ---------------------------------------------------------------------------


class CanslimScorer:
    """CANSLIM 七因子打分器（INTERFACES.md §6）。"""

    def __init__(self, cfg: AppConfig, ds: DataSource, offline: bool = False):
        self.cfg = cfg
        self.ds = ds
        self.offline = offline
        self.canslim: CANSLIMConfig = cfg.canslim
        self._weights: dict[str, float] = {
            "C": self.canslim.weight_c,
            "A": self.canslim.weight_a,
            "N": self.canslim.weight_n,
            "S": self.canslim.weight_s,
            "L": self.canslim.weight_l,
            "I": self.canslim.weight_i,
            "M": self.canslim.weight_m,
        }
        self._quarterly_cache: dict[str, pd.DataFrame | None] = {}
        self._fin_cache: dict[str, pd.DataFrame | None] = {}
        self._holders_cache: dict[str, pd.DataFrame | None] = {}
        self._top_holders_cache: dict[tuple, tuple[pd.DataFrame | None, dict]] = {}
        self._daily_cache: dict[tuple, pd.DataFrame | None] = {}
        self._return_cache: dict[tuple, float | None] = {}
        self._spot_cache: dict[str, dict | None] = {}
        self._quote_cache: dict[tuple, dict | None] = {}
        self._industry_cache: dict[str, str | None] = {}
        self._breadth_cache: dict[str, dict | None] = {}
        self._m_cache: dict[str, FactorScore] = {}
        self._index_code: str | None = None
        self._last_error: str | None = None

    # ---- 数据访问（失败一律降级为 None，绝不抛异常） ----

    def _try(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # DataUnavailable / 网络 / 解析异常 → 一律降级
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _quarterly_report(self, period: str) -> pd.DataFrame | None:
        if period not in self._quarterly_cache:
            self._quarterly_cache[period] = self._try(self.ds.quarterly_report, period)
        return self._quarterly_cache[period]

    def _fin_indicator(self, code: str) -> pd.DataFrame | None:
        if code not in self._fin_cache:
            start_year = str(_parse_date(self._today_hint(code)).year - 6)
            self._fin_cache[code] = self._try(self.ds.fin_indicator, code, start_year)
        return self._fin_cache[code]

    def _today_hint(self, code: str) -> date:  # pragma: no cover - 仅用于推算取数起始年份
        return date.today()

    def _holders(self, period: str) -> pd.DataFrame | None:
        if period not in self._holders_cache:
            self._holders_cache[period] = self._try(self.ds.holders, period)
        return self._holders_cache[period]

    def _top_holders(self, code: str, asof: str) -> tuple[pd.DataFrame | None, dict]:
        """取 ``asof`` 时点**已披露**的十大股东期次，返回 ``(frame, meta)``。

        §4 的 ``top_holders(code, asof)`` 负责按期披露日过滤（``asof`` 之前无已披露期次 → 空表）；
        ``frame.attrs["top_holders_meta"]`` 提供 ``report_date`` / ``announce_date`` / ``disclosed_on`` /
        ``is_historical`` / ``periods``。**消费方绝不回落到未来期次**（CR-7：禁止前视）。
        """
        key = (code, str(asof))
        if key not in self._top_holders_cache:
            frame = self._try(self.ds.top_holders, code, str(asof))
            meta: dict = {}
            attrs = getattr(frame, "attrs", None)
            if isinstance(attrs, dict):
                raw = attrs.get("top_holders_meta")
                if isinstance(raw, dict):
                    meta = dict(raw)
            self._top_holders_cache[key] = (frame, meta)
        return self._top_holders_cache[key]

    def _daily_window(self, code: str, asof: str, window: int = N_WINDOW) -> pd.DataFrame | None:
        key = ("daily", code, asof, window)
        if key in self._daily_cache:
            return self._daily_cache[key]
        start = (_parse_date(asof) - timedelta(days=400)).isoformat()
        frame = _norm_daily(self._try(self.ds.daily, code, start, str(asof)))
        if frame is not None:
            frame = frame[frame["date"] <= str(asof)]
            frame = frame.tail(window).reset_index(drop=True)
            if frame.empty:
                frame = None
        self._daily_cache[key] = frame
        return frame

    def _spot_row(self, code: str) -> dict | None:
        if code not in self._spot_cache:
            self._spot_cache[code] = _pick_row(self._try(self.ds.spot, [code]), code)
        return self._spot_cache[code]

    def _quote(self, code: str, asof: str) -> dict | None:
        key = (code, str(asof))
        if key not in self._quote_cache:
            quote = self._try(self.ds.quote, code, str(asof))
            self._quote_cache[key] = quote if isinstance(quote, dict) and quote else None
        return self._quote_cache[key]

    def _industry(self, code: str) -> str | None:
        if code not in self._industry_cache:
            industry = self._try(self.ds.industry_of, code)
            text = str(industry).strip() if industry is not None else ""
            self._industry_cache[code] = text or None
        return self._industry_cache[code]

    def _breadth(self, asof: str) -> dict | None:
        if str(asof) not in self._breadth_cache:
            data = self._try(self.ds.market_breadth, str(asof))
            self._breadth_cache[str(asof)] = data if isinstance(data, dict) and data else None
        return self._breadth_cache[str(asof)]

    # ---- 派生数据 ----

    @staticmethod
    def _return_of(df: pd.DataFrame | None, asof: str) -> float | None:
        frame = _norm_daily(df)
        if frame is None:
            return None
        frame = frame[frame["date"] <= str(asof)]
        if len(frame) < 2 or "close" not in frame.columns:
            return None
        first = _num(frame["close"].iloc[0])
        last = _num(frame["close"].iloc[-1])
        if first is None or last is None or first <= 0:
            return None
        return last / first - 1.0

    def _stock_return(self, code: str, asof: str) -> float | None:
        key = ("ret", code, str(asof))
        if key in self._return_cache:
            return self._return_cache[key]
        start = (_parse_date(asof) - timedelta(days=L_WINDOW_DAYS)).isoformat()
        value = self._return_of(self._try(self.ds.daily, code, start, str(asof)), str(asof))
        self._return_cache[key] = value
        return value

    def _index_return(self, asof: str) -> float | None:
        key = ("idx", str(asof))
        if key in self._return_cache:
            return self._return_cache[key]
        start = (_parse_date(asof) - timedelta(days=L_WINDOW_DAYS)).isoformat()
        value: float | None = None
        for index_code in ((self._index_code,) if self._index_code else DEFAULT_INDEX_CANDIDATES):
            frame = self._try(self.ds.index_daily, index_code, start, str(asof))
            value = self._return_of(frame, str(asof))
            if value is not None:
                self._index_code = index_code
                break
        self._return_cache[key] = value
        return value

    # ---- 因子：缺失兜底 ----

    def _missing_factor(self, key: str, detail: str, risk: str | None = None) -> FactorScore:
        value: dict[str, Any] = {"missing": True, "detail": detail}
        if risk:
            value["risk"] = risk
        return FactorScore(
            key=key,
            score=NEUTRAL_SCORE,
            weight=self._weights[key],
            value=value,
            note=f"{key} 因子主数据不可得（{detail}），按中性 {NEUTRAL_SCORE:.0f} 分处理",
        )

    def _latest_trade_date(self) -> str | None:
        getter = getattr(self.ds, "latest_trade_date", None)
        if not callable(getter):
            return None
        value = self._try(getter)
        text = str(value)[:10] if value else ""
        return text or None

    def _is_historical(self, asof: str) -> bool:
        """``asof`` 早于最新交易日 → 历史回溯。

        历史回溯时**禁止使用** ``spot()``：§4 的 ``spot()`` 是"实时快照"（无 asof 参数），
        返回的是"当下"的数据，用它给历史 ``asof`` 打分等于引入未来信息（前视偏差）。
        """
        latest = self._latest_trade_date()
        return bool(latest) and str(asof)[:10] < latest

    # ---- C：当季 EPS 增长（含加速加成） ----

    def _factor_c(self, ctx: _Ctx) -> FactorScore:
        key = "C"
        periods = report_periods(ctx.asof)
        row: dict | None = None
        period: str | None = None
        for candidate in periods:
            found = _pick_row(self._quarterly_report(candidate), ctx.code)
            if found is not None:
                row, period = found, candidate
                break
        if row is None or period is None:
            return self._missing_factor(key, "业绩报表 quarterly_report 无该标的记录")

        yoy = _num(row.get("net_profit_yoy"))
        if yoy is None:
            return self._missing_factor(key, f"报告期 {period} 的 net_profit_yoy 为空")

        base = map_c_growth(yoy)
        prev_yoy = self._previous_quarter_yoy(ctx, periods, period)
        accelerated = prev_yoy is not None and yoy > prev_yoy
        bonus = float(self.canslim.c_accel_bonus)
        score = _clamp(base * bonus) if accelerated else base

        if accelerated:
            note = (
                f"当季净利润同比 {yoy:+.1f}%（报告期 {period}）> 上季 {prev_yoy:+.1f}%，增长加速："
                f"基础分 {base:.0f} × {bonus:.2f} = {score:.1f}"
            )
        elif prev_yoy is not None:
            note = (
                f"当季净利润同比 {yoy:+.1f}%（报告期 {period}），上季 {prev_yoy:+.1f}% 未加速，得分 {score:.0f}"
            )
        else:
            note = f"当季净利润同比 {yoy:+.1f}%（报告期 {period}），上季增速不可得，得分 {score:.0f}"
        return FactorScore(
            key=key,
            score=score,
            weight=self._weights[key],
            value={
                "period": period,
                "net_profit_yoy": yoy,
                "prev_net_profit_yoy": prev_yoy,
                "base_score": base,
                "accelerated": bool(accelerated),
                "accel_bonus": bonus,
            },
            note=note,
        )

    def _previous_quarter_yoy(self, ctx: _Ctx, periods: list[str], period: str) -> float | None:
        """上一报告期的净利润同比（优先 quarterly_report，兜底 fin_indicator 季度序列）。"""
        if period in periods:
            index = periods.index(period)
            if index + 1 < len(periods):
                previous = periods[index + 1]
                row = _pick_row(self._quarterly_report(previous), ctx.code)
                value = _num(row.get("net_profit_yoy")) if row else None
                if value is not None:
                    return value
                fallback = self._fin_yoy_map(ctx.code).get(previous)
                if fallback is not None:
                    return fallback
        return None

    def _fin_yoy_map(self, code: str) -> dict[str, float]:
        rows = _frame_rows(self._fin_indicator(code))
        out: dict[str, float] = {}
        for row in rows:
            period = str(row.get("date", "")).replace("-", "")[:8]
            value = _num(row.get("net_profit_yoy"))
            if len(period) == 8 and value is not None:
                out.setdefault(period, value)
        return out

    # ---- A：年度 EPS 增长（最近 3 个年报均值 + ROE 加成） ----

    def _factor_a(self, ctx: _Ctx) -> FactorScore:
        key = "A"
        rows = _frame_rows(self._fin_indicator(ctx.code))
        if not rows:
            return self._missing_factor(key, "财务指标 fin_indicator 无该标的记录")

        # **禁止未来函数**：只采用截至 asof 已披露的年报（年报法定披露截止 = 次年 4-30）。
        # report_periods() 已按披露截止日过滤，故用它的周期集合做白名单。
        disclosed = set(report_periods(ctx.asof))
        annual: list[tuple[str, dict]] = []
        for row in rows:
            date_text = str(row.get("date", "")).replace("-", "")
            if len(date_text) >= 8 and date_text[4:8] == "1231" and date_text[:8] in disclosed:
                annual.append((date_text[:8], row))
        if not annual:
            return self._missing_factor(key, f"截至 {ctx.asof} 无已披露的年报（1231）记录")

        annual.sort(key=lambda item: item[0], reverse=True)
        recent = list(reversed(annual[:3]))  # 最近 3 个年报，按时间升序展示

        year_scores: list[float] = []
        details: list[dict] = []
        for period, row in recent:
            yoy = _num(row.get("net_profit_yoy"))
            if yoy is None:
                continue
            roe = _num(row.get("roe"))
            score = map_a_growth(yoy)
            bonus = 10.0 if (roe is not None and roe >= I_ROE_HURDLE) else 0.0
            score = _clamp(score + bonus)
            year_scores.append(score)
            details.append({"period": period, "net_profit_yoy": yoy, "roe": roe, "score": score})

        if not year_scores:
            return self._missing_factor(key, "年报记录中 net_profit_yoy 全部为空")

        score = _clamp(sum(year_scores) / len(year_scores))
        yoy_text = "、".join(f"{item['period'][:4]}年 {item['net_profit_yoy']:+.1f}%" for item in details)
        roe_years = [item["period"][:4] for item in details if item["roe"] is not None and item["roe"] >= I_ROE_HURDLE]
        bonus_text = f"，{'/'.join(roe_years)} 年 ROE ≥ {I_ROE_HURDLE:.0f}% 各 +10" if roe_years else ""
        note = f"最近 {len(details)} 个年报净利润同比：{yoy_text}{bonus_text}，年度均分 {score:.1f}"
        return FactorScore(
            key=key,
            score=score,
            weight=self._weights[key],
            value={"annual": details, "years_used": len(details), "roe_hurdle": I_ROE_HURDLE},
            note=note,
        )

    # ---- N：新产品/新高（250 日位置 + 量能） ----

    def _factor_n(self, ctx: _Ctx) -> FactorScore:
        key = "N"
        frame = ctx.daily
        if frame is None or len(frame) < 2 or "close" not in frame.columns:
            return self._missing_factor(key, "日线不足 2 根，无法判断 250 日区间位置")

        high_series = frame["high"] if "high" in frame.columns else frame["close"]
        close = _num(frame["close"].iloc[-1])
        high_250 = _num(high_series.max())
        if close is None or high_250 is None or high_250 <= 0:
            return self._missing_factor(key, "日线最高价/收盘价为空")

        prior = high_series.iloc[:-1]
        prior_high = _num(prior.max()) if len(prior) else None
        distance = (high_250 - close) / high_250
        new_high = distance <= (1.0 - N_NEW_HIGH_DISCOUNT) + EPS  # 收盘价 ≥ 250日最高 × 0.98
        breakout = new_high or (prior_high is not None and close > prior_high)

        volumes = frame["volume"] if "volume" in frame.columns else None
        vol_ratio: float | None = None
        volume = _num(volumes.iloc[-1]) if volumes is not None else None
        if volumes is not None and len(volumes) > 1:
            base_window = volumes.iloc[:-1].tail(N_VOLUME_WINDOW)
            base_volume = _num(pd.to_numeric(base_window, errors="coerce").mean())
            if volume is not None and base_volume:
                vol_ratio = volume / base_volume

        score = map_n_position(distance, vol_ratio, breakout=breakout)
        volume_confirmed = vol_ratio is not None and vol_ratio > N_BREAKOUT_VOLUME_RATIO
        if new_high:
            note = f"收盘价 {close:.2f} 距 250 日最高 {high_250:.2f} 仅 {distance * 100:.2f}%（创新高区间），得分 {score:.0f}"
        else:
            note = f"收盘价 {close:.2f} 距 250 日最高 {high_250:.2f} 回撤 {distance * 100:.1f}%，得分 {score:.0f}"
        if breakout and volume_confirmed:
            note += f"；且放量突破（量比 50 日均量 {vol_ratio:.2f}× > {N_BREAKOUT_VOLUME_RATIO}）→ 取 max(得分, 90) = {score:.0f}"
        return FactorScore(
            key=key,
            score=score,
            weight=self._weights[key],
            value={
                "close": close,
                "high_250": high_250,
                "prior_high_250": prior_high,
                "distance_to_high": distance,
                "new_high": bool(new_high),
                "breakout": bool(breakout),
                "volume": volume,
                "vol_ratio_50d": vol_ratio,
                "bars": int(len(frame)),
            },
            note=note,
        )

    # ---- S：供给（流通盘 + 量能 + 股东户数） ----

    def _factor_s(self, ctx: _Ctx) -> FactorScore:
        key = "S"
        if self._is_historical(ctx.asof):
            return self._factor_s_historical(ctx)
        spot = ctx.spot or {}
        float_mv_raw = _num(spot.get("float_mv"))
        if float_mv_raw is None:
            return self._missing_factor(key, "快照 spot 无 float_mv（流通市值）")
        float_mv_yi = _to_yi(float_mv_raw)
        score = map_s_float_mv(float_mv_yi)

        turnover = _num(spot.get("turnover"))
        turnover_bonus = 0.0
        if turnover is not None and S_TURNOVER_LOW <= turnover <= S_TURNOVER_HIGH:
            turnover_bonus = 10.0
            score += turnover_bonus

        holders_chg, holders_period = self._holders_change(ctx)
        holders_bonus = 0.0
        if holders_chg is not None:
            if holders_chg < 0:
                holders_bonus = 10.0
            elif holders_chg > 0:
                holders_bonus = -10.0
        score = _clamp(score + holders_bonus)

        parts = [f"流通市值 {float_mv_yi:.1f} 亿→基础 {map_s_float_mv(float_mv_yi):.0f} 分"]
        if turnover_bonus:
            parts.append(f"换手率 {turnover:.2f}% 落在 {S_TURNOVER_LOW:.0f}%-{S_TURNOVER_HIGH:.0f}% → +10")
        elif turnover is not None:
            parts.append(f"换手率 {turnover:.2f}% 不在 {S_TURNOVER_LOW:.0f}%-{S_TURNOVER_HIGH:.0f}% 区间 → +0")
        if holders_chg is not None:
            direction = "减少（筹码集中）" if holders_chg < 0 else ("增加（筹码分散）" if holders_chg > 0 else "持平")
            parts.append(f"股东户数 {holders_period} 环比 {holders_chg:+.2f}% {direction} → {holders_bonus:+.0f}")
        note = "；".join(parts) + f"，合计 {score:.0f} 分"
        return FactorScore(
            key=key,
            score=score,
            weight=self._weights[key],
            value={
                "float_mv_raw": float_mv_raw,
                "float_mv_yi": float_mv_yi,
                "turnover": turnover,
                "turnover_bonus": turnover_bonus,
                "holders_chg_pct": holders_chg,
                "holders_period": holders_period,
                "holders_bonus": holders_bonus,
                "last": _num(spot.get("last")),
            },
            note=note,
        )

    def _holders_row(self, ctx: _Ctx) -> tuple[dict | None, str | None]:
        """最近一期已披露（相对 ``asof``）的股东户数行。"""
        for period in report_periods(ctx.asof):
            row = _pick_row(self._holders(period), ctx.code)
            if row is not None:
                return row, period
        return None, None

    def _holders_change(self, ctx: _Ctx) -> tuple[float | None, str | None]:
        row, period = self._holders_row(ctx)
        if row is None:
            return None, None
        return _num(row.get("holders_chg_pct")), period

    def _factor_s_historical(self, ctx: _Ctx) -> FactorScore:
        """S 因子的**历史回溯**路径（``asof`` < 最新交易日）：绝不使用 ``spot()``。

        - ``float_mv`` 近似 = ``当日收盘价 × 流通股本``，流通股本取最近已披露 ``holders().total_share``；
        - ``turnover`` 取当日**日线自带**列（数据层保证为百分数）；
        - 任一缺失 → 50 分中性 + ``risks`` 标注 ``"S：历史回溯缺当日快照"``，**不回退到 spot()**。
        """
        key = "S"
        frame = ctx.daily
        close: float | None = None
        turnover: float | None = None
        if frame is not None and len(frame):
            close = _num(frame["close"].iloc[-1]) if "close" in frame.columns else None
            if "turnover" in frame.columns:
                turnover = _num(frame["turnover"].iloc[-1])

        row, holders_period = self._holders_row(ctx)
        total_share = _num(row.get("total_share")) if row else None
        holders_chg = _num(row.get("holders_chg_pct")) if row else None

        if close is None or turnover is None or total_share is None:
            factor = self._missing_factor(
                key,
                f"历史回溯缺当日快照/流通股本（收盘={close}、换手率={turnover}、流通股本={total_share}）",
                risk="S：历史回溯缺当日快照",
            )
            return factor

        float_mv_raw = close * total_share
        float_mv_yi = _to_yi(float_mv_raw)
        score = map_s_float_mv(float_mv_yi)

        turnover_bonus = 0.0
        if S_TURNOVER_LOW <= turnover <= S_TURNOVER_HIGH:
            turnover_bonus = 10.0
            score += turnover_bonus
        holders_bonus = 0.0
        if holders_chg is not None:
            if holders_chg < 0:
                holders_bonus = 10.0
            elif holders_chg > 0:
                holders_bonus = -10.0
        score = _clamp(score + holders_bonus)

        parts = [
            f"历史回溯（asof {ctx.asof} < 最新交易日）：以当日收盘 {close:.2f} 元 × 流通股本 "
            f"{total_share:.0f} 股 ≈ 流通市值 {float_mv_yi:.1f} 亿（未使用实时快照）→ 基础 {map_s_float_mv(float_mv_yi):.0f} 分"
        ]
        parts.append(f"当日换手率 {turnover:.2f}% → {turnover_bonus:+.0f}")
        if holders_chg is not None:
            direction = "减少（筹码集中）" if holders_chg < 0 else ("增加（筹码分散）" if holders_chg > 0 else "持平")
            parts.append(f"股东户数 {holders_period} 环比 {holders_chg:+.2f}% {direction} → {holders_bonus:+.0f}")
        return FactorScore(
            key=key,
            score=score,
            weight=self._weights[key],
            value={
                "path": "historical_daily",
                "float_mv_raw": float_mv_raw,
                "float_mv_yi": float_mv_yi,
                "close": close,
                "total_share": total_share,
                "turnover": turnover,
                "turnover_bonus": turnover_bonus,
                "holders_chg_pct": holders_chg,
                "holders_period": holders_period,
                "holders_bonus": holders_bonus,
                "last": close,
            },
            note="；".join(parts) + f"，合计 {score:.0f} 分",
        )

    # ---- L：相对强度（RS = 个股近 6 月涨幅 − 指数近 6 月涨幅） ----

    def _factor_l(self, ctx: _Ctx) -> FactorScore:
        """L 因子（§6.1）：RS = 个股近 6 月涨幅 − 指数近 6 月涨幅。

        **样本口径三处写死一致**（verifier CR-16 要求，勿改其一）：
        ``n`` = 该票所属行业内**可算 RS 的成分股数，含自身**（``industry_constituents`` ∪ {自身}，
        按 6 位代码去重、截断 ``L_MAX_INDUSTRY_PEERS``；日线不足算不出 RS 的同业剔除）；
        ``value["peers"]`` / 门槛判定用 ``n``，``percentile`` 分母用 ``n - 1``（竞争者数，自身不计入），
        ``value["competitors"]`` = ``n - 1``。
        **``n < L_MIN_INDUSTRY_PEERS(10)`` 或行业不可得 → 强制降级为绝对 RS 分档**，并在 ``risks`` 标注
        （契约 L10：退化不得静默）。**分值严格等于 §6.1 分档值**（不因绝对 RS 而钳制）；
        买入的绝对强度由 §6.2 的 ``abs_rs > 0`` 独立前置把守。
        """
        key = "L"
        stock_return = self._stock_return(ctx.code, ctx.asof)
        index_return = self._index_return(ctx.asof)
        if stock_return is None or index_return is None:
            detail = "日线不足 2 根" if stock_return is None else "指数日线不可得"
            return self._missing_factor(key, f"{detail}，无法计算近 6 月相对强度")
        abs_rs = (stock_return - index_return) * 100.0
        industry = self._industry(ctx.code)
        instrument = self._instrument_type(ctx.code)
        peer_rs, capped = (
            self._industry_rs(ctx.code, ctx.asof, index_return, industry) if industry else ([], False)
        )
        peers_total = len(peer_rs)
        competitors = max(0, peers_total - 1)
        peers_gate = competitors >= L_MIN_COMPETITORS
        common = {
            "stock_return_6m": stock_return,
            "index_return_6m": index_return,
            "rs": abs_rs,                       # 兼容旧字段名
            "abs_rs": abs_rs,
            "industry": industry,
            "peers": peers_total,               # 兼容旧字段名（= 含自身的可算 RS 成分股数）
            "peers_total": peers_total,
            "competitors": competitors,
            "gate_basis": "competitors",
            "peers_gate": peers_gate,
            "tie_policy": "strictly_lower",     # 并列者不算"被击败"，候选股不得因打平获益
            "peers_capped": capped,
            "cap": L_MAX_INDUSTRY_PEERS,
            "instrument_type": instrument,
            "close": self._last_close(ctx),
            "abs_rs_gate": abs_rs > 0.0,
        }

        if peers_gate:
            below = sum(1 for value in peer_rs if value < abs_rs - EPS)
            percentile = below / competitors if competitors else 0.0
            score = map_l_percentile(percentile)
            rank_pct = max(0.0, (1.0 - percentile)) * 100.0
            note = (
                f"近 6 月涨幅 {stock_return * 100:+.1f}% − 指数 {index_return * 100:+.1f}% = RS {abs_rs:+.1f}%，"
                f"在「{industry}」{peers_total} 只可算 RS 的成分股（可比较对手 {competitors} 只）中"
                f"强于 {percentile * 100:.0f}% 的样本（约前 {rank_pct:.0f}%，并列不计胜），"
                f"得分 {score:.0f}（板块内相对强度；绝对强度另由 abs_rs 前置把守）"
            )
            if capped:
                note += f"；候选集已按代码升序截断至 {L_MAX_INDUSTRY_PEERS}（peers_capped=True）"
            return FactorScore(
                key=key,
                score=score,
                weight=self._weights[key],
                value={**common, "percentile": percentile, "path": "industry_rank",
                       "abs_rs_path": "industry_rank"},
                note=note,
            )

        # ---- 降级：绝对 RS 分档（§6.1：可比较对手 < 10 只，或行业不可得）----
        # 文案区分：**ETF 本就没有行业分类**（不是数据缺陷），余下才是"行业归属不可得"。
        if not industry and instrument == "etf":
            reason = "ETF 无行业归属"
            degrade_text = "ETF 无行业归属，L 按绝对 RS 分档"
        elif not industry:
            reason = "行业归属不可得"
            degrade_text = "行业归属不可得，已降级为**绝对 RS 分档**（板块内相对强度不可用）"
        else:
            reason = (
                f"行业「{industry}」可比较对手仅 {competitors} 只"
                f"（可算 RS 成分股 {peers_total} 只，< 门槛 {L_MIN_COMPETITORS}）"
            )
            degrade_text = f"{reason}，已降级为**绝对 RS 分档**（板块内相对强度不可用）"
        score = map_l_absolute(abs_rs)
        note = (
            f"近 6 月涨幅 {stock_return * 100:+.1f}% − 指数 {index_return * 100:+.1f}% = RS {abs_rs:+.1f}%；"
            f"{degrade_text} → {score:.0f} 分"
        )
        return FactorScore(
            key=key,
            score=score,
            weight=self._weights[key],
            value={
                **common,
                "path": "absolute_rs",
                "abs_rs_path": "absolute_rs",
                "percentile": None,          # schema 稳定：降级路径也保留该键（消费方不得 KeyError）
                "risk": f"L：{reason}，已降级为绝对 RS 分档（本分值仅为绝对强度，板块内相对强度不可用）",
            },
            note=note,
        )

    def _last_close(self, ctx: _Ctx) -> float | None:
        if ctx.daily is not None and "close" in ctx.daily.columns and len(ctx.daily):
            return _num(ctx.daily["close"].iloc[-1])
        return None

    @staticmethod
    def _instrument_type(code: str) -> str:
        """``"etf"``（``5xxxxx`` / ``1xxxxx``）或 ``"stock"``。

        ETF/LOF **本身没有行业分类**，``industry_of`` 返回空串属正常语义而非数据缺陷，
        故 L 因子的文案要对两者区分（分值规则不变，仍走绝对 RS 分档）。
        """
        normalized = _norm_code(code)
        return "etf" if normalized[:1] in ("5", "1") else "stock"

    def _industry_rs(
        self, code: str, asof: str, index_return: float, industry: str
    ) -> tuple[list[float], bool]:
        """行业成分股近 6 月 RS 列表（**含自身**；算不出 RS 的同业剔除）。

        返回 ``(rs_list, capped)``。候选集 = ``industry_constituents(industry)`` ∪ {自身}，
        按 6 位代码去重、**先按代码升序再截断至** ``L_MAX_INDUSTRY_PEERS`` —— 保证大行业下的
        子集是**确定且可复现**的（不引入"未定义排序子集"的偏差）；``capped=True`` 会被暴露到
        ``value["peers_capped"]`` 供审计。
        """
        constituents = self._try(self.ds.industry_constituents, industry) or []
        codes: list[str] = []
        for item in constituents:
            normalized = _norm_code(item)
            if normalized and normalized not in codes:
                codes.append(normalized)
        if _norm_code(code) not in codes:
            codes.append(_norm_code(code))
        codes.sort()
        capped = len(codes) > L_MAX_INDUSTRY_PEERS
        out: list[float] = []
        for peer in codes[:L_MAX_INDUSTRY_PEERS]:
            peer_return = self._stock_return(peer, asof)
            if peer_return is None:
                continue
            out.append((peer_return - index_return) * 100.0)
        return out, capped

    # ---- I：机构认同度 ----

    def _factor_i(self, ctx: _Ctx) -> FactorScore:
        """I 因子（§6.1）：机构合计持股比例 + 环比变动。

        **CR-7 asof 纪律**：只用 ``asof`` 时点**已披露**的期次
        （``report_date`` 缺失，或该期在 ``asof`` 时尚未披露）→ **50 分中性** + ``risks`` 两条标注，
        **绝不回落到"当期快照"**（那会把当下的持股结构当作历史事实，且 akshare
        ``stock_main_stock_holder`` 只提供当期，无历史期次可查）。
        """
        key = "I"
        frame, meta = self._top_holders(ctx.code, ctx.asof)
        rows = _frame_rows(frame)
        report_date = str(meta.get("report_date") or "")[:10]
        if not rows:
            return self._missing_factor(
                key,
                f"asof={ctx.asof} 时点无已披露的十大股东期次（数据源仅提供当期）",
                risk="I：asof 时点无对应期次股东数据（数据源仅提供当期）",
            )
        if not report_date or report_date in ("nan", "None", "NaT") or report_date > str(ctx.asof)[:10]:
            return self._missing_factor(
                key,
                f"十大股东报告期 {report_date or '未知'} 不在 asof={ctx.asof} 的可披露范围内",
                risk="I：asof 时点无对应期次股东数据（数据源仅提供当期）",
            )

        institutions = [
            row for row in rows
            if _is_institution(row.get("holder_type"), row.get("holder_name"))
        ]
        if not institutions:
            return self._missing_factor(key, "十大股东中未识别出机构投资者")

        ratio_sum = 0.0
        ratio_count = 0
        for row in institutions:
            value = _num(row.get("ratio"))
            if value is not None:
                ratio_sum += value
                ratio_count += 1
        if ratio_count == 0:
            return self._missing_factor(key, "机构持股比例 ratio 全为空")
        ratio = _to_ratio(ratio_sum)

        score = map_i_ratio(ratio)
        net_change = 0.0
        has_change = False
        ups = downs = 0
        for row in institutions:
            change = _num(row.get("change"))
            if change is None:
                continue
            has_change = True
            net_change += change
            if change > 0:
                ups += 1
            elif change < 0:
                downs += 1
        change_bonus = 0.0
        if has_change:
            if net_change > 0 or (net_change == 0 and ups > downs):
                change_bonus = 10.0
            elif net_change < 0 or downs > ups:
                change_bonus = -10.0
        score = _clamp(score + change_bonus)

        note = (
            f"{len(institutions)} 家机构合计持股 {ratio * 100:.2f}%（原始合计 {ratio_sum:.2f}）→ 基础 {map_i_ratio(ratio):.0f} 分"
        )
        if has_change:
            direction = "增持" if change_bonus > 0 else ("减持" if change_bonus < 0 else "持平")
            note += f"；环比{direction}（净变动 {net_change:+.0f} 股，增持 {ups} 家 / 减持 {downs} 家）→ {change_bonus:+.0f}"
        else:
            note += "；环比变动数据缺失 → +0"
        note += f"，合计 {score:.0f} 分"
        return FactorScore(
            key=key,
            score=score,
            weight=self._weights[key],
            value={
                "institution_count": len(institutions),
                "ratio_sum_raw": ratio_sum,
                "ratio": ratio,
                "net_change": net_change if has_change else None,
                "increase_count": ups,
                "decrease_count": downs,
                "change_bonus": change_bonus,
                "report_date": report_date,
                "announce_date": str(meta.get("announce_date") or "")[:10] or None,
                "is_historical": bool(meta.get("is_historical")),
                "top_holders_asof": str(ctx.asof),
            },
            note=note,
        )

    # ---- M：市场方向（同一 asof 缓存） ----

    def market_direction(self, asof: str) -> FactorScore:
        """M 因子（不随个股变化），按 ``asof`` 缓存。"""
        key = "M"
        asof_text = str(asof)
        if asof_text in self._m_cache:
            return self._m_cache[asof_text]

        breadth = self._breadth(asof_text)
        if breadth is None:
            factor = self._missing_factor(key, "市场宽度 market_breadth 不可得")
        else:
            score = map_m_breadth(breadth)
            ratio_text = "—"
            advancers = _num(breadth.get("advancers"))
            decliners = _num(breadth.get("decliners"))
            if advancers is not None and decliners is not None:
                ratio_text = f"{advancers / decliners:.2f}" if decliners else "∞"
            change_20d = _num(breadth.get("pct_chg_20d"))
            change_text = f"{change_20d:+.2f}%" if change_20d is not None else "不可得"
            limit_up = _num(breadth.get("limit_up_count"))
            limit_up_text = f"{limit_up:.0f}" if limit_up is not None else "不可得"
            note = (
                f"基础 20 分；指数 "
                f"{'在' if breadth.get('above_ma50') else '跌破'} 50 日线（{'+20' if breadth.get('above_ma50') else '+0'}）、"
                f"{'在' if breadth.get('above_ma200') else '跌破'} 200 日线（{'+20' if breadth.get('above_ma200') else '+0'}）；"
                f"20 日涨幅 {change_text}；涨跌家数比 {ratio_text}；涨停 {limit_up_text} 家"
                f" → 合计 {score:.0f} 分"
            )
            factor = FactorScore(
                key=key,
                score=score,
                weight=self._weights[key],
                value=dict(breadth),
                note=note,
            )
        self._m_cache[asof_text] = factor
        return factor

    # ---- 单票评分 ----

    def score(self, code: str, asof: str) -> CanslimResult:
        """对单只标的打分；因子级数据缺失一律降级为 50 分并记入 ``risks``，不抛异常。

        仅当该标的**完全没有任何行情数据**（日线与快照/快照快照均不可得）时抛
        :class:`StrategyDataError`。
        """
        code = str(code).strip()
        asof_text = str(asof)
        daily = self._daily_window(code, asof_text, N_WINDOW)
        spot = self._spot_row(code)
        quote = self._quote(code, asof_text)
        if daily is None and not _has_market_info(quote, spot):
            raise StrategyDataError(
                f"标的 {code} 无任何行情数据（asof={asof_text}, offline={self.offline}），无法评分"
            )

        ctx = _Ctx(code=code, asof=asof_text, daily=daily, spot=spot, quote=quote)
        m_factor = self.market_direction(asof_text)

        factors: list[FactorScore] = []
        risks: list[str] = []
        reasons: list[str] = []
        for key in FACTORS:
            if key == "M":
                factor = m_factor
            else:
                factor = getattr(self, f"_factor_{key.lower()}")(ctx)
            factors.append(factor)
            if factor.value.get("missing"):
                risks.append(f"{key}数据缺失")
            else:
                reasons.append(f"{key}：{factor.note}")
            extra_risk = factor.value.get("risk")
            if extra_risk:
                risks.append(str(extra_risk))       # 契约 L10：退化不得静默

        total = 0.0
        for factor in factors:
            total += factor.score * factor.weight

        m_score = m_factor.score
        m_missing = bool(m_factor.value.get("missing"))
        l_factor = next((item for item in factors if item.key == "L"), None)
        l_score = l_factor.score if l_factor is not None else NEUTRAL_SCORE
        l_missing = bool(l_factor.value.get("missing")) if l_factor is not None else True
        abs_rs = _num(l_factor.value.get("abs_rs")) if l_factor is not None else None
        signal, signal_reason = _decide_signal(
            total, m_score, l_score, self.canslim, m_missing=m_missing, abs_rs=abs_rs
        )

        if signal == "short":
            risks.append(f"总分 {total:.2f} ≤ 做空门槛 {self.canslim.short_threshold:.1f}：仅两融标的可融券做空，需券商券源与保证金")
        if m_missing:
            risks.append("市场方向(M)数据缺失，按审慎原则不放行买入")
        elif m_score < 50.0:
            risks.append(f"市场方向偏弱：M={m_score:.0f} < 50，系统性风险偏高")
        if l_missing:
            risks.append("相对强度(L)数据缺失，无法确认是否跑赢指数")
        elif l_score < 60.0:
            risks.append(f"相对强度不足：L={l_score:.0f} < 60，未跑赢指数")
        if abs_rs is None:
            risks.append("L：绝对 RS 不可得（缺指数或个股日线），买入前置按 fail-closed 处理")
        elif abs_rs <= 0.0:
            risks.append(f"L：绝对 RS {abs_rs:+.2f}% ≤ 0，未跑赢指数（买入前置不通过）")
        if quote is not None and quote.get("suspended"):
            risks.append("标的当前处于停牌状态，无法成交")

        name = self._resolve_name(code, ctx)

        return CanslimResult(
            code=code,
            name=name,
            asof=asof_text,
            total=total,
            factors=factors,
            signal=signal,
            reasons=[signal_reason] + reasons,
            risks=risks,
        )

    def _resolve_name(self, code: str, ctx: _Ctx) -> str:
        for source in (ctx.quote, ctx.spot):
            if isinstance(source, dict):
                name = str(source.get("name") or "").strip()
                if name:
                    return name
        return code

    def score_many(self, codes: list[str], asof: str) -> list[CanslimResult]:
        """批量打分，按 ``total`` 降序；单票失败**不抛异常**，给 ``total=0`` 并记入 ``risks``。"""
        results: list[CanslimResult] = []
        for code in codes:
            try:
                results.append(self.score(code, asof))
            except Exception as exc:  # 单票失败不得影响整批
                results.append(self._failed_result(str(code).strip(), str(asof), exc))
        results.sort(key=lambda item: (-item.total, item.code))
        return results

    def _failed_result(self, code: str, asof: str, exc: Exception) -> CanslimResult:
        """取数/评分失败兜底：``total=0``、``factors=[]``、``signal="avoid"``。

        注意：失败**不套用 §6.2 第 1 条**（否则数据故障会被误判为做空候选）。
        """
        name = code
        spot = self._spot_row(code)
        quote = self._quote(code, asof)
        for source in (quote, spot):
            if isinstance(source, dict) and str(source.get("name") or "").strip():
                name = str(source["name"]).strip()
                break
        return CanslimResult(
            code=code,
            name=name,
            asof=asof,
            total=0.0,
            factors=[],
            signal="avoid",
            reasons=[f"评分失败：{type(exc).__name__}: {exc}（七因子均未取到数据，总分记 0）"],
            risks=[
                f"数据获取失败，无法评分：{type(exc).__name__}",
                "该票总分记为 0，不纳入买入/做空候选，请先确认代码与数据可用性",
            ],
        )


# ---------------------------------------------------------------------------
# 排序与建议（INTERFACES.md §6）
# ---------------------------------------------------------------------------

_ACTION_BY_SIGNAL: dict[str, tuple[str, str]] = {
    "buy": ("open_long", "long"),
    "short": ("open_short", "short"),
    "watch": ("watch", "none"),
    "avoid": ("close", "none"),
}


def _is_failed(result: CanslimResult) -> bool:
    """是否为「取数失败」的兜底结果（INTERFACES.md §6.2：``factors == []`` 或 ``total == 0`` 且 ``risks`` 非空）。"""
    if not result.factors:
        return True
    return result.total == 0.0 and bool(result.risks)


def rank(results: list[CanslimResult], cfg: AppConfig) -> dict:
    """把打分结果分组排序。

    - ``buys``：``signal == "buy"`` 且 ``total >= buy_threshold``，按 ``total`` 降序
    - ``shorts``：``signal == "short"`` 且 ``total <= short_threshold``，按 ``total`` 升序；
      **取数失败票一律排除**（契约 §6.2：失败≠看空）
    - ``watch`` / ``avoid``：按 ``signal`` 分组（watch 降序、avoid 升序）
    - ``top``：全部结果按 ``total`` 降序
    - ``asof``：结果对应的交易日（空列表 → ``""``）
    """
    buy_line = cfg.canslim.buy_threshold
    short_line = cfg.canslim.short_threshold

    buys = sorted(
        (item for item in results if item.signal == "buy" and item.total >= buy_line - EPS),
        key=lambda item: (-item.total, item.code),
    )
    shorts = sorted(
        (
            item for item in results
            if item.signal == "short" and item.total <= short_line + EPS and not _is_failed(item)
        ),
        key=lambda item: (item.total, item.code),
    )
    watch = sorted(
        (item for item in results if item.signal == "watch"),
        key=lambda item: (-item.total, item.code),
    )
    avoid = sorted(
        (item for item in results if item.signal == "avoid"),
        key=lambda item: (item.total, item.code),
    )
    top = sorted(results, key=lambda item: (-item.total, item.code))
    return {
        "buys": list(buys),
        "shorts": list(shorts),
        "watch": list(watch),
        "avoid": list(avoid),
        "top": list(top),
        "asof": results[0].asof if results else "",
    }


def recommend(results: list[CanslimResult], cfg: AppConfig) -> list[dict]:
    """把打分结果转成可执行建议（字段见 INTERFACES.md §6）。

    - ``entry``：取打分结果中记录的当日收盘价（``value["close"]`` / ``value["last"]``）；
      **取不到价格时该条降级为 ``action="watch"``、``side="none"``** 并记入 ``risks``
      （避免下游 ``engine.build_plan`` 拿着 ``entry=0`` 算出 qty=0 或除零）
    - ``stop_loss``：多头 ``entry × (1 + risk.stop_loss_pct)``；空头 ``entry × (1 - risk.stop_loss_pct)``
    - ``take_profit``：多头 ``entry × (1 + risk.take_profit_pct)``；空头 ``entry × (1 - risk.take_profit_pct)``
    - ``position_pct``：可执行信号（open_long/open_short）取 ``risk.max_position_pct``，其余 0.0
    - ``confidence``：``|total - 50| / 50``，截断到 ``[0, 1]``
    """
    risk = cfg.risk
    stop_loss_pct = risk.stop_loss_pct
    take_profit_pct = risk.take_profit_pct
    max_position_pct = risk.max_position_pct

    out: list[dict] = []
    for item in results:
        action, side = _ACTION_BY_SIGNAL.get(item.signal, ("watch", "none"))
        entry = _entry_of(item)
        lacks_price = entry <= 0.0
        if lacks_price:
            # 无收盘价 → 不可执行，降级为观察
            action, side = "watch", "none"
        if side == "short":
            stop_loss = entry * (1.0 - stop_loss_pct)
            take_profit = entry * (1.0 - take_profit_pct)
        else:
            stop_loss = entry * (1.0 + stop_loss_pct)
            take_profit = entry * (1.0 + take_profit_pct)
        confidence = _clamp(abs(item.total - 50.0) / 50.0, 0.0, 1.0)
        risks = list(item.risks)
        if lacks_price:
            risks.append("缺少收盘价，无法计算入场价")
        out.append({
            "code": item.code,
            "name": item.name,
            "action": action,
            "side": side,
            "score": item.total,
            "confidence": confidence,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "position_pct": float(max_position_pct) if action in ("open_long", "open_short") else 0.0,
            "reasons": [
                f"建议动作 {action}（信号 {item.signal}，总分 {item.total:.2f}）"
            ] + list(item.reasons),
            "risks": risks,
            "factors": {factor.key: factor.score for factor in item.factors},
        })
    return out


def _entry_of(result: CanslimResult) -> float:
    """从因子 ``value`` 中提取最新收盘价作为建仓参考价（契约签名不含 DataSource）。"""
    for key in FACTORS:
        factor = result.factor(key)
        if factor is None:
            continue
        for field in ("close", "last", "price"):
            value = _num(factor.value.get(field)) if isinstance(factor.value, dict) else None
            if value is not None and value > 0:
                return float(value)
    return 0.0
