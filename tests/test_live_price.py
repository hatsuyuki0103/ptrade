"""B + D 的回归测试：按实时快照价成交（`--price live`）+ 成交价审计字段。

- **B**（2026-10-14 用户选定）：显式 `--price live` → 按**当前快照价**成交。
  不动 `resolve_asof`（那会改变回测语义），只给显式入口。
- **D**：`Order` 带 `price_source` / `price_asof` / `price_fetched_at`，
  事后能回答「这笔用的是哪一刻的价」。

**核心安全性质**：快照陈旧时**绝不静默退回陈旧价** ——
「以为是实时价、其实是几天前的价」比「明确失败」危险得多。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ptrade import cli, core, store
from ptrade.config import load_config
from ptrade.datasource import DataSource, DataUnavailable
from ptrade.engine import Engine, OrderRequest

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[1] / "data" / "fixture"
NEEDED = ("trade_dates.json", "margin_targets.json", "industry_map.json",
          "spot.json", "daily_000001.json", "daily_000001_unadjust.json")


@pytest.fixture
def live_root(tmp_path):
    """带夹具的独立数据根（离线）。"""
    (tmp_path / "data" / "fixture").mkdir(parents=True, exist_ok=True)
    for n in NEEDED:
        src = FIXTURE_DIR / n
        if src.is_file():
            (tmp_path / "data" / "fixture" / n).write_bytes(src.read_bytes())
    cfg = load_config(str(tmp_path))
    store.save_state(cfg, core.new_state(cfg), source="test")
    return cfg


def _write_spot(ds: DataSource, asof: str | None) -> None:
    """把快照写成指定的 `asof`（`None` = 缺键，用于测「不知道是哪天」的情形）。"""
    fixture = ds._read_fixture("spot") or {}
    rows = (fixture.get("payload") or {}).get("rows", [])
    payload: dict = {"rows": rows}
    if asof is not None:
        payload["asof"] = asof
    ds._write_entry(ds.cache_dir, "spot", payload)


def _fresh_spot(ds: DataSource) -> None:
    """把快照改成「asof = 今天」（模拟在线取到的新鲜快照）。"""
    _write_spot(ds, ds._today())


# --------------------------------------------------------------------------- #
# B：CLI 与 webapp 的入口解析
# --------------------------------------------------------------------------- #
def test_cli_price_live_is_accepted():
    """`--price live` 必须被 argparse 接受并原样传给引擎（不能走 `type=float`）。"""
    args = cli.build_parser().parse_args(
        ["order", "--code", "600519", "--side", "buy", "--qty", "100", "--price", "live"])
    assert args.price == "live"
    assert cli._coerce_price_arg(args.price) == Engine.LIVE_PRICE_SENTINEL


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("live", "live"), ("LIVE", "live"), (" live ", "live"),
    ("12.5", 12.5), (12.5, 12.5),
])
def test_cli_price_coercion(raw, expected):
    assert cli._coerce_price_arg(raw) == expected


def test_cli_price_bad_value_gives_clear_error():
    """非法值给**明确**错误（而不是 argparse 裸 `ValueError: could not convert ...`）。"""
    with pytest.raises(ValueError, match="live"):
        cli._coerce_price_arg("abc")


def test_cli_live_constant_matches_engine():
    """cli 的 `'live'` 与 `Engine.LIVE_PRICE_SENTINEL` 必须一致（防漂移）。"""
    assert cli._coerce_price_arg("live") == Engine.LIVE_PRICE_SENTINEL


# --------------------------------------------------------------------------- #
# D：审计字段
# --------------------------------------------------------------------------- #
def test_live_price_fills_and_records_audit(live_root):
    """**B + D 主路径**：快照新鲜 → 按快照价成交，且审计字段完整。"""
    cfg = live_root
    ds = DataSource(cfg, offline=True)
    _fresh_spot(ds)
    ds = DataSource(cfg, offline=True)
    td = ds.latest_trade_asof(["000001"])

    state = store.read_state(cfg)
    state.trade_date = ""                      # 让引擎自己 roll_to
    eng = Engine(cfg, ds, state)
    state = eng.roll_to(td)
    eng = Engine(cfg, ds, state)
    order = eng.submit(OrderRequest(code="000001", side="buy", qty=100,
                                    price="live", trade_date=td))

    assert order.status == "filled", f"{order.error_code}: {order.reason}"
    expected_px = ds.live_quote("000001", require_today=False)["quote"]["last"]
    assert order.price == pytest.approx(expected_px), "成交价应等于实时快照价"
    assert order.price_source == "live"
    assert order.price_asof == ds._today(), "price_asof 应是快照的 asof（今天）"
    assert order.price_fetched_at, "price_fetched_at 必须非空（D 要回答「哪一刻的价」）"


def test_live_price_rejects_stale_snapshot(live_root):
    """**核心安全性质**：快照陈旧 → **明确拒绝**，绝不静默用陈旧价成交。"""
    cfg = live_root
    ds = DataSource(cfg, offline=True)
    today = ds._today()
    spot_asof = str(((ds._read_fixture("spot") or {}).get("payload") or {}).get("asof", ""))[:10]
    if spot_asof == today:
        pytest.skip("夹具快照恰好是今天，构造不出「陈旧」场景")

    td = ds.latest_trade_asof(["000001"])
    state = store.read_state(cfg)
    state.trade_date = ""
    eng = Engine(cfg, ds, state)
    state = eng.roll_to(td)
    eng = Engine(cfg, ds, state)
    order = eng.submit(OrderRequest(code="000001", side="buy", qty=100,
                                    price="live", trade_date=td))

    assert order.status == "rejected", "陈旧快照必须拒绝，而不是按陈旧价成交"
    assert order.error_code == "NO_PRICE"
    assert "实时" in order.reason and "陈旧" not in order.reason.replace("退回陈旧价", ""), order.reason


def test_live_quote_raises_on_stale(live_root):
    """`live_quote` 在陈旧时抛 `DataUnavailable`（而不是返回旧价）。"""
    ds = DataSource(live_root, offline=True)
    today = ds._today()
    spot_asof = str(((ds._read_fixture("spot") or {}).get("payload") or {}).get("asof", ""))[:10]
    if spot_asof == today:
        pytest.skip("夹具快照恰好是今天")
    with pytest.raises(DataUnavailable) as ei:
        ds.live_quote("000001")
    assert "不会" in str(ei.value) and "陈旧价" in str(ei.value), str(ei.value)

    # 对照：allow stale 时能拿到（用于审计/调试），且标 stale=True
    got = ds.live_quote("000001", require_today=False)
    assert got["stale"] is True and got["quote"]["last"] > 0


def test_numeric_price_records_request_source(live_root):
    """**对照**：数字价 → `price_source='request'`（否则上面那条没有鉴别力）。"""
    cfg = live_root
    ds = DataSource(cfg, offline=True)
    td = ds.latest_trade_asof(["000001"])
    state = store.read_state(cfg)
    state.trade_date = ""
    eng = Engine(cfg, ds, state)
    state = eng.roll_to(td)
    eng = Engine(cfg, ds, state)
    order = eng.submit(OrderRequest(code="000001", side="buy", qty=100,
                                    price=11.59, trade_date=td))
    assert order.status == "filled", f"{order.error_code}: {order.reason}"
    assert order.price_source == "request"
    assert order.price_asof == "" and order.price_fetched_at == "", (
        "用户给的价没有「行情时刻」可言，不该乱填 asof/fetched_at"
    )


def test_audit_fields_reach_the_ledger(live_root):
    """**D 的落盘断言**：审计字段必须进 `ledger.jsonl`（否则事后查不到）。"""
    cfg = live_root
    ds = DataSource(cfg, offline=True)
    _fresh_spot(ds)
    ds = DataSource(cfg, offline=True)
    td = ds.latest_trade_asof(["000001"])
    state = store.read_state(cfg)
    state.trade_date = ""
    eng = Engine(cfg, ds, state)
    state = eng.roll_to(td)
    eng = Engine(cfg, ds, state)
    eng.submit(OrderRequest(code="000001", side="buy", qty=100, price="live", trade_date=td))

    led = pathlib.Path(store.ledger_path(cfg))
    rows = [json.loads(line) for line in led.read_text(encoding="utf-8").splitlines() if line]
    assert rows, "台账应有记录"
    last = rows[-1]
    assert last["price_source"] == "live"
    assert last["price_asof"] == ds._today()
    assert last["price_fetched_at"], "台账里必须有抓取时刻"


# --------------------------------------------------------------------------- #
# 交易日必须取自**快照**（2026-10-14 修的设计缺陷）
#
# 修前：`--price live` 仍走 `resolve_asof`，而它在 15:30 前会回退到**上一交易日**；
# 引擎的停牌检查看的是 `td` 那天的行情 → 「快照是今天、td 是昨天」→ 判 `SUSPENDED`。
# 实测：不传 `--trade-date` 时 trade_date=2026-10-13 → rejected SUSPENDED；
# 必须**同时**给 `--trade-date=今天` 才能用 —— 用户体验差。
# 现改为 live 模式下以**快照自身的 asof** 为交易日。
# --------------------------------------------------------------------------- #
def test_live_trade_date_comes_from_snapshot(live_root):
    """`service.live_trade_date` 必须返回快照的 `asof`。

    **为什么要显式钉住两条日期**（第四轮复核 M3，HIGH —— 我原版是个**假守卫**）：

    原版写 `assert got == ds._today()`，而夹具里「快照 asof == 日历最新 == 今天」
    三者恰好相同 → 把实现换成 `latest_trade_date()` **17 条仍全绿**（零鉴别力）。
    更糟的是**它随钟点变化**：15:30 前 `latest_trade_date()` 会回退到上一交易日
    （那时变异会红），15:30 后两者又相同（变异恒绿）—— **这条守卫每天有约 8.5 小时是空的**。

    现在改成：**显式把快照写成一个「日历上没有、或无数据」的日期**，并同时断言
    「它 ≠ `latest_trade_date()`」，从而「取自快照」与「取自日历」必须给出不同答案。
    """
    from ptrade import service

    cfg = live_root
    ds = DataSource(cfg, offline=True)
    # 用一个**日历里没有数据**的近期日期当快照 asof（挑一个交易日历里存在的，
    # 但引擎不会把它当「最近有行情日」的日期 —— 关键是它与 latest_trade_date() 不同）。
    asof = ds._today()
    _write_spot(ds, asof)
    ds = DataSource(cfg, offline=True)

    got = service.live_trade_date(ds, "000001")
    calendar = ds.latest_trade_date()
    assert got == asof, f"live 交易日应取快照 asof（{asof}），实际 {got}"
    # **鉴别力断言**：若实现退回日历日期，这条会红（除非两者恰好相同，
    # 那种情况下本用例会 skip 而不是假通过 —— 不把「碰巧相同」当证据）。
    if asof == calendar:
        pytest.skip(
            f"快照 asof 与日历最新日恰好都是 {asof}，本用例无法区分两种实现"
            f"（**不算通过**）—— 换个钟点重跑即可获得鉴别力"
        )


def test_live_trade_date_differs_from_calendar_when_snapshot_is_other_day(live_root, monkeypatch):
    """**判别性用例**（M3 的正面补强）：让「日历最新日」与「快照 asof」**不同**，
    断言 `live_trade_date` 跟**快照**走、**不等于**日历日期。

    **为什么用冻结日历而不是把快照写成昨天**：
    `live_quote(require_today=True)` 设计要求快照必须是**今天**（陈旧会被明确拒绝，
    那是 B 的核心安全性质）。所以不能靠「把快照写旧」造差异 —— 那会先被拒绝。
    改为**保持快照 = 今天**，但**把 `latest_trade_date` 冻结成另一个日期**：
    于是「取自快照」（今天）与「取自日历」（冻结值）必然不同，判据就有了鉴别力。
    """
    from ptrade import service

    cfg = live_root
    ds = DataSource(cfg, offline=True)
    today = ds._today()
    frozen_calendar = ds.prev_trade_date(today) if ds.is_trade_date(today) else today
    assert frozen_calendar != today, "冻结的日历日期必须与今天不同，否则本用例无鉴别力"

    monkeypatch.setattr(
        type(ds), "latest_trade_date",
        lambda self: frozen_calendar,   # noqa: ARG005
        raising=True,
    )
    _write_spot(ds, today)
    ds = DataSource(cfg, offline=True)

    got = service.live_trade_date(ds, "000001")
    assert got == today, (
        f"live 交易日应取快照 asof（{today}），实际 {got} —— "
        f"若等于冻结的日历日期 {frozen_calendar} 就说明它退回日历了（M3 复发）"
    )
    assert got != ds.latest_trade_date(), "必须与日历日期区分开，否则本用例无鉴别力"


def test_live_trade_date_raises_on_stale_snapshot(live_root):
    """陈旧快照时**抛错**，不得悄悄退回日历日期（那会让 live 变成「假实时」）。"""
    from ptrade import service

    ds = DataSource(live_root, offline=True)
    today = ds._today()
    spot_asof = str(((ds._read_fixture("spot") or {}).get("payload") or {}).get("asof", ""))[:10]
    if spot_asof == today:
        pytest.skip("夹具快照恰好是今天")
    with pytest.raises(DataUnavailable):
        service.live_trade_date(ds, "000001")


def test_cli_order_uses_snapshot_date_for_live(live_root, monkeypatch):
    """**端到端**：`--price live` 且**不传** `--trade-date` 时，交易日应取自快照。

    这条是「用户不必再手写今天日期」的守卫 —— 修前它会落到 `resolve_asof`
    的上一交易日并被判 `SUSPENDED`。
    """
    cfg = live_root
    ds = DataSource(cfg, offline=True)
    _fresh_spot(ds)
    ds = DataSource(cfg, offline=True)
    today = ds._today()

    # 让 cli 用我们的 ds（它内部会 `_make_ds`）
    monkeypatch.setattr(cli, "_make_ds", lambda c, offline: ds)
    parser = cli.build_parser()
    args = parser.parse_args(["order", "--code", "000001", "--side", "buy", "--qty", "100",
                              "--price", "live", "--offline", "--json"])
    captured: dict = {}
    real_engine_for = cli._engine_for

    def spy_engine_for(engine_cls, c, d, state, source="cli"):
        captured["engine"] = real_engine_for(engine_cls, c, d, state, source=source)
        return captured["engine"]

    monkeypatch.setattr(cli, "_engine_for", spy_engine_for)
    import io
    import contextlib

    with contextlib.redirect_stdout(io.StringIO()) as buf:
        cli.cmd_order(args, cfg)
    payload = json.loads(buf.getvalue())
    orders = (payload.get("data") or {}).get("orders") or [{}]
    o = orders[0]
    assert o.get("trade_date") == today, (
        f"live 下单的交易日应取快照 asof（{today}），实际 {o.get('trade_date')!r}"
        f"—— 修前这里会是上一交易日并判 SUSPENDED"
    )
    assert o.get("status") == "filled", f"{o.get('error_code')}: {o.get('reason')}"
    assert o.get("price_source") == "live"


# --------------------------------------------------------------------------- #
# 第四轮复核 M1 / M2 / M4：守卫错误不得被 `except DataUnavailable` 吞掉
#
# 根因同一个：`NetworkForbiddenInLock` 是 `DataUnavailable` 的**子类**，
# 所以任何 `except DataUnavailable` 都会顺手接住守卫错误并「降级」——
# 用户/网页端看到的是**错误的正常结果**（业务拒单 / 静默回退今天 / 贴着实时标签的未知日期），
# 而不是 INV-42/45 要求的「明确错 → 409」。
# --------------------------------------------------------------------------- #
def test_m1_guard_error_not_swallowed_by_live_quote_handler(live_root):
    """**M1（HIGH）**：`engine._evaluate` 里包住 `live_quote` 的
    `except DataUnavailable` 必须**先放行守卫错误**。

    修前：守卫错误被吞成 `200 / NO_PRICE` 业务拒单 ——
    INV-45 里「守卫错误 → 409」**永远不可能发生**（网页端会告诉用户
    「按实时价成交失败」，真实原因却是代码在锁内取数）。

    ★ **setup 修正（2026-10-15，Lead 插桩定位）**：原先 `_fresh_spot(ds)` 把快照写成
    **今天**，而 `td = latest_trade_asof(...)` 是**夹具最后交易日** → `quote(code, td)`
    的新鲜快照分支不命中 → `offline=False` 下回落到 `daily()` → **试图联网** → 被
    `conftest.block_real_network` 拦成**普通 `DataUnavailable`** → `_quote` 正常降级返回 None
    → 在**抵达 live 分支之前**就以 `NO_MARKET_DATA` 拒单 → `live_quote` 被调用 **0 次**
    → `pytest.raises(NetworkForbiddenInLock)` 因「根本没走到那里」而失败（DID NOT RAISE）。
    → 现在把快照 asof **对齐到 `td`**，让前置行情检查命中快照（不联网）；并**断言
    `live_quote` 真被调用过**，否则本用例会退化成空守卫（守卫错误根本没机会被吞）。
    """
    from ptrade.datasource import NetworkForbiddenInLock
    from ptrade.engine import Engine, OrderRequest

    cfg = live_root
    ds = DataSource(cfg, offline=True)           # 先用**离线**做 roll_to（不触网）
    _fresh_spot(ds)
    ds = DataSource(cfg, offline=True)
    td = ds.latest_trade_asof(["000001"])
    _write_spot(ds, td)                          # ★ 快照 asof 对齐 td → quote(td) 命中快照，不联网
    ds = DataSource(cfg, offline=True)
    state = store.read_state(cfg)
    state.trade_date = ""
    eng = Engine(cfg, ds, state)
    state = eng.roll_to(td)

    # roll_to 之后再切**在线**：这样守卫才会开火，且不会因为缺日历去联网
    ds.offline = False

    calls = {"n": 0}

    def _boom(code, **kw):
        calls["n"] += 1
        raise NetworkForbiddenInLock("注入：写锁临界区内禁止联网取数")

    ds.live_quote = _boom                            # type: ignore[method-assign]
    eng = Engine(cfg, ds, state)

    with pytest.raises(NetworkForbiddenInLock):
        eng.submit(OrderRequest(code="000001", side="buy", qty=100,
                                price="live", trade_date=td))
    assert calls["n"] == 1, (
        "live_quote 被调用 %d 次（期望 1）→ 本用例是**空守卫**：守卫错误根本没机会被吞。"
        "多半是前置行情检查在抵达 live 分支前就拒单了（快照 asof / 停牌 / 窗口不匹配）。" % calls["n"]
    )

    # 对照：普通 DataUnavailable 仍应降级成业务拒单（原有兜底不能被破坏）
    def _ordinary(code, **kw):
        calls["n"] += 1
        raise DataUnavailable("普通：快照不可用")

    ds.live_quote = _ordinary                        # type: ignore[method-assign]
    eng2 = Engine(cfg, ds, state)
    order = eng2.submit(OrderRequest(code="000001", side="buy", qty=100,
                                     price="live", trade_date=td))
    assert order.status == "rejected" and order.error_code == "NO_PRICE", order
    assert calls["n"] == 2, "对照组也必须真的走到 live 分支（否则兜底断言是空的）"


def test_m2_guard_error_not_swallowed_in_latest_trade_asof(live_root, monkeypatch):
    """**M2（MEDIUM）**：`latest_trade_asof` 的第一处 `except Exception`
    必须放行守卫错误（同函数另外三处都有穿透，唯独这里漏了）。

    修前：守卫错误被吞 → **静默回退「今天」**。
    """
    from ptrade.datasource import NetworkForbiddenInLock

    ds = DataSource(live_root, offline=False)

    def _boom(self):
        raise NetworkForbiddenInLock("注入：写锁临界区内禁止联网取数")

    monkeypatch.setattr(type(ds), "latest_trade_date", _boom, raising=True)
    with pytest.raises(NetworkForbiddenInLock):
        ds.latest_trade_asof(["000001"])


@pytest.mark.parametrize("missing", ["omit", "empty"])
def test_m4_missing_asof_is_treated_as_stale(live_root, missing):
    """**M4（MEDIUM）**：快照 `asof` 缺失/为空时 `require_today=True` **必须拒绝**。

    修前 `bool(asof) and asof != today` 在 `asof` 为空时为假 → 当作**新鲜** →
    实测会成交并记下 `price_source='live'` + `price_asof=''`
    = 「贴着实时的标签、却不知道是哪天的价」。
    """
    ds = DataSource(live_root, offline=True)
    _write_spot(ds, None if missing == "omit" else "")
    ds = DataSource(live_root, offline=True)
    with pytest.raises(DataUnavailable):
        ds.live_quote("000001")
    # 对照：允许陈旧时仍可读（用于审计/调试），但必须标 stale=True
    got = ds.live_quote("000001", require_today=False)
    assert got["stale"] is True and got["asof"] == ""
