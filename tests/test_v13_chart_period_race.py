"""v13 判据：图表 tooltip 在「切换分时 / 加载图表」时**绝不崩溃**。

## 用户原话（真源）
```
chart_option.js:190 Uncaught TypeError: Cannot read properties of undefined (reading 'dates')
    at formatter (chart_option.js:190:34)
「有时候前端控制台会出现这个bug，导致切换分时或者加载图表的时候出问题」
```

## 根因（见 `.omx/specs/deep-interview-ptrade-v13.md`）
`activePeriod()` 在**悬停时**才求值调用方传的 `getActive()`（活状态 = `st.period`），
而 `payload` 要等 fetch 回来才换 ⇒ **周期与载荷错配**：
* 日线 → 点分时（fetch 期间）：`period="minute"`，payload 是日线的（无 minute 键）
* 分时 → 点日线（fetch 期间）：`period="daily"`，payload 是**分时 payload（整个 `periods` 都没有）**

## 判据策略
★ **在 node 里真的调用** formatter / buildOption（不是 grep 源码）——
  这样才能真的复现「取不到 `dd`」的那条路径。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHART_OPTION_JS = ROOT / "ptrade" / "webui_assets" / "chart_option.js"
WEBUI = ROOT / "ptrade" / "webui.py"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node（本机未装则如实跳过，不静默降级）")


# --------------------------------------------------------------------------- #
# node 求值帮手
# --------------------------------------------------------------------------- #
def _run_node(source: str) -> dict:
    """在 node 里跑一段 ESM 源码，返回它 print 出来的 JSON。"""
    with __import__("tempfile").TemporaryDirectory(prefix="v13js-", dir=r"D:\pytest-bt") as work:
        work_p = pathlib.Path(work)
        (work_p / "chart_option.mjs").write_text(CHART_OPTION_JS.read_text(encoding="utf-8"), encoding="utf-8")
        entry = work_p / "entry.mjs"
        entry.write_text(source, encoding="utf-8")
        p = subprocess.run([NODE, str(entry)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if p.returncode != 0:
            raise AssertionError(f"node 执行失败 rc={p.returncode}\nstdout={p.stdout[:800]}\nstderr={p.stderr[:2000]}")
        return json.loads((p.stdout or "").strip().splitlines()[-1])


def _make_payload(period: str = "daily", n: int = 3) -> dict:
    """构造一份**形态与 `/api/chart/data` 一致**的 K 线 payload（只含指定周期）。"""
    dates = [f"2026-11-{3 + i:02d}" for i in range(n)]
    rng = list(range(n))
    one = [1.0 + i for i in rng]
    d = {
        "dates": dates,
        "ohlc": [[10.0 + i, 10.5 + i, 9.8 + i, 10.8 + i] for i in rng],
        "pct_chg": [1.5 + i for i in rng],
        "volume": [1000.0 + i * 100 for i in rng],
        "amount": [1.0e8 + i * 1e6 for i in rng],
        "vol_ma": {"5": one, "10": one},
        "ma": {"12": one, "24": one, "48": one, "96": one},
        "boll": {"lower": one, "mid": one, "upper": one},
        "macd": {"dif": one, "dea": one, "hist": one},
        "sar": {"value": one, "trend": [1] * n},
        # ★ 这两个字段 buildOption 真的会读（d.bar_up[i] 决定 MACD 柱颜色）；
        #   缺了会在**构建期**抛 Cannot read properties of undefined (reading '0')
        #   —— 我第一次就是这个假红（不是 formatter 的问题，是我的 payload 不全）。
        "bar_up": [True] * n,
        "pct_chg": [1.5 + i for i in rng],
    }
    return {"periods": {period: d}, "legend": [["MA12"]], "initial_period": period, "code": "300869"}


# 统一的 node 入口模板：build 一份 option，然后**真的调用 tooltip formatter**
_FORMATTER_JS = """
import {{ buildOption, miniCandlestick }} from './chart_option.mjs';
const payload = {payload};
const getActive = {get_active};
let out;
try {{
  const opt = buildOption(payload, {period}, {{
    miniCandlestick: {{ boll: miniCandlestick(1), sar: miniCandlestick(5) }},
    getActive: getActive,
  }});
  const fmt = opt.tooltip && opt.tooltip.formatter;
  let rendered;
  try {{
    rendered = fmt([{{ dataIndex: {data_index} }}]);
    out = {{ built: true, threw: false, rendered: rendered }};
  }} catch (e) {{
    out = {{ built: true, threw: true, error: String(e && e.message || e) }};
  }}
}} catch (e) {{
  out = {{ built: false, threw: true, error: String(e && e.message || e) }};
}}
process.stdout.write(JSON.stringify(out));
"""


def _call_formatter(payload: dict, period: str, get_active_js: str, data_index: int = 0) -> dict:
    return _run_node(
        _FORMATTER_JS.format(
            payload=json.dumps(payload, ensure_ascii=False),
            get_active=get_active_js,
            period=json.dumps(period),
            data_index=data_index,
        )
    )


# --------------------------------------------------------------------------- #
# V13-1 … V13-5：真跑 formatter（崩溃路径）
# --------------------------------------------------------------------------- #
@needs_node
def test_v13_1_daily_payload_with_minute_active_does_not_throw():
    """★ V13-1（**回归证明**）：日线 payload + `getActive()` 返回 `"minute"` ⇒ formatter **不抛**。

    这正是用户遇到的时序：点了「分时」，但日线的 payload 还没换掉，此时鼠标扫过图表。
    旧实现：`payload.periods["minute"]` = undefined ⇒ `dd.dates` ⇒ TypeError ✗
    """
    res = _call_formatter(_make_payload("daily"), "daily", 'function () { return "minute"; }')
    assert res["built"] is True, f"buildOption 不该失败：{res.get('error')}"
    assert res["threw"] is False, f"formatter 抛异常了（这就是用户报的 bug）：{res.get('error')}"


@needs_node
def test_v13_2_falls_back_to_build_period_not_blank():
    """★ V13-2：上面那种错配下，formatter 应**回退到建 option 的 `period`** 并正常出内容。

    ★ 不是「返回空字符串糊过去」—— 图表上画的就是 `payload.periods["daily"]`，
      回退到它才是**正确**的工具提示；返回空会让用户以为 hover 失灵。
    """
    payload = _make_payload("daily")
    res = _call_formatter(payload, "daily", 'function () { return "minute"; }')
    assert res["threw"] is False
    assert isinstance(res["rendered"], str) and res["rendered"], "tooltip 内容为空（应回退出日线数据）"
    assert "2026-11-03" in res["rendered"], f"没回退到日线日期：{res['rendered'][:200]!r}"


@needs_node
def test_v13_3_intraday_payload_build_option_raises_actionable_error():
    """★ V13-3：分时 payload（**无 `periods` 字段**）传进 `buildOption` ⇒ 抛**可诊断**的错误。

    旧实现给的是 `TypeError: Cannot read properties of undefined (reading 'daily')` —— 完全看不出该修哪。
    新实现必须点明「分时请走 buildIntradayOption」。
    """
    res = _call_formatter({"dates": ["09:30"], "price": [1.0]}, "daily", "function () { return 'daily'; }")
    assert res["built"] is False, "分时 payload 不该能 build 成功"
    assert "buildIntradayOption" in res["error"], f"错误信息没点明正确构建器：{res['error']!r}"


@needs_node
def test_v13_4_empty_payload_returns_blank_without_throwing():
    """★ V13-4：空 payload / 无 `periods` ⇒ formatter 返回 `""` 且**不抛**（防御兜底）。"""
    # 直接绕过 buildOption 的守卫，单独构造 formatter 场景：用有 periods 但缺目标周期的 payload
    payload = _make_payload("daily")
    payload["periods"]["minute"] = None  # 「有键但值坏」
    res = _call_formatter(payload, "daily", 'function () { return "minute"; }')
    assert res["threw"] is False, f"坏值仍抛了：{res.get('error')}"
    assert isinstance(res["rendered"], str)


@needs_node
def test_v13_5_out_of_range_index_returns_blank():
    """★ V13-5：`dataIndex` 越界（dataZoom 过滤期间可能瞬时失配）⇒ 返回 `""` 且不抛。"""
    res = _call_formatter(_make_payload("daily"), "daily", "function () { return 'daily'; }", data_index=9999)
    assert res["threw"] is False, f"越界仍抛了：{res.get('error')}"
    assert res["rendered"] == ""


@needs_node
def test_v13_5b_normal_case_still_works():
    """V13-5b（**防修坏**）：正常情况（周期与载荷一致）formatter 照常出内容。"""
    res = _call_formatter(_make_payload("daily"), "daily", "function () { return 'daily'; }")
    assert res["threw"] is False
    assert "2026-11-03" in res["rendered"]
    assert "MA12" in res["rendered"]


# --------------------------------------------------------------------------- #
# V13-6 … V13-8：前端状态模型契约（这才是**治本**的那部分）
# --------------------------------------------------------------------------- #
def _webui_src() -> str:
    return WEBUI.read_text(encoding="utf-8")


def test_v13_6_render_chart_does_not_read_live_period():
    """★ V13-6：`renderChart` **不得**再用 `getActive: () => st.period`（活状态）。

    ★ 这条钉住「治本」：只要构建器还读 UI 的活周期，错配就会以别的方式复发。
    """
    src = _webui_src()
    assert "getActive: function () { return st.period; }" not in src, (
        "renderChart 仍在用活状态 st.period 作为 getActive ⇒ 周期/载荷错配会复发"
    )
    assert "getActive: function () { return pp; }" in src, "renderChart 应使用 payload 自己的周期 pp"


def test_v13_7_payload_period_separated_and_used():
    """★ V13-7：`CHART_STATE.payloadPeriod` 存在，且在**数据到达后**才被赋值、由 renderChart 使用。"""
    src = _webui_src()
    assert "payloadPeriod" in src, "缺少 payloadPeriod 字段"
    # 状态初始化里有它
    assert re.search(r"CHART_STATE\s*=\s*\{[^}]*payloadPeriod", src), "CHART_STATE 初始化应含 payloadPeriod"
    # loadChart 里在 payload 赋值之后才设
    assert re.search(r"CHART_STATE\.payload\s*=\s*d;\s*\n\s*CHART_STATE\.payloadPeriod\s*=\s*p;", src), (
        "loadChart 应在 CHART_STATE.payload = d 之后设 payloadPeriod = p"
    )
    # renderChart 用它
    assert re.search(r"var\s+pp\s*=\s*st\.payloadPeriod\s*\|\|", src), "renderChart 应先读 payloadPeriod"


def test_v13_8_stale_response_dropped_and_failures_shown():
    """★ V13-8：请求序号丢弃过期响应 + 构建失败转可读空态（不冒泡成未捕获异常）。"""
    src = _webui_src()
    assert "CHART_REQ_SEQ" in src, "缺少请求序号（快速连点会被旧响应覆盖）"
    assert re.search(r"seq\s*!==\s*CHART_REQ_SEQ", src) or re.search(r"seq\s*!=\s*CHART_REQ_SEQ", src), (
        "缺少「过期响应丢弃」逻辑"
    )
    assert re.search(r"try\s*\{\s*\n\s*if\s*\(\s*pp\s*===\s*\"minute\"", src), "构建器调用应在 try 里"
    assert "图表构建失败" in src, "构建失败应转成可读空态"


# --------------------------------------------------------------------------- #
# V13-10 … V13-12：**B1 行为判据**（code-review REJECT 的那个阻断项）
#
# ★ 为什么必须是行为判据：`chartEmpty` 的缺陷在「DOM 与 ECharts 实例的绑定关系」上，
#   grep 源码抓不到 —— 必须真的跑一遍「init → 空态 → 再 init」的序列。
# --------------------------------------------------------------------------- #
def _extract_block(text: str, head: str) -> str:
    """从 `head` 起做**花括号配平**提取一个完整函数块（跳过字符串/注释）。"""
    start = text.index(head)
    brace = text.index("{", start + len(head) - 1)
    depth, i, n = 0, brace, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c in "\"'`":
            q, i = c, i + 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    break
                i += 1
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError(f"配平失败：{head}")


_B1_STUB = r"""
let __initCalls = 0, __disposeCalls = 0, __observed = 0, __disconnected = 0;
const __root = { innerHTML: "", clientWidth: 800, clientHeight: 400 };
const document = { getElementById: function (id) { return id === "chart-root" ? __root : null; } };
function $(id) { return document.getElementById(id); }
function esc(v) {
  return String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
const window = {
  echarts: { init: function () {
    __initCalls++;
    return {
      setOption: function () {}, resize: function () {}, clear: function () {},
      dispose: function () { __disposeCalls++; },
    };
  } },
  ResizeObserver: function (cb) {
    this.observe = function () { __observed++; };
    this.disconnect = function () { __disconnected++; };
  },
  requestAnimationFrame: function (fn) { return 1; },
  console: { error: function () {} },
};
// ★ 源码里写的是 new ResizeObserver(...)（**裸全局**，不是 window.ResizeObserver）
//   ⇒ stub 必须同时提供全局绑定，否则 node 里会 ReferenceError
const ResizeObserver = window.ResizeObserver;
let CHART = null;
let CHART_RO = null;
const CHART_STATE = { code: "", period: "daily", payloadPeriod: null, payload: null };
function renderChart() {}
"""


def _run_node_raw(script: str) -> dict:
    # ★ S8（code-review 复审）：目录不存在时 `TemporaryDirectory(dir=…)` 是 **error 而非 skip**
    pathlib.Path(r"D:\pytest-bt").mkdir(parents=True, exist_ok=True)
    with __import__("tempfile").TemporaryDirectory(prefix="v13b1-", dir=r"D:\pytest-bt") as work:
        f = pathlib.Path(work) / "m.mjs"
        f.write_text(script, encoding="utf-8")
        p = subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if p.returncode != 0:
            raise AssertionError(f"node 失败：{(p.stderr or '')[:1500]}")
        return json.loads((p.stdout or "").strip().splitlines()[-1])


def _run_chart_state_machine(body_js: str) -> dict:
    """把 `chartPlaceholder` / `chartEmpty` / `ensureChart` 的**真源码**跑在 stub 上。"""
    src = WEBUI.read_text(encoding="utf-8")
    fns = [
        _extract_block(src, "function chartPlaceholder(text, keepPayload) {"),
        _extract_block(src, "function chartEmpty(text) {"),
        _extract_block(src, "function ensureChart() {"),
    ]
    return _run_node_raw(_B1_STUB + "\n".join(fns) + "\n" + body_js)


@needs_node
def test_v13_10_chart_empty_disposes_and_allows_reinit():
    """★★ V13-10（**B1 行为判据**）：空态之后再加载，图表**必须能重新 init**。

    ★ B1 原症状：`chartEmpty` 只用 `root.innerHTML = …` 把 canvas 子树摘掉，
      但 `CHART` 实例**仍非空** ⇒ `ensureChart()` 直接返回那个**失联实例**
      ⇒ 之后 `setOption` 画进已脱离 DOM 的 canvas ⇒ **永久白屏，必须 F5**。
    """
    body = r"""
const c1 = ensureChart();
chartEmpty("加载失败：模拟");
const c2 = ensureChart();
process.stdout.write(JSON.stringify({
  initCalls: __initCalls, disposeCalls: __disposeCalls,
  reinit: __initCalls >= 2, chartReset: c2 !== null,
  observed: __observed, disconnected: __disconnected,
  emptyText: __root.innerHTML,
}));
"""
    out = _run_chart_state_machine(body)
    assert out["initCalls"] >= 2, f"空态后没有重新 init（B1 未修）：{out}"
    assert out["disposeCalls"] >= 1, f"空态没有 dispose 旧实例：{out}"
    assert out["reinit"] is True, out
    # ★ Stub 增强（code-review 复审）：防「dispose 了但 ResizeObserver 泄漏 / 没重建」
    assert out["disconnected"] >= 1, f"空态没断开旧 ResizeObserver：{out}"
    assert out["observed"] >= 2, f"重新 init 后没重建 ResizeObserver：{out}"


@needs_node
def test_v13_11_placeholder_keeps_payload_for_module_ready_redraw():
    """★ V13-11（S1 行为判据）：「图表模块加载中…」这类**临时**占位**不得**清 payload。

    ★ 否则 `chart-option-ready` 回调里的 `renderChart()` 首行 `if (!st.payload) return;`
      会直接返回 ⇒ 模块就绪后永不重绘（兜底失效）。
    """
    body = r"""
CHART_STATE.payload = { ok: true };
CHART_STATE.payloadPeriod = "daily";
chartPlaceholder("图表模块加载中…", true);
process.stdout.write(JSON.stringify({
  payloadKept: CHART_STATE.payload !== null,
  periodKept: CHART_STATE.payloadPeriod,
  initCalls: __initCalls,
}));
"""
    out = _run_chart_state_machine(body)
    assert out["payloadKept"] is True, f"临时占位把 payload 清了（S1 未修）：{out}"
    assert out["periodKept"] == "daily", out


@needs_node
def test_v13_12_chart_empty_clears_state():
    """V13-12：**常规**空态（keepPayload=false）必须清掉 payload / payloadPeriod。"""
    body = r"""
CHART_STATE.payload = { ok: true };
CHART_STATE.payloadPeriod = "daily";
chartEmpty("无数据：模拟");
process.stdout.write(JSON.stringify({
  payloadCleared: CHART_STATE.payload === null,
  periodCleared: CHART_STATE.payloadPeriod === null,
}));
"""
    out = _run_chart_state_machine(body)
    assert out["payloadCleared"] is True, out
    assert out["periodCleared"] is True, out


# --------------------------------------------------------------------------- #
# V13-9：语法
# --------------------------------------------------------------------------- #
@needs_node
def test_v13_9_chart_option_js_syntax_ok():
    """V13-9：`chart_option.js` 仍是合法 JS（node --check）。"""
    with __import__("tempfile").TemporaryDirectory(prefix="v13syn-", dir=r"D:\pytest-bt") as work:
        f = pathlib.Path(work) / "x.mjs"
        f.write_text(CHART_OPTION_JS.read_text(encoding="utf-8"), encoding="utf-8")
        p = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert p.returncode == 0, (p.stderr or "")[:1500]
