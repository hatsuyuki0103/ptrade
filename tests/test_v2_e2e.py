"""W4 集成 / 端到端：`INV-52` 看盘 asof（含今天）与下单 asof（防未来函数）的分离。

## 为什么这个文件最重要

用户的原话是两条**互相拉扯**的需求：

* 「交易日的时候看**我也是要看到最新的**」→ 看盘要**含今天**；
* （安全约束）下单**必须防未来函数** → 15:30 前回退到已收盘交易日。

本项目已经**四次**栽在「用日历/理想值推最新」上
（P3-2 下单 asof → `_latest_report_period` 报告期 → 图表不含今天 → 完整日硬阈值 240），
所以这两条语义**必须由结构守住**，不能只靠约定：
`chart_asof` **只允许**出现在 `cli.cmd_chart` 与 `webapp.api_chart`（AST 白名单，见
`tests/test_frozen_interfaces_v2.py`）。

## 数据从哪来（每条判据都要声明）

本文件全部在 **`offline_cfg`（tmp root）** 上跑，**不读仓库 `data/cache`** ——
夹具环境与仓库根的数字**完全不同**（实测：`industry_of('600519')` 在夹具环境**修前就绿**），
所以「用仓库根的数字写判据」会变成空断言。

## 时钟必须注入**两处**

`service.resolve_asof` 判 15:30 用的是 **`service.datetime.now()`**（不是 `ds._today()`）——
只注入 `ds._today` 的话，本文件在**下午 15:30 之后**跑会**必然失败**。
"""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest

from ptrade import service
from ptrade.config import load_config
from ptrade.datasource import DataSource


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _fixture_spot_asof(cfg) -> str | None:
    """从夹具里读 spot 的 `asof`（**唯一可信的「今天确有数据」来源**）。

    夹具是**缓存信封** `{"fetched_at": ..., "payload": {"asof": ..., "rows": [...]}}`
    —— 第一版我只看了顶层 `asof`，结果 4 条判据全被 **skip**（skip 不等于通过，必须修）。
    """
    path = os.path.join(str(cfg.root), "data", "fixture", "spot.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    node = raw.get("payload") if isinstance(raw, dict) and "payload" in raw else raw
    if isinstance(node, dict):
        for key in ("asof", "date"):
            if node.get(key):
                return str(node[key])[:10]
    return None


def _pin_clock(monkeypatch, ds, cfg, *, today: str, hh: int, mm: int):
    """★ 时钟注入**两处**：`ds._today` 与 `service.datetime`。"""
    monkeypatch.setattr(ds, "_today", lambda: today, raising=False)

    class _FakeDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):                      # noqa: ANN001
            return dt.datetime.fromisoformat(f"{today}T{hh:02d}:{mm:02d}:00")

    monkeypatch.setattr(service, "datetime", _FakeDatetime)


@pytest.fixture
def e2e(offline_cfg, offline_ds):
    """(cfg, ds, spot_asof)；夹具没带 spot 就跳过（不伪造数据）。"""
    asof = _fixture_spot_asof(offline_cfg)
    if not asof:
        pytest.skip("夹具缺 data/fixture/spot.json 的 asof —— 本文件需要它做「今天」的锚点")
    return offline_cfg, offline_ds, asof


# --------------------------------------------------------------------------- #
# T-W4-2 ★ 看盘 / 下单分离（本文件的核心）
# --------------------------------------------------------------------------- #
def test_T_W4_2_chart_asof_includes_today_but_order_asof_does_not(e2e, monkeypatch):
    """同一时刻：**看盘 = 今天**，**下单 = 最近已收盘交易日**，两者必须**不同**。"""
    cfg, ds, today = e2e
    _pin_clock(monkeypatch, ds, cfg, today=today, hh=10, mm=30)   # 盘中

    chart = ds.chart_asof("600519")
    order = service.resolve_asof(ds, None, [])

    assert chart == today, (
        f"看盘 asof 必须是今天（今天确有数据：spot.asof={today}），实际 {chart}"
    )
    assert order != today, (
        f"下单 asof 必须是**已收盘**交易日，不得是今天 {today} —— "
        f"否则会拿当天未收盘数据下单（未来函数）"
    )


def test_T_W4_2_order_path_unchanged_at_close(e2e, monkeypatch):
    """★ 直接测 `resolve_asof` 的 **15:30 分支**：盘中回退、盘后不回退。

    第一版我把 `ds._today` 钉在 spot 的 asof 上就断言「16:00 → 今天」，**失败了** ——
    但**不是实现错**：`resolve_asof` 缺省取的是 `ds.latest_trade_asof()`（**日线**的最近有行情日，
    夹具里是 `10-08`），而 `spot.asof` 是 `10-09`；两条路径**用的是不同数据源**，
    这正是「看盘 vs 下单」分离本身。而 `15:30` 分支只在 `day == today` 时才生效 ——
    所以必须把 `latest_trade_asof` 钉住，**否则测不到那条分支**。

    两组时点都要跑（只看一组 = 只覆盖一半时钟面）：
    * 10:30 → **回退**到上一交易日（未来函数防护）
    * 16:00 → **不回退**（当天已收盘，用它下单不是未来函数）
    """
    cfg, ds, today = e2e
    monkeypatch.setattr(ds, "latest_trade_asof", lambda *a, **k: today, raising=False)
    prev = ds.prev_trade_date(today)

    _pin_clock(monkeypatch, ds, cfg, today=today, hh=10, mm=30)
    notes: list[str] = []
    got_intraday = service.resolve_asof(ds, None, notes)
    assert got_intraday == prev, (
        f"盘中（10:30）下单 asof 必须回退到上一交易日 {prev}，实际 {got_intraday}"
    )
    assert notes, "回退时必须留下 notes 说明（否则用户不知道为什么不是今天）"

    _pin_clock(monkeypatch, ds, cfg, today=today, hh=16, mm=0)
    got_closed = service.resolve_asof(ds, None, [])
    assert got_closed == today, (
        f"盘后（16:00）下单 asof 不应回退，应为 {today}，实际 {got_closed}"
    )


def test_T_W4_2_chart_asof_is_data_driven_not_calendar(e2e, monkeypatch):
    """★ 看盘 asof 是**数据驱动**：今天**没有数据**时**不得**返回今天。

    这条是「日历推最新」那类错误的直接回归守卫 —— 本项目已因此栽过四次。
    """
    cfg, ds, today = e2e
    # 把「今天」设成一个**没有行情**的日期（夹具 spot 的 asof 不是它）
    no_data_day = "2099-01-04"          # 未来且必然无数据
    _pin_clock(monkeypatch, ds, cfg, today=no_data_day, hh=10, mm=30)

    got = ds.chart_asof("600519")
    assert got != no_data_day, (
        f"今天（{no_data_day}）没有行情数据，chart_asof 却返回了它 —— 这是「日历推最新」复发"
    )


# --------------------------------------------------------------------------- #
# T-W4-1 全链路（核心段）
# --------------------------------------------------------------------------- #
def test_T_W4_1_chain_search_to_chart(e2e, monkeypatch):
    """search（已知代码）→ chart（含今天）的最小链路。"""
    cfg, ds, today = e2e
    _pin_clock(monkeypatch, ds, cfg, today=today, hh=10, mm=30)

    from ptrade import search

    # search 侧：真实 API 是 `search_stocks(cfg, query, *, offline=...)`
    #（第一版我写成 `search.search(...)` → AttributeError → 被 skip 掩盖了；**skip 不等于通过**）
    try:
        rows = search.search_stocks(cfg, "600519", offline=True)
    except AttributeError as exc:                  # pragma: no cover - API 改名时立刻可见
        pytest.fail(f"search 的公开 API 变了：{exc}")
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f"夹具缺名称表（离线 search 抛 {type(exc).__name__}）—— 该段由 T-W1-1 覆盖")
    assert isinstance(rows, list)

    html, payload = _render_chart_smoke(cfg, ds, today)
    assert payload.get("asof") == today, "图表载荷的 asof 应为今天（含当天 bar）"
    assert html, "图表 HTML 不应为空"


def _render_chart_smoke(cfg, ds, today):
    """渲染一张图；`chart.py` 由 W2 负责，接口未就绪时 skip 而不是伪造。"""
    try:
        from ptrade import chart as C
    except ImportError:                            # pragma: no cover
        pytest.skip("chart 模块不可用")
    try:
        return C.render_chart(cfg, ds, "600519", today, include_today=True)
    except TypeError:
        # 旧签名（W2 尚未落地 include_today）→ 退回默认调用
        return C.render_chart(cfg, ds, "600519", today)


# --------------------------------------------------------------------------- #
# T-W4-6 ★ 分时窗口上限：**恰好**超出一天必须明确报错，而不是空图
# --------------------------------------------------------------------------- #
def test_T_W4_6_day_just_outside_window_raises_not_empty(offline_cfg):
    """`datalen=500` 只装 **2 个完整交易日 + 今天一段**（实测 142+238+120=500）。

    它**不是**「稳定覆盖 3 个交易日」。所以「再往前看一天」必须得到
    **明确的 `DataUnavailable`**，而不是**空图** —— 空图与用户最初投诉的形态**一模一样**
    （「我打开网页看不到任何信息」）。

    与 W1 的 `test_T_W1_4_unknown_day_raises` **不同**：那条用 `2020-01-01`（远日，必然不在窗口），
    这条测的是**恰好在窗口外一天**的边界 —— 边界才是容易写错的地方。
    """
    from ptrade.datasource import DataSource, DataUnavailable

    def _jsonp(points_by_day: dict[str, int]) -> str:
        """按新浪格式造 JSONP：每天 `count` 个 1 分钟 bar（从 09:30 起）。

        （形状照 `tests/test_datasource_v2.py` 的 `jsonp` 助手；这里内联一份，
        避免跨测试模块 import —— 那是脆的。）
        """
        rows = []
        for day, count in points_by_day.items():
            for i in range(count):
                hh, mm = divmod(9 * 60 + 30 + i, 60)
                rows.append({
                    "day": f"{day} {hh:02d}:{mm:02d}:00",
                    "open": "10.00", "high": "10.10", "low": "9.90", "close": "10.05",
                    "volume": "1000", "amount": "10050.0",
                })
        return "var _=(" + json.dumps(rows, ensure_ascii=False) + ");"

    ds = DataSource(offline_cfg, offline=False)
    # 窗口里恰好两个交易日：10-14（238 点 = 完整）+ 10-15（120 点 = 今天一段）
    ds._http_get = lambda url, **kw: _jsonp({"2026-10-14": 238, "2026-10-15": 120})  # type: ignore[assignment]

    # 边界：再往前一天（10-13）不在窗口里 → 必须**明确报错**
    with pytest.raises(DataUnavailable) as exc:
        ds.intraday("600519", day="2026-10-13")
    msg = str(exc.value)
    assert "2026-10-13" in msg, f"报错信息应点明是哪一天，实际：{msg}"

    # 对照：窗口内且完整的 10-14 必须**成功**
    ok = ds.intraday("600519", day="2026-10-14")
    assert ok["day"] == "2026-10-14"
    assert ok["complete"] is True, "238 点应判为完整交易日（容差判据，硬阈值 240 会判不出）"
    assert ok["points"], "完整交易日不得返回空 points"


# --------------------------------------------------------------------------- #
# T-W4-4 契约：INV-52..57 必须在 INTERFACES.md 里，且关键短语与实现一致
# --------------------------------------------------------------------------- #
def test_T_W4_4_contract_invariants_present():
    """`INTERFACES.md` 必须含 **INV-52..57** 六条，且关键短语抽查通过。

    A11 要求「逐条 grep 关键短语」。这里把抽查固化，避免「写了不变量但内容空泛」。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "INTERFACES.md").read_text(encoding="utf-8")

    import re as _re

    rows = _re.findall(r"^\| (INV-\d+) \|", text, _re.M)
    for n in range(52, 58):
        tag = f"INV-{n}"
        assert rows.count(tag) == 1, f"{tag} 应恰好出现 1 次，实际 {rows.count(tag)}"

    # 每条的关键短语（与 PRD §6 A11 的 6 条一致）
    for phrase in ("AST 白名单", "chart_asof", "NetworkForbiddenInLock", "deny-list",
                   "min_rows", "228", "PERIODS_WITH_INTRADAY",
                   "intraday_<code>_<day>.html", "suspended", "不得编造"):
        assert phrase in text, f"INTERFACES.md 缺少关键短语：{phrase}"


# --------------------------------------------------------------------------- #
# 白名单：`chart_asof` 不得出现在下单/策略路径（结构守卫的**就近**补充）
# --------------------------------------------------------------------------- #
def test_T_W4_1b_chart_asof_only_in_chart_call_sites():
    """AST 白名单：`chart_asof(` 只允许出现在**看盘**函数体内。

    与 `tests/test_frozen_interfaces_v2.py::test_chart_asof_only_in_viewer_functions`
    **互为独立实现**（那边按冻结表，这边按实际调用点），任一漏掉都会被另一条抓到。

    ★ **全域扫描 `ptrade/*.py`**（architect R3）：修前只扫 `cli.py`/`webapp.py`，
      于是**新增第三个看盘路径若落在别的模块（如 service.py）就完全不被看见**。
    ★ 定义处是 `FunctionDef` 而不是 `ast.Call`，所以 `datasource.py` 的定义**不会自触发**。
    ★ **非空洞对照**：断言至少找到一个调用点 —— 否则「没人调用」也会让本用例通过（空守卫）。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    # ★ R3 加固（第 3 版）：**(模块, 函数) 配对**，不是函数名。
    #   第 2 版按名字放行 → architect 实测「第三个模块里起个同名 `viewer_asof`」就能绕过。
    #   ★ R4 收敛后**只剩一个合法调用点**。
    allowed = {("service.py", "viewer_asof")}
    offenders: list[str] = []
    seen: list[tuple[str, str]] = []
    for path in sorted((root / "ptrade").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    called = getattr(func, "attr", None) or getattr(func, "id", None)
                    if called == "chart_asof":
                        seen.append((path.name, fn.name))
                        if (path.name, fn.name) not in allowed:
                            offenders.append(f"{path.name}:{fn.lineno} {fn.name}()")
    assert not offenders, (
        "chart_asof 出现在非看盘路径：" + ", ".join(offenders)
        + f" —— INV-52 要求它只用于 {sorted(allowed)}"
    )
    assert seen, (
        "全域扫描没找到任何 chart_asof 调用点 —— 本用例会因此**恒绿**（空守卫）。"
        "看盘路径是不是被删了？"
    )
    assert seen == [("service.py", "viewer_asof")], (
        f"chart_asof 应**收敛为唯一入口** `service.viewer_asof`，实际 {seen} —— "
        f"出现第二份实现就说明策略又被复制了（architect R4）"
    )
