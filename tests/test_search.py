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
import pathlib
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
    # ★ 本用例测的是「过期 → 刷新 → 落盘」，**不是**行数守卫（INV-54）。
    #   STORY_ROWS 只有 5 行，而真实下限是 1000，会把落盘挡掉 ——
    #   所以这里把下限调到本用例的规模；INV-54 的**真实阈值**由
    #   `test_T_W1_1_below_min_rows_is_not_written` 与
    #   `test_T_W1_5_stock_list_min_rows_is_1000` 直接钉住。
    monkeypatch.setattr(search, "STOCK_LIST_MIN_ROWS", len(STORY_ROWS))
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


# --------------------------------------------------------------------------- #
# T-W1-1 / T-W1-2（PRD v2 · G002-W1）：名称表两臂 + 全角空格 + 行数守卫
# --------------------------------------------------------------------------- #
def _name_table(n: int) -> list[dict]:
    """造 n 行名称表：000001 平安银行 一定在内（A1 要断言命中）。"""
    rows = [{"code": "000001", "name": "平安银行"},
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000002", "name": "万科A"}]
    for i in range(len(rows), n):
        rows.append({"code": f"{i:06d}", "name": f"票{i}"})
    return rows[:n]


def test_T_W1_1_big_table_is_written_and_searchable(offline_cfg, monkeypatch):
    """★ 第一臂：mock `fetch_name_table` 返回 **5576 行** → 必须落盘 + 搜得到。

    数据来源：**mock 的 5576 行**（不联网、不依赖仓库 `data/cache`）。
    """
    rows = _name_table(5576)
    monkeypatch.setattr(search, "fetch_name_table", lambda: rows)
    got = search.load_name_table(offline_cfg, offline=False, force_refresh=True)
    assert len(got) == 5576
    cache = os.path.join(str(offline_cfg.root), "data", "cache", "stock_list.json")
    assert os.path.isfile(cache), "≥1000 行的表必须落盘"
    hits = search.search_stocks(offline_cfg, "000001", offline=False)
    assert [h["code"] for h in hits][:1] == ["000001"]
    assert hits[0]["name"] == "平安银行"


def test_T_W1_1_small_table_is_not_written_but_still_returned(offline_cfg, monkeypatch, caplog):
    """★ 第二臂：mock 返回 **5 行** → **不落盘** + warning，但返回值仍交给调用方（INV-54）。

    这条正是 A1 的「5 行不写」——防止残缺缓存长期伪装成「搜不到」。
    """
    rows = _name_table(5)
    monkeypatch.setattr(search, "fetch_name_table", lambda: rows)
    with caplog.at_level("WARNING"):
        got = search.load_name_table(offline_cfg, offline=False, force_refresh=True)
    assert len(got) == 5, "低行数不改变返回值（只在落盘上设闸）"
    cache = os.path.join(str(offline_cfg.root), "data", "cache", "stock_list.json")
    assert not os.path.isfile(cache), "5 行竟被写进缓存（伪造成『全市场表』）"
    assert any("STOCK_LIST_MIN_ROWS" in str(r.args) or "STOCK_LIST_MIN_ROWS" in str(r.message)
               for r in caplog.records), "低于阈值必须留 warning"


def test_T_W1_5_stock_list_min_rows_is_1000():
    """INV-54 的**真实阈值**必须钉住（否则上一条可以用 monkeypatch 把守卫调没）。"""
    assert search.STOCK_LIST_MIN_ROWS == 1000


def test_T_W1_1_guard_is_not_vacuous(offline_cfg, monkeypatch):
    """非空洞：把阈值调成 0 → 同一个 5 行表**会**落盘（证明上一条的红不是别的原因）。"""
    rows = _name_table(5)
    monkeypatch.setattr(search, "fetch_name_table", lambda: rows)
    monkeypatch.setattr(search, "STOCK_LIST_MIN_ROWS", 0)
    search.load_name_table(offline_cfg, offline=False, force_refresh=True)
    cache = os.path.join(str(offline_cfg.root), "data", "cache", "stock_list.json")
    assert os.path.isfile(cache), "阈值=0 时仍不落盘 → 说明拦它的是别的东西，守卫是空的"


def test_T_W1_2_fullwidth_query_hits_and_missing_returns_empty(offline_cfg, monkeypatch):
    """T-W1-2：`search("万科")` 命中 000002（含全角空格/全角数字归一）；不存在的票 → count==0。

    数据来源：mock 名称表（离线）。「搜不到」与「拿不到数据」必须区分（INV-36）。
    """
    monkeypatch.setattr(search, "fetch_name_table", lambda: _name_table(1200))
    hits = search.search_stocks(offline_cfg, "万科", offline=False, force_refresh=True)
    assert [h["code"] for h in hits] == ["000002"]
    # 全角数字 / 全角空格归一化
    assert [h["code"] for h in search.search_stocks(offline_cfg, "６００５１９", offline=False)] \
        == ["600519"]
    assert [h["code"] for h in search.search_stocks(offline_cfg, "  平安  ", offline=False)] \
        == ["000001"]
    assert search.search_stocks(offline_cfg, "不存在这只票", offline=False) == [], \
        "无命中必须是空列表（不是异常）"
    assert search.DEFAULT_TTL_DAYS == 7


# --------------------------------------------------------------------------- #
# V3-W3（task-38）A1 + A2：名称表守卫 / 内容锚点 / spot 兜底 / 强制刷新 / 自选补名称
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _legacy_small_tables(request, monkeypatch):
    """**历史用例隔离**（V3-W3）：旧用例的**被测语义**是 TTL / limit / 两融标志，**不是「坏表检测」**。

    ⇒ 它们在 `load_name_table` 引入**读路径守卫**（行数 ≥3000 + 内容锚点）后会被拦下，
    但那是**守卫生效的必然结果**，不是它们的被测目标。
    本 fixture 只给 `test_T_V3_*` **以外**的用例**临时**放宽阈值。

    ★★ **这不是「为变绿放宽断言」**：守卫的**鉴别力**由 `T_V3_*` 系列在**真实阈值**下钉住
    （见下方 `_v3_keeps_true_thresholds` 的**反向断言**）。
    ★★ **反向断言**：`T_V3_*` 一律**不进入**本条宽松范围 —— 将来有人把新用例写进宽松范围，
    那条反向断言会立刻红。
    """
    if request.node.name.startswith("test_T_V3"):
        return                      # ← T_V3_* 保持真实阈值（反向断言在下面那条 fixture）
    monkeypatch.setattr(search, "NAME_TABLE_MIN_ROWS", 1)
    monkeypatch.setattr(search, "NAME_TABLE_ANCHORS", {})


@pytest.fixture(autouse=True)
def _v3_keeps_true_thresholds(request):
    """**反向断言**：`T_V3_*` 运行时，阈值必须仍是**真实值**（证明上面的隔离 fixture 没漏进新用例）。"""
    if not request.node.name.startswith("test_T_V3"):
        return
    assert search.NAME_TABLE_MIN_ROWS == 3000, "T_V3_* 竟被历史用例的宽松 fixture 影响（静默失守）"
    assert search.NAME_TABLE_ANCHORS.get("603009") == "北特科技", "内容锚点被削弱了"


def _big_table(n: int = 5600) -> list[dict]:
    """造一张**过得了锚点**的全市场表（含三只锚点票 + 603009 事故票）。"""
    rows = [{"code": c, "name": nm} for c, nm in search.NAME_TABLE_ANCHORS.items()]
    for i in range(len(rows), n):
        rows.append({"code": f"{i:06d}", "name": f"票{i}"})
    return rows[:n]


def _seed(cfg, rows: list[dict]) -> None:
    DataSource._write_entry(os.path.join(str(cfg.root), "data", "cache"), "stock_list", rows)


def test_T_V3_1_polluted_table_raises_instead_of_empty(offline_cfg):
    """★ **T-V3-1 修前必红**：**5 行污染表**必须报「名称表不可用」，**不得**静默返回 `[]`。

    修前实测：`load_name_table()` 照单全收 5 行 → `search()` 对不在这 5 行里的票返回 `[]`
    ⇒ 用户以为「没这只票」（这就是「搜索没用」的真根因；缓存 TTL 7 天，联网也读它）。
    """
    _seed(offline_cfg, [{"code": "600519", "name": "贵州茅台"}])
    with pytest.raises(DataUnavailable) as exc:
        search.load_name_table(offline_cfg, offline=False)
    msg = str(exc.value)
    assert "名称表不可用" in msg and "3000" in msg, msg
    assert "fetched_at" in msg, "错误信息必须带 fetched_at（便于判断是不是旧表）"


def test_T_V3_1b_content_anchors_catch_half_tables(offline_cfg):
    """★ **T-V3-1b**：内容锚点**双向**判据（半截表抓、合法改名放过）。

    ## 为什么是双向的（ultraqa **F3**）

    锚点最初是**全等**校验 ⇒ 锚点票（`600519`/`000001`/**`603009` 北特科技** 都是**会合法改名的
    普通公司**）只要有一只改名，就会：① 旧表校验失败；② 用户照提示点「刷新名称表」，
    新抓的表**结构完好但锚点不符** ⇒ **不落盘** ⇒ **连自救路径也死掉，只能改代码**。

    故锚点改为 **≥2/3 命中**。本判据**同时钉住两个方向**（比原来的单向「全等」更强）：

    * **缺 1 个 ⇒ 必须放行**（合法改名场景，F3 要保护的正是它）；
    * **缺 2 个 ⇒ 必须红**（半截表/串表的典型症状，原判据要抓的正是它）。
    """
    anchors = dict(search.NAME_TABLE_ANCHORS)
    assert len(anchors) >= 3, f"本判据假设至少 3 个锚点，实际 {len(anchors)}"

    # ① 缺 **1** 个锚点（= 某只票合法改名）⇒ **必须放行**
    one = sorted(anchors)[0]
    rows1 = [r for r in _big_table() if r["code"] != one]
    _seed(offline_cfg, rows1)
    got = search.load_name_table(offline_cfg, offline=False)
    # ★ 反向断言：放行**不是**因为表变空/变小 —— 行数仍须达标
    assert len(got) >= search.NAME_TABLE_MIN_ROWS, (
        f"缺 1 个锚点被放行了，但表只剩 {len(got)} 行（< 下限 {search.NAME_TABLE_MIN_ROWS}）"
        " —— 那不是「合法改名」，是坏表！"
    )

    # ② 缺 **2** 个锚点 ⇒ **必须红**（半截/串表）
    two = sorted(anchors)[:2]
    rows2 = [r for r in _big_table() if r["code"] not in two]
    _seed(offline_cfg, rows2)
    with pytest.raises(DataUnavailable) as exc:
        search.load_name_table(offline_cfg, offline=False)
    msg = str(exc.value)
    assert "内容锚点" in msg, msg
    assert all(code in msg for code in two), f"错误信息应点名缺失的锚点 {two}：{msg}"


def test_T_V3_2_spot_fixture_fallback_is_offline_and_silent(offline_cfg, monkeypatch):
    """★ **T-V3-2**：名称表**缓存缺失**时，用 `data/fixture/spot.json`（5572 行）兜底。

    ★ 必须**离线可判定**且 **socket 计数 = 0**（兜底绝不能变成偷偷联网）。
    """
    sockets: list[int] = []
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: sockets.append(1), raising=False)
    (pathlib.Path(str(offline_cfg.root)) / "data" / "cache" / "stock_list.json").unlink(missing_ok=True)
    rows = search.load_name_table(offline_cfg, offline=True)
    assert len(rows) >= 3000, f"spot 兜底只拿到 {len(rows)} 行"
    by_code = {r["code"]: r["name"] for r in rows}
    for code, want in search.NAME_TABLE_ANCHORS.items():
        assert by_code.get(code) == want, f"{code} 在兜底表里应为 {want!r}"
    assert sockets == [], "spot 兜底竟然联网了"


def test_T_V3_3_refresh_offline_refuses_and_bad_fetch_is_rejected(offline_cfg, monkeypatch):
    """**T-V3-3**：`refresh_name_table` —— `--offline` 必须拒绝；抓回半截表必须拒绝且**不落盘**。"""
    with pytest.raises(DataUnavailable) as exc:
        search.refresh_name_table(offline_cfg, offline=True)
    assert "离线" in str(exc.value)

    monkeypatch.setattr(search, "fetch_name_table", lambda: [{"code": "600519", "name": "贵州茅台"}])
    with pytest.raises(DataUnavailable) as exc2:
        search.refresh_name_table(offline_cfg, offline=False)
    assert "名称表不可用" in str(exc2.value)
    cache_file = pathlib.Path(str(offline_cfg.root)) / "data" / "cache" / "stock_list.json"
    assert not cache_file.exists(), "半截表竟被落盘（下次又会被读回去）"

    good = _big_table()
    monkeypatch.setattr(search, "fetch_name_table", lambda: good)
    out = search.refresh_name_table(offline_cfg, offline=False)
    assert out["rows"] == len(good) and out["fetched_at"], out
    assert cache_file.exists(), "刷新成功却没落盘"


def test_T_V3_5_watchlist_add_resolves_name_server_side(offline_cfg):
    """★ **A2 / T-V3-5 修前必红**：只传 code 加入自选 → **服务端补全名称**。

    修前实测：`add_to_watchlist(cfg, "603009")` 落盘的 `name` 是 **空串**
    ⇒ 自选池里只有代码、没有名称（用户「加进自选也没什么可看」的真根因之一）。
    修后：走 **cache-only** 解析（缓存 → 夹具 spot），`name` 必须是「北特科技」。
    """
    from ptrade import watchlist as wl

    doc = wl.add_to_watchlist(offline_cfg, "603009")
    item = doc["items"][0]
    assert item["name"] == "北特科技", f"名称没补全：{item['name']!r}"
    # 再读回来（落盘内容）也必须带名称
    assert wl.list_watchlist(offline_cfg)[0]["name"] == "北特科技"


def test_T_V3_5b_resolve_name_hook_is_used(offline_cfg):
    """`resolve_name` 钩子可用（webapp 用它注入 `ds.name_of`），且**空名称不会被覆盖**。"""
    from ptrade import watchlist as wl

    doc = wl.add_to_watchlist(offline_cfg, "603009", resolve_name=lambda c: f"注入名-{c}")
    assert doc["items"][0]["name"] == "注入名-603009"
    # 幂等：再次 add（不传 name）不得把已有名称清空
    doc2 = wl.add_to_watchlist(offline_cfg, "603009")
    assert doc2["items"][0]["name"] == "注入名-603009"


def test_T_V3_22_name_resolution_must_be_outside_state_lock():
    """★ **T-V3-22（AST）**：名称解析**不得**出现在 `with …state_lock(…)` 块内（P0-4）。

    为什么必须 AST：行为判据只能验「name 非空」，**抓不到锁卫生** ——
    在锁内联网就是违 INV-42（写者互相饿死、网页 503），而那类缺陷**不会**让 name 变空。
    """
    import ast
    import pathlib as _pl

    repo = _pl.Path(__file__).resolve().parent.parent
    suspicious = ("name_of", "load_name_table", "resolve_name", "_name_from_cache_only")
    problems: list[str] = []
    for name in ("watchlist.py", "webapp.py"):
        path = repo / "ptrade" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            is_lock = any(
                "state_lock" in ast.unparse(item.context_expr) for item in node.items
            )
            if not is_lock:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    called = getattr(inner.func, "attr", None) or getattr(inner.func, "id", None)
                    if called in suspicious:
                        problems.append(f"{name}:{inner.lineno} 在 state_lock 块内调用 {called}()")
    assert not problems, "P0-4 违规（名称解析必须在锁外）：\n  " + "\n  ".join(problems)


def test_T_V3_29_refresh_single_flight(offline_cfg, monkeypatch):
    """★★ **T-V3-29**（F2b / ultraqa）：**5 路并发刷新名称表，`fetch_name_table` 只能被调用 1 次**。

    ### 为什么这是个真问题

    刷新 = **联网抓 18 页（实测 ~18-23s）再落盘**。此前 `refresh_name_table` **无条件**调
    `fetch_name_table()` ⇒ **冷缓存下 N 路并发 = N 次上游抓取**（5 路就是 5×~20s 的上游压力）。

    ### 为什么必须用桩（而不是从 HTTP 层测）

    真实 akshare 有 HTTP 缓存 ⇒ 5 路并发每路只要 ~0.2s ⇒ **快到来不及并发**，
    从 HTTP 层根本看不出单飞有没有生效。用「慢桩」（sleep + 计数）才能把并发窗口拉开。

    ### 判据

    1. **并发** 5 路 ⇒ `fetch_name_table` 调用 **恰好 1 次**（其余 4 路等锁后复用）；
    2. ★ **非空洞对照**：**串行** 5 次 ⇒ 调用 **恰好 5 次**（证明「只调 1 次」不是因为该函数本来就不调）；
    3. ★ **语义不变**：并发的 5 路都必须返回**非空 rows**（不能因为复用而返回空/报错）。
    """
    import threading
    import time

    from ptrade import search as S

    calls: list[float] = []
    lock = threading.Lock()
    # ★ 用 **9xxxxx** 段：6 位、且与三个内容锚点（600519 / 000001 / 603009）**不重叠**
    #   （第一版用了 600000+i ⇒ 撞到锚点 603009 ⇒ 假红「code 不唯一」）
    real_rows = [{"code": f"{900000 + i}", "name": f"票{i}"} for i in range(3500)]
    for anchor in S.NAME_TABLE_ANCHORS:
        real_rows.append({"code": anchor, "name": S.NAME_TABLE_ANCHORS[anchor]})

    def slow_fetch() -> list[dict]:
        with lock:
            calls.append(time.time())
        time.sleep(1.5)          # ★ 把并发窗口拉开（真实环境是 ~18-23s 的 18 页网络）
        return list(real_rows)

    monkeypatch.setattr(S, "fetch_name_table", slow_fetch)

    # ── ① 并发 5 路
    results: list[dict] = []
    errs: list[BaseException] = []
    rlock = threading.Lock()

    def one() -> None:
        try:
            r = S.refresh_name_table(offline_cfg, offline=False)
            with rlock:
                results.append(r)
        except BaseException as exc:  # noqa: BLE001
            with rlock:
                errs.append(exc)

    ths = [threading.Thread(target=one, daemon=True) for _ in range(5)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=120)

    assert not errs, f"并发刷新有异常：{errs[:2]}"
    assert len(results) == 5, f"应有 5 个结果，实际 {len(results)}"
    assert len(calls) == 1, (
        f"★ 单飞失效：`fetch_name_table` 被调用 **{len(calls)}** 次（5 路并发应只 1 次）—— "
        "冷缓存下这就是 N 倍上游压力"
    )
    assert all(isinstance(r.get("rows"), int) and r["rows"] > 0 for r in results), (
        f"复用路径返回了空/非法 rows：{[r.get('rows') for r in results]}"
    )

    # ── ② 非空洞对照：串行 5 次 ⇒ 必须调 5 次
    calls.clear()
    for _ in range(5):
        S.refresh_name_table(offline_cfg, offline=False)
    assert len(calls) == 5, (
        f"非空洞对照失败：串行 5 次只调了 {len(calls)} 次 `fetch_name_table` —— "
        "说明「只调 1 次」不是因为该函数本来就不调，那么并发那条判据是空的"
    )
