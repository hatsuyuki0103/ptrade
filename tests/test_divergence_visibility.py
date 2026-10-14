"""N7：账实分叉的**可见性**。

分叉（ledger 有单、state 无单）意味着「重试同一笔下单可能重复成交」。
修前三处都看不见：`/api/health` 只报 `status:"ok"`、网页 UI **完全不渲染**、
`status` 分叉时仍 rc=0。

**三态必须分清**（本项目铁律：不把「不知道」当成「没问题」）：

* `diverged is True`  → 一致性问题**已确认**
* `diverged is None`  → **无法判定**（拿不到写锁 / 缺水位 / 一侧不可读）
* `diverged is False` → 一致
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from ptrade import core, store
from ptrade.config import load_config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def web_cfg(tmp_path):
    """隔离数据根 + 已建仓账户 + **已刷 ledger 水位**的 state.json。

    这是 `tests/test_webapp.py::web_cfg` 的精简版（夹具不能跨文件共享，
    所以这里重建 —— 但**必须带水位**，否则一致性恒为「无法判定」，
    N7 的三态就测不出来）。
    """
    for name in ("fixture", "scenario"):
        source = REPO_ROOT / "data" / name
        if not source.is_dir():
            continue
        target = tmp_path / "data" / name
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.suffix == ".json":
                shutil.copy2(item, target / item.name)

    from ptrade.datasource import DataSource
    from tests._guards import price_asof

    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=True)
    latest = price_asof(ds)
    prev = ds.prev_trade_date(latest)
    price = ds.last_close("600519", prev)
    state = core.new_state(cfg)
    order = core.Order(
        order_id=core.next_order_id(state), ts=f"{prev}T15:00:00", trade_date=prev,
        code="600519", name="贵州茅台", side=core.Side.BUY.value, qty=100, price=price,
    )
    commission, tax, transfer = core.calc_fees(order.side, price, order.qty, cfg.fee)
    order.fee, order.tax, order.transfer_fee = commission, tax, transfer
    plan = core.fill_plan(state, order, cfg.margin, cfg.fee)
    order.cash_delta, order.margin_used = plan["cash_delta"], plan["margin_used"]
    order.debt_delta, order.realized_pnl = plan["debt_delta"], plan["realized_pnl"]
    state = core.apply_fill(state, order, cfg.margin, cfg.fee)
    state.history.append(order)
    state = core.settle_t1(state, latest)
    # save_state 会写 **ledger 水位**（app.ledger_lines）→ 一致性可判定
    store.save_state(cfg, state, source="test")
    return cfg


def _inject_ghost_line(cfg) -> None:
    """在 ledger 末尾追加一行**幽灵成交**（state 侧不知道）→ 制造真实分叉。"""
    store.append_jsonl(
        pathlib.Path(store.ledger_path(cfg)),
        {"order_id": "OGHOST", "code": "600519", "side": "buy", "qty": 100,
         "price": 100.0, "status": "filled", "error_code": "OK"},
    )


# --------------------------------------------------------------------------- #
# /api/health
# --------------------------------------------------------------------------- #
def _health(cfg) -> tuple[int, dict]:
    from tests.test_webapp import _app, _call

    status, payload = _call(_app(cfg), "GET", "/api/health")
    return status, payload["data"]


def test_n7_health_reports_divergence_and_503(web_cfg):
    """**分叉 → HTTP 503 + `status:"diverged"`**（否则 `curl -f` 判不出来）。"""
    assert _health(web_cfg)[1]["diverged"] is False       # 夹具先是一致
    _inject_ghost_line(web_cfg)

    status, data = _health(web_cfg)
    assert status == 503, f"分叉时健康检查必须非 2xx（实测 {status}）"
    assert data["status"] == "diverged", data
    assert data["diverged"] is True
    assert data["consistent"] is False
    # 扁平化字段：脚本一条 `jq .data.diverged` 就能判，不必懂内部结构
    assert data["ledger_consistency"]["ledger_lines"] == data["ledger_consistency"]["recorded"] + 1


def test_n7_health_stays_ok_when_consistent(web_cfg):
    """**对照**：一致时仍是 200 + `ok`（否则「503」可以靠「恒报不健康」蒙过去）。"""
    status, data = _health(web_cfg)
    assert status == 200 and data["status"] == "ok", data
    assert data["diverged"] is False and data["consistent"] is True


def test_n7_health_unknown_when_no_watermark(tmp_path):
    """**无水位**（旧版 state.json）→ `diverged is None` → **200 + `status:"unknown"`**。

    **不改报 `ok`**：那是把「不知道」当成「没问题」。
    **也不报 503**：「没查成」不等于「不健康」（否则脚本会误以为账户坏了）。
    """
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    cfg = load_config(str(tmp_path))
    doc = core.state_document(core.new_state(cfg))
    doc["app"].pop("ledger_lines", None)          # 抹掉水位
    path = os.path.join(str(tmp_path), "data", "state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)

    status, data = _health(cfg)
    assert status == 200, f"「无法判定」不该报不健康（实测 {status}）"
    assert data["status"] == "unknown", data
    assert data["diverged"] is None, data
    assert data["consistent"] is False, "consistent 必须为 False（不知道 ≠ 一致）"


# --------------------------------------------------------------------------- #
# CLI status 的退出码
# --------------------------------------------------------------------------- #
def _run_status(cfg) -> tuple[int, dict]:
    p = subprocess.run(
        [sys.executable, "-m", "ptrade", "status", "--json", "--root", str(cfg.root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), env={**os.environ, "PYTHONUTF8": "1"}, timeout=180,
    )
    return p.returncode, json.loads(p.stdout)


def test_n7_status_rc1_on_divergence(web_cfg):
    """**分叉 → rc=1**（修前「分叉了但 rc=0」，`if ! ptrade status` 完全看不出来）。"""
    rc, payload = _run_status(web_cfg)
    assert rc == 0 and payload["data"]["ledger_consistency"]["diverged"] is False

    _inject_ghost_line(web_cfg)
    rc, payload = _run_status(web_cfg)
    assert rc == 1, f"分叉时退出码必须为 1（实测 {rc}）"
    # **`ok` 仍为 true**：命令确实成功了；`ok` 描述命令，退出码描述「是否需要人工处理」
    assert payload["ok"] is True, "分叉不是「命令失败」，ok 应仍为 true"
    assert payload["data"]["ledger_consistency"]["diverged"] is True


def test_n7_status_rc0_when_consistent(web_cfg):
    """**对照**：一致时 rc=0（否则「rc=1」可以靠「恒返 1」蒙过去）。"""
    rc, payload = _run_status(web_cfg)
    assert rc == 0, payload
    assert payload["ok"] is True


# --------------------------------------------------------------------------- #
# 网页 UI 渲染
# --------------------------------------------------------------------------- #
def test_n7_webui_renders_divergence_banner():
    """**网页 UI 必须真的渲染分叉** —— 修前 `webui.py` 里 `diverged` 命中 0 次。"""
    import io

    from ptrade import webui

    html = io.StringIO()
    webui.write_index(html) if hasattr(webui, "write_index") else None
    src = pathlib.Path(webui.__file__).read_text(encoding="utf-8")

    assert "divergence" in src, "缺少分叉横幅元素"
    assert "renderDivergence" in src, "缺少渲染函数"
    assert "is-diverged" in src and "is-unknown" in src, "缺少三态样式"
    # 三态文案都要在（把「不知道」写成「没问题」是最容易犯的错）
    assert "账实分叉" in src, "缺「已确认分叉」的文案"
    assert "无法判定" in src, "缺「无法判定」的文案"


def test_n7_webui_divergence_rendered_from_api_shape(web_cfg):
    """**端到端**：`/api/state` 的 `ledger_consistency` 能喂给 UI 的渲染函数。

    只查关键字不算验证 —— 这里断言 **UI 渲染所需的字段确实来自 API 响应**，
    并且三态的判据（`diverged` 是 `True`/`None`/`False`）在真实响应里都可达。
    """
    from tests.test_webapp import _app, _call

    app = _app(web_cfg)
    _, payload = _call(app, "GET", "/api/state")
    lc = payload["data"]["ledger_consistency"]
    assert lc["diverged"] is False, "夹具应一致"

    _inject_ghost_line(web_cfg)
    _, payload2 = _call(app, "GET", "/api/state")
    lc2 = payload2["data"]["ledger_consistency"]
    assert lc2["diverged"] is True
    # UI 读的三个键都在
    for key in ("diverged", "ledger_lines", "recorded"):
        assert key in lc2, f"UI 需要 {key}，响应里没有：{lc2}"
