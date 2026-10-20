"""W4 前端契约测试：``ptrade/webui.py`` 的 ``APP_HTML``。

覆盖 ``.omx/plans/test-spec-ptrade-web.md`` §5 的 U1–U4，并额外加**非空洞守卫**：
U1–U4 若被写成"永远为真"的断言就没有意义，因此本文件同时用「注坏 → 必须变红」的
夹具证明每条断言真的能失败（见 ``test_u1_guard_*`` / ``test_u2_guard_*`` /
``test_u4_guard_*``）。

不依赖任何服务端：全部是对字符串的结构断言（W3 的 ``webapp.py`` 尚未落盘）。

## U5：整段 JS 必须能被**解析**（2026-10-15 补）

**这是一次真实事故换来的守卫。** 修前页面能打开、元素都在，但**一个数据都不加载**，
浏览器控制台报 `Uncaught SyntaxError: Unexpected string` ——
`renderPositions` 里有一处 JS 字符串的引号没转义：

```python
# 源码里写的是（本意是让 JS 拿到 \"）
return "<tr data-code=\\"" + esc(p.code) + "\\"><td>" + ...
# 但 Python **非 raw** 字符串会把 \" 变成 "，于是运行时 JS 变成：
return "<tr data-code="" + esc(p.code) + ""><td>" + ...   # ← 语法错误
```

**为什么既有测试全都漏了**：

* 原有的 U1–U4 都是**字符串结构断言**（有没有外链、有没有某个 id），
  **没有任何一条解析 JS**；
* 我在别处写的 JS 行为测试（`test_divergence_ui_js.py`）只从 ``APP_HTML`` 里
  **抽出 3 个函数**（`$`/`esc`/`renderDivergence`）在 node 里跑 ——
  那证明那 3 个函数没问题，**但从没解析过整段脚本**；
* 于是「脚本里其它地方有语法错误 → 整个 `<script>` 不执行 → 页面全是加载中」
  这种故障，**所有闸都是绿的**。

U5 用 `node --check` 解析**整段** `<script>`。无 node 时跳过（并如实标注）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from ptrade.chart import external_refs, no_external_refs
from ptrade.webui import APP_HTML

ROOT = Path(__file__).resolve().parents[1]

#: V3-A3/A5 冻结的必需元素 id（一个都不能少；`search-box`/`chart-frame` 已随 A3/A5 退场）
REQUIRED_IDS = (
    "state-panel",
    "positions",
    "risk",
    "action-bar",
    "stock-code",
    "search-results",
    "watchlist",
    "score-table",
    "chart-root",
    "analysis-state",
    "order-form",
    "settle-btn",
    "toast",
)

#: V3 冻结的端点（前端必须按这些路径对接；A5 起图表数据走 JSON 端点）
REQUIRED_ENDPOINTS = (
    "/api/health",
    "/api/state",
    "/api/positions",
    "/api/risk",
    "/api/search",
    "/api/watchlist",
    "/api/watchlist/score",
    "/api/score",
    "/api/order",
    "/api/close",
    "/api/settle",
    "/api/chart/data",
    "/api/quote",
    "/api/analysis",
    "/api/name/refresh",
)

#: 六种交易方向（core.Side 的值）
REQUIRED_SIDES = ("buy", "sell", "margin_buy", "short_sell", "buy_to_cover", "repay_margin")

MAX_BYTES = 200 * 1024


# ─────────────────────────── U1：外链为 0（INV-42） ───────────────────────────

def test_u1_no_external_refs() -> None:
    """**零外链**（V3-A5 / INV-59 同族）：不得引用任何**跨源/CDN** 资源。

    ★ V3 的形态变了（这是**判据进化**，不是放宽）：图表库不再由服务端内联进图表 HTML，
      而是主页面引用**本地** vendored 资源（`/vendor/echarts.min.js`、`/static/chart_option.js`）。
      所以判据从「任何非 data: 的引用都算外链」改为：**不得有绝对 http(s) 外链**，
      且**允许并仅允许**这两个同源静态路径。
    """
    refs = external_refs(APP_HTML)
    # ① 允许清单：同源静态资源（唯一允许的「非内联」引用）
    allowed = {"/vendor/echarts.min.js", "/static/chart_option.js"}
    unexpected = [r for r in refs if not any(a in r for a in allowed)]
    assert not unexpected, f"APP_HTML 出现未允许的外部/相对引用：{unexpected}"
    # ② 绝对 URL 一律禁止（CDN / 第三方）
    assert not re.search(r'(?:src|href)="https?://', APP_HTML), "不得引用任何绝对 http(s) 资源"
    # ③ 本地的两个资源**必须**引用（A5 的前提：库只加载一次，不在每次请求里重发）
    assert '<script src="/vendor/echarts.min.js">' in APP_HTML
    assert "/static/chart_option.js" in APP_HTML


def test_u1_guard_script_src_is_caught() -> None:
    """非空洞守卫：塞一个**跨源** `<script src>` → 本判据必须变红。"""
    broken = APP_HTML.replace("<script>", '<script src="https://cdn.example.com/evil.js">', 1)
    assert broken != APP_HTML
    assert re.search(r'(?:src|href)="https?://', broken), "跨源脚本必须被 ② 抓到"
    assert no_external_refs(broken) is False


def test_u1_guard_iframe_src_is_caught() -> None:
    """非空洞守卫：`<img src="https://…">` 必须被抓（媒体 src 类别；V3 起页面里没有框架元素）。"""
    broken = APP_HTML.replace("<body", '<body><img src="https://evil.example.com/x.png">', 1)
    assert broken != APP_HTML
    assert no_external_refs(broken) is False
    assert any("img src" in r or "media" in r or "src" in r for r in external_refs(broken))


def test_u1_guard_relative_css_url_is_caught() -> None:
    """非空洞守卫：**相对路径**也算外链（单文件产物一旦引用外部资源就不自包含）。"""
    broken = APP_HTML.replace("<style>", "<style>body{background:url(/static/bg.png)}", 1)
    assert no_external_refs(broken) is False
    assert any("css url()" in r for r in external_refs(broken))


def test_u1_guard_not_vacuous_when_no_script() -> None:
    """``_strip_script_bodies`` 只剥内容、保留开标签；顺带证明 APP_HTML 真有 ``<script>``。"""
    assert "<script" in APP_HTML, "U1 的 JS 网络调用扫描需要真实 <script> 块，否则守卫空洞"
    assert "fetch(" in APP_HTML, "前端必须用 fetch 调 API（否则页面无法工作）"


def test_u1_guard_js_literal_external_fetch_is_caught() -> None:
    """非空洞守卫：JS 里字面量 ``fetch("https://…")`` 必须被抓（JS 网络调用类别）。"""
    broken = APP_HTML.replace("fetch(", 'fetch("https://evil.example.com/steal") || fetch(', 1)
    assert no_external_refs(broken) is False
    assert any("js network call" in r for r in external_refs(broken))


def test_no_nested_frame_at_all() -> None:
    """★ V3-A5 / `T-V3-9`：页面里**没有**任何嵌套框架（静态、动态、内联字符串都算）。

    修前 K 线图靠 `<iframe id="chart-frame">` 承载（运行时赋 src）——「iframe 割裂」正是
    用户三个痛点之一，V3 改为**同页** `#chart-root` 直接渲染。
    """
    lowered = APP_HTML.lower()
    assert "<iframe" not in lowered, "不得再有 <iframe> 标记"
    assert 'createelement("iframe"' not in lowered.replace(" ", ""), "不得动态创建框架"
    assert "chart-frame" not in lowered, "旧的 `chart-frame` 引用必须清除"
    # 正对照：同页图表容器必须在
    assert 'id="chart-root"' in APP_HTML and 'data-testid="chart-root"' in APP_HTML
    assert "echarts.init(" in APP_HTML, "必须在**本页**初始化 ECharts（而不是把图塞进框架）"


def test_nested_frame_guard_is_not_vacuous() -> None:
    """非空洞：把旧的 `<iframe>` 加回去 → 上一条必须红。"""
    broken = APP_HTML.replace('<div id="chart-root"', '<iframe id="chart-frame"></iframe><div id="chart-root"', 1)
    assert broken != APP_HTML, "变异没落地"
    assert "<iframe" in broken.lower()


# ─────────────────────── U2：必需 id 全在（逐个断言） ───────────────────────

@pytest.mark.parametrize("element_id", REQUIRED_IDS)
def test_u2_required_id_present(element_id: str) -> None:
    assert f'id="{element_id}"' in APP_HTML, f"缺少必需元素 id=#{element_id}（PRD §5.5）"


def test_u2_all_ids_count() -> None:
    found = sorted(set(re.findall(r'id="([^"]+)"', APP_HTML)))
    missing = [i for i in REQUIRED_IDS if i not in found]
    assert not missing, f"缺 id：{missing}"


def test_u2_guard_id_check_can_fail() -> None:
    """非空洞守卫：删掉一个 id → 断言必须变红。"""
    broken = APP_HTML.replace('id="toast"', 'id="toast-x"', 1)
    assert broken != APP_HTML
    assert 'id="toast"' not in broken
    with pytest.raises(AssertionError):
        assert 'id="toast"' in broken, "缺少必需元素 id=#toast"


# ───────────────────────── U3：体积受控（< 200 KB） ─────────────────────────

def test_u3_size_under_limit() -> None:
    size = len(APP_HTML.encode("utf-8"))
    assert size < MAX_BYTES, f"APP_HTML 体积 {size} 字节 ≥ {MAX_BYTES}（不得内联 ECharts 本体）"
    assert size > 4096, "体积过小，疑似生成的 HTML 不完整"


# ─────────────────── U4：不含内联 ECharts 指纹（防塞两遍） ───────────────────

def test_u4_no_inlined_echarts() -> None:
    """V3-A5：图表库**允许引用本地 vendored 文件**，但**不得内联**（1 MB 进每页就白改）。

    ★ 判据进化：修前「APP_HTML 里连 `echarts` 这个词都不能出现」（那时由服务端内联图表 HTML）；
      V3 起主页面**必须**引用 `/vendor/echarts.min.js`（只加载一次），所以改为：
      有引用、**无内联**、且**无 CDN**。
    """
    assert "/vendor/echarts.min.js" in APP_HTML, "A5 要求主页面引用本地 vendored 库"
    assert "echarts.min.js" in APP_HTML
    # 不得把库本体内联进来（体积判据 U3 是同一件事的量化版）
    assert "Licensed to the Apache Software Foundation" not in APP_HTML, "疑似把 ECharts 本体内联进了页面"
    assert "https://" not in APP_HTML.lower() or "cdn" not in APP_HTML.lower(), "不得引用 CDN"


def test_u4_guard_vendor_fingerprint_absent() -> None:
    """若 vendored ECharts 存在，它的内容指纹必须不在 APP_HTML 里（注坏即红）。"""
    vendor = ROOT / "ptrade" / "vendor" / "echarts.min.js"
    if not vendor.exists():
        pytest.skip("ptrade/vendor/echarts.min.js 未落盘（W3/chart 侧），跳过指纹比对")
    text = vendor.read_text(encoding="utf-8", errors="ignore")
    marker = "".join(ch for ch in text[:4000] if not ch.isspace())[:200]
    assert marker, "无法提取 ECharts 指纹"
    assert marker not in APP_HTML, "APP_HTML 疑似内联了 ECharts 本体"


def test_u4_guard_echarts_injection_is_caught() -> None:
    broken = APP_HTML + "\n<script>window.echarts = {};</script>\n"
    with pytest.raises(AssertionError):
        assert "echarts" not in broken.lower(), "注入 ECharts 后必须变红"


# ───────────────── 对接契约：端点 / 方向 / 错误语义 / 二次确认 ─────────────────

@pytest.mark.parametrize("endpoint", REQUIRED_ENDPOINTS)
def test_js_references_every_endpoint(endpoint: str) -> None:
    assert endpoint in APP_HTML, f"前端缺少端点对接：{endpoint}"


def test_all_six_sides_available() -> None:
    form = APP_HTML.split('data-testid="order-form"')[1].split("</form>")[0]
    for side in REQUIRED_SIDES:
        assert f'value="{side}"' in form, f"下单表单缺少方向 {side}"


def test_business_rejection_is_not_reported_as_failure() -> None:
    """业务拒单 = HTTP 200 + ok=true + orders[].status=="rejected"，必须显示 error_code。"""
    assert "rejected" in APP_HTML
    assert "error_code" in APP_HTML
    assert "业务拒单" in APP_HTML, "必须把业务拒单与「请求失败」区分开"


def test_http_error_semantics_covered() -> None:
    assert "503" in APP_HTML and "锁" in APP_HTML, "必须提示写锁超时（HTTP 503）"
    assert "400" in APP_HTML, "必须提示参数非法（HTTP 400）"


def test_write_operations_require_confirmation() -> None:
    """下单 / 平仓 / 结算 都要二次确认。"""
    assert "confirmWrite" in APP_HTML
    assert APP_HTML.count("confirmWrite(") >= 5, "三处写操作（下单/平仓/结算）+ 自选池删除+批量打分 都需确认"
    for label in ("下单：", "平仓 ", "结算到 "):
        assert label in APP_HTML, f"缺少二次确认文案：{label}"


def test_chinese_ui() -> None:
    cjk = re.findall(r"[\u4e00-\u9fff]", APP_HTML)
    assert len(cjk) >= 100, "界面文案应为中文"


# --------------------------------------------------------------------------- #
# U5：整段 JS 必须能被**解析**（2026-10-15 补 —— 一次真实事故换来的守卫）
#
# 见模块 docstring：页面元素都在、但 `<script>` 有语法错误 → 整段不执行 →
# 所有面板停在「加载中…」。U1–U4（字符串结构断言）**全部是绿的**，
# 我的 JS 行为测试也只抽了 3 个函数、没解析整段。
# --------------------------------------------------------------------------- #
NODE = shutil.which("node")


def _all_scripts() -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", APP_HTML, re.S)


def _node_check(src: str) -> tuple[int, str]:
    """用 `node --check` 解析一段 JS，返回 (退出码, stderr)。

    ★ V3：页面里有一个 `<script type="module">`（`import { buildOption } …`），
      它**不是**经典脚本 —— `node --check foo.js` 会报「Cannot use import statement outside a module」。
      所以含 `import`/`export` 的块按 **`.mjs`** 校验（同一套 `--check`，只是模块语义）。
    """
    suffix = ".mjs" if re.search(r"^\s*(?:import|export)\s", src, re.M) else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(src)
        path = fh.name
    try:
        p = subprocess.run([NODE, "--check", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return p.returncode, (p.stderr or "")
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.skipif(NODE is None, reason="需要 node 才能解析 JS（本机未装 node）")
def test_u5_all_scripts_parse() -> None:
    """**每一段** `<script>` 都必须能被 JS 引擎解析。

    这是唯一能抓住「引号/括号转义写错 → 整段脚本静默不执行」的守卫：
    页面元素照旧存在、U1–U4 照旧全绿，但用户看到的是一个死页面。
    """
    scripts = _all_scripts()
    assert scripts, "APP_HTML 里找不到 <script>（前端不该是空的）"

    failures: list[str] = []
    for i, src in enumerate(scripts):
        if not src.strip():
            continue
        code, err = _node_check(src)
        if code != 0:
            failures.append(f"段 {i}（{len(src):,} 字符）:\n{err[:1200]}")

    assert not failures, (
        "★JS 语法错误 —— 整段 <script> 不会执行，页面会停在「加载中…」：\n\n"
        + "\n\n".join(failures)
    )


def test_u5_guard_is_not_vacuous() -> None:
    """**非空洞守卫**：U5 的判据必须真的能判出坏脚本。

    用一个**已知坏**的 JS 片段走同一条路径；如果它也「通过」，说明 U5 是空的。
    """
    if NODE is None:
        pytest.skip("需要 node")
    # 正是本次事故的形态：JS 字符串里嵌了未转义的引号
    bad = 'function f(){ return "<tr data-code="" + x + ""><td>"; }'
    code, err = _node_check(bad)
    assert code != 0, "★U5 的判据无法判出坏脚本 —— 它是空守卫"
    assert "SyntaxError" in err or "Unexpected" in err, err[:300]


def test_u5_script_is_substantial() -> None:
    """**非空洞守卫**：脚本不能是空的/被抽掉的 —— 否则 U5「解析通过」毫无意义。"""
    scripts = _all_scripts()
    total = sum(len(s) for s in scripts)
    assert total > 8_000, f"前端脚本只有 {total:,} 字符，疑似被抽空 —— U5 会假通过"
    # 用**实际存在**的函数名（我第一版猜了 `loadAll`/`searchStocks`，那是错的：
    # 真实入口是 `refreshAll`，搜索是 `doSearch`）。猜名字会让守卫变成噪音。
    for needle in ("renderState", "renderPositions", "renderRisk", "doSearch",
                   "loadChart", "refreshAll"):
        assert needle in APP_HTML, f"前端缺少函数 {needle}"
    # 入口必须在（脚本解析通过但没挂载任何东西，页面同样是死的）
    assert "DOMContentLoaded" in APP_HTML and "refreshAll()" in APP_HTML, \
        "缺少入口挂载/首次调用 —— 页面会停在「加载中…」"


def test_u5_no_unescaped_double_quote_in_js_strings() -> None:
    """**定点守卫**：JS 源码里不得出现 `="` + `"` 这种「字符串被提前闭合」的形态。

    与 U5 互补：U5 管「整体能不能解析」，这条把**本次事故的具体形态**钉住，
    即使将来有人把 `node` 去掉（U5 跳过），这条仍会红。
    它对**运行时**的 JS 取值断言 —— Python 的转义规则已经生效之后的样子。
    """
    js = "\n".join(_all_scripts())
    # 事故形态：`="` 之后紧跟 `"`（本该是转义引号，被 Python 吃掉反斜杠）
    bad = re.findall(r'="\s*"', js)
    assert not bad, (
        f"运行时 JS 里出现 {len(bad)} 处 `=\"...\" ` 提前闭合的字符串 —— "
        f"典型成因是 Python **非 raw** 字符串把源码里的 `\\\"` 变成了 `\"`。"
        f"修法：JS 字符串改用**单引号**（`'<tr data-code=\"' + ...`）。"
    )
    # 事故形态也会留下「连续两个字符串字面量相邻」的痕迹
    assert '"" +' not in js, "运行时 JS 里出现 `\"\" +` —— 字符串被提前闭合"
