"""离线夹具生成器（T1 独占；`data/fixture/**` 为本模块的写入范围）。

用法（在 `D:\\Ai投资` 下）：:

    $env:PYTHONUTF8='1'; python data/fixture/build_fixtures.py            # 增量生成
    $env:PYTHONUTF8='1'; python data/fixture/build_fixtures.py --force    # 全量重建

生成物（全部遵循 INTERFACES.md §4 的**强制单位约定表**）：

| 文件 | 内容 |
|---|---|
| `trade_dates.json` | 交易日历（升序 YYYY-MM-DD） |
| `daily_<code>.json` | 日线（**前复权**，2024-09-01 起） |
| `daily_<code>_unadjust.json` | 日线（**不复权**，K 线图真实价/成交额） |
| `spot.json` | 全市场快照（`asof` = 最近交易日；比对池标的补齐 `float_mv`/`turnover`） |
| `margin_targets.json` | 沪深两融标的（深市用官方标记，沪市用融券余量判定可融券） |
| `quarterly_<period>.json` | 业绩报表（仅保留比对池标的以控体积；`announce_date` = **真实公告日**） |
| `fin_indicator_<code>.json` | 财务指标（含**年报行**，供 A 因子取最近 3 个年报） |
| `holders_<period>.json` | 股东户数（仅比对池标的） |
| `top_holders_<code>.json` | 十大股东（`ratio` 单位：百分数） |
| `index_sh000001.json` | 上证指数日线（M / L 因子的基准） |
| `industry_map.json` | 行业归属（仅比对池标的，保证离线可闭环） |
| `universe.json` | 比对池代码表 |

**字段语义（重要）**：`announce_date` 是**该期报告的真实披露日**（取自
`stock_report_disclosure` 的「实际披露」，落在法定披露窗口内：一季 ≤4/30、半年报 ≤8/31、
三季 ≤10/31、年报 ≤次年4/30），**不是抓取日**。akshare `stock_yjbb_em` 的「最新公告日期」
对部分股票是**后续更正/重述**日期（实测 `20250331` 曾被记成 `2026-04-25`，晚 1 年 → 会造成
C 因子的前视偏差），因此本生成器**不直接使用**该列。

单位（§4 强制表）：价格=元；`volume`=**股**；`amount`=元；`total_mv`/`float_mv`=元；
`turnover`/`pct_chg`/`*_yoy`/`*_qoq`/`roe`/`gross_margin`/`debt_ratio`/`holders_chg_pct`/`ratio`=**百分数**。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def _resolve_root() -> str:
    """定位仓库根：优先 ``PTRADE_HOME``，否则从本脚本向上找含 ``ptrade/`` 包与 ``pyproject.toml`` 的目录。

    本脚本放在 ``tools/`` 下，因此不能再假设「上两级即仓库根」。
    """
    env = os.environ.get("PTRADE_HOME")
    if env and env.strip():
        return os.path.abspath(os.path.expanduser(env.strip()))
    cur = HERE
    for _ in range(6):
        nxt = os.path.dirname(cur)
        if nxt == cur:
            break
        if os.path.isdir(os.path.join(nxt, "ptrade")) and os.path.isfile(
            os.path.join(nxt, "pyproject.toml")
        ):
            return nxt
        cur = nxt
    return os.path.dirname(HERE)


ROOT = _resolve_root()
sys.path.insert(0, ROOT)

from ptrade.config import load_config  # noqa: E402
from ptrade.datasource import (  # noqa: E402
    QUARTER_COLUMNS,
    SPOT_COLUMNS,
    STORY_CODES,
    UNIVERSE_CODES,
    DataSource,
    _norm_code,
    _records,
)

DAILY_START = "2024-09-01"
FIN_START_YEAR = "2021"
# 20241231 是**上季链的锚**：asof 落在 2025 上半年时，C 的加速加成需要「上季同比」，
# 缺这一期会让链条断在 20250331（CR-12）。
QUARTER_PERIODS = ["20241231", "20250331", "20250630", "20250930", "20251231", "20260331", "20260630"]
HOLDERS_PERIODS = ["20250630", "20251231", "20260630"]
UN_ADJUST_CODES = ["600519", "002594", "000001", "601398", "600110", "588000", "510050", "159915"]
FIN_CODES = list(UNIVERSE_CODES)  # 全比对池：A 因子（年报）需要覆盖任意被打分标的
TOPH_CODES = STORY_CODES + ["002415"]
INDUSTRY_CODES = list(UNIVERSE_CODES)


class Builder:
    def __init__(self, force: bool = False) -> None:
        self.cfg = load_config(ROOT)
        self.ds = DataSource(self.cfg, offline=False)
        self.force = force
        self.written: list[str] = []
        self.skipped: list[str] = []
        self.failed: list[tuple[str, str]] = []

    # ---- 工具 ---- #
    def path(self, key: str) -> str:
        return os.path.join(self.ds.fixture_dir, f"{key}.json")

    def exists(self, key: str) -> bool:
        return os.path.isfile(self.path(key))

    def write(self, key: str, payload: Any) -> None:
        if self.exists(key) and not self.force:
            self.skipped.append(key)
            return
        self.ds._write_entry(self.ds.fixture_dir, key, payload)
        size = os.path.getsize(self.path(key))
        self.written.append(f"{key} ({size / 1024:.0f} KB)")
        print(f"  [写入] {key:38s} {size / 1024:8.0f} KB", flush=True)

    def step(self, label: str, fn: Callable[[], None]) -> None:
        t0 = time.time()
        print(f"[{label}] ...", flush=True)
        try:
            fn()
            print(f"[{label}] 完成 ({time.time() - t0:.1f}s)", flush=True)
        except Exception as exc:  # noqa: BLE001 - 单个数据集失败不阻塞整体
            self.failed.append((label, f"{type(exc).__name__}: {exc}"))
            print(f"[{label}] 失败: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()

    # ---- 各数据集 ---- #
    def build_trade_dates(self) -> None:
        self.write("trade_dates", self.ds._fetch_trade_dates())

    def build_daily(self) -> None:
        end = self.ds.latest_trade_date()
        print(f"  日线区间 {DAILY_START} ~ {end}（{len(UNIVERSE_CODES)} 个标的）", flush=True)
        for code in UNIVERSE_CODES:
            key = f"daily_{_norm_code(code)}"
            if self.exists(key) and not self.force:
                self.skipped.append(key)
                continue
            try:
                payload = self.ds._fetch_daily(_norm_code(code), "qfq", DAILY_START, end)
                if not payload:
                    raise RuntimeError("空数据")
                self.write(key, payload)
            except Exception as exc:  # noqa: BLE001
                self.failed.append((key, f"{type(exc).__name__}: {exc}"))
                print(f"  [失败] {key}: {type(exc).__name__}: {exc}", flush=True)
        for code in UN_ADJUST_CODES:
            key = f"daily_{_norm_code(code)}_unadjust"
            if self.exists(key) and not self.force:
                self.skipped.append(key)
                continue
            try:
                payload = self.ds._fetch_daily(_norm_code(code), "", DAILY_START, end)
                if not payload:
                    raise RuntimeError("空数据")
                self.write(key, payload)
            except Exception as exc:  # noqa: BLE001
                self.failed.append((key, f"{type(exc).__name__}: {exc}"))
                print(f"  [失败] {key}: {type(exc).__name__}: {exc}", flush=True)

    def build_spot(self) -> None:
        payload = self.ds._fetch_spot()
        frame = self.ds._frame(payload, SPOT_COLUMNS)
        if len(frame) == 0:
            raise RuntimeError("空快照")
        frame["code"] = frame["code"].astype(str).map(_norm_code)
        wanted = {_norm_code(c) for c in UNIVERSE_CODES}
        idx = frame.index[frame["code"].isin(wanted)]
        enriched = self.ds._enrich_spot(frame.loc[idx])
        frame.loc[enriched.index, ["float_mv", "turnover"]] = enriched[["float_mv", "turnover"]]
        latest = self.ds.latest_trade_date()
        self.write("spot", {"asof": latest, "rows": _records(frame, SPOT_COLUMNS)})
        print(f"  快照 {len(frame)} 行，asof={latest}，比对池已补 float_mv/turnover", flush=True)

    def build_margin_targets(self) -> None:
        rows = self.ds._fetch_margin_targets(None)
        merged = {str(r["code"]): r for r in rows}
        # 剧情标的必须齐备；601398 作为「融资标的但不可融券」负例
        missing = [c for c in STORY_CODES if c not in merged]
        print(f"  两融标的 {len(rows)} 个；剧情标的缺失: {missing or '无'}", flush=True)
        for code in missing:
            raise RuntimeError(f"两融名单缺少剧情标的 {code}")
        self.write("margin_targets", rows)

    def build_quarterly(self) -> None:
        keep = {_norm_code(c) for c in INDUSTRY_CODES}
        for period in QUARTER_PERIODS:
            rows = self.ds._fetch_quarterly(period)
            if not rows:
                print(f"  报告期 {period}: 空，跳过", flush=True)
                continue
            slim = [r for r in rows if _norm_code(r.get("code", "")) in keep]
            if not slim:
                print(f"  报告期 {period}: 比对池无数据，跳过", flush=True)
                continue
            # CR-8：announce_date 一律用「实际披露日」（真实公告日），不用 yjbb 的抓取/更正日
            real = self._disclosure_map(period)
            fixed = missing = 0
            for row in slim:
                code = _norm_code(row.get("code", ""))
                if code in real:
                    if str(row.get("announce_date"))[:10] != real[code]:
                        fixed += 1
                    row["announce_date"] = real[code]
                else:
                    missing += 1
            days = sorted({str(r["announce_date"])[:10] for r in slim if r.get("announce_date")})
            print(f"    公告日修正 {fixed} 行，缺真实公告日 {missing} 行，范围 {days[0] if days else '-'} ~ {days[-1] if days else '-'}", flush=True)
            self.write(f"quarterly_{period}", slim)
            hit = [r for r in slim if _norm_code(r["code"]) in {"600519", "000001"}]
            for row in hit:
                print(
                    f"    {period} {row['code']} 净利润同比={row.get('net_profit_yoy')} "
                    f"行业={row.get('industry')}",
                    flush=True,
                )

    def build_fin_indicator(self) -> None:
        for code in FIN_CODES:
            key = f"fin_indicator_{_norm_code(code)}"
            if self.exists(key) and not self.force:
                self.skipped.append(key)
                continue
            try:
                frame = self.ds._call(
                    self.ds._ak().stock_financial_analysis_indicator,
                    symbol=_norm_code(code), start_year=FIN_START_YEAR,
                )
                rows = self.ds._map_fin_indicator(frame)
                if not rows:
                    raise RuntimeError("空数据")
                annual = [r for r in rows if str(r["date"]).endswith("12-31")]
                self.write(key, rows)
                print(f"    {code}: {len(rows)} 行，年报行 {len(annual)} 个", flush=True)
            except Exception as exc:  # noqa: BLE001
                self.failed.append((key, f"{type(exc).__name__}: {exc}"))
                print(f"  [失败] {key}: {exc}", flush=True)

    def build_holders(self) -> None:
        keep = {_norm_code(c) for c in INDUSTRY_CODES}
        for period in HOLDERS_PERIODS:
            key = f"holders_{period}"
            if self.exists(key) and not self.force:
                self.skipped.append(key)
                continue
            try:
                rows = self.ds._fetch_holders(period)
                slim = [r for r in rows if _norm_code(r.get("code", "")) in keep]
                if not slim:
                    raise RuntimeError("比对池无数据")
                self.write(key, slim)
                hit = [r for r in slim if _norm_code(r["code"]) == "600519"]
                if hit:
                    print(f"    600519 股东户数变化={hit[0].get('holders_chg_pct')}%", flush=True)
            except Exception as exc:  # noqa: BLE001
                self.failed.append((key, f"{type(exc).__name__}: {exc}"))
                print(f"  [失败] {key}: {exc}", flush=True)

    DISCLOSURE_KEYS = {"0331": "一季", "0630": "半年报", "0930": "三季", "1231": "年报"}

    def _disclosure_map(self, period: str) -> dict[str, str]:
        """{code: 实际披露日} —— 该期报告的**真实公告日**（法定窗口内）。

        来源：``stock_report_disclosure(market='沪深京', period='2025一季' | '2025半年报' | '2025三季' | '2025年报')``。
        """
        cache = getattr(self, "_disclosure_cache", None)
        if cache is None:
            cache = {}
            self._disclosure_cache = cache
        if period in cache:
            return cache[period]
        result: dict[str, str] = {}
        year, tail = period[:4], period[4:]
        name = self.DISCLOSURE_KEYS.get(tail)
        if not name:
            cache[period] = result
            return result
        try:
            frame = self.ds._call(
                self.ds._ak().stock_report_disclosure, market="沪深京", period=f"{year}{name}"
            )
            codes = frame["股票代码"].astype(str).str.zfill(6)
            actual = frame["实际披露"].astype(str).str[:10]
            for code, day in zip(codes, actual):
                if day and day not in ("NaT", "None", "nan"):
                    result[code] = day
        except Exception as exc:  # noqa: BLE001 - 取不到就保留源值并计数
            print(f"  [警告] {period} 实际披露日获取失败: {type(exc).__name__}: {exc}", flush=True)
        cache[period] = result
        return result

    def _total_share_map(self) -> dict[str, float]:
        """总股本（股）映射：用于补齐十大股东缺失的持股比例（真实数据计算，非虚构）。"""
        if getattr(self, "_shares_cache", None) is None:
            shares: dict[str, float] = {}
            for period in sorted(HOLDERS_PERIODS, reverse=True):
                try:
                    rows = self.ds._fetch_holders(period)
                except Exception:  # noqa: BLE001
                    continue
                for row in rows:
                    code = _norm_code(row.get("code", ""))
                    value = row.get("total_share")
                    if code and value and code not in shares:
                        try:
                            shares[code] = float(value)
                        except (TypeError, ValueError):
                            continue
                if shares:
                    print(f"  总股本取自 holders_{period}（{len(shares)} 个标的）", flush=True)
                    break
            self._shares_cache = shares
        return self._shares_cache

    def _fill_missing_ratio(self, rows: list[dict], code: str) -> tuple[list[dict], int]:
        """``ratio`` 缺失时用 持股数量 / 总股本 × 100 补齐（源接口对部分股东返回空）。"""
        total_share = self._total_share_map().get(_norm_code(code))
        filled = 0
        if not total_share:
            return rows, 0
        for row in rows:
            if row.get("ratio") is not None and row.get("shares"):
                continue
            try:
                shares = float(row.get("shares") or 0)
            except (TypeError, ValueError):
                continue
            if shares > 0:
                row["ratio"] = round(shares / total_share * 100.0, 4)
                filled += 1
        return rows, filled

    TOPH_MIN_PERIOD = "2023-01-01"  # 保留的真实期次下界（控体积；期内全部期次都留）

    def build_top_holders(self) -> None:
        for code in TOPH_CODES:
            key = f"top_holders_{_norm_code(code)}"
            if self.exists(key) and not self.force:
                self.skipped.append(key)
                continue
            try:
                frame = self.ds._call(self.ds._ak().stock_main_stock_holder, stock=_norm_code(code))
                rows = self.ds._map_top_holders(frame, _norm_code(code))
                if not rows:
                    raise RuntimeError("空数据")
                # 保留多个**真实报告期**（top_holders(asof=...) 据此防前视）
                rows = [r for r in rows if str(r.get("report_date") or "") >= self.TOPH_MIN_PERIOD]
                if not rows:
                    raise RuntimeError("过滤后无数据")
                rows, filled = self._fill_missing_ratio(rows, code)
                missing = sum(1 for r in rows if r.get("ratio") is None)
                periods = sorted({str(r.get("report_date")) for r in rows})
                print(f"    {code}: {len(rows)} 行 / {len(periods)} 期次（{periods[0]}~{periods[-1]}），"
                      f"ratio 补齐 {filled} 行，仍缺 {missing} 行", flush=True)
                self.write(key, rows)
            except Exception as exc:  # noqa: BLE001
                self.failed.append((key, f"{type(exc).__name__}: {exc}"))
                print(f"  [失败] {key}: {exc}", flush=True)

    def build_index(self) -> None:
        end = self.ds.latest_trade_date()
        for key, code in (("index_sh000001", "sh000001"),):
            if self.exists(key) and not self.force:
                self.skipped.append(key)
                continue
            try:
                payload = self.ds._fetch_index(code, DAILY_START, end)
                if not payload:
                    raise RuntimeError("空数据")
                self.write(key, payload)
                print(f"    {code}: {len(payload)} 行 ({payload[0]['date']} ~ {payload[-1]['date']})", flush=True)
            except Exception as exc:  # noqa: BLE001
                self.failed.append((key, f"{type(exc).__name__}: {exc}"))
                print(f"  [失败] {key}: {exc}", flush=True)

    def build_industry_and_universe(self) -> None:
        # 行业归属：取最近一期有数据的业绩报表的「所处行业」，仅保留比对池（保证离线可闭环）
        mapping: dict[str, str] = {}
        for period in sorted(QUARTER_PERIODS, reverse=True):
            key = f"quarterly_{period}"
            if not self.exists(key):
                continue
            with open(self.path(key), "r", encoding="utf-8") as fh:
                rows = json.load(fh).get("payload") or []
            for row in rows:
                code = _norm_code(row.get("code", ""))
                industry = str(row.get("industry") or "").strip()
                if code and industry and industry.lower() != "nan":
                    mapping.setdefault(code, industry)
            if mapping:
                print(f"  行业取自 quarterly_{period}", flush=True)
                break
        rows = [{"code": c, "industry": mapping.get(c, "")} for c in INDUSTRY_CODES]
        self.write("industry_map", {"rows": rows})
        filled = sum(1 for r in rows if r["industry"])
        print(f"  行业归属 {filled}/{len(rows)} 个标的非空", flush=True)
        self.write("universe", {"rows": [_norm_code(c) for c in UNIVERSE_CODES]})

    # ---- 单位自检 ---- #
    def unit_check(self) -> None:
        print("\n[单位自检（§4 强制表）]", flush=True)
        problems: list[str] = []

        def load(key: str) -> Any:
            with open(self.path(key), "r", encoding="utf-8") as fh:
                return json.load(fh).get("payload")

        daily = load("daily_600519") or []
        if daily:
            last = daily[-1]
            print(f"  daily_600519: {len(daily)} 行，末行 {last['date']} close={last['close']} "
                  f"volume={last['volume']:.0f} amount={last['amount']:.0f} turnover={last['turnover']}",
                  flush=True)
            if not (1e5 < float(last["volume"]) < 1e9):
                problems.append("volume 量级不像「股」")
            if not (1e6 < float(last["amount"]) < 1e11):
                problems.append("amount 量级不像「元」")
            if not (0 < float(last["turnover"]) < 100):
                problems.append("turnover 不像百分数")
        spot = (load("spot") or {}).get("rows") or []
        row = next((r for r in spot if str(r.get("code")) == "600519"), None)
        if row:
            print(f"  spot 600519: last={row['last']} float_mv={row['float_mv']} "
                  f"turnover={row['turnover']} limit_up={row['limit_up']}", flush=True)
            if row.get("float_mv") and not (1e9 < float(row["float_mv"]) < 1e13):
                problems.append("float_mv 量级不像「元」")
        fin = load("fin_indicator_600519") or []
        annual = [r for r in fin if str(r["date"]).endswith("12-31")]
        if fin:
            print(f"  fin_indicator_600519: {len(fin)} 行，年报 {len(annual)} 行，"
                  f"最新 roe={fin[-1].get('roe')} net_profit_yoy={fin[-1].get('net_profit_yoy')}", flush=True)
            if len(annual) < 3:
                problems.append("年报行 < 3（A 因子需要最近 3 个年报）")
            roe = next((r.get("roe") for r in reversed(fin) if r.get("roe") is not None), None)
            if roe is not None and abs(float(roe)) > 150.0:
                problems.append("roe 超过 150%，可能不是百分数")
            if roe is not None and abs(float(roe)) < 1.0 and abs(float(roe)) > 0:
                problems.append("roe < 1，可能是小数而非百分数")
        toph_all = load("top_holders_600519") or []
        periods = sorted({str(r.get("report_date")) for r in toph_all})
        toph = [r for r in toph_all if str(r.get("report_date")) == periods[-1]] if periods else toph_all
        if toph:
            print(f"  top_holders_600519: {len(toph_all)} 行 / {len(periods)} 期次，"
                  f"最新期 {periods[-1] if periods else '-'}（{len(toph)} 行）", flush=True)
            missing = sum(1 for r in toph if r.get("ratio") is None)
            if missing:
                problems.append(f"top_holders_600519 仍有 {missing} 行 ratio 为空")
            ratios = [float(r["ratio"]) for r in toph if r.get("ratio") is not None]
            print(f"  top_holders_600519: {len(toph)} 行，ratio 合计={sum(ratios):.2f}，"
                  f"样例={toph[0]['holder_name'] if toph else ''}", flush=True)
            if ratios and sum(ratios) <= 1.5:
                problems.append("ratio 合计 <=1.5，可能不是百分数")
        mt = load("margin_targets") or []
        if mt:
            by = {str(r["code"]): r for r in mt}
            print(f"  margin_targets: {len(mt)} 个；"
                  + "；".join(f"{c}: finance={by[c]['can_finance']} short={by[c]['can_short']}"
                              for c in STORY_CODES if c in by), flush=True)
            for code in STORY_CODES:
                if code not in by:
                    problems.append(f"两融名单缺少 {code}")
            # 负例（融资标的但不可融券）必须真实存在：沪市用「融券余量>0」判定可融券，
            # 因此融券余量=0 的融资标的即为负例（600110 在 2026-10-09 满足）。
            negatives = [c for c, r in by.items() if r["can_finance"] and not r["can_short"]]
            print(f"    可融资但不可融券的负例 {len(negatives)} 个，样例 {negatives[:6]}", flush=True)
            if not negatives:
                problems.append("两融名单里没有 can_finance=True 且 can_short=False 的负例")
            if "600110" not in by:
                problems.append("两融名单缺少负例 600110")
            elif by["600110"]["can_short"]:
                problems.append("600110 应为 can_short=False 的融资标的负例")
        idx = load("index_sh000001") or []
        if idx:
            print(f"  index_sh000001: {len(idx)} 行，末行 {idx[-1]['date']} close={idx[-1]['close']}", flush=True)
        q = load("quarterly_20250630") or []
        story = {str(r["code"]): r for r in q if str(r["code"]) in {"600519", "002594", "000001"}}
        for code, row in story.items():
            print(f"  quarterly_20250630 {code}: net_profit_yoy={row.get('net_profit_yoy')} "
                  f"roe={row.get('roe')}", flush=True)
        if problems:
            print("\n  ⚠ 单位/剧情自检问题:", flush=True)
            for p in problems:
                print(f"    - {p}", flush=True)
        else:
            print("  单位自检全部通过", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 data/fixture 离线夹具")
    parser.add_argument("--force", action="store_true", help="覆盖已存在夹具")
    parser.add_argument("--clean", action="store_true", help="先清空 fixture 目录（保留本脚本）")
    parser.add_argument("--only", default="", help="只跑指定步骤，逗号分隔")
    args = parser.parse_args()

    if args.clean:
        # 清空的是数据根下的 data/fixture/，不是脚本所在目录（脚本在 tools/）
        target = os.path.join(ROOT, "data", "fixture")
        os.makedirs(target, exist_ok=True)
        n = 0
        for name in os.listdir(target):
            if name.endswith(".json"):
                os.remove(os.path.join(target, name))
                n += 1
        print(f"已清空旧夹具（{target}，删除 {n} 个）", flush=True)

    builder = Builder(force=args.force)
    steps = [
        ("交易日历", builder.build_trade_dates),
        ("日线", builder.build_daily),
        ("指数日线", builder.build_index),
        ("两融标的", builder.build_margin_targets),
        ("业绩报表", builder.build_quarterly),
        ("财务指标", builder.build_fin_indicator),
        ("股东户数", builder.build_holders),
        ("十大股东", builder.build_top_holders),
        ("快照", builder.build_spot),
        ("行业与比对池", builder.build_industry_and_universe),
    ]
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    t0 = time.time()
    for label, fn in steps:
        if only and label not in only:
            continue
        builder.step(label, fn)

    print(f"\n夹具目录: {builder.ds.fixture_dir}", flush=True)
    print(f"写入 {len(builder.written)} 个 / 跳过 {len(builder.skipped)} 个 / 失败 {len(builder.failed)} 个"
          f"（耗时 {time.time() - t0:.1f}s）", flush=True)
    for label, err in builder.failed:
        print(f"  失败: {label} -> {err}", flush=True)
    try:
        builder.unit_check()
    except Exception as exc:  # noqa: BLE001
        print(f"单位自检异常: {exc}", flush=True)
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
