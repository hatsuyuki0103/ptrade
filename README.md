# ptrade —— A 股 CANSLIM 模拟盘

一个**离线可复现**的 A 股模拟交易系统：真实 akshare 数据 + 本地缓存/夹具 + T+1 与融资融券记账 +
CANSLIM 七因子打分 + 单文件 K 线图。接口契约见 [`INTERFACES.md`](INTERFACES.md)（**v3.5**，唯一真源）。

> 仅供研究与模拟教学，**不构成任何投资建议**。

---

## 安装

```sh
pip install ptrade                      # 从 PyPI（若已发布）
pip install git+https://github.com/hatsuyuki0103/ptrade   # 直接从仓库
pip install -e .                        # 源码检出（开发）
```

或者从 [Releases](https://github.com/hatsuyuki0103/ptrade/releases) 下载 wheel / sdist：

```sh
pip install ptrade-1.1.0-py3-none-any.whl
```

依赖：Python ≥ 3.11、`pandas`、`akshare`；测试另需 `pytest`（**无其他第三方依赖**）。

> `ptrade/vendor/echarts.min.js`（ECharts 5.5.1，约 1 MB）随包分发，用于让 `chart` 生成
> **离线单文件 HTML**（零外链、双击即看）。它只是静态资源，不是 Python 依赖。

## 数据根（data root）

所有运行时数据（缓存 / 夹具 / 账户状态 / 台账 / 图表产物）都在一个**数据根目录**下，
按以下顺序解析：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `--root <path>` | 每次命令显式指定，最高优先 |
| 2 | 环境变量 `PTRADE_HOME` | 适合固定一个工作目录 |
| 3 | 源码检出根 | 若 `ptrade/` 上一级存在 `data/`，沿用它（`git clone` 即用） |
| 4 | `~/.ptrade` | **pip 安装后的默认**，安装位置不可写时用这里 |

```sh
export PTRADE_HOME=/path/to/my-trading        # Linux/macOS
$env:PTRADE_HOME = 'D:\my-trading'            # Windows PowerShell
ptrade --root /path/to/my-trading status      # 或每次显式指定
```

目录结构（首次使用可让 `ptrade init` 自动创建）：

```
<root>/
  config.json          可选：覆盖默认阈值（缺失字段用默认值）
  data/cache/          akshare 拉取结果的缓存
  data/fixture/        离线夹具（测试与 --offline 用）
  data/state.json      账户状态
  data/ledger.jsonl    逐笔成交流水
  reports/             K 线图 HTML 产物
```

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

> **`--codes` 多代码在 PowerShell 下必须加引号**：裸写 `--codes 000001,601398` 会被 PowerShell
> 当数组运算符解析，前导零丢失（实收 `1,601398`），表现为该票 `score=0` 且 `name` 等于代码。
> 用 `--codes "000001,601398"` 或 Linux/macOS/cmd 即可。

## 离线夹具与测试

`data/fixture/` **不入 git**（90 个 JSON / 约 6.6 MB，机器生成，每次重生成都是整文件 diff），
按需三选一获取后即可跑全部测试与 `--offline` 命令：

```sh
python tools/fetch-fixtures.py     # 从 GitHub Release 下载（推荐，最快）
python tools/build_fixtures.py     # 用 akshare 现拉重建（需联网，约 15 分钟）
```

`data/scenario/` 是**人为构造**的行情剧本（用于算法标定，与真实夹具物理隔离），
每个文件带 `"_synthetic": true` 标记，由 `tools/build_scenario.py` 确定性生成。

## 命令一览

| 命令 | 说明 |
|---|---|
| `init [--cash N] [--reset]` | 初始化账户（`data/state.json`，`schema_version=3`） |
| `status [--json]` | 账户状态（净值/担保比例/杠杆/持仓） |
| `sync --codes ... [--start] [--end] [--offline] [--unadjust]` | 预取日线到缓存 |
| `scan --codes ... [--asof] [--offline] [--json]` | CANSLIM 七因子打分 |
| `recommend --codes ...` | 交易建议 |
| `plan --codes ...` | 建仓/调仓计划 |
| `order --code --side --qty [--price] [--trade-date]` | 下单（六种方向） |
| `settle --trade-date` | 推进交易日：T+1 结算 → 计息 → 盯市 → 风险 |
| `risk [--json]` | 风险报告（担保比例/杠杆/敞口/告警） |
| `close --code [--ratio]` | 平仓（先买券还券，再卖券还款/卖出） |
| `ledger [--limit N]` | 成交流水（`data/ledger.jsonl`） |
| `chart --code [--period daily\|weekly\|monthly] [--out]` | 离线单文件 K 线图 HTML |
| `search <代码或名称> [--limit N] [--offline]` | 搜索股票（代码前缀 / 中文名子串） |
| `watchlist --action add\|remove\|list [code] [--name] [--note]` | 自选池增删查（幂等） |
| `serve [--host] [--port] [--offline]` | **启动本地网页端**（默认 `127.0.0.1:8765`） |

全局 `--root <path>` 覆盖数据根目录；`--json` 输出 `{"ok","command","data","error"}`，**退出码 0/1**
（业务拒单不算错误：`ok=true` 且 `data.orders[].status == "rejected"`）。

## 网页端

```powershell
python -m ptrade serve            # 浏览器打开 http://127.0.0.1:8765
```

在浏览器里可以看账户 / 持仓 / 风险 / 七因子打分数表 / K 线图，并能**下单、平仓、结算**。
前端是**单个自包含页面**（内联 CSS/JS，**零外链**）；K 线图由 `/api/chart` 返回完整 HTML 内嵌显示。

> ⚠️ **网页端没有鉴权**。默认只绑 `127.0.0.1`，这是**唯一的边界**。
> `--host 0.0.0.0` 会让同网段任何人都能对你的模拟盘下单 —— 启动横幅会就此警告。

**写者互斥**：网页端与 CLI 会写**同一份** `data/state.json` 与 `data/ledger.jsonl`，
因此两者共用一把跨进程写锁（`data/.ptrade.lock`）：任一时刻只有一个写者在「读 → 改 → 写」临界区内。
并发写会排队；锁超时（默认 10s）时网页端返回 **HTTP 503**（`error_code=STATE_LOCK_TIMEOUT`），
CLI 返回退出码 1 —— **都不会静默写坏状态**。

**账实一致性**：`status` 与 `/api/state` 都会给出 `ledger_consistency`
（`{"consistent","ledger_lines","recorded","diverged"}`）。写一单的时序是「先落 ledger，再原子写 state」，
若 state 落盘失败就会出现「ledger 有单、state 无单」的分叉 —— 这个字段让它**可被检出**
（`diverged=true`），而不是悄悄留下不一致。`diverged=null` 表示**无法判定**（例如有写事务正在进行），
此时 `consistent=false`：**不把「不知道」当成「没问题」**。

## 搜索与自选池

```powershell
python -m ptrade search "茅台" --offline              # 按中文名
python -m ptrade search "600519" --offline            # 按代码
python -m ptrade watchlist --action add 600519 --name 贵州茅台
python -m ptrade watchlist --action list
```

`search` 的全市场名称表缓存在 `data/cache/stock_list.json`（TTL 7 天）。
**`--offline` 只读缓存**：没有缓存会**明确报错并给出获取方式**，而不是静默返回空；
**无命中**则返回 `ok=true` 且 `count=0` —— 两者被严格区分。
自选池 `add` 是**幂等**的（重复添加不产生重复项、不报错）。


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
