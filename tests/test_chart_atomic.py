"""W-6：图表落盘的**原子性**与「不两趟盘」。

## 背景

`/api/chart` 原先的做法是：`build_chart` → `write_text` 500 KB → `open().read()` 读回来。
两个问题：

1. **非原子写**：写到一半被杀（Ctrl+C / 断电 / 磁盘满）会留下**半截文件**；
   而文件名按「代码 + asof」定址 → 下次同参数请求会**命中这个残文件**当成有效结果。
2. **两趟盘**：渲染出的 html 就在内存里，却要落盘再读回。

修法：`render_chart()` 返回 `(html, payload)` **不落盘**；`write_chart()` 原子写；
`build_chart()` = 渲染 + 原子写（返回路径，**保持既有契约**）。
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from ptrade import chart as C
from ptrade import webapp
from tests._guards import price_asof
from tests.test_webapp import _SimpleProxy, _app, _call


@pytest.fixture
def asof(offline_ds) -> str:
    return price_asof(offline_ds)


MID = "000001"


# --------------------------------------------------------------------------- #
# 渲染与落盘**分离**
# --------------------------------------------------------------------------- #
def test_render_chart_returns_html_without_writing(offline_cfg, offline_ds, asof):
    """`render_chart` **不得**落盘（它是给 `/api/chart` 用的内存返回路径）。

    **做法**：先把 `reports/` 清空，调 `render_chart` 后断言它**仍然为空**。

    第一版我用「调用前后对比文件集合」—— 那有缺陷：变异版按**同一个**路径写，
    前后必然都「多出同一个文件」而集合相等，测试照样通过（我实测变异全绿才发现）。
    清空后断言为空是不可绕过的。
    """
    import shutil

    reports = pathlib.Path(offline_cfg.root) / "reports"
    if reports.is_dir():
        shutil.rmtree(reports)                       # 先清干净

    html, payload = C.render_chart(offline_cfg, offline_ds, MID, asof)

    assert html.startswith("<!DOCTYPE html>"), html[:80]
    assert payload["asof"] and payload["code"] == MID
    left = sorted(p.name for p in reports.iterdir()) if reports.is_dir() else []
    assert left == [], (
        f"render_chart 不该写任何文件，但 reports/ 里有 {left} —— "
        f"它必须是纯渲染（落盘由 write_chart/build_chart 负责）"
    )


def test_build_chart_still_returns_path(offline_cfg, offline_ds, asof):
    """**契约不变**：`build_chart` 仍返回**路径字符串**（CLI 与 7 处测试依赖它）。"""
    path = C.build_chart(offline_cfg, offline_ds, MID, asof)
    assert isinstance(path, str)
    assert os.path.isabs(path)
    assert pathlib.Path(path).is_file()


def test_written_file_matches_returned_html(offline_cfg, offline_ds, asof):
    """**一致性**：落盘内容必须与返回的 html 逐字节相同（同一次渲染）。"""
    html, payload = C.render_chart(offline_cfg, offline_ds, MID, asof)
    path = C.write_chart(C.chart_out_path(offline_cfg, MID, payload["asof"]), html)
    assert pathlib.Path(path).read_text(encoding="utf-8") == html


# --------------------------------------------------------------------------- #
# 原子性
# --------------------------------------------------------------------------- #
def test_chart_write_is_atomic_on_failure(offline_cfg, offline_ds, asof, monkeypatch, tmp_path):
    """**核心断言**：写入中途失败时，目标路径**不得**留下半截文件。

    做法：让底层的 `atomic_write_text` 在「写 tmp」之后、`os.replace` 之前抛错，
    然后断言目标文件仍是**旧内容**（而不是被截断的新内容、也不是不存在）。

    **注意用子目录**：`tmp_path` 下还有夹具铺的 `data/`，
    直接列 `tmp_path` 会把夹具目录当成「临时文件残留」（我第一版就这么错了）。
    """
    workdir = tmp_path / "charts"          # 只在这个子目录里检查残留
    workdir.mkdir()
    target = workdir / "chart.html"
    target.write_text("旧内容-完整", encoding="utf-8")

    def boom(path, text, **kw):
        # 模拟「写了 tmp 但替换前崩了」——真实原子写在这里不会破坏目标文件
        raise OSError("注入：写盘中断")

    monkeypatch.setattr(C.store, "atomic_write_text", boom, raising=True)
    # **不匹配错误文案**：用 `pytest.raises(OSError)` 即可。
    # 固定文案会把测试钉死在「抛的是我注入的那句」上 —— 变异成非原子写时
    # 抛的是**别的** OSError，测试会在这一步就红，**走不到**下面真正要验的
    # 「目标文件是否被截断」断言（我第一版就这样，变异实验才发现）。
    with pytest.raises(OSError):
        C.write_chart(target, "新内容" * 100)

    assert target.read_text(encoding="utf-8") == "旧内容-完整", (
        "写入失败后目标文件必须保持旧内容 —— 若被截断，下次同参数请求会命中残文件"
    )
    # tmp 文件不得残留（否则 reports/ 会堆垃圾）
    leftovers = [p.name for p in workdir.iterdir() if p.name != "chart.html"]
    assert not leftovers, f"残留临时文件：{leftovers}"


def test_chart_write_leaves_no_tmp_on_success(offline_cfg, offline_ds, asof, tmp_path):
    """**对照**：成功路径也不得留下临时文件。"""
    workdir = tmp_path / "charts"
    workdir.mkdir()
    html, payload = C.render_chart(offline_cfg, offline_ds, MID, asof)
    target = workdir / "c.html"
    C.write_chart(target, html)
    assert target.is_file()
    assert [p.name for p in workdir.iterdir()] == ["c.html"], list(workdir.iterdir())


def test_write_chart_creates_parent_dir(tmp_path):
    """父目录不存在时应自动建（`reports/` 在全新数据根下可能还没有）。"""
    deep = tmp_path / "a" / "b" / "c.html"
    C.write_chart(deep, "<html>x</html>")
    assert deep.read_text(encoding="utf-8") == "<html>x</html>"


# --------------------------------------------------------------------------- #
# 端到端：网页端
# --------------------------------------------------------------------------- #
def test_api_chart_returns_html_and_persists_file(web_root):
    """**端到端**：`/api/chart` 既内联返回 html，又在 `reports/` 留下同一个文件。

    这条同时锁住「内存返回」与「仍然落盘」两个要求 ——
    只满足一个（例如干脆不落盘）也会红。
    """
    from ptrade import webapp

    app = webapp.WebApp(web_root, offline=True, lock_timeout=5.0)
    resp = app.dispatch("GET", "/api/chart", {"code": [MID]}, None)
    assert resp.status == 200, resp.status
    body = resp.to_bytes().decode("utf-8")
    assert body.startswith("<!DOCTYPE html>")

    reports = pathlib.Path(web_root.root) / "reports"
    files = sorted(p for p in reports.glob(f"chart_{MID}_*.html")) if reports.is_dir() else []
    assert files, f"reports/ 下没有图表文件：{list(reports.glob('*')) if reports.is_dir() else '目录不存在'}"
    on_disk = files[-1].read_text(encoding="utf-8")
    assert on_disk == body, (
        "落盘内容与内联返回的 html 必须一致（同一次渲染）——"
        "若不一致说明渲染了两次或读回时被截断"
    )


@pytest.fixture
def web_root(tmp_path):
    """离线数据根（真实夹具），够 `/api/chart` 跑通。"""
    import shutil

    repo = pathlib.Path(__file__).resolve().parents[1]
    for name in ("fixture", "scenario"):
        src = repo / "data" / name
        if not src.is_dir():
            continue
        dst = tmp_path / "data" / name
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.suffix == ".json":
                shutil.copy2(item, dst / item.name)

    from ptrade.config import load_config

    return load_config(str(tmp_path))


# --------------------------------------------------------------------------- #
# 同类问题：`data/cache/*.json` 的写者原先也是**非原子**的
#
# WATCH 项「`data/cache/*.json` 写者未归类」的收口。失败模式比图表更严重：
# `open(path, "w")` **先截断目标**，随后 `json.dump` 抛错 →
# 缓存文件残缺 → `_read_entry` 的 `json.load` 抛 → 返回 `None`
# → 该 key 被当成**没有缓存**，直到重新联网重建。
# 离线场景下就是「数据突然全没了」，且**没有任何提示**。
# --------------------------------------------------------------------------- #
class _NotJsonable:
    """不能 JSON 序列化的对象（模拟缓存里混进脏对象）。"""


def test_cache_write_is_atomic_on_serialize_failure(tmp_path):
    """**核心断言**：缓存序列化失败时，已有缓存文件**逐字节不变**。"""
    from ptrade.datasource import DataSource

    cache = tmp_path / "cache"
    DataSource._write_entry(str(cache), "k", {"good": 1, "rows": [1, 2, 3]})
    target = cache / "k.json"
    before = target.read_bytes()

    with pytest.raises(TypeError):
        DataSource._write_entry(str(cache), "k", {"bad": _NotJsonable()})

    assert target.read_bytes() == before, (
        "★序列化失败后缓存被破坏了 —— 非原子写会先截断目标，"
        "残缺内容会被 `_read_entry` 当成「没有缓存」，离线时等于数据全丢"
    )
    # 仍然可读（没被截断成非法 JSON）
    got = DataSource._read_entry(str(cache), "k")
    assert got is not None and got["payload"] == {"good": 1, "rows": [1, 2, 3]}, got
    # 不残留 tmp
    assert sorted(p.name for p in cache.iterdir()) == ["k.json"], list(cache.iterdir())


def test_cache_write_keeps_nan_tolerance(tmp_path):
    """**行为保持不变**：缓存写仍**容忍 NaN**（没有换成 `allow_nan=False` 的原子写）。

    理由：行情里偶尔出现 NaN；为了「更严格」而让 `quote()` 报「无数据」是更糟的取舍。
    若哪天有人把 `_write_entry` 换成 `store.atomic_write_json`，这条会红。
    """
    from ptrade.datasource import DataSource

    cache = tmp_path / "cache"
    DataSource._write_entry(str(cache), "nan", {"last": float("nan"), "rows": []})
    got = DataSource._read_entry(str(cache), "nan")
    assert got is not None, "含 NaN 的缓存应能写入并读回"
    assert got["payload"]["last"] != got["payload"]["last"], "NaN 应保持为 NaN（不等于自身）"


# --------------------------------------------------------------------------- #
# 第五轮复核 L3 的结构性收口：取锁超时与块内异常**用类型区分**
#
# `locked_update` 的形状是 `with state_lock(...): ... mutate(...) ...` ——
# 两个阶段抛的都是 `StateLockTimeout`，调用方的 `except StateLockTimeout -> 503`
# 会把「块体自己的 bug」伪装成「锁忙，请重试」。
#
# **我第一版的修法是错的**：抽 `locked_update_split` 只做了「取锁阶段与块体分离」，
# 两个阶段抛的仍是同一类型 → 调用点照样分不清（实测：块内异常仍返回 503）。
# 真正的修法是**用类型区分阶段**：取锁失败 → `AcquireTimeout`（子类）；
# 块内异常 → 包装成普通 `RuntimeError`。
# --------------------------------------------------------------------------- #
def test_locked_update_split_marks_stage_by_exception_type(tmp_path):
    """**核心**：块内 `StateLockTimeout` 必须**不再**是 `StateLockTimeout`（也不是其子类）。"""
    from ptrade import core, store
    from ptrade.config import load_config

    cfg = load_config(str(tmp_path))
    store.locked_update_split(cfg, lambda s: core.new_state(cfg), source="t")

    def mutate(state):
        raise store.StateLockTimeout("注入：块体内的 bug")

    with pytest.raises(RuntimeError) as ei:
        store.locked_update_split(cfg, mutate, source="t")

    assert not isinstance(ei.value, store.StateLockTimeout), (
        "块内异常仍是 StateLockTimeout（或其子类）→ 调用方的 "
        "`except StateLockTimeout -> 503` 会把它误判成「锁忙，请重试」。"
        "取锁阶段与块体必须用**不同类型**区分。"
    )
    assert "块体内" in str(ei.value), ei.value


def test_acquire_timeout_is_a_state_lock_timeout_subclass():
    """**向后兼容**：`AcquireTimeout` 必须是 `StateLockTimeout` 的子类。

    否则既有那些 `except StateLockTimeout` 的调用点会**漏接**而崩栈 ——
    修一个「错误分类」问题不该引入新的崩溃面。
    """
    from ptrade import store

    assert issubclass(store.AcquireTimeout, store.StateLockTimeout)


def test_locked_update_split_equivalent_on_normal_paths(tmp_path):
    """**行为等价**：正常路径（初始化 / 就地修改 / `NO_WRITE`）两个函数结果一致。

    否则「更严格」就成了「换了一套语义」。
    """
    from ptrade import core, store
    from ptrade.config import load_config

    for fn in (store.locked_update, store.locked_update_split):
        root = tmp_path / fn.__name__
        root.mkdir()
        cfg = load_config(str(root))
        st = fn(cfg, lambda s, c=cfg: core.new_state(c), source="t")
        assert isinstance(st, core.AccountState), f"{fn.__name__} 初始化应返回 AccountState"

        def bump(s):
            s.trade_date = "2026-10-09"
            return s

        r = fn(cfg, bump, source="t")
        assert isinstance(r, core.AccountState) and r.trade_date == "2026-10-09"
        assert store.read_state(cfg).trade_date == "2026-10-09", "改动应落盘"

        # NO_WRITE：明确不落盘，但返回值是「实际被持久化的那个 state」（契约）
        r_no = fn(cfg, lambda s: store.NO_WRITE, source="t")
        assert isinstance(r_no, core.AccountState), f"{fn.__name__} NO_WRITE 应返回当前 state"
        # 返回 None：保存被就地修改的入参，并返回它
        r_none = fn(cfg, lambda s: None, source="t")
        assert isinstance(r_none, core.AccountState), f"{fn.__name__} None 应返回当前 state"


def test_l3_in_body_timeout_is_not_reported_as_lock_busy(web_root, monkeypatch):
    """**端到端**：`mutate` 体内抛锁超时 → 网页端 **500**（不是 503）。

    这条是 L3 的真正验收判据 —— 修前实测返回 **503**
    （「锁忙、请重试」），而真实原因可能是块体自身的缺陷。

    （用本文件的 `web_root` 夹具；`web_cfg` 是 `test_webapp.py` 的局部夹具，
    不能跨文件共享。）
    """
    import ptrade.store as store

    real = store.locked_update_split

    def _in_body(cfg_, mutate, *, source="cli", timeout=10.0):
        def bad(state):
            raise store.StateLockTimeout("注入：mutate 自己的 bug")
        return real(cfg_, bad, source=source, timeout=timeout)

    proxy = _SimpleProxy(store, locked_update_split=_in_body)
    monkeypatch.setattr(webapp, "_store", lambda: proxy)

    status, payload = _call(_app(web_root), "POST", "/api/order",
                            body={"code": "000001", "side": "buy", "qty": 100})
    assert status == 500, (
        f"块内异常必须走 500（实测 {status}）—— 若为 503 说明它又被伪装成「锁忙、请重试」；"
        f"error={payload.get('error')}"
    )
    assert "块体内" in str(payload.get("error")), payload
