"""v9 判据：持仓**空头列** + **读时实时重估**（只读无副作用）。

真源：`.omx/plans/prd-ptrade-v9.md`

## 用户实测（2026-11-03）
> 「我昨天模拟下单了，怎么**成本、市值、浮盈**这些都还没计算出来呢
>   而且昨天 3.19 下的单，现在 3.16，我应该也有赚了」

两个缺陷：
* **A**：持仓表**只有多头列** ⇒ 空头仓位完全看不见（纯前端）
* **B**：`last_price` 只在 `settle` 时更新 ⇒ 浮盈恒 0（后端需读时重估）

★ 修复中还抓到我自己的一个 bug：`webapp.py` 的 `from . import service` 是**局部 import**（避循环导入），
  导致 `api_positions` 里 `service` **NameError**，又被宽 `except Exception` **静默吞掉** ✗
  ⇒ 本条判据同时钉住「**两处局部 import 必须存在**」和「**不吞 NameError**」（V9-7）。
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from ptrade import service
from ptrade import webapp as webapp_mod
from ptrade import webui as webui_mod

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _FakeSpot:
    """假 `spot()`：返回指定价格的 DataFrame。"""

    def __init__(self, price: float):
        import pandas as pd

        self._df = pd.DataFrame([{"code": "600594", "name": "益佰制药", "last": price}])

    def __len__(self):
        return 1

    def __getattr__(self, item):
        return getattr(self._df, item)


class _FakeDS:
    def __init__(self, price: float | None = 3.16, raises: bool = False):
        self._price = price
        self._raises = raises

    def spot(self, codes=None):  # noqa: ARG002
        if self._raises:
            raise RuntimeError("取价失败")
        if self._price is None:
            import pandas as pd

            return pd.DataFrame(columns=["code", "last"])
        return _FakeSpot(self._price)


POS = [{
    "code": "600594", "name": "益佰制药",
    "long_qty": 0, "long_cost": 0.0, "long_value": 0.0, "long_pnl": 0.0,
    "short_qty": 15700, "short_cost": 3.19, "last_price": 3.19,
    "short_value": 50083.0, "short_pnl": 0.0,
}]


# ── V9-1 / V9-2 · 重估用实时价 + 空头浮盈公式
def test_v9_1_2_live_remark_uses_current_price():
    """V9-1/V9-2：`last_price` = 实时价 3.16；空头浮盈 = (3.19−3.16)×15700 = **+471**。"""
    out = service.mark_positions_live(_FakeDS(3.16), POS)
    assert len(out) == 1
    p = out[0]
    assert p["last_price"] == pytest.approx(3.16, abs=1e-9), f"应用实时价，实际 {p['last_price']}"
    assert p["short_value"] == pytest.approx(15700 * 3.16, abs=0.01)
    assert p["short_pnl"] == pytest.approx((3.19 - 3.16) * 15700, abs=0.01), (
        f"空头浮盈应为 (cost−last)×qty = +471，实际 {p['short_pnl']}"
    )
    assert p["price_source"] == "live"


# ── V9-4 · 取不到价 ⇒ 退回 state 的 last_price（不编造、不置 0）
def test_v9_4_falls_back_to_state_price_when_quote_unavailable():
    """V9-4：取价失败时**退回** `state.last_price`（3.19），浮盈为 0 —— **不是编造或置空**。"""
    out = service.mark_positions_live(_FakeDS(raises=True), POS)
    p = out[0]
    assert p["last_price"] == pytest.approx(3.19), f"应退回 state 价，实际 {p['last_price']}"
    assert p["short_pnl"] == pytest.approx(0.0, abs=1e-9)
    assert p["price_source"] == "state"


# ── V9-3 · 重估**不修改入参**（读路径无副作用的前提）
def test_v9_3_remark_does_not_mutate_input():
    """V9-3：`mark_positions_live` **不得修改传入的 position**（它是读路径，state 必须不变）。"""
    original = [dict(POS[0])]
    snapshot = {k: v for k, v in original[0].items()}
    service.mark_positions_live(_FakeDS(3.16), original)
    assert original[0] == snapshot, "入参被修改了 —— 读路径必须无副作用"


# ── V9-7 · 两处局部 import 必须存在（否则 NameError 被宽 except 吞掉）
def test_v9_7_service_imported_locally_in_both_endpoints():
    """V9-7：`api_positions` / `api_state` 里都必须有 `from . import service`。

    ★ 这条防的正是本轮踩到的坑：`webapp.py` 的 `service` 是**局部 import**（避循环导入），
      忘了在本方法里再 import 一次 ⇒ `NameError` ⇒ 被宽 `except` 吞掉 ⇒ 「重估静默不生效」✗
    """
    # ★ 只要求 `/api/positions` —— `/api/state` **刻意不做重估**（它受 INV-40 约束，
    #   必须与 `ptrade status --json` 逐字段一致，见 test_webapp.py::test_w2_...）
    src = inspect.getsource(webapp_mod.WebApp.api_positions)
    assert "from . import service" in src, "api_positions 缺局部 import ⇒ 会 NameError"
    assert "mark_positions_live" in src, "api_positions 没有接入重估"
    state_src = inspect.getsource(webapp_mod.WebApp.api_state)
    assert "mark_positions_live" not in state_src, (
        "api_state 不得重估 —— 它受 INV-40 约束，必须与 `ptrade status --json` 逐字段一致"
    )


# ── V9-7b · 不许再用宽 `except Exception` 吞重估错误
def test_v9_7b_no_broad_except_around_remark():
    """V9-7b：重估处的 `except` **不得**是裸 `Exception`（会吞掉 NameError/AttributeError 这类**编程错误**）。

    ★ 本轮实测：宽 except 把 `NameError: service` 吞了 ⇒ 表现为「功能静默失效」，极难查 ✗
    """
    src = inspect.getsource(webapp_mod.WebApp.api_positions)
    assert "except Exception" not in src, "api_positions 又用了宽 except（会吞编程错误）✗"


# ── V9-5 · 前端持仓表含空头列
def test_v9_5_positions_table_has_short_columns():
    """V9-5：持仓表的表头必须含**空头**四列（数量/成本/市值/浮盈）+ 实时价。"""
    html = webui_mod.APP_HTML
    for token in ("空头数量", "空头成本", "空头市值", "空头浮盈", "实时价"):
        assert token in html, f"持仓表表头缺「{token}」"
    # 旧表头不应再是唯一形态
    assert "多头数量" in html and "sideCells" in html, "缺少多空两侧的单元格构造"
    # `num0` 必须定义（我在 renderPositions 里用了它）
    assert "function num0(" in html, "num0 未定义 ⇒ renderPositions 会 ReferenceError"
