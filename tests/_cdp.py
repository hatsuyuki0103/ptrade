"""CDP harness（V4-W3）：常驻 headless Chrome + ``Emulation`` 域 + 页面布局测量。

为什么必须有这个模块（Critic C2）：四档视口（1600×900 / 1920×945 / 1920×1080 / 2560×1440）
**物理上**只能靠 ``Emulation.setDeviceMetricsOverride`` 做到 —— 只开 ``--window-size`` 会被
Chrome 的窗口装饰/DPI 吃掉，而 ``Runtime.*``/``Page.*`` 两个域**没有**改视口的能力。
没有这一域，视口类判据只能 skip ⇒ 套件仍绿 = 假绿。

用法（测试侧）::

    from tests import _cdp
    with _cdp.serve(cfg, offline=True) as base:
        m = _cdp.measure((1920, 945), url=base)

    print(_cdp.status())      # {'available': bool, 'reason': ..., 'chrome': ..., 'node': ...}

设计：
* **常驻**：一个 node 驱动进程 + 一个 Chrome 进程，整个 pytest session 复用（``measure()``
  内部惰性启动单例）。每次 ``measure()`` 只做 override → navigate → 采集。
* **协议**：node 驱动从 stdin 读一行 JSON、往 stdout 写一行 JSON（Python 侧用管道，
  本项目已实测管道可用：``tests/test_perf_analysis.py::test_v3_24b`` 用 node 子进程）。
* **降级**：找不到 Chrome/node → ``CDPUnavailable``；测试侧必须 ``pytest.skip`` 并显式记录
  （**不得**计入「通过」）。``status()`` 给出原因，报告里照抄。
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

__all__ = [
    "CDPUnavailable",
    "SESSION_TIMEOUT_S",
    "close",
    "find_chrome",
    "measure",
    "serve",
    "status",
]

#: 单次 measure 的上限（导航 + 首帧 + 采集；冷夹具分析较慢，给足）
SESSION_TIMEOUT_S = 90.0

_CHROME_CANDIDATES = (
    os.environ.get("PTRADE_CHROME", ""),
    r"C:\Users\yaoyufeng\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

#: 页内要点击/读取的兜底选择器（v3 → v4 改名时不至于全线崩）
_CHART_DAILY_SELECTORS = (
    '[data-testid="chart-daily"]',
    "#act-chart-daily",
    '.chart-period[data-period="daily"]',
    '[data-period="daily"]',
    '[data-testid="chart"]',
)
_CODE_INPUTS = ('#stock-code', '[data-testid="stock-code-input"]', '[data-testid="stock-code"]')

_DRIVER_JS = r"""
// CDP 驱动（由 tests/_cdp.py 写到临时文件后 node 执行）。stdin/stdout 各一行 JSON。
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = (obj) => { process.stdout.write(JSON.stringify(obj) + "\n"); };

const CHROME = process.argv[2];
const DEBUG_PORT = Number(process.argv[3]);

const profile = mkdtempSync(join(tmpdir(), "ptrade-v4-cdp-"));
const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
  "--hide-scrollbars=false", "--force-device-scale-factor=1",
  `--remote-debugging-port=${DEBUG_PORT}`, `--user-data-dir=${profile}`,
  // 初始窗口随便给；真正的视口由 Emulation.setDeviceMetricsOverride 决定
  "--window-size=1920,1080", "about:blank",
], { stdio: "ignore" });

let wsUrl = null;
for (let i = 0; i < 120; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
    const list = await r.json();
    const page = list.find((t) => t.type === "page");
    if (page && page.webSocketDebuggerUrl) { wsUrl = page.webSocketDebuggerUrl; break; }
  } catch {}
  await sleep(250);
}
if (!wsUrl) { out({ ok: false, fatal: "chrome 未在 ~30s 内暴露 DevTools（端口=" + DEBUG_PORT + "）" }); process.exit(3); }

const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws 连接失败")); });
let seq = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  }
};
function send(method, params = {}) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const r = await send("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  });
  if (r.exceptionDetails) {
    const d = r.exceptionDetails;
    throw new Error("page JS 抛错: " + (d.text || "") + " " +
                    (d.exception && d.exception.description || "").slice(0, 300));
  }
  return r.result ? r.result.value : undefined;
}

await send("Runtime.enable");
await send("Page.enable");
out({ ok: true, ready: true });

// ---- 页内测量（一次 evaluate 返回全部字段） ----
const MEASURE_JS = String.raw`(() => {
  const de = document.documentElement, bd = document.body;
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height, top: r.top, bottom: r.bottom,
             left: r.left, right: r.right };
  };
  const pct = (v) => {
    if (typeof v === "string" && v.trim().endsWith("%")) {
      const n = parseFloat(v); return Number.isFinite(n) ? n : null;
    }
    return null;
  };
  const root = document.querySelector("#chart-root");
  const inst = (root && window.echarts) ? window.echarts.getInstanceByDom(root) : null;
  let opt = null;
  try { opt = inst ? inst.getOption() : null; } catch (e) { opt = null; }
  // ---- 图例 ----
  const legendData = [];
  if (opt && opt.legend) {
    const legends = Array.isArray(opt.legend) ? opt.legend : [opt.legend];
    for (const lg of legends) {
      for (const item of (lg.data || [])) {
        legendData.push(typeof item === "string" ? item : (item && item.name) || "");
      }
    }
  }
  const legendSelected = {};
  if (inst) {
    try {
      const lm = inst.getModel().getComponent("legend");
      const sel = (lm && lm.get("selected")) || {};
      for (const k of Object.keys(sel)) legendSelected[k] = sel[k];
    } catch (e) {}
  }
  // ---- title / grid ----
  const titles = (opt && opt.title) ? (Array.isArray(opt.title) ? opt.title : [opt.title]) : [];
  const titleTexts = titles.map((t) => String((t && t.text) || ""));
  const titleTops = titles.map((t) => (t ? t.top : null));
  const titleTopsPct = titleTops.map((v) => {
    if (typeof v === "string") return pct(v);
    if (typeof v === "number") return v;       // 数字=px，交给测试判定（不假装是 %）
    return null;
  });
  const gridsRaw = (opt && opt.grid) ? (Array.isArray(opt.grid) ? opt.grid : [opt.grid]) : [];
  const grids = gridsRaw.map((g) => ({ top: g ? g.top : null, height: g ? g.height : null,
                                       topPct: pct(g ? g.top : null),
                                       heightPct: pct(g ? g.height : null) }));
  // ---- series 按 xAxisIndex 分组（面板归属） ----
  const series = (opt && opt.series) ? opt.series : [];
  const seriesByGrid = {};
  const candlestickAxes = [];
  for (const s of series) {
    const ax = (s && s.xAxisIndex !== undefined && s.xAxisIndex !== null) ? s.xAxisIndex : 0;
    seriesByGrid[ax] = (seriesByGrid[ax] || 0) + 1;
    if (s && s.type === "candlestick") candlestickAxes.push(ax);
  }
  // ---- 右列 / 卡片 / 折叠 ----
  const main = document.querySelector("main");
  const chartCol = document.querySelector(".chart-col") || (root ? root.closest(".chart-col") : null);
  const infoCol = document.querySelector(".info-col") || document.querySelector('[data-testid="info-col"]')
    || (main && main.children.length >= 2 ? main.children[main.children.length - 1] : null);
  const infoColDeclared = !!(document.querySelector(".info-col") || document.querySelector('[data-testid="info-col"]'));
  const cards = [];
  if (infoCol) {
    for (const el of infoCol.children) {
      const r = rect(el);
      if (r && r.h > 0 && r.w > 0) cards.push({ tag: el.tagName.toLowerCase(),
        cls: String(el.className || "").slice(0, 40), ...r });
    }
  }
  // 折叠：<details> 双判据（open is False **且** 内容 offsetHeight === 0）
  const detailsInfo = [];
  const collapsed = { score: null, positions: null, analysis: null };
  const foldable = { score: false, positions: false, analysis: false };
  const summaryText = { score: null, positions: null, analysis: null };
  const classify = (txt) => {
    const t = String(txt || "");
    if (/打分|评分|score/i.test(t)) return "score";
    if (/持仓|position/i.test(t)) return "positions";
    if (/分析|analysis/i.test(t)) return "analysis";
    return null;
  };
  if (infoCol) {
    for (const d of infoCol.querySelectorAll("details")) {
      const sum = d.querySelector("summary");
      const sumTxt = sum ? String(sum.textContent || "").trim() : "";
      const key = classify(sumTxt) || classify(String(d.className || "")) || classify(String(d.id || ""));
      const body = d.querySelector(":scope > :not(summary)");
      const contentH = body ? body.offsetHeight : 0;
      // ★ R3-D 双判据的**可靠侧**：offsetHeight 在 Chrome 的 content-visibility 实现下
      //   即便内容被隐藏也可能非 0（实测 closed 状态仍有 19px）⇒ 必须用 checkVisibility /
      //   getClientRects / computedStyle.display 三者一起判「内容到底可不可见」。
      let visible = null, display = null, rects = null;
      if (body) {
        try {
          display = window.getComputedStyle(body).display;
          rects = body.getClientRects().length;
          visible = (typeof body.checkVisibility === "function")
            ? body.checkVisibility({ contentVisibilityAuto: true, visibilityProperty: true,
                                     opacityProperty: false })
            : (display !== "none" && rects > 0);
        } catch (e) { visible = null; }
      }
      detailsInfo.push({ key, open: d.open, contentH, summary: sumTxt.slice(0, 160),
                         contentVisible: visible, contentDisplay: display, contentRects: rects });
      if (key) {
        // ★ R3-A/R3-D：闭合(**open is False**) 且 内容**不可见** 才算「折叠生效」；
        //   若 open=false 但内容仍可见（CSS 覆盖/实现缺陷）⇒ 判「没折叠」，可被抓到。
        collapsed[key] = (d.open === false) && (visible === false || (visible === null && contentH === 0));
        foldable[key] = true;
        summaryText[key] = sumTxt;
      }
    }
    // ★ 非 <details> 的卡片 = **永远展开**（例如「持仓」默认就不该折叠）
    //   ⇒ collapsed=false（而不是 null）；null 只留给「这张卡根本不存在」。
    for (const el of infoCol.querySelectorAll(".card, section, article")) {
      if (el.tagName.toLowerCase() === "details") continue;
      const head = el.querySelector("h2, h3, .card-title, [data-title]");
      const headTxt = head ? String(head.textContent || "") : "";
      const cardAttr = el.getAttribute && el.getAttribute("data-collapse");
      const key = classify(cardAttr) || classify(headTxt)
        || classify(String(el.id || "")) || classify(String(el.className || ""));
      if (key && collapsed[key] === null) {
        collapsed[key] = false;
        foldable[key] = false;
        if (summaryText[key] === null) {
          const small = el.querySelector(".small, .summary");
          summaryText[key] = small ? String(small.textContent || "").trim() : headTxt.trim();
        }
      }
    }
  }
  const analysisPanel = document.querySelector("#analysis-panel")
    || document.querySelector('[data-testid="analysis-panel"]');
  // ---- ECharts 实例尺寸 ----
  let echartsWidth = 0, echartsHeight = 0;
  try {
    if (inst) { echartsWidth = inst.getWidth(); echartsHeight = inst.getHeight(); }
  } catch (e) {}
  return {
    viewport: { w: window.innerWidth, h: window.innerHeight,
                dpr: window.devicePixelRatio || 1 },
    clientH: de.clientHeight, scrollH: de.scrollHeight,
    clientW: de.clientWidth, scrollW: de.scrollWidth,
    bodyClientH: bd.clientHeight, bodyScrollH: bd.scrollHeight,
    chartRoot: rect(root),
    chartRootScrollH: root ? root.scrollHeight : null,
    chartRootClientH: root ? root.clientHeight : null,
    chartCol: rect(chartCol),
    infoCol: rect(infoCol),
    infoColDeclared,
    infoColScrollH: infoCol ? infoCol.scrollHeight : null,
    infoColClientH: infoCol ? infoCol.clientHeight : null,
    infoCards: cards,
    grids,
    gridCount: gridsRaw.length,
    titleTexts, titleTops, titleTopsPct,
    legendData,
    legendSelected,
    seriesByGrid,
    seriesCount: series.length,
    candlestickAxes,
    collapsed,
    collapsedDetails: detailsInfo,
    foldable,
    summaryText,
    analysisPanelRect: rect(analysisPanel),
    echartsWidth, echartsHeight,
    events: (window.__cdpEvents || []).slice(),
    now: Date.now(),
  };
})()`;

const HOOK_JS = String.raw`(() => {
  window.__cdpEvents = window.__cdpEvents || [];
  const root = document.querySelector("#chart-root");
  const inst = (root && window.echarts) ? window.echarts.getInstanceByDom(root) : null;
  if (!inst) return false;
  if (window.__cdpHookedInst === inst) return true;
  window.__cdpHookedInst = inst;
  try {
    inst.on("legendselectchanged", (e) => {
      window.__cdpEvents.push("legendselectchanged:" + ((e && e.name) || ""));
    });
  } catch (e) {}
  return true;
})()`;

const CLICK_SETUP_JS = (name) => String.raw`(() => {
  const root = document.querySelector("#chart-root");
  const inst = (root && window.echarts) ? window.echarts.getInstanceByDom(root) : null;
  if (!inst) return { mode: "none", reason: "no chart instance" };
  // 在 zrender 展示列表里找 legend 项（文字 == name 的那一格）→ 真实鼠标点击坐标
  const zr = inst.getZr();
  const found = [];
  const walk = (el, depth) => {
    if (!el || depth > 8) return;
    const style = el.style || {};
    const txt = style.text;
    if (txt !== undefined && String(txt) === String("${name}")) {
      try {
        const r = el.getBoundingRect();
        const p = el.transformCoordToGlobal(r.x + r.width / 2, r.y + r.height / 2);
        found.push(p);
      } catch (e) {}
    }
    const kids = el.children ? el.children() : [];
    for (const k of kids) walk(k, depth + 1);
  };
  try {
    const lm = inst.getModel().getComponent("legend");
    const view = inst.getViewOfComponentModel(lm);
    if (view && view.group) walk(view.group, 0);
  } catch (e) {}
  if (!found.length) {
    // 兜底：走 ECharts action（会触发同一个 legendselectchanged 事件，但不是真鼠标）
    inst.dispatchAction({ type: "legendToggleSelect", name: "${name}" });
    return { mode: "action" };
  }
  // ★ zrender 的全局坐标是**相对画布**的；Input.dispatchMouseEvent 要**视口**坐标 ⇒ 加画布偏移
  const canvas = root.querySelector("canvas") || root;
  const cr = canvas.getBoundingClientRect();
  return { mode: "real", x: found[0][0] + cr.left, y: found[0][1] + cr.top,
           canvasRect: { left: cr.left, top: cr.top, w: cr.width, h: cr.height } };
})()`;

async function waitFor(expression, timeoutMs, intervalMs = 200) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try { last = await evaluate(expression); } catch (e) { last = null; }
    if (last) return true;
    await sleep(intervalMs);
  }
  return false;
}

async function measureOne(req) {
  const [vw, vh] = req.viewport;
  await send("Emulation.setDeviceMetricsOverride", {
    width: vw, height: vh, deviceScaleFactor: 1, mobile: false,
  });
  // ★ 每次测量前清掉 **localStorage**：V4 的折叠选择会写 localStorage（`ptrade.v4.collapse`），
  //   同一 Chrome profile 跨视口复用时会「上一个视口的用户选择」泄漏到下一次测量 ⇒ L6 不可复现。
  try {
    const u = new URL(req.url);
    await send("Storage.clearDataForOrigin",
               { origin: u.origin, storageTypes: "local_storage" });
  } catch (e) {}
  await evaluate("window.__cdpEvents = []; window.__cdpHookedInst = null;");
  await send("Page.navigate", { url: req.url });
  const loaded = await waitFor("document.readyState === 'complete'", 20000);
  if (!loaded) return { ok: false, error: "页面 20s 未加载完成（readyState != complete）" };

  // 等应用骨架出现
  const booted = await waitFor("!!document.querySelector('#chart-root')", 15000);
  if (!booted) return { ok: false, error: "页面无 #chart-root（应用骨架没起来）" };

  if (req.triggerChart !== false) {
    // 触发一次日线图：填代码 → 点日线（选择器兜底：v3 与 v4 的 id 可能不同）
    const codeSelectors = JSON.parse(req.codeSelectorsJson);
    const dailySelectors = JSON.parse(req.dailySelectorsJson);
    await evaluate(`(() => {
      const sels = ${JSON.stringify(codeSelectors)};
      const codes = ${JSON.stringify(["600519"])};
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) { el.value = codes[0];
          el.dispatchEvent(new Event("input", {bubbles: true}));
          el.dispatchEvent(new Event("change", {bubbles: true})); return "input:" + sel; }
      }
      return "no-code-input";
    })()`);
    await evaluate(`(() => {
      const sels = ${JSON.stringify(dailySelectors)};
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) { el.click(); return "click:" + sel; }
      }
      if (typeof window.loadChart === "function") { window.loadChart("daily"); return "call:loadChart"; }
      return "no-daily-trigger";
    })()`);
  }

  const ready = await waitFor(`(() => {
    const root = document.querySelector("#chart-root");
    if (!root || !window.echarts) return false;
    const inst = window.echarts.getInstanceByDom(root);
    if (!inst) return false;
    try { const o = inst.getOption(); return !!(o && o.series && o.series.length); }
    catch (e) { return false; }
  })()`, req.timeoutMs || 30000);
  if (!ready) return { ok: false, error: "图表 30s 内未完成首帧（无 echarts 实例或 series 为空）" };

  await evaluate(HOOK_JS);
  if (req.waitAnalysisReady) {
    // L6 量的是**稳态**折叠默认值：等分析真正落地（pending → ready/failed）再量，
    // 否则「分析到达后自动展开」这类行为会被早测漏掉。
    await waitFor(`(() => {
      const el = document.querySelector("#analysis-panel");
      if (!el) return true;
      const st = el.getAttribute("data-analysis-state");
      return (el.textContent || "").trim().length > 0 || st === "ready" || st === "failed";
    })()`, 25000);
    await evaluate(HOOK_JS);
  }
  if (req.waitAfterInitMs) await sleep(req.waitAfterInitMs);
  else await sleep(req.settleMs || 1200);

  let clickInfo = null;
  if (req.clickLegend) {
    clickInfo = await evaluate(CLICK_SETUP_JS(req.clickLegend));
    if (clickInfo && clickInfo.mode === "real") {
      const x = clickInfo.x, y = clickInfo.y;
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none", clickCount: 0 });
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
      await sleep(400);
    }
  }
  await evaluate(HOOK_JS);
  const data = await evaluate(MEASURE_JS);
  data.legendClick = clickInfo;
  return { ok: true, data };
}

createInterface({ input: process.stdin, terminal: false }).on("line", async (line) => {
  let req;
  try { req = JSON.parse(line); } catch (e) { out({ ok: false, error: "bad request json" }); return; }
  try {
    if (req.cmd === "ping") { out({ ok: true, pong: true }); return; }
    if (req.cmd === "measure") { out(await measureOne(req)); return; }
    if (req.cmd === "shutdown") {
      try { await send("Emulation.clearDeviceMetricsOverride"); } catch (e) {}
      try { ws.close(); } catch (e) {}
      try { chrome.kill(); } catch (e) {}
      out({ ok: true, bye: true });
      setTimeout(() => process.exit(0), 50);
      return;
    }
    out({ ok: false, error: "unknown cmd: " + req.cmd });
  } catch (e) {
    out({ ok: false, error: String((e && e.message) || e).slice(0, 500) });
  }
});
"""


class CDPUnavailable(RuntimeError):
    """本机没有可用的 Chrome/node —— 调用方必须 skip 并**显式记录**。"""


def find_chrome() -> str | None:
    """返回 Chrome 可执行文件；找不到 → ``None``。"""
    for cand in _CHROME_CANDIDATES:
        if cand and pathlib.Path(cand).is_file():
            return cand
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_node() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def status(probe: bool = False) -> dict:
    """报告 harness 可用性（**供测试 skip 时记录原因 + 报告「未验证」清单**）。"""
    chrome = find_chrome()
    node = _find_node()
    info = {
        "available": bool(chrome and node),
        "chrome": chrome,
        "node": node,
        "reason": None,
        "emulation_domain_required": "Emulation.setDeviceMetricsOverride",
    }
    if not chrome:
        info["reason"] = "找不到 Chrome 可执行文件（可用 PTRADE_CHROME 环境变量指定）"
    elif not node:
        info["reason"] = "找不到 node（CDP 驱动需要 node ≥22 的全局 WebSocket）"
    elif probe:
        try:
            sess = _session()
            pong = sess.cmd({"cmd": "ping"})
            info["ping"] = pong
        except Exception as exc:  # noqa: BLE001
            info["available"] = False
            info["reason"] = f"CDP 驱动启动失败：{type(exc).__name__}: {exc}"
    # ★ 记录到临时文件，便于报告里单列「未验证」
    try:
        marker = pathlib.Path(tempfile.gettempdir()) / "ptrade-v4-cdp-status.json"
        marker.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return info


class _Session:
    """常驻 node 驱动 + Chrome（单例）。"""

    def __init__(self) -> None:
        chrome = find_chrome()
        node = _find_node()
        if not chrome or not node:
            raise CDPUnavailable(status()["reason"] or "CDP 不可用")
        self._tmp = tempfile.mkdtemp(prefix="ptrade-v4-cdpsess-")
        self._driver = pathlib.Path(self._tmp) / "cdp_driver.mjs"
        self._driver.write_text(_DRIVER_JS, encoding="utf-8")
        self._port = _free_port()
        self._proc = subprocess.Popen(  # noqa: S603 - 本地受控可执行文件
            [node, str(self._driver), chrome, str(self._port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=self._tmp,
        )
        hello = self._read(timeout=60.0)
        if not hello or not hello.get("ok"):
            stderr = ""
            with contextlib.suppress(Exception):
                stderr = (self._proc.stderr.read() or "")[-1500:]
            raise CDPUnavailable(
                f"CDP 驱动未就绪：{(hello or {}).get('fatal') or (hello or {}).get('error')} "
                f"stderr={stderr}"
            )
        self._lock = threading.Lock()

    # -- 协议 ------------------------------------------------------------
    def _read(self, timeout: float = SESSION_TIMEOUT_S) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if line:
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except ValueError:
                    continue
            if self._proc.poll() is not None:
                return None
            time.sleep(0.02)
        return None

    def cmd(self, payload: dict, timeout: float = SESSION_TIMEOUT_S) -> dict:
        with self._lock:
            if self._proc.poll() is not None:
                raise CDPUnavailable("CDP 驱动进程已退出")
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            reply = self._read(timeout=timeout)
        if reply is None:
            raise CDPUnavailable(f"CDP 超时（{timeout:.0f}s）：{payload.get('cmd')}")
        return reply

    def measure(self, viewport: tuple[int, int], **opts) -> dict:
        # 测试侧用 snake_case，驱动侧用 camelCase —— 在这里统一翻译
        camel = {
            "click_legend": "clickLegend",
            "wait_after_init_ms": "waitAfterInitMs",
            "wait_analysis_ready": "waitAnalysisReady",
            "settle_ms": "settleMs",
            "trigger_chart": "triggerChart",
            "timeout_ms": "timeoutMs",
        }
        norm: dict = {}
        for key, value in opts.items():
            norm[camel.get(key, key)] = value
        req = {
            "cmd": "measure",
            "viewport": [int(viewport[0]), int(viewport[1])],
            "url": norm.pop("url"),
            "codeSelectorsJson": json.dumps(list(_CODE_INPUTS)),
            "dailySelectorsJson": json.dumps(list(_CHART_DAILY_SELECTORS)),
            **norm,
        }
        reply = self.cmd(req, timeout=float(norm.get("timeoutMs", 30000)) / 1000.0 + 45.0)
        if not reply.get("ok"):
            raise CDPUnavailable(f"CDP measure 失败：{reply.get('error')}")
        data = reply["data"]
        data["_emulation_ok"] = (
            int(data.get("viewport", {}).get("w", -1)) == int(viewport[0])
            and int(data.get("viewport", {}).get("h", -1)) == int(viewport[1])
        )
        return data

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.cmd({"cmd": "shutdown"}, timeout=10.0)
        with contextlib.suppress(Exception):
            self._proc.kill()
        with contextlib.suppress(Exception):
            shutil.rmtree(self._tmp, ignore_errors=True)


_SESSION: _Session | None = None


def _session() -> _Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _Session()
    return _SESSION


def measure(viewport: tuple[int, int], **opts) -> dict:
    """在**真 Chrome** 里量 ``viewport`` 下的布局（Emulation 域改视口）。

    :param viewport: ``(width, height)``；必走 ``Emulation.setDeviceMetricsOverride``。
    :param opts: ``url``（必填，被测站点的 base URL）· ``click_legend`` · ``wait_after_init_ms``
        · ``settle_ms`` · ``trigger_chart``（默认 True）· ``timeout_ms``
    :return: 见 ``_DRIVER_JS`` 的 ``MEASURE_JS``（viewport/clientH/scrollH/chartRoot/infoCol/
        infoCards/grids/titleTexts/titleTops/legendData/seriesByGrid/candlestickAxes/
        collapsed/summaryText/legendSelected/events/analysisPanelRect/echartsWidth…
        + ``_emulation_ok``：**请求视口 == 实测 innerWidth/innerHeight**）
    """
    if "url" not in opts:
        raise ValueError("measure() 必须给 url=")
    data = _session().measure(viewport, **opts)
    # ★ 自证：Emulation 域真的生效了（请求视口 == innerWidth/innerHeight）
    if not data.get("_emulation_ok"):
        raise CDPUnavailable(
            f"Emulation 未生效：请求 {viewport} 实测 {data.get('viewport')}"
        )
    return data


def close() -> None:
    """关闭常驻会话（pytest session 结束时自动调用）。"""
    global _SESSION
    if _SESSION is not None:
        with contextlib.suppress(Exception):
            _SESSION.close()
        _SESSION = None


atexit.register(close)


@contextlib.contextmanager
def serve(cfg, offline: bool = True):
    """在**随机端口**起一个本站服务（不碰 8765），yield base URL。"""
    from ptrade import webapp  # 延迟导入：本模块在无浏览器环境下也要能 import

    server = webapp.create_server(cfg, host="127.0.0.1", port=0, offline=offline)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        with contextlib.suppress(Exception):
            server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()
        thread.join(timeout=10)
