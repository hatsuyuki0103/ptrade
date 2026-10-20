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


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """在**类**上阻断 `DataSource._ak()`，避免 `import akshare` 与其后的取数（autouse）。

    ## 与既有的 `_no_network` 分工（两者互补，都要）

    `_no_network` 拦 `socket.connect` / `socket.create_connection` —— 它保证
    **真的联网会失败**（正确性）。但它**拦不住 `import akshare` 的耗时**：
    akshare 是个很重的包（0.9~1.6s，还可能触发额外的网络/文件 IO），
    于是「用 `offline=False` 但实际只读夹具/缓存」的用例会白等。

    ## 实测收益（2026-10-14）

    | 文件 | 加本夹具前 | 加本夹具后 |
    |---|---|---|
    | `tests/test_search.py` | 16s | **2.54s** |
    | `tests/test_inlock_network_guard.py` | 22.96s | **2.8s** |

    而且这是**变异实验的前提**：把守卫实现注掉后，锁内会真的去取数 ——
    实测子进程卡 **30 分钟 / 1802s CPU**，我不得不杀两次。
    **测试在「实现被注掉」时必须是有界失败，不能转去联网。**

    ## 做法

    在**类**上替换（不是实例 —— 实例属性挡不住 `make_ds()` 造的新实例），
    保留两段既有语义（`offline=True` 报错、锁内禁网拦截），
    只把「真 import akshare」换成**任何属性访问都抛错**的替身。
    需要真实数据的用例应自备缓存/夹具，或在用例内显式 monkeypatch 覆盖本守卫。
    """
    from ptrade import store
    from ptrade.datasource import DataUnavailable, NetworkForbiddenInLock

    class _NoNetworkAk:
        """任何属性访问都抛错 —— 从机制上断绝联网。"""

        def __getattr__(self, name: str):
            raise RuntimeError(
                f"测试禁止访问 akshare.{name}（`conftest.block_real_network` 在拦截）。"
                f"若本用例需要真实数据，请写缓存/夹具，或在用例内显式 monkeypatch 本守卫。"
            )

    def blocked_ak(self):
        if self.offline:
            raise DataUnavailable(
                "offline=True：禁止访问 akshare（离线模式只读 data/cache 与 data/fixture）"
            )
        if store.in_critical_section():
            raise NetworkForbiddenInLock(
                "写锁临界区内禁止联网取数：请把该数据的获取移到锁外"
                "（用 service.prewarm_before_write 预热），或改用 --offline。"
            )
        return _NoNetworkAk()

    monkeypatch.setattr(DataSource, "_ak", blocked_ak, raising=True)

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


# =========================================================================== #
# V5-8 ★★★ 用户自选池**字节守卫**（会话级 autouse）
#
# 事故背景：本轮开发中 `data/watchlist.json` 被清空 **4 次**
# （证据：`.omx/evidence/watchlist-polluted-20261019T1349.bak` / `watchlist-smoke.bak`
#   / `watchlist-v4-probe.bak` / `watchlist-xss.bak`）。
#
# 设计要点（评审 R2-11 / test-spec V5-8）：
# * **任何形态的写入都会红**（write_text / write_bytes / json.dump / rename / unlink 都改变字节）；
# * **误报为零**：只比较会话开始 vs 结束的 `data/watchlist.json` 原始字节；
# * 红之前**先字节级还原**（用户数据不能因为一次测试跑而丢）；
# * 范围是**会话级**（不是 function 级）：能抓住「某个脚本在测试之外改了它」这类形态。
#
# ★ 本夹具只读仓库根的 `data/watchlist.json`；**测试自己的数据根一律用 tmp root**。
# =========================================================================== #
WATCHLIST_PATH = os.path.join(ROOT, "data", "watchlist.json")


def _watchlist_bytes() -> bytes | None:
    try:
        with open(WATCHLIST_PATH, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except OSError:                                     # pragma: no cover - 权限异常按不存在处理
        return None


@pytest.fixture(scope="session", autouse=True)
def _watchlist_bytes_guard():
    """会话级字节守卫：跑完整个 session 后 `data/watchlist.json` 必须**逐字节不变**。"""
    before = _watchlist_bytes()
    yield
    after = _watchlist_bytes()
    if after == before:
        return
    # ---- 有变化：先字节级还原，再响亮失败 ----
    try:
        if before is None:
            if after is not None:
                os.unlink(WATCHLIST_PATH)
        else:
            with open(WATCHLIST_PATH, "wb") as fh:
                fh.write(before)
    except OSError as exc:                              # pragma: no cover - 还原失败要更响亮
        pytest.fail(
            f"★ 有测试改写了用户自选池 {WATCHLIST_PATH}，且**还原失败**（{exc}）；"
            f"原字节 {len(before or b'')} B / 现字节 {len(after or b'')} B"
        )
    pytest.fail(
        f"★ 有测试改写了用户自选池 {WATCHLIST_PATH}"
        f"（会话前 {len(before or b'')} B → 会话后 {len(after or b'')} B）—— **已字节级还原**。"
        "测试/探针必须使用隔离 root（tmp_path），任何写仓库 `data/` 的行为都是事故（本轮已发生 4 次）。"
    )
