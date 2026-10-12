"""生成 `data/scenario/` —— **人为构造的剧本数据（SYNTHETIC）**。

⚠️ 这里的数字**不是真实行情**，只用于单元测试标定「强票 / 弱票」打分路径。
每个 JSON 顶层带 ``"_synthetic": true`` 标记；``DataSource`` 默认**不读**该目录
（仅 ``DataSource(cfg, offline=True, scenario=True)`` 时叠加），因此 `--offline` 的
CLI 路径绝不会把剧本数据当成真实数据用。

用法::

    $env:PYTHONUTF8='1'; python data/scenario/build_scenario.py [--force]

剧本设计（对齐 strategy-builder 的推演，asof = 2026-10-09）：

| 标的 | 剧本 | 期望因子 |
|---|---|---|
| 600519 | 单调上行 + 末值即 250 日新高 + 放量；2025Q2 净利同比 68%（上季 35% → 加速） | C=100, N=100, S=100, L≥75, I≥85, M≈88 → **total ≥ 75 且 signal=buy** |
| 000001 | 单调下行（250 日回撤 ≈ 43%）+ 净利同比为负 | N=25, C=15, L=20 → **total 明显 < 50（avoid/short）** |
| 指数 | 温和上行（站上 MA50/MA200） | M 的指数分项满分 |
| 快照 | 涨跌家数 4:1、涨停 24 家 | M 的宽度分项 +20/+8 |
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def _resolve_root() -> str:
    """定位仓库根：优先 ``PTRADE_HOME``，否则从本脚本向上找含 ``ptrade/`` 包与 ``pyproject.toml`` 的目录。"""
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
FIXTURE = os.path.join(ROOT, "data", "fixture")
SCENARIO = os.path.join(ROOT, "data", "scenario")
DAILY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]
SPOT_COLUMNS = [
    "code", "name", "last", "pct_chg", "open", "high", "low", "prev_close",
    "volume", "amount", "turnover", "pe_ttm", "pb", "total_mv", "float_mv",
    "limit_up", "limit_down",
]


def _load_fixture(key: str):
    path = os.path.join(FIXTURE, f"{key}.json")
    if not os.path.isfile(path):
        raise SystemExit(f"缺少夹具 {path}（请先跑 tools/build_fixtures.py 或 tools/fetch-fixtures.py）")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)["payload"]


def _envelope(payload, note: str):
    return {
        "_synthetic": True,
        "_note": f"SYNTHETIC / 剧本数据，非真实行情 —— {note}",
        "payload": payload,
    }


def _write(key: str, payload, note: str, force: bool) -> None:
    # 剧本写入数据根下的 data/scenario/（不再是脚本所在目录）
    os.makedirs(SCENARIO, exist_ok=True)
    path = os.path.join(SCENARIO, f"{key}.json")
    if os.path.isfile(path) and not force:
        print(f"  [跳过] {key}")
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_envelope(payload, note), fh, ensure_ascii=False)
    print(f"  [写入] {key:28s} {os.path.getsize(path) / 1024:6.0f} KB")


def _ramp(dates, start: float, end: float, volume: float, wiggle: float = 0.0):
    """确定性斜坡（无随机数）：close 从 start 线性到 end，high/low 围绕 close。"""
    rows = []
    n = len(dates)
    for i, day in enumerate(dates):
        t = i / max(n - 1, 1)
        close = start + (end - start) * t
        if wiggle:
            close *= 1.0 + wiggle * math.sin(i * 0.7)
        # 成交量：最后 20 个交易日放量 1.8 倍（触发「突破 + 放量」规则）
        vol = volume * (1.8 if i >= n - 20 else 1.0)
        high = close * 1.004
        low = close * 0.996 if i == n - 1 else close * 0.99
        rows.append({
            "date": day,
            "open": round(close * 0.998, 3),
            "high": round(high, 3),
            "low": round(min(low, close), 3),
            "close": round(close, 3),
            "volume": round(vol, 0),
            "amount": round(vol * close, 0),
            "pct_chg": round((end - start) / max(n - 1, 1) / max(start, 1e-9) * 100, 4),
            "turnover": round(4.0 if volume > 1e6 else 1.0, 4),
        })
    return rows


def build(force: bool) -> None:
    os.makedirs(HERE, exist_ok=True)
    real_daily = _load_fixture("daily_600519")
    dates = [r["date"] for r in real_daily]
    asof = dates[-1]

    # ---- 强票剧本：600519 上行到年内新高，放量 ----
    strong = _ramp(dates, start=760.0, end=1000.0, volume=3.6e6)
    _write("daily_600519", strong, "600519 强票剧本：上行 + 末值新高 + 放量", force)

    # ---- 弱票剧本：000001 下行（250 日回撤 ≈43%）----
    weak = _ramp(dates, start=20.0, end=4.0, volume=6.0e7)  # 单边下行：任意 250 日窗口回撤 >= 35%
    _write("daily_000001", weak, "000001 弱票剧本：单边下行", force)

    # ---- 指数剧本：温和上行（站上 MA50/MA200）----
    real_index = _load_fixture("index_sh000001")
    index_rows = []
    for i, row in enumerate(real_index):
        t = i / max(len(real_index) - 1, 1)
        close = 3000.0 + 400.0 * t
        index_rows.append({
            "date": row["date"], "open": round(close * 0.999, 2), "high": round(close * 1.003, 2),
            "low": round(close * 0.997, 2), "close": round(close, 2),
            "volume": 3.0e8, "amount": 4.0e11, "pct_chg": 0.08,
        })
    _write("index_sh000001", index_rows, "指数剧本：温和上行", force)

    # ---- 业绩剧本：**覆盖全部 6 期**，使任意 asof 都不会回落真实数据 ----
    # 每期 600519 同比递增（每期都比上期高 → 恒触发「加速加成」）；000001 恒为负增长。
    # 含 20241231（上季锚，CR-12）：链条 20 → 35 → 68 → 72 → 75 → 78 → 80 单调递增
    GROWTH = {"20241231": 20.0, "20250331": 35.0, "20250630": 68.0, "20250930": 72.0,
              "20251231": 75.0, "20260331": 78.0, "20260630": 80.0}
    for period, yoy in GROWTH.items():
        book = {r["code"]: dict(r) for r in _load_fixture(f"quarterly_{period}")}
        if "600519" in book:
            book["600519"]["net_profit_yoy"] = yoy
            book["600519"]["net_profit_qoq"] = yoy / 2
            book["600519"]["roe"] = 24.0
        if "000001" in book:
            book["000001"]["net_profit_yoy"] = -22.0
            book["000001"]["net_profit_qoq"] = -5.0
            book["000001"]["roe"] = 3.0
        _write(f"quarterly_{period}", list(book.values()),
               f"600519 净利同比 {yoy}%（逐期加速）", force)

    # ---- 机构认同剧本：全部为机构型股东，合计 ~35%，环比增加 ----
    holders = []
    plan = [
        ("中国某某证券投资基金", "证券投资基金", 6.0, 0.8),
        ("某某人寿保险股份有限公司", "保险", 5.0, 0.5),
        ("全国社保基金一一二组合", "社保", 4.0, 0.3),
        ("某某证券股份有限公司", "证券", 3.5, 0.2),
        ("某某QFII基金", "QFII", 3.0, 0.4),
        ("某某资产管理计划", "资管", 3.0, 0.1),
        ("某某产业投资基金", "产业基金", 2.5, 0.2),
        ("某某信托有限责任公司", "信托", 2.5, 0.15),
        ("某某银行股份有限公司", "银行", 2.5, 0.1),
        ("某某集团有限公司", "法人", 3.0, 0.25),
    ]
    for name, kind, ratio, change in plan:
        holders.append({
            "code": "600519", "holder_name": name, "holder_type": kind,
            "shares": round(ratio / 100 * 1.256e9, 0), "ratio": ratio, "change": change,
            # 期次 = 2025H1（真实披露日 2025-08-13）→ asof >= 2025-08-13 才可见，防前视
            "report_date": "2025-06-30", "announce_date": "2025-08-13",
        })
    _write("top_holders_600519", holders,
           "机构合计 35%（10%-60% 区间）且环比增加；期次 2025H1", force)

    # ---- 快照剧本：涨跌家数 4:1、涨停 24 家、强票 float_mv 落在 50-200 亿档 ----
    rows = []
    base_codes = ["600519", "000001", "002594", "600036", "601398", "000858", "600030", "300750"]
    for idx in range(120):
        code = base_codes[idx % len(base_codes)] if idx < len(base_codes) else f"9{idx:05d}"
        prev = 10.0 + (idx % 17)
        if idx < 24:  # 24 家涨停
            last = round(prev * 1.10, 2)
        elif idx < 96:  # 上涨
            last = round(prev * (1 + 0.01 + (idx % 5) * 0.002), 2)
        else:  # 下跌
            last = round(prev * 0.99, 2)
        code_final = code if idx < len(base_codes) else f"{900000 + idx}"
        rows.append({
            "code": code_final, "name": f"剧本{code_final}", "last": last,
            "pct_chg": round((last / prev - 1) * 100, 3), "open": prev, "high": last,
            "low": prev * 0.99, "prev_close": prev, "volume": 5.0e6, "amount": 5.0e6 * last,
            "turnover": 5.0, "pe_ttm": 20.0, "pb": 3.0, "total_mv": 1.2e11, "float_mv": 1.2e11,
            "limit_up": round(prev * 1.10, 2), "limit_down": round(prev * 0.90, 2),
        })
    overrides = {"600519": dict(last=1000.0, prev_close=990.0, float_mv=1.2e10, turnover=5.0, limit_up=1089.0, limit_down=891.0),
                 "000001": dict(last=8.0, prev_close=8.2, float_mv=3.0e11, turnover=1.0)}
    for row in rows:
        if row["code"] in overrides:
            row.update(overrides[row["code"]])
    _write("spot", {"asof": README_ASOF, "rows": rows},
           "涨跌家数 4:1 + 涨停 22 家；600519 float_mv=120 亿/换手 5%（asof 与 README 示例对齐）", force)


SELF_CONSISTENT_WINDOW = ("2025-08-13", "2026-10-09")
README_ASOF = "2025-10-09"


def check() -> int:
    """脚本自检：README 示例 asof 必须落在剧本自洽窗口内，且剧本形态在**该 asof 下**成立。"""
    sys.path.insert(0, ROOT)
    import json as _json

    from ptrade.config import load_config
    from ptrade.datasource import DataSource

    problems: list[str] = []
    # 剧本说明文档可能位于数据根（生成物）或仓库内（元数据）——两处都找。
    readme = ""
    for cand in (
        os.path.join(ROOT, "data", "scenario", "README.md"),
        os.path.join(HERE, "scenario-README.md"),
        os.path.join(ROOT, "docs", "scenario-README.md"),
    ):
        if os.path.isfile(cand):
            readme = cand
            break
    if not readme:
        problems.append(
            "找不到剧本 README（期望 <root>/data/scenario/README.md）；"
            "先用 --force 生成，或确认数据根正确"
        )
        text = ""
    else:
        with open(readme, "r", encoding="utf-8") as fh:
            text = fh.read()
    if text and f'asof="{README_ASOF}"' not in text and f"'{README_ASOF}'" not in text:
        problems.append(f"README 示例 asof 不是 {README_ASOF}")
    low, high = SELF_CONSISTENT_WINDOW
    if not (low <= README_ASOF <= high):
        problems.append(f"README asof {README_ASOF} 不在自洽窗口 [{low}, {high}]")

    cfg = load_config(ROOT)
    ds = DataSource(cfg, offline=True, scenario=True)
    asof = README_ASOF
    # 1) 该 asof 已披露的最新报告期必须在剧本里有覆盖（否则回落真实数据）
    periods = [p for p in ("20250331", "20250630", "20250930", "20251231", "20260331", "20260630")]
    disclosed = [p for p in periods if str(_announce(p)) <= asof.replace("-", "")]
    if not disclosed:
        problems.append(f"{asof} 之前无已披露报告期")
    else:
        latest = disclosed[-1]
        # 剧本目录随数据根走（不再是脚本所在目录）。
        scen_dir = getattr(ds, "scenario_dir", os.path.join(ROOT, "data", "scenario"))
        if not os.path.isfile(os.path.join(scen_dir, f"quarterly_{latest}.json")):
            problems.append(
                f"剧本缺少 quarterly_{latest}.json（该 asof 的最新已披露期）；"
                f"在 {scen_dir} 下未找到"
            )
    # 2) 强票形态
    strong = ds.daily("600519", "2024-09-01", asof).tail(250)
    if len(strong) < 200:
        problems.append("600519 日线不足 200 根")
    ratio = float(strong.iloc[-1]["close"]) / float(strong["high"].max())
    if ratio < 0.98:
        problems.append(f"600519 末值/250日最高 = {ratio:.4f} < 0.98（N 因子拿不到 100）")
    # 3) 弱票形态
    weak = ds.daily("000001", "2024-09-01", asof).tail(250)
    drawdown = 1 - float(weak.iloc[-1]["close"]) / float(weak["high"].max())
    if drawdown < 0.35:
        problems.append(f"000001 250 日回撤 {drawdown:.2%} < 35%")
    # 4) 指数与宽度
    breadth = ds.market_breadth(asof)
    if not (breadth["above_ma50"] and breadth["above_ma200"]):
        problems.append("指数未同时站上 MA50/MA200")
    # 宽度分项只在「asof == 快照 asof」时走剧本快照；其它 asof 走比对池日线口径，
    # M 的门禁（>=50）由「基础 20 + MA50/MA200 各 20」保证，故此处不强制宽度值。
    spot_entry = ds._read_fixture("spot") or {}
    spot_asof = str((spot_entry.get("payload") or {}).get("asof", ""))[:10]
    if spot_asof == asof:
        if breadth["decliners"] and breadth["advancers"] / breadth["decliners"] <= 2:
            problems.append("涨跌家数比 <= 2")
        if breadth["limit_up_count"] < 20:
            problems.append(f"涨停家数 {breadth['limit_up_count']} < 20")
    else:
        print(f"  （asof={asof} != 快照 asof={spot_asof} → 宽度走日线口径，不作剧本断言）")
    # 5) top_holders 期次在 asof 前已披露
    frame = ds.top_holders("600519", asof)
    meta = getattr(frame, "attrs", {}).get("top_holders_meta", {})
    if len(frame) == 0:
        problems.append(f"top_holders({asof}) 为空（剧本期次 {meta.get('report_date')}）")
    elif not meta.get("is_historical"):
        problems.append("剧本 top_holders 期次在 asof 时点尚未披露（前视）")

    print(f"自洽窗口: [{SELF_CONSISTENT_WINDOW[0]}, {SELF_CONSISTENT_WINDOW[1]}]   README 示例 asof: {README_ASOF}")
    print(f"  600519 末值/250日最高 = {ratio:.4f}（需 >= 0.98）")
    print(f"  000001 250日回撤     = {drawdown:.2%}（需 >= 35%）")
    print(f"  宽度: 涨 {breadth['advancers']} / 跌 {breadth['decliners']} / 涨停 {breadth['limit_up_count']}；指数 MA50={breadth['above_ma50']} MA200={breadth['above_ma200']}")
    print(f"  top_holders 期次 = {meta.get('report_date')}（公告 {meta.get('announce_date')}, is_historical={meta.get('is_historical')}）")
    if problems:
        print("\n❌ 自检未通过:")
        for item in problems:
            print("   -", item)
        return 1
    print("\n✅ 剧本自洽性自检通过")
    return 0


ANNOUNCE_REAL = {"20250331": "20250430", "20250630": "20250813", "20250930": "20251030",
                 "20251231": "20260417", "20260331": "20260425", "20260630": "20260815"}


def _announce(period: str) -> str:
    return ANNOUNCE_REAL.get(period, period)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 data/scenario 剧本数据（SYNTHETIC）")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check", action="store_true", help="只跑自洽性自检")
    args = parser.parse_args()
    if args.check:
        return check()
    print(f"剧本目录: {HERE}")
    build(args.force)
    print("\n完成。⚠️ 全部为 SYNTHETIC 剧本数据，非真实行情；DataSource 默认不读该目录。")
    print("时间戳:", datetime.now(timezone.utc).isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
