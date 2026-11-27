# -*- coding: utf-8 -*-
"""v18 判据：K 线「滚轮缩放 → 左右平移 → 再缩放」后线条乱飞/过冲出界。

真源：`.omx/specs/deep-interview-ptrade-v18.md`
用户原话：「滚轮缩放过后，再进行左右挪动，再缩放，k线有时候特别乱，你需要帮我修复这个bug」
用户裁决：症状 = **线条乱飞/过冲出界** · 频率 = **偶尔** · 均线形态 = **直线段**

## 根因（已核实）
`chart_option.js` 里 `smooth:true` 恰好 4 处 = MA12/24/48/96（**全在 panel ①**，截图里最乱的）。
ECharts `smooth:true` = Catmull-Rom 样条，控制点由相邻点**外推**求得 ⇒
窗口内波动剧烈时控制点被拉出数据范围 ⇒ 曲线**甩出面板** ✗（数据平坦时看不出 ⇒ 「偶尔」）

## 判据分工
* **V18-1..3**：node 里**真的调用 `buildOption`**，断言 option 结构（最强、无浏览器也跑）
* **V18-4**：真 Chrome（CDP）—— 跑真实缩放序列，读**渲染 model** 断言几何同步 + 无平滑来源
* **V18-5**：`node --check` 语法
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHART_OPTION_JS = ROOT / "ptrade" / "webui_assets" / "chart_option.js"
NODE = __import__("shutil").which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node（本机未装则如实跳过，不静默降级）")


def _run_node(source: str) -> dict:
    """在 node 里跑 ESM 源码，返回它 print 的 JSON（复用 test_v13 的既有模式）。"""
    with tempfile.TemporaryDirectory(prefix="v18js-", dir=r"D:\pytest-bt") as work:
        w = pathlib.Path(work)
        (w / "chart_option.mjs").write_text(CHART_OPTION_JS.read_text(encoding="utf-8"), encoding="utf-8")
        entry = w / "entry.mjs"
        entry.write_text(source, encoding="utf-8")
        p = subprocess.run([NODE, str(entry)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if p.returncode != 0:
            raise AssertionError(f"node 失败 rc={p.returncode}\nstdout={p.stdout[:900]}\nstderr={p.stderr[:2000]}")
        return json.loads((p.stdout or "").strip().splitlines()[-1])


def _payload(n: int = 1200) -> dict:
    """形态与 `/api/chart/data` 一致的 K 线 payload。

    ★ `bar_up` 必须给（`buildOption` 真的读 `d.bar_up[i]`，缺了会在构建期抛错）；
      这里故意给**剧烈波动**的数据 —— 正是会触发 spline 过冲的场景。
    """
    rng = list(range(n))
    # ★ code-review 建议10：原写法在 1200 根时会产生 `2026-43-xx` 这种**非法月份**。
    #   category 轴上只当 label 用所以无害，但若将来换 time 轴就会炸 ⇒ 改成真实递增日期。
    import datetime as _dt

    _base = _dt.date(2021, 1, 4)
    dates = [(_base + _dt.timedelta(days=i)).isoformat() for i in rng]
    # ★★★ 用户新截图提供的决定性信息：实际是**缩到很远**（时间轴 2022→2026，约 1000 根）
    #   才乱，**不是**缩到很近。机理：数据点密度 > 像素密度 ⇒ 样条控制点间距 < 1 像素
    #   ⇒ 一条均线在每个像素内反复折返、画出几十段锯齿 ⇒ 4 条均线叠成「无数条线」⇒ 乱成一团。
    #   ⇒ 本 fixture 用 **1200 根剧烈锯齿**（正是复现场景），不用平坦小样本糊过去。
    # ★ 剧烈锯齿：±3 元交替 ⇒ 样条控制点被外推很远（旧实现会画出密集折返）
    zig = [10.0 + (3.0 if i % 2 == 0 else -3.0) + i * 0.05 for i in rng]
    d = {
        "dates": dates,
        "ohlc": [[v, v + 1.5, v - 1.5, v + 2.0] for v in zig],
        "pct_chg": [(v - 10.0) for v in zig],
        "volume": [1000.0 + i * 100 for i in rng],
        "amount": [1.0e8 + i * 1e6 for i in rng],
        "vol_ma": {"5": zig, "10": zig},
        "ma": {"12": zig, "24": zig, "48": zig, "96": zig},
        "boll": {"lower": zig, "mid": zig, "upper": zig},
        "macd": {"dif": zig, "dea": zig, "hist": zig},
        "sar": {"value": zig, "trend": [1 if i % 2 == 0 else -1 for i in rng]},
        "bar_up": [True if i % 2 == 0 else False for i in rng],
    }
    return {"periods": {"daily": d}, "legend": [["MA12", "MA24", "MA48", "MA96"]],
            "initial_period": "daily", "code": "300869"}


_BUILD_JS = """
import { buildOption, miniCandlestick } from './chart_option.mjs';
const payload = %PAYLOAD%;
const opt = buildOption(payload, "daily", {
  miniCandlestick: { boll: miniCandlestick(1), sar: miniCandlestick(5) },
  getActive: () => "daily",
});
const series = (opt.series || []).map(s => ({
  name: s.name, type: s.type,
  smooth: (s.smooth === undefined ? null : s.smooth),
  clip: (s.clip === undefined ? null : s.clip),
  xAxisIndex: s.xAxisIndex, yAxisIndex: s.yAxisIndex,
  dataLen: (s.data || []).length,
}));
const dz = (opt.dataZoom || []).map(d => ({
  type: d.type, minValueSpan: (d.minValueSpan === undefined ? null : d.minValueSpan),
  filterMode: d.filterMode,
}));
console.log(JSON.stringify({series, dz, nSeries: series.length, xAxisCount: (opt.xAxis || []).length,
                            gridCount: (opt.grid || []).length}));
"""


# --------------------------------------------------------------------------- #
# V18-1 ★★ 核心判据：任何 line series 都不得有 truthy `smooth`
# --------------------------------------------------------------------------- #
@needs_node
def test_v18_1_no_line_series_is_smoothed():
    """★★ V18-1：**所有** `type:"line"` 的 series 不得有 truthy 的 `smooth`。

    ★ 这是本 bug 的**根因判据**：`smooth:true` = Catmull-Rom 样条，控制点外推 ⇒
      窄窗口 + 剧烈波动时曲线甩出面板（用户实测「有时候特别乱」）。
      直线段在**几何上不可能**超出相邻数据点的取值范围 ⇒ 过冲结构上根除。
    ★ 用**剧烈锯齿**数据构建（正是会触发过冲的场景），不是用平坦数据糊过去。
    """
    got = _run_node(_BUILD_JS.replace("%PAYLOAD%", json.dumps(_payload(), ensure_ascii=False)))
    lines = [s for s in got["series"] if s["type"] == "line"]
    assert len(lines) >= 8, f"line series 太少（{len(lines)}），判据会变空"
    bad = [s for s in lines if s["smooth"]]
    assert not bad, (
        f"以下 line series 仍开了 smooth（会让曲线过冲出面板）：{[(s['name'], s['smooth']) for s in bad]}"
    )
    # 反向确认：MA 四条**确实存在**且是 line（防止「删掉 series」式的假修复）
    names = {s["name"] for s in got["series"]}
    for must in ("MA12", "MA24", "MA48", "MA96"):
        assert must in names, f"均线 {must} 不见了（不许用删 series 来消灭过冲）"
    # ★ 反向确认数据真的灌进去了（防「给空数组」式的假修复）
    for s in got["series"]:
        if s["type"] in ("line", "candlestick", "bar"):
            assert s["dataLen"] == 1200, (
                f"{s['name']} 数据长度 {s['dataLen']} != 1200（fixture 没灌进去，判据会变空）")


# --------------------------------------------------------------------------- #
# V18-2 ★ 所有绘制型 series 显式 clip（不依赖 ECharts 默认）
# --------------------------------------------------------------------------- #
@needs_node
def test_v18_2_every_series_is_explicitly_clipped():
    """★ V18-2：所有绘制型 series 必须**显式** `clip:true`。

    ★ 为什么要求「显式」而非依赖默认：ECharts 5 对直角坐标系默认 clip=true，但那是**默认值**不是契约。
      本图 6 个 panel 上下紧贴，一条失控的线会串到隔壁 ⇒ 显式写死，将来任何越界绘制都被裁在自己的 grid 内。
    ★ `series.forEach` 兜底覆盖（见实现），所以**将来新增 series 也自动满足**。
    """
    got = _run_node(_BUILD_JS.replace("%PAYLOAD%", json.dumps(_payload(), ensure_ascii=False)))
    drawing = {"candlestick", "line", "bar", "scatter"}
    targets = [s for s in got["series"] if s["type"] in drawing]
    assert len(targets) >= 15, f"绘制型 series 太少（{len(targets)}）"
    bad = [s for s in targets if s["clip"] is not True]
    assert not bad, f"以下 series 未显式 clip:true：{[(s['name'], s['clip']) for s in bad]}"


# --------------------------------------------------------------------------- #
# V18-3 ★ dataZoom 必须有 minValueSpan（防缩到 1~2 根）
# --------------------------------------------------------------------------- #
@needs_node
def test_v18_3_datazoom_has_min_value_span():
    """★ V18-3：两个 dataZoom 组件都必须有**数字型** `minValueSpan >= 2`。

    ★ 没有它就能缩到 1~2 根：蜡烛被拉到整屏宽、均线退化成单段直线 ⇒ 同样是「乱」的观感。
    ★ 两个组件都要写 —— 只给 inside 写，拖 slider 仍能缩到 1 根。
    """
    got = _run_node(_BUILD_JS.replace("%PAYLOAD%", json.dumps(_payload(), ensure_ascii=False)))
    dzz = got["dz"]
    assert len(dzz) == 2, f"dataZoom 应 2 个（inside + slider），实际 {len(dzz)}"
    types = {d["type"] for d in dzz}
    assert types == {"inside", "slider"}, f"dataZoom 类型不对：{types}"
    for d in dzz:
        assert isinstance(d["minValueSpan"], (int, float)), f"{d['type']} 的 minValueSpan 缺失：{d['minValueSpan']}"
        assert d["minValueSpan"] >= 2, f"{d['type']} 的 minValueSpan={d['minValueSpan']} 太小（应 >= 2）"


# --------------------------------------------------------------------------- #
# V18-4 ★★ 真 Chrome：真实缩放序列后，几何同步 + 渲染态无平滑来源
# --------------------------------------------------------------------------- #
ZOOM_SEQ_JS = r"""
(() => {
  const root = document.querySelector('#chart-root');
  const inst = (root && window.echarts) ? window.echarts.getInstanceByDom(root) : null;
  if (!inst) return JSON.stringify({err: 'no-instance'});
  const A = (inst.getOption().xAxis && inst.getOption().xAxis[0] && inst.getOption().xAxis[0].data) || [];
  const N = A.length;
  const z = (a, b) => {
    inst.dispatchAction({type: 'dataZoom', dataZoomIndex: 0, startValue: a, endValue: b});
    inst.dispatchAction({type: 'dataZoom', dataZoomIndex: 1, startValue: a, endValue: b});
  };
  const cur = () => {
    const d = inst.getOption().dataZoom[0];
    let s = Number(d.startValue), e = Number(d.endValue);
    if (!isFinite(s)) s = 0;
    if (!isFinite(e)) e = N - 1;
    return [s, e];
  };
  let [s, e] = cur();
  const mid = Math.floor((s + e) / 2);
  const win = (c, w) => { const a = Math.max(0, Math.min(N - w - 1, c - Math.floor(w / 2))); return [a, a + w - 1]; };

  // ★ 用户描述的真实序列：缩放 → 左右平移 → 再缩放 → 再平移 → 再缩放
  let [a, b] = win(mid, 40); z(a, b);
  let span = b - a; let a2 = Math.min(N - 1 - span, a + 20); z(a2, a2 + span);
  let [a3, b3] = win(a2 + Math.floor(span / 2), 25); z(a3, b3);
  let a4 = Math.min(N - 1 - 25, a3 + 10); z(a4, a4 + 25);
  let [a5, b5] = win(a4 + 12, 15); z(a5, b5);

  const model = inst.getModel();
  const [ds, de] = cur();
  const winN = de - ds + 1;

  // 6 个 grid 的 rect 是否有效
  const grids = [];
  for (let gi = 0; gi < 6; gi++) {
    try {
      const g = model.getComponent('grid', gi);
      const r = g.coordinateSystem.getRect();
      grids.push({gi, w: Math.round(r.width), h: Math.round(r.height)});
    } catch (e) { grids.push({gi, err: String(e)}); }
  }

  // 每个 series 的真实点数 + 渲染态 smooth + clip
  const ss = model.getSeries();
  const series = ss.map((sm, i) => {
    let cnt = null;
    try { cnt = sm.getData().count(); } catch (e) { cnt = null; }
    let opt = {};
    try { opt = sm.option || {}; } catch (e) { opt = {}; }
    return {i, name: sm.name, type: sm.subType || sm.type, count: cnt,
            smooth: (opt.smooth === undefined ? null : opt.smooth),
            clip: (opt.clip === undefined ? null : opt.clip)};
  });

  // 每个 xAxis 的真实 extent
  const axes = [];
  for (let ai = 0; ai < 6; ai++) {
    try { axes.push(model.getComponent('xAxis', ai).axis.scale.getExtent()); }
    catch (e) { axes.push('ERR'); }
  }
  return JSON.stringify({N, ds, de, winN, grids, series, axes});
})()
"""


def test_v18_4_real_zoom_sequence_geometry_and_no_smooth():
    """★★ V18-4：真 Chrome 跑「缩放→平移→缩放→平移→缩放」后：
    ① 6 个 grid rect 有效
    ② 所有 series 的**渲染态**点数与 dataZoom 窗口根数一致（几何同步）
    ③ 所有 line series 的**渲染态** `smooth` 为 falsy（修复真的生效到渲染层）

    ★ 用 `dispatchAction` 模拟而不是真鼠标手势：CDP 的 `measure` 不提供滚轮/拖拽输入。
      这一点**如实声明**：本判据验的是「几何与渲染配置」，**不是**真实手势。真手势需人工验证。
    """
    try:
        from tests import _cdp
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"_cdp 不可用：{exc}")
    if not _cdp.status(probe=True).get("available"):
        pytest.skip("Chrome/CDP 不可用（如实跳过，不静默降级）")
    from ptrade.config import load_config
    from tests.test_ui_layout_v4 import _build_root

    cfg = load_config(str(_build_root(0)))
    with _cdp.serve(cfg, offline=True) as base:
        sess = _cdp._session()
        sess.measure((1920, 1080), url=base)                 # 先把图表画出来
        m = sess.measure((1920, 1080), url=base, extra_eval=ZOOM_SEQ_JS)
        _cdp.close()

    raw = m.get("extraEval") or m.get("extra_eval") or ""
    d = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
    assert not d.get("err"), f"CDP 取实例失败：{raw}"
    win_n = d["winN"]
    assert win_n >= 2, f"缩放窗口异常：winN={win_n}"

    # ① grid rect 有效
    for g in d["grids"]:
        assert not g.get("err"), f"grid {g['gi']} 取 rect 失败：{g.get('err')}"
        assert g["w"] > 0 and g["h"] > 0, f"grid {g['gi']} 尺寸为 0：{g}"

    # ② 6 个轴 extent 必须彼此一致（面板同步 ⇒ 不会上下错位）
    axes = d["axes"]
    assert all(isinstance(a, list) for a in axes), f"有轴取不到 extent：{axes}"
    first = axes[0]
    for i, a in enumerate(axes[1:], 1):
        assert a == first, f"xAxis[{i}] extent={a} 与 xAxis[0]={first} 不一致 ⇒ 面板错位"

    # ③ 所有 series 点数据 == 窗口根数（几何同步）
    bad_cnt = [(s["name"], s["count"]) for s in d["series"] if s["count"] != win_n]
    assert not bad_cnt, f"以下 series 点数与窗口 {win_n} 不一致：{bad_cnt}"

    # ④ 渲染态没有平滑来源（本 bug 的根因）
    bad_smooth = [(s["name"], s["smooth"]) for s in d["series"] if s["type"] == "line" and s["smooth"]]
    assert not bad_smooth, f"渲染态仍有平滑 line series（会过冲出面板）：{bad_smooth}"


# --------------------------------------------------------------------------- #
# V18-5 语法
# --------------------------------------------------------------------------- #
def test_v18_5_chart_option_js_syntax_ok():
    """V18-5：`chart_option.js` 仍是合法 ESM（`node --check`）。"""
    if NODE is None:
        pytest.skip("需要 node（本机未装则如实跳过）")
    p = subprocess.run([NODE, "--check", str(CHART_OPTION_JS)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 0, f"语法错：{(p.stderr or '')[:1500]}"


# --------------------------------------------------------------------------- #
# V18-6 ★ 静态资源缓存：改了 JS 必须让用户**普通刷新**就能拿到
# --------------------------------------------------------------------------- #
def test_v18_6_asset_cache_busting_and_revalidate():
    """★ V18-6：`chart_option.js` 必须带**内容指纹**版本串 + 响应头 `no-cache`。

    ★ 为什么这是**本 bug 的一部分**而不是"顺手优化"：
      修复前该资源以 `Cache-Control: public, max-age=86400` 且**无 ETag** 返回
      ⇒ 浏览器 **1 天内根本不发请求** ⇒ 用户普通刷新永远拿旧 JS
      ⇒ 修复"看起来没生效"（实测：用户截图里线条依旧乱，而磁盘文件已是新版）。
    ★ 两层保障：① URL 带内容哈希（唯一能立即绕过已缓存副本）② 响应头 no-cache（长期正确）。
    """
    from ptrade import webui

    html = webui.APP_HTML
    assert "__PTRADE_VER__" not in html, "版本占位符没被替换（模块末注入没执行）"
    import re

    m = re.search(r"/static/chart_option\.js\?v=([A-Za-z0-9._-]+)", html)
    assert m, "页面里的 chart_option.js URL 没带版本查询串（cache-busting 失效 ⇒ 发版后用户拿不到新版）"
    tag = m.group(1)
    assert len(tag.split("-")[-1]) >= 8, f"版本串 {tag!r} 里没有足够长的内容指纹（应含 chart_option.js 的 sha256 前缀）"
    assert "/static/chart_option.js" in html, "判据 test_webui.py::test_u1 依赖这个子串，不得删"

    # 内容指纹必须**随文件内容变化** ⇒ 手动重算校验（防止硬编码一个常量糊过去）
    import hashlib
    from ptrade import webui_assets  # noqa: F401  （确保包内路径可用）

    js_path = pathlib.Path(webui.__file__).with_name("webui_assets") / "chart_option.js"
    real = hashlib.sha256(js_path.read_bytes()).hexdigest()[:12]
    assert real in tag, f"版本串 {tag!r} 不含当前 chart_option.js 的 sha256 前缀 {real} ⇒ 指纹没随内容更新"


def test_v18_7_static_cache_control_headers():
    """★ V18-7：`chart_option.js` 必须是 `no-cache`；`echarts.min.js` 仍保持长缓存。

    ★ 不能一刀切把所有 JS 改 no-cache —— 1 MB 的 ECharts 几乎不变，
      长缓存是「pin 一次加载」的前提，动它会拖慢每次打开。
    """
    from ptrade import webapp
    from ptrade.config import load_config

    # ★★ B1（code-review 阻断）：**必须走 `dispatch` 路由**，不能直调 `vendor_asset()`。
    #   直调只能证明「函数在被显式传参时行为正确」，抓不到**路由层**把 `revalidate=True`
    #   删掉的回归 —— 而那正是本 bug 的唯一实际生效路径（用户从 `/static/...` 取 JS）。
    import tempfile as _tf

    with _tf.TemporaryDirectory(prefix="v18route-", dir=r"D:\pytest-bt") as work:
        app = webapp.WebApp(load_config(work), offline=True, lock_timeout=2.0)

        r_static = app.dispatch("GET", "/static/chart_option.js", {}, None)
        assert r_static.status == 200, f"/static/chart_option.js 返回 {r_static.status}"
        assert r_static.cache_control == "no-cache", (
            f"路由层 /static/chart_option.js 的 cache_control = {r_static.cache_control!r}"
            "（应 no-cache）—— 若这里变成 None/max-age，用户发版后普通刷新拿不到新版")

        r_vendor_cfg = app.dispatch("GET", "/vendor/chart_option.js", {}, None)
        assert r_vendor_cfg.status == 200, f"/vendor/chart_option.js 返回 {r_vendor_cfg.status}"
        assert r_vendor_cfg.cache_control == "no-cache", (
            "★ code-review 建议1：`/vendor/chart_option.js` 是**长缓存后门** —— "
            f"同一份代码资源两条路径两种策略（现在 {r_vendor_cfg.cache_control!r}）"
        )

        r_vendor_ec = app.dispatch("GET", "/vendor/echarts.min.js", {}, None)
        assert r_vendor_ec.status == 200
        assert r_vendor_ec.cache_control is None, (
            f"echarts.min.js 不应覆盖 cache_control（现在 {r_vendor_ec.cache_control!r}）"
            " —— 1MB 大文件要保持长缓存，不能被一刀切")



# --------------------------------------------------------------------------- #
# V18-8 的 node 脚本：对小数据集（0/1/2/3/6 根）真调 buildOption
# --------------------------------------------------------------------------- #
SMALL_JS = r"""
import { buildOption, miniCandlestick } from './chart_option.mjs';
const out = [];
for (const n of [0, 1, 2, 3, 6]) {
  const rng = [...Array(n).keys()];
  const v = rng.map(i => 10 + i);
  const d = {
    dates: rng.map(i => "2026-01-" + String(i + 1).padStart(2, "0")),
    ohlc: v.map(x => [x, x + 1, x - 1, x + 2]),
    pct_chg: v, volume: v, amount: v,
    vol_ma: {"5": v, "10": v}, ma: {"12": v, "24": v, "48": v, "96": v},
    boll: {lower: v, mid: v, upper: v}, macd: {dif: v, dea: v, hist: v},
    sar: {value: v, trend: rng.map(i => (i % 2 ? -1 : 1))}, bar_up: rng.map(() => true),
  };
  try {
    const opt = buildOption(
      {periods: {daily: d}, legend: [["MA12"]], initial_period: "daily", code: "x"},
      "daily",
      {miniCandlestick: {boll: miniCandlestick(1), sar: miniCandlestick(5)}, getActive: () => "daily"}
    );
    out.push({n: n, ok: true, mvs: opt.dataZoom.map(z => z.minValueSpan)});
  } catch (e) {
    out.push({n: n, ok: false, err: String(e)});
  }
}
console.log(JSON.stringify({out: out}));
"""

# --------------------------------------------------------------------------- #
# V18-8 ★ 小数据下 minValueSpan 不得大于数据量
# --------------------------------------------------------------------------- #
@needs_node
def test_v18_8_small_dataset_min_value_span():
    """★ V18-8（code-review 建议4）：数据只有 0/1/2/3/6 根时，`minValueSpan` 必须钳在 `[1, n-1]`。

    ★ 为什么需要：`minValueSpan:5` 是本次新增的。**新股/次新股的月线可能只有 2~3 根**
      ⇒ 若写死 5 就超过数据长度 ⇒ dataZoom 拿到矛盾区间（窗口下限 > 数据长度）⇒ 行为不确定。
      实现改成 `Math.max(1, Math.min(5, n-1))`：
        n=0 ⇒ 1 · n=1 ⇒ 1 · n=2 ⇒ 1 · n=3 ⇒ 2 · n=6 ⇒ 5
    ★ 断言两件事：① 不抛错（不崩）② 每个组件的 minValueSpan 都在 `[1, max(1, n-1)]` 内。
    """
    got = _run_node(SMALL_JS)
    for row in got["out"]:
        n = row["n"]
        assert row["ok"], f"数据 {n} 根时 buildOption 抛错：{row.get('err')}"
        upper = max(1, n - 1)
        for i, mvs in enumerate(row["mvs"]):
            assert isinstance(mvs, (int, float)), f"数据 {n} 根 · dataZoom[{i}] 的 minValueSpan={mvs!r} 不是数字"
            assert 1 <= mvs <= upper, (
                f"数据 {n} 根 · dataZoom[{i}] 的 minValueSpan={mvs} 超出 [1, {upper}] "
                "—— 窗口下限超过数据长度会让 zoom 行为不确定")
