# Changelog

本文件记录 ptrade 的版本变更。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
