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


def _fresh_spot(ds: DataSource) -> None:
    """把快照改成「asof = 今天」（模拟在线取到的新鲜快照）。"""
    fixture = ds._read_fixture("spot") or {}
    rows = (fixture.get("payload") or {}).get("rows", [])
    ds._write_entry(ds.cache_dir, "spot", {"asof": ds._today(), "rows": rows})


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
    """`service.live_trade_date` 必须返回快照的 `asof`（而不是 `resolve_asof` 的结果）。"""
    from ptrade import service

    cfg = live_root
    ds = DataSource(cfg, offline=True)
    _fresh_spot(ds)
    ds = DataSource(cfg, offline=True)

    got = service.live_trade_date(ds, "000001")
    assert got == ds._today(), f"live 交易日应取快照 asof（{ds._today()}），实际 {got}"


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
