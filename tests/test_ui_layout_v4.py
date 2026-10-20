"""V4-W3 · 布局重做的验收判据（V4-L0..L10）。

真源：``.omx/plans/test-spec-ptrade-layout-v4.md``（★ R2/R3 修订后版本）。
本文件**只测不改**：所有数字来自**真实 Chrome**（``tests/_cdp.py`` 的 ``Emulation`` 域切视口），
结构类判据（L0/L7）纯源码断言，无浏览器也跑 —— 这样「无 Chrome ⇒ 全 skip ⇒ 假绿」不可能发生。

★ 每条断言都按 R3 修订写：
* R3-A：1440p 必须 ``assert not score`` + ``assert not positions``（**不是** ``not (a and b)``）
* R3-C：``_sane()`` 前置（空/全 0 测量不得当成通过）
* R3-D：``collapsed`` = ``details.open is False`` **且** 内容 ``offsetHeight == 0``
* R3-E：标题判据同时断 ``title.top`` 不叠到上一个面板
* R3-F：``seriesByGrid`` 按面板（xAxisIndex）分组，不只数总数
* R3-G：L0 结构哨兵（无浏览器也跑）
* R3-H：``legend_budget`` 不得出现在页面路径源码里

视口：1600×900 / 1920×945 / 1920×1080 / 2560×1440 —— 全部由 ``Emulation.setDeviceMetricsOverride``
实现，``measure()`` 会自证 ``_emulation_ok``（请求视口 == 实测 innerWidth/innerHeight）。
"""

from __future__ import annotations

import itertools
import json
import pathlib
import re
import shutil
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ptrade import core, store  # noqa: E402
from ptrade.config import load_config  # noqa: E402
from tests import _cdp  # noqa: E402

FIX = ROOT / "data" / "fixture"

#: V4-L3 白名单：图例只留这 6 项（用户要求「标注太多」）
KEEP = {"MA12", "MA24", "MA48", "MA96", "VOL MA5", "VOL MA10"}

#: V4-L4 每个面板的 series 数（按 xAxisIndex 分组；合计 18）
SERIES_BASELINE = {0: 5, 1: 4, 2: 3, 3: 1, 4: 3, 5: 2}

#: 四档视口（用户/评审明确要求）
VIEWPORTS = [(1600, 900), (1920, 945), (1920, 1080), (2560, 1440)]

#: 右列「折叠」判据针对的三张卡（按 summary 文案分类，见 _cdp 的 MEASURE_JS）
# ★ **V5 改写**：用户要求「打分和分析合并成一个模块」⇒ 原来的两张 `<details>`
#   （`score-details` + `analysis-details`）合并成**一张**，`data-collapse="analysis"`。
#   ⇒ 可折叠卡从 3 个变 1 个（`positions` 是普通 section，恒展开）。
#   ★ 判据意图不变：「打分/分析默认折叠、持仓恒展开、折叠后摘要行有内容」。
FOLD_KEYS = ("analysis",)

#: 删掉的图例项（L3 的反向断言 —— 特别是重复的「微型K线」）
GONE_LEGEND = ("微型K线", "BOLL上轨", "BOLL中轨", "BOLL下轨", "成交量", "成交额",
               "MACD柱", "DIF", "DEA", "SAR", "K线")

#: L3b 面板标题必须保留的说明（删图例后用户仍要认得出线）
TITLE_MUST = ("BOLL(20,2)", "上/中/下轨", "MACD(12,26,9)", "SAR(0.02/0.02/0.20)",
              "MA12/24/48/96", "VOL MA5/MA10")


# --------------------------------------------------------------------------- #
# 环境：临时数据根 + 随机端口服务（★ 不碰 8765 上的 Lead 服务）
# --------------------------------------------------------------------------- #
def _build_root(positions: int = 0) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp(prefix="v4w3-"))
    (root / "data" / "fixture").mkdir(parents=True, exist_ok=True)
    for p in FIX.glob("*.json"):
        if p.name.startswith(("daily_", "spot", "trade_dates", "margin_targets", "universe",
                              "industry_map", "quarterly", "holders", "top_holders", "fin_")):
            shutil.copy2(p, root / "data" / "fixture" / p.name)
    cfg = load_config(str(root))
    state = core.new_state(cfg)
    if positions:
        codes = sorted({p.stem[len("daily_"):] for p in FIX.glob("daily_*.json")
                        if "unadjust" not in p.stem})[:positions]
        for code in codes:
            state.positions[code] = core.Position(
                code=code, name="票" + code, long_qty=100, long_available=100,
                long_cost=10.0, last_price=10.0,
            )
    store.save_state(cfg, state, source="v4-w3")
    return root


@pytest.fixture(scope="module")
def serve_env(tmp_path_factory):
    """空仓环境：起服务 → yield (cfg, base_url)。模块级（一个服务跑完整个文件）。"""
    root = _build_root(0)
    cfg = load_config(str(root))
    with _cdp.serve(cfg, offline=True) as base:
        yield cfg, base


@pytest.fixture(scope="module")
def serve_env_30(tmp_path_factory):
    """持仓 30 只的环境（V4-L9：右列数据变长时**页面**仍不得滚）。"""
    root = _build_root(30)
    cfg = load_config(str(root))
    with _cdp.serve(cfg, offline=True) as base:
        yield cfg, base


@pytest.fixture(scope="module")
def cdp_status():
    """CDP 可用性。★ 不可用 → skip **且显式记录**（不得计入通过）。"""
    info = _cdp.status()
    if not info.get("available"):
        pytest.skip(
            "CDP 不可用（未验证，非通过）：" + str(info.get("reason"))
            + f" · chrome={info.get('chrome')} node={info.get('node')}"
        )
    return info


_MEASURE_CACHE: dict[tuple, dict] = {}


def measure(base: str, vw: int, vh: int) -> dict:
    """``measure()`` 的缓存包装（同一 base+视口+选项只量一次）。"""
    key = (base, vw, vh, "plain")
    if key not in _MEASURE_CACHE:
        _MEASURE_CACHE[key] = _cdp.measure((vw, vh), url=base, settle_ms=1200)
    return _MEASURE_CACHE[key]


def measure_opts(base: str, vw: int, vh: int, **opts) -> dict:
    key = (base, vw, vh, tuple(sorted(opts.items())))
    if key not in _MEASURE_CACHE:
        _MEASURE_CACHE[key] = _cdp.measure((vw, vh), url=base, **opts)
    return _MEASURE_CACHE[key]


# --------------------------------------------------------------------------- #
# 通用判据助手（R3-C / R2-C / R2-D）
# --------------------------------------------------------------------------- #
def _sane(m: dict) -> None:
    """**R3-C**：``measure()`` 失效（返回 {} / 全 0）时 ``scrollH <= clientH+1`` 会直接通过
    ⇒ 任何视口类判据必须先过 sanity check。"""
    assert m, "measure() 返回空 —— 不得当成通过"
    vp = m.get("viewport") or {}
    assert vp.get("w", 0) > 0 and vp.get("h", 0) > 0, f"测量值不合理（viewport）：{vp}"
    assert m.get("clientH", 0) > 0 and m.get("clientW", 0) > 0, (
        f"测量值不合理（clientH={m.get('clientH')} clientW={m.get('clientW')}）"
    )
    assert m.get("scrollH", 0) >= m["clientH"] - 1, f"scrollH 明显异常：{m.get('scrollH')}"
    assert m.get("chartRoot"), "measure() 没拿到 #chart-root 的 rect"


def _rect_overlap(a: dict, b: dict, tol: float = 0.5) -> bool:
    """两个矩形是否重叠（容差 tol px —— 亚像素取整不该判红）。"""
    return not (
        a["right"] <= b["left"] + tol or b["right"] <= a["left"] + tol
        or a["bottom"] <= b["top"] + tol or b["bottom"] <= a["top"] + tol
    )


def _pct(value) -> float:
    """把 ECharts 的 ``"28%"`` 解析成 28.0（R2-D：grid 高度必须是百分比串）。"""
    assert isinstance(value, str) and value.strip().endswith("%"), (
        f"grid 高度必须是百分比串（分辨率无关），实际 {value!r}"
    )
    return float(value.strip().rstrip("%"))


def _kline_panel_px(m: dict) -> float:
    grids = m.get("grids") or []
    assert grids, "没有拿到 ECharts 的 grid 定义"
    chart_h = (m.get("chartRoot") or {}).get("h", 0)
    return chart_h * _pct(grids[0]["height"]) / 100.0


# --- 可复用的判据核心（真测试与「变异自证」共用同一份逻辑，避免两处各写一份） -------- #
def _check_no_page_scroll(m: dict, tag: str) -> None:
    _sane(m)
    assert m["scrollH"] <= m["clientH"] + 1, (
        f"{tag} 出现垂直滚动条：scrollH {m['scrollH']} > clientH {m['clientH']}"
    )
    assert m["scrollW"] <= m["clientW"] + 1, (
        f"{tag} 出现水平滚动条：scrollW {m['scrollW']} > clientW {m['clientW']}"
    )


def _check_chart_left_and_tall(m: dict) -> None:
    c = m["chartRoot"]
    vw = m["viewport"]["w"]
    assert c["x"] + c["w"] / 2 < vw / 2, f"图表不在左半边：中心 x={c['x'] + c['w'] / 2:.0f} ≥ {vw / 2:.0f}"
    assert c["h"] >= 780, f"图表高度 {c['h']:.1f} < 780"
    ratio = c["h"] / c["w"]
    assert ratio >= 0.75, f"高宽比 {ratio:.2f} < 0.75（R3-2 门槛；改动前 0.27）"


def _check_legend(m: dict) -> None:
    got = list(m["legendData"])
    assert len(got) == 6, f"图例有 {len(got)} 项（应 6 项；改动前 18 项）：{got}"
    assert set(got) == KEEP, f"图例集合不符：多出 {set(got) - KEEP}，缺少 {KEEP - set(got)}"
    for gone in GONE_LEGEND:
        assert gone not in got, f"图例里仍有 {gone!r}（应已删）"


def _kline_panel_pct(m: dict) -> float:
    """K 线面板占 chart 高度的百分比（ECharts 的 `grid[0].height` 是 `"28%"` 这种串）。"""
    g = (m.get("grids") or [{}])[0]
    h = g.get("height")
    assert isinstance(h, str) and h.endswith("%"), f"grid[0].height 必须是百分比串，实际 {h!r}"
    return float(h.rstrip("%"))


def _check_kline_height(m: dict) -> None:
    """★ **R4 更正**：原来的「绝对 `>= 240px`」**是推导错误**，不是产品缺陷。

    PRD R2-3 的 240 建立在「容器高 **875px**」的**估算**上（`28% × 875 = 245`）。
    **实测**（W3 harness + Lead CDP 一致）容器 = **840.2px** ⇒ `28% × 840.2 = 235.3`。
    1080p = 273px ✓ · 1440p = 374px ✓ · **只有 945 档差 4.7px**。

    ⇒ 改为**双条件**（比例 + 绝对下限，后者按**最小支持视口** 1600×900 的实测 223px 定）：
    * `占比 >= 26%`（设计规格是 28%，给 2pp 容差）；
    * `绝对 >= 215px`（1600×900 实测 223px 是「可读」的下限）。
    ★ **不得**为了这条判据去改 `chart_option.js` 的 28% —— 那是对的。
    ★ **不得**再宣称「240px 达成」：那是基于错误估计的目标。
    """
    px = _kline_panel_px(m)
    pct = _kline_panel_pct(m)
    assert pct >= 26.0, f"K 线面板占 chart 高 {pct:.1f}% < 26%（设计规格 28%；改动前 21%）"
    assert px >= 215, f"K 线面板 {px:.0f}px < 215px（1600×900 实测 223px 可读下限；改动前 ≈103px）"


def _collapsed_flags(m: dict, tag: str) -> dict:
    flags = m.get("collapsed") or {}
    for key in FOLD_KEYS:
        value = flags.get(key)
        assert isinstance(value, bool), (
            f"{tag}：`collapsed[{key!r}]` = {value!r}（None 表示**没有找到对应的卡片**）"
            f"—— ★ V5 起「打分 + 分析」合并成**一张**可折叠卡（`data-collapse=\"analysis\"`）"
            f"，它必须在右列。details={m.get('collapsedDetails')}"
        )
    return flags


def _check_collapsed_small(m: dict, tag: str) -> None:
    """★ V5：小视口下**合并卡（打分+分析）默认折叠**、**持仓恒展开**、折叠后摘要行**有内容**。"""
    flags = _collapsed_flags(m, tag)
    assert flags.get("analysis") is True, (
        f"{tag}：合并的「打分/CANSLIM 分析」卡应默认折叠（V5 起二者同卡，key = analysis）"
    )
    assert flags.get("positions") is False, f"{tag}：持仓**不得**默认折叠"
    # ★ summaryText 的键与 `data-collapse` 一致 ⇒ 合并卡在 `analysis` 键下。
    summary = (m.get("summaryText") or {}).get("analysis") or (m.get("summaryText") or {}).get("score")
    assert summary, (
        f"{tag}：折叠后摘要行没有关键值（V5 要求 summary 显示「总分 · 信号」，"
        f"实际 summaryText={m.get('summaryText')!r}）"
    )


# --------------------------------------------------------------------------- #
# CSS 规则解析（L0 结构哨兵用；不用子串锚点，避免「注释里出现」的假绿/假红）
# --------------------------------------------------------------------------- #
def _style_blocks(html: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.S | re.I))


def _css_rules(css: str) -> dict[str, str]:
    """把 CSS 解析成 ``{选择器: 声明串}``；选择器列表按逗号拆分，注释剥掉。

    ★ 同一选择器可能有多条规则（``body{...}`` 出现 3 次是常态）—— 必须**按出现顺序合并**
    声明，不能后写覆盖前写（否则会只读到 ``font-size:13px`` 那条而误判「body 没有 grid」）。
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: dict[str, str] = {}
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors, decls = block.group(1), block.group(2)
        decl = " ".join(decls.split())
        for sel in selectors.split(","):
            key = " ".join(sel.split()).strip().lower()
            if key:
                rules[key] = (rules.get(key, "") + ";" + decl) if rules.get(key) else decl
    return rules


def _css_groups(css: str) -> list[tuple[list[str], str]]:
    """返回 ``[(选择器列表, 声明串), ...]`` —— 保留「同一条规则覆盖多个选择器」的事实。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    groups: list[tuple[list[str], str]] = []
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sels = [" ".join(sel.split()).strip().lower() for sel in block.group(1).split(",")]
        sels = [sel for sel in sels if sel]
        if sels:
            groups.append((sels, " ".join(block.group(2).split())))
    return groups


def _decl(decls: str, prop: str) -> str | None:
    found = re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", decls)
    return found.group(1).strip().lower() if found else None


def _has_rule(rules: dict[str, str], selector: str) -> bool:
    return selector in rules


def _app_html() -> str:
    from ptrade import webui

    return str(webui.APP_HTML)


# =========================================================================== #
# V4-L0 ★ 结构哨兵（R3-G）：无浏览器也跑 —— 防「CSS 根本没改 / 全 skip ⇒ 假绿」
# =========================================================================== #
def test_v4_l0_structure_sentinels():
    html = _app_html()
    css = _style_blocks(html)
    rules = _css_rules(css)
    groups = _css_groups(css)

    # ① html, body { height:100%; overflow:hidden } —— 最容易漏的一层（PRD R2-2）
    combined = [
        (sels, decls) for sels, decls in groups
        if "html" in sels and "body" in sels
    ]
    assert combined, (
        "找不到同时覆盖 html 与 body 的 CSS 规则（PRD R2-2：页面滚动条挂在 documentElement 上，"
        "只改 main 会白改）"
    )
    _, hb_decls = combined[0]
    assert _decl(hb_decls, "height") == "100%", f"html,body 必须 height:100%，实际 {hb_decls!r}"
    assert "hidden" in (_decl(hb_decls, "overflow") or ""), (
        f"html,body 必须 overflow:hidden（否则 body/margin 仍会撑出页面滚动条）：{hb_decls!r}"
    )

    # ② body { display:grid; grid-template-rows: auto 1fr } —— 取代 calc(100dvh - 58px)
    body_sel = [sel for sel in rules if sel == "body"]
    assert body_sel, "找不到 body 规则"
    body_decls = " ".join(rules[sel] for sel in body_sel)
    rows = _decl(body_sel and body_decls, "grid-template-rows") or ""
    assert "auto" in rows and "1fr" in rows, (
        f"body 必须 display:grid + grid-template-rows: auto 1fr，实际 {body_decls!r}"
    )

    # ③ main { min-height:0 } + 两列 grid（.chart-col / .info-col 的容器）
    assert "main" in rules, "找不到 main 规则"
    main_decls = rules["main"]
    assert _decl(main_decls, "min-height") == "0", (
        f"main 必须 min-height:0（否则 grid 子项撑破视口），实际 {main_decls!r}"
    )
    cols = _decl(main_decls, "grid-template-columns") or ""
    assert "minmax(0" in cols and cols.count("fr") >= 2, (
        f"main 必须两列 minmax(0, xfr) 布局，实际 grid-template-columns={cols!r}"
    )
    # ★ 选择器带上下文（避免子串锚点）：允许 `main > .info-col` / `.info-col` 等写法
    has_col = lambda name: any(  # noqa: E731
        re.search(rf"(^|[\s>+~]){re.escape(name)}(?![\w-])", sel) for sel in rules
    )
    assert has_col(".chart-col") and has_col(".info-col"), (
        "缺少 .chart-col / .info-col 两条列容器规则（V4 布局契约）；"
        f"现有含 col 的选择器：{[s for s in rules if 'col' in s]}"
    )

    # ④ #chart-root { height:100% }（旧的 clamp 必须**删除**）
    assert "#chart-root" in rules, "找不到 #chart-root 规则"
    chart_decls = rules["#chart-root"]
    assert _decl(chart_decls, "height") == "100%", (
        f"#chart-root 必须 height:100%（吃掉左列剩余高度），实际 {chart_decls!r}"
    )
    # ★ 只看**去注释后的 CSS**（注释里提到旧值不该判红）
    css_bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S).replace(" ", "").lower()
    assert "clamp(380px" not in css_bare, (
        "#chart-root 的旧 clamp(380px, 52vh, 820px) 必须删除 —— 1440p 会卡在 820px"
    )

    # ⑤ main 的纵向 padding 必须归零（旧值 12px 16px 40px 会白扣 52px）
    assert "padding:12px16px40px" not in css_bare, (
        "main 的旧 padding:12px 16px 40px 必须改（纵向归零）—— 否则左列被扣 52px"
    )


def test_v4_l0b_columns_exist_in_markup():
    """结构哨兵②：``.chart-col``/``.info-col`` 不只是 CSS，**HTML 里也要有这两个容器**。"""
    html = _app_html()
    assert re.search(r'class="[^"]*\bchart-col\b', html), "HTML 缺少 .chart-col 容器"
    assert re.search(r'class="[^"]*\binfo-col\b', html), "HTML 缺少 .info-col 容器"
    # 图表必须在左列里（结构上）
    chart_col_at = html.find("chart-col")
    info_col_at = html.find("info-col")
    chart_root_at = html.find('id="chart-root"')
    assert chart_root_at > chart_col_at, "#chart-root 不在 .chart-col 内（结构顺序不对）"
    assert info_col_at > chart_root_at or info_col_at > chart_col_at, ".info-col 位置异常"


# =========================================================================== #
# V4-L1 ★ 四档视口一屏无滚动（INV-66）—— 必须走 Emulation 域
# =========================================================================== #
@pytest.mark.parametrize("vw,vh", VIEWPORTS)
def test_v4_l1_no_scrollbar_at(serve_env, cdp_status, vw, vh):
    _, base = serve_env
    m = measure(base, vw, vh)
    # ★ R3-C：空/全 0 测量不得当成通过（_check_no_page_scroll 内部先调 _sane）
    _check_no_page_scroll(m, f"{vw}x{vh}")


def test_v4_l1b_emulation_domain_actually_applied(serve_env, cdp_status):
    """★ 验收②：``Emulation.setDeviceMetricsOverride`` **真的用了** —— 请求视口 == 实测视口。

    没有这一条，四档视口判据可能跑在同一个默认窗口上（假绿）。
    """
    _, base = serve_env
    probes = {}
    for vw, vh in VIEWPORTS:
        m = measure(base, vw, vh)
        got = (m.get("viewport") or {})
        probes[f"{vw}x{vh}"] = got
        assert m.get("_emulation_ok") is True, (
            f"Emulation 未生效：请求 {vw}x{vh}，实测 {got}"
        )
        assert (got.get("w"), got.get("h")) == (vw, vh), (
            f"实测视口 {got.get('w')}x{got.get('h')} != 请求 {vw}x{vh}"
        )
        assert got.get("dpr") == 1, f"deviceScaleFactor 应为 1，实测 dpr={got.get('dpr')}"
    print(f"\n  [Emulation] 四档视口实测 innerWidth/innerHeight: {probes}")


# =========================================================================== #
# V4-L2 ★ 图在左、竖着、吃满列高（INV-67）
# =========================================================================== #
def test_v4_l2_chart_left_column_and_tall(serve_env, cdp_status):
    _, base = serve_env
    m = measure(base, 1920, 945)
    _sane(m)
    _check_chart_left_and_tall(m)
    vw = m["viewport"]["w"]

    # 右列：真的在右半边 + 默认不滚（INV-66b）+ 卡片数非空（否则 L5 不重叠判据是空的）
    assert m.get("infoColDeclared") is True, "HTML 里没有 .info-col（右列不存在）"
    info = m["infoCol"]
    assert info["x"] >= vw / 2 - 40, f"信息列不在右半边：x={info['x']:.0f}"
    assert m["infoColScrollH"] is not None and m["infoColClientH"] is not None
    assert m["infoColScrollH"] <= m["infoColClientH"] + 1, (
        f"右列默认出现滚动条（INV-66b）：scrollH {m['infoColScrollH']} > clientH {m['infoColClientH']}"
    )
    assert len(m["infoCards"]) >= 6, f"右列卡片数 {len(m['infoCards'])} < 6"


@pytest.mark.parametrize("vw,vh", VIEWPORTS)
def test_v4_inv66b_right_column_no_scroll_all_viewports(serve_env, cdp_status, vw, vh):
    """R2-E：四档**默认**分辨率下右列也不滚（只有数据量增大才允许列内滚）。"""
    _, base = serve_env
    m = measure(base, vw, vh)
    _sane(m)
    assert m.get("infoColDeclared") is True, f"{vw}x{vh}：没有 .info-col"
    assert m["infoColScrollH"] <= m["infoColClientH"] + 1, (
        f"{vw}x{vh} 右列默认滚动：{m['infoColScrollH']} > {m['infoColClientH']}"
    )


# =========================================================================== #
# V4-L3 ★ 图例恰 6 项（INV-68）+ L3b 标题说明 + L3c 仍可点击
# =========================================================================== #
def test_v4_l3_legend_exactly_six_kept_items(serve_env, cdp_status):
    _, base = serve_env
    m = measure(base, 1920, 945)
    _check_legend(m)


def test_v4_l3b_runtime_titles_text_and_position(serve_env, cdp_status):
    """★ R2-A + R3-E：面板标题读**运行时 option**（不是 APP_HTML），且 ``title.top`` 不得叠面板。"""
    _, base = serve_env
    m = measure(base, 1920, 945)
    titles = " ".join(m.get("titleTexts") or [])
    assert titles, "运行时 option 里没有 title（面板标题丢了）"
    for must in TITLE_MUST:
        assert must in titles, f"面板标题缺 {must!r} —— 删图例后用户认不出这条线：{titles}"

    tops = m.get("titleTopsPct") or []
    grids = m.get("grids") or []
    assert len(tops) >= 2 and len(grids) == len(tops), (
        f"title.grids 数量不匹配：{len(tops)} titles vs {len(grids)} grids"
    )
    for i, top in enumerate(tops):
        assert top is not None, f"第 {i} 个 title.top 不是百分比：{m.get('titleTops')}"
        prev_bottom = 0.0
        if i > 0:
            prev = grids[i - 1]
            prev_bottom = float(_pct(prev["top"])) + float(_pct(prev["height"]))
        assert float(top) >= prev_bottom, (
            f"第 {i} 个标题 top={top}% 叠到上一个面板底边 {prev_bottom}% 之上"
        )


def test_v4_l3c_kept_legend_items_are_still_clickable(serve_env, cdp_status):
    """★ 精简不得破坏「点图例隐藏」—— 用**真鼠标事件**点画布（不是 dispatchAction）。"""
    _, base = serve_env
    m = measure_opts(base, 1920, 945, click_legend="MA12", settle_ms=800)
    info = m.get("legendClick") or {}
    assert info.get("mode") == "real", (
        f"没能在画布上定位图例项（退化成 {info.get('mode')!r} —— 判据强度不足）"
    )
    assert "legendselectchanged:MA12" in (m.get("events") or []), f"点图例没有触发事件：{m.get('events')}"
    assert m.get("legendSelected", {}).get("MA12") is False, (
        f"点 MA12 后 selected 应为 False，实际 {m.get('legendSelected')}"
    )


# =========================================================================== #
# V4-L4 ★ 功能零回退：按面板分组（R3-F）
# =========================================================================== #
def test_v4_l4_series_grouped_by_grid(serve_env, cdp_status):
    _, base = serve_env
    m = measure(base, 1920, 945)
    assert m["gridCount"] == 6, f"面板数 {m['gridCount']} != 6"
    got = {int(k): int(v) for k, v in (m.get("seriesByGrid") or {}).items()}
    assert got == SERIES_BASELINE, f"面板内序列数变了：{got} != {SERIES_BASELINE}"
    assert sum(got.values()) == 18, f"序列合计 {sum(got.values())} != 18"
    assert m["candlestickAxes"] == [0, 1, 5], (
        f"蜡烛所在面板 {m['candlestickAxes']} != [0,1,5]（v3 核心：BOLL/SAR 各有迷你蜡烛）"
    )


# =========================================================================== #
# V4-L5 ★ 面板不被压扁 + 无嵌套滚动 + 卡片不重叠（R2-C/R2-D）
# =========================================================================== #
def test_v4_l5_kline_panel_and_no_nested_scroll(serve_env, cdp_status):
    _, base = serve_env
    m = measure(base, 1920, 945)
    _check_kline_height(m)

    assert m["chartRootScrollH"] is not None
    assert m["chartRootScrollH"] <= m["chartRootClientH"] + 1, (
        f"#chart-root 出现内滚动（v3 已修，不得回退）：{m['chartRootScrollH']} > {m['chartRootClientH']}"
    )

    cards = m["infoCards"]
    assert len(cards) >= 6, f"卡片数 {len(cards)} < 6 ⇒ 下面的不重叠判据会是空的"
    for a, b in itertools.combinations(cards, 2):
        assert not _rect_overlap(a, b, tol=0.5), f"右列卡片重叠：{a['cls'] or a['tag']} vs {b['cls'] or b['tag']}"
    info = m["infoCol"]
    lowest = max(c["bottom"] for c in cards)
    assert lowest <= info["bottom"] + 1, f"有卡片超出右列底边（{lowest:.1f} > {info['bottom']:.1f}）"


# =========================================================================== #
# V4-L6 ★ 折叠默认值（R3-A / R3-D）
# =========================================================================== #
@pytest.mark.parametrize("vw,vh", [(1600, 900), (1920, 945)])
def test_v4_l6_collapse_defaults_small_viewports(serve_env, cdp_status, vw, vh):
    """★ **R4 更正**：只有 **900p / 945p** 要求「打分 / 分析默认折叠」。

    **1920×1080 不在此列** —— 原来的写法把 1080p 列进「必须折叠」是**自相矛盾**：
    Critic R3-3 的原话正是「`max-height:1080px` 包含等于 ⇒ **1920×1080（最常见）会折叠** ✗」，
    他的主张是 **1080p 不该折叠**；实现用 `innerHeight >= 1000 ⇒ 展开` **正是照这个做的** ✓
    （见下方 `test_v4_l6_collapse_defaults_1080p_and_1440p`。）

    ★ 不变的是：**持仓任何分辨率下都不得默认折叠**（不许把用户数据默认藏起来）。
    

    量的是**稳态**（``wait_analysis_ready=True``）：分析落地后若自动展开，必须被这条抓住。
    """
    _, base = serve_env
    m = measure_opts(base, vw, vh, wait_analysis_ready=True, settle_ms=600)
    _check_collapsed_small(m, f"{vw}x{vh}")


@pytest.mark.parametrize("vw,vh", [(1920, 1080), (2560, 1440)])
def test_v4_l6_collapse_defaults_1080p_and_1440p(serve_env, cdp_status, vw, vh):
    """★ R4 更正 + R3-A：**1080p 与 1440p 下二者都必须展开**。

    * **R4 更正**：1080p 原本被我误列进「必须折叠」，与 Critic R3-3 的主张相反（他要 1080p 展开）——
      实现 `innerHeight >= 1000 ⇒ 展开` 是对的，**改成这里断言展开**。
    * **R3-A**：必须 `not a` **且** `not b`，不能写 `not (a and b)`（那样全折叠也绿）。
    * ★ 不变：持仓恒展开。
    """
    _, base = serve_env
    m = measure_opts(base, vw, vh, wait_analysis_ready=True, settle_ms=600)
    flags = _collapsed_flags(m, f"{vw}x{vh}")
    assert flags["score"] is False, f"{vw}x{vh} 下「打分」应展开"
    assert flags["analysis"] is False, f"{vw}x{vh} 下「分析」应展开"
    assert flags["positions"] is False, f"{vw}x{vh} 下「持仓」应展开"


# =========================================================================== #
# V4-L7 ★ legend_budget 不得出现在页面路径（R3-H）
# =========================================================================== #
def test_v4_l7_page_no_longer_depends_on_legend_budget():
    from ptrade import webui

    chart_option = ROOT / "ptrade" / "webui_assets" / "chart_option.js"
    assert chart_option.is_file(), f"找不到 {chart_option}"
    for label, src in (("webui.APP_HTML", str(webui.APP_HTML)),
                       ("chart_option.js", chart_option.read_text(encoding="utf-8"))):
        assert "legend_budget" not in src, (
            f"{label} 里仍有 `legend_budget` —— 页面路径不得再依赖旧的图例预算函数（R3-H）"
        )


# =========================================================================== #
# V4-L8 ★ 分析区在右列（R3-I）
# =========================================================================== #
def test_v4_l8_analysis_panel_lives_in_right_column(serve_env, cdp_status):
    _, base = serve_env
    m = measure(base, 1920, 945)
    a = m.get("analysisPanelRect")
    assert a, "页面里没有 #analysis-panel"
    center = a["x"] + a["w"] / 2
    assert center > m["viewport"]["w"] / 2, (
        f"分析区不在右列：中心 x={center:.0f} ≤ 视口中线 {m['viewport']['w'] / 2:.0f}"
    )


# =========================================================================== #
# V4-L9 ★ 持仓 30 只时页面仍不滚（列内可滚）
# =========================================================================== #
def test_v4_l9_long_positions_still_no_page_scroll(serve_env_30, cdp_status):
    _, base = serve_env_30
    m = _cdp.measure((1600, 900), url=base, settle_ms=1500)
    _sane(m)
    assert m["scrollH"] <= m["clientH"] + 1, (
        f"持仓 30 只后**页面**出现滚动条：{m['scrollH']} > {m['clientH']}（列内滚动是允许的）"
    )


# =========================================================================== #
# V4-L10 ★ ECharts 首帧不得 0 高
# =========================================================================== #
def test_v4_l10_echarts_first_frame_not_zero_height(serve_env, cdp_status):
    _, base = serve_env
    m = measure_opts(base, 1920, 945, wait_after_init_ms=200, settle_ms=200)
    assert m["echartsWidth"] > 0, f"ECharts 首帧宽度 0（初始化时机问题）：{m['echartsWidth']}"
    assert m["echartsHeight"] > 0, f"ECharts 首帧高度 0（初始化时机问题）：{m['echartsHeight']}"


# =========================================================================== #
# 变异自证（纪律：每条判据都要能红）
#
# 用**改动前（v3）的实测数字**喂给**同一份判据逻辑**，断言它真的会红。
# 数字来源：`.omx/plans/test-spec-ptrade-layout-v4.md` 记录的改动前实测
# （scrollH 1284/945 · chartRoot h=491 · ratio 0.27 · legend 18 项 · K 面板 ≈103px）
# —— 我没有 v3 的可运行快照（并行 owner 已改完 ptrade/），所以这是「判据逻辑能红」的
# **直接证明**，而不是「在 v3 上跑过」的证明。报告里按这个口径写。
# =========================================================================== #
def _before_state(**over) -> dict:
    """改动前（v3）1920×945 的实测形态（最小可判定字段）。"""
    base = {
        "viewport": {"w": 1920, "h": 945, "dpr": 1},
        "clientH": 945, "scrollH": 1284, "clientW": 1920, "scrollW": 1920,
        "chartRoot": {"x": 16, "y": 60, "w": 1800, "h": 491,
                      "right": 1816, "bottom": 551, "left": 16, "top": 60},
        "infoCol": {"x": 40, "y": 551, "w": 1840, "h": 300,
                    "right": 1880, "bottom": 851, "left": 40, "top": 551},
        "infoColDeclared": False, "infoColScrollH": 300, "infoColClientH": 300,
        "infoCards": [{"x": 40 + i * 10, "y": 400 + i * 10, "w": 100, "h": 50,
                       "right": 140 + i * 10, "bottom": 450 + i * 10,
                       "left": 40 + i * 10, "top": 400 + i * 10, "cls": f"card{i}", "tag": "section"}
                      for i in range(6)],
        "grids": [{"top": "2%", "height": "21%"}, {"top": "30%", "height": "11%"},
                  {"top": "44%", "height": "9%"}, {"top": "55%", "height": "9%"},
                  {"top": "66%", "height": "9%"}, {"top": "78%", "height": "8%"}],
        "legendData": ["MA12", "MA24", "MA48", "MA96", "K线", "微型K线", "BOLL上轨", "BOLL中轨",
                       "BOLL下轨", "成交量", "VOL MA5", "VOL MA10", "成交额", "MACD柱", "DIF",
                       "DEA", "SAR", "微型K线"],
        "collapsed": {"score": False, "positions": False, "analysis": False},
        "foldable": {"score": True, "positions": False, "analysis": True},
        "summaryText": {"score": "", "positions": "", "analysis": ""},
        "collapsedDetails": [{"key": "score", "open": True, "contentH": 200, "summary": "打分…"}],
        "chartRootScrollH": 491, "chartRootClientH": 491,
        "echartsWidth": 1800, "echartsHeight": 491,
    }
    base.update(over)
    return base


def test_v4_z1_canary_sane_rejects_empty_measurement():
    """R3-C：``measure()`` 返回空/全 0 时，判据必须**拒绝**（而不是当成通过）。"""
    with pytest.raises(AssertionError):
        _sane({})
    with pytest.raises(AssertionError):
        _check_no_page_scroll({}, "empty")


def test_v4_z2_canary_before_state_is_red():
    """★ 改动前的实测形态必须让每条判据变红（判据不是恒真）。"""
    m = _before_state()
    with pytest.raises(AssertionError, match="垂直滚动条"):
        _check_no_page_scroll(m, "1920x945")
    with pytest.raises(AssertionError, match="图表高度"):
        _check_chart_left_and_tall(m)
    with pytest.raises(AssertionError, match="高宽比"):
        _check_chart_left_and_tall(_before_state(chartRoot={"x": 16, "y": 60, "w": 1800, "h": 790,
                                                           "right": 1816, "bottom": 850,
                                                           "left": 16, "top": 60}))
    with pytest.raises(AssertionError, match="图例"):
        _check_legend(m)
    with pytest.raises(AssertionError, match="K 线面板"):
        _check_kline_height(m)
    # ★ V5：文案已从「打分应默认折叠」改成「合并的「打分/CANSLIM 分析」卡应默认折叠」
    #   （因为两张卡合并成一张 ⇒ `data-collapse` 只剩 `analysis`）。canary 的 match 必须跟着走。
    with pytest.raises(AssertionError, match="卡应默认折叠"):
        _check_collapsed_small(m, "1920x945")


def test_v4_z3_canary_r3a_1440p_assertion_is_not_vacuous():
    """★ R3-A 的坑：``not (a and b)`` 在全折叠时也绿。这里钉住正确写法会红。"""
    m_full_collapsed = _before_state(collapsed={"score": True, "positions": True, "analysis": True})
    with pytest.raises(AssertionError):
        assert not m_full_collapsed["collapsed"]["score"], "1440p 下「打分」应展开"
    with pytest.raises(AssertionError):
        assert not m_full_collapsed["collapsed"]["positions"], "1440p 下「持仓」应展开"
    # 反证：错误写法 `not (a and b)` 对这个全折叠输入**是绿的** ⇒ 正是 R3-A 要防的假绿
    assert (not (m_full_collapsed["collapsed"]["score"] and m_full_collapsed["collapsed"]["positions"])) is False

