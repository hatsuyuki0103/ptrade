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

* **INV-54**：名称表写入有**行数下限** ``STOCK_LIST_MIN_ROWS``（1000）——
  低于阈值**不落盘** + warning，**返回值仍交给调用方**。
  为什么必须有：曾出现「上游只返回 5 行也落盘」→ 之后每次都从这份残缺缓存搜，
  表现为「搜什么都说没有」。口径与 PRD F2 的 ``min_rows`` 一致。

复用而非另起一套：缓存信封读写与 TTL 判定直接复用 :class:`ptrade.datasource.DataSource`
的静态助手（``_read_entry`` / ``_write_entry`` / ``_expired``），代码归一化复用 ``_norm_code``。
"""

from __future__ import annotations

import logging
import os
import unicodedata
from typing import Any, Iterable

from .config import AppConfig
from .datasource import DataSource, DataUnavailable, _norm_code, _single_flight_lock

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_NAME",
    "DEFAULT_TTL_DAYS",
    "NAME_TABLE_ANCHORS",
    "NAME_TABLE_MIN_ROWS",
    "STOCK_LIST_MIN_ROWS",
    "cache_path",
    "fetch_name_table",
    "load_name_table",
    "refresh_name_table",
    "search_stocks",
]

CACHE_NAME = "stock_list.json"
DEFAULT_TTL_DAYS = 7
DEFAULT_TTL_SECONDS = DEFAULT_TTL_DAYS * 24 * 3600

#: INV-54 / PRD F2：``stock_list`` 的行数下限（全市场 A 股约 5500+ 只）。
#: 守卫在**调用时**读模块属性，所以测试可以
#: ``monkeypatch.setattr(search, "STOCK_LIST_MIN_ROWS", n)`` 调小它。
STOCK_LIST_MIN_ROWS = 1000

#: ★ **A1（V3-W3）名称表最小行数守卫**：全市场 A 股 5500+ 只，低于此数一律视为
#: **不可用/已损坏**（Lead 实测：缓存曾被污染成 **5 行**且 TTL 7 天未过期 ⇒ 联网也读这 5 行
#: ⇒ 搜索永远 0 条）。**不得静默返回 `[]`**（那会让用户以为「没这只票」）。
NAME_TABLE_MIN_ROWS = 3000

#: ★ **内容锚点**（Architect M6）：行数守卫挡不住「半截表 2500–4000」。
#: `603009→北特科技` 是**事故票**（用户实测搜不到的那只），必须钉住。
NAME_TABLE_ANCHORS: dict[str, str] = {
    "600519": "贵州茅台",
    "000001": "平安银行",
    "603009": "北特科技",
}

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
    # ★★ F2（ultraqa，MEDIUM）：**这里必须和 `DataSource._ak()` 用同一道守卫**。
    #   本函数是**第二个网络出口**，直接 `import akshare` ⇒ 曾经**绕过** `in_critical_section()`：
    #   持写锁调 `refresh_name_table` 会**真正跑满 18/18 页网络（~10s）**才往下走，违 INV-42。
    #   fail-fast：锁内**立即**响亮报错，绝不等待、绝不联网。
    from . import store as _store

    if _store.in_critical_section():
        from .datasource import NetworkForbiddenInLock

        raise NetworkForbiddenInLock(
            "写锁临界区内禁止联网抓取名称表：请把刷新移到锁外（/api/name/refresh 已不取锁），"
            "或改用 --offline。详见 INTERFACES.md INV-42 / F2。"
        )
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


def _validate_name_table(
    rows: Any, *, fetched_at: Any = None, source: str = "cache", anchors: bool = True
) -> None:
    """**名称表守卫**（A1 / Architect M6）：行数下限 + 内容锚点 + 结构完整性。

    为什么必须有（Lead 实测的真缺陷）：`data/cache/stock_list.json` 曾被污染成 **5 行**，
    而 TTL 7 天未过期 ⇒ **联网也读这 5 行** ⇒ 搜索永远 0 条，用户以为「没这只票」。

    ★ 只查行数**不够**：「半截表」（2500–4000 行）能绕过行数门槛 ⇒ 还要查**内容锚点**：
      事故票 `603009→北特科技`、`600519→贵州茅台`、`000001→平安银行`；
    ★ 再加结构完整性：code 唯一率 100% / 全部 6 位 / name 非空率 > 95%。

    失败一律 :class:`DataUnavailable`（**绝不静默返回 `[]`** —— 那是 INV-36 的「没搜到」语义），
    且错误信息里**带行数与 ``fetched_at``**，便于用户判断是不是旧表。
    """
    where = f"{source}, fetched_at={fetched_at!r}"
    if not isinstance(rows, list) or not rows:
        raise DataUnavailable(f"名称表不可用：内容为空或不是数组（{where}）")
    total = len(rows)
    if total < NAME_TABLE_MIN_ROWS:
        raise DataUnavailable(
            f"名称表不可用/已损坏：只有 {total} 行 < 下限 {NAME_TABLE_MIN_ROWS}"
            f"（{where}）。请在网页端点「刷新名称表」，或删除 {CACHE_NAME} 缓存后重试"
        )
    codes: list[str] = []
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise DataUnavailable(f"名称表不可用：存在非对象行（{where}）")
        codes.append(str(row.get("code") or ""))
        names.append(str(row.get("name") or "").strip())
    if len(set(codes)) != total:
        dup = total - len(set(codes))
        raise DataUnavailable(f"名称表不可用：code 不唯一（{total} 行里 {dup} 条重复，{where}）")
    bad = [c for c in codes if len(c) != 6 or not c.isdigit()]
    if bad:
        raise DataUnavailable(
            f"名称表不可用：{len(bad)} 个 code 不是 6 位数字（例 {bad[:3]}，{where}）"
        )
    filled = sum(1 for n in names if n)
    if filled / total <= 0.95:
        raise DataUnavailable(
            f"名称表不可用：name 非空率 {filled}/{total} = {filled / total:.1%} ≤ 95%（{where}）"
        )
    mapping = dict(zip(codes, names))
    missing = {code: want for code, want in NAME_TABLE_ANCHORS.items() if mapping.get(code) != want}
    # ★★ F3（ultraqa，MEDIUM）：**锚点不能搞「全等」**。
    #   锚点票是**会合法改名的普通公司**；全等意味着「任意一只改名 ⇒ 读路径硬拒」，
    #   而刷新写进去的新表**同样**缺这个锚点 ⇒ **用户点刷新后依然搜不到**（自救只修了写没修读）。
    #   ⇒ 改为 **≥2/3 命中**：单只改名不再致命；而「半截表/串表」通常缺 2 个以上 ⇒ **仍被拦**。
    #   （行数下限 `NAME_TABLE_MIN_ROWS` 那道硬闸仍在，两者互补。）
    if len(missing) >= 2:
        detail = "；".join(f"{code} 期望 {want!r}，实际 {mapping.get(code)!r}"
                          for code, want in missing.items())
        msg = f"名称表内容锚点不匹配（半截/串表的典型症状）：{detail}（{where}）"
        if anchors:
            raise DataUnavailable(msg)
        # ★★ H2（code-review，HIGH）：**刷新（自救）路径不得被内容锚点锁死**。
        #   锚点票是**会合法改名的普通公司**（`603009`、`600519`、`000001`）——
        #   一旦其中一只改名，就变成：① 旧缓存校验失败 → 搜索报错；
        #   ② 用户照提示点「刷新名称表」，新抓的表**结构完好但锚点不符** → 抛错、**不落盘**
        #      ⇒ **连自救路径也死掉，只能改代码才能恢复** ✗
        #   所以刷新路径只做「结构 + 行数」硬校验；锚点不符**只告警、仍然落盘**（用户至少能恢复可用）。
        #   ★ 读路径（`load_name_table`）仍然 `anchors=True` 硬拒 —— 守卫的鉴别力不变。
        logger.warning(
            "名称表内容锚点不匹配，但这是**刷新路径** ⇒ 按 H2 设计只告警、仍落盘：%s", msg
        )


def _name_table_from_spot(cfg: AppConfig) -> list[dict]:
    """从 **夹具 spot**（`data/fixture/spot.json`，实测 5572 行、含 `name`）构造只读兜底名称表。

    ★ **只读夹具、绝不联网**（T-V3-2：socket 计数必须为 0）；spot 缺失/不可解析 → `[]`。
    ★ 与 `datasource._spot_frame_raw` 同源口径：spot 的 `code` 列 + `name` 列。
    """
    fixture_dir = os.path.join(str(cfg.root), "data", "fixture")
    entry = DataSource._read_entry(fixture_dir, "spot")
    if not isinstance(entry, dict):
        return []
    payload = entry.get("payload")
    raw = payload.get("rows") if isinstance(payload, dict) and "rows" in payload else payload
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = _norm_code(row.get("code"))
        name = str(row.get("name") or "").strip()
        if len(code) == 6 and code.isdigit() and name:
            out.append({"code": code, "name": name})
    return out


def _peer_already_refreshed(entry: dict | None, called_at: float) -> list[dict] | None:
    """若缓存的 ``fetched_at`` **晚于** ``called_at``（= 等锁期间**别的并发请求**刚写进去的），返回它的行；否则 ``None``。

    ## 为什么这么判（而不是「冷却窗口」）

    「冷却窗口」（例如 120 秒内重复刷新直接返回缓存）会让**用户主动点的刷新**也**不联网**
    ⇒ 用户点了「强制刷新」却没真刷 ⇒ **语义说谎**（本项目反复防的那类问题）。

    这里只在**单飞锁内**、且缓存**晚于本次调用进入函数的时刻**时才复用 ——
    那必然是「同时发生的另一路请求已经替我抓完了」。⇒
    **并发请求共享一次取数（单飞成立），而任何一次主动刷新都真联网。**
    """
    if not isinstance(entry, dict):
        return None
    rows = entry.get("payload")
    if not isinstance(rows, list) or not rows:
        return None
    stamp = entry.get("fetched_at")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        from datetime import datetime, timezone

        when = datetime.fromisoformat(stamp)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        ts = when.timestamp()
    except Exception:  # noqa: BLE001 - 时间戳解析失败 ⇒ 保守地「不算别人刷过」
        return None
    # ★ 严格晚于本次调用时刻才算「别人替我刷了」；留 1ms 容差给同毫秒的边界。
    return rows if ts > called_at + 0.001 else None


def refresh_name_table(cfg: AppConfig, *, offline: bool) -> dict:
    """**强制刷新**名称表（A1 / T-V3-3；`/api/name/refresh` 的后端）。

    * ``offline=True`` → **拒绝**（明确错误、**绝不联网**）；
    * 联网抓取 → **行数 + 内容锚点都要过**（防止又抓回半截表）→ 落盘；
    * 返回 ``{"rows", "fetched_at", "path"}`` 供 UI 展示。
    """
    if offline:
        raise DataUnavailable(
            "离线模式不允许刷新名称表（--offline 下不发起任何网络请求）；"
            "请联网后重试，或解除离线勾选"
        )
    # ★ F2b：记下「本次调用进入函数的时刻」—— 锁内用它判断「等锁期间是否已有别的并发请求替我刷过」。
    #   ★ 不用冷却窗口（那会让用户主动刷新不联网 = 语义说谎）。
    import time as _time

    called_at = _time.time()
    # ★★ F2b（ultraqa MEDIUM）：**并发刷新必须共享一次取数（单飞）**。
    #   此前这里无条件 `fetch_name_table()` ⇒ 冷缓存下 N 路并发 = N 次上游抓取
    #   （实测单次真联网 ~23s，5 路并发就是 ~5×23s 的上游压力）。
    #   ★ **不加冷却窗口**：那会让「强制刷新」在语义上说谎（用户点了却没真刷）。
    #     单飞只合并**同时发生**的并发请求：拿锁的人去抓，其余人**等它抓完**、直接用刚写进缓存的那份。
    cache_dir = os.path.join(str(cfg.root), "data", "cache")
    flight = _single_flight_lock(f"{os.path.abspath(cache_dir)}::stock_list_refresh")
    with flight:
        # ★ 先看「等锁期间是否已有人刚刷新过」—— 是的话直接复用，不再抓第二次。
        #   判据：锁一进来就检查缓存是否「刚刚」被写过（距 now 在本次等待窗口内）。
        entry = DataSource._read_entry(cache_dir, "stock_list")
        already = _peer_already_refreshed(entry, called_at)
        if already:
            logger.info("刷新名称表：并发请求已被其它线程完成，直接复用（单飞生效）")
            return {
                "rows": len(already),
                "fetched_at": (entry or {}).get("fetched_at"),
                "path": cache_path(cfg),
                "shared": True,
            }
        rows = fetch_name_table()
        # ★★ H2：刷新（自救）路径**只做结构+行数硬校验**，内容锚点不符**只告警、仍落盘** ——
        #    否则锚点票合法改名会让「点刷新」这条自救路径也锁死（详见 `_validate_name_table` 注释）。
        _validate_name_table(
            rows, fetched_at="(just fetched)", source="fetch_name_table", anchors=False
        )
        DataSource._write_entry(cache_dir, "stock_list", rows)
        entry = DataSource._read_entry(cache_dir, "stock_list")
        return {
            "rows": len(rows),
            "fetched_at": (entry or {}).get("fetched_at"),
            "path": cache_path(cfg),
            "shared": False,
        }


def load_name_table(
    cfg: AppConfig, *, offline: bool, force_refresh: bool = False
) -> list[dict]:
    """返回 ``[{"code","name","is_margin","is_short"}]``（按 code 升序）。

    * 新鲜缓存（< TTL）或 ``force_refresh=False`` 下的过期缓存 → 直接用（**但要过守卫**）；
    * ``offline=True``：**只读缓存**；无缓存 → **spot 夹具兜底** → 都没有才 ``DataUnavailable``
      （**不得静默返回空**）；
    * ``offline=False``：缓存过期（或 ``force_refresh=True``）→ 联网刷新并落盘；
    * ★ **任何来源的行数 < ``NAME_TABLE_MIN_ROWS`` 或内容锚点不过 ⇒ ``DataUnavailable``**
      （不得把污染表当正常表用 —— 这正是「搜索没用」的真根因）。
    """
    cache_dir = os.path.join(str(cfg.root), "data", "cache")
    entry = DataSource._read_entry(cache_dir, "stock_list")
    fresh = entry is not None and not DataSource._expired(entry, DEFAULT_TTL_SECONDS)

    def _fallback_or_raise(reason: str) -> list[dict]:
        """名称表不可用时的兜底链：**spot 夹具**（只读、不联网）→ 仍不行则报错。"""
        spot_rows = _name_table_from_spot(cfg)
        if spot_rows:
            try:
                _validate_name_table(spot_rows, fetched_at="(fixture spot)", source="fixture/spot")
            except DataUnavailable:
                pass
            else:
                logger.warning("名称表不可用（%s）→ 已退回 **spot 夹具兜底**（%d 行，只读）",
                               reason, len(spot_rows))
                return spot_rows
        raise DataUnavailable(f"{reason}；且 spot 夹具兜底也不可用（{cache_path(cfg)}）")

    if fresh and not force_refresh:
        rows = entry["payload"]
    elif offline:
        if entry is None:
            rows = _fallback_or_raise(
                f"离线模式缺少股票名称表缓存（{cache_path(cfg)}）"
            )
        else:
            rows = entry["payload"]  # 离线只能用过期缓存（与 datasource._cached 口径一致）
    else:
        rows = fetch_name_table()
        if not rows:
            raise DataUnavailable("股票名称表为空表，拒绝写入缓存")
        if len(rows) < STOCK_LIST_MIN_ROWS:
            # INV-54：**不落盘**（残缺缓存会长期伪装成"搜不到"），但返回值仍交给调用方
            logger.warning(
                "名称表写入被拒：rows=%d < STOCK_LIST_MIN_ROWS=%d（不落盘）",
                len(rows), STOCK_LIST_MIN_ROWS,
            )
        else:
            DataSource._write_entry(cache_dir, "stock_list", rows)

    if not isinstance(rows, list) or not rows:
        raise DataUnavailable(f"股票名称表缓存内容非法（{cache_path(cfg)}）")
    # ★ 守卫：**损坏/污染 ⇒ 响亮报错**（不静默、也不拿兜底掩盖 —— 那会让用户继续搜不到新票）；
    #   只有「**缓存缺失**」才走 spot 夹具兜底（见上面 `_fallback_or_raise`）。
    _validate_name_table(
        rows,
        fetched_at=(entry or {}).get("fetched_at") if isinstance(entry, dict) else None,
        source="cache" if fresh else "cache(stale)" if offline else "fetched",
    )
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
