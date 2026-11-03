"""v8 判据：**长周期共享同一时间视野**（10 年）。

真源：`.omx/plans/prd-ptrade-v8.md` · `.omx/plans/test-spec-ptrade-v8.md`

## 为什么需要这条
用户 2026-11-03 反馈「月线初始给的柱子太少了…月线帮我再放长一点时间」。
根因不是"月线窗口短"（周线月线**都是 5 年**），而是**根数差 4.3 倍**（520 vs 60 之前是 260 vs 60）⇒
用户看着月线"稀疏" ✗

★ 所以真正的契约是：**所有"长周期"必须共享同一时间视野**
  `weekly_bars / 52 ≈ monthly_bars / 12`（**年数相同**）
⇒ 将来只改一个、忘了另一个，这条判据就会红 ✓
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = ROOT / "ptrade" / "webui_assets" / "chart_option.js"


def _bars() -> dict[str, int]:
    text = JS.read_text(encoding="utf-8")
    m = re.search(r"const DEFAULT_VISIBLE_BARS = \{([^}]*)\};", text)
    assert m, "chart_option.js 里找不到 DEFAULT_VISIBLE_BARS"
    out: dict[str, int] = {}
    for key, val in re.findall(r"(\w+)\s*:\s*(\d+)", m.group(1)):
        out[key] = int(val)
    return out


def test_v8_1_default_visible_bars_values():
    """V8-1：三个周期的默认可见根数 = 120 / 260 / 240。

    ★ **用户二次确认（2026-11-03）原话**：
      「你可以**周线 5 年**，**月线 10 年甚至 20 年**，**周线 5 年这个量我满足了**」
      ⇒ weekly 回 260（5 年）· monthly 240（20 年）· daily 120（近半年）
    """
    bars = _bars()
    assert bars.get("daily") == 120, f"daily 应为 120（近半年），实际 {bars}"
    assert bars.get("weekly") == 260, f"weekly 应为 260（5 年，用户满意），实际 {bars}"
    assert bars.get("monthly") == 240, f"monthly 应为 240（20 年），实际 {bars}"


def test_v8_1b_monthly_is_much_longer_than_weekly():
    """V8-1b（**核心，已按用户二次确认改写**）：**月线视野必须显著长于周线**。

    ★ 第一版这条判据断言「周月**同一**视野」（都 10 年），但用户随后明确：
      「周线 5 年…**月线 10 年甚至 20 年**」⇒ **两者本来就该不同** ✗ 已改。
    ⇒ 现在的契约：**monthly 的年数 ≥ weekly 的 1.8 倍**（5 年 → 20 年 = 4 倍，远超阈值）。
      这条能抓住「只把 monthly 改回 60 却忘改判据」的回归 ✓
    """
    bars = _bars()
    weekly_years = bars["weekly"] / 52.0
    monthly_years = bars["monthly"] / 12.0
    assert monthly_years >= 10.0, f"月线至少 10 年（用户说 10 年甚至 20 年），实际 {monthly_years:.1f} 年"
    assert monthly_years >= weekly_years * 1.8, (
        f"月线视野必须显著长于周线：weekly={bars['weekly']}≈{weekly_years:.1f}年 "
        f"vs monthly={bars['monthly']}≈{monthly_years:.1f}年"
    )


def test_v8_1c_daily_still_half_year():
    """V8-1b：日线首屏仍是「近半年」（≈120 交易日），不许被顺手改掉。"""
    bars = _bars()
    assert 100 <= bars["daily"] <= 140, f"daily 应≈120（近半年），实际 {bars['daily']}"


def test_v8_1d_v6_contract_kept():
    """V8-5：v6 的缩放契约不变 —— 用**索引**、两个组件同步、`filterMode:"filter"` 显式。"""
    text = JS.read_text(encoding="utf-8")
    assert "startValue:startIndex" in text and "endValue:endIndex" in text, "必须用索引而非百分比"
    assert 'filterMode: "filter"' in text or 'filterMode:"filter"' in text, "filterMode 必须显式"
    assert "start:startPct" not in text, "旧百分比形态必须彻底删除"
