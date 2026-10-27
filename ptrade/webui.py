"""ptrade 本地网页控制台的前端资源（W4）。

本模块只导出一个常量 :data:`APP_HTML` —— **单个自包含 HTML**（内联 CSS + JS、零外链），
由 W3 的 ``ptrade/webapp.py`` 在 ``GET /`` 直接返回，浏览器打开即用。

设计约束（来自 ``.omx/plans/prd-ptrade-web.md`` §5.4/§5.5 与 ``.omx/plans/test-spec-ptrade-web.md`` §5）:

* ★★ **v3 起：图表在「本页」渲染，不再用 iframe**（用户 2026-10-19 的明确要求）。
  - 图表容器是**:data:`APP_HTML` 里的一个 `<div id="chart-root">``**，ECharts 直接画在主页面上；
  - **ECharts 作为静态资源只加载一次**：``<script src="/vendor/echarts.min.js">``（本地 vendored
    1,030,855 B，**零 CDN**）；每次换图只取 ``/api/chart/data`` 的 **JSON**（~139 KB），
    而不是每张图重发 1.17 MB 的完整 HTML（这是「加载慢」的真解药）；
  - option 构建器**唯一真源**是 ``ptrade/webui_assets/chart_option.js``（ESM，经 ``/static/chart_option.js``
    原样返回）：K 线用 ``buildOption(payload, period, opts)``、分时用 ``buildIntradayOption(payload)``；
    ``ptrade/chart.py`` 的 HTML/CLI 路径**内联同一份文件** ⇒ 两条路径**按构造**同源（INV-65）；
  - 图表高度：**由左列网格的第二行决定**（``height:100%`` + ``min-height:0``）+ ``ResizeObserver``
    ⇒ 随视口真实伸缩、**无嵌套滚动条**；★ v4 起**不再有固定的高度上限**（那会让 1440p 图变矮）。
- ★ **没有前端开关**（INV-64）：前端**不拼接任何服务端开关参数**，取数模式**只由服务端 ``--offline`` 决定**。
  ★（写文档时也别写出「参数名+等号」那个字面量 —— ``test_t_v3_18`` 会扫**整个模块源**，它会把文档里的示例也当成违规。这是判据**有意**的粗粒度，不要为它放宽判据。）
* **零外链**：``<script src="/vendor/…">`` / ``<script type="module" src="/static/…">`` 都是
  **同源的本地静态路由**（不是外链）；**禁止**任何 ``http(s)://`` / ``//`` / CSS ``url(http…)``。
  ★ 注意：``chart.py`` 的 ``no_external_refs`` 判的是「**单文件自包含**」（相对路径也算外链），
  **那套判据只适用于 CLI 导出，不适用于本页** —— 见 ``INTERFACES.md`` **§8.4.1**（两套外链定义必须分写）。
* 体积 < 200 KB；中文界面；写操作（下单 / 平仓 / 结算）都有二次确认。
  ★ 「下单（预填表单）」**只预填、绝不直接提交**（一键下单 = 花真钱）。

响应契约（PRD §5.4）：所有 API 同构 ``{"ok": bool, "command": str, "data": {...}, "error": str|null}``；
HTTP 200 = 业务处理完成（**业务拒单也是 200**：``ok=true`` 且 ``data.orders[].status == "rejected"``，
必须展示 ``error_code`` 而不是报"请求失败"）、400 = 参数非法、503 = 写锁超时、500 = 内部错误。
"""

from __future__ import annotations

__all__ = ["APP_HTML"]

APP_HTML: str = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ptrade 模拟盘控制台</title>
<style>
:root{--bg:#0f1420;--card:#171e2e;--line:#26314a;--fg:#e6ecf5;--mut:#8fa0bd;--ok:#3ddc97;--warn:#ffb454;--err:#ff6b6b;--up:#ff5c5c;--down:#3ddc97;--acc:#5aa9ff}
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.35 "Microsoft YaHei","PingFang SC",system-ui,sans-serif;
     display:grid;grid-template-rows:auto 1fr;height:100%}
header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:6px 16px;border-bottom:1px solid var(--line);background:var(--card);z-index:9}
h1{font-size:15px;margin:0 12px 0 0}
h2{font-size:14px;margin:0 0 6px;color:var(--acc)}
/* ★ V4（用户拍板：图在左竖 / 卡片右列 / 零滚动）：
   两列网格 + **纵向 padding 归零**（padding:0 16px）—— 纵向 52px 会直接扣掉左列高度，
   1920×945 下 ratio 掉到 0.772（Critic 点名「不删就假红」）。 */
main{min-height:0;display:grid;overflow:hidden;gap:12px;padding:0 16px 8px;
     grid-template-columns:minmax(0,1.25fr) minmax(340px,1fr);
     grid-template-rows:minmax(0,1fr)}
main > .chart-col{min-height:0;display:grid;grid-template-rows:auto minmax(0,1fr);gap:8px}
main > .info-col{min-height:0;display:grid;gap:8px;align-content:start;
                 overflow-y:auto;scrollbar-width:thin;overscroll-behavior:contain}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;min-width:0}
.card-bar{display:flex;align-items:center;justify-content:space-between;gap:8px;
          background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
details.card > summary{cursor:pointer;color:var(--acc);font-size:13px;list-style:none}
details.card > summary::-webkit-details-marker{display:none}
details.card > summary::before{content:"▸ ";color:var(--mut)}
details.card[open] > summary::before{content:"▾ "}
details.card > summary .small{color:var(--mut);font-size:11px;margin-left:8px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.muted{color:var(--mut)}
.small{font-size:12px}
button{background:#22304c;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 10px;cursor:pointer;font:inherit}
button:hover{border-color:var(--acc)}
button.primary{background:#1d4ed8;border-color:#1d4ed8}
button.danger{background:#7f1d1d;border-color:#991b1b}
input,select{background:#0d1524;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font:inherit}
input:focus,select:focus{outline:1px solid var(--acc)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:500}
tbody tr:hover{background:#1d2739;cursor:pointer}
.k{color:var(--mut);margin-right:4px}
.v{font-weight:600;margin-right:12px}
.up{color:var(--up)}.down{color:var(--down)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px 12px}
#toast{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:6px;z-index:99;max-width:420px}
.toast-item{border:1px solid var(--line);border-left-width:4px;border-radius:6px;padding:8px 10px;background:#1b2437;box-shadow:0 4px 16px rgba(0,0,0,.4);font-size:13px}
.toast-ok{border-left-color:var(--ok)}.toast-warn{border-left-color:var(--warn)}.toast-err{border-left-color:var(--err)}
/* ★ V4：**删除**旧的高度上限（不是覆盖 —— 1440p 会卡在上限上 ⇒ 图变矮、ratio 必红）。
   现在高度由左列网格的第二行决定（`height:100%`），随视口真实伸缩。 */
/* ★ V5（用户拍板）：自选池**卡内固定高度 + 内部滚，不分页**（十几二十只也能看全）。
   行高用 CSS 变量 ⇒ 6.5 行可见，滚动条细、滚动不外溢（overscroll-behavior:contain）。 */
:root{--wl-row-h:24px}
#watchlist{height:calc(6.5 * var(--wl-row-h));max-height:calc(6.5 * var(--wl-row-h));overflow-y:auto;scrollbar-width:thin;overscroll-behavior:contain}
#watchlist tr{min-height:var(--wl-row-h)}
#watchlist th{position:sticky;top:0;background:var(--card);z-index:1}
#chart-root{width:100%;height:100%;min-height:0;border:1px solid var(--line);border-radius:6px;background:#0d1524}
.chart-period{cursor:pointer}
.chart-period.active{outline:2px solid #58a6ff}
/* ★ V4 紧凑化（把余量拉到 200+）：行距/字号/内边距一起压，**只紧凑、不隐藏数据** */
.small{font-size:11px}
th,td{padding:2px 6px}
.card{padding:8px}
@media (max-width:1500px){
  body{font-size:13px}
  .card{padding:7px 8px}
  h2{font-size:13px;margin:0 0 4px}
  th,td{padding:1px 5px}
}
.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 6px;font-size:11px;color:var(--mut);margin-left:4px}
.badge{display:inline-block;border-radius:4px;padding:1px 6px;font-size:12px}
.badge-buy{background:#14532d;color:#86efac}.badge-short{background:#7f1d1d;color:#fecaca}
.badge-watch{background:#3f3f46;color:#e4e4e7}.badge-avoid{background:#422006;color:#fed7aa}
.err-txt{color:var(--err)}.warn-txt{color:var(--warn)}
.empty{color:var(--mut);padding:6px 0}
/* 账实分叉**常驻**横幅（N7）：用常驻而不是 toast —— 分叉是需要用户持续看见的状态，
   toast 会自己消失，不适合承载「账实可能不一致」这种必须被处理的信息。 */
#divergence{display:none;margin:0;padding:10px 14px;border-left:5px solid var(--warn);
  background:#3a2c08;color:#ffe6b0;font-size:14px;line-height:1.5}
#divergence.is-diverged{border-left-color:var(--err);background:#3d1518;color:#ffd0d3}
#divergence.is-unknown{border-left-color:var(--mut);background:#252c3d;color:#cfd8e8}
#divergence code{background:rgba(0,0,0,.28);padding:1px 5px;border-radius:4px}
</style>
</head>
<body>
<header>
  <h1>ptrade 模拟盘控制台</h1>
  <button id="refresh-btn" title="重新拉取账户/持仓/风险/自选池">刷新全部</button>
  <span class="row">
    <input id="settle-date" size="10" placeholder="结算交易日 2025-10-10" title="T+1 结算目标交易日">
    <button id="settle-btn" class="primary" title="推进到新交易日：T+1 结算 / 计息 / 盯市 / 风险评估">结算</button>
  </span>
  <span id="health" class="small muted"></span>
  <div id="toast" role="status" aria-live="polite"></div>
</header>

<!-- N7：账实分叉横幅。放在 header 与 main 之间 —— 必须是**页面级**可见，
     不能藏在某个卡片里（分叉影响对**所有**数字的信任）。 -->
<div id="divergence" role="alert" aria-live="assertive"></div>

<main>
  <!-- ★ 左列：图（用户拍板「竖屏布局」= 图占左半、纵向撑满）；周期条在 auto 行、图在 1fr 行 -->
  <section class="chart-col">
    <div class="card-bar">
      <span class="row">
        <button id="period-daily" class="chart-period active" data-period="daily">日线</button>
        <button id="period-weekly" class="chart-period" data-period="weekly">周线</button>
        <button id="period-monthly" class="chart-period" data-period="monthly">月线</button>
      </span>
      <span class="row">
        <span id="last-updated" class="small muted">上次更新 —</span>
        <span class="small muted" data-testid="quote-freshness-hint">行情每 60s 更新 · 界面每 5s 刷新</span>
        <!-- ★ v6：**数据覆盖范围**的诚实标注 —— 让用户自己判断「这条线从哪天开始」。
             离线（夹具只有 2 年）时会明示「数据仅到 …（离线）」，否则用户会以为
             「从上市日算起」这个需求没修。 -->
        <span id="chart-coverage" class="small muted" data-testid="chart-coverage"></span>
        <span id="analysis-state" data-testid="analysis-state" class="small muted">分析：未加载</span>
      </span>
    </div>
    <div id="chart-root" data-testid="chart-root" class="chart-root">
      <div class="empty">在上方操作条输入代码后点「加载图表（日线）」</div>
    </div>
  </section>

  <!-- ★ 右列：信息卡（操作条/账户/风险/自选池/持仓 = 默认展开；打分/分析 = 可折叠） -->
  <section class="info-col">
    <section class="card" id="action-bar" data-testid="action-bar">
      <div class="row">
        <input id="stock-code" data-testid="stock-code-input" size="14" placeholder="600519 或 茅台" autocomplete="off">
        <button id="act-search" data-testid="search">搜索</button>
        <button id="act-chart-daily" data-testid="chart-daily" class="primary">加载图表（日线）</button>
        <button id="act-chart-minute" data-testid="chart-minute">分时</button>
        <button id="act-watchlist-add" data-testid="watchlist-add">加自选</button>
        <button id="act-order" data-testid="order">下单（预填表单）</button>
      </div>
      <div id="search-results" class="small"><span class="empty">输入代码或名称后按回车搜索</span></div>
    </section>

    <section class="card">
      <h2>账户状态</h2>
      <div id="state-panel" class="grid2"><span class="empty">加载中…</span></div>
    </section>

    <section class="card">
      <h2>风险</h2>
      <div id="risk"><span class="empty">加载中…</span></div>
    </section>

    <section class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">自选池</h2>
        <span class="row">
          <button id="watchlist-refresh-btn">刷新</button>
          <button id="score-watchlist-btn" class="primary" title="对自选池全部标的批量七因子打分（缺数据标注「无数据」，不记 0 分）">批量打分</button>
        </span>
      </div>
      <div id="watchlist"><span class="empty">加载中…</span></div>
    </section>

    <section class="card">
      <h2>持仓</h2>
      <div id="positions"><span class="empty">加载中…</span></div>
    </section>

    <section class="card">
      <h2>下单 / 平仓</h2>
      <form id="order-form" data-testid="order-form" autocomplete="off" onsubmit="return false">
        <div class="grid2">
          <label class="small">代码 <input id="order-code" size="8" placeholder="代码" required></label>
          <label class="small">方向
            <select id="order-side">
              <option value="buy">买入</option>
              <option value="sell">卖出</option>
              <option value="margin_buy">融资买入</option>
              <option value="short_sell">融券卖出</option>
              <option value="buy_to_cover">买券还券</option>
              <option value="repay_margin">卖券还款</option>
            </select>
          </label>
          <label class="small">数量 <input id="order-qty" size="6" placeholder="100" required></label>
          <label class="small">价格 <input id="order-price" size="6" placeholder="市价"></label>
          <label class="small">交易日 <input id="order-date" size="10" placeholder="当前"></label>
          <label class="small">平仓比例 <input id="close-ratio" size="4" placeholder="1.0"></label>
        </div>
        <div class="row" style="margin-top:6px">
          <button id="order-submit-btn" class="primary" type="button">提交下单</button>
          <button id="close-submit-btn" class="danger" type="button">平仓</button>
        </div>
      </form>
    </section>

    <!-- ★ V5（用户拍板）：**打分 + CANSLIM 分析合并成一张卡**，顺序冻结：
         总分 → 七因子 → 逐条解释 → 风险 → 建议。五段用 `data-section` 标识（判据按属性取，不靠文本猜）。
         默认折叠（保 v4 的四档零滚动）。 -->
    <details class="card" id="analysis-details" data-collapse="analysis">
      <summary>打分 / CANSLIM 分析（七因子） <span class="small" id="analysis-summary">未加载</span></summary>
      <div data-testid="analysis-panel" id="analysis-panel" class="small">
        <div data-section="total" class="empty">未加载</div>
        <div data-section="factors"></div>
        <div data-section="reasons"></div>
        <div data-section="risks"></div>
        <div data-section="advice" class="empty">未加载</div>
        <div id="score-table"><span class="empty">尚未打分</span></div>
      </div>
    </details>
  </section>
</main>

<!-- ★ V3-A5：ECharts **本地 vendored**（1,030,855 B），**只在本页加载一次**；零 CDN（INV-59 同族）。
     静态资源由服务端 `/vendor/echarts.min.js` 提供（白名单 + Cache-Control: public, max-age=86400）。 -->
<script src="/vendor/echarts.min.js"></script>
<script type="module">
/* ★ V3-W1（task-36）：K 线 option 的**唯一真源**是 `chart_option.js`（ESM，服务端原样返回）。
   本模块只把它挂到 `window` 上供经典脚本调用 —— **绝不在这里写第二份 option 构建器**
   （INV-65 / `T-V3-19` 会扫全仓断言「只有一份」）。 */
import { buildOption, buildIntradayOption, miniCandlestick } from "/static/chart_option.js";
window.buildOption = buildOption;
window.buildIntradayOption = buildIntradayOption;
window.miniCandlestick = miniCandlestick;
window.dispatchEvent(new Event("chart-option-ready"));
</script>
<script>
"use strict";
var API = {
  health: "/api/health",
  state: "/api/state",
  positions: "/api/positions",
  risk: "/api/risk",
  search: "/api/search",
  watchlist: "/api/watchlist",
  watchlistScore: "/api/watchlist/score",
  score: "/api/score",
  order: "/api/order",
  close: "/api/close",
  settle: "/api/settle",
  chart: "/api/chart",
  chartData: "/api/chart/data",
  quote: "/api/quote",
  nameRefresh: "/api/name/refresh",
  analysis: "/api/analysis"
};
/* ★ V3-A5：图表在**本页**直接渲染（无嵌套框架）。`CHART` 是 ECharts 实例，
   `CHART_STATE` 是当前数据（`/api/chart/data` 的 JSON），`CHART_RO` 是高度自适应观察器。 */
var CHART = null;
var CHART_RO = null;
var CHART_MODULE_TIMEOUT = null;   /* ★ M2：图表模块加载超时兜底（见 renderChart） */
var CHART_STATE = {code: "", period: "daily", payload: null};
var FACTOR_KEYS = ["C", "A", "N", "S", "L", "I", "M"];

function $(id) { return document.getElementById(id); }
function esc(v) {
  return String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function isNum(v) { return typeof v === "number" && isFinite(v); }
function num(v, digits) {
  if (!isNum(v)) { return v === null || v === undefined || v === "" ? "—" : esc(v); }
  return v.toLocaleString("zh-CN", { minimumFractionDigits: digits === undefined ? 2 : digits, maximumFractionDigits: digits === undefined ? 2 : digits });
}
function pct(v) { return isNum(v) ? (v * 100).toFixed(2) + "%" : "—"; }

function toast(msg, kind) {
  var box = $("toast");
  if (!box || !msg) { return; }
  var el = document.createElement("div");
  el.className = "toast-item toast-" + (kind || "ok");
  el.textContent = String(msg);
  box.appendChild(el);
  setTimeout(function () { if (el.parentNode) { el.parentNode.removeChild(el); } }, kind === "err" ? 9000 : 5000);
}

/* 写操作二次确认：下单 / 平仓 / 结算 全部经过这里。 */
function confirmWrite(label) {
  return window.confirm("确认" + label + "？该操作会写入账户状态与成交流水（ledger），不可撤销。");
}

/* 统一响应处理：200 业务拒单不算失败；400 参数非法；503 写锁超时；其余按错误展示。 */
function api(path, opts) {
  return fetch(path, opts || {}).then(function (resp) {
    if (resp.status === 503) {
      toast("另一个写操作正在进行（锁超时），请稍后重试", "warn");
      return null;
    }
    return resp.text().then(function (text) {
      var body = null;
      try { body = JSON.parse(text); } catch (e) { body = null; }
      if (resp.status === 400) {
        toast("参数非法：" + ((body && body.error) || "HTTP 400"), "err");
        return null;
      }
      if (!body || body.ok !== true) {
        var err = (body && body.error) || ("HTTP " + resp.status);
        toast("请求失败：" + err, "err");
        return null;
      }
      var orders = (body.data && body.data.orders) || [];
      var shown = false;
      orders.forEach(function (o) {
        if (o && o.status === "rejected") {
          shown = true;
          toast("业务拒单：" + esc(o.code) + " " + esc(o.side) + " → " + esc(o.error_code || "REJECTED")
            + (o.reason ? "（" + esc(o.reason) + "）" : ""), "warn");
        }
      });
      orders.forEach(function (o) {
        if (o && o.status === "filled" && !shown) {
          toast("已成交：" + esc(o.code) + " " + esc(o.side) + " " + esc(o.qty) + " @ " + num(o.price), "ok");
        }
      });
      return body;
    });
  }, function (e) {
    toast("无法连接后端：" + (e && e.message ? e.message : e), "err");
    return null;
  });
}

function post(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
}

/* data 可能是 list / {results:[…]} / {items:[…]} / {positions:{code:…}} —— 统一成数组。 */
function asRows(data, keys) {
  if (Array.isArray(data)) { return data; }
  if (!data || typeof data !== "object") { return []; }
  var names = keys || ["results", "items", "rows", "positions", "list", "stocks", "data"];
  for (var i = 0; i < names.length; i++) {
    var v = data[names[i]];
    if (Array.isArray(v)) { return v; }
    if (v && typeof v === "object") {
      return Object.keys(v).map(function (code) {
        var row = v[code] && typeof v[code] === "object" ? v[code] : { value: v[code] };
        if (!row.code) { row.code = code; }
        return row;
      });
    }
  }
  return [];
}
function factorRows(row) {
  var f = row && row.factors;
  if (Array.isArray(f)) { return f; }
  if (f && typeof f === "object") {
    return FACTOR_KEYS.filter(function (k) { return k in f; }).map(function (k) { return { key: k, score: f[k] }; });
  }
  var out = [];
  FACTOR_KEYS.forEach(function (k) { if (row && (k in row)) { out.push({ key: k, score: row[k] }); } });
  return out;
}
function signalBadge(sig) {
  var s = String(sig || "").toLowerCase();
  var cls = s === "buy" ? "badge-buy" : (s === "short" ? "badge-short" : (s === "watch" ? "badge-watch" : "badge-avoid"));
  var label = { buy: "买入", short: "做空", watch: "观察", avoid: "回避" }[s] || (sig || "—");
  return '<span class="badge ' + cls + '">' + esc(label) + "</span>";
}
function table(headers, rowsHtml) {
  if (!rowsHtml.length) { return '<div class="empty">（空）</div>'; }
  return "<table><thead><tr>" + headers.map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("")
    + "</tr></thead><tbody>" + rowsHtml.join("") + "</tbody></table>";
}
function pick(obj, names) {
  for (var i = 0; i < names.length; i++) {
    if (obj && obj[names[i]] !== undefined && obj[names[i]] !== null) { return obj[names[i]]; }
  }
  return null;
}

function renderDivergence(d) {
  // N7：把「账实是否分叉」渲染成**页面级常驻横幅**。
  // 三态，**不得**把「不知道」当成「没问题」：
  //   diverged === true  → 红色：账实不一致，重试可能重复成交
  //   diverged === null  → 灰色：无法判定（拿不到写锁 / 一侧不可读）
  //   diverged === false → 隐藏
  var el = $("divergence");
  if (!el) { return; }
  var lc = (d && d.ledger_consistency) || null;
  var diverged = lc ? lc.diverged : undefined;
  if (diverged === false) {
    el.style.display = "none";
    el.className = "";
    el.innerHTML = "";
    return;
  }
  if (diverged === true) {
    el.className = "is-diverged";
    el.innerHTML = "<strong>⚠️ 账实分叉（ledger 有单、state 无单）</strong>："
      + "台账 <code>" + esc(lc.ledger_lines) + "</code> 行，而 state 水位记的是 <code>"
      + esc(lc.recorded) + "</code>。"
      + "说明某次写单只落了台账、没落账户 —— <strong>此时重试同一笔下单可能重复成交</strong>。"
      + "请先查 <code>ledger.jsonl</code> 末尾与 <code>status</code> 持仓，再决定如何修复"
      + "（不要直接重试下单）。";
    el.style.display = "block";
    return;
  }
  // diverged === null 或 undefined（缺字段 / 不可判定）
  el.className = "is-unknown";
  el.innerHTML = "<strong>账实一致性：无法判定</strong> —— "
    + esc((lc && lc.reason) || "响应里没有 ledger_consistency 字段")
    + "。这不是「没问题」，只是**没查成**（通常是正有写事务在跑）；稍后刷新再看。";
  el.style.display = "block";
}

function renderState(data) {
  var d = (data && data.state) || data || {};
  renderDivergence(d);
  var ratio = pick(d, ["maintenance_ratio"]);
  var items = [
    ["交易日", d.trade_date], ["可用资金", num(d.available_cash)], ["总资产", num(d.total_assets)],
    ["净资产", num(d.net_assets)], ["多头市值", num(d.long_market_value)], ["融券市值", num(d.short_market_value)],
    ["融资负债", num(d.financing_debt)], ["冻结(T+1)", num(d.frozen_t1)], ["冻结(融券券款)", num(d.frozen_collateral)],
    ["应计利息", num(d.interest_accrued)], ["维持担保比例", ratio === null ? "—（无负债）" : num(ratio, 4)],
    ["总盈亏", num(d.total_pnl)]
  ];
  $("state-panel").innerHTML = items.map(function (kv) {
    return '<div><span class="k">' + esc(kv[0]) + '</span><span class="v">' + (kv[1] === undefined || kv[1] === null ? "—" : (typeof kv[1] === "string" ? esc(kv[1]) : kv[1])) + "</span></div>";
  }).join("");
}

function renderPositions(data) {
  var rows = asRows(data && (data.positions || data), ["positions", "results", "items", "rows"]);
  var html = rows.map(function (p) {
    var qty = pick(p, ["long_qty", "qty", "quantity"]) || 0;
    var val = pick(p, ["long_value", "market_value", "value"]);
    var cost = pick(p, ["long_cost", "cost_price", "cost"]);
    var pnl = pick(p, ["long_pnl", "pnl", "float_pnl"]);
    var cls = isNum(pnl) ? (pnl >= 0 ? "up" : "down") : "";
    return '<tr data-code="' + esc(p.code) + '"><td>' + esc(p.code) + " " + esc(p.name || "")
      + '</td><td>' + esc(qty) + "</td><td>" + num(cost) + "</td><td>" + num(val)
      + '</td><td class="' + cls + '">' + num(pnl) + "</td></tr>";
  });
  $("positions").innerHTML = table(["代码/名称", "多头数量", "成本", "市值", "浮盈"], html);
}

function renderRisk(data) {
  var d = (data && data.risk) || data || {};
  var ratio = pick(d, ["maintenance_ratio"]);
  var status = d.status || "—";
  var statusCls = status === "safe" ? "down" : (status === "warn" ? "warn-txt" : "err-txt");
  var alerts = d.alerts || [];
  var exposure = d.exposure || {};
  var head = '<div class="row"><span class="k">状态</span><span class="v ' + statusCls + '">' + esc(status)
    + '</span><span class="k">维持担保比例</span><span class="v">' + (ratio === null ? "—（无负债，字段为 null）" : num(ratio, 4))
    + '</span><span class="k">杠杆</span><span class="v">' + (exposure.leverage === null || exposure.leverage === undefined ? "—" : num(exposure.leverage, 4))
    + "</span></div>";
  var alertHtml = alerts.length
    ? '<ul class="small">' + alerts.map(function (a) {
        var lvl = (a && a.level) || "";
        var cls = lvl === "critical" ? "err-txt" : "warn-txt";
        return '<li class="' + cls + '">' + esc((a && a.code) || "") + " " + esc((a && a.message) || a) + "</li>";
      }).join("") + "</ul>"
    : '<div class="small muted">无告警</div>';
  $("risk").innerHTML = head + alertHtml;
}

function renderSearch(data) {
  var rows = asRows(data && (data.results || data), ["results", "items", "rows", "stocks"]);
  if (!rows.length) { $("search-results").innerHTML = '<div class="empty">无命中（或数据源不可用，请看提示）</div>'; return; }
  $("search-results").innerHTML = table(["代码/名称", "两融", "操作"], rows.map(function (r) {
    var tags = (r.is_margin ? '<span class="tag">融资</span>' : "") + (r.is_short ? '<span class="tag">融券</span>' : "");
    return '<tr><td>' + esc(r.code) + " " + esc(r.name || "") + "</td><td>" + tags + "</td><td>"
      + '<button type="button" data-act="chart" data-code="' + esc(r.code) + '">看图</button> '
      + '<button type="button" data-act="minute" data-code="' + esc(r.code) + '">分时</button> '
      + '<button type="button" data-act="add" data-code="' + esc(r.code) + '" data-name="' + esc(r.name || "") + '">加自选</button> '
      + '<button type="button" data-act="order" data-code="' + esc(r.code) + '">下单（预填表单）</button>'
      + "</td></tr>";
  }));
}

var WL_CODES = "";
function renderWatchlist(data) {
  var rows = asRows(data && (data.items || data), ["items", "results", "rows"]);
  var box = $("watchlist");
  if (!box) { return; }
  if (!rows.length) {
    box.innerHTML = '<div class="empty">自选池为空：用上方操作条输入代码后点「加自选」</div>';
    WL_CODES = "";
    return;
  }
  var keepTop = box.scrollTop || 0;          /* ★ V5：整表重建时也要保住滚动位置 */
  WL_CODES = rows.map(function (r) { return String(r.code); }).join(",");
  box.innerHTML = table(["代码 / 名称", "现价", "涨跌幅", "操作"], rows.map(function (r) {
    var q = QUOTE_MAP[String(r.code)] || {};
    var last = (q.last === null || q.last === undefined) ? "—" : num(q.last, 2);
    var pct = (q.pct_chg === null || q.pct_chg === undefined) ? "—" : (num(q.pct_chg, 2) + "%");
    var pctCls = (q.pct_chg > 0) ? "warn-txt" : (q.pct_chg < 0 ? "down-txt" : "");
    var name = r.name || q.name || "";
    return '<tr data-code="' + esc(r.code) + '"><td>' + esc(r.code) + " " + esc(name) + "</td>"
      + '<td data-role="last">' + last + "</td>"
      + '<td data-role="pct" class="' + pctCls + '">' + pct + "</td><td>"
      + '<button type="button" data-act="chart" data-code="' + esc(r.code) + '">看图</button> '
      + '<button type="button" data-act="minute" data-code="' + esc(r.code) + '">分时</button> '
      + '<button type="button" class="danger" data-act="del" data-code="' + esc(r.code) + '">移除</button>'
      + "</td></tr>";
  }));
  box.scrollTop = keepTop;
}
/* ★★ V5：5 秒刷新**只改数值单元格**（不整体重渲染 ⇒ 列表不跳回顶部、不打断用户操作） */
function updateWatchlistQuotes() {
  var box = $("watchlist");
  if (!box || !box.querySelectorAll) { return; }
  var trs = box.querySelectorAll("tr[data-code]");
  for (var i = 0; i < trs.length; i++) {
    var code = String(trs[i].getAttribute("data-code"));
    var q = QUOTE_MAP[code] || {};
    var lastTd = trs[i].querySelector('[data-role="last"]');
    var pctTd = trs[i].querySelector('[data-role="pct"]');
    if (lastTd) {
      lastTd.textContent = (q.last === null || q.last === undefined) ? "—" : num(q.last, 2);
    }
    if (pctTd) {
      pctTd.textContent = (q.pct_chg === null || q.pct_chg === undefined)
        ? "—" : (num(q.pct_chg, 2) + "%");
      pctTd.className = (q.pct_chg > 0) ? "warn-txt" : (q.pct_chg < 0 ? "down-txt" : "");
    }
  }
}
var QUOTE_MAP = {};
/* ★ V3-A2：自选池每行要有 **名称 / 现价 / 涨跌幅** —— 一次 `/api/quote` 批量取（N 只票 1 次取数）。 */
function refreshWatchlist() {
  return api(API.watchlist).then(function (b) {
    if (!b) { return; }
    var rows = asRows(b.data && (b.data.items || b.data), ["items", "results", "rows"]);
    var codes = rows.map(function (r) { return String(r.code); });
    var sameSet = (codes.join(",") === WL_CODES);     /* 首次/增删 ⇒ 集合变 ⇒ 整表重建 */
    if (!sameSet) { renderWatchlist(b.data); }
    if (!codes.length) { return; }
    return api(API.quote + "?codes=" + encodeURIComponent(codes.join(","))).then(function (q) {
      QUOTE_MAP = {};
      asRows(q && q.data && q.data.items, ["items"]).forEach(function (item) {
        QUOTE_MAP[String(item.code)] = item;
      });
      /* ★★ V5：同一批代码的 5s 刷新 ⇒ **只改数值 td**（不重建 ⇒ scrollTop 不跳、点击不丢） */
      if (sameSet) { updateWatchlistQuotes(); } else { renderWatchlist(b.data); }
    });
  });
}

function renderScores(data) {
  var rows = asRows(data && (data.results || data), ["results", "items", "rows"]);
  var summary = $("score-summary");
  if (summary) {
    /* ★ V4：打分默认折叠 ⇒ summary 必须带关键数字，否则折叠后用户什么也看不到 */
    var best = null;
    rows.forEach(function (r) {
      if (r && r.total !== undefined && r.total !== null) {
        best = (best === null || Number(r.total) > best) ? Number(r.total) : best;
      }
    });
    summary.textContent = rows.length
      ? ("已打分 " + rows.length + " 只" + (best === null ? "" : " · 最高 " + num(best, 2)))
      : "尚未打分";
  }
  if (!rows.length) { $("score-table").innerHTML = '<div class="empty">无打分结果</div>'; return; }
  var headers = ["代码/名称", "信号", "总分"].concat(FACTOR_KEYS);
  var html = rows.map(function (r) {
    var f = {};
    factorRows(r).forEach(function (x) { f[x.key] = x.score; });
    var cells = FACTOR_KEYS.map(function (k) {
      if (r.risks && String(r.risks).indexOf(k + "数据缺失") >= 0) { return '<td class="warn-txt" title="无数据">无数据</td>'; }
      return "<td>" + (f[k] === undefined || f[k] === null ? "—" : num(f[k], 1)) + "</td>";
    }).join("");
    var missing = (r.risks && r.risks.length) ? ' <span class="tag" title="' + esc(r.risks.join(" / ")) + '">风险</span>' : "";
    return '<tr data-code="' + esc(r.code) + '"><td>' + esc(r.code) + " " + esc(r.name || "") + missing + "</td><td>"
      + signalBadge(r.signal) + "</td><td>" + num(r.total, 2) + "</td>" + cells + "</tr>";
  });
  $("score-table").innerHTML = table(headers, html);
}

/* ---- V3-A5：**同页** ECharts（去掉整页嵌入的旧做法）----
 * 数据走 `/api/chart/data`（JSON）；库由 `/vendor/echarts.min.js` 在**本页加载一次**（零 CDN）；
 * option 由 `chart_option.js` 的 `buildOption(payload, period, opts)` 统一构建（**INV-65 单一真源**）。
 * 高度自适应：`ResizeObserver` 观察容器（修「图表被固定高度框住」的割裂感）。
 */
function currentCode() {
  var el = $("stock-code");
  return (el && el.value ? String(el.value) : "").trim();
}
function setCurrentCode(code) {
  var el = $("stock-code");
  if (el && code) { el.value = String(code); }
}
function chartEmpty(text) {
  var root = $("chart-root");
  if (root) { root.innerHTML = '<div class="empty">' + esc(text || "") + "</div>"; }
  CHART_STATE.payload = null;
}
function ensureChart() {
  var root = $("chart-root");
  if (!root) { return null; }
  if (!window.echarts) { chartEmpty("图表库未加载（/vendor/echarts.min.js 不可用）"); return null; }
  if (!CHART) {
    /* ★ V4：首帧容器可能还是 0 高（CSS 网格尚未布局完）—— 此时 init 会得到 0×0 画布。
       下一帧再试一次，只有**真的量到 0** 才重试（判定用 `=== 0`：非浏览器环境可能量不到值，
       那时不该把 `undefined` 当成「高度为 0」而永远不画）。 */
    if (root.clientHeight === 0) {
      if (window.requestAnimationFrame) {
        window.requestAnimationFrame(function () { renderChart(); });
      }
      return null;
    }
    root.innerHTML = "";
    CHART = window.echarts.init(root, null, {renderer: "canvas"});
    if (window.ResizeObserver) {
      CHART_RO = new ResizeObserver(function () {
        var w = root.clientWidth, h = root.clientHeight;
        if (CHART && w && h) { CHART.resize(); }   /* ★ 0 尺寸不 resize（否则画布被压成 0） */
      });
      CHART_RO.observe(root);
    } else {                                   /* 老浏览器兜底：窗口 resize 也能自适应 */
      window.addEventListener("resize", function () { if (CHART) { CHART.resize(); } });
    }
  }
  return CHART;
}
/* ★ v6：**数据覆盖范围**的诚实标注。
   * 为什么需要：`--offline` 只有 2 年夹具 ⇒ 图会"从 2024 开始" ⇒ 用户会以为
   * 「从上市日算起」这个需求**没修**。所以把 `payload.coverage` 如实显示出来：
   *   在线：`数据从 2001-08-27 开始`
   *   离线：`数据仅到 2026-10-14（离线）`   ← 明确告诉用户这是离线夹具的边界
   * ★ `coverage` 只给 `{first, last, requested_start}`，**没有 `complete` 布尔** ——
   *   因为从 `daily()` 的返回无法区分「这只票上市就晚」与「取数被截断」，给假布尔更糟。 */
function renderCoverage(payload) {
  var el = $("chart-coverage");
  if (!el) { return; }
  var cov = (payload && payload.coverage) || {};
  var first = cov.first, last = cov.last;
  if (!first || !last) { el.textContent = ""; return; }
  el.textContent = isOffline()
    ? "数据仅到 " + String(last).slice(0, 10) + "（离线）"
    : "数据从 " + String(first).slice(0, 10) + " 开始";
  el.title = "请求窗口自 " + String(cov.requested_start || "—") + "；实际首根 " + String(first).slice(0, 10)
    + "（该票上市日或数据源边界）";
}
function renderChart() {
  var st = CHART_STATE;
  if (!st.payload) { return; }
  renderCoverage(st.payload);
  if (typeof window.buildOption !== "function") {
    /* 模块脚本是 defer 的：首次可能还没就绪 —— 等事件再画，而不是报错/降级成空白。 */
    chartEmpty("图表模块加载中…");
    var onOptionReady = function () {
      if (CHART_MODULE_TIMEOUT) { clearTimeout(CHART_MODULE_TIMEOUT); CHART_MODULE_TIMEOUT = null; }
      renderChart();
    };
    window.addEventListener("chart-option-ready", onOptionReady, {once: true});
    /* ★★ M2（code-review，MEDIUM）：`/static/chart_option.js` 若 **404 / 语法错 / 被拦**，
       ESM 永不 dispatch `chart-option-ready` ⇒ 页面会**永久停在「图表模块加载中…」**，
       用户完全不知道发生了什么（比报错更糟）。
       ⇒ 4s 超时兜底：显示**可执行**的错误 + 摘掉那个一次性监听（避免它将来突然触发一次「假成功」重绘）。
       ★ 只在**尚未**设过超时时设（重复调用 renderChart 不应叠加定时器）。 */
    if (!CHART_MODULE_TIMEOUT) {
      CHART_MODULE_TIMEOUT = setTimeout(function () {
        if (typeof window.buildOption === "function") { return; }
        window.removeEventListener("chart-option-ready", onOptionReady);
        chartEmpty("图表模块加载失败：/static/chart_option.js 不可用（刷新页面重试；仍失败请查看服务端日志）");
      }, 4000);
    }
    return;
  }
  if (CHART_MODULE_TIMEOUT) { clearTimeout(CHART_MODULE_TIMEOUT); CHART_MODULE_TIMEOUT = null; }
  var inst = ensureChart();
  if (!inst) { return; }
  var opts = {code: st.code, getActive: function () { return st.period; }};
  if (typeof window.miniCandlestick === "function") {
    /* ★ **必须传**：BOLL/SAR 面板的**迷你蜡烛**由它在运行时生成
       （axisIndex 1=BOLL、5=SAR）。不传就**静默没有蜡烛**（Critic C3 的坑）。 */
    opts.miniCandlestick = {boll: window.miniCandlestick(1), sar: window.miniCandlestick(5)};
  }
  /* ★★ X2（真缺陷）：**分时必须走它自己的构建器**。
     分时 payload（`render_intraday` 的产物）**没有 `periods` 字段**，而 `buildOption` 会读
     `payload.periods[period]` ⇒ 直接 `TypeError: Cannot read properties of undefined (reading 'minute')`
     ⇒ 点「分时」抛异常、图表**停在日线**上（我 CDP 实测到的原症状）。
     ⇒ 按周期分派：minute → `buildIntradayOption(payload)`（不吃 opts）；其余 → `buildOption(payload, period, opts)`。
     两个构建器都在**同一份** `chart_option.js` 里 ⇒ 每个图型仍是**单一真源**（`T-V3-19`/`T-V3-27` 守着）。 */
  var option;
  if (st.period === "minute") {
    if (typeof window.buildIntradayOption !== "function") {
      chartEmpty("分时图表模块未就绪（/static/chart_option.js 缺少 buildIntradayOption）");
      return;
    }
    option = window.buildIntradayOption(st.payload);
  } else {
    option = window.buildOption(st.payload, st.period, opts);
  }
  if (!option) { chartEmpty("图表数据为空"); return; }
  inst.setOption(option, true);
  inst.resize();
}
function setPeriodButtons(p) {
  var btns = document.querySelectorAll(".chart-period");
  for (var i = 0; i < btns.length; i++) {
    var on = btns[i].getAttribute("data-period") === p;
    btns[i].className = "chart-period" + (on ? " active" : "");
  }
}
/* ---- V5：分析卡（打分 + CANSLIM **合并**）--------------------------------------------
 * ★★ **唯一写入口** `applyAnalysis(state, payload)`：一次同时写
 *    `#analysis-state` / `#analysis-summary` / `#analysis-panel`（body）。
 *    ⇒ 修掉 v4 的 **summary 双写** bug：`setAnalysisState` 写 summary、`renderAnalysis` 又写一次，
 *      而 pending 只动 summary、不动 body ⇒「summary 说计算中、body 还是上一只股」。
 * ★ `analysisView` 是**纯函数**（state+payload → {state, summary, bodyHtml}），判据可直接调用。
 * ★ 四种空态：未加载 / 无数据 / **离线不可用**（绝不得显示「计算中」）/ 失败。
 */
function analysisView(state, payload) {
  var p = payload || {};
  var st = state || p.state || "idle";
  var summary = "未加载";
  var bodyHtml = '<div class="empty">未加载：在操作条输入代码后点「加载图表（日线）」</div>';
  if (st === "pending") {
    var secs = (p.elapsed_s === null || p.elapsed_s === undefined) ? "—" : p.elapsed_s;
    summary = "已算 " + secs + " 秒…";
    bodyHtml = '<div class="empty">分析计算中（已算 ' + esc(String(secs)) + ' 秒）…</div>';
    if (p.give_up) {
      summary = "仍在计算（已放弃自动刷新）";
      bodyHtml = '<div class="empty">仍在后台计算… '
        + '<button type="button" data-act="analysis-retry">仍在计算，点击重试</button></div>';
    }
  } else if (st === "offline") {
    summary = "分析不可用（离线模式）";
    bodyHtml = '<div class="empty warn-txt">分析不可用（离线模式）：七因子需要联网取数，'
      + '离线模式下该功能被禁用 —— **不是**「计算中」，永远不会完成。</div>';
  } else if (st === "no_data") {
    summary = "该标的无数据";
    bodyHtml = '<div class="empty">该标的无数据：缺少可用的行情/财务缓存，暂无法打分。</div>';
  } else if (st === "minute") {
    summary = "分时图无七因子分析";
    bodyHtml = '<div class="empty">分时图不提供 CANSLIM 七因子分析（只对日线/周线/月线计算）。</div>';
  } else if (st === "failed") {
    summary = "分析失败：" + String(p.error || "未知原因");
    bodyHtml = '<div class="empty err-txt">分析失败：' + esc(String(p.error || "未知原因")) + "</div>";
  } else if (st === "ready") {
    summary = "总分 " + num(p.total, 2) + " · " + String(p.signal || "—") + " · " + (p.reasons || []).length + " 条解释";
    bodyHtml = analysisReadyHtml(p);
  }
  return {state: st, summary: summary, bodyHtml: bodyHtml};
}
/* ★ 五段**冻结契约**：data-section = total / factors / reasons / risks / advice（判据按属性取） */
function analysisSection(name, inner) { return '<div data-section="' + name + '">' + inner + "</div>"; }
function analysisReadyHtml(p) {
  var f = p.factors || {};
  var adv = p.recommend || {};
  var total = '<b>总分 ' + num(p.total, 2) + "</b> · 信号 " + signalBadge(p.signal)
    + (adv.action ? " · 操作 <b>" + esc(String(adv.action)) + "</b>" : "")
    + (adv.confidence === undefined || adv.confidence === null ? "" : " · 置信度 " + num(adv.confidence, 2));
  var factors = FACTOR_KEYS.map(function (k) {
    return '<span class="tag">' + k + " <b>"
      + (f[k] === undefined || f[k] === null ? "—" : num(f[k], 0)) + "</b></span>";
  }).join(" ");
  var reasons = (p.reasons || []).map(function (t) { return "<li>" + esc(String(t)) + "</li>"; }).join("");
  var risks = (p.risks || []).map(function (t) { return "<li>" + esc(String(t)) + "</li>"; }).join("");
  /* ★ V5 修复：字段是 `position_pct`（0..1，来自 `strategy.recommend()`），不是 `position`。
     `recommend` 是**透传自分析缓存**的（`webapp.analysis_status` 的 `data["recommend"]`），
     所以**字段名必须与 `strategy.recommend()` 的返回键逐字对齐** —— 否则仓位永远显示 `—`（实测踩过）。 */
  var positionText = null;
  if (adv.position_pct !== undefined && adv.position_pct !== null) {
    var pp = Number(adv.position_pct);
    positionText = (isFinite(pp) && pp > 0) ? (Math.round(pp * 1000) / 10) + "%" : "—";
  }
  var advicePairs = [["入场", adv.entry], ["止损", adv.stop_loss], ["止盈", adv.take_profit],
                     ["仓位", positionText]];
  var advice = advicePairs.map(function (pair) {
    var v = pair[1];
    return "<span class='k'>" + pair[0] + "</span>"
      + (v === undefined || v === null || v === "" ? "—" : esc(String(v))) + " ";
  }).join(" · ");
  return analysisSection("total", total)
    + analysisSection("factors", factors)
    + analysisSection("reasons", "<h2>逐因子解释（" + (p.reasons || []).length + " 条）</h2>"
      + (reasons ? "<ul>" + reasons + "</ul>" : '<div class="empty">无逐因子解释</div>'))
    + analysisSection("risks", "<h2>风险</h2>"
      + (risks ? "<ul>" + risks + "</ul>" : '<div class="empty">无风险提示</div>'))
    + analysisSection("advice", "<h2>交易建议</h2><div>" + (advice || "—") + "</div>");
}
function applyAnalysis(state, payload) {
  var view = analysisView(state, payload);
  var st = $("analysis-state"), su = $("analysis-summary"), pan = $("analysis-panel");
  if (st) { st.setAttribute("data-state", view.state); st.textContent = "分析：" + view.summary; }
  if (su) { su.textContent = view.summary; }          /* ★ summary 的唯一写点 */
  if (pan) { pan.innerHTML = view.bodyHtml; }         /* ★ body 的唯一写点 */
  var retry = (pan && pan.querySelector) ? pan.querySelector('[data-act="analysis-retry"]') : null;
  if (retry) {
    retry.addEventListener("click", function () {
      ANALYSIS.done = false; ANALYSIS.n = 0;
      applyAnalysis("pending", {elapsed_s: null});
      scheduleAnalysisPoll(ANALYSIS.code || currentCode());
    });
  }
  return view;
}
function loadChart(period) {
  var code = currentCode();
  if (!code) { toast("请先在操作条里填写代码", "warn"); return; }
  var p = period || CHART_STATE.period || "daily";
  CHART_STATE.period = p;
  setPeriodButtons(p);
  var url = API.chartData + "?code=" + encodeURIComponent(code) + "&period=" + encodeURIComponent(p);
  api(url).then(function (b) {
    var d = (b && b.data) || null;
    if (!d) { chartEmpty("加载失败：后端不可达"); return; }
    if (d.ok === false || d.error) { chartEmpty("无数据：" + (d.error || "")); return; }
    CHART_STATE.code = code;
    CHART_STATE.payload = d;
    renderChart();
    if (p === "minute") {
      stopAnalysisPoll();
      applyAnalysis("minute", {});
    } else if (isOffline()) {
      /* ★★ V5：`--offline` 下七因子永远算不完 ⇒ **必须**显示「分析不可用（离线模式）」，
         绝不得显示「计算中…（后台）」（那是欺骗）；也不排轮询（省掉无意义的请求）。 */
      stopAnalysisPoll();
      applyAnalysis("offline", {});
    } else {
      applyAnalysis("pending", {elapsed_s: null});   /* ★ 同时刷新 summary + body（单一写入口） */
      scheduleAnalysisPoll(code);
    }
    toast("已加载 " + code + "（" + p + "）", "ok");
  });
}
/* ★ P0-3（Architect）：**「下单」只打开并预填表单，绝不提交**（一键下单 = 花真钱）。 */
function prefillOrder(code) {
  var target = String(code || currentCode() || "").trim();
  if (!target) { toast("请先填写代码", "warn"); return; }
  var field = $("order-code");
  if (field) { field.value = target; }
  var form = $("order-form");
  if (form && form.scrollIntoView) { form.scrollIntoView({behavior: "smooth", block: "center"}); }
  toast("已预填下单表单（" + target + "）—— 请核对数量/价格后**手动**提交", "ok");
}

function refreshState() { return api(API.state).then(function (b) { if (b) { renderState(b.data); } }); }
function refreshPositions() { return api(API.positions).then(function (b) { if (b) { renderPositions(b.data); } }); }
function refreshRisk() { return api(API.risk).then(function (b) { if (b) { renderRisk(b.data); } }); }
/* ★★ X3（真缺陷，已修）：这里**曾经**又定义了一次 `refreshWatchlist`：
 *      function refreshWatchlist() { return api(API.watchlist).then(...) }
 *    JS 里**后定义的函数覆盖先定义的** ⇒ 上面 L480 那个「会再调 /api/quote 取现价/涨跌幅」的版本被**整个盖掉**
 *    ⇒ `QUOTE_MAP` 永远空 ⇒ 自选池的**现价 / 涨跌幅永远是 `—`**（用户要求 ② 只做到一半）。
 *    ★ 这是**只读接口测试完全抓不到**的那类缺陷：`/api/quote` 本身返回 200 且数据正确，
 *      坏在「页面用的是哪个函数」。**只有真机看渲染出来的文本才能发现。**
 *    ⇒ 现在**只保留 L480 那一处定义**（它同时负责 renderWatchlist 与取价），
 *      并由 `T-V3-28` 钉住「`function refreshWatchlist` 全页**恰好 1 处**且必须出现 `API.quote`」。
 */
/* ---- 自动刷新：仅盘中（依据**后端** market_open 三态，**不用客户端时钟**） ----
 * true  盘中         → 每 POLL_ACTIVE_MS 拉一次
 * false 交易日非盘中 → **停**（不轮询）
 * null  不可判定     → **固定低频探测**（与 false 的「停」不同：日历缺失时若直接停，
 *                      用户久未联网会让自动刷新**静默死掉**）
 * 基础节拍 POLL_TICK_MS 单位毫秒，∈ (0, 60) 秒。
 */
var POLL_TICK_MS = 5000;
var POLL_ACTIVE_MS = 5000;      /* ★ V5：盘中 **5 秒**（用户要求「涨幅随时刷新」） */
var POLL_PROBE_MS = 15000;      /* ★ V5：判不出时段时的探测频率 */
var POLL = { marketOpen: null, delay: POLL_PROBE_MS, last: 0, cadence: null };
var HEALTH = { offline: false, cadence: null };
var PN_CONFIG = window.PN_CONFIG || {};
function isOffline() { return HEALTH.offline === true; }
/* ★★ V5：刷新节拍**只由服务端 `/api/health.poll_cadence_ms` 决定**
 *   （盘中 5000 / 非盘中 0 / 判不出 15000）—— 前端**不许自己算时段**：
 *   源码里没有任何本地交易时段常量，也没有本地时间取小时的家庭作坊（判据会 grep 源码）。
 */
function applyCadence(ms) {
  if (typeof ms !== "number" || !isFinite(ms) || ms < 0) { return; }
  HEALTH.cadence = ms;
  POLL.cadence = ms;
  if (POLL.marketOpen !== false) { POLL.delay = ms; }
}
function pollCadence() {
  if (POLL.cadence !== null) { return POLL.cadence; }
  return POLL.delay;
}
function pollDelayFor(marketOpen) {
  if (marketOpen === true) { return POLL_ACTIVE_MS; }
  if (marketOpen === false) { return 0; }
  return POLL_PROBE_MS;
}
function shouldPoll(marketOpen) { return marketOpen !== false; }
function applyMarketOpen(value) {
  POLL.marketOpen = (value === true || value === false) ? value : null;
  POLL.delay = pollDelayFor(POLL.marketOpen);
}
function markUpdated() {
  /* ★ V5：「上次更新 HH:MM:SS」—— 用 `toLocaleTimeString` 取本地时刻（不含本地时段常量） */
  var el = $("last-updated");
  if (!el) { return; }
  var now = new Date();
  el.textContent = "上次更新 " + now.toLocaleTimeString("zh-CN", {hour12: false});
}
function autoRefreshTick() {
  if (!shouldPoll(POLL.marketOpen)) { return; }
  if (document.hidden) { return; }                  /* ★ V5：页面隐藏时暂停（省流量/省电） */
  var cadence = pollCadence();
  if (cadence === 0) { return; }                    /* 服务端说 0 ⇒ 非盘中，不自动刷 */
  var now = Date.now();
  if (POLL.last && (now - POLL.last) < cadence) { return; }
  POLL.last = now;
  refreshAll();
  markUpdated();
}
/* ★ V5：切回页面**立即刷一次**（否则用户要等到下一个节拍） */
document.addEventListener("visibilitychange", function () {
  if (!document.hidden) {
    POLL.last = 0;
    autoRefreshTick();
  }
});
function marketOpenLabel() {
  if (POLL.marketOpen === true) { return "盘中"; }
  if (POLL.marketOpen === false) { return "非盘中"; }
  return "盘中状态不可判定（低频探测）";
}
/* ---- PN：分析异步 —— **有界**轮询小 JSON，`ready` 后**只刷一次** ----
 * 为什么不是「25s 只重试一次」：Architect 实测首算 57–153s ⇒ 25s 时**必然仍 pending**，
 * 那等于用户**永远看不到分析**。改成有界轮询：≤ANALYSIS_POLL_MAX 次、总时长 ≤2min。
 * 为什么ready后只渲染小面板而不是整页：分析块不再嵌在整页里，重排整页是浪费。
 * ★ 只刷**一次**：拿到 ready 就 return，不再排下一次（不得形成循环）。
 */
var ANALYSIS_POLL_MAX = 60;        /* ★ V5：60×10s = 600s 窗口 > 实测首算 132s */
var ANALYSIS_POLL_MS = 10000;      /* ★ V5：10s 一次（原 20s 太钝，用户以为「不刷新」） */
/* ★★ 两个常量**可注入**：判据用 `window.PN_CONFIG = {analysisPollMs:20, analysisPollMax:40}`
 *   跑「假分析」而不必真等 100s。读不到就用默认值。 */
function analysisPollMs() { return (PN_CONFIG && PN_CONFIG.analysisPollMs) || ANALYSIS_POLL_MS; }
function analysisPollMax() { return (PN_CONFIG && PN_CONFIG.analysisPollMax) || ANALYSIS_POLL_MAX; }
var ANALYSIS = { timer: null, n: 0, code: "", done: false };
function stopAnalysisPoll() {
  if (ANALYSIS.timer) { clearTimeout(ANALYSIS.timer); ANALYSIS.timer = null; }
  ANALYSIS.done = false;
}
function scheduleAnalysisPoll(code) {
  stopAnalysisPoll();
  ANALYSIS.n = 0; ANALYSIS.code = code;
  /* ★ INV-64：轮询**不带 offline 参数** —— 前端不得决定该开关（`--offline` 才是服务端硬闸）。 */
  ANALYSIS.timer = setTimeout(analysisTick, 1200);
}
function analysisTick() {
  ANALYSIS.timer = null;
  if (ANALYSIS.done) { return; }
  ANALYSIS.n += 1;
  var q = "code=" + encodeURIComponent(ANALYSIS.code);
  api(API.analysis + "?" + q).then(function (b) {
    var d = (b && b.data) || {};
    var st = d.state || "pending";
    if (st === "ready") {                    /* ★ 只渲染**一次**分析面板（不重排整页） */
      ANALYSIS.done = true;
      applyAnalysis("ready", d);
      return;
    }
    if (st === "failed") {                   /* 失败：**带原因文案**告诉用户，不停在「永远在算」 */
      ANALYSIS.done = true;
      applyAnalysis("failed", d);
      toast("分析失败：" + (d.error || "未知原因"), "warn");
      return;
    }
    if (st === "no_data") {                  /* ★ V5：无数据不是「计算中」 */
      ANALYSIS.done = true;
      applyAnalysis("no_data", d);
      return;
    }
    if (isOffline()) {                       /* ★★ 离线：**永远算不完**，绝不得继续显示「计算中」 */
      ANALYSIS.done = true;
      applyAnalysis("offline", d);
      return;
    }
    if (ANALYSIS.n < analysisPollMax()) {
      applyAnalysis("pending", d);           /* ★ summary 与 body **同源**刷新（已算 N 秒） */
      ANALYSIS.timer = setTimeout(analysisTick, analysisPollMs());
    } else {
      ANALYSIS.done = true;
      applyAnalysis("pending", {elapsed_s: d.elapsed_s, give_up: true});   /* ★ 可点「点击重试」 */
    }
  });
}
function refreshHealth() {
  return api(API.health).then(function (b) {
    var d = (b && b.data) || {};
    if (b) {
      applyMarketOpen(d.market_open);
      /* ★★ V5：刷新节拍**消费服务端 cadence**（盘中 5000 / 非盘中 0 / 判不出 15000）——
         前端不算时段；字段缺失时退回三态启发（仍不读本地时间）。 */
      if (d.poll_cadence_ms === undefined || d.poll_cadence_ms === null) {
        applyCadence(pollDelayFor(d.market_open));
      } else {
        applyCadence(d.poll_cadence_ms);
      }
      HEALTH.offline = (d.offline === true);
      markUpdated();
    }
    $("health").textContent = b
      ? ("后端在线 · " + (d.root || "") + (d.trade_date ? " · 交易日 " + d.trade_date : "")
         + " · " + marketOpenLabel() + (HEALTH.cadence === null ? "" : " · 界面每 " + (HEALTH.cadence / 1000) + "s 刷新"))
      : "后端不可达";
  });
}
function refreshAll() { refreshHealth(); refreshState(); refreshPositions(); refreshRisk(); refreshWatchlist(); }

function doSearch() {
  var q = currentCode();
  if (!q) { toast("请输入代码或名称关键字", "warn"); return; }
  /* ★ INV-64：搜索请求也**不带 offline 参数**（服务端 --offline 是唯一开关）。 */
  var url = API.search + "?q=" + encodeURIComponent(q) + "&limit=30";
  api(url).then(function (b) {
    /* ★★ H1（code-review，HIGH）：服务端把「名称表不可用/已损坏」作为
       **200 业务态 + error** 返回（既定契约）。这里若只读 `b.data`，就会
       显示「无命中」+ **绿色 toast「搜索完成」** —— 那正是 A1 守卫要消灭的症状
       （用户以为「没这只票」）。**服务端已经不静默了，UI 不得把它静默回去。**
       ⇒ 有 error 必须显式显示错误并 return，**不得**渲染成「无命中」。 */
    if (b && b.error) {
      $("search-results").innerHTML =
        '<div class="err-txt">搜索失败：' + esc(b.error) + "</div>";
      toast(b.error, "err");
      return;
    }
    if (b) { renderSearch(b.data); toast("搜索完成", "ok"); }
  });
}

function addToWatchlist(code, name) {
  if (!code) { return; }
  post(API.watchlist, { code: code, name: name || "" }).then(function (b) {
    if (b) { toast("已加入自选池：" + code, "ok"); refreshWatchlist(); }
  });
}
function removeFromWatchlist(code) {
  if (!code || !confirmWrite("从自选池移除 " + code)) { return; }
  api(API.watchlist + "?code=" + encodeURIComponent(code), { method: "DELETE" }).then(function (b) {
    if (b) { toast("已移除：" + code, "ok"); refreshWatchlist(); }
  });
}
function scoreWatchlist() {
  post(API.watchlistScore, {}).then(function (b) { if (b) { renderScores(b.data); toast("自选池打分完成", "ok"); } });
}
function scoreOne(code) {
  if (!code) { return; }
  post(API.score, { codes: [code] }).then(function (b) { if (b) { renderScores(b.data); } });
}

function submitOrder() {
  var code = ($("order-code").value || "").trim();
  var side = $("order-side").value;
  var qty = parseInt($("order-qty").value, 10);
  if (!code) { toast("请填写代码", "warn"); return; }
  if (!qty || qty <= 0) { toast("数量必须是正整数（A 股需为 100 的整数倍）", "warn"); return; }
  if (qty % 100 !== 0) { toast("数量必须是 100 的整数倍，否则会被拒单 INVALID_LOT", "warn"); return; }
  if (!confirmWrite("下单：" + side + " " + code + " × " + qty)) { return; }
  var payload = { code: code, side: side, qty: qty };
  var price = ($("order-price").value || "").trim();
  if (price) { payload.price = Number(price); }
  var date = ($("order-date").value || "").trim();
  if (date) { payload.trade_date = date; }
  post(API.order, payload).then(function (b) {
    if (b) { refreshState(); refreshPositions(); refreshRisk(); renderScores({ results: (b.data && b.data.results) || [] }); }
  });
}

function submitClose() {
  var code = ($("order-code").value || "").trim();
  if (!code) { toast("请填写要平仓的代码", "warn"); return; }
  var ratio = Number(($("close-ratio").value || "1.0").trim() || "1.0");
  if (!(ratio > 0 && ratio <= 1)) { toast("平仓比例需在 (0, 1] 区间", "warn"); return; }
  if (!confirmWrite("平仓 " + code + "（比例 " + ratio + "，先买券还券、再卖券还款/卖出）")) { return; }
  post(API.close, { code: code, ratio: ratio }).then(function (b) {
    if (b) { refreshState(); refreshPositions(); refreshRisk(); }
  });
}

function submitSettle() {
  var date = ($("settle-date").value || "").trim();
  if (!date) { toast("请填写结算目标交易日（如 2025-10-10）", "warn"); return; }
  if (!confirmWrite("结算到 " + date + "（T+1 交收 / 按日计息 / 盯市 / 风险评估）")) { return; }
  post(API.settle, { trade_date: date }).then(function (b) {
    if (b) { refreshAll(); toast("已结算到 " + date, "ok"); }
  });
}

/* 事件绑定：表格里的按钮走事件委托，避免内联 onclick（便于 CSP 与测试断言）。 */
document.addEventListener("click", function (ev) {
  var t = ev.target;
  if (!t || t.tagName !== "BUTTON") { return; }
  var act = t.getAttribute("data-act");
  var code = t.getAttribute("data-code") || "";
  if (act === "chart") {
    setCurrentCode(code);
    loadChart("daily");
  } else if (act === "minute") {
    setCurrentCode(code);
    loadChart("minute");
  } else if (act === "add") {
    addToWatchlist(code, t.getAttribute("data-name"));
  } else if (act === "del") {
    removeFromWatchlist(code);
  } else if (act === "order") {
    /* ★ P0-3：只预填表单，**不 POST /api/order** */
    prefillOrder(code);
  }
});
function onRowClick(ev) {
  var tr = ev.target && ev.target.closest ? ev.target.closest("tr[data-code]") : null;
  if (!tr || (ev.target.tagName === "BUTTON")) { return; }
  var code = tr.getAttribute("data-code");
  if (code) { setCurrentCode(code); loadChart("daily"); }
}
["positions", "score-table", "watchlist"].forEach(function (id) {
  var el = $(id);
  if (el) { el.addEventListener("click", onRowClick); }
});
$("refresh-btn").addEventListener("click", refreshAll);
$("act-search").addEventListener("click", doSearch);
$("stock-code").addEventListener("keydown", function (e) {
  if (e.key === "Enter") {
    e.preventDefault();
    /* 回车 = 搜索；若输入的是 6 位代码则直接出图（少一次点击） */
    var code = currentCode();
    if (/^\\d{6}$/.test(code)) { loadChart("daily"); } else { doSearch(); }
  }
});
$("act-chart-daily").addEventListener("click", function () { loadChart("daily"); });
$("act-chart-minute").addEventListener("click", function () { loadChart("minute"); });
$("act-watchlist-add").addEventListener("click", function () {
  var code = currentCode();
  if (!code) { toast("请先填写代码", "warn"); return; }
  addToWatchlist(code, "");
});
$("act-order").addEventListener("click", function () { prefillOrder(currentCode()); });
var periodBtns = document.querySelectorAll(".chart-period");
for (var bi = 0; bi < periodBtns.length; bi++) {
  periodBtns[bi].addEventListener("click", function (ev) {
    loadChart(ev.currentTarget.getAttribute("data-period"));
  });
}
$("watchlist-refresh-btn").addEventListener("click", refreshWatchlist);
$("score-watchlist-btn").addEventListener("click", function () {
  if (!confirmWrite("对自选池全部标的批量打分")) { return; }
  scoreWatchlist();
});
$("order-submit-btn").addEventListener("click", submitOrder);
$("close-submit-btn").addEventListener("click", submitClose);
$("settle-btn").addEventListener("click", submitSettle);
/* ---- V4 折叠 v2（R3-3）----
 * 默认：持仓/自选池/账户/风险/操作条 **展开**；打分 / CANSLIM 分析 **折叠**（<details>）。
 * ★ 控制方式：**首次加载读一次 innerHeight**（阈值 1000px，非整点）决定开合；
 *   之后**手动开合记进 localStorage**（同一个键），刷新后沿用用户的选择。
 * ★ **不用 @media 控制 open** —— 拖窗口时卡片突然开合是惊吓；@media 只做紧凑化。
 * ★ 删掉了「≥1200 自动展开」：1080–1200 曾是规则真空。
 */
var COLLAPSE_KEY = "ptrade.v4.collapse";
function collapseBoxes() { return document.querySelectorAll("details[data-collapse]"); }
function initCollapse() {
  var saved = null;
  try { saved = window.localStorage.getItem(COLLAPSE_KEY); } catch (e) { saved = null; }
  var opened;
  if (saved === "1" || saved === "0") {
    opened = (saved === "1");
  } else {
    opened = (window.innerHeight || 0) >= 1000;      /* ★ 阈值 1000px */
  }
  var boxes = collapseBoxes();
  for (var i = 0; i < boxes.length; i++) {
    boxes[i].open = opened;
    boxes[i].addEventListener("toggle", function (ev) {
      try { window.localStorage.setItem(COLLAPSE_KEY, ev.currentTarget.open ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
    });
  }
}
setInterval(autoRefreshTick, POLL_TICK_MS);
window.addEventListener("DOMContentLoaded", function () { refreshAll(); initCollapse(); });
refreshAll();
initCollapse();
</script>
</body>
</html>
"""
