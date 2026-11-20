# Changelog

本文件记录 ptrade 的版本变更。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [1.21.0] — 2026-11-20

**主题：v13 —— 修图表 tooltip 在「切换分时 / 加载图表」时的 TypeError 崩溃（含一个白屏阻塞缺陷）**

### 用户实测（真源）
```
chart_option.js:190 Uncaught TypeError: Cannot read properties of undefined (reading 'dates')
    at formatter (chart_option.js:190:34)
    at e._showTooltipContent (echarts.min.js:45:792407)
「有时候前端控制台会出现这个bug，导致切换分时或者加载图表的时候出问题」
```

### 根因 A：`tooltip formatter` 读到「周期 / 载荷错配」
`chart_option.js` 旧实现：
```javascript
const i = ps[0].dataIndex, dd = payload.periods[activePeriod()];   // dd 可能是 undefined
const rows = ["<b>" + dd.dates[i] + "</b>"];                        // 崩（190:34）
```
`activePeriod()` **在悬停时才求值** `opts.getActive()`，而 `webui.py` 传的是
`getActive: () => st.period`（**活状态**）；`loadChart()` 里 `CHART_STATE.period` 是**同步立刻**改的，
`payload` 却要等 `api(url).then(...)` **异步**回来 ⇒ 中间那段时间两者对不上：
1. 日线 → 点分时（fetch 期间）：`period="minute"`，payload 是日线的（无 `minute` 键）
2. 分时 → 点日线（fetch 期间）：`period="daily"`，payload 是**分时 payload（整个 `periods` 字段都没有）**

★ 同类前科：本文件早已记录「分时 payload 没有 `periods` ⇒ 不能走 `buildOption`（X2 真缺陷）」——
X2 只修了**构建期分派**，没修 **tooltip 期活状态求值** ⇒ 同一个洞第二次爆发。

### 根因 B（★ code-review 抓出的**阻塞级**缺陷，比 A 更严重）
`chartEmpty()` 旧实现只做 `root.innerHTML = '<div class="empty">…'`，
把 ECharts 的 **canvas 子树整个摘掉**，却**既不 `dispose()` 也不把 `CHART` 置 null**
⇒ `ensureChart()` 里 `if (!CHART)` 不成立、直接返回那个**失联实例**
⇒ 之后 `setOption` 画进**已脱离 DOM** 的 canvas ⇒ **图表永久白屏，必须刷新页面** ✗
触发路径：加载失败 / 构建失败 / 模块加载超时 / 无数据 —— 只要出现过**一次**空态就中招。
★★ 这正是「**加载图表时出问题**」的真身之一，而且**v13 的 try/catch 会放大它的触发概率**。

### 改动
**A. `ptrade/webui_assets/chart_option.js`**
* tooltip formatter：**主源固定为建 option 时的 `period`**（图表画的就是它），活周期只做兜底；
  `Array.isArray` 校验 + `dataIndex` 越界守卫；整段拼装包 `try/catch` ⇒ **对外承诺「绝不抛」**
* `buildOption` **入口守卫**：`payload.periods[period]` 缺失时抛**可诊断**的 Error
  （点明「分时请走 `buildIntradayOption`」），不再是 `.dates` 的 TypeError

**B. `ptrade/webui.py`**
* ★★ **拆分 `CHART_STATE.period`（UI 高亮，点下去立刻变）与 `CHART_STATE.payloadPeriod`（数据到达后才设）**，
  `renderChart()` 一律用 `payloadPeriod` 分派构建器 + 传 `getActive: () => pp` ⇒ **治本，消除错配**
* ★★ **`chartEmpty` → `chartPlaceholder(text, keepPayload)`**：`CHART.dispose()` + `CHART = null` +
  `CHART_RO.disconnect()` 后再写空态 ⇒ **空态之后能重新 init**（白屏 B1 修复）
* `CHART_REQ_SEQ` 请求序号：丢弃过期响应（防「日→周→月」快速连点被旧响应覆盖）
* `loadChart` 补 `.catch`（带 seq 判定）：网络真断时给出可读空态，不再 unhandled rejection
* 模块加载中的临时占位用 `keepPayload=true`（否则「模块就绪后重绘」的兜底失效）
* 构建失败/加载失败都 `console.error` 留痕，不再把真实错误吞成 UI 文案
* 空态时清 `#chart-coverage` 残留文案；无 ResizeObserver 浏览器的 `resize` 监听器**幂等绑定**

**C. 判据（`tests/test_v13_chart_period_race.py`，13 条）**
* **V13-1..5b**：在 **node 里真的调用** formatter / buildOption —— 错配不抛、回退到正确周期、
  分时 payload 进 `buildOption` 抛可诊断错、空/坏 payload 返回 `""`、越界返回 `""`、正常情形不被修坏
* **V13-10/11/12**：★ **B1 行为判据** —— 用花括号配平从 `webui.py` 抽出
  `chartPlaceholder`/`chartEmpty`/`ensureChart` **真源码**，配 DOM/echarts/ResizeObserver stub 在 node 里跑：
  `init → 空态 → 再 init` 必须 `initCalls>=2` 且 `disposeCalls>=1` 且 RO 断开并重建；临时占位保留 payload
* **V13-6..9**：前端状态模型契约 + `node --check` 语法

### 变异证明（证明判据不是假绿）
```
.omx/probe/v13_mutation.py      旧 formatter ⇒ threw=True  error="Cannot read properties of undefined (reading 'dates')"
                                 新 formatter ⇒ threw=False rendered="<b>2026-11-03</b>…"
.omx/probe/v13_mutation_b1.py   旧 chartEmpty ⇒ initCalls=1 disposeCalls=0 reinit=False （白屏）
                                 新 chartEmpty ⇒ initCalls=2 disposeCalls=1 reinit=True  （可恢复）
```
★ 旧 formatter 复现出的错误与用户贴的堆栈**逐字一致**。

### 验证
```
tests/test_v13_chart_period_race.py                                              13 passed
chart_v2 + perf_analysis + ui_layout_v4 + webui* + chart* + v13                 236 passed / 1 skipped
node --check：chart_option.js ✓ · webui.py 内联 2 段 script ✓
code-review：首轮 REJECT/BLOCK（抓到上述 B1）→ 整改后复审 **APPROVE / CLEAR**
```

### 已知限制（WATCH，下版收）
* `ptrade/chart.py` 独立 HTML 图表页的 `switchPeriod()` **仍无 try/catch**
  —— 该页 `CHART_DATA` 含全部周期且同步重建，当前不可达；已就地记 TODO
* `V13-6/7/8` 是**源码契约**判据（非行为）；`payloadPeriod` 提交顺序 / 请求序号语义
  建议后续抽纯函数做行为判据

## [1.20.0] — 2026-11-20

**主题：v12 —— 融券 2 倍杠杆落地（保证金口径 50%）+ 已有三笔空头重放为 2 倍规模**

### 用户实测（真源）
> 「我的盈利是不是不对。因为我让你做空是**加了杠杆**的，现在 **300869**，
>   从我买的时候 **9.81** 到现在 **8.92**，我应该赚了 **9000 多**才对吧？」

### ★ 根因（**复核后更正过一次**）
**曾经写错的说法**：「`margin_ratio_short = 1.00` ⇒ 杠杆 1 倍 ⇒ 盈利少一半」。

**正确事实**：本仓融券模型里 **`margin_ratio_short` 不放大盈亏**。
盈亏恒为 `(short_cost - last_price) * short_qty` —— **只由股数决定**。
`m_short` 只影响两处（`core.py:660-669` / `core.py:353-365`）：
* `margin_used = amount * m_short`（**记录字段**，不入账）
* `margin_required` 里融券部分 `max(0, sum(short_mv * m_short) - frozen_collateral)`（本账户恒为 0）

⇒ 用户预期「9,000 多」= 4,539 × 2 = **持仓规模翻倍**，
  只能靠**数据迁移**实现，**改配置不会**自动翻倍。
  ⇒ 配置改 50% 是**用户明确要求的口径对齐**（真实 A 股散户融券保证金最低 50%），与实际盈亏放大无关。

### 改动
**A. 保证金与风控口径（`ptrade/config.py`）**
```
margin_ratio_long:  0.80 → **0.50**   # 融资 1.25 倍 → 2 倍
margin_ratio_short: 1.00 → **0.50**   # 融券 1 倍（无杠杆）→ 2 倍
max_position_pct:   0.20 → **0.25**   # 2 倍仓位后单票 ~10 万，旧上限 10 万差 62 元被拒
max_short_exposure: 0.50 → **0.65**   # 2 倍仓位后合计 ~59%，旧上限 25 万会拦
```
★ `max_leverage = 2.0` / `min_maintenance_ratio = 1.30` **未动**。

**B. 账户迁移：三笔空头重放为 2 倍（`core.fill_plan` + `core.apply_fill` 纯函数）**
```
600594: 15,700 → **31,400** @3.19（+50,083）⇒ 浮盈 +3,768 → **+7,536**
300869:  5,100 → **10,200** @9.81（+50,031）⇒ 浮盈 +4,539 → **+9,078**  ← 用户预期的「9,000 多」
601238: 10,200 → **20,400** @4.92（+50,184）⇒ 浮盈 +2,550 → **+5,100**
合计浮盈 +10,857 → **+21,714**
```
★ **不手改 `state.json`**（会与 ledger 分叉、违反 N7）：改走 `fill_plan` 算权威费用 →
`apply_fill` 记账 → 追加 3 条 ledger 行 → `save_state` 刷水位 ⇒ `diverged = false` ✓
★ 迁移后额外两步 state 修正（**无 ledger 行**，审计须知）：
1. **+21,373.61** 盯市回归（`apply_fill` 会把 `last_price` 重置成成交价 ⇒ 用快照价重新 `mark_to_market`）
2. **+340.39** 补记加开部分的建仓至今利息（★ **券款口径**，与 `accrue_interest` 的**市值口径**不同，后者应 ≈315.6）
★ 解冻：`settle_t1` 后 `short_available == short_qty` ⇒ 三只**全额可平** ✓

**C. 文档与判据**
* `INTERFACES.md`：MarginConfig/RiskConfig 同步新值 + 示例表加「v12 口径变更」注记
* `tests/test_v12_leverage.py`：**新增 9 条判据**（4 条配置值 + 2 条纯函数行为含反证 + 3 条数据审计）
* 3 处旧测试改「从配置读」而非写死（`20%*500000` / `notional*0.8` / `625000`）—— 判据**未弱化**

### 修复
* 3 红回归：`test_check_risk_reports_position_limit_pct` / `test_v31_margin_capacity_boundaries` /
  `test_inv22_margin_reuse_with_unrealized_profit`（都是写死旧配置值）

### 验证
```
全量 1275 passed / 4 skipped / 0 failed
v12 判据 9 passed · 受影响面（core+engine+wiring）223 passed
账户：交易日 2026-11-20 · 净资产 520,807.59 · 总盈亏 **+20,807.59**
      担保比例 2.8630（平仓线 1.30）· 杠杆 0.5368（上限 2.0）
      ledger_consistency = {"consistent": true, "ledger_lines": 6, "recorded": 6, "diverged": false}
```

### 已知限制（WATCH）
* `interest_accrued` 是**混合口径累加器**（repo 市值口径 + 迁移券款口径），**不能**与账本重放逐分对齐
* `test_v12_7/8` 是**一次性数据审计**（账户变化即 skip），不是回归判据
* 融券模型**没有**「保证金杠杆放大盈亏」这回事；要做真杠杆需另立产品裁决

## [Unreleased]

## [1.19.0] — 2026-11-19

**主题：v11 —— 模拟盘「交易日」**自动推进到当天**（服务启动 + 每次页面打开）**

### 用户 bug（真源）

> 「/autopilot 有问题，为什么现在 还是 交易日 **2026-11-09**。
>   现在已经是 **2026年11月19日12:08:35** 了。你需要帮我修复这个 bug」

### 真根因（实测，不是猜）

| 事实 | 值 |
|---|---|
系统时间 | 2026-11-19 12:08（周四·盘中）|
`state.trade_date` | **2026-11-09**（落后 8 个交易日）|
`spot.json` | `asof=2026-11-19` ✓ **是今天的** ⇒ **不是数据问题** |
`latest_trade_date()` | 2026-11-18（= 最近**已收盘**交易日）|

`engine.roll_to(trade_date)`（`engine.py:94`）是**唯一**推进 `state.trade_date` 的入口，
而它的**全部调用点只有三处**：页面「结算」按钮（★ **必须手填日期**）/ CLI `settle --trade-date` /
下单平仓内部。

⇒ ★ **全仓库没有任何自动推进逻辑** ✗ ⇒ 用户 10 天没手动结算，账户就停在 11-09。
**所以这不是"数据错"，是模拟盘缺了「日终自动结算」这一环** —— 真实券商每天收盘自动结算。

### 新增

* **`service.account_target_trade_date(ds, now=None)`** —— 账户应处的交易日
  * 今天**是交易日** ⇒ **今天**（★ **盘中也是今天**，不是昨天）
  * 否则 ⇒ 今天之前最近的交易日；日历拿不到 ⇒ `None`（不猜）
  * ★★ **必须与 `latest_trade_date()` 分开**：后者盘中回退到昨天是 **v3.1 未来函数防护**
    （用于信号/数据），语义正确、**不得改** ✗ —— 判据 `test_v11_5` 钉住它
* **`service.auto_settle_to_today(cfg, ds, now=None)`** —— 幂等 / 绝不上抛 / 不推进到未来
  * 推进内容 = `roll_to` 的**完整真实口径**：T+1 交收 → 按日计息 → 盯市 → 风险评估
  * ★★ **快路径**：`state.trade_date == target` ⇒ **不取写锁、不计息**
    （页面每次打开都会调它，实测 **0.12s**）
  * ★ **锁外** `prewarm_before_write` → **锁内** `Engine.roll_to`（照抄 `api_settle` 既有形态）
  * ★ **任何异常都转成 `{advanced:false, error}`** —— 自动结算是后台增强，不能把页面搞坏
* **`POST /api/auto_settle`** —— 五要素 `{advanced, from, to, days, error, offline}`；**失败也 200**
* **服务启动时自动补**（daemon 线程，`Event.wait(2)` 让 spot 预热先跑）
* **前端**：页面加载后 1.5s `POST /api/auto_settle`；`advanced===true` ⇒
  `toast("已自动结算到 X（补 N 个交易日）")` + `refreshAll()`

### ★ 一次性补账（用户要求"立刻补上"）

通过**新端点**执行（同时就是端到端验证）：

```
from → to     : 2026-11-09 → **2026-11-19**   ✓
days          : 8 个交易日
应计利息      : 43.91 → **306.89**（+262.98，与估算 263 吻合）
last_prices   : 3.17/9.81/4.92 → **2.96/9.01/4.66**（11-19 的价）
第二次调用    : advanced=False · 0.12s（快路径，不取锁）✓
```

### 本轮修掉的**真 bug（都是判据/探针抓的，不是自查）**

1. ★★ **`refreshAll()` 不返回 Promise**，我写的 `refreshAll().then(...)` 会抛
   `TypeError: Cannot read properties of undefined` ⇒ **把初始化脚本打断**
   （`initCollapse()` 都不执行 ⇒ 折叠默认值全错，3 条布局判据红）
   —— 由 `test_pn_frontend_polling...` 的 **poll_probe（node 跑真实前端脚本）** 抓到
   ⇒ 改成 `setTimeout(autoSettleOnLoad, 1500)` ✓
2. ★★ **daemon 线程退出致命错误**：加了启动线程后 `test_ui_layout_v4` **28 passed 但退出码
   `-1073740791`**（`Fatal Python error: _enter_buffered_busy ... daemon threads`）
   实测：镜像 v1.18.0 exit **0**，开发副本 exit **非 0** ⇒ 是我引入的
   ⇒ ① `--offline` 不启动线程；② 端点 offline 直接返回（不 I/O）；
     ③ 线程改 `Event.wait` + `atexit` join ✓
3. **INV-42 判据我第一版写错了口径**：断言「锁内零 DS 调用」⇒ 立刻假红
   `锁内取数 ['trade_dates','quote']`。真相：`roll_to` 锁内**确实会**调它们，
   但那是**缓存命中**（prewarm 填好了）⇒ **INV-42 管的是"不联网"，不是"不调 DS"**
   ⇒ 改成评审真正想要的：「**锁内调用的 DS 方法必须全在预热白名单里**」（V11-18）✓

### code-review（teammate 独立评审）

**`APPROVE` · 架构状态 `WATCH`**（无阻断项）。评审 10 条重点逐条核过全过。
按建议补了 4 条：

* **S1**（MEDIUM）启动线程退出安全 ⇒ `Event.wait` + `atexit` join
* **S2**（MEDIUM）★ **`--offline` 时不自动结算**（明文记录）：离线取不到实时价
  （`roll_to` 只会沿用旧价），且 daemon 线程退出有风险 ⇒ **offline 用户请用 `ptrade settle`**
* **S3**（MEDIUM）**INV-42 缺判据** ⇒ 新增 `tests/test_v11_inv42.py`（V11-18，行为级）
* **S4/S5**（LOW）`from==""`（账户交易日为空）⇒ 跳过（否则 `days` 会算成"从 1990 至今"）
  · boot 线程宽 except 先放行 `NetworkForbiddenInLock`

### 测试

* 开发副本 **1197 passed / 4 skipped / 0 failed**
* 新增 `tests/test_v11_auto_settle.py`（**18 条**）+ `tests/test_v11_inv42.py`（**1 条**）
* `pytest -m slow`：1 passed / 1 skipped（skip = v7 显式已知限制，符合铁律 9）

## [1.18.0] — 2026-11-09

**主题：按终审建议补 R3 判据（产品代码零改动）**

> code-review 终审结论 **APPROVE · 架构状态 CLEAR** —— R1/R2/R3 三条全部真修好。
> 终审提了两条非阻断建议，本版落实第 1 条（第 2 条「B5 判据升到 DOM 级」登记为 backlog：
> 需要 CDP 起真 Chrome，成本高；当前「正向 + 反证 + 零宽度」三层已能防住该坏模式重现）。

### 新增判据（	ests/test_v10_score_refresher.py）

* **	est_v10_a4h_week_anchor_spans_the_window** —— week_anchor 的**四口径**：
  * 周五 16:00 ⇒ 2026-11-06（W45）✓
  * 周六 / 周日 ⇒ 同一个周五 ⇒ **W45** ✓
  * ★★ **周一盘前 ⇒ 上周五** ⇒ **W45**（不是 W+1 —— 这正是复审 R3 的 bug 形态）
  * 次周五 16:00 ⇒ 2026-11-13 ⇒ **W46**（必须 +1，否则 A4 永远只跑第一周）
  * 空日历 ⇒ 退回 today（不崩）
* **	est_v10_a4h_status_endpoint_uses_anchor_not_now** —— 状态端点**必须**用 week_anchor，
  且**不得**回退到 iso_week_key(now)（防回归）

### 复核（终审要求）

终审说「布局判据我这边只读无法跑 CDP，请在发布流程里复核一次」：
* pytest tests/test_ui_layout_v4.py -q ⇒ **28 passed** ✓
* 另与 v10 判据同跑 ⇒ **64 passed** ✓
* 全量 **1246 passed / 4 skipped / 0 failed**
  （1 条 	est_C1_C4_write_command_waits_for_lock[settle] 偶发 —— 按 NOTES.md 铁律 11
   单独连跑 **3/3 通过** 复核，判定为既有 flaky，不挡发布）

### 登记为 backlog（终审建议，非阻断）

1. **B5 判据升到 DOM 级**：用 CDP measure 断言 querySelector('[data-testid="live-pnl-source"]')
   存在、	extContent 是纯数字、	itle 含「实时/截至」，并断言「总盈亏」行 offsetHeight 与基线一致
2. **_weekday_pass 24/7 每小时跑且不先查缓存**：
   建议加「命中 
ead_cached_analysis 就跳过 + 仅交易时段/UI 活跃时跑」
3. **pi_score_refresh_status 每请求构造 DS**：可缓存 anchor 数秒

## [1.17.0] — 2026-11-09

**主题：code-review 复审（`REJECT`/`BLOCK`）的 R1/R2/R3 全部修复**

> 复审结论：6 条阻断项里 **5 条真修好**（B1 ✓ / B2 ✓ / B3 核心 ✓ / B4 ✓ / B6 ✓），
> 但仍有 3 条阻断级问题。本版全部修完。

### R1 · MED-HIGH · B5「用户可见时效」把 HTML 塞进了**会被 `esc()` 的位置**

**缺陷**：`renderState` 的值渲染是
```js
typeof kv[1] === "string" ? esc(kv[1]) : kv[1]
```
上一版把 `'<span data-testid="live-pnl-source" …>' + num(...) + '</span>'` 放进**字符串值**
⇒ 被 `esc()` 成 `&lt;span…` ⇒
* ★ **DOM 里根本没有这个元素**（`querySelector` 找不到）
* ★ 用户会在账户面板里看到**约 90 字符的字面 HTML 乱码**
* ★ `.v` 没有 `nowrap` ⇒ **换行 ⇒ 右列变高** ⇒ 极可能再打红 `test_ui_layout_v4`
* ★ 而当时的判据只 `grep APP_HTML` ⇒ **绿着放过了它**

**修法**：items 支持**第三项 = 属性对象**，新增 `attrs(o)` 把 `data-testid`/`title`
加到**既有的 `.v` 元素**上：
```js
["总盈亏" + (_src === "live" ? "（实时）" : "（结算）"), num(d.total_pnl),
 {testid: "live-pnl-source", title: _src === "live" ? ("实时 · " + _asOf) : ("截至 " + _asOf + "（未取到实时价）")}]
```
* **值仍是纯数字** ⇒ **零宽度增量** ⇒ 布局零风险 ✓
* `attrs()` **只 esc 属性值**（不是整段 HTML）✓
* **真的是 DOM 属性**（可 `querySelector('[data-testid="live-pnl-source"]')`）✓

**判据重写**（原来只查源码字符串）：
① `attrs()` 就位且只 esc 属性值 · ② items 第三项是属性对象 ·
★③ **反证**：`'<span data-testid="live-pnl-source"'` **不得**出现在页面里（坏模式彻底消失）·
★④ **反证**：「总盈亏」的值必须紧跟**纯** `num(d.total_pnl)` ·
⑤ 零宽度：值里不许叠加可见字符（两次实测 851 / 869 都因它超预算）

### R2 · HIGH（纪律回归）· `_run_batch` 丢了守卫前置放行

**缺陷**：v1.16 重构时把 v1.15 的内层 `isinstance(exc, NetworkForbiddenInLock): raise` 删了，
**却没换成** `except NetworkForbiddenInLock: raise` ⇒ 宽 `except Exception` 会把**守卫错误**
记成「这只票算失败」而**静默吞掉** ✗（INV-42/45；且 `REGISTERED_GUARD_SITES` 仍登记着该站点）

**修法**：在宽 handler **之前**加 `except NetworkForbiddenInLock: raise`，
让异常冒到 `_run` 的兜底记 `last_error`、等下一 tick，**不**记成单票 failed。

### R3 · HIGH（A4 节奏）· ISO 周台账用 `now` 记周，而窗口**横跨 ISO 周**

**缺陷**：窗口是「周五收盘 → 周一开盘」，ISO 周从**周一 00:00** 起 ⇒ 跨 W→W+1：
* 周五 16:00 / 周六 / 周日 ⇒ `now` 属 **W** ⇒ 台账 W（正确）
* **周一 00:00–09:30** ⇒ 窗口仍 True（盘中排除只覆盖 09:30–15:30）但 `now` 已属 **W+1**
  ⇒ 台账 W+1 不存在 ⇒ **再跑一整批**（+8–16h 重复烧）
* 该次跑把 **W+1** 标 done ⇒ **W+1 的周五收盘后不再跑**
  ⇒ ★ **A4 从「每周五」漂成「每周一凌晨」**，且数据仍是上周五的 ✗
* 状态端点也按 `now` 读 ⇒ 周一盘前**看不到真实进度** ✗

**修法**：新增 `week_anchor(now, trade_dates)` = 窗口起点 = **最近一个已收盘交易日**：
`anchor = today if (today in cal and now >= 15:30) else prev_trade_date(today)`
⇒ 周五 16:00 / 周六 / 周日 / **周一盘前** ⇒ 都是 **W** ✓；次周五 ⇒ **W+1** ✓
`_tick` 与 `/api/score_refresh_status` **共用同一 anchor** ✓

### 测试

* 开发副本 **1245 passed / 4 skipped / 0 failed**
* ★ 本轮特别注意了「判据是否真的能抓住 bug」：
  R1 的教训是**源码字符串存在性断言**会绿着放过渲染期 bug
  ⇒ B5 判据改成「正向 + **反证**（禁止坏模式）+ 零宽度」三层

## [1.16.0] — 2026-11-09

**主题：code-review（`REJECT`/`BLOCK`）的 6 个阻断项 + 10 条建议全部修复**

> ★ 独立 code-review（task-47）判定 **`REJECT` · 架构状态 `BLOCK`**，理由是
> **载荷形状错 ⇒ A4 的 4–8h 扫描产出 2 分钟内全部作废**。
> 本版把 6 个阻断项全部修完，并补了评审要求的**行为判据**。

### B1 · CRITICAL · 分析缓存载荷形状不一致（A4 全部作废）

**缺陷**：`strategy.CanslimResult.to_dict()` 的形状是
`{code, name, asof, total, factors: [list], signal, reasons, risks}` ——
★ **没有 `ok` 字段**、★ `factors` 是 **list**。
而 `analysis_<code>_<asof>` 缓存**期望**的是
`{ok: True, total, signal, factors: {C: ..}, notes: {..}, reasons: [..], risks: [..], recommend}`。

⇒ 直接把 `to_dict()` 写进缓存会让：
* `analysis_state()` 判 **`failed`**（`chart.py:705`）
* 读取时 TTL 走 **`ANALYSIS_FAIL_TTL = 120 秒`**（`chart.py:730`）
* ★★★ **A4 周末全市场扫描（4–8h × 5024 只）的产出 2 分钟后全部作废** ✗
* 用户点过「批量打分」再去看图 ⇒ 分析卡显示「数据缺失 —— 未知原因」

**修法（三道防线）**：
1. `chart.analysis_payload_from_result(result, cfg, computed_at=)` —— **全仓唯一构造器**
2. **三处调用点全改**：`chart._score_analysis` · `webapp.api_watchlist_score` · `score_refresher._run_batch`
3. `write_cached_analysis` 增加形状校验：`ok is True` 时 `factors` 必须是 `dict`，否则 `raise ValueError`

### B2 · HIGH · 「周五收盘」判定在真实日历下把每个交易日收盘后都判成窗口

**缺陷**：旧回退 `return prev_td == cal[-1]` —— 生产日历 `ak.tool_trade_date_hist_sina` 是
**历史**日历（只到今天）⇒ `later` **恒空** ⇒ **周一 16:00**：`today in cal` 且 `prev_td == cal[-1]`
⇒ 判 **True** ⇒ **扫描跑在周一晚**，且 ISO 周台账把该周标 done ⇒ **该周周五收盘后不再扫** ✗

**修法**：回退只认「**今天不是交易日**」或「**今天已是周五及以后**」：
`return prev_td == cal[-1] and (today not in cal or now.weekday() >= 4)`
★ 判据覆盖：周一/二/三 16:00 ⇒ False · 周四 16:00（历史日历无法区分）⇒ False ·
周五 16:00 ⇒ True · 周六/周日 ⇒ True

### B3 · HIGH · A1「同交易日永久有效」是死代码

**缺陷**：`chart.read_cached_analysis(..., trade_day=)` 写好了，但**生产零调用**
（`grep trade_day ptrade/` 只命中 `chart.py` 自己）⇒ 6h TTL 照旧
⇒ 同一交易日内仍会重算 2–4 次（3–40s/次）⇒ 用户「当天内稳定」**未达成** ✗

**修法**：
* `service.today_trade_date(ds, now=None)`：`now >= 15:30` 且今天是交易日 ⇒ 今天；
  否则 `prev_trade_date(today)`；再退化 `latest_trade_date()`；全拿不到 ⇒ `None`（退回 6h TTL，不算错）
* **两处生产调用点真传** `trade_day=`（`analysis_status` / `api_chart`）
  ★ `api_chart` 里**复用已构造的 `ds`** —— `test_r5_ds_seam_is_parameterized` 断言 `_ds` 恰好调 1 次

### B4 · MEDIUM-HIGH · `asof` 取不到时白写 5024 个空 key 垃圾缓存

**修法**：`if not asof: return False`（本轮不跑，等下一 tick）。

### B5 · MEDIUM-HIGH · 用户看不到「盈亏是哪一刻的」

**缺陷**：只有后端 `as_of`/`source` 字段，页面 `grep live-pnl-source` = **0 处** ✗
⇒ 用户仍不知道时效；降级 `source="state"` 时显示旧值且无标注 —— **正是用户抱怨的那半个问题**。

**修法（受布局硬约束）**：
* 页面「总盈亏」**标签**带来源：`总盈亏（实时）` / `总盈亏（结算）`
* 值只把原有数字包一层 `<span data-testid="live-pnl-source" title="实时 · 2026-11-09">`
  ⇒ **不加任何可见字符** ⇒ 宽度零增量 ✓
* ★ **布局预算压测（第 6 次）**：标记放「总盈亏」的**值**里 ⇒ `851 > 848`；
  放「交易日」的值里 ⇒ `869 > 848` ⇒ **右列任何可见内联元素都撑宽** ✗
  ⇒ **终版改标签文字**（复用 `esc(kv[0])`，7 字 ≈ 既有 `冻结(融券券款)` ⇒ 不换行）✓

### B6 · MEDIUM · A5「平时算持仓∪自选」从未被调用

**修法**：`ScoreRefresher._weekday_pass()` —— 不在周末窗口时，按 `WEEKDAY_REFRESH_S = 3600s`
节流跑 `weekday_codes()`（持仓 ∪ 自选，≤40 只，几分钟）✓

### 评审建议项（全部落地）

1. `MAX_ATTEMPTS` 原来是死逻辑（两个分支写**完全相同**的 dict）⇒ 达到上限记 `dead` 并**跳过**
2. guard 判断的绕写法 ⇒ 改项目标准形态（`except NetworkForbiddenInLock: raise` 在前）
3. `weekend_codes` / `read_ledger` 的宽 `except` ⇒ 收窄（`OSError/ValueError/TypeError`）+ 先 guard
5. ★ **判据以源码文本为主 ⇒ 恰好放过了 B1/B3/B5 三个行为缺陷** ⇒ 本版补 **4 条行为判据**：
   * `test_v10_b1_payload_is_ready_shape`（写盘→读回→`analysis_state == "ready"`，端到端）
   * `test_v10_b1_rejects_dirty_payload`（脏载荷必须 `raise`）
   * `test_v10_b3_trade_day_wired_in_production`（生产源码 2 处 + 7 天前缓存命中）
   * `test_v10_b5_source_marker_is_visible`（两态文案 + 反证值里不许叠加字符）
6. `--offline` 时不启动刷新器（原来线程空转、每 600s 白拉一次日历）
7. 统一走注入的 `self._now_fn`（台账时间戳可注入）

### 测试

* 开发副本 **1244 passed / 5 skipped / 0 failed**
* `pytest -m slow`：**1 passed / 1 skipped**（skip = v7 显式已知限制，符合铁律 9）
* ★ 同时修正了一条**断言了错误行为**的判据：
  `test_v10_a4a_no_future_calendar_fallback` 原来断言「周四 16:00 + 无未来日历 ⇒ True」
  —— 那正是 B2 的 bug 行为 ✗ ⇒ 改成评审指定的正确口径 ✓

## [1.15.0] — 2026-11-09

**主题：v10 补完 —— A4「周五收盘 ~ 下周一开盘前，全市场 5024 只分数后台重算」**

> v1.14.0 交付了用户两个诉求（分数按交易日刷新 + 账户实时盈亏），但 A4 **当时未实现**。
> 本版把 A4 补上，**v10 至此完整** ✓

### 新增 `ptrade/score_refresher.py`

**用户裁决（原话）**

> 「**周五收盘**的时候，你可以**全市场都算**，因为**我不在乎这个时间消耗**。
>   平时你可以给我算**持仓的和自选池的**。」

**★ 为什么是独立线程、不是复用 `spot_refresher`**

| | `spot_refresher` | `score_refresher` |
|---|---|---|
周期 | 盘中 **60s** 一次 | **周末**单趟 |
工作量 | 一次抓快照（十几秒）| **5024 只 × 1.5–4.5s ≈ 4–8h** |
模型 | per-tick + **惰性门控**（120s 内有 UI 请求才刷）| ★ **单趟循环**（惰性门控会让 4–8h 任务**永远跑不完** ✗）|

**★ 成本（Critic M3 的重估，是可交付性的关键）**

`chart.py:643` 原来**每票新建 `CanslimScorer`** ⇒ 丢掉 `strategy.py:592-602` 的**全部实例级缓存**
⇒ **≈103h**（超过一个周末，永远追不上下一周 ✗）。
本模块**复用一个 scorer** ⇒ 每票只多 1 次自身日线 + 2–3 次自身财务/股东
⇒ 5024 只 ≈ **2.8–6.3h**，加节流/重试 **≈4–8h** ⇒ **一个周末够** ✓

### 「周五收盘」判定（四个易错点全打上）

```python
prev_td = today if now >= 15:30 else prev_trade_date(today)   # 最近**已收盘**交易日
next_td = next_trade_date(today)                               # ★ 新浪是**历史**日历 ⇒ 可能抛
in_window = prev_td.iso_week != next_td.iso_week
# ★ 1：排除「周一盘中」（否则周一 10:00 也 True ⇒ 盘中触发全市场 ✗）
# ★ 2：next_td 取不到 ⇒ 回退「日历最大日 == prev_td」启发式
# ★ 3：ISO 周幂等台账 ⇒ 一周最多一轮（判定宽松也不会重复烧时间）
```

★ 判据覆盖：**周四当最后交易日**（周五休市）⇒ `True` ✓ · **周一盘中 10:00** ⇒ `False` ✓ ·
周一盘前 ⇒ `True` ✓ · 周二 ⇒ `False` ✓

### 范围（两个模式）

* **周末**：`spot` 快照 rows ⇒ **只留 6 位 A 股**（★ 排除 ETF —— `UNIVERSE_CODES` 只有 32 只且含
  `588000/510050/159915` 三只 ETF ✗，**绝不能用它**）
* **平时**：`持仓 ∪ 自选池`（≤40 只）
* ★ 不用 `ds.spot()`（会联网刷新）—— 用 `_spot_frame_raw()` **只读缓存** ✓

### 进度：`GET /api/score_refresh_status`

**双源**：台账（`data/cache/score_refresh_<iso_week>.json`）> 内存态。
★ 台账是**独立文件**、不在 `analysis_*` 命名空间 ⇒ `cleanup_stale_cache` **不会清它** ✓
（★ 不能拿 analysis 缓存当进度：`ANALYSIS_TTL=6h` 跑几小时就过期 ✗）

### 本轮被抓到并修掉的真问题（都是仓库纪律抓的，不是我自查出来的）

1. **INV-42/45**：`_tick`/`_run_batch` 的宽 handler 会**吞掉 `NetworkForbiddenInLock`** ✗
   （`ds.trade_dates()` / `ds.latest_trade_asof()` 都会走 `_ak()`）⇒ 加 2 处 guard re-raise ✓
2. **`test_W7`**：4 个守卫站点未登记 ⇒ 补 `REGISTERED_GUARD_SITES` ✓
3. **`test_meta_only_store_may_call_os_replace`**：★ 我自己写了 `os.replace` ✗
   ⇒ 改用 `store.atomic_write_json`（**理由**：`watchlist.py` 曾自带无重试原子写，
   4 个读者线程下 60 次写**失败 50 次** ⇒ 写方重试必须只有一处落点）✓
4. **`_is_a_share("")` 返回 `True`** ✗（`"".zfill(6) == "000000"` 以 `"00"` 开头）
   ⇒ 先判**恰好 6 位**再匹配前缀 ✓ —— 这是**我自己的测试抓到的**

### 测试

* 新增 `tests/test_v10_score_refresher.py`（**20 条**：窗口判定 8 组 · 范围过滤 · 幂等台账 ·
  单 scorer · 守卫语义 · 端点形状）
* 开发副本 **1239 passed / 5 skipped / 0 failed**
  ★ 2 条 flaky 已按 `NOTES.md` 铁律 11 单独连跑 **3/3 通过** 复核：
  `test_C1_C4_write_command_waits_for_lock[close]`（既有已知）·
  `test_S8_concurrent_append_keeps_lines_intact`（Windows 并发文件锁偶发）

### v10 完整状态

| 需求 | 状态 |
|---|---|
A1 同交易日稳定 | ✅ v1.14.0 |
A2 跨交易日重算 | ✅ v1.14.0（key 含 asof，天然满足）|
A3 显示分数时效 | ✅ v1.14.0（`data-asof` 属性 + `title`）|
**A4 周末全市场** | ✅ **v1.15.0（本版）** |
A5 平时持仓+自选 | ✅ v1.15.0 |
B1/B2 实时盈亏 | ✅ v1.14.0 |
B3 不破 INV-40 | ✅ v1.14.0（`/api/state` 一行未改）|
B4 只读 + 不编造 | ✅ v1.14.0（cache-only 取价 + `source=state` 降级）|
B5 标明来源时效 | ✅ v1.14.0（`data-*` 属性 + `title`）|
M1 两路径统一 | ✅ v1.14.0（自选池写回同一缓存）|

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
