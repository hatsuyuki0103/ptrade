# -*- coding: utf-8 -*-
"""tests/test_chart_v2.py —— W2 渲染层（PRD v2 §3 F3 / test-spec v2 T-W2-0 … T-W2-5）。

数据来源（test-spec §0.4 强制声明）
----------------------------------
* K 线：``offline_cfg``（tmp root，复制 ``data/fixture``）—— **不依赖仓库 ``data/cache``**；
* 分时：``data/fixture`` **无分时夹具** → 按 §0.7 用
  ``DataSource._write_entry(ds.cache_dir, "intraday_<code>", payload)`` **注入 tmp cache**；
* 「当天 bar」：再注入一份**新鲜**的 ``spot`` 缓存（asof = 注入的今天）+ **注入时钟**
  （``ds._today`` 实例属性 + ``chart.datetime`` 模块属性，test-spec §0.5 要求两处都注入）。

纪律：每条新判据都配了「变异验证」脚本的证据（见交付报告）；本文件里的每条断言都是
「注掉实现就必须红」的那种，不做恒绿断言。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from ptrade import chart as C
from ptrade.config import load_config
from ptrade.datasource import DataSource

BIG = "600519"

#: `node --check`（本机未装 node 时**如实跳过**，不静默降级）
NODE = shutil.which("node")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _inline_scripts(html: str) -> list[str]:
    """抽出页面里**所有** ``<script>`` 的内容（含内联 ECharts 与数据脚本）。"""
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


def _node_check(src: str) -> tuple[int, str]:
    """用 ``node --check`` 解析一段 JS，返回 ``(退出码, stderr)``。

    临时文件放系统 temp（小而短命）；**不写仓库**。
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8",
                                     newline="\n") as handle:
        handle.write(src)
        path = handle.name
    try:
        proc = subprocess.run([NODE, "--check", path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        return proc.returncode, (proc.stderr or "")
    finally:
        Path(path).unlink(missing_ok=True)


def _mini_candlestick_panels() -> set[int]:
    """从 ``chart.py`` 源码里取出 ``miniCandlestick(<n>)`` 的**调用点面板号**集合。

    ★ 结构收口后迷你蜡烛**只有一份实现**（`webui_assets/chart_option.js`）；
    `chart.py` 的页面胶水直接调 ``miniCandlestick(1)`` / ``(5)`` —— 这里就扫这两个调用点。
    ★ 用**源码调用点**而不是硬编码 ``{1, 5}``：将来新增共享迷你 K 线的面板会自动被要求配好 y 轴。
    """
    source = Path(C.__file__).read_text(encoding="utf-8")
    return {int(n) for n in re.findall(r"miniCandlestick\(\s*(\d+)\s*\)", source)}


def _yaxis_entries(html: str) -> list[str]:
    """从**渲染后**的 HTML 里切出 ``const yAxis = [ ... ]`` 的每个顶层对象（花括号配对）。"""
    text = html[html.index("const yAxis = ["):]
    entries: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
            if depth == 1:
                buf = []
        if depth:
            buf.append(ch)
        if ch == "}":
            depth -= 1
            if depth == 0:
                entries.append("".join(buf))
        elif ch == "]" and depth == 0 and entries:
            break
    return entries


def _yaxis_by_grid(html: str) -> dict[int, str]:
    """``{gridIndex: 该面板的 yAxis 配置文本}``（未写 gridIndex 的项按其数组位置）。"""
    out: dict[int, str] = {}
    for position, entry in enumerate(_yaxis_entries(html)):
        found = re.search(r"gridIndex\s*:\s*(\d+)", entry)
        out[int(found.group(1)) if found else position] = entry
    return out


def _assert_scripts_parse(scripts: list[str], label: str) -> None:
    failures: list[str] = []
    for index, src in enumerate(scripts):
        if not src.strip():
            continue
        code, err = _node_check(src)
        if code != 0:
            failures.append(f"段 {index}（{len(src):,} 字符）:\n{err[:1200]}")
    assert not failures, (
        f"★{label} 存在 JS 语法错误 —— 整段 <script> 不会执行，iframe 会停在「加载中…」：\n\n"
        + "\n\n".join(failures)
    )


def _flatten(groups) -> list[str]:
    return [name for group in groups for name in group]


def _inline_chart_data(html: str) -> dict:
    """取出页面内联的 ``const CHART_DATA = {...};``（避免对 1 MB 字符串直接断言）。"""
    marker = "const CHART_DATA = "
    assert marker in html, "页面必须内联 CHART_DATA"
    start = html.index(marker) + len(marker)
    end = html.index("};", start) + 1
    return json.loads(html[start:end])


def _series_names(html: str) -> list[str]:
    """页面 ``series:[...]`` 块里的 series 名（顺序即声明序）。"""
    flat = re.sub(r"\s+", " ", html)
    block = flat.split("const series = [", 1)[1].split("];", 1)[0]
    return re.findall(r'name:"([^"]+)"', block)


def _write_intraday_cache(ds: DataSource, code: str, days: dict[str, list[tuple]]) -> None:
    """注入分时缓存（tmp cache，不是夹具目录）；``days = {day: [(t, close, vol, amount), ...]}``。"""
    bars = []
    for day, points in days.items():
        for minute, close, vol, amount in points:
            bars.append({"day": day, "t": minute, "close": close, "volume": vol, "amount": amount})
    DataSource._write_entry(
        str(ds.cache_dir), f"intraday_{code}",
        {"bars": bars, "source": "sina", "fetched_at": datetime.now().isoformat()},
    )


def _write_spot_cache(ds: DataSource, asof: str, code: str, last: float, prev_close: float) -> None:
    """注入**新鲜**快照缓存（asof 必须等于被测的「今天」，否则 with_today_bar 拒合成）。"""
    DataSource._write_entry(
        str(ds.cache_dir), "spot",
        {
            "asof": asof,
            "rows": [{
                "code": code, "name": "贵州茅台", "last": last, "prev_close": prev_close,
                "open": last, "high": last, "low": last, "volume": 1_000_000,
                "amount": last * 1_000_000, "turnover": 1.0, "pe_ttm": 20.0, "pb": 3.0,
                "total_mv": 1e12, "float_mv": 1e12,
            }],
        },
    )


class _FakeDatetime(datetime):
    """注入 ``chart.datetime``（模块属性可换）—— 见 test-spec §0.5。"""

    fixed = datetime(2026, 10, 15, 10, 30)

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return cls.fixed


# --------------------------------------------------------------------------- #
# T-W2-0 ★ minute 不在 PERIODS
# --------------------------------------------------------------------------- #
def test_tw2_0_minute_is_not_in_periods():
    """★ P0：``minute`` **不得**进 ``PERIODS``（``resample_ohlc`` 对非日/周/月抛 ValueError）。"""
    assert C.PERIODS == ("daily", "weekly", "monthly")
    assert "minute" not in C.PERIODS
    assert C.PERIODS_WITH_INTRADAY == C.PERIODS + ("minute",)
    # 单一真源：对外清单必须**由 PERIODS 派生**（而不是各写一份字面量）
    assert C.PERIODS_WITH_INTRADAY[: len(C.PERIODS)] == C.PERIODS
    assert C.PERIOD_LABEL["minute"] == "分时"


def test_tw2_0_every_period_renders(offline_cfg, offline_ds):
    """遍历 ``PERIODS`` 调 ``chart_payload`` 不抛错（守住 ``resample_ohlc`` 的取值域）。"""
    asof = offline_ds.latest_trade_date()
    for period in C.PERIODS:
        payload = C.chart_payload(offline_cfg, offline_ds, BIG, asof)
        assert payload["periods"][period]["dates"], period
        html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof, period=period)
        assert f'"{period}":' in html, f"{period} 数据必须内联"


def test_tw2_0_minute_rejected_by_kline_pipeline(offline_cfg, offline_ds):
    """分时**不得**走 K 线管线：``render_chart(period="minute")`` 必须明确拒绝。"""
    asof = offline_ds.latest_trade_date()
    with pytest.raises(ValueError) as excinfo:
        C.render_chart(offline_cfg, offline_ds, BIG, asof, period="minute")
    assert "minute" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# T-W2-1 分时渲染
# --------------------------------------------------------------------------- #
def test_tw2_1_intraday_render_is_selfcontained(offline_cfg, offline_ds):
    """分时：自包含 HTML（零外链）+ 价格线 + 均价线 + 分时量。"""
    _write_intraday_cache(offline_ds, BIG, {
        "2026-10-09": [("09:30", 10.0, 100.0, 1000.0), ("09:31", 10.2, 120.0, 1224.0),
                       ("09:32", 10.1, 80.0, 808.0)],
        "2026-10-08": [("09:30", 9.8, 90.0, 882.0), ("09:31", 9.9, 110.0, 1089.0)],
    })
    html, payload = C.render_intraday(offline_cfg, offline_ds, BIG)

    assert payload["kind"] == "intraday"
    assert payload["day"] == "2026-10-09"                     # 缺省 = 最近一天
    assert payload["source"] == "sina"
    assert payload["times"] == ["09:30", "09:31", "09:32"]
    assert payload["price"][-1] == pytest.approx(10.1)
    assert payload["avg"][-1] == pytest.approx(3032.0 / 300.0)  # 累计 VWAP
    assert payload["vol"] == [100.0, 120.0, 80.0]
    assert payload["bar_up"] == [True, True, False]            # 与上一分钟比较

    assert html.lstrip().startswith("<!DOCTYPE html")
    assert C.no_external_refs(html) is True, C.external_refs(html)
    for token in ('name:"价格"', 'name:"均价"', 'name:"分时量"', 'type:"bar"'):
        assert token in html, token
    assert C.VENDOR_ECHARTS.read_text(encoding="utf-8")[:200] in html, "ECharts 必须内联"


def test_tw2_1_intraday_non_trading_day_falls_back_to_prev(offline_cfg, offline_ds):
    """非交易日（周六）→ 渲染**前一交易日**（不得报错、不得渲染未来日）。"""
    _write_intraday_cache(offline_ds, BIG, {
        "2026-10-09": [("09:30", 10.0, 100.0, 1000.0)],
        "2026-10-08": [("09:30", 9.8, 90.0, 882.0)],
    })
    _, payload = C.render_intraday(offline_cfg, offline_ds, BIG, "2026-10-10")   # 周六
    assert payload["day"] == "2026-10-09"
    # 注入了两天数据 → 必须是「前一交易日」，不是随便一天
    assert payload["day"] != "2026-10-08"

    # 请求一个**未来**的、数据里没有的交易日 → 不得回落到更晚的日期（数据里也没有）
    _, payload2 = C.render_intraday(offline_cfg, offline_ds, BIG, "2026-10-20")
    assert payload2["day"] == "2026-10-09"


def test_tw2_1_intraday_file_name_does_not_clobber_kline(offline_cfg, offline_ds):
    """★ 落盘名必须是 ``intraday_<code>_<day>.html``，**且不得覆盖** ``chart_<code>_<asof>.html``。"""
    _write_intraday_cache(offline_ds, BIG, {"2026-10-09": [("09:30", 10.0, 100.0, 1000.0)]})
    asof = offline_ds.latest_trade_date()

    kline_path = Path(C.build_chart(offline_cfg, offline_ds, BIG, asof))
    kline_before = kline_path.read_bytes()
    assert kline_path.name == f"chart_{BIG}_{asof}.html"

    intraday_path = Path(C.build_intraday(offline_cfg, offline_ds, BIG, "2026-10-09"))
    assert intraday_path.name == f"intraday_{BIG}_2026-10-09.html"
    assert intraday_path.parent == kline_path.parent
    assert intraday_path.is_file()
    assert kline_path.read_bytes() == kline_before, "生成分时**不得**改动 K 线图文件"
    assert Path(C.intraday_out_path(offline_cfg, BIG, "2026-10-09")) == intraday_path


# --------------------------------------------------------------------------- #
# T-W2-2 ★ 图例宽度预算
# --------------------------------------------------------------------------- #
def test_tw2_2_legend_groups_match_panels_and_page(offline_cfg, offline_ds):
    """分组数 == 面板数；Python 分组与**页面图例/运行时 series** 同源（不可能各自漂移）。

    ★ 2026-10-**改判据形态**（Lead 批准，V3-W1 / Critic C3）：③ 原写法在**源码字面量**里数
    series 声明数；迷你蜡烛现在由 ``opts.miniCandlestick`` **参数化**、在**运行时**生成 ——
    字面量里不该再有（有就是又在 HTML 里复制了一份，破坏单一真源）。
    ⇒ 改为对**运行时 option 对象**断言（node 求值页面真正会渲染的那份），**更强**：
    直接拿「运行时 series 名列表」与 Python 的 ``legend_groups()`` 比，而不是比源码文本。
    """
    asof = offline_ds.latest_trade_date()
    payload = C.chart_payload(offline_cfg, offline_ds, BIG, asof)
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)

    groups = C.legend_groups(payload)
    flat = re.sub(r"\s+", " ", html)
    grids = re.findall(r'\{left:66, right:26, top:"[^"]+", height:"[^"]+"\}', flat)
    assert len(groups) == len(grids) == 6, (len(groups), len(grids))
    assert all(group for group in groups), f"每个面板都必须有图例项：{groups}"

    # ① Python 分组 == 页面内联的 CHART_DATA.legend（同一真源）
    data = _inline_chart_data(html)
    assert data["legend"] == groups

    # ②③ 对**运行时 option** 断言（不是源码字面量）
    from tests.test_perf_analysis import _render_option_via_page  # 同一套 node 求值 helper

    option = _render_option_via_page(html)
    runtime_names = [s["name"] for s in option["series"] if s.get("name")]
    names_on_page = set(runtime_names)
    for name in _flatten(groups):
        assert name in names_on_page, f"图例项 {name} 在运行时 series 里没有对应项"

    # 覆盖性（契约原文「每组 series 数之和 == 总 series 数」）：
    # 「微型K线」在 BOLL 与 SAR 两个面板各一次 → 声明 18 条、去重 17 名，两者都断言。
    assert len(runtime_names) == sum(len(group) for group in groups), (runtime_names, groups)
    assert set(runtime_names) == set(_flatten(groups))
    assert len(set(runtime_names)) == 17 and len(runtime_names) == 18
    # 图例控件自己拿到的 data 也必须与之一致（页面真正绑定给 legend 的那份）
    assert option["legend"]["data"] == [n for n in runtime_names if n in set(option["legend"]["data"])]


def test_tw2_2_legend_budget_three_numbers(monkeypatch):
    """★ 三个数：现状不误红 / 30 项变异必红 / ``label_width=0`` 明令禁止。"""
    # ① 现状：最大组 5 项，label_width 推导 = 12×4 = 48 → 5×(14+10+48) = 360 ≤ 900
    assert C.legend_budget() is True
    assert C.legend_budget(label_width=50) is True          # 规格示例算法：5×74 = 370 ≤ 900

    # ② 变异：30 项一组 → 30×(14+10+50) = 2220 > 900 必须红
    monkeypatch.setattr(C, "LEGEND_BY_PANEL", (tuple(f"S{i}" for i in range(30)),))
    assert C.legend_budget(label_width=50) is False

    # ③ label_width=0 → 30×(14+10+0) = 720 ≤ 900 **恒绿** → 明令禁止（必须报错）
    with pytest.raises(ValueError) as excinfo:
        C.legend_budget(label_width=0)
    assert "恒绿" in str(excinfo.value)


def test_tw2_2_derived_label_width_can_never_be_zero(monkeypatch):
    """``label_width=None`` 时由名字推导（``12 × max(len(name))``）→ **从根上不可能取 0**。"""
    monkeypatch.setattr(C, "LEGEND_BY_PANEL", (("K",),))
    assert C.legend_budget(nominal_panel_width=1) is False    # 宽度小 → 说明 label_width > 0
    assert C._legend_label_width(["K"]) == C._LEGEND_LABEL_CHAR_PX
    assert C.legend_budget(label_width=None, nominal_panel_width=36) is True   # 1×(14+10+12)=36


# --------------------------------------------------------------------------- #
# T-W2-3 ★ 迷你 K 线复用
# --------------------------------------------------------------------------- #
def test_tw2_3_three_candlestick_series(offline_cfg, offline_ds):
    """candlestick series 数 == 3（主图 + BOLL + SAR），且分别在 panel 0 / 1 / 5。

    ★ 2026-10-**改判据形态**（Lead 批准，V3-W1 / Critic C3）：原写法在
    ``render_chart`` 的内联 HTML 里数**源码字面量** ``type:"candlestick"``；
    而迷你蜡烛现在由 ``opts.miniCandlestick`` **参数化**、在**运行时**由 JS 工厂生成 ——
    字面量里本来就不该再有它们（有就说明又在 HTML 里复制了一份，破坏单一真源）。
    ⇒ 改为对**运行时 option 对象**断言（node 求值页面真实会渲染的那份），**更强**：
    验的是「真会被渲染的对象」而不是「源码里写了几个」。**不得**为此在 HTML 里补字面量。
    """
    asof = offline_ds.latest_trade_date()
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)
    from tests.test_perf_analysis import _render_option_via_page  # 同一套 node 求值 helper

    option = _render_option_via_page(html)
    candles = [s for s in option["series"] if s.get("type") == "candlestick"]
    assert len(candles) == 3, f"candlestick 应为 3 颗（主图+BOLL+SAR），实际 {len(candles)}"
    assert sorted(s["xAxisIndex"] for s in candles) == [0, 1, 5], (
        f"三颗蜡烛的面板应为 0/1/5，实际 {[s['xAxisIndex'] for s in candles]}"
    )


def test_tw2_3_factory_change_moves_both_panels(offline_cfg, offline_ds):
    """★ **等价（并加强）**：迷你蜡烛**只有一份实现**，且 BOLL/SAR **两个面板都取该值**。

    ★ 2026-10-**改判据形态**（Lead 核准，V3-W1 结构收口）：原写法 monkeypatch
    ``chart._MINI_BAR_WIDTH`` 再数 HTML 里 ``barWidth:"62%"`` 出现 **2 次**（= 两处各自硬编码的防线）。
    现在 Python 那份工厂**已删**，全仓只有 ``chart_option.js`` 的 ``miniCandlestick()`` 一份实现
    ⇒ 页面里该值只出现 **1 次**（工厂定义处）。
    ★ **加强点**：不仅数次数，还断言两个面板的**运行时**蜡烛都真的取了工厂的值（原来只数文本）。
    """
    asof = offline_ds.latest_trade_date()
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)

    js_source = C.CHART_OPTION_JS.read_text(encoding="utf-8")
    found = re.search(r'barWidth:\s*"([^"]+)"', js_source)
    assert found, "chart_option.js 的迷你蜡烛工厂里找不到 barWidth"
    factory_bar_width = found.group(1)

    # ① 该**取值**在页面里只出现 1 次（只有工厂那一处；若又冒出第二处硬编码 → 变 2 次）
    occurrences = html.count(f'"{factory_bar_width}"')
    assert occurrences == 1, (
        f'"{factory_bar_width}" 在页面里应只出现 1 次（唯一实现），实际 {occurrences}'
    )
    assert C._load_chart_option_js() in html, "内联的必须是同一份工厂"

    # ② 两个面板的**运行时**蜡烛都取了工厂的值（加强）
    from tests.test_perf_analysis import _render_option_via_page

    option = _render_option_via_page(html)
    for axis in (1, 5):
        candles = [x for x in option["series"]
                   if x.get("type") == "candlestick" and x.get("xAxisIndex") == axis]
        assert len(candles) == 1, f"面板 {axis} 应有 1 颗迷你蜡烛"
        assert candles[0]["barWidth"] == factory_bar_width


# --------------------------------------------------------------------------- #
# T-W2-4 成交额成柱 + SAR 点不变（回归）
# --------------------------------------------------------------------------- #
def test_tw2_4_amount_is_bar_and_sar_regression(offline_cfg, offline_ds):
    asof = offline_ds.latest_trade_date()
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, asof)
    flat = re.sub(r"\s+", " ", html)

    # 成交额：柱状（按涨跌红/绿），仍挂独立面板 3
    assert re.search(r'name:"成交额", type:"bar".{0,200}xAxisIndex:3, yAxisIndex:3', flat), \
        "成交额必须是独立面板上的 bar"
    assert re.search(r'name:"成交额".{0,160}d\.bar_up\[i\] \? UP : DOWN', flat)
    assert 'name:"成交额", type:"line"' not in flat

    # SAR：仍是散点 + symbolSize 4 + 按 trend 红/绿（用户明确要求「红绿点不动」）
    assert re.search(r'name:"SAR", type:"scatter".{0,200}symbolSize:4', flat)
    assert re.search(r'name:"SAR".{0,160}d\.sar\.trend\[i\] > 0 \? UP : DOWN', flat)
    assert 'name:"SAR", type:"bar"' not in flat


# --------------------------------------------------------------------------- #
# T-W2-5 含今天标注 + latest 重算（时钟注入）
# --------------------------------------------------------------------------- #
def _today_payload(offline_cfg, offline_ds, monkeypatch, *, clock, include_today=True):
    monkeypatch.setattr(offline_ds, "_today", lambda: "2026-10-15")     # 实例属性遮蔽 staticmethod
    _FakeDatetime.fixed = clock
    monkeypatch.setattr(C, "datetime", _FakeDatetime)                   # 模块属性（§0.5 两处都注入）
    _write_spot_cache(offline_ds, "2026-10-15", BIG, last=1300.0, prev_close=1280.0)
    return C.chart_payload(offline_cfg, offline_ds, BIG, "2026-10-15",
                           include_today=include_today)


def test_tw2_5_include_today_merges_bar_and_recomputes_latest(offline_cfg, offline_ds, monkeypatch):
    """当天 bar 必须并入（指标随之重算），且 ``latest`` 从**合成后的** frame 重算。"""
    base = C.chart_payload(offline_cfg, offline_ds, BIG, "2026-10-15")   # 默认 include_today=False
    assert base["include_today"] is False and base["today_bar"] is False
    assert base["provisional"] is False
    base_bars = len(base["periods"]["daily"]["dates"])
    base_close = base["latest"]["close"]
    assert base_close != 1300.0

    payload = _today_payload(offline_cfg, offline_ds, monkeypatch,
                             clock=datetime(2026, 10, 15, 10, 30))
    assert payload["today_bar"] is True
    assert payload["provisional"] is True
    assert len(payload["periods"]["daily"]["dates"]) == base_bars + 1
    assert payload["periods"]["daily"]["dates"][-1] == "2026-10-15"
    # ★ latest 重算：图头必须显示今天的价（否则图与头自相矛盾）
    assert payload["latest"]["close"] == pytest.approx(1300.0)
    assert payload["latest"]["date"] == "2026-10-15"
    # 等长：当天 bar 在 add_indicators **之前**并入 → 各指标数组长度都跟着 +1
    frame = payload["periods"]["daily"]
    n = len(frame["dates"])
    assert all(len(v) == n for v in frame["ma"].values())
    assert all(len(v) == n for v in frame["macd"].values())
    assert len(frame["sar"]["value"]) == n and len(frame["ohlc"]) == n


# --------------------------------------------------------------------------- #
# M1（CR / INV-57）：不加 bar 必须**显式标注原因**（三态 + 未请求）
# --------------------------------------------------------------------------- #
def test_m1_today_bar_reason_three_states(offline_cfg, offline_ds, monkeypatch):
    """★ INV-57：``today_bar_reason`` 必须能区分四种情形（否则前端只能看到三个布尔）。

    取值域：``not-requested`` / ``added-fresh-snapshot`` / ``already-in-frame`` / ``no-fresh-snapshot``
    """
    # ① 没请求（默认）→ not-requested，且不得捏造其它状态
    off = C.chart_payload(offline_cfg, offline_ds, BIG, "2026-10-15")
    assert off["today_bar_reason"] == "not-requested"
    assert off["today_bar"] is False

    # ② 请求了但**真实日线已含当天**（帧末行 == asof）→ already-in-frame
    #    注：夹具 calendar 的 latest_trade_date()=2026-10-15 比日线数据（2026-10-09）更晚，
    #    所以「已含当天」必须用 latest_trade_asof()（= 真的有行情的那天）。
    covered = offline_ds.latest_trade_asof([BIG])
    probe = C.chart_payload(offline_cfg, offline_ds, BIG, covered)
    assert probe["periods"]["daily"]["dates"][-1] == covered, "前置：帧末行必须就是 covered"
    already = C.chart_payload(offline_cfg, offline_ds, BIG, covered, include_today=True)
    assert already["today_bar"] is False
    assert already["today_bar_reason"] == "already-in-frame"

    # ③ 快照新鲜且日期相符 → added-fresh-snapshot（此时才可能出现 provisional）
    fresh = _today_payload(offline_cfg, offline_ds, monkeypatch,
                           clock=datetime(2026, 10, 15, 10, 30))
    assert fresh["today_bar"] is True
    assert fresh["today_bar_reason"] == "added-fresh-snapshot"
    assert fresh["provisional"] is True

    # ④ 快照**陈旧**（缓存 asof ≠ 请求 asof）→ no-fresh-snapshot（不得静默当作"没有今天"）
    _write_spot_cache(offline_ds, "2026-10-14", BIG, last=1300.0, prev_close=1280.0)
    stale = C.chart_payload(offline_cfg, offline_ds, BIG, "2026-10-15", include_today=True)
    assert stale["today_bar"] is False
    assert stale["today_bar_reason"] == "no-fresh-snapshot"
    assert stale["provisional"] is False


def test_m1_today_bar_reason_suspended_is_not_greenwashed(offline_cfg, offline_ds, monkeypatch):
    """停牌 / 无成交的快照**同样不合成**，且原因必须落 ``no-fresh-snapshot``（不得标成 added）。"""
    monkeypatch.setattr(offline_ds, "_today", lambda: "2026-10-15")
    _write_spot_cache(offline_ds, "2026-10-15", BIG, last=0.0, prev_close=0.0)   # 无有效成交
    payload = C.chart_payload(offline_cfg, offline_ds, BIG, "2026-10-15", include_today=True)
    assert payload["today_bar"] is False
    assert payload["today_bar_reason"] == "no-fresh-snapshot"


def test_m1_reason_is_always_present_and_from_closed_set(offline_cfg, offline_ds):
    """字段恒存在且取值在**封闭集合**内（前端可穷举分支，不会拿到 None/未知串）。"""
    allowed = {"not-requested", "added-fresh-snapshot", "already-in-frame", "no-fresh-snapshot"}
    for include_today in (False, True):
        payload = C.chart_payload(offline_cfg, offline_ds, BIG,
                                  offline_ds.latest_trade_date(), include_today=include_today)
        assert "today_bar_reason" in payload
        assert payload["today_bar_reason"] in allowed
        # 与 today_bar 布尔一致：只有 added 才允许 today_bar=True
        assert (payload["today_bar"] is True) == (payload["today_bar_reason"] == "added-fresh-snapshot")


# --------------------------------------------------------------------------- #
# R1 ★ iframe 内联 JS 语法守卫（node --check 此前只覆盖 webui.APP_HTML）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(NODE is None, reason="需要 node 才能解析 JS（本机未装 node，如实跳过）")
def test_r1_kline_page_all_inline_scripts_parse(offline_cfg, offline_ds):
    """★ R1：**iframe 里的 K 线页**所有 ``<script>`` 必须能被 JS 引擎解析。

    背景：全仓的 ``node --check`` 取样一直只来自 ``webui.APP_HTML``，而 ``chart.py`` 的
    ``_APP_JS``（以及分时页的 ``_INTRADAY_JS``）从未被解析过 —— 2026-10-15 用户报的
    「打开网页看不到任何信息」**正是**内联 JS 语法错误（整段 ``<script>`` 静默不执行）。
    """
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, offline_ds.latest_trade_date())
    scripts = [src for src in _inline_scripts(html) if src.strip()]
    assert scripts, "K 线页里没有 <script>（页面不该是空的）"
    # 非空洞：必须真的抽到**我们自己的**那段（否则只剩 ECharts 内联库，判据打偏）
    assert any("function buildOption" in src and "switchPeriod" in src for src in scripts), \
        "没有抽到 _APP_JS（取样偏了 → 守卫空转）"
    _assert_scripts_parse(scripts, "K线页")


@pytest.mark.skipif(NODE is None, reason="需要 node 才能解析 JS（本机未装 node，如实跳过）")
def test_r1_intraday_page_all_inline_scripts_parse(offline_cfg, offline_ds):
    """★ R1：**iframe 里的分时页**所有 ``<script>`` 同样必须能解析（注入分时数据，不跳过该路径）。"""
    _write_intraday_cache(offline_ds, BIG, {
        "2026-10-09": [("09:30", 10.0, 100.0, 1000.0), ("09:31", 10.2, 120.0, 1224.0)],
    })
    html, payload = C.render_intraday(offline_cfg, offline_ds, BIG)
    assert payload["times"], "分时载荷必须非空（否则这条守卫是空的）"
    scripts = [src for src in _inline_scripts(html) if src.strip()]
    assert any("INTRADAY" in src and "buildOption" in src for src in scripts), \
        "没有抽到 _INTRADAY_JS（取样偏了 → 守卫空转）"
    _assert_scripts_parse(scripts, "分时页")


def test_r1_guard_is_not_vacuous():
    """**非空洞对照**：喂一段**已知坏 JS** → ``node --check`` **必须报错**。

    坏的形态正是本次事故：JS 字符串里嵌了未转义的引号。
    """
    if NODE is None:
        pytest.skip("需要 node")
    bad = 'function f(){ return "<tr data-code="" + x + ""><td>"; }'
    code, err = _node_check(bad)
    assert code != 0, "★R1 的判据无法判出坏脚本 —— 它是空守卫"
    assert "SyntaxError" in err or "Unexpected" in err, err[:300]


# --------------------------------------------------------------------------- #
# R6 ★ `_intraday_data`：守卫错误必须上抛，**且不得再发起第二次取数**
# --------------------------------------------------------------------------- #
def test_r6_guard_error_propagates_without_second_intraday_attempt(
    offline_cfg, offline_ds, monkeypatch
):
    """★ R6：``NetworkForbiddenInLock ⊆ DataUnavailable``，若 ``except DataUnavailable`` 在前面，
    守卫错误会掉进「换个交易日再试」的兜底 → **在锁内多发起一次取数**（INV-42 禁止）且掩盖真因。

    为什么除了接线判据还要这条：``tests/test_wiring_invariants_v2.py::test_W1_*`` 只做 AST
    **顺序**扫描 —— 它区分不了 ``except NetworkForbiddenInLock: raise`` 与
    ``except NetworkForbiddenInLock: return None``（后者照样吞）。这里用**调用计数**把行为钉死。
    """
    from ptrade.datasource import NetworkForbiddenInLock

    calls = {"n": 0}

    def _boom(code, *, day=None):
        calls["n"] += 1
        raise NetworkForbiddenInLock("注入：写锁临界区内禁止联网取数")

    monkeypatch.setattr(offline_ds, "intraday", _boom)
    with pytest.raises(NetworkForbiddenInLock):
        C.render_intraday(offline_cfg, offline_ds, BIG, "2026-10-10")
    assert calls["n"] == 1, (
        f"守卫错误之后仍发起了第 {calls['n']} 次取数 —— "
        "R6：锁内禁网场景下不得再试第二次（且真因会被第二次调用的堆栈掩盖）"
    )


def test_r6_ordinary_dataunavailable_still_falls_back(offline_cfg, offline_ds, monkeypatch):
    """对照：**普通** ``DataUnavailable``（非守卫）仍必须走「回落到上一交易日」的兜底，不得被搞坏。"""
    from ptrade.datasource import DataUnavailable

    calls = {"n": 0}

    def _flaky(code, *, day=None):
        calls["n"] += 1
        if day:                                  # 指定日没有 → 缺省日给上一交易日
            raise DataUnavailable("普通：该日无分时数据")
        return {"day": "2026-10-09", "points": [{"t": "09:30", "price": 10.0, "avg": 10.0,
                                                 "vol": 100.0, "amount": 1000.0}],
                "source": "sina", "complete": True, "fetched_at": "2026-10-15T10:00:00",
                "asof": "2026-10-09"}

    monkeypatch.setattr(offline_ds, "intraday", _flaky)
    html, payload = C.render_intraday(offline_cfg, offline_ds, BIG, "2026-10-10")
    assert payload["day"] == "2026-10-09"        # 兜底仍然生效
    assert calls["n"] == 2, "兜底路径必须恰好尝试两次（指定日失败 → 缺省日）"
    assert "09:30" in html


def test_tw2_5_provisional_annotation_is_not_always_on(offline_cfg, offline_ds, monkeypatch):
    """``provisional`` 决定是否含「未收盘」标注：盘中 True（标注在）/ 盘后 False（**不得**标注）。"""
    intraday = _today_payload(offline_cfg, offline_ds, monkeypatch, clock=datetime(2026, 10, 15, 10, 30))
    assert intraday["provisional"] is True
    html = C._render_html(intraday, None, "daily")
    assert "未收盘" in html

    after_close = _today_payload(offline_cfg, offline_ds, monkeypatch, clock=datetime(2026, 10, 15, 16, 0))
    assert after_close["today_bar"] is True          # bar 仍合成（快照确实是今天的）
    assert after_close["provisional"] is False       # 但**不得**贴「未收盘」标签
    html2 = C._render_html(after_close, None, "daily")
    assert "未收盘" not in html2

    # 历史日期（asof != 今天）→ 同样不得标注
    monkeypatch.setattr(offline_ds, "_today", lambda: "2026-10-15")
    hist = C.chart_payload(offline_cfg, offline_ds, BIG, offline_ds.latest_trade_date(),
                           include_today=True)
    assert hist["provisional"] is False
    assert "未收盘" not in C._render_html(hist, None, "daily")


# --------------------------------------------------------------------------- #
# V3 ★ 共享迷你 K 线的面板必须 scale:true（"SAR 面板没有蜡烛" 的真缺陷）
#
# 为什么以前全绿却漏掉了：series **确实存在**（名字对、数据对、node --check 过、AST 判据过），
# 问题**只在 y 轴配置**：ECharts 默认 scale:false ⇒ 轴从 0 起 ⇒ 1100-1400 的价格被压进
# 面板顶部约 20% ⇒ 蜡烛没有最小尺寸、被压成 <1 像素 ⇒ **完全不可见**；
# 而散点有固定 symbolSize:4，所以「还有东西在动」，看起来不像坏了。
# → 必须在**配置层**做一致性判据（Lead 用 Chrome 真渲染 + 肉眼放大才发现）。
# --------------------------------------------------------------------------- #
def _series_entries(html: str) -> list[str]:
    """切出页面 ``const series = [ ... ]`` 的每个顶层 series 对象（花括号配对）。"""
    text = html[html.index("const series = ["):]
    entries: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
            if depth == 1:
                buf = []
        if depth:
            buf.append(ch)
        if ch == "}":
            depth -= 1
            if depth == 0:
                entries.append("".join(buf))
        elif ch == "]" and depth == 0 and entries:
            break
    return entries


def _bar_panels(html: str) -> set[int]:
    """柱状（``type:"bar"``）series 所在的面板号集合。"""
    panels: set[int] = set()
    for entry in _series_entries(html):
        if not re.search(r'type\s*:\s*"bar"', entry):
            continue
        found = re.search(r"xAxisIndex\s*:\s*(\d+)", entry)
        if found:
            panels.add(int(found.group(1)))
    return panels


def test_v3_mini_candlestick_panels_must_have_scaled_yaxis(offline_cfg, offline_ds):
    """★ V3：**凡共享 `_miniCandlestick(n)` 的面板，`yAxis[n]` 必须含 `scale:true`。**

    面板号从 ``chart.py`` 源码的 `_miniCandlestick(<n>)` **调用点**派生（不是硬编码 {1,5}）：
    新增共享蜡烛的面板会自动被这条判据要求；改面板号而忘了改 y 轴会立刻红。
    修前（SAR 缺 scale:true）本用例**必红** —— 这正是它存在的意义。
    """
    panels = _mini_candlestick_panels()
    assert {1, 5} <= panels, f"取样打偏：没取到 BOLL(1)/SAR(5) 调用点（实际 {sorted(panels)}）"

    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, offline_ds.latest_trade_date())
    by_grid = _yaxis_by_grid(html)
    assert len(by_grid) == 6, f"应有 6 个图表面板的 yAxis，实际 {sorted(by_grid)}"

    missing = [n for n in sorted(panels) if "scale:true" not in by_grid[n]]
    assert not missing, (
        f"★面板 {missing} 铺了 `_miniCandlestick` 但 yAxis 缺 `scale:true` → 轴从 0 起，"
        "价格被压进面板顶部 ~20%，蜡烛被压成 <1 像素（**完全不可见**）；"
        "scatter 因有固定 symbolSize 仍可见，所以肉眼看像「只有散点没有蜡烛」"
    )


def test_v3_price_panels_scaled_bar_panels_zero_based(offline_cfg, offline_ds):
    """★ V3 对称判据：**柱状**面板必须从 0 起；**非柱状（价格）**面板必须 `scale:true`。

    我的判断（task-32 §2 要求写进报告）：
    * `yAxis[2]` 成交量 / `yAxis[3]` 成交额 / `yAxis[4]` MACD柱 —— **柱高编码的是量级**，
      轴若 `scale:true` 会**切掉 0 基线**，柱高比例失真（量小的柱子看起来和量大的一样高）；
      MACD 还靠 0 线判金叉/死叉。→ **三者都不得加 scale**（本用例将其钉住，防止"顺手加"）。
    * `yAxis[0]/[1]/[5]` —— 价格单位（K线/BOLL/迷你K线）→ **必须 scale:true**。
    """
    html, _ = C.render_chart(offline_cfg, offline_ds, BIG, offline_ds.latest_trade_date())
    by_grid = _yaxis_by_grid(html)
    bars = _bar_panels(html)
    assert bars == {2, 3, 4}, f"取样打偏：柱状面板应为 2/3/4，实际 {sorted(bars)}"

    scaled_bars = [n for n in sorted(bars) if "scale:true" in by_grid[n]]
    assert not scaled_bars, (
        f"★柱状面板 {scaled_bars} 不得有 scale:true —— 会切掉 0 基线，柱高比例失真"
    )

    unscaled_price = [n for n in sorted(set(by_grid) - bars) if "scale:true" not in by_grid[n]]
    assert not unscaled_price, (
        f"★价格面板 {unscaled_price} 必须有 scale:true —— 否则价格区间被压扁（蜡烛不可见）"
    )
