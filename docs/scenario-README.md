# `data/scenario/` —— ⚠️ SYNTHETIC 剧本数据（非真实行情）

本目录由 [`build_scenario.py`](build_scenario.py) 生成，**每一份 JSON 顶层都带 `"_synthetic": true`**，
内容**全部是人为构造的数字**，只用于单元测试标定 CANSLIM 打分路径（强票 / 弱票）。

## 为什么需要它

`data/fixture/` 是**纯真实行情**（由 `data/fixture/build_fixtures.py` 从 akshare 拉取，未经任何美化）。
实测真实数据下，`600519` 在 2025 年净利同比仅 +8.89%、近 6 月相对强度 −22.9%（RS 落后），
**它是"优质但落后"的票，不是 CANSLIM 意义上的强势票**：C=40、L=20，即便 S/I/M 全给满分，
加权总分上限也只有 ≈65.5 < 75，`signal` 永远到不了 `buy`。
若为了测试而篡改 `data/fixture/`，用户 `--offline` 跑出来的就是**伪造的 buy 信号**去下单——因此
真实夹具一个字都不改，改为在本目录放**显式标注的构造数据**。

## 隔离保证

* `DataSource(cfg, offline=..., scenario=False)` 是默认值 → **不读本目录**；
  只有显式 `scenario=True`（单元测试注入）时才把它叠在 `data/fixture` **之前**。
* CLI 没有 `--scenario` 开关 → `python -m ptrade ... --offline` 永远读真实夹具。
* 本目录缺失的文件自动回落到 `data/fixture/`（同一信封格式）。

## 剧本内容

| 文件 | 剧本设定 | 标定的因子 |
|---|---|---|
| `daily_600519.json` | 单调上行（760 → 1000），末值即 250 日新高（比值 0.996），最后 20 日放量 1.8× | N = 100（创新高 + 放量） |
| `daily_000001.json` | 单边下行（20 → 8），250 日回撤 ≈ 42.7% | N = 25 |
| `index_sh000001.json` | 温和上行（3000 → 3400），站上 MA50/MA200 | M 的指数分项满分 |
| `spot.json` | 120 行，涨跌家数 95:25（比值 3.8）、涨停 22 家；600519 `float_mv`=120 亿、换手 5%；`asof=2025-10-09` | M 的宽度分项 +20/+8（仅 `asof=2025-10-09` 走快照口径） |
| `quarterly_2025{0331,0630,0930}.json` / `quarterly_2026{0331,0630}.json` / `quarterly_20251231.json` | 600519 同比**逐期加速**：35 → 68 → 72 → 75 → 78 → 80；000001 恒为 −22% | C：最新已披露期 ≥50 → 90，且每期都比上期高 → ×1.25 → **100**；**覆盖全部 6 期**，任意 asof 都不回落真实数据 |
| `top_holders_600519.json` | 10 行全为机构型股东，`ratio` 合计 35% 且环比增加；`report_date=2025-06-30`、`announce_date=2025-08-13` | I：10%-60% → 85，+10 → 95；期次在 asof 时点已披露（无前视） |

**自洽窗口**：`asof ∈ [2025-08-13, 2026-10-09]`（下界 = 2025H1 的真实披露日 2025-08-13；
上界 = 现有日线末端）。**README 示例固定用 `asof="2025-10-09"`**（实测 `total = 88.04 / buy`）。

> ⚠️ 该窗口内 `S` 因子实测为 **45**（= 流通市值 ≥1000 亿档给 45，换手不在 3%-15% 区间不加分）。
> 若消费方在**历史 asof** 下按前视纪律禁用 `spot()` 快照，则 `S` 只能走日线口径得到 45；
> `asof=2026-10-09`（快照当日）才会用到 `spot.json` 的 `float_mv=120 亿 / 换手 5%`。
> 两者的差别不影响 `total ≥ 75`。

**期望**：`600519` → `total ≥ 75` 且 `signal == "buy"`；`000001` → `total < 50`（`avoid`/`short`）。

## 使用方式

```python
from ptrade.config import load_config
from ptrade.datasource import DataSource
from ptrade.strategy import CanslimScorer

cfg = load_config("D:/Ai投资")
ds = DataSource(cfg, offline=True, scenario=True)     # ← 只有显式指定才读剧本
scorer = CanslimScorer(cfg, ds, offline=True)
result = scorer.score("600519", "2025-10-09")     # ← 自洽窗口内的示例 asof
assert result.total >= 75 and result.signal == "buy"
```

重建与自检：:

```powershell
$env:PYTHONUTF8='1'
python data/scenario/build_scenario.py --force    # 重新生成
python data/scenario/build_scenario.py --check    # 自洽性自检（README asof 必须落在窗口内）
```

`--check` 会断言：README 的示例 asof 在自洽窗口内、该 asof 的最新**已披露**报告期在剧本里有覆盖、
强票形态（末值/250 日最高 ≥ 0.98）、弱票形态（250 日回撤 ≥ 35%）、指数站上 MA50/MA200、
`top_holders` 期次在 asof 前已披露。**不靠人眼**。
