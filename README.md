# ptrade —— A 股 CANSLIM 模拟盘

一个**离线可复现**的 A 股模拟交易系统：真实 akshare 数据 + 本地缓存/夹具 + T+1 与融资融券记账 +
CANSLIM 七因子打分 + 单文件 K 线图。接口契约见 [`INTERFACES.md`](INTERFACES.md)（**v3.2**，唯一真源）。

> 仅供研究与模拟教学，**不构成任何投资建议**。

---

## 快速开始

```powershell
$env:PYTHONUTF8='1'                     # 中文 Windows 必需
python -m ptrade init --cash 500000     # 初始化模拟账户
python -m ptrade status                 # 账户状态
python -m ptrade sync --codes 600519,002594 --start 2025-04-01 --end 2025-10-09
python -m ptrade scan --codes 600519,002594,000001 --offline --json
python -m ptrade chart --code 588000 --offline      # 生成 K 线图 HTML
python -m pytest tests -q
```

依赖：Python ≥ 3.11、`pandas`、`akshare`、`pytest`（**无其他第三方依赖**）。

## 命令一览

| 命令 | 说明 |
|---|---|
| `init [--cash N] [--reset]` | 初始化账户（`data/state.json`，`schema_version=3`） |
| `status [--json]` | 账户状态（净值/担保比例/杠杆/持仓） |
| `sync --codes ... [--start] [--end] [--offline] [--unadjust]` | 预取日线到缓存 |
| `scan --codes ... [--asof] [--offline] [--json]` | CANSLIM 七因子打分 |
| `recommend --codes ...` | 交易建议 |
| `plan --codes ...` | 建仓/调仓计划 |
| `order --code --side --qty [--price PRICE\|live] [--trade-date]` | 下单（六种方向） |
| `settle --trade-date` | 推进交易日：T+1 结算 → 计息 → 盯市 → 风险 |
| `risk [--json]` | 风险报告（担保比例/杠杆/敞口/告警） |
| `close --code [--ratio]` | 平仓（先买券还券，再卖券还款/卖出） |
| `ledger [--limit N]` | 成交流水（`data/ledger.jsonl`） |
| `chart --code [--period daily\|weekly\|monthly] [--out]` | 离线单文件 K 线图 HTML |

全局 `--root <path>` 覆盖数据根目录；`--json` 输出 `{"ok","command","data","error"}`，**退出码 0/1**
（**例外**：`status` 检出**账实分叉**时退出码为 **1** 而 JSON 里 `ok` 仍为 `true` ——
`ok` 描述命令本身，退出码描述「是否需要人工处理」；详见 `INTERFACES.md` INV-47）
（业务拒单不算错误：`ok=true` 且 `data.orders[].status == "rejected"`）。

## 成交价：三种来源，以及「按实时价成交」

下单时的成交价有三种来源，**订单上会记录是哪种**：

| `--price` | 成交价来源 | `price_source` |
|---|---|---|
| `live` | **当前快照价**（`data/cache/spot.json`，在线时是 akshare 实时全市场快照，TTL 60s） | `live` |
| 具体数字 | 你给的价 | `request` |
| 不传 | 行情 `last` ± 滑点（市价） | `""` |

```powershell
python -m ptrade order --code 600519 --side buy --qty 100 --price live
```

**不必再手写 `--trade-date`**：live 模式下交易日**取自快照自身的 `asof`**
（`service.live_trade_date`）。这一点很关键 —— `resolve_asof` 在 15:30 前会回退到
**上一交易日**，而引擎的停牌检查看的是那一天有没有行情，两者不一致会让 live 下单被判
`SUSPENDED`（实测：不传 `--trade-date` 时 `trade_date` 落到昨天 → 拒绝）。

**为什么需要 `live`**：`--trade-date` 缺省走 `resolve_asof`，它在 **15:30 前会回退到
上一交易日**（未来函数防护），于是盘中缺省下单会走到「日线推导」分支并**被判停牌**。
`--price live` 是**显式**入口，不动回测语义。

⚠️ **快照陈旧时 `--price live` 会明确拒绝**，**不会**退回陈旧价 ——
「以为是实时价、其实是几天前的价」比明确失败危险得多。错误信息会告诉你
`asof` / `fetched_at` 与实际今天，并给出「在线重取快照」或「改用 `--price <价格>`」两条出路。

### 成交价审计（每笔都能回答「用的是哪一刻的价」）

`ledger.jsonl` 与 `status --json` 的每条订单都带：

| 字段 | 含义 |
|---|---|
| `price_source` | `"live"` / `"request"` / `""`（市价） |
| `price_asof` | 该价对应的**行情日期**（快照的 `asof`） |
| `price_fetched_at` | 该快照的**抓取时刻**（真正回答「哪一刻的价」） |

**与真实盘的已知差距**（诚实列出）：没有盘口/撮合（一律立即全额成交）、没有滑点、
价格分辨率是 60 秒（`TTL_SPOT`）而非逐笔、涨跌停只有价格边界检查没有排队。


## 账户数学（v3.2 口径）

```
total_assets      = available_cash + frozen_t1 + frozen_collateral + long_market_value
collateral_value  = available_cash + frozen_collateral + long_market_value   # 担保物（不含未交收款）
total_debt        = financing_debt + short_market_value + interest_accrued
net_assets        = total_assets - total_debt
maintenance_ratio = collateral_value / total_debt          # 交易所口径；分母<=0 → inf
leverage          = total_debt / net_assets
```

仓位与利润表（`N` = 成交金额，`F` = 费用合计）：

| 操作 | available_cash | frozen_t1 | frozen_collateral | financing_debt |
|---|---|---|---|---|
| 普通买入 | `-(N+F)` | 0 | 0 | 0 |
| 普通卖出 | 0（T+1 到账） | `+(N-F)` | 0 | 0 |
| 融资买入 | `-F`（自有资金留作担保物） | 0 | 0 | `+N`（全额） |
| 融券卖出 | `-F` | 0 | `+N` | 0 |
| 买券还券 | `-(N+F)+券款` | 0 | `-券款` | 0 |
| 卖券还款 | `+(N-F)-repay` | 0 | 0 | `-repay`（全额还款） |

**四条不变量**（`tests/test_core.py` 逐条断言）：I1 零费用下开仓 `net_assets == initial_cash`；
I2 `net - initial == 多头浮盈 + 空头浮盈 - 累计费用 - 累计利息`；I3 `maintenance_ratio` 与交易所口径一致；
I4 开仓后 `maintenance_ratio >= 1.30`（由引擎 `max_leverage` + 保证金额度共同保证）。

**维护保证金关键数**：`max_leverage=2.0`、`margin_ratio_long=0.80`、`margin_ratio_short=1.00`、
平仓线 130%、警戒线 150%。融资满额 `N=625000` 时开仓 `ratio=1.80`，标的跌 50% 精确触及 1.30。

## 数据层

* 缓存 `<root>/data/cache/<key>.json`（`{"fetched_at","payload"}`，TTL 见契约 §4）；
  离线夹具 `<root>/data/fixture/*.json` 使用**同一信封格式**。
* `offline=True` **绝不联网**：akshare 惰性导入，离线路径调用 `_ak()` 直接抛错；测试用
  `conftest.py` 的 autouse 夹具直接封掉 `socket.connect` 作为双重证明。
* 在线取数：东财优先（`stock_zh_a_hist` / `stock_zh_a_spot_em`）→ 新浪兜底
  （`stock_zh_a_daily` / `stock_zh_a_spot` / `fund_etf_hist_sina`）；指数走
  `stock_zh_index_daily_em` → `stock_zh_index_daily`。
* 节流 0.3s/次，失败重试 2 次（退避 1s/3s）。

### 单位约定（强制，违反会导致打分静默失效）

| 字段 | 单位 |
|---|---|
| 价格类（`last`/`open`/`high`/`low`/`close`/`prev_close`/`limit_up`/`limit_down`） | 元 |
| `volume` | **股**（东财源为「手」，数据层 ×100；新浪源本身为股/份） |
| `amount` | 元 |
| `total_mv` / `float_mv` | 元（不转亿元） |
| `turnover` / `pct_chg` | 百分数（`3.5` = 3.5%） |
| `revenue_yoy` / `net_profit_yoy` / `*_qoq` | 百分数 |
| `roe` / `gross_margin` / `debt_ratio` | 百分数 |
| `eps` / `bps` / `ocf_ps` | 元/股 |
| `holders_chg_pct` / `ratio`（top_holders） | 百分数 |
| `revenue` / `net_profit` / `shares` | 元 / 元 / 股 |

### 重建夹具

```powershell
$env:PYTHONUTF8='1'
python data/fixture/build_fixtures.py            # 增量
python data/fixture/build_fixtures.py --force    # 全量（约 5 分钟）
```

生成器是**唯一**的夹具来源，末尾自带单位自检（价格/成交量/成交额/ROE/比例量纲 + 剧情标的检查），
不会手工写 JSON。夹具沿真实行情生成，**不做任何数据美化**。

## 目录

```
ptrade/  config.py  core.py(领域模型+账户数学,纯函数)  datasource.py(akshare+缓存+夹具)
         engine.py(T+1/两融/风控/下单)  strategy.py(CANSLIM 七因子)  cli.py  chart.py
tests/   conftest.py(离线夹具+全局限网)  test_core.py  test_datasource.py
         test_engine.py  test_strategy.py  test_cli.py
data/    cache/(运行时,不入库)  fixture/(离线夹具,入库)
```

## 已知限制

* 东方财富行情端点在本机长期不可用（`RemoteDisconnected`），因此 `spot()` 回退新浪源：
  `pe_ttm`/`pb`/`total_mv` 为 `null`，`float_mv`/`turnover` 仅对**显式请求的代码**用新浪日线补齐。
* 沪市「可融券」判定用 `融券余量 > 0`（保守口径，避免假阳性券源）；因此 `can_short` 会随当日券源变化。
* `REPAY_MARGIN` 只支持全额还款；部分还款未实现。
* 离线夹具的基本面类数据集只保留**比对池标的**（控制仓库体积），并非全市场。
