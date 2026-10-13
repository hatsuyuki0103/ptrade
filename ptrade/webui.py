"""ptrade 本地网页控制台的前端资源（W4）。

本模块只导出一个常量 :data:`APP_HTML` —— **单个自包含 HTML**（内联 CSS + JS、零外链），
由 W3 的 ``ptrade/webapp.py`` 在 ``GET /`` 直接返回，浏览器打开即用。

设计约束（来自 ``.omx/plans/prd-ptrade-web.md`` §5.4/§5.5 与 ``.omx/plans/test-spec-ptrade-web.md`` §5）:

* **INV-42**：``APP_HTML`` 中不得有任何外链资源引用。校验器复用
  :func:`ptrade.chart.no_external_refs`，它**不只看 http(s)** —— 任何
  ``<script src>`` / ``<link href>`` / ``<img|iframe|source|video|audio|embed|object|track src>`` /
  ``<a href>`` / ``@import`` / CSS ``url(...)`` 只要不是 ``data:``/``javascript:``/``about:``/``blob:``/``#``
  **都算外链**（含相对路径）。因此：
  - 不写任何 ``<link>`` / ``<img>`` / ``<a href="…">``；按钮一律用 ``<button>``；
  - CSS 里不出现 ``url(...)``；
  - ``<iframe id="chart-frame">`` **不带 src 属性** —— K 线图 HTML 在运行时由 JS 赋
    ``iframe.src = API.chart + '?…'``（这样静态文本里没有任何资源引用）。
* **不内联 ECharts**（1 MB）：K 线图由 ``GET /api/chart`` 返回**完整 HTML**，前端用
  ``<iframe id="chart-frame">`` 承载；``APP_HTML`` 里连 ``echarts`` 这个词都不出现。
* 体积 < 200 KB；中文界面；写操作（下单 / 平仓 / 结算）都有二次确认。

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
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:9}
h1{font-size:16px;margin:0 12px 0 0}
h2{font-size:14px;margin:0 0 8px;color:var(--acc)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px;padding:12px 16px 40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
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
#chart-frame{width:100%;height:520px;border:1px solid var(--line);border-radius:6px;background:#0d1524}
.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 6px;font-size:11px;color:var(--mut);margin-left:4px}
.badge{display:inline-block;border-radius:4px;padding:1px 6px;font-size:12px}
.badge-buy{background:#14532d;color:#86efac}.badge-short{background:#7f1d1d;color:#fecaca}
.badge-watch{background:#3f3f46;color:#e4e4e7}.badge-avoid{background:#422006;color:#fed7aa}
.err-txt{color:var(--err)}.warn-txt{color:var(--warn)}
.empty{color:var(--mut);padding:6px 0}
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

<main>
  <section class="card">
    <h2>账户状态</h2>
    <div id="state-panel" class="grid2"><span class="empty">加载中…</span></div>
  </section>

  <section class="card">
    <h2>风险</h2>
    <div id="risk"><span class="empty">加载中…</span></div>
  </section>

  <section class="card">
    <h2>搜索（代码 / 中文名）</h2>
    <div class="row">
      <input id="search-box" size="24" placeholder="600519 或 茅台" autocomplete="off">
      <label class="small muted"><input id="search-offline" type="checkbox" checked> 离线</label>
      <button id="search-btn">搜索</button>
    </div>
    <div id="search-results" class="small"><span class="empty">输入关键字后回车搜索</span></div>
  </section>

  <section class="card">
    <h2>自选池</h2>
    <div class="row">
      <button id="watchlist-refresh-btn">刷新自选池</button>
      <button id="score-watchlist-btn" class="primary" title="对自选池全部标的批量七因子打分（缺数据标注「无数据」，不记 0 分）">批量打分</button>
      <span class="small muted">点行看 K 线，点「删」移除</span>
    </div>
    <div id="watchlist"><span class="empty">加载中…</span></div>
  </section>

  <section class="card">
    <h2>打分（七因子 C/A/N/S/L/I/M + 信号）</h2>
    <div id="score-table"><span class="empty">尚未打分</span></div>
  </section>

  <section class="card">
    <h2>持仓</h2>
    <div id="positions"><span class="empty">加载中…</span></div>
  </section>

  <section class="card">
    <h2>K 线图（日 / 周 / 月线，前复权）</h2>
    <div class="row">
      <input id="chart-code" size="10" placeholder="代码 601398">
      <select id="chart-period">
        <option value="daily">日线</option>
        <option value="weekly">周线</option>
        <option value="monthly">月线</option>
      </select>
      <input id="chart-asof" size="10" placeholder="asof 2025-10-09">
      <button id="chart-load-btn" class="primary">加载图表</button>
      <span class="small muted">图表由后端 /api/chart 内联返回完整 HTML（图表库与数据全部内联），用 iframe 承载</span>
    </div>
    <iframe id="chart-frame" title="K 线图"></iframe>
  </section>

  <section class="card">
    <h2>下单 / 平仓</h2>
    <form id="order-form" autocomplete="off" onsubmit="return false">
      <div class="grid2">
        <label class="small">代码 <input id="order-code" size="10" placeholder="601398" required></label>
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
        <label class="small">数量 <input id="order-qty" size="8" placeholder="100" required></label>
        <label class="small">价格（留空=市价） <input id="order-price" size="8" placeholder="7.02"></label>
        <label class="small">交易日（留空=当前） <input id="order-date" size="10" placeholder="2025-10-09"></label>
        <label class="small">平仓比例 <input id="close-ratio" size="6" placeholder="1.0"></label>
      </div>
      <div class="row" style="margin-top:8px">
        <button id="order-submit-btn" class="primary" type="button">提交下单</button>
        <button id="close-submit-btn" class="danger" type="button">平仓</button>
        <span class="small muted">下单前请核对单票上限 = 净资产 × 20%（50 万账户 ≈ 10 万/票）；写操作需二次确认</span>
      </div>
    </form>
  </section>
</main>

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
  chart: "/api/chart"
};
var CHART_FRAME = document.getElementById("chart-frame");
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

function renderState(data) {
  var d = (data && data.state) || data || {};
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
    return "<tr data-code=\"" + esc(p.code) + "\"><td>" + esc(p.code) + " " + esc(p.name || "")
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
    ? "<ul class=\"small\">" + alerts.map(function (a) {
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
      + '<button type="button" data-act="add" data-code="' + esc(r.code) + '" data-name="' + esc(r.name || "") + '">加入自选</button>'
      + "</td></tr>";
  }));
}

function renderWatchlist(data) {
  var rows = asRows(data && (data.items || data), ["items", "results", "rows"]);
  if (!rows.length) { $("watchlist").innerHTML = '<div class="empty">自选池为空：用上面的搜索添加</div>'; return; }
  $("watchlist").innerHTML = table(["代码/名称", "备注", "操作"], rows.map(function (r) {
    return '<tr data-code="' + esc(r.code) + '"><td>' + esc(r.code) + " " + esc(r.name || "") + "</td><td>"
      + esc(r.note || "") + '</td><td><button type="button" data-act="chart" data-code="' + esc(r.code)
      + '">看图</button> <button type="button" class="danger" data-act="del" data-code="' + esc(r.code) + '">删</button></td></tr>';
  }));
}

function renderScores(data) {
  var rows = asRows(data && (data.results || data), ["results", "items", "rows"]);
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

function loadChart() {
  var code = ($("chart-code").value || "").trim();
  if (!code) { toast("请填写要绘制的代码", "warn"); return; }
  var q = [["code", code], ["period", $("chart-period").value || "daily"]];
  var asof = ($("chart-asof").value || "").trim();
  if (asof) { q.push(["asof", asof]); }
  q.push(["offline", "1"]);
  var url = API.chart + "?" + q.map(function (kv) { return encodeURIComponent(kv[0]) + "=" + encodeURIComponent(kv[1]); }).join("&");
  CHART_FRAME.removeAttribute("srcdoc");
  CHART_FRAME.src = url;
  toast("正在加载 " + code + " 的图表（后端内联返回完整 HTML）", "ok");
}

function refreshState() { return api(API.state).then(function (b) { if (b) { renderState(b.data); } }); }
function refreshPositions() { return api(API.positions).then(function (b) { if (b) { renderPositions(b.data); } }); }
function refreshRisk() { return api(API.risk).then(function (b) { if (b) { renderRisk(b.data); } }); }
function refreshWatchlist() { return api(API.watchlist).then(function (b) { if (b) { renderWatchlist(b.data); } }); }
function refreshHealth() {
  return api(API.health).then(function (b) {
    var d = (b && b.data) || {};
    $("health").textContent = b ? ("后端在线 · " + (d.root || "") + (d.trade_date ? " · 交易日 " + d.trade_date : "")) : "后端不可达";
  });
}
function refreshAll() { refreshHealth(); refreshState(); refreshPositions(); refreshRisk(); refreshWatchlist(); }

function doSearch() {
  var q = ($("search-box").value || "").trim();
  if (!q) { toast("请输入代码或名称关键字", "warn"); return; }
  var offline = $("search-offline").checked ? "1" : "0";
  var url = API.search + "?q=" + encodeURIComponent(q) + "&limit=30&offline=" + offline;
  api(url).then(function (b) {
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
  if (act === "chart") {
    $("chart-code").value = t.getAttribute("data-code") || "";
    loadChart();
  } else if (act === "add") {
    addToWatchlist(t.getAttribute("data-code"), t.getAttribute("data-name"));
  } else if (act === "del") {
    removeFromWatchlist(t.getAttribute("data-code"));
  }
});
function onRowClick(ev) {
  var tr = ev.target && ev.target.closest ? ev.target.closest("tr[data-code]") : null;
  if (!tr || (ev.target.tagName === "BUTTON")) { return; }
  var code = tr.getAttribute("data-code");
  if (code) { $("chart-code").value = code; loadChart(); }
}
["positions", "score-table", "watchlist"].forEach(function (id) {
  var el = $(id);
  if (el) { el.addEventListener("click", onRowClick); }
});
$("refresh-btn").addEventListener("click", refreshAll);
$("search-btn").addEventListener("click", doSearch);
$("search-box").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); doSearch(); } });
$("watchlist-refresh-btn").addEventListener("click", refreshWatchlist);
$("score-watchlist-btn").addEventListener("click", function () {
  if (!confirmWrite("对自选池全部标的批量打分")) { return; }
  scoreWatchlist();
});
$("chart-load-btn").addEventListener("click", loadChart);
$("order-submit-btn").addEventListener("click", submitOrder);
$("close-submit-btn").addEventListener("click", submitClose);
$("settle-btn").addEventListener("click", submitSettle);
window.addEventListener("DOMContentLoaded", refreshAll);
refreshAll();
</script>
</body>
</html>
"""
