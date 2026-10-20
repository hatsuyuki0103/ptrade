# -*- coding: utf-8 -*-
"""V3 浏览器级验证（**重写**，task-37）：同页图表 · 高度自适应 · ECharts 交互。

## 与旧版（PN 阶段，151 行）的关系：这是**替换**，不是删断言

旧版验的是「**iframe 内**的图表 DOM」（iframe 正是 V3 要消灭的形态）。V3-A5 之后图表在主页面里，
旧判据**全部失效**；本文件重写为三类证据，并**逐条标注强度**：

| 类别 | 用什么验 | 强度 |
|---|---|---|
| **结构**：无任何嵌套框架、`#chart-root` 在、旧 `chart-frame` 彻底消失 | 真 Chrome `--dump-dom` | 真机渲染 ✓ |
| **资源**：ECharts 与 option 构建器都由**本地**提供、MIME 正确、构建器是**单一真源** | 真 HTTP（loopback） | 协议级 ✓ |
| **交互**：tooltip / legend / dataZoom 在 option 里**真实存在**且含迷你蜡烛 | Node 里对 `chart_option.js` **真调用** `buildOption` | **API 级** |

## ★★ 诚实声明：**真机手势未验证**

tooltip 悬停、dataZoom 拖拽/滚轮、点击图例切换 —— 需要 CDP 输入事件（鼠标/滚轮），本轮**没有**做。
评审判定：「配了 tooltip/dataZoom」= §8.3 形态④（只验配置不验语义），所以**只能**记「API 级」，
**不得**记「交互已验证」。见文件末尾的 `skip` 记录（机器可读）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ptrade import chart as chart_mod  # noqa: E402
from ptrade import webapp as webapp_mod  # noqa: E402
from ptrade.config import load_config  # noqa: E402

CHROME_CANDIDATES = [
    Path(r"C:\Users\yaoyufeng\AppData\Local\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]
CHROME = next((p for p in CHROME_CANDIDATES if p.is_file()), None)
NODE = shutil.which("node")

#: conftest 的 `_no_network` 会封掉 socket.connect；真 loopback 用例需要原版
import socket as _socket_mod  # noqa: E402

_REAL_CONNECT = _socket_mod.socket.connect
_REAL_CREATE_CONNECTION = _socket_mod.create_connection


def _copy_fixtures(root: str) -> None:
    for name in ("fixture", "scenario"):
        src = REPO_ROOT / "data" / name
        if not src.is_dir():
            continue
        dst = Path(root) / "data" / name
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.suffix == ".json":
                shutil.copy2(item, dst / item.name)
    cache = REPO_ROOT / "data" / "cache"
    if cache.is_dir():
        dst = Path(root) / "data" / "cache"
        dst.mkdir(parents=True, exist_ok=True)
        for item in cache.glob("*.json"):
            shutil.copy2(item, dst / item.name)


@pytest.fixture()
def served(tmp_path):
    """进程内 loopback 服务（port=0）：真浏览器与真 HTTP 断言都打在它上面。"""
    root = str(tmp_path / "root")
    _copy_fixtures(root)
    cfg = load_config(root)
    server = webapp_mod.create_server(cfg, host="127.0.0.1", port=0, offline=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], int(server.server_address[1])
    try:
        yield f"http://{host}:{port}", cfg
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[bytes, str]:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read(), str(resp.headers.get("Content-Type") or "")


def _dump_dom(url: str, tmp_path: Path) -> str:
    out = tmp_path / "dom.html"
    with open(out, "w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", f"--user-data-dir={tmp_path / 'profile'}",
             "--virtual-time-budget=8000", "--dump-dom", url],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=180,
        )
    assert proc.returncode == 0, f"Chrome 退出 {proc.returncode}"
    dom = out.read_text(encoding="utf-8", errors="replace")
    assert "<html" in dom.lower(), f"没有拿到 DOM：{dom[:200]!r}"
    return dom


# --------------------------------------------------------------------------- #
# 结构（真机渲染）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(CHROME is None, reason="本机没有 Chrome/Edge，浏览器级验证跳过")
def test_v3_real_dom_has_no_nested_frame(served, tmp_path) -> None:
    """★ V3-A5 / `T-V3-9`：**真实渲染后**的 DOM 里没有任何嵌套框架，图表容器在主页。"""
    base, _cfg = served
    dom = _dump_dom(base + "/", tmp_path)
    lowered = dom.lower()
    assert "<iframe" not in lowered, "★ 真实 DOM 里仍有嵌套框架 —— A5 未落地"
    assert "chart-frame" not in lowered, "旧的 `chart-frame` 引用必须彻底消失"
    assert 'id="chart-root"' in dom, "缺同页图表容器 `#chart-root`"
    assert 'data-testid="chart-root"' in dom
    assert dom.count('data-testid="stock-code-input"') == 1, "A3：代码输入框必须恰好一个"
    for tid in ("search", "chart-daily", "chart-minute", "watchlist-add", "order"):
        assert f'data-testid="{tid}"' in dom, f"操作条缺动作 {tid}"
    external = [u for u in re.findall(r'(?:src|href)="(https?://[^"]+)"', dom)
                if "127.0.0.1" not in u]
    assert external == [], f"真实 DOM 出现外部资源：{external}"


# --------------------------------------------------------------------------- #
# 资源（协议级）
# --------------------------------------------------------------------------- #
def test_v3_local_assets_are_served(served, monkeypatch) -> None:
    import socket as _socket

    # conftest 的 `_no_network` 会封掉 socket.connect；本用例需要真 loopback 请求
    monkeypatch.setattr(_socket.socket, "connect", _REAL_CONNECT)
    monkeypatch.setattr(_socket, "create_connection", _REAL_CREATE_CONNECTION)
    base, _cfg = served
    echarts, ctype = _get(base + "/vendor/echarts.min.js")
    assert len(echarts) > 1_000_000, f"ECharts 只有 {len(echarts)} 字节，疑似不是 vendored 全量"
    assert "javascript" in ctype, ctype
    assert b"Apache Software Foundation" in echarts[:400], "拿到的不是 ECharts 本体"

    option_js, ctype2 = _get(base + "/static/chart_option.js")
    assert "javascript" in ctype2, ctype2
    text = option_js.decode("utf-8")
    assert "export function buildOption" in text, "★ 必须原样返回 W1 的单一真源（含 export）"
    assert "export function miniCandlestick" in text, "缺迷你蜡烛构建器（BOLL/SAR 面板要用）"
    with pytest.raises(Exception):                 # noqa: B017 - HTTPError 即可
        _get(base + "/vendor/../ptrade/webapp.py")
    print(f"  [V3 资源] echarts={len(echarts):,}B · chart_option={len(option_js):,}B · 穿越=404 ✓")


# --------------------------------------------------------------------------- #
# 交互（**API 级**）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(NODE is None, reason="需要 node 才能求值 option")
def test_v3_option_has_tooltip_legend_datazoom_api_level(served, tmp_path) -> None:
    """★ **API 级**：`buildOption(...)` 产出的 option 里 tooltip / legend / dataZoom 都在，且含迷你蜡烛。"""
    base, cfg = served
    ds = webapp_mod._svc().make_ds(cfg, True)
    asof = str(webapp_mod._svc().viewer_asof(ds, "600519", None, []))[:10]
    payload = webapp_mod._svc().jsonable(
        chart_mod.chart_payload(cfg, ds, "600519", asof, include_today=True))
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    module_url = Path(chart_mod.CHART_OPTION_JS).resolve().as_uri()
    script = tmp_path / "opt.mjs"
    script.write_text(
        f'import {{ buildOption, miniCandlestick }} from "{module_url}";\n'
        "import { readFileSync } from 'node:fs';\n"
        f"const payload = JSON.parse(readFileSync({json.dumps(str(payload_file))}, 'utf8'));\n"
        "const opt = buildOption(payload, 'daily', "
        "{miniCandlestick: {boll: miniCandlestick(1), sar: miniCandlestick(5)}});\n"
        "const series = Array.isArray(opt.series) ? opt.series : [];\n"
        "const grids = series.filter(s => s.type === 'candlestick')\n"
        "  .map(s => (s.xAxisIndex !== undefined ? s.xAxisIndex : s.gridIndex));\n"
        "console.log(JSON.stringify({hasTooltip: !!opt.tooltip, hasLegend: !!opt.legend,\n"
        "  hasDataZoom: !!opt.dataZoom, gridCount: (opt.grid || []).length, series: series.length,\n"
        "  candleGrids: grids,\n"
        "  legendNames: ((opt.legend && opt.legend[0] && opt.legend[0].data) || []).length}));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, f"node 求值 option 失败：{(proc.stderr or '')[:800]}"
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["hasTooltip"] is True, "option 缺 tooltip（R4d）"
    assert data["hasLegend"] is True, "option 缺 legend（R4d）"
    assert data["hasDataZoom"] is True, "option 缺 dataZoom（R4d）"
    assert data["gridCount"] >= 6, f"面板数 {data['gridCount']} < 6（K线/BOLL/量/额/MACD/SAR）"
    assert data["series"] >= 10, f"series 只有 {data['series']} 条（K线+均线+BOLL+量+额+MACD+SAR）"
    assert 1 in data["candleGrids"] and 5 in data["candleGrids"], data["candleGrids"]
    print(f"  [V3 交互/API级] tooltip/legend/dataZoom ✓ · grid={data['gridCount']} · "
          f"series={data['series']} · 迷你蜡烛在 grid {data['candleGrids']}")


# --------------------------------------------------------------------------- #
# ★ 显式记录「未验证」
# --------------------------------------------------------------------------- #
@pytest.mark.skip(reason="**真机手势：手工 CDP 已验证，但本仓库的自动化未覆盖**。"
                         "证据：.omx/evidence/v3-gestures.json（tooltip: showtip+DOM 文案含 6 面板数值；"
                         "dataZoom: 真滚轮 start 75.2066→77.2935）· v3-legend3.json（真点击 → "
                         "legendselectchanged:K线 + selected['K线']=false）· v3-gesture-tooltip.png（截图）。"
                         "★ 需要用 Input.dispatchMouseEvent / mouseWheel；CI 无 Chrome/CDP 驱动 ⇒ 仍 skip。"
                         "★★ 不得把「手工验证过」写成「测试已覆盖」。")
def test_v3_real_gestures_manually_cdp_verified_but_not_automated() -> None:
    """把「**手工已验证 / 自动化未覆盖**」这个区别**显式记录下来**，不让它悄悄消失。

    ## 第 5 轮（2026-10-19）的手工 CDP 证据

    | 手势 | 做法 | 结果 |
    |---|---|---|
    | **tooltip** | `Input.dispatchMouseEvent{type:"mouseMoved"}` 移到数据点 | `showtip` 触发 42 次；**DOM 出现 tooltip**，文案含 6 面板数值（O/H/L/C/涨跌幅/量/MA/成交额/BOLL/MACD/SAR）|
    | **dataZoom** | `Input.dispatchMouseEvent{type:"mouseWheel", deltaY:-240}` ×2 | `datazoom` 事件 2 次；`dataZoom[0].start` **75.2066 → 77.2935** |
    | **图例点击** | `findHover` 定位图例组件 → 真 `mousePressed`/`mouseReleased` | `legendselectchanged:K线`；`legend.selected['K线'] = false`（该序列真的被隐藏）|

    ★ 图例这条**当轮修出了一个真缺陷（X1）**：Web 路径的 `/api/chart/data` 曾**不带 `legend`**
      ⇒ 前端 `legend.data = []` ⇒ **图例空、根本点不了** ⇒ 用户要求的「图例点击隐藏」实际未实现。
      修法：抽 `chart.option_data()` 作唯一真源，HTML 与 Web 两条路径共用；
      并用 **`T-V3-25`**（图例非空 + 两路径同源，纯 JSON、**不依赖 node**）钉死（变异已验证会红）。
    """

    # 这条测例**故意**不 assert 手势 —— 它只是「记录差异」的哨兵。
    # 真正的自动化需要 CI 具备 Chrome + CDP，属于基础设施决定，不是代码决定。
    assert True
