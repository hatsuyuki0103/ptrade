"""tests/test_search.py — W2 搜索（test-spec `test-spec-ptrade-web.md` §2 Q1–Q10）。

纪律
----
* 全程**离线**：`tests/conftest.py` 的 `_no_network`（autouse）拦截 `socket.connect`；
  凡是可能联网的用例一律 monkeypatch `search.fetch_name_table`，**没有任何用例期望触网**。
* 非空守卫：Q6/Q7 都配「违反输入必须变红」的自测（见 `test_inv36_*` / `test_inv35_*`）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ptrade import search  # noqa: E402
from ptrade.config import load_config  # noqa: E402
from ptrade.datasource import DataUnavailable, DataSource  # noqa: E402

#: 剧情票（与 data/fixture 对齐，用于两融标注断言）
STORY_ROWS = [
    {"code": "600519", "name": "贵州茅台"},
    {"code": "002594", "name": "比亚迪"},
    {"code": "000001", "name": "平安银行"},
    {"code": "601398", "name": "工商银行"},
    {"code": "600000", "name": "浦发银行"},
]

#: Q3 专用：同一 query 同时命中「代码前缀 / 名称前缀 / 名称子串」三组
ORDER_ROWS = [
    {"code": "600001", "name": "AAA银行"},   # 代码前缀命中
    {"code": "000060", "name": "6000科技"},  # 名称前缀命中
    {"code": "000061", "name": "某某6000"},  # 名称子串命中
    {"code": "000062", "name": "无关票"},    # 不命中
]


def seed_cache(cfg, rows, *, fetched_at: datetime | None = None) -> str:
    """写入名称表缓存（信封格式与 datasource 一致）。"""
    cache_dir = os.path.join(str(cfg.root), "data", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "stock_list.json")
    entry = {
        "fetched_at": (fetched_at or datetime.now(timezone.utc)).isoformat(),
        "payload": rows,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False)
    return path


@pytest.fixture
def cfg(offline_cfg):
    """带夹具的临时数据根（offline_cfg 已复制 data/fixture）。"""
    return offline_cfg


@pytest.fixture
def cached_cfg(offline_cfg):
    seed_cache(offline_cfg, STORY_ROWS)
    return offline_cfg


def bare_cfg(tmp_path):
    """**空数据根**：无缓存、无夹具（Q6 用）。"""
    root = tmp_path / "bare_root"
    (root / "data").mkdir(parents=True, exist_ok=True)
    return load_config(str(root))


# =========================================================================== #
# Q1 / Q2 —— 代码前缀、名称子串
# =========================================================================== #
def test_q1_code_prefix_hit(cached_cfg):
    hits = search.search_stocks(cached_cfg, "600519", offline=True)
    assert [row["code"] for row in hits] == ["600519"]
    assert hits[0]["name"] == "贵州茅台"


def test_q2_chinese_name_substring_hit(cached_cfg):
    hits = search.search_stocks(cached_cfg, "茅台", offline=True)
    assert "600519" in [row["code"] for row in hits]
    # 名称子串也应命中「银行」这类共用词
    banks = [row["code"] for row in search.search_stocks(cached_cfg, "银行", offline=True)]
    assert {"000001", "601398", "600000"} <= set(banks)


# =========================================================================== #
# Q3 —— 排序（代码前缀 > 名称前缀 > 名称子串；同组按 code 升序）
# =========================================================================== #
def test_q3_ranking_order(offline_cfg):
    seed_cache(offline_cfg, ORDER_ROWS)
    hits = search.search_stocks(offline_cfg, "6000", offline=True)
    codes = [row["code"] for row in hits]
    assert codes == ["600001", "000060", "000061"], f"排序必须命中三组且按组升序: {codes}"


def test_q3_same_group_sorted_by_code(offline_cfg):
    seed_cache(
        offline_cfg,
        [
            {"code": "600900", "name": "长江电力"},
            {"code": "600100", "name": "同方股份"},
            {"code": "600300", "name": "维维股份"},
        ],
    )
    hits = search.search_stocks(offline_cfg, "600", offline=True)
    assert [row["code"] for row in hits] == ["600100", "600300", "600900"], "同组内必须按 code 升序"


# =========================================================================== #
# Q4 —— 全角 / 大小写不敏感
# =========================================================================== #
def test_q4_fullwidth_and_case_insensitive(cached_cfg):
    fullwidth = search.search_stocks(cached_cfg, "６００５１９", offline=True)
    halfwidth = search.search_stocks(cached_cfg, "600519", offline=True)
    assert [r["code"] for r in fullwidth] == [r["code"] for r in halfwidth] == ["600519"]
    # 名称的大小写也要不敏感（造一个含字母的名称）
    seed_cache(cached_cfg, [{"code": "300001", "name": "特锐德TGOOD"}])
    assert search.search_stocks(cached_cfg, "tgood", offline=True)[0]["code"] == "300001"


# =========================================================================== #
# Q5 —— 无命中返回 []（不抛错）
# =========================================================================== #
def test_q5_no_hit_returns_empty_list(cached_cfg):
    assert search.search_stocks(cached_cfg, "zzzz不存在", offline=True) == []
    assert search.search_stocks(cached_cfg, "", offline=True) == []


# =========================================================================== #
# Q6 —— offline 无缓存必须抛错（INV-36：区分「没搜到」与「拿不到数据」）
# =========================================================================== #
def test_q6_offline_without_cache_raises(tmp_path):
    cfg = bare_cfg(tmp_path)
    with pytest.raises(DataUnavailable):
        search.search_stocks(cfg, "600519", offline=True)
    with pytest.raises(DataUnavailable):
        search.load_name_table(cfg, offline=True)


def test_inv36_distinguishes_no_hit_from_no_data(cached_cfg, tmp_path):
    """同一函数：**没搜到** → ``[]``；**拿不到数据** → raise。两者必须可区分。"""
    assert search.search_stocks(cached_cfg, "zzzz不存在", offline=True) == []
    with pytest.raises(DataUnavailable):
        search.search_stocks(bare_cfg(tmp_path), "zzzz不存在", offline=True)
    # 非空守卫：把 raise 改成 return [] 会让上面第二条断言变红（见报告 INV-36 短路实验）


# =========================================================================== #
# Q7 —— offline 绝不联网（在 socket 守卫下跑全路径）
# =========================================================================== #
def test_q7_offline_never_calls_fetch(cached_cfg, monkeypatch):
    """INV-35 非空守卫：把网络入口换成「一调用就炸」，offline 路径必须完好无损。"""

    def _boom(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("offline=True 时绝不允许调用 fetch_name_table()")

    monkeypatch.setattr(search, "fetch_name_table", _boom)
    hits = search.search_stocks(cached_cfg, "茅台", offline=True)
    assert [r["code"] for r in hits] == ["600519"]
    assert search.load_name_table(cached_cfg, offline=True)


def test_q7_offline_force_refresh_still_no_network(cached_cfg, monkeypatch):
    """即便 force_refresh=True，offline 也不得联网（只读缓存）。"""

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("offline=True 时 force_refresh 也不得联网")

    monkeypatch.setattr(search, "fetch_name_table", _boom)
    rows = search.load_name_table(cached_cfg, offline=True, force_refresh=True)
    assert rows and rows[0]["code"] == "600519" or rows


def test_q7_offline_stale_cache_is_used_not_network(cached_cfg, monkeypatch):
    """离线时过期缓存仍可用（与 datasource._cached 口径一致），且绝不联网。"""
    seed_cache(cached_cfg, STORY_ROWS, fetched_at=datetime.now(timezone.utc) - timedelta(days=30))
    monkeypatch.setattr(
        search, "fetch_name_table", lambda *a, **k: pytest.fail("offline 不得联网")
    )
    assert search.search_stocks(cached_cfg, "600519", offline=True)[0]["code"] == "600519"


# =========================================================================== #
# Q8 —— 缓存 TTL（7 天）
# =========================================================================== #
def test_q8_expired_cache_triggers_refresh(offline_cfg, monkeypatch):
    seed_cache(offline_cfg, [{"code": "600519", "name": "旧名"}],
               fetched_at=datetime.now(timezone.utc) - timedelta(days=8))
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return STORY_ROWS

    monkeypatch.setattr(search, "fetch_name_table", _fetch)
    rows = search.load_name_table(offline_cfg, offline=False)
    assert calls["n"] == 1, "超出 TTL 必须刷新一次"
    assert any(r["code"] == "600519" and r["name"] == "贵州茅台" for r in rows), "必须用新数据"
    # 刷新结果必须落盘
    entry = DataSource._read_entry(os.path.join(str(offline_cfg.root), "data", "cache"), "stock_list")
    assert len(entry["payload"]) == len(STORY_ROWS)


def test_q8_fresh_cache_does_not_refresh(offline_cfg, monkeypatch):
    seed_cache(offline_cfg, STORY_ROWS, fetched_at=datetime.now(timezone.utc) - timedelta(days=6))

    def _fetch():  # pragma: no cover - 触发即失败
        pytest.fail("TTL 内不得刷新")

    monkeypatch.setattr(search, "fetch_name_table", _fetch)
    rows = search.load_name_table(offline_cfg, offline=False)
    assert len(rows) == len(STORY_ROWS)


def test_q8_force_refresh_bypasses_fresh_cache(offline_cfg, monkeypatch):
    seed_cache(offline_cfg, [{"code": "600519", "name": "旧名"}])
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return STORY_ROWS

    monkeypatch.setattr(search, "fetch_name_table", _fetch)
    search.load_name_table(offline_cfg, offline=False, force_refresh=True)
    assert calls["n"] == 1


# =========================================================================== #
# Q9 —— limit
# =========================================================================== #
def test_q9_limit(offline_cfg):
    rows = [{"code": f"0000{i:02d}", "name": f"样本{i}"} for i in range(10)]
    seed_cache(offline_cfg, rows)
    hits = search.search_stocks(offline_cfg, "0000", offline=True, limit=5)
    assert len(hits) == 5
    assert [r["code"] for r in hits] == ["000000", "000001", "000002", "000003", "000004"]
    assert len(search.search_stocks(offline_cfg, "0000", offline=True, limit=0)) == 0
    assert len(search.search_stocks(offline_cfg, "0000", offline=True, limit=-3)) == 0


# =========================================================================== #
# Q10 —— 两融标注与 margin_targets.json 一致
# =========================================================================== #
def test_q10_margin_flags_match_margin_table(cached_cfg):
    table = DataSource(cached_cfg, offline=True).margin_targets()
    expected = {
        str(code).zfill(6): (bool(fin), bool(short))
        for code, fin, short in zip(table["code"], table["can_finance"], table["can_short"])
    }
    hits = {row["code"]: row for row in search.load_name_table(cached_cfg, offline=True)}
    for code in ("600519", "002594", "000001"):
        assert expected[code] == (hits[code]["is_margin"], hits[code]["is_short"]), code
    # 表外代码 → 明确 False（不是未知）
    seed_cache(cached_cfg, STORY_ROWS + [{"code": "999999", "name": "表外票"}])
    out = {row["code"]: row for row in search.load_name_table(cached_cfg, offline=True)}
    assert (out["999999"]["is_margin"], out["999999"]["is_short"]) == (False, False)


def test_q10_margin_flags_unknown_when_table_unavailable(tmp_path):
    """两融表不可用 → 标注 ``None``（未知），**不谎报 False**。"""
    cfg = bare_cfg(tmp_path)
    seed_cache(cfg, STORY_ROWS)
    rows = search.load_name_table(cfg, offline=True)
    assert rows and all(r["is_margin"] is None and r["is_short"] is None for r in rows)


# =========================================================================== #
# 名称表抓取（网络路径，全部 monkeypatch，绝不真联网）
# =========================================================================== #
def test_fetch_name_table_parses_and_sorts(monkeypatch):
    pd = pytest.importorskip("pandas")

    calls = []

    class _FakeAk:
        @staticmethod
        def stock_info_a_code_name():
            calls.append("primary")
            return pd.DataFrame({"code": ["000001", "600519"], "name": ["平安银行", "贵州茅台"]})

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk)
    rows = search.fetch_name_table()
    assert [r["code"] for r in rows] == ["000001", "600519"]
    assert calls == ["primary"]


def test_fetch_name_table_falls_back_to_spot(monkeypatch):
    pd = pytest.importorskip("pandas")
    calls = []

    class _FakeAk:
        @staticmethod
        def stock_info_a_code_name():
            calls.append("primary")
            raise RuntimeError("接口挂了")

        @staticmethod
        def stock_zh_a_spot_em():
            calls.append("fallback")
            return pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"]})

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk)
    rows = search.fetch_name_table()
    assert calls == ["primary", "fallback"]
    assert rows == [{"code": "600519", "name": "贵州茅台"}]


def test_fetch_name_table_raises_when_all_sources_fail(monkeypatch):
    class _FakeAk:
        @staticmethod
        def stock_info_a_code_name():
            raise RuntimeError("x")

        @staticmethod
        def stock_zh_a_spot_em():
            raise RuntimeError("y")

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk)
    with pytest.raises(DataUnavailable):
        search.fetch_name_table()


def test_fetch_name_table_rejects_empty_payload(offline_cfg, monkeypatch):
    """上游返回空表 → 必须抛错，**不得**把空表写进缓存冒充成功（INV-36 的后半）。"""
    monkeypatch.setattr(search, "fetch_name_table", lambda: [])
    with pytest.raises(DataUnavailable):
        search.load_name_table(offline_cfg, offline=False)
    assert not os.path.isfile(search.cache_path(offline_cfg)), "空表不得落盘"


def test_cache_path_is_under_root(cfg):
    assert search.cache_path(cfg) == os.path.join(str(cfg.root), "data", "cache", "stock_list.json")
    assert search.CACHE_NAME == "stock_list.json"
    assert search.DEFAULT_TTL_DAYS == 7
