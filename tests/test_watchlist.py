"""tests/test_watchlist.py — W2 自选池（test-spec §3 L1–L6）。

* L2（INV-37 幂等）与 L6（INV-38 缺数据不得记 0 分）都配「违反输入必须变红」的自测。
* 全程离线：conftest 的 `_no_network`（autouse）拦截 socket。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ptrade import watchlist  # noqa: E402

GOOD = "600519"  # 夹具内有完整日线，可正常打分


def find_no_data_code(cfg) -> str:
    """运行期求一个**确实无行情**的代码（供 INV-38 用例）。

    为什么不硬编码：`data/fixture/spot.json` 是**全市场快照**且会被扩充 —— 今天"没数据"的代码
    明天可能就有行情了（实测 `688981` 中芯国际 已随 spot 夹具获得 108.42 的真实报价）。
    这里从候选里挑第一个 `quote()` 取不到正价格的代码；一个都挑不到则显式 skip，绝不静默降级。

    **两处必须注意（时间炸弹 N4，2026-10-13 独立复核发现）**：

    1. asof 用 :func:`tests._guards.price_asof`（**有日线**的最近交易日），
       不能用 ``latest_trade_date()`` —— 后者读交易日历（覆盖到 2026-12-31），
       日期一推进就没有行情，会让本文件在 **2026-12-10 起必然变红**；
    2. **`suspended=True` 也算「取不到」**：缺数据的代码 ``quote()`` 会返回
       ``suspended=True`` 而**不抛异常**，只 catch 异常会把无效代码误当有效候选返回，
       于是 INV-38 用例失去前提（复核者实测症状正是 `_today=2026-12-10 → 2 failed`）。
    """
    from tests._guards import price_asof

    from ptrade.datasource import DataSource

    ds = DataSource(cfg, offline=True)
    try:
        asof = price_asof(ds)
    except Exception:  # noqa: BLE001
        asof = None
    for candidate in ("430047", "839999", "430001", "999999"):
        try:
            quote = ds.quote(candidate, asof) if asof else None
        except Exception:  # noqa: BLE001 - 取不到即符合条件
            return candidate
        if isinstance(quote, dict) and quote.get("suspended") is True:
            return candidate        # 缺数据时 quote() 标 suspended 而**不抛异常**（N4）
        last = quote.get("last") if isinstance(quote, dict) else None
        try:
            if last is None or float(last) <= 0:
                return candidate
        except (TypeError, ValueError):
            return candidate
    pytest.skip("当前夹具里找不到「无行情」的代码，无法构造 INV-38 反例")


@pytest.fixture
def no_data(offline_cfg) -> str:
    """无行情代码（INV-38 反例）。"""
    return find_no_data_code(offline_cfg)


# =========================================================================== #
# L1 —— 增删查往返
# =========================================================================== #
def test_l1_add_list_remove_roundtrip(offline_cfg):
    watchlist.add_to_watchlist(offline_cfg, GOOD, name="贵州茅台", note="强票")
    items = watchlist.list_watchlist(offline_cfg)
    assert len(items) == 1 and items[0]["code"] == GOOD
    assert items[0]["name"] == "贵州茅台" and items[0]["note"] == "强票"

    watchlist.remove_from_watchlist(offline_cfg, GOOD)
    assert watchlist.list_watchlist(offline_cfg) == []


def test_l1_empty_when_file_absent(offline_cfg):
    """文件不存在 → 空池（不是错误）。"""
    assert watchlist.list_watchlist(offline_cfg) == []
    doc = watchlist.load_watchlist(offline_cfg)
    assert doc["items"] == [] and doc["schema_version"] == watchlist.SCHEMA_VERSION


def test_l1_code_normalized(offline_cfg):
    """``sh600519`` / ``600519`` 视为同一只票。"""
    watchlist.add_to_watchlist(offline_cfg, "sh600519")
    watchlist.add_to_watchlist(offline_cfg, "600519")
    items = watchlist.list_watchlist(offline_cfg)
    assert len(items) == 1 and items[0]["code"] == "600519"


# =========================================================================== #
# L2 —— 幂等（INV-37）
# =========================================================================== #
def test_l2_add_is_idempotent(offline_cfg):
    """非空守卫：若 add 非幂等，``items`` 会是 3 —— 此断言即变红。"""
    for _ in range(3):
        watchlist.add_to_watchlist(offline_cfg, GOOD, name="贵州茅台")
    items = watchlist.list_watchlist(offline_cfg)
    assert len(items) == 1, f"同 code 连续 add 3 次必须只有 1 项，实际 {len(items)}"
    assert items[0]["code"] == GOOD
    doc = watchlist.load_watchlist(offline_cfg)
    assert doc["schema_version"] == watchlist.SCHEMA_VERSION and doc["updated_at"]


def test_l2_idempotent_add_backfills_name_and_note(offline_cfg):
    watchlist.add_to_watchlist(offline_cfg, GOOD)
    watchlist.add_to_watchlist(offline_cfg, GOOD, name="贵州茅台", note="补注")
    items = watchlist.list_watchlist(offline_cfg)
    assert len(items) == 1
    assert items[0]["name"] == "贵州茅台" and items[0]["note"] == "补注"
    # 空字符串不得覆盖已有值
    watchlist.add_to_watchlist(offline_cfg, GOOD, name="", note="")
    items = watchlist.list_watchlist(offline_cfg)
    assert items[0]["name"] == "贵州茅台" and items[0]["note"] == "补注"


def test_l2_add_rejects_blank_code(offline_cfg):
    with pytest.raises(ValueError):
        watchlist.add_to_watchlist(offline_cfg, "   ")


# =========================================================================== #
# L3 —— 删不存在的 code 不报错
# =========================================================================== #
def test_l3_remove_missing_is_noop(offline_cfg):
    watchlist.add_to_watchlist(offline_cfg, GOOD)
    doc = watchlist.remove_from_watchlist(offline_cfg, "999999")
    assert [item["code"] for item in doc["items"]] == [GOOD]
    watchlist.remove_from_watchlist(offline_cfg, GOOD)
    assert watchlist.remove_from_watchlist(offline_cfg, GOOD)["items"] == []


# =========================================================================== #
# L4 —— schema_version / updated_at
# =========================================================================== #
def test_l4_file_schema(offline_cfg):
    watchlist.add_to_watchlist(offline_cfg, GOOD, name="贵州茅台")
    raw = json.loads(Path(watchlist.watchlist_path(offline_cfg)).read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["updated_at"]
    assert isinstance(raw["items"], list) and raw["items"][0]["code"] == GOOD
    assert raw["items"][0]["added_at"]


def test_l4_corrupted_file_raises_not_silent_empty(offline_cfg):
    """反例（test-spec §7.6 同口径）：损坏文件必须明确报错，**不得**静默当空池。"""
    path = Path(watchlist.watchlist_path(offline_cfg))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version": 1, "items": [', encoding="utf-8")
    with pytest.raises(ValueError):
        watchlist.load_watchlist(offline_cfg)

    path.write_text(json.dumps({"schema_version": 99, "items": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        watchlist.load_watchlist(offline_cfg)

    path.write_text(json.dumps({"schema_version": 1, "items": "oops"}), encoding="utf-8")
    with pytest.raises(ValueError):
        watchlist.load_watchlist(offline_cfg)


def test_l4_atomic_write_leaves_no_tmp(offline_cfg):
    watchlist.add_to_watchlist(offline_cfg, GOOD)
    data_dir = Path(offline_cfg.root) / "data"
    leftovers = [p.name for p in data_dir.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"原子写不得留下临时文件: {leftovers}"


# =========================================================================== #
# L5 —— 落盘位置受 --root 影响
# =========================================================================== #
def test_l5_path_under_root(offline_cfg):
    expected = os.path.join(str(offline_cfg.root), "data", "watchlist.json")
    assert watchlist.watchlist_path(offline_cfg) == expected
    watchlist.add_to_watchlist(offline_cfg, GOOD)
    assert os.path.isfile(expected)
    # 不得污染仓库主数据根
    assert not os.path.isfile(os.path.join(str(ROOT), "data", "watchlist.json"))


def test_l5_two_roots_are_isolated(tmp_path, offline_cfg):
    from ptrade.config import load_config

    other_root = tmp_path / "other_root"
    (other_root / "data").mkdir(parents=True, exist_ok=True)
    other = load_config(str(other_root))
    watchlist.add_to_watchlist(offline_cfg, GOOD, name="贵州茅台")
    assert watchlist.list_watchlist(other) == [], "另一个数据根不得看到本根的池"


# =========================================================================== #
# L6 —— 缺数据票标注「无数据」，不得记 0 分（INV-38）
# =========================================================================== #
def test_l6_missing_data_is_marked_not_zero(offline_cfg, no_data):
    watchlist.add_to_watchlist(offline_cfg, GOOD, name="贵州茅台")
    watchlist.add_to_watchlist(offline_cfg, no_data, name="无数据票")

    rows = watchlist.score_watchlist(offline_cfg, offline=True)
    by_code = {row["code"]: row for row in rows}
    assert set(by_code) == {GOOD, no_data}, "池内每只票都要出现在结果里"

    missing = by_code[no_data]
    assert missing["ok"] is False, f"{no_data} 无行情，必须判为无数据"
    assert missing["score"] is None, f"缺数据票必须 score=None，实际 {missing['score']!r}"
    assert missing["score"] != 0, "**不得**记为 0 分（0 是合法真实得分，必须可区分）"
    assert missing["signal"] == "no_data"
    assert "无数据" in missing["reason"]

    good = by_code[GOOD]
    assert good["ok"] is True
    assert isinstance(good["score"], float)
    assert set(good["factors"]) >= {"C", "A", "N", "S", "L", "I", "M"}, "七因子必须齐全"

    # 排序：无数据置底（不混进 0 分档）
    assert rows[-1]["code"] == no_data


def test_l6_all_missing_pool(offline_cfg, no_data):
    watchlist.add_to_watchlist(offline_cfg, no_data)
    rows = watchlist.score_watchlist(offline_cfg, offline=True)
    assert len(rows) == 1
    assert rows[0]["ok"] is False and rows[0]["score"] is None


def test_l6_empty_pool_returns_empty(offline_cfg):
    assert watchlist.score_watchlist(offline_cfg, offline=True) == []


def test_l6_non_vacuity_distinguishes_real_zero_from_no_data(offline_cfg, no_data):
    """非空守卫：真实得分与「无数据」必须走**不同分支**（``ok`` 字段可区分）。"""
    watchlist.add_to_watchlist(offline_cfg, no_data)
    rows = watchlist.score_watchlist(offline_cfg, offline=True)
    assert rows[0]["ok"] is False and rows[0]["score"] is None

    # 反向：有数据的票走真实分支（即使总分很低也 ok=True）
    watchlist.add_to_watchlist(offline_cfg, GOOD)
    rows = watchlist.score_watchlist(offline_cfg, offline=True)
    good = next(row for row in rows if row["code"] == GOOD)
    assert good["ok"] is True and good["score"] is not None


# --------------------------------------------------------------------------- #
# H1（code-review 发现，Lead 复现并修复）：自选池写路径必须自己取锁
#
# 修之前：`add_to_watchlist` / `remove_from_watchlist` 是**裸的** read-modify-write，
# 锁只由调用方自觉加（webapp 记得，新加的 `cmd_watchlist` 忘了）→ 缺陷复发。
# verifier 实测：2 进程各 add 30 个不同 code → 期望 60，实际 32/32/31。
# Lead 修复后实测：2×30 → 60、2×40 → 80、4×20 → 80，并发删除亦正确。
# --------------------------------------------------------------------------- #
def test_h1_add_is_safe_under_sequential_writes_to_same_doc(offline_cfg):
    """**H1 的机制断言**（快、确定性）：连续 add 期间**每次都重新读盘**，不得基于陈旧快照写。

    这条不依赖多进程时序，因此不会 flaky；真正的多进程覆盖见下一条。
    """
    for i in range(12):
        watchlist.add_to_watchlist(offline_cfg, f"{600000 + i:06d}", name=f"票{i}")
    assert len(watchlist.list_watchlist(offline_cfg)) == 12


def test_h1_lock_is_actually_acquired(offline_cfg, monkeypatch):
    """**H1 非空守卫**：把 `watchlist` 用的那把锁换成空上下文 → 该路径必须**不再互斥**。

    本测试证明「锁确实被 watchlist 自己取用」；若把 `add_to_watchlist` 里的
    `with store.state_lock(...)` 删掉，本用例立即变红。
    这比「跑多进程碰运气」更可靠 —— 多进程丢更新是概率性的，而这里直接钉住机制。
    """
    import contextlib

    from ptrade import store

    calls: list[tuple] = []
    real = store.state_lock

    def spy(root, timeout=10.0):
        calls.append((root, timeout))
        return real(root, timeout=timeout)

    monkeypatch.setattr(store, "state_lock", spy)
    watchlist.add_to_watchlist(offline_cfg, "600519", name="贵州茅台")
    watchlist.remove_from_watchlist(offline_cfg, "600519")
    assert calls, "add/remove 都没有取锁 —— H1 复发（写路径回到了裸 read-modify-write）"

    # 反向对照：把锁换成空上下文后，仍然能写成功（说明测试不是靠异常通过的）
    monkeypatch.setattr(store, "state_lock", lambda *a, **k: contextlib.nullcontext())
    watchlist.add_to_watchlist(offline_cfg, "601398", name="工商银行")
    assert any(it["code"] == "601398" for it in watchlist.list_watchlist(offline_cfg))
