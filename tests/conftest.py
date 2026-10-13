"""pytest 公共夹具（T1 提供，供 T2/T3 复用）。

约定：

* **禁止联网**：``_no_network`` 是 autouse 夹具，直接封掉 ``socket.connect``；
  任何试图联网的测试都会当场失败，从而证明 ``offline=True`` 真的不联网。
* ``offline_cfg``：以 ``tmp_path`` 为 root，并预置 ``data/fixture`` 夹具（从仓库目录复制）。
* ``engine_offline`` / ``scorer_offline``：延迟导入，未就绪时自动 skip（不阻塞 T1 验收）。
"""

from __future__ import annotations

import os
import shutil
import socket
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ptrade import core  # noqa: E402
from ptrade.config import load_config  # noqa: E402
from ptrade.datasource import DataSource  # noqa: E402

FIXTURE_DIR = os.path.join(ROOT, "data", "fixture")
SCENARIO_DIR = os.path.join(ROOT, "data", "scenario")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """全局断网：任何 socket 连接尝试都判失败（证明测试全程离线）。"""

    def _blocked(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("测试期间禁止联网（socket.connect 被拦截）")

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=False)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=False)
    yield


def _copy_fixtures(target_root: str) -> None:
    """复制真实夹具；若存在 `data/scenario`（SYNTHETIC 剧本）也一并复制，
    但 `DataSource` 默认**不读**它，只有 `scenario=True` 才叠加。"""
    for name, source in (("fixture", FIXTURE_DIR), ("scenario", SCENARIO_DIR)):
        if not os.path.isdir(source):
            continue
        dst = os.path.join(target_root, "data", name)
        os.makedirs(dst, exist_ok=True)
        for file in os.listdir(source):
            if file.endswith(".json"):
                shutil.copy2(os.path.join(source, file), os.path.join(dst, file))


@pytest.fixture
def offline_cfg(tmp_path):
    """以 tmp_path 为 root 的配置（预置夹具，无 config.json → 全默认值）。"""
    _copy_fixtures(str(tmp_path))
    return load_config(str(tmp_path))


@pytest.fixture
def offline_ds(offline_cfg):
    return DataSource(offline_cfg, offline=True)



@pytest.fixture
def seeded_account(offline_cfg, offline_ds):
    """已建仓账户：上一交易日买入 600519 100 股，已结算（可卖）。"""
    from tests._guards import price_asof as _price_asof

    state = core.new_state(offline_cfg)
    latest = _price_asof(offline_ds)                # ← 有数据的最新日（不是日历最新日）
    prev = offline_ds.prev_trade_date(latest)
    price = offline_ds.last_close("600519", prev)
    trade_date = prev
    order = core.Order(
        order_id=core.next_order_id(state),
        ts=f"{trade_date}T15:00:00",
        trade_date=trade_date,
        code="600519",
        name="贵州茅台",
        side=core.Side.BUY.value,
        qty=100,
        price=price,
    )
    commission, tax, transfer = core.calc_fees(order.side, price, order.qty, offline_cfg.fee)
    order.fee, order.tax, order.transfer_fee = commission, tax, transfer
    plan = core.fill_plan(state, order, offline_cfg.margin, offline_cfg.fee)
    order.cash_delta = plan["cash_delta"]
    order.margin_used = plan["margin_used"]
    order.debt_delta = plan["debt_delta"]
    order.realized_pnl = plan["realized_pnl"]
    state = core.apply_fill(state, order, offline_cfg.margin, offline_cfg.fee)
    state.history.append(order)
    state = core.settle_t1(state, latest)
    return state


@pytest.fixture
def engine_offline(offline_cfg, offline_ds, seeded_account):
    """T2 的 Engine（未就绪时 skip）。"""
    engine_mod = pytest.importorskip("ptrade.engine", reason="engine.py 尚未就绪（T2）")
    return engine_mod.Engine(offline_cfg, offline_ds, seeded_account)


@pytest.fixture
def scorer_offline(offline_cfg, offline_ds):
    """T3 的 CanslimScorer（未就绪时 skip）。"""
    strategy = pytest.importorskip("ptrade.strategy", reason="strategy.py 尚未就绪（T3）")
    return strategy.CanslimScorer(offline_cfg, offline_ds, offline=True)


@pytest.fixture
def write_state(offline_cfg):
    """把账户写入 tmp root 的 data/state.json（CLI 端到端用）。"""

    def _write(state):
        import json

        path = os.path.join(offline_cfg.root, "data", "state.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(core.state_document(state), fh, ensure_ascii=False, indent=2)
        return path

    return _write
