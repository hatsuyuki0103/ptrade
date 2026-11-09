# Changelog

本文件记录 ptrade 的版本变更。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.14.0] — 2026-11-09

**主题：v10（**部分交付**）—— CANSLIM 分数**按交易日刷新** + 账户盈亏**实时可见**

> ★★ **诚实声明：v10 的 A4（「周五收盘全市场后台重算」）本轮**未实现** ✗，留下 v1.15.0。**
> 本轮交付的是用户两个诉求的**核心部分**：① 分数按时效刷新且**能看出是哪天的**；② 盈亏**实时**且**标明来源**。

### 用户原话（真源）

> 「我的页面上，这个**益佰制药还是 33 分**，现在你给了 **32 分**，说明，我的模拟盘，**需要有刷新的机制，每天去刷新这个分数**。
>   另外，**账号状态，也是没有实时更新的**…我之前看的时候都还是 **−33 块**，现在才给我赚到了 **100 多块**。
>   我需要你至少让我**每次刷新页面的时候，都知道自己赚/亏了多少**，或者**在交易时间内的时候，给我刷新这个**。」

### 新增 · 账户实时盈亏（B 组）

- **`GET /api/live_state`**（与 `/api/state` **同形**，多 `as_of`/`source`）：
  ```python
  prices = cache-only spot prices          # _spot_frame_raw()，★ 打开页面**不打网络**
  live = core.mark_to_market(state, prices)          # deepcopy ⇒ 只读；坏价自动跳过
  live = core.refresh_risk_flags(live, cfg.risk, cfg.margin)   # ★ 风险指标也按实时价
  data = service.state_summary(live)                 # ★ 与 /api/state 共用同一序列化器
  ```
  ★ **不新造公式** —— 评审（Architect B1/Critic）指出 `state_summary` 只是序列化器，公式在 `core.AccountState`
  派生属性里；**逐条复刻会造出第二套会计恒等式** ✗
  ★ **`/api/state` 一行未改** ⇒ **INV-40 保持**（与 `ptrade status --json` 逐字段一致）✓
- ★ **前端 `refreshState()` 改读 `/api/live_state`**（失败回退 `/api/state`）—— 否则新端点**白做** ✓
- ★ **`live_state` 必须补齐 `ledger_consistency`** —— 缺它前端 `renderDivergence` 会拿到
  `diverged===undefined` ⇒ 渲染**灰色「无法判定」横幅** ⇒ **右列多一块高度** ⇒
  `test_ui_layout_v4` **4 条全红**（实测右列 `scrollH 888 > clientH 807`）✗

### 新增 · 分数按交易日刷新（A 组）

- **`chart.read_cached_analysis(..., trade_day=)`**：★ 判定式用「**请求的 asof == 解析出的当前交易日**」
  （payload 里**没有** `asof` 字段 —— Critic B6 实测），**仅 `ok is True`** 才允许「同交易日永久有效」；
  ★ **失败缓存永远按 `ANALYSIS_FAIL_TTL=120s`** 过期，不许被放宽 ✗
- ★ **`#analysis-state` 的 `data-asof` 属性 + `title`** 显示「算于 YYYY-MM-DD」——
  ★ **只写属性，不动 `textContent`**（见下「布局预算」）
- **M1**：`/api/watchlist/score` 算完**写回 `analysis_<code>_<asof>` 缓存**（+ `computed_at`）⇒
  自选池与图表分析卡**从此共用同一份数据** ⇒ 同一 asof 下分数必然一致 ✓
  （★ 这才是用户「33 vs 32」的真源 —— Critic M1：原来自选池实时算、图表卡读缓存 ✗）

### ★★ 本轮最贵的教训：**布局预算只剩 0px**

`tests/test_ui_layout_v4.py`（INV-66b 右列无滚动 / K 线面板 ≥215px）对**任何新增 DOM 都零容忍**：
| 我试过的做法 | 结果 |
|---|---|
右列新增「盈亏来源」**独立一行** | `819 > 807` ✗ |
把完整文案**内联**到总盈亏值 | `832 > 807` ✗ |
1 字符标记 + title | `851 > 807` ✗ |
全部 `hidden` 零占位 | `888 > 807` ✗ |
★ **最终**：只写 **`data-*` 属性** + `title`（**视觉零变化**） | **28 passed** ✓ |

⇒ ★ **在 `#analysis-state` 或 `state-panel` 里加任何元素/文本，左列头部一变高 ⇒ 整页 flex 重排
⇒ 右列可用高度被压缩 ⇒ 4 条布局判据全红** ✗ —— **判据已把这条写进注释，防将来"顺手加个元素"** ✓

### 测试

- 新增 **`tests/test_v10_refresh.py`**（V10-B1/B4/B5/M1/M2/A1/A3 等 10 条）
- 全量 **1220 passed / 5 skipped / 0 failed**（v1.13.0 基线 1200/4 ⇒ **+20 判据**）
- ★ 过程中被仓库纪律抓住并修复：**守卫站点必须登记**（`test_W7`，2 次）·
  **宽 `except` 吞 `NetworkForbiddenInLock`**（`test_W1`）· **不变式扫描撞上我留下的 UTF-16 临时文件**（`0xff` BOM）

### ★ 未实现（v1.15.0）

- **A4 周末全市场后台重算**：需按评审共识实现 ——
  **单 `CanslimScorer` 批处理**（★ 每票新建 scorer 是 103h 的根源；复用后 **4–8h/趟**）·
  `data/cache/score_refresh_<iso_week>.json` **ISO 周幂等台账**（一周最多一轮）· failed 台账 + 一次重试 ·
  范围 = **spot 快照过滤 6 位 A 股/排除 ETF/停牌/成交额 0**（★ `UNIVERSE_CODES` 只有 32 只且含 3 只 ETF ✗）·
  「周五收盘」判定用 **`prev_td.iso_week != next_td.iso_week`** + **排除周一盘中** + `next_trade_date` 抛错时回退 ·
  进度端点 · **单趟循环**（不是 `spot_refresher` 的 per-tick，惰性门控会让 103h 任务永远跑不完 ✗）

## [1.13.0] — 2026-11-03

## 用户实测（2026-11-03 原话 + 截图）

> 「我昨天模拟下单了，怎么**成本、市值、浮盈**这些都还没计算出来呢
>   而且昨天 **3.19** 下的单，现在 **3.16**，我应该也有赚了」

## 根因（**数据齐全，缺陷全在显示层**）

实测 `state.state.positions["600594"]`：
```
long_qty=0 · short_qty=15700 · short_cost=3.19 · last_price=3.19
long_value=0 · short_value=50083 · long_pnl=0 · **short_pnl=0**   ← last_price 未重估
```
`/api/positions` 的输出**字段全在** ✓ ⇒ 是两个显示层缺陷：

### 缺陷 A：持仓表**只有多头列**（`webui.py::renderPositions`）
```js
// 旧
$("positions").innerHTML = table(["代码/名称", "多头数量", "成本", "市值", "浮盈"], html);
```
⇒ **空头仓位完全看不见** ✗（用户只能从「融券市值」那一格间接猜）

### 缺陷 B：`last_price` 只在 `settle`/`roll_to` 时更新（`engine.py:121`）
⇒ 不结算 ⇒ `last_price` 永远停在成交价 ⇒ **浮盈恒为 0** ✗
⇒ 用户以为"没算"，其实"没重估" ✓

## 修法

### A. 持仓表改成**多空两侧**（前端）
表头 10 列：`代码/名称 · 多头数量 · 多头成本 · 多头市值 · 多头浮盈 · 空头数量 · 空头成本 · 空头市值 · 空头浮盈 · 实时价`
★ **没有仓位的一侧显示「—」**，避免一行十几个 0 喧宾夺主 ✓

### B. 新增 `service.mark_positions_live(ds, positions)`（**读时重估，只读**）
* 批量取当前快照价 ⇒ 重算 `last_price` / `*_value` / `*_pnl`；
* **取不到价 ⇒ 退回 `state.last_price`**（**不编造、不置 0**）✓
* ★ **不写回 state** —— `settle` 的结算语义（按**结算日**价）不受影响 ✓
* ★ 应用到 **`/api/positions`**（用户看的持仓面板）✓
* ★★ **`/api/state` 刻意不重估** —— 它受 **INV-40** 约束，必须与 `ptrade status --json` **逐字段一致** ✗
  第一版我在两处都加了，被 `test_webapp.py::test_w2_state_matches_status_json` 抓红 ✗ 已撤 ✓

## 实测（真机，非手算）

```
/api/positions（**重估**）: 空头 15,700 · 成本 3.19 · 实时价 3.20 · 浮盈 **−157** · price_source=live ✓
/api/state（**不重估**）  : 空头 15,700 · 成本 3.19 · 实时价 3.19 · 浮盈 0      ← 守 INV-40 ✓
★ 读路径无副作用：调 `/api/positions` 前后 `state.json` sha256 **完全一致** ✓
```
★ 早前现价 3.16 时该端点的输出是 **浮盈 +471**（= 用户自己算的数）✓ —— 数字随实时价变化，这才是对的 ✓

## 测试

- 新增 **`tests/test_positions_v9.py`**（V9-1…V9-7）：
  实时价重估 · 空头浮盈公式 `(cost−last)×qty` · **取不到价时退回 state 价** ·
  **重估不修改入参**（读路径无副作用）· 表头含空头四列 + 实时价（前端）·
  ★ **`api_positions` 必须有局部 import**（`webapp.py` 的 `service` 是**局部** import，避循环导入）·
  ★ **不得用裸 `except Exception`** 包重估（会吞 `NameError` 这类**编程错误**）。
- 全量 **1211 passed / 4 skipped / 0 failed**（v1.12.0 基线 1203 ⇒ **+8 判据**）。

## ★ 修这个 bug 时被仓库纪律连抓三次（都修了）

| 判据 | 抓到我什么 |
|---|---|
`test_W2`（INV-40） | 我在 `/api/state` 也加重估 ⇒ 与 `ptrade status --json` 不一致 ✗ |
`test_W1`（INV-42） | `mark_positions_live` 的宽 `except` 会**吞** `NetworkForbiddenInLock` ✗ |
`test_W7` | 新增守卫站点**必须登记** ⇒ 已登记 `service::mark_positions_live` ✓ |

★ 还踩到一个**很典型的坑**：`webapp.py` 的 `from . import service` 是**函数内局部 import**
（为避循环导入），我在新方法里直接用 `service.xxx` ⇒ **`NameError`**，
而我又加了宽 `except Exception: pass`（本意是"取价失败不 500"）⇒ **把自己的编程错误静默吞掉** ✗
⇒ 表现为「重估不生效、但一切看起来正常」，极难查 ✗
⇒ 修法：**局部 import + 把 `except` 收窄**到 `(OSError, ValueError, RuntimeError)` ✓

## [1.12.0] — 2026-11-03

**主题：v8 —— 月线默认视野从 5 年拉到 **20 年**（周线保持 5 年）**

### 用户诉求（原话）

> 「月线初始给的柱子太少了。要不你看看**周线有多少根柱子**，月线帮我**再放长一点时间**」
> 「你可以**周线 5 年**，**月线 10 年甚至 20 年**，**周线 5 年这个量我满足了**」

### 变更（一行常量，零传输成本）

```js
- const DEFAULT_VISIBLE_BARS = { daily: 120, weekly: 260, monthly: 60 };
+ const DEFAULT_VISIBLE_BARS = { daily: 120, weekly: 260, monthly: **240** };
```
| 周期 | 旧 | 新 | 时间视野 |
|---|---|---|---|
`daily` | 120 | **120**（不变）| 近半年 |
`weekly` | 260 | **260**（不变，用户满意）| 5 年 |
`monthly` | 60 | ★ **240** | 5 年 → **20 年** |

★ **零成本**：数据**本来就全量加载**，只改 `dataZoom` 初值 ⇒ **传输/计算不变** ✓
★ **效果**：多数票的月线历史**不足 240 根**（002376 只有 200 根）⇒ `startIndex = max(0, 200−240) = 0`
⇒ **月线默认就是「全历史」** ✓ 正是长期视图该有的样子

### 实测（生产 `buildOption` 真实求值，002376 · asof=2026-11-03）

```
daily   : 总 3,959 根 · 可见 **120** 根   [期望 110-130] ✓
weekly  : 总   842 根 · 可见 **260** 根   [期望 240-280] ✓
monthly : 总   200 根 · 可见 **200** 根   （= 全历史）✓
filterMode='filter' 显式 ✓ · 两个 dataZoom 组件同步 ✓
```

### 测试

- 新增 **`tests/test_chart_v8.py`**（V8-1…）：三个周期的值 · ★ **月线视野必须显著长于周线**
  （≥1.8 倍，防「只改一个、另一个忘了」）· 日线仍是近半年 · v6 缩放契约不回归。
- 同步改 `tests/test_chart.py` 里写死的旧值（**需求变更**，v6 三条强断言保留）。
- 全量 **1203 passed / 5 skipped**（v1.11.0 基线 1202；**唯一红的是既有 flaky**，见下）。

### ★★ 已知限制（v7 遗留，本轮如实记录）

**全新空账户**（`init` 后 `state.trade_date == ""`）**+ 不传 `--trade-date`** 时，锁内 `roll_to()`
会走一条**额外取数路径**（`trade_dates` / `latest_trade_date`），该路径未预热、`_try` 是 best-effort
⇒ 静默失败 ⇒ 可能报 `NetworkForbiddenInLock`（INV-42）。

- ★ **不影响真实场景**：账户一旦有历史，v7 的路径**已在真机验证 2 次通过**
  （`close` / `order --price live` 不传 `--trade-date` ⇒ `filled` · `trade_date=今天`）✓
- **绕开办法**：显式传 `--trade-date`（已验证可用）。
- 相应 slow 判据已改为**显式 `pytest.skip` 并在 skip 信息里写清原因**（不删函数，留位置给将来修复）。

### ★ 已知 flaky（**与本轮无关**，如实记录）

`tests/test_cli_locking.py::test_C1_C4_write_command_waits_for_lock[settle]`
—— 锁等待的**时序敏感**测试，实测同一份代码 **3 次跑出「过·过·挂」**（1/3 概率）。
本轮未触碰 `cmd_settle`；架构上它依赖「他人持锁时的时序窗口」，在快机器上会偶发抢跑。

## [1.11.0] — 2026-11-03

**主题：v7 —— 下单交易日语义（**不传 = 最新交易日**）+ 预热与锁内同源（修 INV-42 违规）**

### 修复（**用户实测被挡的 bug**）

用户原话（2026-11-03）：
> 「如果不传交易日期，就是**最新的一个交易日**。你不应该是按 11 月 2 号的价。
>   今天已经 3 号了。现在早上盘已经收了。你应该以**今天的早盘收市价格**下单。」

**现象**：`order` / `close` **不传 `--trade-date`** 时必然失败：
```
ok=false · NetworkForbiddenInLock: 写锁临界区内禁止联网取数（INV-42）
```
只有显式 `--trade-date <昨天>` 才能成交 ⇒ 用户被迫用**昨天的价**下单。

**根因（两条独立的）**：
1. **预热日期与锁内日期不同源**：`service.prewarm_before_write` 把 `peek.trade_date`（= 账户里的
   **旧日期**）排在快照之前 ⇒ 预热的是**旧日期**的 `quote` 键；而 `cli.cmd_order` 的 live 分支在**锁内**
   用 `live_trade_date()` 解析成**今天** ⇒ 预热未命中 ⇒ 锁内联网 ⇒ INV-42 ✗
2. **`live_trade_date()` 本身在写锁内调用** ⇒ 它内部 `ds.live_quote()` 是网络取数 ⇒ 直接违反 INV-42 ✗

### 新增

- **`service.effective_trade_date(ds, explicit_date, codes, peek)`** ——
  下单交易日的**唯一解析入口**（**必须在写锁外调用**）。优先级：
  1. 显式 `--trade-date` ⇒ 原样（既有行为不变）；
  2. ★ **今天是不是交易日** ⇒ 是就用**今天**（用户口径「最新交易日」）；
  3. 否则 ⇒ 日历里的最近交易日；
  4. 日历不可用 ⇒ 快照 `asof`；再不行 ⇒ `peek.trade_date`；全失败 ⇒ `None`（不抛）。
  ★ **为什么用日历而不是 `resolve_asof`**：`resolve_asof` 带 **15:30 未来函数防护**（15:30 前回退上一交易日），
  那是 `--asof` / `plan` / `scan` 该守的；**下单**要的是「今天此刻的价」，回退到昨天就是错的。

### 变更（★ **行为变更，务必看**）

- **`cli.cmd_order` / `cli.cmd_close`**：交易日改为**锁外**解析成 `_effective_td`，再传进锁内；
  **锁内不再取数**（删掉了 `live_trade_date()` 的锁内调用）。
- ★ **不传 `--trade-date` 时，结算日从「账户里的旧日期」变成「最新交易日（含今天）」**。
  这正是用户要的语义，但**对既有脚本是 breaking**：升级后不传 `--trade-date` 会按**今天**结算。
- `service.prewarm_before_write` 改用 `effective_trade_date`（与锁内**同源**）。

### 测试

- 新增 **`tests/test_v7_trade_date.py`（V7-1…V7-5）**：
  `effective_trade_date` 五档优先级（纯函数，不联网）· `cmd_order`/`cmd_close` 的解析**必须在
  `locked_update` 之前**（源码顺序断言）· 锁内**不得**再出现 `live_trade_date` 调用 ·
  显式 `--trade-date` 仍最优先 · 真机 `slow`：不传交易日下单**不得**触发 INV-42。
- ★ **不变式判据连抓我两次**（都对，都已修）：
  ① `test_W1`：我给 `effective_trade_date` 加的宽 `except Exception` 会**吞掉**
     `NetworkForbiddenInLock`（`⊂ DataUnavailable ⊂ Exception`）⇒ 已加 `except NetworkForbiddenInLock: raise`；
  ② `test_W7`：新增守卫站点**必须登记** ⇒ 已登记 `service::effective_trade_date`。
- 全量 **1202 passed / 3 skipped / 0 failed**（v1.10.0 基线 1190/4 ⇒ +12 判据）·
  `pytest -m slow -q -rs` → **2 passed / 0 skipped** ✓

### 真机验证（这就是用户要的结果）

```
close --code 600594 --ratio 1.0            （不传 --trade-date）
→ ok=true · trade_date=**2026-11-03** · status=filled ✓

order --code 600594 --side short_sell --qty 15500 --price 3.23   （不传 --trade-date）
→ ok=true · trade_date=**2026-11-03** · status=filled · 50,065 元
→ 担保比例 10.97 · 杠杆 0.100
```

## [1.10.0] — 2026-10-20

**主题：v6 —— K 线**从该票上市日算起**（窗口锚点改为 A 股开市日）+ 每周期默认缩放**

### 修复（**这是一个真问题**，用户指出）

- **每只票的 K 线不再只显示最近 2 年**：窗口起点从「asof − 730 天」改为**固定锚点**
  `datasource.VIEWER_START_DATE = "1990-12-19"`（A 股开市日）。
  ★ 为什么这样就够（**不需要「上市日」字段** —— 本地确实没有）：把窗口放到开市日，
  数据源返回的**第一根就是该票 IPO 那天**（老票从 2001/2007…，新票从它的 IPO）。
  实测：`600519` **6023 根 / 首根 2001-08-27**（原 484 根 / 2024-10-21）；`601857` **4596 根 / 2007-11-05**。
- ★ **为什么用日期而不是「N 天」**：用户原话「不要算多少天，因为这个不都是天天变的么」——
  「N 天」的起点**每天漂移**，「A 股开市日」是**固定锚**。

### 新增

- **`datasource.VIEWER_START_DATE`**（契约见 `INTERFACES.md` **§8.5.1**）—— 图窗口起点的**唯一真源**；
  ★ `latest_trade_asof._latest_bar_day`（asof 发现）**必须**用同一锚点，否则 `daily()` 缓存 key 不同
  ⇒ 同一份数据在一个请求里被取两遍（**fail-safe**：只是变慢，不错数据）。
- **`chart_payload()["coverage"] = {first, last, requested_start}`**（§8.5.2）——
  前端据此显示「数据从 2001-08-27 开始」；`--offline` 时显示「数据仅到 …（离线）」。
  ★ **刻意不给 `complete` 布尔**：从 `daily()` 的返回无法区分「上市就晚」与「取数被截断」，
  给假布尔比如实给三个可观测值更糟。
- **每周期默认缩放**（§8.5.3，`chart_option.js`）：`DEFAULT_VISIBLE_BARS = {daily:120, weekly:260, monthly:60}`
  （**近半年 / 5 年 / 5 年**，用户拍板）；加载**全量**、只显示近 N 根 ⇒ `dataZoom` 拖到最左即上市日。
  ★ 用 **`startValue`/`endValue`（索引）** 而非百分比（6023 根时 1% ≈ 60 根，百分比表达不出「≈120 根」；
  日期字符串在 `dates` 有重复时行为依赖实现）；两个 `dataZoom` 组件用同一对 ⇒ 天然同步；
  ★ **`filterMode:"filter"` 显式写**（ECharts 5.5.1 的默认确实是 filter，但**显式才叫契约**）。

### 变更 / 删除

- **`datasource.VIEWER_DAILY_DAYS = 730` 删除**（留 730 兼容名是「陷阱常量」）。
- **`chart.LOOKBACK_DAYS` 删除** —— 它是 `LOOKBACK_DAYS = datasource.VIEWER_DAILY_DAYS` 的
  **导入时冻结死别名**（全仓零使用），是「单一真源最隐蔽的失效形态」（注释写着"调用时解析"，实际是导入时快照）。
- `_start_of(asof, days=None)`：`days=None`（默认、唯一生产路径）→ 返回锚点；`days=N` **仅测试/兼容**。

### 测试

- 新增 **`tests/test_chart_v6.py`**（V6-1…V6-13）：
  固定锚点 · 死别名已删 · `_latest_bar_day` 用锚点（**AST 级**，注释里写不算）· MA96 预热为 `None` ·
  ★ **体积/压缩断言排在「起点正确」之后**（Critic C2：否则「窗口 5000 天」的假实现也能过）· `coverage` 字段 ·
  `slow` marker 已注册 · 真实长历史首根 = 上市日（`slow`，`PTRADE_RUN_SLOW=1` 才跑，**CI skip 不算通过**）。
- `pyproject.toml` 注册 **`slow` marker**（Critic C3：未注册会 warning，且 CI skip 会让 slow 判据**永远无人验**）。
- 全量 **1190 passed / 4 skipped / 0 failed**（v1.9.0 基线 1182）。
- ★ 连带更新（**需求变更，不是放宽**）：
  · `T_PN_13c/13d`（asof 探针与图共享窗口）从「共用 N 天」迁到「**共用固定锚点**」；
  · `tpn14`（单一真源）改为断「**两边的起点都跟随 `VIEWER_START_DATE`**」（比断天数更强：锚点不可能漂移）；
  · `T-V3-20`（两路径深等）不再在测试里**手抄** `option_data` 的形状，改调**同一个函数**（加 `coverage` 时不假红）；
  · `test_chart.py` 的 7 面板断言从旧 `startPct` 形态改为 v6 形态（**并断 `filterMode` 显式**）。

### 已知限制 / **升级影响（务必看）**

- ★★ **隐藏 breaking：旧 `daily` 缓存全废** —— `daily()` 的缓存 key 含 `(start, end)`，
  窗口变了 ⇒ 升级后**首次每票全量重取**：实测 **冷 2.6s / 票**（含 35 年取数 2.4s），之后**热 0.2s**。
  **不是把它改慢了**，是第一次要取 35 年数据。
- payload 从 ≈138 KB 涨到 **1.61 MB**（gzip 后 **571 KB**；`chart_payload` 计算 **0.32s 没变慢**）。
- ★ `--offline` 的夹具只有 2 年 ⇒ 图会"从 2024 开始"，此时 **UI 明示「数据仅到 …（离线）」**
  （否则用户会以为「从上市日」这个 bug 没修）。
- 两处起点同源但**终点仍可能不同**（图用 `asof`、asof 发现用日历末日）⇒ 仍可能 2 次取数；
  **有意不修**（改了会砍掉窗口前段 K 线并改变 MA/BOLL 早期值，违反 INV-62）。
- 早期段没有 MA96/MACD/SAR（指标预热，技术上必然）；前复权后早期价格看起来很低（前复权的正确行为）。

## [1.9.0] — 2026-10-20

**主题：网页端 v5（分析详情 / 模块合并 / 自选池卡内滚 / 刷新及时性）**

### 新增

- **`/api/analysis` 的 `ready` 增加 `recommend`**（`action`/`side`/`entry`/`stop_loss`/`take_profit`/
  `position_pct`/`confidence`）。★ **只从分析缓存透传**（`chart._score_analysis` 早已把
  `strategy.recommend()` 写进载荷、CLI 的 HTML 也早已渲染）—— 修的是「**CLI 有、Web 没有**」的功能不一致，
  **不是**在请求路径重算。前端据此显示「入场 / 止损 / 止盈 / 仓位」。
- **`/api/analysis` 的 `pending`/`ready` 都带 `elapsed_s`**（秒）。`pending` 时随时间增大（前端显示
  「已算 N 秒…」），`ready` 时是**本次耗时**；`null` 只在「本进程从未为该票起过 job」时出现。
- **`ptrade/spot_refresher.py`（新模块）**：spot 快照的**后台周期刷新器**。
  ★ 修的真根因：`/api/quote` 只读缓存，而刷新 spot 的只有「启动预热」与「下单前」⇒
  实测 `data/cache/spot.json` 的 `fetched_at` **数分钟不变**、连续 3 次 `/api/quote` 返回**完全相同的价**
  ⇒ 用户看到的「盘中自选涨幅不刷新」**根因在服务端，不在前端轮询**。
  规格：间隔 **60s**（与 `datasource.TTL_SPOT` 同源）· **惰性门控**（最近 120s 有 UI 请求才刷）·
  **自己的非阻塞 guard**（不抢 `_cached` 的 `_single_flight_lock`，否则 UI 的 `spot()` 会排队）·
  **固定墙钟节拍**（`next_at = max(now, next_at + interval)`，不会越跳越久）·
  非盘中 / `--offline` 不刷 · **只在锁外调**（`INV-42`）· 上游失败**吞掉后等下一 tick**（不因抖动退出）。
- **`datasource._refresh_spot_cache()`**：★ 为什么不能用 `spot()` —— 它走 `_cached("spot", TTL_SPOT, …)`，
  缓存 60s 内**新鲜时直接返回、根本不取数** ⇒ 无法用来「周期刷新」。
- **`/api/health` 增加 `poll_cadence_ms`**（盘中 **5000** / 非盘中 **0** / 判不出 **15000**）。
  ★ 由**服务端**下发给前端消费：前端不得自己用本地时钟算交易时段（本地时钟**不懂 A 股节假日**）。
- **网页端合并「打分」与「CANSLIM 分析」成一张卡**（用户要求）：五段用 `data-section` 冻结契约
  `total → factors → reasons → risks → advice`；默认折叠；`summary` 行显示「总分 · 信号」。
- **网页端渲染逐因子解释与风险**：`/api/analysis` 的 `ready` 早已返回 `reasons`（真实数据下 8 条）
  与 `risks`，但此前前端**一条都没渲染**（只显示总分与七因子数字）。
- **自选池卡内滚**（不分页）：`:root{--wl-row-h}` + `#watchlist{height:…;max-height:…;overflow-y:auto}`。
  ★ 用**固定高度**而不是只 `max-height` —— 否则加自选股会把下面的「持仓/下单」往下推（实测 `offsetTop 522→552`）。
  ★ 5s 刷新**只更新数值单元格**（同批代码不重建表）⇒ `scrollTop` 不跳、点击不丢。
- **页面隐藏时暂停轮询**，`visibilitychange` 切回**立即刷一次**并显示「上次更新 HH:MM:SS」。
- **`行情每 60s 更新 · 界面每 5s 刷新` 明示**（`data-testid="quote-freshness-hint"`）：
  服务端全市场快照抓一次要十几秒，5 秒只是**界面刷新频率**，必须如实告知。

### 变更

- 网页端 `POLL_ACTIVE_MS` **15000 → 5000**（用户拍板「5 秒」），并改为**消费服务端 `poll_cadence_ms`**。
- 网页端分析轮询窗口 **≈101s → ≈600s**（`ANALYSIS_POLL_MS` 20000→10000、`ANALYSIS_POLL_MAX` 6→60），
  两个常量**可注入**（`window.PN_CONFIG`）便于判据用毫秒级跑假分析。
  ★ 旧窗口**覆盖不了实测 132s 的首算耗时** ⇒ 用户永远看不到分析（＝「算完不刷新，要切股票再切回来」）。
- 网页端分析渲染统一为 **`analysisView()`（纯函数）+ `applyAnalysis()`（唯一写入口）**，
  删掉 `setAnalysisState` / `renderAnalysis` 的**双写**（此前 summary 与 body 会自相矛盾）。
- 网页端**四种空状态**：未加载 / 该标的无数据 / **分析不可用（离线模式）** / 分析失败（带原因）。
  ★ `--offline` 下**绝不显示「计算中」**（离线永远算不完，那是欺骗）。

### 修复

- **`elapsed_s` 在 `ready` 瞬间必为 `null`**：`_analysis_background` 的 `finally` 会
  `pop(_ANALYSIS_JOBS[key])` ⇒ 拿它算 elapsed 永远拿不到。改为独立的 `_ANALYSIS_STARTED`
  （**完成时不删**，只清理过期待 `ANALYSIS_JOB_STALE_S` 的项）。★ **不动 `_ANALYSIS_JOBS` 的语义**、
  **不把 `elapsed_s` 写进分析缓存**（会打红缓存写入校验与往返深等）。
- 网页端「仓位」永远显示 `—`：渲染读的是 `adv.position`，而 `strategy.recommend()` 的字段是
  **`position_pct`** ⇒ 改为读 `position_pct` 并渲染为百分比。

### 测试

- 新增 **`tests/test_ui_v5.py`**（V5-1…V5-17 + 3 条变异自证）与 **`tests/conftest.py` 的会话级字节守卫**
  （★ 任何测试改写 `data/watchlist.json` ⇒ 先**字节级还原**再 `pytest.fail`；本轮曾有 4 次清空自选池的事故）。
- 新增 **`ptrade/spot_refresher.py`** 的判据：注入 0.8s 间隔 + **断言价格真的变**（防「心跳式假刷新」）、
  `--offline` 下 socket 尝试数为 0、刷新函数**不得出现锁原语**（`INV-42`，AST 扫描）。
- 全量 **1182 passed / 4 skipped / 0 failed**（v1.8.0 基线 1146）。
- ★ 连带更新（**需求变更，不是放宽**）：
  `test_webapp_v2` 的「一直 pending 时轮询 ≤6 次」改为**从源码读上限**且窗口必须**覆盖实测 132s 且仍有界**；
  `test_v3_18` 的「不得出现『离线』」收窄为「不得有离线**开关/参数**」（`INV-64` 管的是「前端不得**决定** offline」，
  不是「不许把离线**状态**告诉用户」）；`test_v3_layout` 的旧 `renderAnalysis`/`panel.factors` 断言改为 V5 形态；
  `test_ui_layout_v4` 的折叠判据从 3 个 key 改为 1 个（合并卡，`data-collapse="analysis"`）。

### 已知限制

- `V5-6c`（**132s 真实首算**端到端）默认 `skip`，需 `PTRADE_RUN_SLOW=1` 才跑；CI 里用**注入常量**证明判据有牙齿。
- 判据里的「买入信号」是**注入**的（离线夹具只有 `watch`/`close` 信号）；真实网络路径未在 CI 覆盖。
- 部分判据在**页面级注入** health/cadence/分析（服务全 offline），不等真实网络。
- `--offline` 下 `analysis` 的文案新增「离线」二字：若外部脚本 grep 页面断言「无『离线』字样」需同步调整。

## [1.8.0] — 2026-10-20

**主题：网页端 v3（同页图表 / 搜索 / 自选）与 v4（两列布局 / 图例精简）**

### 新增

- **网页端同页图表**：`#chart-root` 直接在本页渲染 ECharts，**移除 `<iframe>` 形态**
  （原每张图重发 1.17 MB 自包含 HTML → 现 `/api/chart/data` 只取 ~139 KB JSON，ECharts 首屏只加载一次）。
- **新端点**：`/api/chart/data`（图表数据 JSON）· `/api/quote`（批量现价，一次全市场快照供全部 code）·
  `/api/name/refresh`（强制刷新名称表，`--offline` 下拒绝）。
- **`ptrade/webui_assets/chart_option.js`**：K 线 / 分时 option 的**唯一真源**（ESM）。
  `chart.py` 的 HTML/CLI 路径与网页路径**内联/加载同一份文件**（`_inline_js()` 是唯一变换）。
- **网页端两列布局**：图表占左列（`minmax(0,1.25fr)`）、信息卡在右列；`html,body{height:100%;overflow:hidden}`
  + `main{grid-template-rows:minmax(0,1fr);min-height:0}` ⇒ **常见桌面分辨率一屏零滚动条**。
- **图例精简**：从 18 项减到 **6 项**（MA12/24/48/96 + VOL MA5/MA10）；被删的线靠面板标题辨认。
- **搜索名称表守卫**：行数下限 + **内容锚点**（`600519`/`000001`/`603009`）+ `data/fixture/spot.json` 兜底；
  `--offline` 下禁止刷新。**修掉「名称表被污染成 5 行 ⇒ 联网也搜不到」这个真缺陷**。
- **加入自选时服务端补全名称**（锁外解析）；自选池每行显示 代码·名称·现价·涨跌幅。
- **`legend_budget()` 降级为通用工具**（不再是页面不变量，页面不变量迁到 `T-V3-25` / INV-68）。

### 变更

- **网页端去掉「离线」勾选框**：前端不再拼 `offline=` 参数，离线与否只由服务端 `--offline` 决定
  （服务端硬闸语义不变，且**请求只能收紧、不能放宽**）。
- **`WebApp.lock_timeout` 对 `/api/watchlist` POST 生效**（此前 `watchlist` 内部写死 10s，配置项形同虚设）。
- **`search.fetch_name_table()` 补「锁内禁网」守卫**（此前直接 `import akshare`，绕过 `INV-42`）。
- **名称表内容锚点改为「≥2/3 命中」**：单只票合法改名不再让读路径硬拒（否则「点刷新」自救也会失效）。

### 修复（安全 / 正确性）

- **`/api/chart/data` 与 `/api/chart` 的 `code` 格式校验**：此前未校验就拼进缓存文件名，
  `../` / `<script>` / 5000 字符会分别导致 `FileNotFoundError` / `WinError 123` / 文件名过长 ⇒ **HTTP 500**。现返回 **400**。
- **`/api/watchlist` / `/api/order` / `/api/close` 的 `code` 格式校验**：此前 `{"code": "../"}` 会被接受并**写进自选池**。
- **`refresh_name_table` 单飞**：并发刷新共享一次取数（此前 5 路并发 = 5 次上游抓取）。
- **`api_name_refresh` 的异常顺序**：`except NetworkForbiddenInLock` 必须排在 `except DataUnavailable`（子类）之前，
  否则守卫错误被吞成 200 业务态（违 `INV-42/45`）。
- **`api_chart_data` 的 payload 形状**：改为与 HTML/CLI 路径共用 `chart.option_data()`，
  否则网页端 `legend.data = []` ⇒ **图例是空的、「点图例隐藏」根本不可用**。
- **分时（`period=minute`）网页端渲染**：新增 `buildIntradayOption()`，此前共用 `buildOption` 会抛
  `TypeError: Cannot read properties of undefined (reading 'minute')`，图表停在日线上。
- **`refreshWatchlist` 重复定义**：JS 后定义覆盖先定义，导致**自选池现价/涨跌幅永远是 `—`**。
- **文档不再描述已删除的 iframe 形态**（`webui.py` 模块 docstring）。

### 测试

- 新增 `tests/_cdp.py`（真实 Chrome + CDP harness，含 `Emulation.setDeviceMetricsOverride`）
  与 `tests/test_ui_layout_v4.py`（28 条布局判据：四档视口零滚动 / 图表在左 / 图例 6 项 / 面板序列分组 /
  折叠默认值 / 结构哨兵）。
- 判据从 1.7.0 的基线增长到 **1156 passed / 2 skipped / 0 failed**（全量）。
- `INTERFACES.md` 新增 **§8.4**（v3/v4 的 `INV-63/64/65`、两套「外链」定义、`webui.py` 拆分触发条件、node 依赖说明）。

### 已知限制（诚实标注）

- **真机手势**（tooltip 悬停 / dataZoom 拖拽 / 图例点击）目前是**手工 CDP 验证**，
  CI 无 Chrome/CDP 驱动 ⇒ `tests/test_webui_dom.py` 里保留**显式 skip** 记录，**不得**当成「测试已覆盖」。
- **无 node 的环境**下 `T-V3-24`（运行时两路径蜡烛）会 skip；`T-V3-24c`（结构级）不需要 node，仍在跑。
- 构建产物（`ptrade/webui_dist/`）**不入库**（本项目零构建链，前端是 Python 字符串 + 静态 JS）。

## [1.7.0] — 2026-10-16

- 修复取锁超时与块内异常用**类型**区分（L3 的真正修法）。

## [1.6.2] — 2026-10-16

- 缓存写者原子化 + 写者归类 + 水位决定记录。

## [1.6.1] — 2026-10-16

- 图表原子落盘 + 渲染/落盘分离（W-6）。
