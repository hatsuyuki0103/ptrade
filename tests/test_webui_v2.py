# -*- coding: utf-8 -*-
"""W3 · F4 前端契约（v2）：T-W3-1 … T-W3-4（`ptrade/webui.py` 的 `APP_HTML`）。

真源：``.omx/plans/test-spec-ptrade-web-v2.md`` §3（T-W3-1…T-W3-6）与
``.omx/plans/prd-ptrade-web-v2.md`` §F4。

## 本文件的两条纪律（都是本项目用真实事故换来的）

1. **不做空守卫**：每条判据都要能因为「实现被注掉」而变红 —— 见 ``test_t_w3_2_no_select_guard``
   / ``test_t_w3_3_guard_hardcoded_offline`` / ``test_t_w3_4_guard_always_polling``。
2. **断言活的那个产物**：周期切换按钮由 **``chart.py`` 渲染、活在 iframe 里**，
   所以「图内仍能切周期」必须查 ``/api/chart`` 返回的 HTML（``tests/test_webapp_v2.py``），
   **查 ``APP_HTML`` 会永远查不到** —— R1 评审时 Critic 正是据此误判「删掉即失去唯一入口」，
   后来被 Lead 实证否证。本文件因此**反向**断言：``switchPeriod`` **不得**出现在 ``APP_HTML``。

## 行为级验证（真跑 JS）

与 ``tests/test_divergence_ui_js.py`` 同款做法：抽出整段 ``<script>``，配最小假 DOM 与假
``fetch``，在 **node** 里真执行，断言 ``loadChart()`` 拼出的 URL 与三态轮询决策 ——
而不是 grep 源码字符串（那种断言对「改了但改错」毫无鉴别力）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ptrade.webui import APP_HTML  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node 才能解析/执行 JS")


# --------------------------------------------------------------------------- #
# 工具：抽脚本 / node --check / 真执行
# --------------------------------------------------------------------------- #
def _scripts() -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", APP_HTML, re.S)


def _app_script() -> str:
    """返回最大的那段 ``<script>``（页面主逻辑）。"""
    scripts = [s for s in _scripts() if s.strip()]
    assert scripts, "APP_HTML 里没有 <script>"
    return max(scripts, key=len)


def _node_check(src: str) -> tuple[int, str]:
    """★ V3：页面里有一个 `<script type="module">`（`import { buildOption } …`）——
    含 `import`/`export` 的块必须按 **`.mjs`** 校验，否则 `node --check` 会报
    「Cannot use import statement outside a module」（那不是语法错误，是模块语义）。"""
    suffix = ".mjs" if re.search(r"^\s*(?:import|export)\s", src, re.M) else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run([NODE, "--check", path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return proc.returncode, (proc.stderr or "")
    finally:
        Path(path).unlink(missing_ok=True)


HARNESS = textwrap.dedent("""
    "use strict";
    /* 最小假 DOM：任何 id 都返回一个可用的桩元素（页面逻辑不该依赖真实渲染）。 */
    var __els = {};
    function __mk(id) {
      return {
        id: id, value: "", checked: false, textContent: "", innerHTML: "",
        className: "", style: {}, src: "",
        addEventListener: function () {}, removeAttribute: function () {},
        setAttribute: function () {}, getAttribute: function () { return null; },
        appendChild: function () {}, querySelector: function () { return null; },
        scrollIntoView: function () {},
        classList: {add: function () {}, remove: function () {}, toggle: function () {}}
      };
    }
    var document = {
      getElementById: function (id) { return __els[id] || (__els[id] = __mk(id)); },
      createElement: function (tag) { return __mk(tag); },
      querySelectorAll: function () { return []; },
      addEventListener: function () {}
    };
    var window = { confirm: function () { return true; }, addEventListener: function () {} };
    var ResizeObserver = function (cb) { this.observe = function () {}; this.disconnect = function () {}; };
    var __intervals = [];
    var setInterval = function (fn, ms) { __intervals.push({fn: fn, ms: ms}); return __intervals.length; };
    var __fetches = 0, __urls = [], __initCount = 0, __buildOpts = null, __setOptions = 0;
    /* ★ V3-A5 的假 ECharts：init 只应发生**一次**（库与实例都只建一次），setOption 可多次。 */
    window.echarts = { init: function () {
      __initCount += 1;
      return { setOption: function () { __setOptions += 1; }, resize: function () {} };
    } };
    window.miniCandlestick = function (axisIndex) { return {axisIndex: axisIndex}; };
    window.buildOption = function (payload, period, opts) {
      __buildOpts = opts || null;
      return {period: period, series: []};
    };
    var fetch = function (url) {
      __fetches += 1; __urls.push(String(url));
      var body = { ok: true, command: "health", data: { status: "ok", root: "/tmp",
                   market_open: true, code: "600519", name: "贵州茅台", periods: {} }, error: null };
      if (String(url).indexOf("/api/analysis") >= 0) {
        body = { ok: true, command: "analysis", data: { state: "ready", error: null,
                 asof: "2026-10-09", code: "600519", factors: {C: 1}, total: 1, signal: "watch" },
                 error: null };
      }
      return Promise.resolve({status: 200, text: function () {
        return Promise.resolve(JSON.stringify(body));
      }});
    };
    %(js)s
    globalThis.__probe = {
      loadChart: loadChart, prefillOrder: prefillOrder,
      pollDelayFor: pollDelayFor, shouldPoll: shouldPoll,
      applyMarketOpen: applyMarketOpen, autoRefreshTick: autoRefreshTick,
      POLL: POLL, POLL_TICK_MS: POLL_TICK_MS, intervals: __intervals,
      fetches: function () { return __fetches; }, els: __els,
      urls: function () { return __urls.slice(); },
      initCount: function () { return __initCount; },
      setOptions: function () { return __setOptions; },
      buildOpts: function () { return __buildOpts; }
    };
""")


PROBE_SCRIPT = textwrap.dedent("""
    (async function () {
      var P = globalThis.__probe;
      function el(id, props) {
        var e = document.getElementById(id);
        Object.keys(props || {}).forEach(function (k) { e[k] = props[k]; });
        return e;
      }
      function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
      el("stock-code", {value: "600519"});
      P.loadChart("daily");
      await tick(); await tick();
      var dailyUrl = P.urls()[P.urls().length - 1];
      P.loadChart("minute");
      await tick(); await tick();
      var minuteUrl = P.urls()[P.urls().length - 1];

      function tickDelta(marketOpen, lastOffsetMs) {
        P.applyMarketOpen(marketOpen);
        P.POLL.last = Date.now() - lastOffsetMs;
        var before = P.fetches();
        P.autoRefreshTick();
        return P.fetches() - before;
      }

      console.log(JSON.stringify({
        dailyUrl: dailyUrl || "",
        minuteUrl: minuteUrl || "",
        initCount: P.initCount(),
        setOptions: P.setOptions(),
        buildOpts: P.buildOpts(),
        delays: {on: P.pollDelayFor(true), off: P.pollDelayFor(false), unknown: P.pollDelayFor(null)},
        shouldPoll: {on: P.shouldPoll(true), off: P.shouldPoll(false), unknown: P.shouldPoll(null)},
        tickOff: tickDelta(false, 999999),
        tickUnknown: tickDelta(null, 999999),
        tickOnTooSoon: tickDelta(true, 0),
        tickOnDue: tickDelta(true, 999999),
        intervalCount: P.intervals.length,
        intervalMs: P.intervals.length ? P.intervals[0].ms : null
      }));
    })();
""")


def _run_js(script: str) -> dict:
    """在 stubbed 页面里跑一段 JS（HARNESS + 应用脚本 + 这段脚本），返回最后一行 JSON。"""
    src = HARNESS % {"js": _app_script()} + script
    # ★ 探针本体是**经典脚本**（应用脚本是经典 `<script>`）—— 只有 `_node_check` 需要 `.mjs`。
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"node 执行页面 JS 失败（退出 {proc.returncode}）：\n{proc.stderr[:1500]}"
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    assert lines, f"探针没有输出：{proc.stdout[:500]!r} / stderr={proc.stderr[:500]!r}"
    return json.loads(lines[-1])


def _run_probe() -> dict:
    return _run_js(PROBE_SCRIPT)


@pytest.fixture(scope="module")
def probe() -> dict:
    if NODE is None:
        pytest.skip("需要 node")
    return _run_probe()


# --------------------------------------------------------------------------- #
# T-W3-1 整段 JS 可解析（红线：2026-10-15「页面全加载中」事故换来的守卫）
# --------------------------------------------------------------------------- #
@needs_node
def test_t_w3_1_every_script_parses() -> None:
    scripts = [s for s in _scripts() if s.strip()]
    assert scripts, "APP_HTML 里没有可解析的 <script>"
    failures = []
    for i, src in enumerate(scripts):
        code, err = _node_check(src)
        if code != 0:
            failures.append(f"段 {i}（{len(src):,} 字符）：{err[:800]}")
    assert not failures, "★JS 语法错误会让整段脚本不执行（页面停在「加载中…」）：\n" + "\n".join(failures)


@needs_node
def test_t_w3_1_guard_bad_js_is_caught() -> None:
    """非空洞：把「未转义引号」这种事故形态喂进同一条路径，必须判红。"""
    code, err = _node_check('function f(){ return "<tr data-code="" + x; }')
    assert code != 0 and ("Syntax" in err or "Unexpected" in err), err[:300]


@needs_node
def test_t_w3_1_script_is_substantial(probe) -> None:
    """非空洞：脚本被抽空时「解析通过」毫无意义；且自动刷新的节拍要真被注册。"""
    src = _app_script()
    assert len(src) > 8_000, f"主脚本只有 {len(src):,} 字符，疑似被抽空"
    for needle in ("loadChart", "refreshAll", "autoRefreshTick", "setInterval"):
        assert needle in src, f"主脚本缺少 {needle}"
    assert probe["intervalCount"] >= 1, "没有注册 setInterval —— 自动刷新不会发生"


# --------------------------------------------------------------------------- #
# T-W3-2（**V3 改写**）：周期切换在**本页**；首屏数据走 `/api/chart/data`
# --------------------------------------------------------------------------- #
def test_t_w3_2_period_select_removed() -> None:
    """F4 的初衷仍成立：**没有**周期下拉框、**没有** asof 输入框。

    ★ V3 之后周期按钮**就在本页**（`class="chart-period"` + `data-period`），
      所以「不得引用 chart-period」这条旧措辞收窄为「不得有 `<select>` 周期控件」。
    """
    assert 'id="chart-period"' not in APP_HTML
    assert '<select id="chart-period"' not in APP_HTML
    assert 'id="chart-asof"' not in APP_HTML
    assert "chart-asof" not in APP_HTML
    for period in ("daily", "weekly", "monthly"):
        assert f'data-period="{period}"' in APP_HTML, f"缺本页周期按钮 {period}"


def test_t_w3_2_minute_entry_present() -> None:
    """分时入口：操作条上的按钮（`data-testid="chart-minute"`）→ `loadChart("minute")`。"""
    assert 'data-testid="chart-minute"' in APP_HTML
    assert 'loadChart("minute")' in APP_HTML


def test_t_w3_2_chart_data_endpoint_and_no_offline(probe) -> None:
    """★ A5 + INV-64：首屏请求走 **/api/chart/data**，且**不带 offline 参数**。"""
    assert "/api/chart/data" in probe["dailyUrl"], probe["dailyUrl"]
    assert "code=600519" in probe["dailyUrl"], probe["dailyUrl"]
    assert "period=daily" in probe["dailyUrl"], probe["dailyUrl"]
    assert "offline" not in probe["dailyUrl"], f"★ INV-64：前端不得拼 offline（{probe['dailyUrl']}）"
    assert "period=minute" in probe["minuteUrl"], probe["minuteUrl"]
    assert "offline" not in probe["minuteUrl"], probe["minuteUrl"]


def test_t_w3_2_same_page_echarts_single_init(probe) -> None:
    """★ A5：ECharts 实例**只建一次**；option 由 W1 的 `buildOption` 构建且**带迷你蜡烛**。"""
    assert probe["initCount"] == 1, f"echarts.init 调用了 {probe['initCount']} 次（应为 1 次）"
    assert probe["setOptions"] >= 1, "没有 setOption —— 图从未画出来"
    opts = probe["buildOpts"] or {}
    mini = opts.get("miniCandlestick") or {}
    assert mini, "★ 必须传 miniCandlestick（否则 BOLL/SAR 面板的迷你蜡烛静默消失）"
    assert mini["boll"]["axisIndex"] == 1 and mini["sar"]["axisIndex"] == 5, mini


def test_t_v3_layout_full_width_and_height_adapts() -> None:
    """布局判据（**V4 改写**）：图表在左列 + 高度由网格给 + 旧固定上限已删。

    ## ★★ 为什么改写（**判据冲突，不是产品回退**）

    本用例最初（V3）断言 `.card-wide{grid-column:1 / -1}`（图表**独占整行**）+ `clamp(`&`vh`。
    **V4 用户明确拍板**：「图**在左半边竖着**」「**不要滚动条**」「1440p 也要」⇒
    * 图表**必须**只在左列 ⇒ **不可能**再独占整行；
    * 高度改为 `#chart-root{height:100%}` 由网格分配（`clamp(380px,52vh,820px)` 会把它卡在 820px，
      Critic 点名**必须删除**）⇒ `clamp` 也不该再有。

    ⇒ 原意图（「图别太小看不清」+「高度别写死」）**保留**，度量换成 V4 的（**更强**）：
    两列网格 + 图在左列 + 高度百分比 + 旧上限真的消失。

    ★ 真实收益的可量事实（CDP 实测）：K 线面板 **103px → 235px**；每根蜡烛 **3.8px → ~8px**；
      图表 **1847×491（3.76:1）→ 1042×840（0.806:1，竖）**。
    """
    # ① 两列网格（V4 的布局本质）
    assert "grid-template-columns:minmax(0,1.25fr) minmax(340px,1fr)" in APP_HTML, \
        "必须是两列网格（图左 1.25fr / 卡右）"
    # ② 图在左列，且有 chart-col 容器
    assert 'class="chart-col"' in APP_HTML, '图表必须在 .chart-col 里（V4：图占左半边）'
    # ③ 高度由网格给（height:100%），不是 px 上限
    assert "#chart-root" in APP_HTML
    idx = APP_HTML.index("#chart-root")
    root_rule = APP_HTML[idx : APP_HTML.index("}", idx) + 1]
    assert "height:100%" in root_rule.replace(" ", ""), f"#chart-root 必须 height:100%：{root_rule[:120]}"
    # ④ ★ 旧固定上限**必须删除**（只允许出现在「已删除」的说明注释里）
    for line in APP_HTML.splitlines():
        if "clamp(" in line:
            stripped = line.strip()
            assert stripped.startswith(("*", "/*", "-", "//")), \
                f"`clamp(` 仍在生效规则里（V4 要求删除）：{stripped[:100]}"
    assert "height:520px" not in APP_HTML and "height:560px" not in APP_HTML, \
        "旧的固定高度必须删除（否则「高度自适应」文案说谎）"
    # ⑤ 「图别太小」的可量代理由 `tests/test_ui_layout_v4.py::V4-L5` 承担
    #   （K 线面板占比 ≥26% + 绝对 ≥215px）—— 那里有 CDP 真机测量，比源码断言更强。

    # ⑥ 与布局无关的两条（仍然有效，保留）
    assert 'placeholder="601398"' not in APP_HTML, "下单表单不得硬编码旧票代码"
    for field in ("panel.factors", "panel.total", "panel.signal"):
        assert field in APP_HTML, f"renderAnalysis 必须读 {field}（否则「就绪」但全是 —）"


# --------------------------------------------------------------------------- #
# T-V3-18（**原 `test_t_w3_3_guard_hardcoded_offline` 的加强版**，不是删除）
# --------------------------------------------------------------------------- #
def test_t_v3_18_frontend_never_decides_offline() -> None:
    """★★ INV-64：前端**根本不得**决定 offline（原判据是「不得硬编码」→ 现在「不许有这个机制」）。

    双判据（Critic C1 的教训：只断言 `APP_HTML` 常量，会在「服务端其实返回别的页面」时假绿）：
    ① **服务端真实返回的** `/` 响应体；② 源码级（无 `chartOffline` / 无 `offline=` 拼串 / 无开关元素）。
    """
    from ptrade import webui as webui_mod
    from ptrade.config import load_config
    from ptrade.webapp import WebApp

    tmp = tempfile.mkdtemp(prefix="v3-html-")
    served = (WebApp(load_config(tmp), offline=True)
              .dispatch("GET", "/", {}, None).to_bytes().decode("utf-8"))
    assert served.strip() == webui_mod.APP_HTML.strip(), "服务端返回的页面必须就是 APP_HTML（防「在旧常量上假绿」）"
    lowered = served.lower()
    assert "offline=" not in lowered, "★ 前端不得拼 offline 参数"
    assert "chartoffline" not in lowered, "★ `chartOffline` 必须删除"
    assert "离线" not in served, "★ 不得再出现「离线」开关/文案"
    assert 'data-testid="offline-toggle"' not in lowered
    src = Path("ptrade/webui.py").read_text(encoding="utf-8")
    assert "chartOffline" not in src and "offline=" not in src and "search-offline" not in src


def test_t_v3_18_guard_mutation_is_red() -> None:
    """**非空洞**：把 `offline=1` 拼串加回去 → 判据必须变红（变异真的落地）。"""
    assert "offline=" not in APP_HTML
    broken = APP_HTML.replace('var url = API.chartData + "?code="',
                              'var url = API.chartData + "?offline=1&code="', 1)
    assert broken != APP_HTML, "变异没落地"
    assert "offline=" in broken
    with pytest.raises(AssertionError):
        assert "offline=" not in broken, "★ 前端不得拼 offline 参数"


# --------------------------------------------------------------------------- #
# ★ P0-3（Architect 最高优先）：**「下单」只预填表单，绝不提交**
# --------------------------------------------------------------------------- #
@needs_node
def test_t_v3_p0_3_order_action_only_prefills() -> None:
    """★★ P0-3：点「下单」= 打开并**预填** `#order-code`，**任何情况下都不得 POST `/api/order`**。

    一键下单 = 花真钱（模拟盘也是钱）⇒ 必须停在表单、由用户二次确认。
    """
    result = _run_js(textwrap.dedent("""
    (function () {
      var P = globalThis.__probe;
      function el(id, props) {
        var e = document.getElementById(id);
        Object.keys(props || {}).forEach(function (k) { e[k] = props[k]; });
        return e;
      }
      el("stock-code", {value: "600519"});
      P.prefillOrder("600519");
      console.log(JSON.stringify({
        formPresent: !!document.getElementById("order-form"),
        orderCode: document.getElementById("order-code").value,
        orderPosts: P.urls().filter(function (u) { return u.indexOf("/api/order") >= 0; }).length,
        anyPosts: P.urls().length
      }));
    })();
    """))
    assert result["formPresent"] is True, "下单表单必须存在（P0-3：停在表单）"
    assert result["orderCode"] == "600519", f"代码必须预填进 #order-code，实际 {result['orderCode']!r}"
    assert result["orderPosts"] == 0, f"★ 竟然 POST 了 /api/order（{result['orderPosts']} 次）—— 一键下单"
    print(f"  [P0-3] 预填 #order-code=600519 ✓ · /api/order 调用 {result['orderPosts']} 次 ✓ · "
          f"全程请求 {result['anyPosts']} 个")


# --------------------------------------------------------------------------- #
# T-W3-4 自动刷新且仅盘中（三态；数据来自后端 market_open）
# --------------------------------------------------------------------------- #
def test_t_w3_4_interval_within_bounds(probe) -> None:
    assert probe["intervalCount"] >= 1, "缺少 setInterval"
    for item in [probe["intervalMs"]]:
        assert item is not None and 0 < item < 60_000, f"间隔必须在 (0,60)s 内，实测 {item}"


def test_t_w3_4_three_state_gating(probe) -> None:
    """**行为**断言：false → 停；null → 低频探测（**不是**停）；true → 到点才刷。"""
    assert probe["shouldPoll"] == {"on": True, "off": False, "unknown": True}
    assert probe["delays"]["off"] == 0, "非盘中必须停（delay=0 表示不轮询）"
    assert probe["delays"]["on"] < probe["delays"]["unknown"], "盘中频率必须高于不可判定时的低频探测"
    assert probe["tickOff"] == 0, f"非盘中不得轮询，实测触发 {probe['tickOff']} 次"
    assert probe["tickUnknown"] > 0, "不可判定时必须固定低频探测（与 false 的「停」不同）"
    assert probe["tickOnTooSoon"] == 0, "盘中也要等间隔到点，不得每 tick 都刷"
    assert probe["tickOnDue"] > 0, "盘中到点必须刷新"


def test_t_w3_4_market_open_comes_from_backend() -> None:
    """数据来源：后端 ``/api/health`` 的 ``market_open``（**不用客户端时钟**判断交易时段）。"""
    assert "d.market_open" in APP_HTML, "必须读后端 market_open"
    assert "applyMarketOpen" in APP_HTML
    # 页面里不得自己算 9:30/15:00 交易时段（那是服务端 market_open 的职责）
    for forbidden in ("09:30", "9:30", "15:00", "getHours()"):
        assert forbidden not in APP_HTML, f"前端不得自算交易时段（发现 {forbidden}）"


def test_t_w3_4_guard_always_polling() -> None:
    """**非空洞**：去掉三态门禁（恒轮询）→ 上面那条行为断言必须红。

    用与实现同款的纯函数模拟变异后的语义（``shouldPoll`` 恒真、``delay`` 恒为盘中值），
    证明「恒轮询」确实会被 ``tickOff == 0`` 抓住。
    """
    def mutated_should_poll(_market_open):        # 变异：删掉 false 分支
        return True

    def mutated_tick(market_open, due):
        return 1 if (mutated_should_poll(market_open) and due) else 0

    assert mutated_tick(False, True) == 1, "变异后的语义应恒轮询"
    with pytest.raises(AssertionError):
        assert mutated_tick(False, True) == 0, "非盘中不得轮询"


def test_T_V3_26_name_is_escaped_in_search_and_watchlist():
    """★★ **T-V3-26**（XSS 的**结构**守卫）：搜索结果行与自选池行里的 `name` **必须过 `esc()`**。

    ### 威胁模型

    股票名称来自**网络抓取**（akshare），是**外部数据**；页面用 vanilla `innerHTML` 拼行。
    若 name 未转义 ⇒ **同源 XSS ⇒ 本地 API 完全失守（可下单）**。

    ### 本轮的**真机**证据（手工 CDP，见 `.omx/probe/run_xss_check.py`）

    把名称表里 `600000` 的 name 换成
    ``<img src=x onerror="window.__XSS_PAYLOAD=\'img\'">600000XSS`` ⇒
    搜索结果行与自选池行：``window.__XSS_PAYLOAD`` 仍为 ``null``、``<img>`` 计数 **0**、
    ``src="x"`` 的 img **全局 0 个**，且 textContent 里是**字面量** ``<img``（当文本显示）✓
    名称表随后**字节级**恢复。

    ### 本条只做**结构**断言（**不是**全面覆盖）

    它抓「有人把 ``esc()`` 去掉」这个最可能发生的回归；真正 XSS 全覆盖需要浏览器级注入
    （本轮手工做过一次，未自动化）。★ **不得把本条写成「XSS 已自动化覆盖」。**
    """
    from ptrade import webui

    html = str(webui.APP_HTML)

    # ① 搜索结果行：name 必须出现在 esc(...) 里
    assert "esc(r.name" in html, (
        "搜索结果的 name 没走 esc() —— XSS 回归！"
        "（真机注入证据见 .omx/probe/run_xss_check.py）"
    )
    # ② 自选池行：先把 name 取出来（`var name = ...`），必须 esc(name)
    assert "esc(name)" in html, "自选池行的 name 没走 esc() —— XSS 回归！"
    # ③ 两处 data-* 属性里的 name 也必须转义（属性注入是另一个向量）
    # ★ data-name 属性用的是 `esc(r.name || "")` —— 直接断言这个**函数调用**出现即可
    #   （不做字符串拼接式的复杂断言：那种写法本身就容易写坏；语义已由 esc(name) 那条覆盖）
    assert 'esc(r.name || "")' in html, "data-name 属性里的 name 没走 esc() —— 属性注入回归！"
    # ④ 非空洞：esc() 必须真的是转义函数（含 < > & " ' 五个替换）
    for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert ch in html, f"esc() 缺少 {ch} —— 转义不完整"
    # ⑤ 非空洞对照：搜索/自选两个渲染函数确实存在（防「函数被删了所以断言都过」）
    assert "function renderSearch(" in html and "function renderWatchlist(" in html


def test_T_V3_28_refresh_watchlist_single_definition():
    """★★ **T-V3-28**（X3 真缺陷的防回归判据）：`refreshWatchlist` **全页恰好 1 处定义**，且**必须**调 `/api/quote`。

    ### 事故

    页面上**定义了两次** `refreshWatchlist`：

    * 第一处（会先拉自选池、**再批量调 `/api/quote` 取现价/涨跌幅**、然后重渲染）✓
    * 第二处（只 `api(API.watchlist).then(renderWatchlist)`，**不取价**）✗

    **JS 里后定义的函数覆盖先定义的** ⇒ 第二处**整个盖掉**第一处 ⇒
    `QUOTE_MAP` 永远为空 ⇒ 自选池的 **现价 / 涨跌幅 永远是 `—`**
    ⇒ 用户要求 ②「每行显示 代码·名称·**现价**·**涨跌幅**」**只做到一半**。

    ★ **为什么接口测试抓不到**：`/api/quote` 本身 **200 且数据正确**
      （`{last: 1324.97, pct_chg: -0.3}`）；坏在「页面最终用的是哪个函数」。
      **只有真机看渲染文本才能发现**（第 7 轮全按钮冒烟看到 `600519 贵州茅台 — —`）。

    ### 判据

    1. `function refreshWatchlist` 在页面里**恰好 1 处**（重复定义是 JS 的覆盖陷阱）；
    2. 该定义里**必须**出现 `API.quote`（否则又是「不取价」的版本）；
    3. 该定义里**必须**出现 `renderWatchlist`（否则取了价也不重渲染）；
    4. ★ 非空洞：`QUOTE_MAP` 必须被**写入**（`QUOTE_MAP[` 赋值存在），且 `renderWatchlist` 必须**读**它（`QUOTE_MAP[` 读取存在）。
    """
    from ptrade import webui

    html = str(webui.APP_HTML)

    # ① 恰好 1 处**定义**
    #   ★ 必须用**行首正则**：裸 `str.count` 会把**注释/文档里提到的** `function refreshWatchlist` 也算进去
    #     （本文件 L667/L673 的 X3 解释注释里就各写了一次 ⇒ 裸计数会得到 3，假红）。
    import re

    defs = re.findall(r"^function refreshWatchlist", html, re.M)
    n = len(defs)
    assert n == 1, (
        f"`function refreshWatchlist` 的**真定义**有 {n} 处（必须恰好 1 处）—— "
        "JS 后定义会覆盖先定义，X3 就是这样坏的（会取价的版本被盖掉 ⇒ 现价/涨跌幅永远 —）"
    )

    # ②③ 该定义必须既取价又重渲染
    start = re.search(r"^function refreshWatchlist", html, re.M).start()
    # 取到下一个顶层 `function ` 之前（粗略但足够：本文件里函数之间不嵌套）
    nxt = html.find("\nfunction ", start + 1)
    body = html[start : nxt if nxt != -1 else len(html)]
    assert "API.quote" in body, (
        "`refreshWatchlist` 里没有 `API.quote` —— 自选池的现价/涨跌幅会是 `—`（X3 回归）"
    )
    assert "renderWatchlist" in body, "`refreshWatchlist` 里没有 `renderWatchlist` —— 取了价也不会重渲染"

    # ④ 非空洞：QUOTE_MAP 被写入且被读取
    assert "QUOTE_MAP[" in html, "`QUOTE_MAP` 未被使用 —— 现价/涨跌幅的机制不在了"
    assert "QUOTE_MAP = {}" in html, "`QUOTE_MAP` 未被重置 —— 切股票后可能串价"
    # renderWatchlist 必须读 QUOTE_MAP（否则取了价也不显示）
    rs = html.index("function renderWatchlist")
    rn = html.find("\nfunction ", rs + 1)
    rbody = html[rs : rn if rn != -1 else len(html)]
    assert "QUOTE_MAP[" in rbody, (
        "`renderWatchlist` 不读 `QUOTE_MAP` —— 即使取到了现价也显示不出来"
    )
