# Changelog

本文件记录 ptrade 的版本变更。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
