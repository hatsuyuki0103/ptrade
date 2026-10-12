# -*- coding: utf-8 -*-
"""tests/test_strategy.py — T3 策略层测试（对应 test-spec TC-S-01 .. TC-S-23）。

两条测试通道，**全部离线、绝不联网**：

1. **纯函数通道**：``map_*`` 打分函数逐点断言 ``INTERFACES.md`` §6.1 的每一个边界值，
   零 IO、零夹具，边界语义一目了然。
2. **组装通道**：``StubDataSource``（实现 DataSource 同名方法的零 IO 假数据源）驱动
   ``CanslimScorer`` 走完整取数→打分→信号→排序→建议链路；夹具通道再用
   ``DataSource(offline=True)`` 跑真实强票/中性票/弱票。

注：本文件只依赖 ``ptrade.config`` / ``ptrade.datasource`` / ``ptrade.strategy``，
夹具由 T1 生成，缺失即测试失败（不联网、不跳过）。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pandas as pd
import pytest

from ptrade.config import (
    AccountConfig,
    AppConfig,
    CANSLIMConfig,
    FeeConfig,
    MarginConfig,
    RiskConfig,
)
from ptrade.datasource import DataSource
from ptrade.strategy import (
    FACTORS,
    CanslimResult,
    CanslimScorer,
    FactorScore,
    StrategyDataError,
    _decide_signal,
    map_a_growth,
    map_c_growth,
    map_i_ratio,
    map_l_absolute,
    map_l_percentile,
    map_m_breadth,
    map_n_position,
    map_s_float_mv,
    rank,
    recommend,
    report_periods,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ASOF = "2025-10-09"                       # 真实夹具（纯真实行情）的评分时点
SCENARIO_ASOF = "2025-10-09"              # 剧本夹具的评分时点（须使最新已披露报告期 = 20250630）
STRONG, NEUTRAL, WEAK = "600519", "002594", "000001"
UNKNOWN = "999999"


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def make_cfg(**canslim_overrides) -> AppConfig:
    """显式构造默认配置（不读 config.json，避免被 CLI init 写出的文件污染）。"""
    return AppConfig(
        account=AccountConfig(),
        fee=FeeConfig(),
        margin=MarginConfig(),
        risk=RiskConfig(),
        canslim=CANSLIMConfig(**canslim_overrides),
        root=str(REPO_ROOT),
    )


def daily_frame(closes, *, highs=None, volumes=None, end=ASOF, start=None) -> pd.DataFrame:
    """构造升序日线：``end`` 为最后一根 K 线日期（默认 = 评分时点）。"""
    count = len(closes)
    if start is not None:
        dates = pd.bdate_range(start=start, periods=count).strftime("%Y-%m-%d")
    else:
        dates = pd.bdate_range(end=end, periods=count).strftime("%Y-%m-%d")
    highs = list(highs) if highs is not None else list(closes)
    volumes = list(volumes) if volumes is not None else [1_000_000] * count
    return pd.DataFrame({
        "date": dates,
        "open": list(closes),
        "high": highs,
        "low": list(closes),
        "close": list(closes),
        "volume": volumes,
        "amount": [c * v for c, v in zip(closes, volumes)],
        "pct_chg": [0.0] * count,
        "turnover": [1.0] * count,
    })


def quarterly_frame(code, yoy, period="20250630", **extra) -> pd.DataFrame:
    row = {
        "code": code,
        "name": f"票{code}",
        "eps": 1.0,
        "revenue": 1e9,
        "revenue_yoy": 10.0,
        "revenue_qoq": 5.0,
        "net_profit": 1e8,
        "net_profit_yoy": yoy,
        "net_profit_qoq": 5.0,
        "bps": 10.0,
        "roe": 15.0,
        "ocf_ps": 1.0,
        "gross_margin": 40.0,
        "industry": "白酒",
        "announce_date": period,
    }
    row.update(extra)
    return pd.DataFrame([row])


def fin_frame(rows) -> pd.DataFrame:
    """rows = [(date, net_profit_yoy, roe), ...]"""
    return pd.DataFrame([
        {
            "date": date,
            "eps": 1.0,
            "eps_deducted": 1.0,
            "bps": 10.0,
            "roe": roe,
            "roe_weighted": roe,
            "revenue_yoy": 10.0,
            "net_profit_yoy": yoy,
            "gross_margin": 40.0,
            "debt_ratio": 30.0,
            "ocf_ps": 1.0,
        }
        for date, yoy, roe in rows
    ])


def spot_frame(code, *, float_mv, turnover, last=100.0, name="测试票") -> pd.DataFrame:
    return pd.DataFrame([{
        "code": code,
        "name": name,
        "last": last,
        "pct_chg": 1.0,
        "open": last,
        "high": last,
        "low": last,
        "prev_close": last,
        "volume": 1_000_000,
        "amount": 1e8,
        "turnover": turnover,
        "pe_ttm": 20.0,
        "pb": 3.0,
        "total_mv": float_mv,
        "float_mv": float_mv,
        "limit_up": last * 1.1,
        "limit_down": last * 0.9,
    }])


def holders_frame(code, chg_pct, period="20250630") -> pd.DataFrame:
    return pd.DataFrame([{
        "code": code,
        "name": "测试票",
        "holders": 100000,
        "holders_prev": 110000,
        "holders_chg_pct": chg_pct,
        "avg_hold_value": 1e5,
        "total_mv": 1e11,
        "total_share": 1e9,
    }])


def top_holders_frame(code, rows, *, report_date="2025-06-30", announce_date="2025-08-13") -> pd.DataFrame:
    """rows = [(holder_name, holder_type, ratio, change), ...]（ratio 为**百分数**）。"""
    return pd.DataFrame([
        {
            "code": code,
            "holder_name": name,
            "holder_type": holder_type,
            "shares": 1e7,
            "ratio": ratio,
            "change": change,
            "report_date": report_date,
            "announce_date": announce_date,
        }
        for name, holder_type, ratio, change in rows
    ])


BREADTH_STRONG = {
    "index_close": 3200.0,
    "index_ma50": 3100.0,
    "index_ma200": 3000.0,
    "above_ma50": True,
    "above_ma200": True,
    "pct_chg_1d": 0.5,
    "pct_chg_20d": 6.0,
    "advancers": 3200,
    "decliners": 1200,
    "limit_up_count": 60,
    "limit_down_count": 3,
}


class StubDataSource:
    """零 IO 假数据源：只实现策略层用到的 DataSource 同名方法。

    - 传 ``None`` 的方法 → 返回空 DataFrame（等价「有接口但无数据」）。
    - ``fail`` 集合中的方法 → 抛 ``RuntimeError``（等价「取数失败/离线缺夹具」）。
    - ``latest`` = ``latest_trade_date()`` 的返回值（默认 ``ASOF``，即 asof == 最新交易日 → 实时路径）。
    """

    def __init__(self, *, daily=None, spot=None, quote=None, quarterly=None, fin=None,
                 holders=None, top_holders=None, industry=None, constituents=None,
                 index_daily=None, breadth=None, fail=(), name="测试票", latest=ASOF,
                 th_report_date="2025-06-30", th_announce_date="2025-08-13"):
        self._daily = daily or {}
        self._spot = spot or {}
        self._quote = quote or {}
        self._quarterly = quarterly or {}
        self._fin = fin or {}
        self._holders = holders or {}
        self._top_holders = top_holders or {}
        self._industry = industry or {}
        self._constituents = constituents or {}
        self._index_daily = index_daily or {}
        self._breadth = breadth or {}
        self._fail = set(fail)
        self._name = name
        self._latest = latest
        self._th_report_date = th_report_date
        self._th_announce_date = th_announce_date
        self.calls: dict[str, int] = {}

    def latest_trade_date(self):
        self._hit("latest_trade_date")
        return self._latest

    def _hit(self, key: str):
        self.calls[key] = self.calls.get(key, 0) + 1
        if key in self._fail:
            raise RuntimeError(f"stub: {key} 取数失败（离线夹具缺失）")

    def daily(self, code, start, end, adjust="qfq"):
        self._hit(f"daily:{code}")
        frame = self._daily.get(code)
        return pd.DataFrame() if frame is None else frame.copy()

    def spot(self, codes=None):
        self._hit("spot")
        wanted = list(codes) if codes else list(self._spot)
        frames = [self._spot[c] for c in wanted if c in self._spot]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def quote(self, code, asof):
        self._hit(f"quote:{code}")
        if code in self._quote:
            return dict(self._quote[code])
        frame = self._spot.get(code)
        if frame is None:
            return {}
        row = frame.iloc[0].to_dict()
        return {
            "code": code,
            "name": row.get("name", self._name),
            "last": float(row.get("last", 0.0)),
            "prev_close": float(row.get("prev_close", 0.0)),
            "limit_up": float(row.get("limit_up", 0.0)),
            "limit_down": float(row.get("limit_down", 0.0)),
            "suspended": False,
        }

    def quarterly_report(self, period):
        self._hit(f"quarterly:{period}")
        frame = self._quarterly.get(period)
        return pd.DataFrame() if frame is None else frame.copy()

    def fin_indicator(self, code, start_year):
        self._hit(f"fin:{code}")
        frame = self._fin.get(code)
        return pd.DataFrame() if frame is None else frame.copy()

    def holders(self, period):
        self._hit(f"holders:{period}")
        frame = self._holders.get(period)
        return pd.DataFrame() if frame is None else frame.copy()

    def top_holders(self, code, asof=None):
        """§4 新签名：带 ``asof`` 与 ``attrs["top_holders_meta"]``（report_date / is_historical）。

        与真实数据源同语义：``asof`` 早于该期披露日 → 返回**空表**（无前视，不回落未来期次）。
        """
        self._hit(f"top_holders:{code}")
        frame = self._top_holders.get(code)
        if frame is None:
            empty = pd.DataFrame()
            empty.attrs["top_holders_meta"] = {
                "requested_asof": str(asof)[:10] if asof else None,
                "report_date": None, "announce_date": None, "is_historical": False, "periods": 0,
            }
            return empty
        out = frame.copy()
        if "report_date" in out.columns:
            report_date = str(out["report_date"].iloc[0])[:10]
        else:
            report_date = self._th_report_date
        if "announce_date" in out.columns:
            announce_date = str(out["announce_date"].iloc[0])[:10]
        else:
            announce_date = self._th_announce_date
        eligible = True if not asof else announce_date <= str(asof)[:10]
        out.attrs["top_holders_meta"] = {
            "requested_asof": str(asof)[:10] if asof else None,
            "report_date": report_date if eligible else None,
            "announce_date": announce_date if eligible else None,
            "disclosed_on": announce_date,
            "is_historical": bool(eligible),
            "periods": 1,
        }
        return out if eligible else pd.DataFrame()

    def industry_of(self, code):
        self._hit(f"industry:{code}")
        value = self._industry.get(code)
        if value is None:
            raise RuntimeError("stub: 行业归属不可得")
        return value

    def industry_constituents(self, industry):
        self._hit(f"constituents:{industry}")
        return list(self._constituents.get(industry, []))

    def index_daily(self, index_code, start, end):
        self._hit(f"index:{index_code}")
        frame = self._index_daily.get(index_code)
        return pd.DataFrame() if frame is None else frame.copy()

    def market_breadth(self, asof):
        self._hit("breadth")
        if not self._breadth:
            raise RuntimeError("stub: 市场宽度不可得")
        return dict(self._breadth)


def make_scorer(ds, cfg=None) -> CanslimScorer:
    return CanslimScorer(cfg or make_cfg(), ds, offline=True)


def patched_scorer(
    scores: dict, cfg=None, *, entry=100.0, code=STRONG, abs_rs=5.0, l_path="absolute_rs",
) -> CanslimScorer:
    """把七因子强制为给定分值，用于端到端验证 §6.2 的五条信号分支。

    权重和为 1 → 总分 = 该分值本身，便于精确构造 ``total`` 落档。
    ``abs_rs`` 注入 L 因子的 ``value``（§6.2 买入独立前置；默认 +5.0 表示"跑赢指数"）。
    """
    cfg = cfg or make_cfg()
    ds = StubDataSource(daily={code: daily_frame([entry] * 5, end=ASOF)}, name="构造票")
    scorer = make_scorer(ds, cfg)
    weights = cfg.canslim.weights

    for key in ("C", "A", "N", "S", "L", "I"):
        def make(key=key):
            def factor(ctx):
                value = {"close": entry, "forced": True}
                if key == "L":
                    value.update({
                        "abs_rs": abs_rs,
                        "path": l_path,
                        "peers_total": 4,
                        "competitors": 3,
                        "gate_basis": "competitors",
                        "peers_gate": False,
                        "tie_policy": "strictly_lower",
                        "peers_capped": False,
                        "abs_rs_gate": abs_rs is not None and abs_rs > 0.0,
                    })
                return FactorScore(
                    key=key,
                    score=float(scores[key]),
                    weight=weights[key],
                    value=value,
                    note=f"{key} 因子构造值 {float(scores[key]):.0f} 分（权重 {weights[key]:.2f}）",
                )
            return factor
        setattr(scorer, f"_factor_{key.lower()}", make())

    def market_direction(asof, key="M"):
        return FactorScore(
            key="M",
            score=float(scores["M"]),
            weight=weights["M"],
            value={"forced": True},
            note=f"M 因子构造值 {float(scores['M']):.0f} 分（权重 {weights['M']:.2f}）",
        )

    scorer.market_direction = market_direction
    return scorer


def build_result(code, total, signal, *, close=None) -> CanslimResult:
    """直接构造 CanslimResult（用于 rank/recommend 的纯结构断言）。"""
    factors = [
        FactorScore(
            key=key,
            score=total,
            weight=1.0 / len(FACTORS),
            value=({"close": close} if (close is not None and key == "N") else {}),
            note=f"{key} 因子 {total:.0f} 分",
        )
        for key in FACTORS
    ]
    return CanslimResult(
        code=code, name=f"票{code}", asof=ASOF, total=total,
        factors=factors, signal=signal,
        reasons=[f"构造理由：总分 {total:.2f}"], risks=[],
    )


# ---------------------------------------------------------------------------
# TC-S-04 / TC-S-06 / TC-S-07 / TC-S-08 / TC-S-10 / TC-S-11 / TC-S-12：纯函数边界
# ---------------------------------------------------------------------------


def test_factors_constant_is_frozen_order():
    """TC-S-01（契约）：七因子键逐字且顺序固定。"""
    assert FACTORS == ("C", "A", "N", "S", "L", "I", "M")


@pytest.mark.parametrize("yoy,expected", [
    (200.0, 100.0), (100.0, 100.0), (99.9, 90.0), (50.0, 90.0), (49.9, 80.0),
    (30.0, 80.0), (29.9, 70.0), (20.0, 70.0), (19.9, 55.0), (10.0, 55.0),
    (9.9, 40.0), (0.0, 40.0), (-0.1, 15.0), (-1.0, 15.0), (-50.0, 15.0),
])
def test_map_c_growth_boundaries(yoy, expected):
    """TC-S-04：C 因子逐点边界（100/50/30/29.9/20/10/0/-1）。"""
    assert map_c_growth(yoy) == expected


@pytest.mark.parametrize("yoy,expected", [
    (80.0, 100.0), (50.0, 100.0), (49.9, 85.0), (25.0, 85.0), (24.9, 70.0),
    (15.0, 70.0), (14.9, 55.0), (5.0, 55.0), (4.9, 40.0), (0.0, 40.0), (-0.1, 20.0),
])
def test_map_a_growth_boundaries(yoy, expected):
    """TC-S-06（档位部分）：A 因子年度基础分逐点边界。"""
    assert map_a_growth(yoy) == expected


@pytest.mark.parametrize("distance,expected", [
    (0.0, 100.0), (0.02, 100.0), (0.0201, 85.0), (0.05, 85.0), (0.0501, 65.0),
    (0.15, 65.0), (0.1501, 45.0), (0.30, 45.0), (0.3001, 25.0), (0.80, 25.0),
])
def test_map_n_position_boundaries(distance, expected):
    """TC-S-07（档位部分）：N 因子 250 日区间位置逐点边界。"""
    assert map_n_position(distance) == expected


def test_map_n_breakout_volume_is_iff():
    """TC-S-07：``max(score, 90)`` 当且仅当「突破 且 量能 > 50 日均量 × 1.5」。"""
    # 突破 + 放量 → 抬到 90（基础 85）
    assert map_n_position(0.05, 1.51, breakout=True) == 90.0
    # 突破但量能不足（=1.5 不算 >1.5）→ 保持基础分
    assert map_n_position(0.05, 1.5, breakout=True) == 85.0
    # 放量但未突破 → 保持基础分（不得仅因放量加分）
    assert map_n_position(0.05, 3.0, breakout=False) == 85.0
    # 突破 + 放量但基础分更高 → 取 max，不降分
    assert map_n_position(0.0, 2.0, breakout=True) == 100.0
    # 无量能数据 → 不触发
    assert map_n_position(0.05, None, breakout=True) == 85.0


@pytest.mark.parametrize("float_mv_yi,expected", [
    (10.0, 80.0), (49.9, 80.0), (50.0, 90.0), (199.9, 90.0), (200.0, 75.0),
    (499.9, 75.0), (500.0, 60.0), (999.9, 60.0), (1000.0, 45.0), (25000.0, 45.0),
])
def test_map_s_float_mv_boundaries(float_mv_yi, expected):
    """TC-S-08（档位部分）：S 因子流通市值分档逐点边界（亿元）。"""
    assert map_s_float_mv(float_mv_yi) == expected


@pytest.mark.parametrize("percentile,expected", [
    (1.0, 100.0), (0.90, 100.0), (0.899, 85.0), (0.75, 85.0), (0.749, 65.0),
    (0.50, 65.0), (0.499, 45.0), (0.25, 45.0), (0.249, 25.0), (0.0, 25.0),
])
def test_map_l_percentile_boundaries(percentile, expected):
    """TC-S-09（档位部分）：L 因子行业排名分档逐点边界。"""
    assert map_l_percentile(percentile) == expected


@pytest.mark.parametrize("rs,expected", [
    (60.0, 90.0), (30.1, 90.0), (30.0, 75.0), (15.1, 75.0), (15.0, 60.0),
    (0.1, 60.0), (0.0, 40.0), (-14.9, 40.0), (-15.0, 20.0), (-40.0, 20.0),
])
def test_map_l_absolute_boundaries(rs, expected):
    """TC-S-10（档位部分）：L 因子退化路径绝对 RS 分档逐点边界。"""
    assert map_l_absolute(rs) == expected


@pytest.mark.parametrize("ratio,expected", [
    (0.90, 55.0), (0.8001, 55.0), (0.80, 70.0), (0.6001, 70.0), (0.60, 85.0),
    (0.35, 85.0), (0.10, 85.0), (0.0999, 60.0), (0.03, 60.0), (0.0299, 35.0), (0.0, 35.0),
])
def test_map_i_ratio_boundaries(ratio, expected):
    """TC-S-11（档位部分）：I 因子机构持股分档逐点边界。"""
    assert map_i_ratio(ratio) == expected


def test_map_m_breadth_accumulates_and_caps():
    """TC-S-12：M 因子分项累加（基础 20 + 均线 20/20 + 涨幅 25 + 家数 20 + 涨停 15）。"""
    strong = dict(BREADTH_STRONG)
    assert map_m_breadth(strong) == 100.0            # 120 → 上限 100
    assert map_m_breadth({}) == 20.0                  # 仅基础分
    assert map_m_breadth({"above_ma50": True}) == 40.0
    assert map_m_breadth({"above_ma50": True, "above_ma200": True}) == 60.0
    assert map_m_breadth({"pct_chg_20d": 5.0}) == 35.0     # ">5%" 严格 → 落入 ">0" 档 +15
    assert map_m_breadth({"pct_chg_20d": 5.1}) == 45.0
    assert map_m_breadth({"pct_chg_20d": 0.0}) == 25.0     # ">0" 严格 → 落入 ">-5" 档 +5
    assert map_m_breadth({"pct_chg_20d": -4.9}) == 25.0
    assert map_m_breadth({"pct_chg_20d": -5.0}) == 20.0    # 否则 0 分
    assert map_m_breadth({"advancers": 100, "decliners": 50}) == 32.0   # 比值 2.0 → 仅 >1 档 +12
    assert map_m_breadth({"advancers": 101, "decliners": 50}) == 40.0   # >2 → +20
    assert map_m_breadth({"advancers": 100, "decliners": 0}) == 40.0    # 分母 0 → ∞
    assert map_m_breadth({"advancers": 2, "decliners": 4}) == 25.0      # 比值 0.5 → +5
    assert map_m_breadth({"advancers": 1, "decliners": 4}) == 20.0      # 比值 0.25 < 0.5 → +0
    assert map_m_breadth({"limit_up_count": 20}) == 28.0
    assert map_m_breadth({"limit_up_count": 51}) == 35.0
    assert map_m_breadth(None) == 20.0


def test_report_periods_uses_disclosure_deadlines():
    """报告期推导：2025-10-09 时点 Q3 未披露，最近报告期应为 20250630。"""
    periods = report_periods(ASOF)
    assert periods[0] == "20250630"
    assert periods[1] == "20250331"
    assert "20250930" not in periods
    assert report_periods("2025-11-01")[0] == "20250930"
    assert report_periods("2025-05-01")[0] == "20250331"
    # 4-29 时年报（法定 4-30 前披露）尚未到期 → 最近已披露报告期为 2024Q3
    assert report_periods("2025-04-29")[0] == "20240930"
    # 4-30 当日年报与一季报同时到期 → 最近报告期为 2025Q1，年报仍在列表中
    assert report_periods("2025-04-30")[0] == "20250331"
    assert "20241231" in report_periods("2025-04-30")


# ---------------------------------------------------------------------------
# TC-S-01 / TC-S-02 / TC-S-03：完整 Seven-factor 组装
# ---------------------------------------------------------------------------


def build_full_stub(code=STRONG) -> StubDataSource:
    """构造一份七因子数据齐备的假数据源（各因子均可得高分）。"""
    closes = [100.0 + i * 0.1 for i in range(249)] + [130.0]
    highs = [c * 1.001 for c in closes]
    volumes = [1_000_000] * 249 + [3_000_000]
    peers = {
        code: daily_frame(closes, highs=highs, volumes=volumes, start="2024-10-01"),
        "000858": daily_frame([100.0] + [105.0] * 248, start="2024-10-01"),
        "000568": daily_frame([100.0] + [90.0] * 248, start="2024-10-01"),
    }
    return StubDataSource(
        daily=peers,
        spot={code: spot_frame(code, float_mv=1.2e10, turnover=6.0, last=130.0)},
        quote={code: {"code": code, "name": "强票", "last": 130.0, "prev_close": 128.0,
                      "limit_up": 140.0, "limit_down": 115.0, "suspended": False}},
        quarterly={
            "20250630": quarterly_frame(code, 82.5),
            "20250331": quarterly_frame(code, 51.0),
        },
        fin={code: fin_frame([("2024-12-31", 30.0, 20.0), ("2023-12-31", 22.0, 18.0),
                              ("2022-12-31", 16.0, 16.0)])},
        holders={"20250630": holders_frame(code, -8.5)},
        top_holders={code: top_holders_frame(code, [
            ("易方达蓝筹精选混合型证券投资基金", "证券投资基金", 6.0, 100.0),
            ("中国证券金融股份有限公司", "机构投资者", 4.0, 50.0),
            ("香港中央结算有限公司", "机构投资者", 2.5, -20.0),
        ])},
        industry={code: "白酒"},
        constituents={"白酒": [code, "000858", "000568"]},
        index_daily={"sh000001": daily_frame([100.0] + [105.0] * 248, start="2024-10-01")},
        breadth=BREADTH_STRONG,
    )


def test_score_returns_seven_unique_factors_in_range():
    """TC-S-01 + TC-S-02：七因子齐全、key 唯一且顺序固定，各分落在 [0, 100]。"""
    result = make_scorer(build_full_stub()).score(STRONG, ASOF)
    keys = [f.key for f in result.factors]
    assert keys == list(FACTORS)
    assert len(set(keys)) == len(FACTORS) == 7
    for factor in result.factors:
        assert 0.0 <= factor.score <= 100.0, factor
        assert factor.weight > 0
        assert factor.note.strip()
    assert 0.0 <= result.total <= 100.0
    assert abs(sum(f.weight for f in result.factors) - 1.0) < 1e-9


def test_total_equals_weighted_sum():
    """TC-S-03：``total`` == 七因子加权和（容差 1e-6）。"""
    result = make_scorer(build_full_stub()).score(STRONG, ASOF)
    expected = sum(f.score * f.weight for f in result.factors)
    assert result.total == pytest.approx(expected, abs=1e-6)
    assert result.code == STRONG
    assert result.asof == ASOF
    assert result.name == "强票"


def test_explanations_are_chinese_with_numbers():
    """TC-S-23：解释文本为中文且引用具体数值。"""
    result = make_scorer(build_full_stub()).score(STRONG, ASOF)
    texts = list(result.reasons) + list(result.risks) + [f.note for f in result.factors]
    assert texts
    for text in texts:
        assert any("\u4e00" <= ch <= "\u9fff" for ch in text), text
    assert any(ch.isdigit() for ch in "".join(result.reasons)), result.reasons
    for factor in result.factors:
        assert any(ch.isdigit() for ch in factor.note), factor.note


# ---------------------------------------------------------------------------
# TC-S-05：C 因子加速加成
# ---------------------------------------------------------------------------


def score_with_c(yoy, prev_yoy, *, bonus=1.25):
    code = STRONG
    stub = build_full_stub()
    stub._quarterly = {
        "20250630": quarterly_frame(code, yoy),
        "20250331": quarterly_frame(code, prev_yoy),
    }
    return make_scorer(stub, make_cfg(c_accel_bonus=bonus)).score(code, ASOF).factor("C")


def test_c_accel_bonus_applied_when_growth_accelerates():
    """TC-S-05：本季 20%（基础 70）> 上季 10% → 70 × 1.25 = 87.5。"""
    factor = score_with_c(20.0, 10.0)
    assert factor.score == pytest.approx(87.5)
    assert factor.value["accelerated"] is True
    assert factor.value["base_score"] == 70.0
    assert "加速" in factor.note


def test_c_accel_bonus_capped_at_100():
    """TC-S-05：本季 50%（基础 90）> 上季 30% → 90 × 1.25 = 112.5 → 上限 100。"""
    factor = score_with_c(50.0, 30.0)
    assert factor.score == 100.0


def test_c_no_accel_when_growth_decelerates_or_flat():
    """TC-S-05 反向：本季 ≤ 上季 → 不加成。"""
    factor = score_with_c(30.0, 50.0)
    assert factor.score == 80.0
    assert factor.value["accelerated"] is False
    flat = score_with_c(30.0, 30.0)
    assert flat.score == 80.0
    assert flat.value["accelerated"] is False


def test_c_accel_bonus_follows_config():
    """加速加成倍数取自 ``CANSLIMConfig.c_accel_bonus``。"""
    factor = score_with_c(20.0, 10.0, bonus=1.1)
    assert factor.score == pytest.approx(77.0)


def test_C_factor_uses_prev_period_from_chain():
    """CR-12：C 的**上季链在早期 asof 真的可用**（``quarterly_20241231`` 提供上季增速 15.38）。

    此前该链在 20241231 断掉 → 加速判定只能走「上季增速不可得」的退化分支。
    """
    cfg = make_cfg()
    require_fixtures()
    scorer = CanslimScorer(cfg, DataSource(cfg, offline=True), offline=True)
    factor = scorer.score(STRONG, "2025-04-30").factor("C")
    assert factor.value["period"] == "20250331"
    assert factor.value["net_profit_yoy"] == pytest.approx(11.56, abs=1e-6)
    assert factor.value["prev_net_profit_yoy"] == pytest.approx(15.38, abs=1e-6)
    assert factor.value["accelerated"] is False                    # 11.56 < 15.38 → 不加速
    assert factor.value["base_score"] == 55.0
    assert factor.score == 55.0                                    # 未加速：不加成
    assert "上季 +15.4%" in factor.note


def test_c_acceleration_actually_enters_the_formula(scenario_scorer):
    """加速标志为真**且真的进了公式**：``score == min(100, base × c_accel_bonus)`` 且 ``score > base``。

    用剧本链条（20→35→68→72→75→78→80 **逐期加速**）—— 天然可判 ``accelerated=True``，
    不依赖真实数据恰好加速；只断言布尔标志无法证明加成真进了计算。
    """
    cfg = make_cfg()
    factor = scenario_scorer.score(STRONG, SCENARIO_ASOF).factor("C")
    value = factor.value
    assert value["accelerated"] is True
    assert value["period"] == "20250630"
    assert value["net_profit_yoy"] == pytest.approx(68.0)
    assert value["prev_net_profit_yoy"] == pytest.approx(35.0)
    base = map_c_growth(value["net_profit_yoy"])
    assert value["base_score"] == base == 90.0                     # 68% → 90 分
    assert factor.score == pytest.approx(min(100.0, base * cfg.canslim.c_accel_bonus))
    assert factor.score > base                                     # 90 → 100：加成确实抬分了
    assert "加速" in factor.note


def test_c_missing_report_period_degrades_to_neutral():
    """TC-S-13：业绩报表缺该票 → C 因子 50 分 + risks 标注，不抛异常。"""
    stub = build_full_stub()
    stub._quarterly = {}
    result = make_scorer(stub).score(STRONG, ASOF)
    factor = result.factor("C")
    assert factor.score == 50.0
    assert factor.value["missing"] is True
    assert "C数据缺失" in result.risks


# ---------------------------------------------------------------------------
# TC-S-06：A 因子（3 年均值 + ROE 加成）
# ---------------------------------------------------------------------------


def test_a_uses_three_year_average_with_roe_bonus():
    """TC-S-06：三年均分 = mean(100, 85, 70)（含 ROE≥17 各 +10 后仍为 100/85/70）。"""
    code = STRONG
    stub = build_full_stub()
    stub._fin = {code: fin_frame([
        ("2024-12-31", 60.0, 20.0),   # 100 + 10 → 上限 100
        ("2023-12-31", 25.0, 17.0),   # 85 + 10 = 95
        ("2022-12-31", 15.0, 10.0),   # 70 + 0  = 70
    ])}
    result = make_scorer(stub).score(code, ASOF)
    factor = result.factor("A")
    assert factor.score == pytest.approx((100.0 + 95.0 + 70.0) / 3)
    assert factor.value["years_used"] == 3
    assert len(factor.value["annual"]) == 3


def test_a_roe_below_hurdle_gets_no_bonus():
    """TC-S-06：ROE < 17% 不加分。"""
    code = STRONG
    stub = build_full_stub()
    stub._fin = {code: fin_frame([
        ("2024-12-31", 60.0, 16.9),
        ("2023-12-31", 30.0, 5.0),
        ("2022-12-31", 20.0, 5.0),
    ])}
    factor = make_scorer(stub).score(code, ASOF).factor("A")
    assert factor.score == pytest.approx((100.0 + 85.0 + 70.0) / 3)


def test_a_only_annual_reports_are_used():
    """TC-S-06：季度行（0331/0630/0930）不参与 A 因子。"""
    code = STRONG
    stub = build_full_stub()
    stub._fin = {code: fin_frame([
        ("2025-06-30", 999.0, 50.0),
        ("2025-03-31", 999.0, 50.0),
        ("2024-12-31", 30.0, 10.0),
    ])}
    factor = make_scorer(stub).score(code, ASOF).factor("A")
    assert factor.value["years_used"] == 1
    assert factor.score == 85.0


def test_a_factor_ignores_undisclosed_future_annual_report():
    """A 因子禁止未来函数：2025-12-31 年报在 asof=2025-10-09 尚未披露（法定 2026-04-30），不得采用。"""
    code = STRONG
    stub = build_full_stub()
    stub._fin = {code: fin_frame([
        ("2025-12-31", 300.0, 99.0),      # 未来数据：不得进入 A 因子
        ("2024-12-31", 30.0, 10.0),
        ("2023-12-31", 30.0, 10.0),
        ("2022-12-31", 30.0, 10.0),
    ])}
    factor = make_scorer(stub).score(code, ASOF).factor("A")
    assert [item["period"] for item in factor.value["annual"]] == ["20221231", "20231231", "20241231"]
    assert factor.value["years_used"] == 3
    assert factor.score == pytest.approx(85.0)      # 30% → 85，未被未来年报拉高


def test_a_missing_fin_indicator_degrades():
    """TC-S-13：财务指标不可得 → A 因子 50 分 + risks 标注。"""
    stub = build_full_stub()
    stub._fin = {}
    result = make_scorer(stub).score(STRONG, ASOF)
    assert result.factor("A").score == 50.0
    assert "A数据缺失" in result.risks


# ---------------------------------------------------------------------------
# TC-S-07：N 因子（新高 + 突破放量）
# ---------------------------------------------------------------------------


def test_n_new_high_scores_100():
    """TC-S-07：收盘价 = 250 日最高 → 创新高 → 100 分。"""
    code = STRONG
    closes = [100.0] * 249 + [100.0]
    stub = build_full_stub()
    stub._daily = {code: daily_frame(closes, highs=closes, start="2024-10-01")}
    factor = make_scorer(stub).score(code, ASOF).factor("N")
    assert factor.score == 100.0
    assert factor.value["new_high"] is True


def test_n_breakout_with_volume_bumps_to_90():
    """TC-S-07：突破前高但收盘距当日最高 4% 时基础 85，放量（3× 50日均量）后取 max(85, 90) = 90。"""
    code = STRONG
    prior = [100.0] * 249
    closes = prior + [107.5]
    highs = prior + [112.0]
    volumes = [1_000_000] * 249 + [3_000_000]
    stub = build_full_stub()
    stub._daily = {code: daily_frame(closes, highs=highs, volumes=volumes, start="2024-10-01")}
    factor = make_scorer(stub).score(code, ASOF).factor("N")
    assert factor.value["breakout"] is True
    assert factor.value["new_high"] is False
    assert factor.value["distance_to_high"] == pytest.approx(4.5 / 112.0)
    assert factor.score == 90.0


def test_n_breakout_without_volume_stays_at_base():
    """TC-S-07 反向：突破但量能不足（1.2×）→ 不加成，保持基础分 85。"""
    code = STRONG
    prior = [100.0] * 249
    closes = prior + [107.5]
    highs = prior + [112.0]
    volumes = [1_000_000] * 249 + [1_200_000]
    stub = build_full_stub()
    stub._daily = {code: daily_frame(closes, highs=highs, volumes=volumes, start="2024-10-01")}
    factor = make_scorer(stub).score(code, ASOF).factor("N")
    assert factor.score == 85.0


def test_n_missing_daily_degrades():
    """TC-S-13：日线不可得 → N 因子 50 分（但该票 spot 仍在，不得抛异常）。"""
    code = STRONG
    stub = build_full_stub()
    stub._daily = {}
    stub._fail = {"daily:" + code}
    result = make_scorer(stub).score(code, ASOF)
    assert result.factor("N").score == 50.0
    assert "N数据缺失" in result.risks


# ---------------------------------------------------------------------------
# TC-S-08：S 因子（流通市值 + 换手 + 股东户数）
# ---------------------------------------------------------------------------


def score_with_s(*, float_mv, turnover, holders_chg):
    code = STRONG
    stub = build_full_stub()
    stub._spot = {code: spot_frame(code, float_mv=float_mv, turnover=turnover)}
    stub._holders = ({"20250630": holders_frame(code, holders_chg)}
                     if holders_chg is not None else {})
    return make_scorer(stub).score(code, ASOF).factor("S")


def test_s_float_mv_bucket_plus_turnover_plus_holders():
    """TC-S-08：120 亿 → 90；换手 6% → +10；股东户数 -8.5% → +10；合计 100（上限）。"""
    factor = score_with_s(float_mv=1.2e10, turnover=6.0, holders_chg=-8.5)
    assert factor.score == 100.0
    assert factor.value["float_mv_yi"] == pytest.approx(120.0)
    assert factor.value["turnover_bonus"] == 10.0
    assert factor.value["holders_bonus"] == 10.0


def test_s_holders_increase_is_penalised():
    """TC-S-08：股东户数增加 → −10。"""
    factor = score_with_s(float_mv=1.2e10, turnover=6.0, holders_chg=12.0)
    assert factor.value["holders_bonus"] == -10.0
    assert factor.score == 90.0


def test_s_turnover_outside_band_gets_no_bonus():
    """TC-S-08：换手率 2%（<3%）→ 不加分（120 亿基础 90 分）。"""
    factor = score_with_s(float_mv=1.2e10, turnover=2.0, holders_chg=None)
    assert factor.value["turnover_bonus"] == 0.0
    assert factor.value["holders_bonus"] == 0.0
    assert factor.score == 90.0


def test_s_score_never_below_zero_or_above_100():
    """TC-S-08：边界上下限钳制（30 亿 → 80，换手 10% → +10，户数 +30% → −10 = 80）。"""
    factor = score_with_s(float_mv=3.0e9, turnover=10.0, holders_chg=30.0)
    assert factor.score == 80.0
    assert 0.0 <= factor.score <= 100.0


def test_s_float_mv_is_yuan_and_converted_to_yi():
    """INTERFACES.md §4 强制单位表：``float_mv`` 为**元**，按 §6.1 的亿元阈值消费时 ÷1e8。

    （不做自适应探测：夹具若写错量纲必须在夹具通道暴露，而不是被启发式掩盖。）
    """
    factor = score_with_s(float_mv=1.2e10, turnover=6.0, holders_chg=-8.5)      # 120 亿元
    assert factor.value["float_mv_raw"] == pytest.approx(1.2e10)
    assert factor.value["float_mv_yi"] == pytest.approx(120.0)
    assert factor.score == 100.0
    # 2.1 万亿元 = 21000 亿元 → >= 1000 亿档 → 基础 45 + 10 + 10 = 65
    large = score_with_s(float_mv=2.1e12, turnover=6.0, holders_chg=-8.5)
    assert large.value["float_mv_yi"] == pytest.approx(21000.0)
    assert large.score == 65.0


def test_s_realtime_path_uses_spot_when_asof_is_latest():
    """asof == 最新交易日（真·实时）→ S 因子走 spot() 快照路径。"""
    code = STRONG
    stub = build_full_stub()
    stub._latest = ASOF
    factor = make_scorer(stub).score(code, ASOF).factor("S")
    assert "path" not in factor.value            # 实时路径：无 historical 标记
    assert stub.calls.get("spot", 0) >= 1
    assert factor.value["float_mv_raw"] == pytest.approx(1.2e10)


def test_s_historical_path_never_uses_spot():
    """裁决 2：``asof < 最新交易日`` → 禁止用 spot（否则引入未来快照），改用当日日线 × 流通股本。

    用「spot() 直接抛错」来证明历史路径不依赖快照：若还依赖，S 会退化为缺失。
    """
    code = STRONG
    stub = build_full_stub()
    stub._latest = "2026-10-09"                  # asof 2025-10-09 属历史回溯
    stub._fail = {"spot"}                        # 快照不可用（模拟"历史日没有当日快照"）
    stub._holders = {"20250630": pd.DataFrame([{
        "code": code, "name": "强票", "holders": 100000, "holders_prev": 110000,
        "holders_chg_pct": -8.5, "avg_hold_value": 1e5, "total_mv": 1e11,
        "total_share": 1.0e9,                    # 10 亿股
    }])}
    frame = daily_frame([130.0] * 5, end=ASOF)
    frame["turnover"] = 6.0
    stub._daily = {code: frame}
    factor = make_scorer(stub).score(code, ASOF).factor("S")
    assert factor.value["path"] == "historical_daily"
    assert factor.value["close"] == pytest.approx(130.0)
    assert factor.value["total_share"] == pytest.approx(1.0e9)
    assert factor.value["float_mv_raw"] == pytest.approx(130.0 * 1.0e9)   # 1.3e11 元
    assert factor.value["float_mv_yi"] == pytest.approx(1300.0)           # 1300 亿 → >=1000 亿档 = 45
    assert factor.score == 65.0                                           # 45 + 10(换手) + 10(户数减少)
    assert "未使用实时快照" in factor.note


def test_s_historical_path_degrades_without_daily_snapshot_fields():
    """裁决 2：历史回溯缺当日 turnover / 流通股本 → 50 分中性 + risks 标注，且不得回退 spot()。"""
    code = STRONG
    stub = build_full_stub()
    stub._latest = "2026-10-09"
    stub._fail = {"spot"}
    stub._holders = {}                            # 无流通股本
    frame = daily_frame([130.0] * 5, end=ASOF)
    frame["turnover"] = None                      # 当日无换手率
    stub._daily = {code: frame}
    result = make_scorer(stub).score(code, ASOF)
    factor = result.factor("S")
    assert factor.score == 50.0
    assert factor.value.get("missing") is True
    assert "S：历史回溯缺当日快照" in result.risks
    assert "S数据缺失" in result.risks


def test_i_uses_asof_eligible_period_and_reports_metadata():
    """CR-7：I 因子只用 ``asof`` 时点**已披露**的期次，并把期次元信息暴露在 ``value``。"""
    code = STRONG
    stub = build_full_stub()
    factor = make_scorer(stub).score(code, ASOF).factor("I")
    assert factor.value["is_historical"] is True
    assert factor.value["report_date"] == "2025-06-30"
    assert factor.value["announce_date"] == "2025-08-13"
    assert factor.value["top_holders_asof"] == ASOF
    assert factor.value["ratio_sum_raw"] > 0


def test_i_missing_period_degrades_to_neutral_with_two_risks():
    """CR-7：``asof`` 时点无已披露期次 → I=50 中性 + ``risks`` **两条**标注（绝不回落到当期快照）。"""
    code = STRONG
    stub = build_full_stub()
    # 该期披露日在 asof 之后 → 数据源返回空表（无前视）
    stub._top_holders = {code: top_holders_frame(
        code, [("某基金", "证券投资基金", 12.0, 10.0)],
        report_date="2026-06-30", announce_date="2026-08-15",
    )}
    result = make_scorer(stub).score(code, ASOF)
    factor = result.factor("I")
    assert factor.score == 50.0
    assert factor.value.get("missing") is True
    assert "I数据缺失" in result.risks
    assert "I：asof 时点无对应期次股东数据（数据源仅提供当期）" in result.risks


def test_i_change_nan_is_skipped_not_counted_as_zero():
    """``change`` 为 NaN 的行必须跳过（不得当 0 累加），有值行照常参与 ±10。"""
    code = STRONG
    stub = build_full_stub()
    stub._top_holders = {code: top_holders_frame(code, [
        ("易方达蓝筹精选混合型证券投资基金", "证券投资基金", 6.0, None),     # 缺 shares → change NaN
        ("全国社保基金一一二组合", "社保基金", 4.0, 100.0),
        ("中国工商银行股份有限公司", "机构投资者", 2.0, 50.0),
    ])}
    factor = make_scorer(stub).score(code, ASOF).factor("I")
    assert factor.value["ratio_sum_raw"] == pytest.approx(12.0)
    assert factor.value["net_change"] == pytest.approx(150.0)     # 只累计有值的两行
    assert factor.value["change_bonus"] == 10.0
    assert factor.score == 95.0


def test_i_skips_nan_ratio_instead_of_counting_zero():
    """裁决 3：``ratio`` 为 NaN 的行必须**跳过**（不得当 0 累加，否则 I 系统性偏低）。"""
    code = STRONG
    stub = build_full_stub()
    stub._top_holders = {code: top_holders_frame(code, [
        ("中国贵州茅台酒厂(集团)有限责任公司", "流通A股", None, None),   # ratio 未披露
        ("贵州省国有资本运营有限责任公司", "流通A股", None, None),
        ("香港中央结算有限公司", "流通A股", 4.3, None),
        ("中国人寿保险股份有限公司", "机构投资者", 0.45, None),
        ("中国银行股份有限公司-招商中证白酒指数分级证券投资基金", "证券投资基金", 0.36, None),
    ])}
    factor = make_scorer(stub).score(code, ASOF).factor("I")
    assert factor.value["ratio_sum_raw"] == pytest.approx(4.3 + 0.45 + 0.36)   # 不含 0
    assert factor.value["ratio"] == pytest.approx(0.0511)
    assert factor.score == 60.0                    # 3%-10% 档（若把 NaN 当 0 也是同档，但合计必须正确）


def test_s_missing_spot_degrades():
    """TC-S-13：快照缺 float_mv → S 因子 50 分 + risks 标注。"""
    code = STRONG
    stub = build_full_stub()
    stub._spot = {}
    stub._quote = {code: {"code": code, "name": "强票", "last": 130.0, "suspended": False}}
    result = make_scorer(stub).score(code, ASOF)
    assert result.factor("S").score == 50.0
    assert "S数据缺失" in result.risks


# ---------------------------------------------------------------------------
# TC-S-09 / TC-S-10：L 因子两条路径
# ---------------------------------------------------------------------------


def industry_stub(target_return, peer_returns, *, index_return=0.05, code=STRONG,
                  industry="白酒", extra_constituents=()):
    """构造「行业排名路径」的可控数据源：目标票 + N 个同业 + 固定指数收益。

    ``_industry_rs`` 样本口径 = 可算 RS 的成分股（含自身）；门槛按 **competitors = n − 1 ≥ 10**。
    第 i 个同业代码固定为 ``0000{i:02d}``，保证候选集确定性排序可复现。
    """
    frames = {code: daily_frame([100.0, 100.0 * (1 + target_return)], start="2024-10-01")}
    constituents = [code]
    for i, ret in enumerate(peer_returns):
        peer = f"0000{i:02d}"
        frames[peer] = daily_frame([100.0, 100.0 * (1 + ret)], start="2024-10-01")
        constituents.append(peer)
    constituents.extend(extra_constituents)
    for extra in extra_constituents:                # 额外成分股也给低收益日线（计入可算样本）
        frames.setdefault(extra, daily_frame([100.0, 100.0], start="2024-10-01"))
    stub = build_full_stub()
    stub._daily = frames
    stub._index_daily = {"sh000001": daily_frame([100.0, 100.0 * (1 + index_return)], start="2024-10-01")}
    stub._industry = {code: industry}
    stub._constituents = {industry: constituents}
    return stub


def test_l_gate_requires_ten_comparators():
    """①门槛：``competitors = n − 1``；9 个对手（n=10）不达标 → 降级；10 个对手（n=11）→ 行业路径。"""
    nine = industry_stub(0.50, [0.0] * 9)
    factor_nine = make_scorer(nine).score(STRONG, ASOF).factor("L")
    assert factor_nine.value["competitors"] == 9
    assert factor_nine.value["peers_gate"] is False
    assert factor_nine.value["path"] == "absolute_rs"

    ten = industry_stub(0.50, [0.0] * 10)
    factor_ten = make_scorer(ten).score(STRONG, ASOF).factor("L")
    assert factor_ten.value["peers_total"] == 11
    assert factor_ten.value["competitors"] == 10
    assert factor_ten.value["peers_gate"] is True
    assert factor_ten.value["path"] == "industry_rank"
    assert factor_ten.value["gate_basis"] == "competitors"
    assert factor_ten.value["percentile"] == pytest.approx(1.0)
    assert factor_ten.score == 100.0


def test_l_industry_rank_path_scores_100_for_leader():
    """TC-S-09：RS 在行业内最高（前 10%）→ 100 分，且走行业排名路径。"""
    stub = industry_stub(0.50, [0.0] * 10)      # 自身 +50%，10 个同业 0%，指数 +5%
    factor = make_scorer(stub).score(STRONG, ASOF).factor("L")
    assert factor.value["path"] == "industry_rank"
    assert factor.value["percentile"] == pytest.approx(1.0)
    assert factor.score == 100.0
    assert factor.value["abs_rs"] == pytest.approx(45.0)
    assert factor.value["tie_policy"] == "strictly_lower"


def test_l_industry_rank_middle_and_laggard():
    """TC-S-09：行业中游 → 65 分；行业垫底 → 25 分。"""
    middle = industry_stub(0.20, [0.30] * 5 + [0.10] * 5)      # 5 家更好、5 家更差 → percentile 0.5
    factor = make_scorer(middle).score(STRONG, ASOF).factor("L")
    assert factor.value["percentile"] == pytest.approx(0.5)
    assert factor.score == 65.0

    laggard = industry_stub(-0.20, [0.30] * 10)
    factor2 = make_scorer(laggard).score(STRONG, ASOF).factor("L")
    assert factor2.value["percentile"] == pytest.approx(0.0)
    assert factor2.score == 25.0


def test_l_tie_policy_strictly_lower():
    """并列口径：并列者**不算被击败**（``tie_policy="strictly_lower"``），候选股不得因打平获益。"""
    # 自身 +20%；10 个对手中 1 个与自身完全相同、9 个更差 → below=9 → percentile=0.9（而非 1.0）
    stub = industry_stub(0.20, [0.20] + [0.0] * 9)
    factor = make_scorer(stub).score(STRONG, ASOF).factor("L")
    assert factor.value["tie_policy"] == "strictly_lower"
    assert factor.value["percentile"] == pytest.approx(0.9)
    assert factor.score == 100.0                                # 0.9 恰在边界（§6.1 用 ≥）


def test_l_peers_cap_is_deterministic():
    """截断口径：候选集**先按代码升序再截断至 50**，并暴露 ``peers_capped`` / ``cap``。"""
    many = [f"9{i:05d}" for i in range(60)]
    stub = industry_stub(0.50, [0.0] * 10, extra_constituents=many)
    factor = make_scorer(stub).score(STRONG, ASOF).factor("L")
    assert factor.value["peers_capped"] is True
    assert factor.value["cap"] == 50
    assert factor.value["peers_total"] == 50        # 截断至 50 只（升序确定性子集）
    assert factor.value["competitors"] == 49
    assert "peers_capped" in factor.note


@pytest.mark.parametrize("percentile,expected", [
    (0.900001, 100.0), (0.90, 100.0), (0.899999, 85.0),
    (0.750001, 85.0), (0.75, 85.0), (0.749999, 65.0),
    (0.500001, 65.0), (0.50, 65.0), (0.499999, 45.0),
    (0.250001, 45.0), (0.25, 45.0), (0.249999, 25.0),
])
def test_l_percentile_boundaries_with_epsilon(percentile, expected):
    """分档边界 pin：0.90/0.75/0.50/0.25 各取「恰好等于」与「±1e-6」两侧（§6.1 用 ≥）。"""
    assert map_l_percentile(percentile) == expected


def test_l_degraded_value_schema_matches_industry_path():
    """verifier 硬指标②：降级路径的 ``value`` **键集**与行业路径一致（只差取值，不缺键）。"""
    degraded = make_scorer(industry_stub(0.50, [0.0] * 9)).score(STRONG, ASOF).factor("L")
    ranked = make_scorer(industry_stub(0.50, [0.0] * 10)).score(STRONG, ASOF).factor("L")
    required = {"percentile", "peers", "peers_total", "competitors", "gate_basis",
                "peers_gate", "tie_policy", "peers_capped", "cap", "abs_rs", "abs_rs_path",
                "path", "abs_rs_gate", "industry", "rs", "close", "instrument_type"}
    assert required <= set(degraded.value), required - set(degraded.value)
    assert required <= set(ranked.value), required - set(ranked.value)
    assert degraded.value["percentile"] is None          # 降级路径显式给 None，而非缺键
    assert ranked.value["percentile"] is not None


def test_l_note_distinguishes_etf_from_missing_industry():
    """ETF（``5xxxxx``/``1xxxxx``）本就没有行业分类：note 必须写「ETF」，不得说成「行业归属不可得」。

    只改文案：**分值规则与审计字段不变**（仍走绝对 RS 分档、``path="absolute_rs"``）。
    """
    cfg = make_cfg()
    require_fixtures()
    scorer = CanslimScorer(cfg, DataSource(cfg, offline=True), offline=True)

    etf_factor = scorer.score("588000", ASOF).factor("L")
    assert "ETF" in etf_factor.note
    assert "ETF 无行业归属，L 按绝对 RS 分档" in etf_factor.note
    assert "行业归属不可得" not in etf_factor.note
    assert etf_factor.value["instrument_type"] == "etf"
    assert etf_factor.value["path"] == "absolute_rs"          # 审计字段不变
    assert etf_factor.value["abs_rs_path"] == "absolute_rs"
    assert etf_factor.value["gate_basis"] == "competitors"

    stock_factor = scorer.score("600519", ASOF).factor("L")
    assert "ETF" not in stock_factor.note
    assert stock_factor.value["instrument_type"] == "stock"
    assert stock_factor.value["path"] == "absolute_rs"

    # 分值规则一致：两者都严格等于绝对 RS 分档值（仅文案不同）
    assert map_l_absolute(etf_factor.value["abs_rs"]) == etf_factor.score
    assert map_l_absolute(stock_factor.value["abs_rs"]) == stock_factor.score


def test_l_degradation_is_annotated_in_risks():
    """契约 L10：L 因子降级为绝对分档时必须在 ``risks`` 标注，不得静默。"""
    stub = industry_stub(-0.20, [0.30] * 9)         # 对手不足（9<10）且绝对 RS 为负 → 双重标注
    result = make_scorer(stub).score(STRONG, ASOF)
    factor = result.factor("L")
    assert factor.value["path"] == "absolute_rs"
    assert factor.score == 20.0                     # RS=−25% 不在「>-15%」档 → 20 分
    assert any("已降级为绝对 RS 分档" in risk for risk in result.risks), result.risks
    assert any("相对强度不足：L=" in risk for risk in result.risks), result.risks
    assert any("绝对 RS -25.00% ≤ 0" in risk for risk in result.risks), result.risks


def test_l_degrades_to_absolute_rs_when_industry_unavailable():
    """TC-S-10：行业取不到 → 绝对 RS 分档（RS=25% > 15% → 75 分）。"""
    code = STRONG
    stub = build_full_stub()
    stub._industry = {}
    factor = make_scorer(stub).score(code, ASOF).factor("L")
    assert factor.value["path"] == "absolute_rs"
    assert factor.value["rs"] == pytest.approx(25.0)
    assert factor.score == 75.0


def test_l_degrades_when_industry_sample_too_small():
    """TC-S-10：行业内可比样本 < 3 只 → 退化到绝对分档。"""
    code = STRONG
    stub = build_full_stub()
    stub._constituents = {"白酒": [code]}
    factor = make_scorer(stub).score(code, ASOF).factor("L")
    assert factor.value["path"] == "absolute_rs"


def test_l_absolute_rs_buckets_end_to_end():
    """TC-S-10：绝对 RS 四个子档（>15 / >0 / >-15 / 其余）端到端。"""
    code = STRONG
    index = {"sh000001": daily_frame([100.0] + [100.0] * 10, start="2024-10-01")}
    for last_close, expected in ((122.0, 75.0), (105.0, 60.0), (92.0, 40.0), (80.0, 20.0)):
        stub = build_full_stub()
        stub._industry = {}
        stub._daily = {code: daily_frame([100.0] + [last_close] * 10, start="2024-10-01")}
        stub._index_daily = index
        factor = make_scorer(stub).score(code, ASOF).factor("L")
        assert factor.score == expected, (last_close, factor.value)


def test_l_missing_index_degrades():
    """TC-S-13：指数日线不可得 → L 因子 50 分 + risks 标注。"""
    code = STRONG
    stub = build_full_stub()
    stub._index_daily = {}
    result = make_scorer(stub).score(code, ASOF)
    assert result.factor("L").score == 50.0
    assert "L数据缺失" in result.risks


# ---------------------------------------------------------------------------
# TC-S-11：I 因子（机构认同度）
# ---------------------------------------------------------------------------


def score_with_i(rows):
    code = STRONG
    stub = build_full_stub()
    stub._top_holders = {code: top_holders_frame(code, rows)}
    return make_scorer(stub).score(code, ASOF).factor("I")


def test_i_ratio_bucket_and_increase_bonus():
    """TC-S-11：机构合计 12% → 85；环比净增持 → +10 → 95。"""
    factor = score_with_i([
        ("易方达蓝筹精选混合型证券投资基金", "证券投资基金", 6.0, 100.0),
        ("全国社保基金一一二组合", "社保基金", 4.0, 50.0),
        ("中国工商银行股份有限公司", "机构投资者", 2.0, 20.0),
    ])
    assert factor.value["ratio"] == pytest.approx(0.12)
    assert factor.value["change_bonus"] == 10.0
    assert factor.score == 95.0


def test_i_ratio_over_80_percent_is_crowded():
    """TC-S-11：机构合计 > 80% → 55 分（过挤），减持 −10 → 45。"""
    factor = score_with_i([
        ("某基金", "证券投资基金", 50.0, -100.0),
        ("某保险", "保险机构", 40.0, -50.0),
    ])
    assert factor.value["ratio"] == pytest.approx(0.90)
    assert factor.value["change_bonus"] == -10.0
    assert factor.score == 45.0


def test_i_ignores_individual_holders():
    """TC-S-11：自然人股东不计入机构合计。"""
    factor = score_with_i([
        ("某基金", "证券投资基金", 12.0, 10.0),
        ("张三", "个人", 30.0, 999.0),
    ])
    assert factor.value["institution_count"] == 1
    assert factor.value["ratio"] == pytest.approx(0.12)


def test_i_missing_top_holders_degrades():
    """TC-S-13：十大股东不可得 → I 因子 50 分 + risks 标注。"""
    stub = build_full_stub()
    stub._top_holders = {}
    result = make_scorer(stub).score(STRONG, ASOF)
    assert result.factor("I").score == 50.0
    assert "I数据缺失" in result.risks


# ---------------------------------------------------------------------------
# TC-S-12：M 因子 + 缓存
# ---------------------------------------------------------------------------


def test_m_factor_end_to_end_and_cached_per_asof():
    """TC-S-12：M 因子端到端 100 分；同一 asof 只取数一次（缓存）。"""
    stub = build_full_stub()
    scorer = make_scorer(stub)
    first = scorer.market_direction(ASOF)
    second = scorer.market_direction(ASOF)
    assert first is second
    assert first.score == 100.0
    assert first.key == "M"
    assert stub.calls["breadth"] == 1
    result = scorer.score(STRONG, ASOF)
    assert result.factor("M").score == 100.0
    assert stub.calls["breadth"] == 1


def test_m_missing_breadth_degrades():
    """TC-S-13：市场宽度不可得 → M 因子 50 分 + risks 标注。"""
    stub = build_full_stub()
    stub._breadth = {}
    result = make_scorer(stub).score(STRONG, ASOF)
    assert result.factor("M").score == 50.0
    assert "M数据缺失" in result.risks


def test_all_fundamental_data_missing_still_scores_without_exception():
    """TC-S-13：除行情外全部基本面缺失 → 七因子仍在、缺失者为 50 分、risks 标注、不抛异常。"""
    code = STRONG
    stub = StubDataSource(
        daily={code: daily_frame([100.0] * 260, start="2024-10-01")},
        spot={code: spot_frame(code, float_mv=1.2e10, turnover=6.0)},
        breadth={},                                   # 市场宽度不可得
        fail={"quarterly:20250630", "fin:" + code, "top_holders:" + code,
              "industry:" + code, "index:sh000001", "index:000001",
              "index:csi931151", "index:sh000300"},
    )
    result = make_scorer(stub).score(code, ASOF)
    missing = {key for key in FACTORS if result.factor(key).score == 50.0}
    assert {"C", "A", "I", "L", "M"} <= missing
    assert "S" not in missing
    for key in missing:
        assert f"{key}数据缺失" in result.risks
    assert len(result.factors) == 7


# ---------------------------------------------------------------------------
# TC-S-14 .. TC-S-18：§6.2 信号判定顺序
# ---------------------------------------------------------------------------


def test_decide_signal_branch_order():
    """TC-S-14..18：§6.2（v3.5）五条分支逐条断言（顺序敏感、先命中先返回）。"""
    cfg = make_cfg().canslim
    # 1. total ≤ 40 → short（且 1 先于 2）
    assert _decide_signal(40.0, 100.0, 100.0, cfg, abs_rs=1.0)[0] == "short"
    assert _decide_signal(30.0, 0.0, 0.0, cfg, abs_rs=-5.0)[0] == "short"
    # 2. 四项前置全满足 → buy
    assert _decide_signal(75.0, 50.0, 60.0, cfg, abs_rs=0.0001)[0] == "buy"
    assert _decide_signal(100.0, 100.0, 100.0, cfg, abs_rs=30.0)[0] == "buy"
    # 3. 总分够但任一前置不满足 → watch
    assert _decide_signal(75.0, 49.9, 100.0, cfg, abs_rs=5.0)[0] == "watch"                    # M < 50
    assert _decide_signal(75.0, 100.0, 59.9, cfg, abs_rs=5.0)[0] == "watch"                    # L < 60
    assert _decide_signal(75.0, 100.0, 100.0, cfg, abs_rs=0.0)[0] == "watch"                   # abs_rs = 0
    assert _decide_signal(75.0, 100.0, 100.0, cfg, abs_rs=-0.1)[0] == "watch"                  # abs_rs < 0
    assert _decide_signal(75.0, 100.0, 100.0, cfg)[0] == "watch"                               # fail-closed
    assert _decide_signal(75.0, 50.0, 100.0, cfg, m_missing=True, abs_rs=5.0)[0] == "watch"    # A12
    # 4. 50 ≤ total < 75 → watch
    assert _decide_signal(60.0, 100.0, 100.0, cfg, abs_rs=5.0)[0] == "watch"
    assert _decide_signal(74.9, 100.0, 100.0, cfg, abs_rs=5.0)[0] == "watch"
    # 5. 其他 → avoid
    assert _decide_signal(45.0, 100.0, 100.0, cfg, abs_rs=5.0)[0] == "avoid"
    assert _decide_signal(40.1, 0.0, 0.0, cfg, abs_rs=-1.0)[0] == "avoid"


def test_decide_signal_abs_rs_boundary_strictly_positive():
    """②边界（verifier #4/#5）：``abs_rs == 0`` 不通过；``+1e-6`` 通过。**严格 > 0，不加 epsilon**。"""
    cfg = make_cfg().canslim
    assert _decide_signal(80.0, 80.0, 100.0, cfg, abs_rs=0.0)[0] == "watch"
    assert _decide_signal(80.0, 80.0, 100.0, cfg, abs_rs=1e-6)[0] == "buy"
    assert _decide_signal(80.0, 80.0, 100.0, cfg, abs_rs=-1e-6)[0] == "watch"


def test_decide_signal_branch3_names_abs_rs_failure():
    """分支 3 穷尽性（verifier #3/#7）：L=100 但 ``abs_rs < 0`` 必须落**分支 3**并指出原因。"""
    cfg = make_cfg().canslim
    signal, reason = _decide_signal(81.6, 87.0, 100.0, cfg, abs_rs=-5.0)
    assert signal == "watch"
    assert "绝对 RS -5.00% ≤ 0" in reason
    assert "介于 50.0 与买入门槛" not in reason      # 证明走的是分支 3，不是分支 4 兜底


def test_decide_signal_fail_closed_when_abs_rs_unavailable():
    """fail-closed（verifier #6）：``abs_rs`` 取不到 → 不得 buy，落分支 3 并标注原因。"""
    cfg = make_cfg().canslim
    signal, reason = _decide_signal(90.0, 90.0, 100.0, cfg)
    assert signal == "watch"
    assert "fail-closed" in reason


def test_signal_branch_3_reports_abs_rs_failure_end_to_end():
    """端到端：L=100、M=87、total≥75，但绝对 RS ≤ 0 → watch（独立前置拦下，**L 不被钳制**）。"""
    scores = {k: 80.0 for k in FACTORS}
    scores["L"] = 100.0
    result = patched_scorer(scores, abs_rs=-5.0).score(STRONG, ASOF)
    assert result.factor("L").score == 100.0         # L 仍是 §6.1 原值
    assert result.total >= 75.0
    assert result.signal == "watch"
    assert any("绝对 RS -5.00% ≤ 0" in text for text in result.reasons)
    assert any("绝对 RS -5.00% ≤ 0" in text for text in result.risks)
    assert result.factor("L").value["abs_rs_gate"] is False


def test_abs_rs_fail_closed_end_to_end_when_index_missing():
    """fail-closed 端到端：指数日线不可得 → L 缺失 → 绝不放行 buy，且 risks 标注。"""
    code = STRONG
    stub = build_full_stub()
    stub._index_daily = {}
    result = make_scorer(stub).score(code, ASOF)
    assert result.factor("L").value.get("missing") is True
    assert result.signal != "buy"
    assert any("fail-closed" in text for text in result.risks), result.risks


def test_verifier_counterexample_real_600519_high_l_is_blocked():
    """verifier 反例：真实 600519 @2026-10-09 绝对 RS=−6.8% 却曾拿 L=100；修复后 L=40 且不得 buy。"""
    cfg = make_cfg()
    require_fixtures()
    scorer = CanslimScorer(cfg, DataSource(cfg, offline=True), offline=True)
    result = scorer.score(STRONG, "2026-10-09")
    factor = result.factor("L")
    assert factor.value["path"] == "absolute_rs"
    assert factor.value["peers_gate"] is False
    assert factor.value["competitors"] < 10
    assert factor.value["abs_rs"] < 0
    assert factor.score == 40.0              # −6.8% 落 §6.1「>-15% → 40」档
    assert result.signal != "buy"


def test_rank_watch_never_produces_open_long():
    """verifier 要求④：``recommend`` 对 watch 绝不产出 ``open_long``。"""
    cfg = make_cfg()
    scores = {k: 60.0 for k in FACTORS}      # total=60 → 分支 4 watch
    result = patched_scorer(scores, abs_rs=5.0).score(STRONG, ASOF)
    assert result.signal == "watch"
    item = recommend([result], cfg)[0]
    assert item["action"] == "watch"
    assert item["side"] == "none"
    assert item["position_pct"] == 0.0


def test_decide_signal_m_missing_flag_blocks_buy():
    """A12 / INV-14：M 缺失的显式标记必须挡掉 buy（中性 50 恰好会通过 M≥50 门禁）。"""
    cfg = make_cfg().canslim
    assert _decide_signal(80.0, 50.0, 100.0, cfg, abs_rs=5.0)[0] == "buy"        # 无标记 → buy
    assert _decide_signal(80.0, 50.0, 100.0, cfg, m_missing=True, abs_rs=5.0)[0] == "watch"
    # 分支 1（做空）不受 M 缺失影响：先命中先返回
    assert _decide_signal(30.0, 50.0, 10.0, cfg, m_missing=True, abs_rs=-5.0)[0] == "short"
    # L 缺失（50 分）本就过不了 L≥60，无需特殊处理
    assert _decide_signal(80.0, 80.0, 50.0, cfg, abs_rs=5.0)[0] == "watch"


def test_m_missing_never_green_lights_buy_end_to_end():
    """A12 / INV-14：市场宽度取数失败 → M 记 50 分，但总分 ≥ 75 且 L ≥ 60 时仍**不得**给 buy。"""
    code = STRONG
    stub = build_full_stub()
    stub._breadth = {}                        # market_breadth 抛错 → M 因子缺失 → 50 分
    result = make_scorer(stub).score(code, ASOF)
    assert result.factor("M").score == 50.0
    assert result.factor("M").value.get("missing") is True
    assert result.total >= 75.0, result.total
    assert result.factor("L").score >= 60.0
    assert result.signal == "watch", result.reasons
    assert any("市场方向(M)数据缺失" in risk for risk in result.risks), result.risks
    # 对照组：市场宽度可得且强势 → 同一只票必须给 buy
    healthy = make_scorer(build_full_stub()).score(code, ASOF)
    assert healthy.factor("M").score == 100.0
    assert healthy.signal == "buy"
    assert not any("市场方向(M)数据缺失" in risk for risk in healthy.risks)


def test_l_missing_is_flagged_in_risks():
    """A12 附带要求：L 因子数据缺失也要在 risks 中标注。"""
    code = STRONG
    stub = build_full_stub()
    stub._index_daily = {}
    result = make_scorer(stub).score(code, ASOF)
    assert result.factor("L").value.get("missing") is True
    assert any("相对强度(L)数据缺失" in risk for risk in result.risks), result.risks


def test_signal_branch_1_short_end_to_end():
    """TC-S-14：总分 30 ≤ 40 → short。"""
    result = patched_scorer({k: 30.0 for k in FACTORS}).score(STRONG, ASOF)
    assert result.total == pytest.approx(30.0)
    assert result.signal == "short"
    assert any("做空" in reason for reason in result.reasons)


def test_signal_branch_2_buy_end_to_end():
    """TC-S-15：总分 80 ≥ 75 且 M=80 ≥ 50、L=80 ≥ 60 → buy。"""
    result = patched_scorer({k: 80.0 for k in FACTORS}).score(STRONG, ASOF)
    assert result.total == pytest.approx(80.0)
    assert result.signal == "buy"


def test_signal_branch_3_watch_when_market_weak():
    """TC-S-16：总分 ≥ 75 但 M=30 < 50 → watch，且 reasons/risks 说明原因。"""
    scores = {k: 90.0 for k in FACTORS}
    scores["M"] = 30.0
    result = patched_scorer(scores).score(STRONG, ASOF)
    assert result.total == pytest.approx(82.8)      # 0.88×90 + 0.12×30
    assert result.total >= 75.0
    assert result.signal == "watch"
    joined = " ".join(result.reasons)
    assert "M=30" in joined and "50" in joined
    assert any("市场方向偏弱" in risk for risk in result.risks)


def test_signal_branch_3_watch_when_relative_strength_weak():
    """TC-S-16：总分 ≥ 75 但 L=50 < 60 → watch。"""
    scores = {k: 90.0 for k in FACTORS}
    scores["L"] = 50.0
    result = patched_scorer(scores).score(STRONG, ASOF)
    assert result.total == pytest.approx(83.6)      # 0.84×90 + 0.16×50
    assert result.signal == "watch"
    assert any("相对强度不足" in risk for risk in result.risks)


def test_signal_branch_4_watch_middle_band():
    """TC-S-17：50 ≤ 总分 60 < 75 → watch。"""
    result = patched_scorer({k: 60.0 for k in FACTORS}).score(STRONG, ASOF)
    assert result.signal == "watch"


def test_signal_branch_5_avoid_ambiguous_band():
    """TC-S-18：40 < 总分 45 < 50 → avoid。"""
    result = patched_scorer({k: 45.0 for k in FACTORS}).score(STRONG, ASOF)
    assert result.signal == "avoid"


def test_signal_thresholds_follow_config():
    """信号阈值取自 ``CANSLIMConfig``：同一 35 分在默认（short=40）为 short，改 30 后落 avoid。"""
    default_result = patched_scorer({k: 35.0 for k in FACTORS}).score(STRONG, ASOF)
    assert default_result.signal == "short"
    cfg = make_cfg(short_threshold=30.0)
    custom = patched_scorer({k: 35.0 for k in FACTORS}, cfg).score(STRONG, ASOF)
    assert custom.signal == "avoid"


# ---------------------------------------------------------------------------
# TC-S-19：score_many 容错
# ---------------------------------------------------------------------------


def test_score_many_isolates_single_failure():
    """TC-S-19：单票取数失败 → total=0、risks 非空、signal 不误判为 short；其余票正常。"""
    good = build_full_stub()
    stub = StubDataSource(
        daily=good._daily,
        spot=good._spot,
        quote=good._quote,
        quarterly=good._quarterly,
        fin=good._fin,
        holders=good._holders,
        top_holders=good._top_holders,
        industry=good._industry,
        constituents=good._constituents,
        index_daily=good._index_daily,
        breadth=good._breadth,
        fail={"quote:999999", "daily:999999", "spot:999999"},
    )
    results = make_scorer(stub).score_many([STRONG, UNKNOWN], ASOF)
    assert len(results) == 2
    failed = next(item for item in results if item.code == UNKNOWN)
    assert failed.total == 0.0
    assert failed.risks
    assert failed.signal == "avoid"          # 取数失败不得被 §6.2 第 1 条误判为做空候选
    assert failed.factors == []
    ok = next(item for item in results if item.code == STRONG)
    assert ok.total > 0.0
    assert ok.signal in {"buy", "watch", "avoid", "short"}
    assert results[0].total >= results[-1].total      # 降序


def test_score_many_sorts_by_total_desc():
    """TC-S-19（排序）：结果按 total 降序。"""
    scorer = make_scorer(build_full_stub())
    results = scorer.score_many([STRONG, UNKNOWN], ASOF)
    totals = [item.total for item in results]
    assert totals == sorted(totals, reverse=True)


def test_score_unknown_code_raises_strategy_data_error():
    """无任何行情数据的标的 → ``StrategyDataError``（score_many 会兜底）。"""
    cfg = make_cfg()
    require_fixtures()
    scorer = CanslimScorer(cfg, DataSource(cfg, offline=True), offline=True)
    with pytest.raises(StrategyDataError):
        scorer.score(UNKNOWN, ASOF)


# ---------------------------------------------------------------------------
# TC-S-20：rank()
# ---------------------------------------------------------------------------


def test_rank_groups_and_orders():
    """TC-S-20：分组正确，buys 降序、shorts 升序、top 全量降序。"""
    cfg = make_cfg()
    results = [
        build_result("600519", 88.0, "buy"),
        build_result("000858", 76.0, "buy"),
        build_result("002594", 60.0, "watch"),
        build_result("600000", 52.0, "watch"),
        build_result("000001", 35.0, "short"),
        build_result("601398", 28.0, "short"),
        build_result("300750", 45.0, "avoid"),
    ]
    grouped = rank(results, cfg)
    assert set(grouped) == {"buys", "shorts", "watch", "avoid", "top", "asof"}
    assert [r.code for r in grouped["buys"]] == ["600519", "000858"]
    assert [r.total for r in grouped["buys"]] == sorted([88.0, 76.0], reverse=True)
    assert [r.code for r in grouped["shorts"]] == ["601398", "000001"]
    assert [r.total for r in grouped["shorts"]] == sorted([35.0, 28.0])
    assert [r.code for r in grouped["watch"]] == ["002594", "600000"]
    assert [r.code for r in grouped["avoid"]] == ["300750"]
    assert [r.total for r in grouped["top"]] == sorted([r.total for r in results], reverse=True)
    assert len(grouped["top"]) == len(results)
    assert grouped["asof"] == ASOF


def test_rank_excludes_failed_results_from_shorts():
    """契约 §6.2：取数失败票（``factors == []`` 或 ``total == 0`` 且 ``risks`` 非空）不得进入 shorts。"""
    cfg = make_cfg()
    no_factors = CanslimResult(
        code="999999", name="999999", asof=ASOF, total=0.0, factors=[],
        signal="short",                       # 即便被误判为 short 也必须被排除
        reasons=["取数失败"], risks=["数据获取失败，无法评分"],
    )
    zero_total = CanslimResult(
        code="300750", name="票300750", asof=ASOF, total=0.0,
        factors=[FactorScore(key=k, score=0.0, weight=0.1, value={}, note="0 分") for k in FACTORS],
        signal="short", reasons=["构造"], risks=["数据异常"],
    )
    genuine = build_result("000001", 30.0, "short")
    grouped = rank([no_factors, zero_total, genuine], cfg)
    assert [r.code for r in grouped["shorts"]] == ["000001"]
    assert len(grouped["top"]) == 3          # 失败票仍在 top/avoid 中可见（不被静默丢弃）


def test_rank_respects_thresholds_and_empty_input():
    """TC-S-20：signal 与阈值必须同时满足才入组；空输入返回空组。"""
    cfg = make_cfg(buy_threshold=90.0, short_threshold=30.0)
    grouped = rank([build_result("600519", 88.0, "buy"), build_result("000001", 35.0, "short")], cfg)
    assert grouped["buys"] == []      # 88 < buy_threshold 90
    assert grouped["shorts"] == []    # 35 > short_threshold 30
    empty = rank([], cfg)
    assert empty == {"buys": [], "shorts": [], "watch": [], "avoid": [], "top": [], "asof": ""}


# ---------------------------------------------------------------------------
# TC-S-21：recommend()
# ---------------------------------------------------------------------------


def test_recommend_long_stop_loss_and_take_profit():
    """TC-S-21：多头 ``stop_loss == entry × 0.93``、``take_profit == entry × 1.25``。"""
    cfg = make_cfg()
    entry = 100.0
    out = recommend([build_result("600519", 88.0, "buy", close=entry)], cfg)
    assert len(out) == 1
    item = out[0]
    assert set(item) == {
        "code", "name", "action", "side", "score", "confidence", "entry",
        "stop_loss", "take_profit", "position_pct", "reasons", "risks", "factors",
    }
    assert item["action"] == "open_long"
    assert item["side"] == "long"
    assert item["entry"] == pytest.approx(entry)
    assert item["stop_loss"] == pytest.approx(entry * 0.93)
    assert item["take_profit"] == pytest.approx(entry * 1.25)
    assert item["position_pct"] == pytest.approx(cfg.risk.max_position_pct)
    assert 0.0 <= item["confidence"] <= 1.0
    assert set(item["factors"]) == set(FACTORS)


def test_recommend_stop_loss_uses_risk_config():
    """TC-S-21：止损/止盈随 ``RiskConfig`` 变化。"""
    cfg = make_cfg()
    cfg = AppConfig(account=cfg.account, fee=cfg.fee, margin=cfg.margin,
                    risk=RiskConfig(stop_loss_pct=-0.10, take_profit_pct=0.50),
                    canslim=cfg.canslim, root=cfg.root)
    item = recommend([build_result("600519", 88.0, "buy", close=50.0)], cfg)[0]
    assert item["stop_loss"] == pytest.approx(45.0)
    assert item["take_profit"] == pytest.approx(75.0)


def test_recommend_short_side_mirrors_stop_and_target():
    """TC-S-21：空头止损在上方（×1.07）、止盈在下方（×0.75）。"""
    cfg = make_cfg()
    item = recommend([build_result("000001", 30.0, "short", close=20.0)], cfg)[0]
    assert item["action"] == "open_short"
    assert item["side"] == "short"
    assert item["stop_loss"] == pytest.approx(20.0 * 1.07)
    assert item["take_profit"] == pytest.approx(20.0 * 0.75)
    assert item["position_pct"] == pytest.approx(cfg.risk.max_position_pct)


def test_recommend_watch_and_avoid_actions():
    """TC-S-21：watch → action=watch/side=none；avoid → action=close/side=none，仓位 0。"""
    cfg = make_cfg()
    watch = recommend([build_result("002594", 60.0, "watch", close=30.0)], cfg)[0]
    avoid = recommend([build_result("300750", 45.0, "avoid", close=30.0)], cfg)[0]
    assert (watch["action"], watch["side"]) == ("watch", "none")
    assert (avoid["action"], avoid["side"]) == ("close", "none")
    assert watch["position_pct"] == 0.0
    assert avoid["position_pct"] == 0.0
    assert watch["reasons"] and watch["risks"] is not None


def test_recommend_entry_falls_back_to_zero_when_no_price():
    """TC-S-21（退化）：无价格记录 → entry 0.0，**action 降级 watch**，risks 说明，不抛异常。"""
    cfg = make_cfg()
    item = recommend([build_result("999999", 0.0, "avoid")], cfg)[0]
    assert item["entry"] == 0.0
    assert item["stop_loss"] == 0.0
    assert item["take_profit"] == 0.0
    assert (item["action"], item["side"]) == ("watch", "none")
    assert item["position_pct"] == 0.0
    assert "缺少收盘价，无法计算入场价" in item["risks"]
    assert set(item["factors"]) == set(FACTORS)      # 有因子但无价格 → 不参与建仓


def test_recommend_buy_without_price_degrades_to_watch():
    """TC-S-21（退化）：即便是 buy 信号，只要拿不到收盘价也必须降级为 watch（防下游 qty=0/除零）。"""
    cfg = make_cfg()
    item = recommend([build_result("600519", 88.0, "buy")], cfg)[0]     # close=None
    assert item["action"] == "watch"
    assert item["side"] == "none"
    assert item["position_pct"] == 0.0
    assert "缺少收盘价，无法计算入场价" in item["risks"]


def test_recommend_failed_result_has_empty_factor_map():
    """TC-S-19 + TC-S-21：取数失败兜底结果（factors=[]）→ 建议结构仍完整且不抛异常。"""
    cfg = make_cfg()
    failed = CanslimResult(
        code=UNKNOWN, name=UNKNOWN, asof=ASOF, total=0.0, factors=[], signal="avoid",
        reasons=["评分失败：StrategyDataError（总分记 0）"], risks=["数据获取失败，无法评分"],
    )
    item = recommend([failed], cfg)[0]
    assert item["factors"] == {}
    assert item["entry"] == 0.0
    assert item["stop_loss"] == 0.0 and item["take_profit"] == 0.0
    assert item["action"] == "watch"
    assert "缺少收盘价，无法计算入场价" in item["risks"]


def test_recommend_entry_comes_from_scored_close():
    """TC-S-21：``entry`` 取自打分结果记录的当日收盘价（N 因子 value["close"]）。"""
    cfg = make_cfg()
    result = make_scorer(build_full_stub(), cfg).score(STRONG, ASOF)
    item = recommend([result], cfg)[0]
    assert item["entry"] == pytest.approx(130.0)
    assert item["stop_loss"] == pytest.approx(130.0 * 0.93)


# ---------------------------------------------------------------------------
# TC-S-22：夹具端到端（强票 / 中性票 / 弱票）
# ---------------------------------------------------------------------------


from tests._guards import require_fixtures, require_scenario  # noqa: E402


@pytest.fixture(scope="module")
def fixture_scorer():
    require_fixtures()
    cfg = make_cfg()
    ds = DataSource(cfg, offline=True)
    return CanslimScorer(cfg, ds, offline=True)


@pytest.fixture(scope="module")
def fixture_pool_scorer():
    """真实夹具通道（scenario=False）—— 用于池级扫描，证明默认通道不读剧本。"""
    require_fixtures()
    cfg = make_cfg()
    ds = DataSource(cfg, offline=True)
    return CanslimScorer(cfg, ds, offline=True)


@pytest.fixture(scope="module")
def fixture_results(fixture_scorer):
    return fixture_scorer.score_many([STRONG, NEUTRAL, WEAK], ASOF)


def require_scored(results):
    """夹具通道守卫：任何一票退化成「取数失败兜底」（total=0 / factors=[]）都视为夹具缺失，直接判失败。

    没有这道守卫，夹具缺失时弱票断言会**空洞通过**（total=0 恰好满足 ``signal in {avoid, short}``），
    把「夹具没生成」伪装成「策略正确」。
    """
    degraded = [item.code for item in results if not item.factors or item.total <= 0.0]
    assert not degraded, (
        f"夹具通道降级为失败兜底：{degraded}；"
        f"risks={[item.risks for item in results if item.code in degraded]}"
    )


def test_tc_s22a_real_fixture_600519_is_not_a_buy(fixture_results):
    """TC-S-22a（真实数据硬事实）：真实夹具下 600519 不是买入标的。

    实测：真实 600519 当季净利同比仅 +8.9%（C=40）、距 250 日最高 −22.1%、近 6 月 RS 落后，
    即便 M/L 给高分也到不了 75 → 这是**正确结论**，不是缺陷。
    """
    require_scored(fixture_results)
    strong = next(item for item in fixture_results if item.code == STRONG)
    assert strong.signal in {"watch", "avoid"}, strong.reasons
    assert 50.0 <= strong.total < 75.0, (strong.total, [(f.key, f.score) for f in strong.factors])
    assert len(strong.factors) == 7
    assert strong.name


def test_tc_s22a_real_fixture_weak_stock_scored_and_below_strong(fixture_results):
    """TC-S-22a：真实夹具下 000001 被正常评分（非兜底），且两只都**不是买入标的**。

    ⚠️ 实测事实留痕（L ①+② + CR-7 落地后）：真实 **600519(≈51.3) 的总分低于 000001(≈53.2)** ——
    茅台的 L 拖累（绝对 RS −27.5% → 降级 20 分）大于平安银行 I/N 的优势。
    因此**不再断言 ``strong.total > weak.total``**（实测已翻转），改为断言"两只都没有买入资格"，
    避免为了保住旧断言而回退口径。
    """
    require_scored(fixture_results)
    weak = next(item for item in fixture_results if item.code == WEAK)
    strong = next(item for item in fixture_results if item.code == STRONG)
    assert len(weak.factors) == 7
    assert weak.signal in {"watch", "avoid", "short"}, weak.reasons
    assert strong.signal in {"watch", "avoid"}, strong.reasons
    for item in (strong, weak):
        assert item.total < 75.0, (item.code, item.total)
        assert item.signal != "buy"


def test_tc_s22a_real_pool_buy_branch_is_reachable_but_rare(fixture_pool_scorer):
    """TC-S-22a（可达性）：真实夹具池内存在达标买入标的 → buy 分支没有被门禁写死。

    实测（asof=2025-10-09，32 只真实标的）：300750 达标；600519 不在其中。
    若 buy 集合为空，说明某个门禁把 buy 写死了；若 buy 占多数，说明门禁失效 —— 两头都要挡。
    """
    cfg = make_cfg()
    codes = sorted(
        path.name[len("daily_"):-len(".json")]
        for path in (REPO_ROOT / "data" / "fixture").glob("daily_*.json")
        if not path.name.endswith("_unadjust.json") and path.name != "build_fixtures.py"
    )
    assert len(codes) >= 20, codes
    results = fixture_pool_scorer.score_many(codes, ASOF)
    grouped = rank(results, cfg)
    buys = grouped["buys"]
    assert buys, "真实池内无任何 buy 信号 → buy 分支可能被门禁写死"
    assert len(buys) <= max(1, int(len(codes) * 0.25)), [r.code for r in buys]
    for item in buys:
        assert item.total >= cfg.canslim.buy_threshold
        assert item.score_of("M") >= 50.0
        assert item.score_of("L") >= 60.0
        assert item.code != STRONG        # 真实强票叙事标的不得混进 buys
    # 做空分支同样必须在真实数据上可达（否则 short 判定形同虚设）
    assert grouped["shorts"], "真实池内无任何 short 信号 → 做空分支可能被门禁写死"
    for item in grouped["shorts"]:
        assert item.total <= cfg.canslim.short_threshold
        assert item.factors, "失败兜底票不得进入 shorts"


# ---- TC-S-22b：剧本（SYNTHETIC）数据 —— 标定 buy 分支可达 ---- #


@pytest.fixture(scope="module")
def scenario_scorer():
    """剧本通道：``scenario=True`` 显式叠加 ``data/scenario``（默认 DataSource 绝不读它）。

    剧本缺失时**跳过**而不是退回真实数据 —— 那些断言标定的是剧本形态，
    在真实数据上跑只会得到无意义的成败（见 conftest.require_scenario）。
    """
    require_scenario()
    cfg = make_cfg()
    ds = DataSource(cfg, offline=True, scenario=True)
    return CanslimScorer(cfg, ds, offline=True)


def test_tc_s22b_scenario_strong_stock_is_buy(scenario_scorer):
    """TC-S-22b：剧本数据下标定「满足全部门禁 → buy」。

    剧本自带 ``quarterly_20250630``（净利同比 68.0）与 ``quarterly_20250331``（35.0），
    故 asof 必须落在「最新已披露报告期 = 20250630」的窗口内（2025-08-31 ~ 2025-10-30）才自洽。
    """
    result = scenario_scorer.score(STRONG, SCENARIO_ASOF)
    assert result.total >= 75.0, [(f.key, f.score, f.note) for f in result.factors]
    assert result.signal == "buy", result.reasons
    assert result.factor("L").score >= 60.0
    assert result.factor("M").score >= 50.0
    assert result.factor("C").score == 100.0        # 68% → 90，且较上季 35% 加速 ×1.25 → 上限 100
    assert result.factor("N").score == 100.0        # 末值即 250 日新高


def test_tc_s22b_scenario_weak_stock_is_avoid_or_short(scenario_scorer):
    """TC-S-22b：剧本数据下标定弱票 < 50 分 → avoid/short。"""
    result = scenario_scorer.score(WEAK, SCENARIO_ASOF)
    assert result.total < 50.0, [(f.key, f.score, f.note) for f in result.factors]
    assert result.signal in {"avoid", "short"}, result.reasons


def test_tc_s22b_scenario_ranking_and_recommendation(scenario_scorer):
    """TC-S-22b：剧本通道下 rank/recommend 端到端可用（buy 进 buys、弱票进 shorts/avoid）。"""
    cfg = make_cfg()
    results = scenario_scorer.score_many([STRONG, NEUTRAL, WEAK], SCENARIO_ASOF)
    grouped = rank(results, cfg)
    assert [item.code for item in grouped["buys"]] == [STRONG]
    assert STRONG not in [item.code for item in grouped["shorts"]]
    recs = {item["code"]: item for item in recommend(results, cfg)}
    assert recs[STRONG]["action"] == "open_long"
    assert recs[STRONG]["side"] == "long"
    assert recs[STRONG]["entry"] > 0
    assert recs[STRONG]["stop_loss"] == pytest.approx(recs[STRONG]["entry"] * 0.93)


def test_scenario_data_never_leaks_into_default_channel(fixture_pool_scorer):
    """隔离保证：默认 DataSource 绝不读 data/scenario（否则用户 --offline 会看到伪造的 buy）。

    这个用例在函数体内**另建** scenario 通道做对比，所以不能只靠 fixture 守卫 ——
    剧本缺失时它会把两个通道都当真实数据，得到 ``51.34 > 51.34`` 这种误导性失败。
    """
    require_scenario()
    cfg = make_cfg()
    real = fixture_pool_scorer.score(STRONG, SCENARIO_ASOF)
    assert real.signal != "buy"
    assert real.total < 75.0
    scen = CanslimScorer(cfg, DataSource(cfg, offline=True, scenario=True), offline=True)
    synthetic = scen.score(STRONG, SCENARIO_ASOF)
    assert synthetic.total > real.total
    assert synthetic.signal == "buy"


def test_fixture_results_are_well_formed(fixture_results):
    """TC-S-01/02/03（夹具通道）：七因子齐全唯一、分数区间合法、total 为精确加权和。"""
    assert len(fixture_results) == 3
    for result in fixture_results:
        assert [f.key for f in result.factors] == list(FACTORS)
        assert len({f.key for f in result.factors}) == 7
        assert all(0.0 <= f.score <= 100.0 for f in result.factors)
        assert result.total == pytest.approx(
            sum(f.score * f.weight for f in result.factors), abs=1e-6
        )
        assert 0.0 <= result.total <= 100.0
        assert result.name and result.asof == ASOF


def test_fixture_ranking_and_recommendation_pipeline(fixture_results):
    """TC-S-20/21（夹具通道）：rank/recommend 消费真实夹具结果不抛异常且结构正确。"""
    require_scored(fixture_results)
    cfg = make_cfg()
    grouped = rank(fixture_results, cfg)
    assert grouped["asof"] == ASOF
    assert sum(len(grouped[key]) for key in ("buys", "shorts", "watch", "avoid")) == 3
    recs = recommend(fixture_results, cfg)
    assert len(recs) == 3
    for item in recs:
        assert item["action"] in {"open_long", "open_short", "close", "watch"}
        assert item["side"] in {"long", "short", "none"}
        if item["side"] == "long":
            assert item["stop_loss"] == pytest.approx(item["entry"] * 0.93)
        elif item["side"] == "short":
            assert item["stop_loss"] == pytest.approx(item["entry"] * 1.07)


def test_fixture_offline_never_touches_socket(monkeypatch, fixture_results):
    """离线铁律：屏蔽 socket 后夹具通道仍可完整跑通（证明策略层零联网）。"""
    def boom(*args, **kwargs):
        raise AssertionError("策略层在 offline 模式下禁止联网")

    monkeypatch.setattr(socket, "socket", boom)
    require_scored(fixture_results)
    assert len(fixture_results) == 3
    assert all(len(item.factors) == 7 for item in fixture_results)
