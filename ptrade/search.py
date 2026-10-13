"""ptrade/search.py — 全市场股票搜索（PRD `prd-ptrade-web.md` §5.2，W2）。

职责
----
* 全市场 A 股「代码 + 名称」表：联网抓取后落 ``<root>/data/cache/stock_list.json``，
  信封格式与 :mod:`ptrade.datasource` 一致（``{"fetched_at", "payload"}``），TTL 默认 7 天。
* 按代码前缀 / 名称前缀 / 名称子串搜索（全角半角、大小写不敏感），结果标注两融属性。

不变量
------
* **INV-35**：``offline=True`` 时**绝不发起任何网络请求** —— 只读缓存（新鲜优先、过期兜底）。
* **INV-36**：**没搜到** → 返回 ``[]``；**拿不到数据**（离线且无缓存 / 上游失败）→ 抛
  :class:`~ptrade.datasource.DataUnavailable`。两者语义严格区分，不得静默返回空表。

复用而非另起一套：缓存信封读写与 TTL 判定直接复用 :class:`ptrade.datasource.DataSource`
的静态助手（``_read_entry`` / ``_write_entry`` / ``_expired``），代码归一化复用 ``_norm_code``。
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any, Iterable

from .config import AppConfig
from .datasource import DataSource, DataUnavailable, _norm_code

__all__ = [
    "CACHE_NAME",
    "DEFAULT_TTL_DAYS",
    "cache_path",
    "fetch_name_table",
    "load_name_table",
    "search_stocks",
]

CACHE_NAME = "stock_list.json"
DEFAULT_TTL_DAYS = 7
DEFAULT_TTL_SECONDS = DEFAULT_TTL_DAYS * 24 * 3600

#: 名称表的 akshare 候选接口（首选专用接口，兜底用已在用的全市场快照）。
_NAME_TABLE_ENDPOINTS = ("stock_info_a_code_name", "stock_zh_a_spot_em")
_CODE_COLUMNS = ("code", "代码", "证券代码")
_NAME_COLUMNS = ("name", "名称", "证券简称")


def cache_path(cfg: AppConfig) -> str:
    """名称表缓存文件路径：``<root>/data/cache/stock_list.json``。"""
    return os.path.join(str(cfg.root), "data", "cache", CACHE_NAME)


def _normalize(text: Any) -> str:
    """搜索归一化：NFKC（全角→半角）→ 去首尾空白 → 小写。

    ``６００５１９`` → ``600519``；``ＢＵＹ`` → ``buy``。
    """
    return unicodedata.normalize("NFKC", str(text or "")).strip().lower()


def _pick_column(frame: Any, candidates: Iterable[str]) -> Any:
    columns = {str(col): col for col in getattr(frame, "columns", [])}
    for name in candidates:
        if name in columns:
            return columns[name]
    return None


def fetch_name_table() -> list[dict]:
    """联网抓取全市场名称表（**本模块唯一的网络入口**，测试里 monkeypatch 此函数）。

    返回 ``[{"code", "name"}]``（代码为 6 位、升序）。上游不可用或返回空表 → ``DataUnavailable``。
    """
    import akshare as ak  # 延迟导入：offline 路径永不触发

    errors: list[str] = []
    for endpoint in _NAME_TABLE_ENDPOINTS:
        fn = getattr(ak, endpoint, None)
        if fn is None:
            errors.append(f"{endpoint}: 接口不存在")
            continue
        try:
            frame = fn()
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一降级到下一个来源
            errors.append(f"{endpoint}: {type(exc).__name__} {exc}")
            continue
        if frame is None or len(frame) == 0:
            errors.append(f"{endpoint}: 返回空表")
            continue
        code_col = _pick_column(frame, _CODE_COLUMNS)
        name_col = _pick_column(frame, _NAME_COLUMNS)
        if code_col is None or name_col is None:
            errors.append(f"{endpoint}: 缺少代码/名称列（实际列 {list(frame.columns)[:6]}）")
            continue
        rows: dict[str, str] = {}
        for code, name in zip(frame[code_col].tolist(), frame[name_col].tolist()):
            normalized = _norm_code(code)
            if len(normalized) != 6 or not normalized.isdigit():
                continue
            rows[normalized] = str(name or "").strip()
        if not rows:
            errors.append(f"{endpoint}: 无有效代码行")
            continue
        return [{"code": code, "name": rows[code]} for code in sorted(rows)]
    raise DataUnavailable("无法获取全市场股票名称表：" + "；".join(errors or ["无可用接口"]))


def _margin_flags(cfg: AppConfig, *, offline: bool) -> dict[str, tuple[bool, bool]] | None:
    """读取两融名单 → ``{code: (is_margin, is_short)}``；不可用返回 ``None``（标注为未知）。"""
    try:
        frame = DataSource(cfg, offline=offline).margin_targets()
    except Exception:  # noqa: BLE001 - 两融表是附注信息，缺失不应让搜索整体失败
        return None
    if frame is None or len(frame) == 0:
        return None
    flags: dict[str, tuple[bool, bool]] = {}
    for code, can_finance, can_short in zip(
        frame["code"].tolist(), frame["can_finance"].tolist(), frame["can_short"].tolist()
    ):
        flags[_norm_code(code)] = (bool(can_finance), bool(can_short))
    return flags


def _annotate(
    cfg: AppConfig, rows: list[dict], *, offline: bool
) -> list[dict]:
    """给名称表补 ``is_margin`` / ``is_short``（两融表不可用时为 ``None`` = 未知，不谎报 False）。"""
    flags = _margin_flags(cfg, offline=offline)
    out: list[dict] = []
    for row in rows:
        code = _norm_code(row.get("code"))
        if flags is None:
            is_margin, is_short = None, None
        else:
            is_margin, is_short = flags.get(code, (False, False))
        out.append(
            {
                "code": code,
                "name": str(row.get("name") or ""),
                "is_margin": is_margin,
                "is_short": is_short,
            }
        )
    return out


def load_name_table(
    cfg: AppConfig, *, offline: bool, force_refresh: bool = False
) -> list[dict]:
    """返回 ``[{"code","name","is_margin","is_short"}]``（按 code 升序）。

    * 新鲜缓存（< TTL）或 ``force_refresh=False`` 下的过期缓存 → 直接用；
    * ``offline=True``：**只读缓存**；无缓存 → ``DataUnavailable``（**不得静默返回空**）；
    * ``offline=False``：缓存过期（或 ``force_refresh=True``）→ 联网刷新并落盘。
    """
    cache_dir = os.path.join(str(cfg.root), "data", "cache")
    entry = DataSource._read_entry(cache_dir, "stock_list")
    fresh = entry is not None and not DataSource._expired(entry, DEFAULT_TTL_SECONDS)

    if fresh and not force_refresh:
        rows = entry["payload"]
    elif offline:
        if entry is None:
            raise DataUnavailable(
                f"离线模式缺少股票名称表缓存（{cache_path(cfg)}），无法搜索；"
                "请先联网执行一次 search 以生成缓存"
            )
        rows = entry["payload"]  # 离线只能用过期缓存（与 datasource._cached 口径一致）
    else:
        rows = fetch_name_table()
        if not rows:
            raise DataUnavailable("股票名称表为空表，拒绝写入缓存")
        DataSource._write_entry(cache_dir, "stock_list", rows)

    if not isinstance(rows, list) or not rows:
        raise DataUnavailable(f"股票名称表缓存内容非法（{cache_path(cfg)}）")
    return _annotate(cfg, rows, offline=offline)


def search_stocks(
    cfg: AppConfig,
    query: str,
    *,
    offline: bool,
    limit: int = 30,
    force_refresh: bool = False,
) -> list[dict]:
    """按 ``query`` 搜索股票。

    排序（PRD §5.2）：**代码前缀 > 名称前缀 > 名称子串**；同组内按 code 升序。
    全角/半角与大小写不敏感。空 query → ``[]``。

    无命中 → ``[]``（INV-36 前半）；名称表不可用 → ``DataUnavailable``（INV-36 后半）。
    """
    needle = _normalize(query)
    if not needle:
        return []
    rows = load_name_table(cfg, offline=offline, force_refresh=force_refresh)

    ranked: list[tuple[int, str, dict]] = []
    for row in rows:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        code_key, name_key = _normalize(code), _normalize(name)
        if code_key.startswith(needle):
            rank = 0
        elif name_key.startswith(needle):
            rank = 1
        elif needle in name_key:
            rank = 2
        else:
            continue
        ranked.append((rank, code, row))

    ranked.sort(key=lambda item: (item[0], item[1]))
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = 30
    return [row for _, _, row in ranked[:cap]]
