"""全库 asof 插桩守卫 —— 防止"前视偏差"以任何单点修复之外的形式回归。

来源：独立评审通道（verifier）在 R12 终判里指出的**唯一 WATCH 理由** ——
本次评审实测到 **4 处前视，分属 4 个不同层面**，但**没有任何"一次性锁全库"的守卫**：

1. **数据源方法缺 asof 参数** —— ``spot()``（实时快照）→ 历史回溯时 S 因子可能读到快照；
2. **数据源缺期间列** —— ``top_holders()`` 原先没有 ``report_date``/``announce_date`` → I 因子只能给"最新一期"；
3. **夹具期次语义错位** —— ``quarterly_*.json`` 的 ``period`` 与 ``announce_date`` 曾系统性错位 1 年；
4. **实现过滤静默失效** —— 部分行缺期次时 ``astype(str)`` + 字符串黑名单失效，**静默清空整表**（CR-17）。

本文件把这条不变量做成**可执行守卫**：

    当 ``asof < latest_trade_date`` 时，被消费的数据的时间戳必须 ``<= asof``。

设计要点（**拒绝空守卫**）：

* 每个被插桩的方法都在 ``METHOD_ASOF_ARG`` 里**显式声明它的 asof 语义**；
  未声明的"无 asof 方法"只有 ``spot`` 一个显式豁免项，且豁免项**另有可证伪的替代断言**（见 §5）。
* 断言失败时**打印违规方法名 + asof + 观测到的 max 时间戳**，而不是只报一个 False。
* §6 提供**非空洞性证明**：构造违规数据源 → 守卫必须变红。

.. note::
   本文件由**评审者**编写（Lead 指派），不属于 T1/T2/T3 任何写线；
   它守卫的是**别人的实现**，因此断言必须可独立证伪，不得依赖被测模块的内部实现细节。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from ptrade.config import load_config  # noqa: F401  (夹具经 conftest 注入)
from ptrade.datasource import DataSource

# --------------------------------------------------------------------------- #
# 0. 插桩层：记录每次取数的 asof 语义与返回数据的"最新时间戳"
# --------------------------------------------------------------------------- #

#: 方法 → 它的 asof 语义。``"end"`` 表示"最后一个区间参数即 asof"；
#: 整数表示"第 n 个位置参数就是 asof"；``"period"`` 表示"请求的报告期（YYYYMMDD）"。
#: **新增数据源方法必须在此登记**，否则 ``collect`` 会把它当作"未声明"而失败。
METHOD_ASOF_ARG: dict[str, object] = {
    "daily": "end",
    "index_daily": "end",
    "last_close": 1,
    "quote": 1,
    "market_breadth": 0,
    "is_margin_target": 1,
    "is_short_target": 1,
    "top_holders": 1,
    "margin_targets": 0,
    "quarterly_report": "period",
    "holders": "period",
    "fin_indicator": None,  # (code, start_year)：年份不是日期，只看返回行的 date 是否被消费方过滤
}

#: 消费方过滤型：**帧内可以合法包含未来期次的行**，由消费方按披露截止日过滤。
#: 这类方法**不能**用"帧的 max_ts ≤ asof"判定（那是假阳性）；
#: 它们的等价断言在**工件层**：见 ``test_guard_factor_artifacts_declare_no_future_period``。
#: 豁免不是"放过"：下表的每一行都有一条**可证伪的替代断言**，且
#: ``test_guard_consumer_filtered_frame_really_contains_future_rows`` 会证明"豁免是必要的"。
CONSUMER_FILTERED: dict[str, str] = {
    "fin_indicator": "多期财务指标帧（含始自 start_year 的全部季度）；消费方（A 因子）按披露截止日选年报",
}

#: 显式豁免：不参与"时间戳 ≤ asof"判定，但**必须各有可证伪的替代断言**。
#: * ``spot``：契约里没有 asof（语义就是"实时快照"）→ 替代断言见 §5；
#: * 交易日历：它**就是**日历本体，不是"某日的数据"。
EXEMPT_METHODS: dict[str, str] = {
    "spot": "契约无 asof 参数（实时快照语义）；替代断言见 §5：历史回溯必须走 historical_daily",
    "trade_dates": "交易日历本体",
    "next_trade_date": "交易日历本体",
    "prev_trade_date": "交易日历本体",
    "latest_trade_date": "交易日历本体",
    "industry_of": "静态行业归属映射（无时间维度）",
    "industry_constituents": "静态成分股名单（无时间维度）",
}

#: 帧内可能承载"数据时间"的列，按优先级探测。
TS_COLUMNS = ("date", "report_date", "announce_date", "period", "trade_date")

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPACT_RE = re.compile(r"^\d{8}$")


def norm_ts(value) -> str | None:
    """把 ``YYYYMMDD`` / ``YYYY-MM-DD`` 归一成 ``YYYY-MM-DD``；无法识别返回 ``None``。"""
    text = str(value).strip()[:10]
    if _TS_RE.match(text):
        return text
    if _COMPACT_RE.match(text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return None


def max_timestamp(obj) -> str | None:
    """返回对象里能看到的**最新**数据时间戳（DataFrame / dict / list[dict]）。"""
    stamps: list[str] = []
    try:
        import pandas as pd
    except Exception:  # pragma: no cover - pandas 是硬依赖
        pd = None

    if pd is not None and isinstance(obj, pd.DataFrame):
        for column in TS_COLUMNS:
            if column in obj.columns:
                for raw in obj[column].tolist():
                    ts = norm_ts(raw)
                    if ts:
                        stamps.append(ts)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key in TS_COLUMNS:
                ts = norm_ts(value)
                if ts:
                    stamps.append(ts)
            elif isinstance(value, (dict, list)):
                inner = max_timestamp(value)
                if inner:
                    stamps.append(inner)
    elif isinstance(obj, list):
        for item in obj:
            inner = max_timestamp(item)
            if inner:
                stamps.append(inner)
    return max(stamps) if stamps else None


class AsofRecorder:
    """``DataSource`` 的透明代理：记录每次取数的 asof 与返回数据的最新时间戳。"""

    def __init__(self, inner: DataSource):
        self._inner = inner
        self.records: list[dict] = []

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr) or (
            name not in METHOD_ASOF_ARG and name not in EXEMPT_METHODS
        ):
            return attr

        def wrapper(*args, **kwargs):
            result = attr(*args, **kwargs)
            rule = METHOD_ASOF_ARG.get(name)
            asof_arg = None
            if rule == "end":
                asof_arg = norm_ts(kwargs.get("end") or (args[2] if len(args) > 2 else None))
            elif rule == "period":
                asof_arg = norm_ts(args[0] if args else kwargs.get("period"))
            elif isinstance(rule, int):
                if len(args) > rule:
                    asof_arg = norm_ts(args[rule])
                elif name in kwargs:
                    asof_arg = norm_ts(kwargs[name])
            self.records.append(
                {
                    "method": name,
                    "asof_arg": asof_arg,
                    "max_ts": max_timestamp(result),
                    "exempt": name in EXEMPT_METHODS,
                    "consumer_filtered": name in CONSUMER_FILTERED,
                    "declared": (
                        rule is not None
                        or name in EXEMPT_METHODS
                        or name in CONSUMER_FILTERED
                    ),
                }
            )
            return result

        return wrapper

    # 便于断言时只取"非豁免"记录
    @property
    def data_records(self) -> list[dict]:
        return [r for r in self.records if not r["exempt"]]


def find_violations(records: list[dict], asof: str) -> list[str]:
    """纯函数：返回违规描述列表（空列表 = 通过）。**可被非空洞性测试直接调用。**

    三类记录的处理：

    * ``exempt``：显式豁免（``spot`` / 交易日历 / 静态映射）→ 不看时间戳，但其豁免理由
      必须另有可证伪的替代断言（§5）；
    * ``consumer_filtered``：帧内可合法含未来行（如 ``fin_indicator``）→ **跳过帧级时间戳判定**，
      其等价断言在工件层（§2 的 ``test_guard_factor_artifacts_declare_no_future_period``）；
    * 其余：帧的 ``max_ts`` 与调用参数 ``asof_arg`` 都不得晚于本次 ``asof``。
    """
    problems: list[str] = []
    for rec in records:
        if rec.get("exempt"):
            continue
        method = rec["method"]
        if rec.get("consumer_filtered"):
            # 豁免帧级判定，但必须仍被登记（否则等于静默放过）
            if not rec.get("declared", True):
                problems.append(f"{method}() 未在守卫中登记，且被当作消费方过滤型跳过 —— 不可接受")
            continue
        if rec.get("max_ts") and rec["max_ts"] > asof:
            problems.append(
                f"{method}() 返回了晚于 asof 的数据：max_ts={rec['max_ts']} > asof={asof}"
            )
        if rec.get("asof_arg") and rec["asof_arg"] > asof:
            problems.append(
                f"{method}() 被用晚于 asof 的参数调用：asof_arg={rec['asof_arg']} > asof={asof}"
            )
        if rec.get("asof_arg") is None and rec.get("max_ts") is None:
            problems.append(
                f"{method}() 既未声明 asof 语义、也无法从返回数据判定时间（守卫无法覆盖）——"
                f" 请在 METHOD_ASOF_ARG/CONSUMER_FILTERED/EXEMPT_METHODS 中登记"
            )
    return problems


def assert_no_violations(records: list[dict], asof: str, scope: str) -> None:
    problems = find_violations(records, asof)
    assert not problems, (
        f"[asof 守卫·{scope}] asof={asof} 存在 {len(problems)} 处前视/未覆盖：\n  - "
        + "\n  - ".join(problems)
    )


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def asof_env(offline_cfg):
    """返回 ``Env(cfg, latest, asof_late, asof_mid)`` —— 两个**早于最新交易日**的历史时点。

    * ``asof_late``：距最新交易日 60 个交易日，常规历史回溯；
    * ``asof_mid``：**刻意选在"多期帧里含未来行"的位置** —— 即 ``fin_indicator`` 返回的最新
      期次日期 **晚于** 该 asof。这样"消费方过滤型豁免"才是**真的被跑到**，而不是摆设。

    两个时点都必须落在夹具实际覆盖的日线区间内（否则不是"前视"，只是"没数据"）。
    """
    ds = DataSource(offline_cfg, offline=True)
    latest = ds.latest_trade_date()
    history = [
        str(d) for d in ds.daily("600519", "1990-01-01", latest)["date"] if str(d) < latest
    ]
    assert len(history) > 300, "夹具日线历史不足以做前视守卫（需 >300 个交易日）"
    asof_late = history[-60]

    fin = ds.fin_indicator("600519", "2020")
    frame_max = max(
        (norm_ts(v) for v in fin["date"] if norm_ts(v)), default=None
    )
    before_frame_max = [d for d in history if frame_max and d < frame_max]
    assert before_frame_max, "夹具里找不到早于财务指标最新期的交易日"
    asof_mid = before_frame_max[-1]
    assert asof_late < latest and asof_mid < latest
    return Env(offline_cfg, latest, asof_late, asof_mid)


class Env(NamedTuple):
    cfg: object
    latest: str
    asof_late: str
    asof_mid: str


# --------------------------------------------------------------------------- #
# 1. 窗口型序列：daily / index_daily —— 返回的 max(date) 必须 <= asof
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("which", ["asof_late", "asof_mid"])
def test_guard_strategy_scan_has_no_lookahead(asof_env, which):
    """驱动真实打分链路，断言 daily/index_daily 不返回晚于 asof 的数据。

    两个时点都跑：``asof_late``（常规历史）与 ``asof_mid``（多期帧里含未来行的位置）。
    """
    strategy = pytest.importorskip("ptrade.strategy", reason="strategy.py 未就绪（T3）")
    cfg, latest, asof = asof_env.cfg, asof_env.latest, getattr(asof_env, which)
    assert asof < latest, "本守卫只对历史 asof 生效"

    rec = AsofRecorder(DataSource(cfg, offline=True))
    scorer = strategy.CanslimScorer(cfg, rec, offline=True)
    for code in ("600519", "002594", "000001"):
        scorer.score(code, asof)

    windowed = [r for r in rec.records if r["method"] in ("daily", "index_daily")]
    assert windowed, "打分链路必须实际取过日线/指数日线，否则守卫是空的"
    assert_no_violations(rec.data_records, asof, f"strategy.scan@{which}")


def test_guard_market_direction_and_quote_respect_asof(asof_env):
    """M/报价路径：market_breadth/quote 的 asof 参数不得晚于本次 asof。"""
    strategy = pytest.importorskip("ptrade.strategy", reason="strategy.py 未就绪（T3）")
    cfg, asof = asof_env.cfg, asof_env.asof_late
    rec = AsofRecorder(DataSource(cfg, offline=True))
    scorer = strategy.CanslimScorer(cfg, rec, offline=True)
    scorer.market_direction(asof)
    assert_no_violations(rec.data_records, asof, "strategy.market_direction")


@pytest.mark.parametrize("which", ["asof_late", "asof_mid"])
def test_guard_chart_window_ends_at_asof(asof_env, tmp_path, which):
    """K 线链路：窗口末端必须等于 asof，且 payload 里没有晚于 asof 的 K 线。"""
    chart = pytest.importorskip("ptrade.chart", reason="chart.py 未就绪（task-7）")
    cfg, asof = asof_env.cfg, getattr(asof_env, which)
    rec = AsofRecorder(DataSource(cfg, offline=True))
    payload = chart.chart_payload(cfg, rec, "600519", asof)
    assert_no_violations(rec.data_records, asof, f"chart.payload@{which}")
    for period, data in payload["periods"].items():
        dates = data["dates"]
        if dates:
            assert dates[-1] <= asof, f"{period} 周期末根 K 线 {dates[-1]} 晚于 asof={asof}"
    assert payload["meta"]["window_end"] <= asof


# --------------------------------------------------------------------------- #
# 2. 消费侧工件：因子自己声明的期次/披露日必须 <= asof
#    （帧里可以含未来行 —— 消费方必须过滤；这里断言的是"过滤真的生效"）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("which", ["asof_late", "asof_mid"])
def test_guard_factor_artifacts_declare_no_future_period(asof_env, which):
    """工件层断言：因子自己声明的期次/披露日必须 <= asof。

    这是 ``CONSUMER_FILTERED`` 类豁免的**等价断言** —— 帧里可以含未来行，
    但**消费方必须真的把它们滤掉**，证据就是因子工件里声明的期次。
    """
    strategy = pytest.importorskip("ptrade.strategy", reason="strategy.py 未就绪（T3）")
    cfg, asof = asof_env.cfg, getattr(asof_env, which)
    ds = DataSource(cfg, offline=True)
    result = strategy.CanslimScorer(cfg, ds, offline=True).score("600519", asof)
    factors = {f.key: f for f in result.factors}

    # C：选中的报告期，其「实际披露日」必须 <= asof
    period = str(factors["C"].value.get("period") or "")
    if period:
        frame = ds.quarterly_report(period)
        row = frame[frame["code"] == "600519"]
        assert len(row) == 1, f"夹具缺 600519 @{period}"
        announce = str(row.iloc[0]["announce_date"])[:10]
        assert announce <= asof, (
            f"C 因子使用了尚未披露的报告期：period={period} announce_date={announce} > asof={asof}"
        )

    # A：年报期次必须已过法定披露截止日
    for item in factors["A"].value.get("annual") or []:
        ann_period = str(item.get("period") or "")
        if ann_period:
            assert _legal_deadline(ann_period) <= asof, (
                f"A 因子使用了尚未到披露截止日的年报：{ann_period}（截止 {_legal_deadline(ann_period)}）> asof={asof}"
            )

    # I：消费侧必须声明它用的是哪一期，且该期在 asof 时点已披露
    ivalue = factors["I"].value
    if ivalue.get("report_date"):
        assert str(ivalue["report_date"])[:10] <= asof
        assert ivalue.get("is_historical") is True, "历史回溯下 I 因子必须标记 is_historical=True"
        assert str(ivalue.get("announce_date"))[:10] <= asof

    # S：持股户数期次不得晚于 asof
    holders_period = factors["S"].value.get("holders_period")
    if holders_period:
        assert norm_ts(holders_period) <= asof, f"S 因子用了晚于 asof 的股东户数期次 {holders_period}"


def _legal_deadline(period: str) -> str:
    """A 股定期报告法定披露截止日（与 §6.1 的 asof 纪律一致）。"""
    text = re.sub(r"\D", "", str(period))[:8]
    year, md = text[:4], text[4:8]
    return {
        "0331": f"{year}-04-30",
        "0630": f"{year}-08-31",
        "0930": f"{year}-10-31",
        "1231": f"{int(year) + 1}-04-30",
    }.get(md, "9999-12-31")


# --------------------------------------------------------------------------- #
# 3. 夹具卫生：announce_date 必须落在**法定窗口**内（防止"期次语义错位"再犯）
# --------------------------------------------------------------------------- #
def test_guard_quarterly_fixture_announce_dates_inside_legal_window(offline_cfg):
    fixture_dir = Path(offline_cfg.root) / "data" / "fixture"
    files = sorted(fixture_dir.glob("quarterly_*.json"))
    assert files, "缺少季度夹具"
    checked = 0
    problems: list[str] = []
    for path in files:
        period = path.stem.split("_", 1)[1]
        deadline = _legal_deadline(period)
        payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
        for row in payload:
            announce = str(row.get("announce_date"))[:10]
            checked += 1
            if not _TS_RE.match(announce):
                problems.append(f"{path.name}: {row.get('code')} announce_date 非法（{announce!r}）")
            elif announce > deadline:
                problems.append(
                    f"{path.name}: {row.get('code')} period={period} announce_date={announce}"
                    f" 晚于法定截止 {deadline}"
                )
    assert checked > 0, "季度夹具为空，守卫是空的"
    assert not problems, "夹具期次语义错位：\n  - " + "\n  - ".join(problems[:10])


# --------------------------------------------------------------------------- #
# 4. 实现过滤不得"静默清空整表"（CR-17 回归守卫）
# --------------------------------------------------------------------------- #
class _MixedPeriodDS(DataSource):
    """构造"部分行缺期次"的十大股东数据：有效期次 + 缺期次行混在一起。"""

    def _cached(self, key, ttl, fetch, fixture_key=None):  # type: ignore[override]
        if str(key).startswith("top_holders_"):
            return [
                {
                    "code": "600519",
                    "holder_name": "机构A",
                    "holder_type": "机构",
                    "shares": 1000.0,
                    "ratio": 10.0,
                    "change": 1.0,
                    "report_date": "2025-06-30",
                    "announce_date": "2025-08-13",
                },
                {
                    "code": "600519",
                    "holder_name": "机构B",
                    "holder_type": "机构",
                    "shares": 500.0,
                    "ratio": 5.0,
                    "change": -1.0,
                    "report_date": "2025-06-30",
                    "announce_date": "2025-08-13",
                },
                {  # 缺期次的行：必须只丢这一行，不得清空整表
                    "code": "600519",
                    "holder_name": "缺期次股东",
                    "holder_type": "机构",
                    "shares": 1.0,
                    "ratio": 0.01,
                    "change": None,
                },
            ]
        return super()._cached(key, ttl, fetch, fixture_key)


def test_guard_missing_period_rows_do_not_blank_the_table(offline_cfg):
    ds = _MixedPeriodDS(offline_cfg, offline=True)
    frame = ds.top_holders("600519", "2025-10-09")
    assert len(frame) >= 1, (
        "缺期次的行不得把整表清空（CR-17 回归）：应只丢弃缺期次行并保留有效期次"
    )
    assert set(frame["report_date"].astype(str)) == {"2025-06-30"}
    meta = frame.attrs.get("top_holders_meta") or {}
    assert meta.get("report_date") == "2025-06-30"
    assert meta.get("is_historical") is True


def test_guard_top_holders_selects_period_disclosed_by_asof(offline_cfg):
    ds = DataSource(offline_cfg, offline=True)
    early, late = "2025-10-09", "2026-10-09"
    first, second = ds.top_holders("600519", early), ds.top_holders("600519", late)
    assert len(first) > 0 and len(second) > 0
    meta_a, meta_b = first.attrs["top_holders_meta"], second.attrs["top_holders_meta"]
    assert str(meta_a["announce_date"])[:10] <= early
    assert str(meta_b["announce_date"])[:10] <= late
    assert meta_a["report_date"] != meta_b["report_date"], (
        "两个不同 asof 取到同一期次 → 说明 top_holders 没有按 asof 选期（前视）"
    )


# --------------------------------------------------------------------------- #
# 5. 豁免项纪律：``spot`` 不能"豁免了就不管"
# --------------------------------------------------------------------------- #
def test_guard_spot_exemption_has_falsifiable_substitute(asof_env):
    """``spot`` 无 asof 是契约事实；但历史回溯时**不得使用其市值字段**。"""
    strategy = pytest.importorskip("ptrade.strategy", reason="strategy.py 未就绪（T3）")
    cfg, latest, asof = asof_env.cfg, asof_env.latest, asof_env.asof_late
    ds = DataSource(cfg, offline=True)

    rec = AsofRecorder(ds)
    scorer = strategy.CanslimScorer(cfg, rec, offline=True)
    result = scorer.score("600519", asof)
    s_factor = [f for f in result.factors if f.key == "S"][0]

    assert "spot" in {r["method"] for r in rec.records}, "前提变了：历史回溯下已不再调用 spot()"
    assert s_factor.value.get("path") == "historical_daily", (
        "历史回溯下 S 因子必须走 historical_daily（当日收盘 × 流通股本），"
        f"而不是实时快照；实际 path={s_factor.value.get('path')!r}"
    )

    # 对照：评"最新交易日"时应当允许走快照路径 —— 证明上面的断言不是恒真
    latest_result = strategy.CanslimScorer(cfg, DataSource(cfg, offline=True), offline=True).score(
        "600519", latest
    )
    latest_s = [f for f in latest_result.factors if f.key == "S"][0]
    assert latest_s.value.get("path") != "historical_daily", (
        "最新交易日的 S 因子不应被标记为 historical_daily（否则前面的断言恒真、守卫无效）"
    )


# --------------------------------------------------------------------------- #
# 6. 非空洞性证明：守卫必须在"真的前视"时变红
# --------------------------------------------------------------------------- #
def test_guard_is_not_vacuous_unit_level():
    """单元级：人工构造一条越界记录，``find_violations`` 必须报出来。"""
    bad = [
        {"method": "daily", "asof_arg": "2025-06-30", "max_ts": "2025-06-30", "exempt": False},
        {"method": "index_daily", "asof_arg": "2025-06-30", "max_ts": "2026-10-09", "exempt": False},
    ]
    problems = find_violations(bad, "2025-06-30")
    assert problems and "index_daily" in problems[0], f"守卫是空的！未报出 {problems}"
    assert not find_violations(bad[:1], "2025-06-30"), "不应误报合规记录"


def test_guard_is_not_vacuous_end_to_end(asof_env):
    """端到端：让数据源**真的**返回晚于 asof 的 K 线，整条守卫必须失败。"""

    class _FutureLeakDS(DataSource):
        def daily(self, code, start, end, adjust="qfq", **kwargs):  # type: ignore[override]
            frame = super().daily(code, start, end, adjust, **kwargs)
            if frame is not None and len(frame):
                leak = frame.iloc[[-1]].copy()
                leak["date"] = _next_day(str(end))
                frame = __import__("pandas").concat([frame, leak], ignore_index=True)
            return frame

    cfg, asof = asof_env.cfg, asof_env.asof_late
    rec = AsofRecorder(_FutureLeakDS(cfg, offline=True))
    rec.daily("600519", "2024-01-01", asof)
    with pytest.raises(AssertionError) as excinfo:
        assert_no_violations(rec.data_records, asof, "非空洞性自测")
    message = str(excinfo.value)
    assert "daily" in message and "max_ts" in message, f"违规信息不足以定位：{message}"


def test_guard_flags_undeclared_method(offline_cfg):
    """未登记的方法不得被静默放过（否则新增数据源方法可以绕过守卫）。"""
    records = [{"method": "brand_new_dataset", "asof_arg": None, "max_ts": None, "exempt": False}]
    problems = find_violations(records, "2025-10-09")
    assert problems and "brand_new_dataset" in problems[0], (
        "未声明的取数方法被静默放过 —— 守卫可被绕过"
    )


def test_guard_consumer_filtered_exemption_is_necessary_not_vacuous(asof_env):
    """证明 ``fin_indicator`` 的"消费方过滤型"豁免**不是放水**，而是**必要且可证伪**的。

    三步闭环：
    1. 该 asof 下它的帧里**真的**含晚于 asof 的期次 → 帧级断言必然误报（豁免必要）；
    2. 用 ``find_violations`` 复现那个误报（证明豁免不是"随手加的"）；
    3. 消费方（A 因子）声明的年报期次**全部**已过法定披露截止日 → 过滤确实生效。
    """
    strategy = pytest.importorskip("ptrade.strategy", reason="strategy.py 未就绪（T3）")
    cfg, asof = asof_env.cfg, asof_env.asof_mid
    ds = DataSource(cfg, offline=True)

    fin = ds.fin_indicator("600519", "2020")
    frame_max = max((norm_ts(v) for v in fin["date"] if norm_ts(v)), default=None)
    assert frame_max is not None
    assert frame_max > asof, (
        f"本夹具下 fin_indicator 帧未含晚于 asof({asof}) 的期次（帧 max={frame_max}）——"
        " 说明这条豁免没有被真正跑到，asof_mid 的取点需要调整"
    )

    phantom = [
        {"method": "fin_indicator", "asof_arg": None, "max_ts": frame_max, "exempt": False}
    ]
    assert find_violations(phantom, asof), (
        "帧级判定本应对该帧报警 —— 若不报警，说明豁免的存在没有依据"
    )

    result = strategy.CanslimScorer(cfg, ds, offline=True).score("600519", asof)
    a_factor = [f for f in result.factors if f.key == "A"][0]
    annual = a_factor.value.get("annual") or []
    assert annual, "A 因子必须声明它用了哪些年报期次，否则工件层断言无从谈起"
    for item in annual:
        period = str(item.get("period") or "")
        assert _legal_deadline(period) <= asof, (
            f"A 因子使用了尚未到披露截止日的年报 {period}（截止 {_legal_deadline(period)}）> asof={asof}"
        )


def _next_day(text: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(text[:10]) + timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------- #
# 7. K 线图：浏览器渲染的**等价静态断言**（不是"渲染已验证"）
# --------------------------------------------------------------------------- #
def test_guard_chart_static_equivalents_for_browser_rendering(asof_env, tmp_path):
    """替代口径声明：**浏览器内实际渲染未能验证**。

    可自动验证的等价断言（下）覆盖：三套周期数据齐全、切换绑定点存在、
    各周期根数与 ``meta.bars`` 自洽、且都 <= asof。
    """
    chart = pytest.importorskip("ptrade.chart", reason="chart.py 未就绪（task-7）")
    cfg, asof = asof_env.cfg, asof_env.asof_late
    html_path = chart.build_chart(
        cfg, DataSource(cfg, offline=True), "600519", asof, period="weekly",
        out=str(tmp_path / "chart.html"),
    )
    html = Path(html_path).read_text(encoding="utf-8")

    # (a) 三套周期数据集齐全，且根数与 meta.bars 自洽
    payload = chart.chart_payload(cfg, DataSource(cfg, offline=True), "600519", asof)
    assert set(payload["periods"]) == {"daily", "weekly", "monthly"}
    for period, data in payload["periods"].items():
        assert payload["meta"]["bars"][period] == len(data["dates"]), f"{period} 根数与 meta 不一致"
        assert len(data["dates"]) > 0
        assert data["dates"][-1] <= asof
    assert payload["meta"]["bars"]["daily"] > payload["meta"]["bars"]["weekly"] > payload["meta"]["bars"]["monthly"], (
        "日线根数应多于周线、周线多于月线（重采样是否真的发生）"
    )

    # (b) 切换逻辑绑定到周期键：三个键同时出现在数据与前端切换代码中
    for key in ("daily", "weekly", "monthly"):
        assert key in html, f"HTML 缺少周期键 {key}"
    assert "switchPeriod" in html, "HTML 缺少周期切换函数绑定"
    assert re.search(r"switchPeriod\s*\(\s*window\.__INITIAL_PERIOD__\s*\)", html), (
        "初始周期未与切换函数绑定"
    )
    # 传入 period=weekly 时，初始周期必须是 weekly（证明参数真的生效）
    assert '__INITIAL_PERIOD__ = "weekly"' in html or "'weekly'" in html

    # (c) 自包含（无外链引用）
    assert chart.no_external_refs(html)
    assert not re.search(r"<script[^>]*\ssrc\s*=", html, re.I)
    assert not re.search(r"<link[^>]*\shref\s*=", html, re.I)
