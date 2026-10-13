"""tests/test_datasource.py —— akshare 适配 / 缓存 / 离线夹具（INTERFACES.md v3.1 §4）。

全部 `offline=True`，且 conftest 的 `_no_network` 已封掉 socket：
任何联网尝试都会当场失败。单位断言严格对齐 §4 强制单位约定表。
"""

from __future__ import annotations

import json
import math
import os

import pytest

from ptrade.datasource import (
    DAILY_COLUMNS,
    FIN_COLUMNS,
    HOLDERS_COLUMNS,
    INDEX_COLUMNS,
    MARGIN_COLUMNS,
    QUARTER_COLUMNS,
    SPOT_COLUMNS,
    TOPH_COLUMNS,
    DataSource,
    DataUnavailable,
    _norm_code,
    price_limit_pct,
)

STORY = ("600519", "002594", "000001")


# --------------------------------------------------------------------------- #
# 构造与离线保证
# --------------------------------------------------------------------------- #
def test_offline_never_touches_akshare(offline_cfg, offline_ds, monkeypatch):
    """把 _ak 换成"一调用就炸"，跑遍所有离线方法：必须全部走 cache/fixture。"""

    def _boom(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("offline=True 时不得接触 akshare")

    monkeypatch.setattr(DataSource, "_ak", _boom)
    assert offline_ds.trade_dates()
    assert len(offline_ds.daily("600519", "2025-01-01", "2026-10-09")) > 0
    assert len(offline_ds.spot(["600519"])) == 1
    offline_ds.quote("600519", offline_ds.latest_trade_date())
    assert len(offline_ds.margin_targets()) > 0
    assert len(offline_ds.index_daily("sh000001", "2025-01-01", "2026-10-09")) > 0
    offline_ds.market_breadth(offline_ds.latest_trade_date())
    assert offline_ds.is_margin_target("600519")


def test_missing_fixture_raises_dataunavailable(tmp_path):
    from ptrade.config import load_config

    cfg = load_config(str(tmp_path))  # 空目录：无 cache / 无 fixture
    ds = DataSource(cfg, offline=True)
    for call in (
        lambda: ds.trade_dates(),
        lambda: ds.daily("600519", "2025-01-01", "2026-10-09"),
        lambda: ds.spot(["600519"]),
        lambda: ds.margin_targets(),
        lambda: ds.index_daily("sh000001", "2025-01-01", "2026-10-09"),
        lambda: ds.quarterly_report("20250630"),
        lambda: ds.fin_indicator("600519", "2021"),
        lambda: ds.holders("20250630"),
        lambda: ds.top_holders("600519"),
    ):
        with pytest.raises(DataUnavailable):
            call()


def test_ak_guard_raises_offline(offline_ds):
    with pytest.raises(DataUnavailable):
        offline_ds._ak()


def test_unknown_code_daily_raises(offline_ds):
    with pytest.raises(DataUnavailable):
        offline_ds.daily("999999", "2025-01-01", "2026-10-09")


def test_fresh_cache_is_preferred_over_fixture(offline_cfg, offline_ds):
    """新鲜缓存优先：手动写入一条 cache，daily() 必须返回它（含标记值）。"""
    key = "daily_600519_qfq_20250101_20250131"
    payload = [
        {"date": "2025-01-02", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
         "volume": 111.0, "amount": 222.0, "pct_chg": 0.5, "turnover": 1.0}
    ]
    offline_ds._write_entry(offline_ds.cache_dir, key, payload)
    frame = offline_ds.daily("600519", "2025-01-01", "2025-01-31")
    assert len(frame) == 1
    assert float(frame.iloc[0]["volume"]) == 111.0


def test_expired_cache_falls_back_to_fixture(offline_cfg, offline_ds):
    key = "daily_600519_qfq_20250101_20250131"
    offline_ds._write_entry(offline_ds.cache_dir, key, [{"date": "2025-01-02", "close": 1.0}])
    path = os.path.join(offline_ds.cache_dir, f"{key}.json")
    with open(path, "r", encoding="utf-8") as fh:
        entry = json.load(fh)
    entry["fetched_at"] = "2000-01-01T00:00:00+00:00"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh)
    frame = offline_ds.daily("600519", "2025-01-01", "2025-01-31")
    assert len(frame) > 1  # 夹具的完整序列，而不是那条 1 行缓存


# --------------------------------------------------------------------------- #
# 交易日历
# --------------------------------------------------------------------------- #
def test_trade_dates_sorted_and_valid(offline_ds):
    dates = offline_ds.trade_dates()
    assert len(dates) > 200
    assert dates == sorted(dates)
    assert all(len(d) == 10 and d[4] == "-" for d in dates[:50])
    latest = offline_ds.latest_trade_date()
    assert latest <= offline_ds._today()  # sina 日历含未来日期，必须裁剪到今天
    assert offline_ds.is_trade_date(latest)


def test_next_and_prev_trade_date(offline_ds):
    dates = offline_ds.trade_dates()
    latest = offline_ds.latest_trade_date()
    prev = offline_ds.prev_trade_date(latest)
    assert prev < latest
    assert offline_ds.next_trade_date(prev) == latest
    with pytest.raises(DataUnavailable):
        offline_ds.prev_trade_date(dates[0])


def test_is_trade_date_negative(offline_ds):
    assert offline_ds.is_trade_date("2026-10-10") is False  # 周六
    assert offline_ds.is_trade_date("1990-01-01") is False


# --------------------------------------------------------------------------- #
# 日线
# --------------------------------------------------------------------------- #
def test_daily_contract(offline_ds):
    frame = offline_ds.daily("600519", "2024-09-01", "2026-10-09")
    assert list(frame.columns) == DAILY_COLUMNS
    assert len(frame) > 250
    dates = frame["date"].astype(str).tolist()
    assert dates == sorted(dates)
    assert all(len(d) == 10 and d[4] == "-" for d in dates)
    assert frame["close"].notna().all()
    assert frame["high"].notna().all()
    assert (frame["high"] >= frame["low"]).all()
    # 单位：成交量=股；成交额=元；换手率/涨跌幅=百分数
    assert (frame["volume"] > 0).all()
    assert frame["volume"].median() > 1e4
    assert frame["amount"].median() > 1e6
    assert frame["turnover"].max() < 100
    assert frame["pct_chg"].abs().max() < 30


@pytest.mark.parametrize("code", ["600519", "002594", "000001", "601398"])
def test_daily_story_codes_cover_story_window(offline_ds, code):
    frame = offline_ds.daily(code, "2025-04-10", "2025-10-09")
    assert len(frame) > 100
    assert frame.iloc[0]["date"] >= "2025-04-10"
    assert frame.iloc[-1]["date"] <= "2025-10-09"


@pytest.mark.parametrize("code", ["588000", "510050", "159915"])
def test_daily_supports_etf_codes(offline_ds, code):
    frame = offline_ds.daily(code, "2024-09-01", "2026-10-09")
    assert len(frame) > 100
    assert frame["close"].notna().all()


def test_daily_slicing_respects_bounds(offline_ds):
    frame = offline_ds.daily("600519", "2025-06-01", "2025-06-30")
    assert len(frame) > 0
    assert frame["date"].min() >= "2025-06-01"
    assert frame["date"].max() <= "2025-06-30"


def test_daily_unadjust_and_qfq_differ(offline_ds):
    qfq = offline_ds.daily("600519", "2024-09-01", "2026-10-09")
    raw = offline_ds.daily("600519", "2024-09-01", "2026-10-09", unadjust=True)
    assert list(raw.columns) == DAILY_COLUMNS
    assert len(qfq) == len(raw)
    assert raw["amount"].median() > 1e6  # 真实成交额
    assert not qfq["close"].equals(raw["close"])  # 复权价与真实价不同


def test_daily_adjust_empty_string_equals_unadjust(offline_ds):
    a = offline_ds.daily("600519", "2025-01-01", "2025-03-01", adjust="")
    b = offline_ds.daily("600519", "2025-01-01", "2025-03-01", unadjust=True)
    assert a["close"].tolist() == b["close"].tolist()


def test_daily_with_prefix_code(offline_ds):
    assert len(offline_ds.daily("sh600519", "2025-01-01", "2025-03-01")) > 0


def test_last_close(offline_ds):
    frame = offline_ds.daily("600519", "2025-09-01", "2025-10-09")
    value = offline_ds.last_close("600519", "2025-10-09")
    assert value == pytest.approx(float(frame.iloc[-1]["close"]))
    assert value > 0


def test_last_close_before_listing_raises(offline_ds):
    with pytest.raises(DataUnavailable):
        offline_ds.last_close("600519", "2010-01-01")


# --------------------------------------------------------------------------- #
# 快照 / 单票报价
# --------------------------------------------------------------------------- #
def test_spot_contract(offline_ds):
    frame = offline_ds.spot()
    assert list(frame.columns) == SPOT_COLUMNS
    assert len(frame) > 1000  # 全市场夹具
    assert frame["code"].is_unique
    row = frame[frame["code"] == "600519"].iloc[0]
    assert row["name"]
    assert float(row["float_mv"]) > 1e9  # 元
    assert 0 < float(row["turnover"]) < 100  # 百分数
    assert float(row["limit_up"]) > float(row["last"]) > float(row["limit_down"])


def test_spot_codes_filter(offline_ds):
    frame = offline_ds.spot(["600519", "002594", "000001"])
    assert sorted(frame["code"]) == ["000001", "002594", "600519"]


def test_spot_limit_up_down_formula(offline_ds):
    frame = offline_ds.spot(["600519", "300750", "588000"])
    for _, row in frame.iterrows():
        prev = float(row["prev_close"])
        pct = price_limit_pct(row["code"], str(row["name"]))
        assert float(row["limit_up"]) == pytest.approx(round(prev * (1 + pct) + 1e-9, 2), abs=0.01)
        assert float(row["limit_down"]) == pytest.approx(round(prev * (1 - pct) + 1e-9, 2), abs=0.01)


def test_quote_contract_exact_keys(offline_ds):
    """报价契约：键集合与不变式。

    **asof 必须用「有数据的最近交易日」**（``price_asof``），不能用
    ``latest_trade_date()`` —— 后者读交易日历（覆盖到 2026-12-31），而日线夹具止于
    2026-10-09，于是 ``quote()`` 会因「日线末值日期 != asof」返回 ``suspended=True``
    （2026-10-13 实测本用例因此失败）。理由详见 ``tests/_guards.py``。
    """
    from tests._guards import price_asof

    asof = price_asof(offline_ds)
    quote = offline_ds.quote("600519", asof)
    assert set(quote.keys()) == {
        "code", "name", "last", "prev_close", "limit_up", "limit_down", "suspended"
    }
    assert quote["code"] == "600519"
    assert quote["last"] > 0
    assert quote["limit_up"] > quote["last"] > quote["limit_down"]
    assert quote["suspended"] is False


def test_quote_historical_asof_uses_daily(offline_ds):
    quote = offline_ds.quote("600519", "2025-10-09")
    expected = offline_ds.last_close("600519", "2025-10-09")
    assert quote["last"] == pytest.approx(expected)
    assert quote["suspended"] is False


def test_quote_suspended_on_non_trading_asof(offline_ds):
    quote = offline_ds.quote("600519", "2025-10-01")  # 国庆假期
    assert quote["suspended"] is True


# --------------------------------------------------------------------------- #
# 两融
# --------------------------------------------------------------------------- #
def test_margin_targets_contract(offline_ds):
    frame = offline_ds.margin_targets()
    assert list(frame.columns) == MARGIN_COLUMNS
    codes = set(frame["code"])
    for code in STORY + ("601398",):
        assert code in codes, code
    assert frame["code"].is_unique


def test_margin_targets_negative_case_600110(offline_ds):
    """负例：**融资标的但不可融券**。

    判定规则（保守近似）：深市用官方「融券标的」标记；沪市用「融券余量 > 0」判定当期可融券。
    实测 2026-10-09：601398 融券余量 50,857,400 股（活跃融券标的，**不是**负例），
    600110 诺德股份融券余量 0（融资余额 4.75 亿）→ 负例。
    """
    frame = offline_ds.margin_targets()
    row = frame[frame["code"] == "600110"].iloc[0]
    assert bool(row["can_finance"]) is True
    assert bool(row["can_short"]) is False
    assert offline_ds.is_margin_target("600110") is True
    assert offline_ds.is_short_target("600110") is False
    # 负例必须真实存在（不依赖具体代码）
    negatives = frame[(frame["can_finance"] == True) & (frame["can_short"] == False)]  # noqa: E712
    assert len(negatives) > 0


def test_margin_targets_story_codes_shortable(offline_ds):
    for code in STORY:
        assert offline_ds.is_margin_target(code) is True
        assert offline_ds.is_short_target(code) is True


def test_margin_targets_unknown_code(offline_ds):
    assert offline_ds.is_margin_target("999999") is False
    assert offline_ds.is_short_target("999999") is False


# --------------------------------------------------------------------------- #
# 基本面
# --------------------------------------------------------------------------- #
def test_quarterly_report_contract(offline_ds):
    frame = offline_ds.quarterly_report("20250630")
    assert list(frame.columns) == QUARTER_COLUMNS
    row = frame[frame["code"] == "600519"]
    assert len(row) == 1
    assert row.iloc[0]["net_profit_yoy"] is not None
    assert row.iloc[0]["industry"]


def test_fin_indicator_contract_and_annual_rows(offline_ds):
    frame = offline_ds.fin_indicator("600519", "2021")
    assert list(frame.columns) == FIN_COLUMNS
    assert len(frame) > 0
    annual = frame[frame["date"].astype(str).str.endswith("12-31")]
    assert len(annual) >= 3  # A 因子取最近 3 个年报
    assert annual["net_profit_yoy"].notna().any()
    assert annual["roe"].notna().any()


def test_holders_contract(offline_ds):
    frame = offline_ds.holders("20250630")
    assert list(frame.columns) == HOLDERS_COLUMNS
    row = frame[frame["code"] == "600519"]
    assert len(row) == 1
    assert row.iloc[0]["holders"] > 0
    assert row.iloc[0]["holders_chg_pct"] is not None


def test_top_holders_contract(offline_ds):
    frame = offline_ds.top_holders("600519")
    assert list(frame.columns) == TOPH_COLUMNS
    assert len(frame) > 0
    assert frame["ratio"].notna().any()
    assert (frame["ratio"] > 1.5).any()  # 百分数而非小数


def test_industry_lookup(offline_ds):
    industry = offline_ds.industry_of("600519")
    assert industry
    members = offline_ds.industry_constituents(industry)
    assert "600519" in members
    for code in members:
        assert len(offline_ds.daily(code, "2025-01-01", "2025-03-01")) > 0  # 离线可闭环


def test_industry_unknown_returns_empty(offline_ds):
    assert offline_ds.industry_of("999999") == ""
    assert offline_ds.industry_constituents("不存在的行业") == []


# --------------------------------------------------------------------------- #
# 指数 / 市场方向
# --------------------------------------------------------------------------- #
def test_index_daily_contract(offline_ds):
    frame = offline_ds.index_daily("sh000001", "2024-09-01", "2026-10-09")
    assert list(frame.columns) == INDEX_COLUMNS
    assert len(frame) > 250
    assert frame["close"].notna().all()


def test_index_code_aliases(offline_ds):
    a = offline_ds.index_daily("000001", "2025-01-01", "2025-06-30")
    b = offline_ds.index_daily("sh000001", "2025-01-01", "2025-06-30")
    assert a["close"].tolist() == b["close"].tolist()


def test_market_breadth_contract(offline_ds):
    asof = offline_ds.latest_trade_date()
    breadth = offline_ds.market_breadth(asof)
    assert set(breadth.keys()) == {
        "index_close", "index_ma50", "index_ma200", "above_ma50", "above_ma200",
        "pct_chg_1d", "pct_chg_20d", "advancers", "decliners", "limit_up_count", "limit_down_count",
    }
    assert breadth["index_close"] > 0
    assert isinstance(breadth["above_ma50"], bool)
    assert breadth["advancers"] >= 0 and breadth["decliners"] >= 0
    # asof == 快照 asof → 走全市场快照：家数上限 = 快照行数
    assert breadth["advancers"] + breadth["decliners"] <= len(offline_ds.spot())
    assert breadth["limit_up_count"] >= 0


def test_market_breadth_historical_asof(offline_ds):
    """历史 asof 无当日快照 → 退化为比对池日线口径（家数 <= 比对池规模）。"""
    breadth = offline_ds.market_breadth("2025-10-09")
    assert breadth["index_close"] > 0
    assert breadth["index_ma200"] > 0
    assert breadth["advancers"] + breadth["decliners"] <= len(offline_ds.universe())


# --------------------------------------------------------------------------- #
# scenario 剧本层（SYNTHETIC，默认不读）
# --------------------------------------------------------------------------- #
def test_scenario_dir_is_synthetic_and_isolated(offline_cfg, offline_ds):
    """默认 DataSource **不得**读到 data/scenario；scenario=True 时才叠加。"""
    scenario_dir = os.path.join(offline_cfg.root, "data", "scenario")
    if not os.path.isdir(scenario_dir):
        pytest.skip("未生成 data/scenario（data/scenario/build_scenario.py）")
    with open(os.path.join(scenario_dir, "daily_600519.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc.get("_synthetic") is True          # 显式标注：构造数据
    assert "SYNTHETIC" in str(doc.get("_note", ""))

    real = offline_ds.daily("600519", "2024-09-01", "2026-10-09")
    synth = DataSource(offline_cfg, offline=True, scenario=True).daily(
        "600519", "2024-09-01", "2026-10-09")
    assert not real["close"].equals(synth["close"])       # 剧本覆盖了真实夹具
    assert len(real) == len(synth)                        # 日期轴一致


def test_scenario_falls_back_to_fixture(offline_cfg):
    scenario_dir = os.path.join(offline_cfg.root, "data", "scenario")
    if not os.path.isdir(scenario_dir):
        pytest.skip("未生成 data/scenario")
    ds = DataSource(offline_cfg, offline=True, scenario=True)
    # 剧本没有覆盖 601398 的日线 → 回落真实夹具，仍可读到
    assert len(ds.daily("601398", "2025-01-01", "2025-06-30")) > 0
    assert ds.industry_of("600519")                        # 行业表来自真实夹具


def test_scenario_story_shapes(offline_cfg):
    """剧本标的形态符合标定意图（新高 / 深回撤 / 加速增长 / 机构合计）。"""
    scenario_dir = os.path.join(offline_cfg.root, "data", "scenario")
    if not os.path.isdir(scenario_dir):
        pytest.skip("未生成 data/scenario")
    ds = DataSource(offline_cfg, offline=True, scenario=True)
    # 自洽窗口内的示例 asof（= data/scenario/README.md 的示例，--check 也断言这一点）
    asof = "2025-10-09"
    strong = ds.daily("600519", "2024-09-01", asof).tail(250)
    ratio = float(strong.iloc[-1]["close"]) / float(strong["high"].max())
    assert ratio >= 0.98                                   # N 因子：创新高
    weak = ds.daily("000001", "2024-09-01", asof).tail(250)
    drawdown = 1 - float(weak.iloc[-1]["close"]) / float(weak["high"].max())
    assert drawdown >= 0.35                                # N 因子：深回撤
    # 全 6 期覆盖：任意 asof 都不回落真实数据
    for period, floor in (("20250331", 35.0), ("20250630", 68.0), ("20250930", 72.0),
                          ("20251231", 75.0), ("20260331", 78.0), ("20260630", 80.0)):
        frame = ds.quarterly_report(period)
        yoy = float(frame[frame["code"] == "600519"].iloc[0]["net_profit_yoy"])
        assert yoy == floor, (period, yoy)
    # 宽度：asof == 快照 asof → 走剧本快照口径（95:25 / 涨停 22）
    breadth = ds.market_breadth(asof)
    assert breadth["advancers"] / max(breadth["decliners"], 1) > 2
    assert breadth["limit_up_count"] >= 20
    assert breadth["above_ma50"] and breadth["above_ma200"]
    # 其它 asof（含最新交易日）：日线口径，M 的门禁由 MA50/MA200 保证
    latest = ds.latest_trade_date()
    assert latest != asof
    wide = ds.market_breadth(latest)
    assert wide["above_ma50"] and wide["above_ma200"]
    late = ds.daily("600519", "2024-09-01", latest).tail(250)
    assert float(late.iloc[-1]["close"]) / float(late["high"].max()) >= 0.98


def test_top_holders_ratio_completed(offline_ds):
    """源接口对部分股东不返回持股比例 → 用 持股数量/总股本 补齐（真实数据计算）。"""
    frame = offline_ds.top_holders("600519")
    assert frame["ratio"].notna().sum() == len(frame)
    total = float(frame["ratio"].sum())
    assert 40 < total <= 100  # 茅台集团等合计应落在合理区间


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
def test_universe_contains_story_codes(offline_ds):
    universe = offline_ds.universe()
    assert len(universe) >= 20
    for code in STORY:
        assert code in universe


@pytest.mark.parametrize(
    "raw,expected",
    [("sh600519", "600519"), ("SZ000001", "000001"), ("600519", "600519"), ("bj920000", "920000")],
)
def test_norm_code(raw, expected):
    assert _norm_code(raw) == expected


@pytest.mark.parametrize(
    "code,name,pct",
    [("600519", "", 0.10), ("300750", "", 0.20), ("688981", "", 0.20),
     ("600519", "ST某某", 0.05), ("830799", "", 0.30), ("588000", "", 0.10)],
)
def test_price_limit_pct(code, name, pct):
    assert price_limit_pct(code, name) == pct


# --------------------------------------------------------------------------- #
# CR-8：季度报告期公告日必须落在**法定披露窗口**内（防前视 / 防审计误导）
# --------------------------------------------------------------------------- #
LEGAL_DEADLINE = {"0331": "-04-30", "0630": "-08-31", "0930": "-10-31", "1231": "-04-30"}


@pytest.mark.parametrize("period", ["20241231", "20250331", "20250630", "20250930",
                                    "20251231", "20260331", "20260630"])
def test_quarterly_announce_date_within_legal_window(offline_ds, period):
    """announce_date = **真实公告日**（不是抓取日）：必须 >= 报告期年份起点且 <= 法定截止日。"""
    frame = offline_ds.quarterly_report(period)
    assert len(frame) > 0
    year = period[:4]
    tail = period[4:]
    if tail == "1231":
        deadline = f"{int(year) + 1}-04-30"
    else:
        deadline = f"{year}{LEGAL_DEADLINE[tail]}"
    days = frame["announce_date"].dropna().astype(str).str[:10]
    assert len(days) > 0
    assert days.min() >= f"{year}-01-01", f"{period} 公告日早于报告期: {days.min()}"
    assert days.max() <= deadline, f"{period} 公告日晚于法定截止日 {deadline}: {days.max()}"
    # 报告期本身不得晚于公告日
    assert days.min() >= f"{year}-01-01"


def test_quarterly_2025h1_announce_date_is_2025_not_2026(offline_ds):
    """回归 CR-8：20250331/20250630 曾被写成 2026 年（前视）。"""
    for period, expected_year in (("20250331", "2025"), ("20250630", "2025")):
        frame = offline_ds.quarterly_report(period)
        row = frame[frame["code"] == "600519"].iloc[0]
        assert str(row["announce_date"])[:4] == expected_year


# --------------------------------------------------------------------------- #
# CR-7：top_holders 的 asof 防前视 + report_date/announce_date + change
# --------------------------------------------------------------------------- #
def test_top_holders_columns_include_period_info(offline_ds):
    frame = offline_ds.top_holders("600519")
    assert list(frame.columns) == TOPH_COLUMNS
    assert "report_date" in TOPH_COLUMNS and "announce_date" in TOPH_COLUMNS


def test_top_holders_asof_returns_different_real_periods(offline_ds):
    """CR-7 验收：两个 asof 必须拿到**不同期次**，且公告日均 <= asof（无前视）。"""
    early = offline_ds.top_holders("600519", "2025-10-09")
    late = offline_ds.top_holders("600519", "2026-10-09")
    meta_e = early.attrs["top_holders_meta"]
    meta_l = late.attrs["top_holders_meta"]
    assert len(early) > 0 and len(late) > 0
    assert meta_e["report_date"] != meta_l["report_date"]
    for frame, asof in ((early, "2025-10-09"), (late, "2026-10-09")):
        meta = frame.attrs["top_holders_meta"]
        assert meta["is_historical"] is True
        assert meta["report_date"] <= asof
        assert meta["disclosed_on"] <= asof
    assert meta_e["report_date"] <= "2025-10-09"
    assert meta_l["report_date"] <= "2026-10-09"


def test_top_holders_asof_none_equals_latest_period(offline_ds):
    latest = offline_ds.top_holders("600519")
    late = offline_ds.top_holders("600519", "2026-10-09")
    assert latest.attrs["top_holders_meta"]["report_date"] == late.attrs["top_holders_meta"]["report_date"]
    assert len(latest) == len(late) == 10


def test_top_holders_before_any_disclosure_is_empty(offline_ds):
    frame = offline_ds.top_holders("600519", "2000-01-01")
    assert len(frame) == 0
    assert list(frame.columns) == TOPH_COLUMNS
    assert frame.attrs["top_holders_meta"]["is_historical"] is False


def test_top_holders_change_computed_from_previous_period(offline_ds):
    """change 必须来自**真实上一期**同名股东，且源缺 shares 的行保持 NaN（不填 0）。"""
    frame = offline_ds.top_holders("600519", "2025-10-09")
    assert frame["change"].notna().any()
    group = frame[frame["holder_name"].astype(str).str.contains("香港中央结算")]
    assert len(group) == 1
    assert group.iloc[0]["change"] is not None  # 上期有该股东 → 可算环比


def test_top_holders_missing_period_rows_do_not_empty_table(offline_cfg, offline_ds):
    """回归 CR-17：个别行缺 report_date 时只丢该行，**不得静默清空整表**。"""
    rows = [
        {"code": "600519", "holder_name": f"机构{i}", "holder_type": "基金",
         "shares": 1000.0 * (i + 1), "ratio": 1.0 + i, "change": None,
         "report_date": "2025-06-30", "announce_date": "2025-08-13"}
        for i in range(10)
    ]
    rows.append({"code": "600519", "holder_name": "无期次股东", "holder_type": "基金",
                 "shares": 1.0, "ratio": 0.1, "change": None,
                 "report_date": None, "announce_date": None})
    offline_ds._write_entry(offline_ds.cache_dir, "top_holders_600519", rows)
    frame = offline_ds.top_holders("600519", "2025-10-09")
    assert len(frame) == 10
    assert frame.attrs["top_holders_meta"]["rows_without_period_dropped"] == 1


def test_quarterly_prev_period_chain_available_for_early_asof(offline_ds):
    """CR-12：`asof` 落在 2025 上半年时，C 的**加速加成**必须能拿到「上季同比」。

    数据前提（本测试锁的就是它）：`20241231`（上季锚）与 `20250331` 两期夹具都存在，
    且在 `asof=2025-04-30` 时**都已披露**（announce_date <= asof），因此
    「本期同比 vs 上季同比」是可判定的（加速 / 未加速都能判）。
    """
    asof = "2025-04-30"
    chain = {}
    for period in ("20241231", "20250331"):
        frame = offline_ds.quarterly_report(period)
        assert len(frame) > 0, f"缺少 quarterly_{period}"
        row = frame[frame["code"] == "600519"].iloc[0]
        announced = str(row["announce_date"])[:10]
        yoy = float(row["net_profit_yoy"])
        assert announced <= asof, f"{period} 在 {asof} 时尚未披露（公告 {announced}）"
        assert row["net_profit_yoy"] is not None
        chain[period] = (announced, yoy)
    # 上季锚必须真的早于本期披露（链条方向正确）
    assert chain["20241231"][0] <= chain["20250331"][0]
    # 加速判定可判：两个同比都是有限数、且比较有明确结果
    prev_yoy, cur_yoy = chain["20241231"][1], chain["20250331"][1]
    assert math.isfinite(prev_yoy) and math.isfinite(cur_yoy)
    assert isinstance(cur_yoy > prev_yoy, bool)
    # 全池覆盖（不是只有剧情标的）
    frame = offline_ds.quarterly_report("20241231")
    assert len(frame) >= 25
    assert frame["net_profit_yoy"].notna().any()


def test_quarterly_prev_period_chain_is_disclosed_in_order(offline_ds):
    """上季链的公告日必须单调不降（20241231 <= 20250331 <= …），否则加速比较会前视。"""
    order = ["20241231", "20250331", "20250630", "20250930", "20251231", "20260331", "20260630"]
    last = ""
    for period in order:
        frame = offline_ds.quarterly_report(period)
        announced = sorted(frame["announce_date"].dropna().astype(str).str[:10].tolist())[0]
        assert announced >= last, f"{period} 公告日 {announced} 早于上一期 {last}"
        last = announced
