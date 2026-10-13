"""ptrade/watchlist.py — 自选池持久化与批量打分（PRD `prd-ptrade-web.md` §5.3，W2）。

* 落盘：``<root>/data/watchlist.json``（``schema_version: 1``，原子写委托 :mod:`ptrade.store`）。
* **INV-37**：``add_to_watchlist`` 幂等 —— 同 code 连续添加不产生重复项、不报错，重复添加
  只补全 ``name`` / ``note``（有值才覆盖）。
* **INV-38**：``score_watchlist`` 对**缺数据**的票标注「无数据」（``score=None`` +
  ``signal="no_data"`` + ``ok=False``），**绝不记 0 分**——0 分是合法的真实得分，两者必须可区分。

代码归一化复用 :func:`ptrade.datasource._norm_code`（与其余模块同一口径）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig
from .datasource import DataSource, _norm_code
from . import store

__all__ = [
    "FILE_NAME",
    "SCHEMA_VERSION",
    "watchlist_path",
    "load_watchlist",
    "save_watchlist",
    "add_to_watchlist",
    "remove_from_watchlist",
    "list_watchlist",
    "score_watchlist",
]

FILE_NAME = "watchlist.json"
SCHEMA_VERSION = 1


def watchlist_path(cfg: AppConfig) -> str:
    """自选池文件路径：``<root>/data/watchlist.json``（受 ``--root`` 影响）。"""
    return os.path.join(str(cfg.root), "data", FILE_NAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "updated_at": _now(), "items": []}


def _atomic_write_json(path: str, obj: Any) -> None:
    """原子写 JSON —— **委托** :func:`ptrade.store.atomic_write_json`。

    委托而非自实现的原因（Critic N-4）：``os.replace`` 在 Windows 上遇到
    「读者持有句柄」会 ``PermissionError errno=13``，实测 4 读者线程下 60 次写
    **失败 50 次**。重试逻辑只应存在于一处（``store``），这里不再各写一份。
    """
    store.atomic_write_json(path, obj)


def load_watchlist(cfg: AppConfig) -> dict:
    """读取自选池。

    * 文件不存在 → 空池（**不是**错误）；
    * 文件存在但损坏 / schema 不合法 → ``ValueError``（**不得静默当作空池**）。
    """
    path = watchlist_path(cfg)
    if not os.path.isfile(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(f"自选池文件损坏，无法解析：{path}（{exc}）") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"自选池文件格式非法（应为对象）：{path}")
    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"自选池 schema_version={version!r} 不受支持（期望 {SCHEMA_VERSION}）：{path}"
        )
    items = doc.get("items")
    if not isinstance(items, list):
        raise ValueError(f"自选池 items 字段非法（应为数组）：{path}")
    doc.setdefault("updated_at", "")
    return doc


def save_watchlist(cfg: AppConfig, doc: dict) -> dict:
    """原子写入自选池（补 ``updated_at``）。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "items": list(doc.get("items") or []),
    }
    _atomic_write_json(watchlist_path(cfg), payload)
    return payload


def list_watchlist(cfg: AppConfig) -> list[dict]:
    """自选池条目（按加入顺序）。"""
    return [dict(item) for item in load_watchlist(cfg).get("items", [])]


def add_to_watchlist(cfg: AppConfig, code: str, name: str = "", note: str = "") -> dict:
    """加入自选池（**幂等**，INV-37）。返回写入后的完整文档。

    **整个「读 → 改 → 写」在写锁内**（code-review H1）：此前这里是裸的
    read-modify-write，两个进程并发 add 会互相覆盖（实测 2 进程各 add 30 个不同
    code → 期望 60，实际 32/32/31）。锁**下沉到数据模块**内，而不是靠调用方自觉 ——
    webapp 曾记得在调用点加锁，新加的 CLI 子命令就忘了，同一个缺陷复发一次。
    外层若已持锁，这里是**可重入**的，不产生额外开销。
    """
    target = _norm_code(code)
    if not target:
        raise ValueError("code 不能为空")
    with store.state_lock(cfg.root, timeout=10.0):
        return _add_locked(cfg, target, name, note)


def _add_locked(cfg: AppConfig, target: str, name: str, note: str) -> dict:
    doc = load_watchlist(cfg)
    items = list(doc.get("items") or [])
    for item in items:
        if _norm_code(item.get("code")) == target:
            if name:
                item["name"] = str(name)
            if note:
                item["note"] = str(note)
            return save_watchlist(cfg, {"items": items})
    items.append(
        {
            "code": target,
            "name": str(name or ""),
            "note": str(note or ""),
            "added_at": _now(),
        }
    )
    return save_watchlist(cfg, {"items": items})


def remove_from_watchlist(cfg: AppConfig, code: str) -> dict:
    """移出自选池（**不存在不报错**）。返回写入后的完整文档。

    **整个「读 → 改 → 写」在写锁内**（code-review H1）—— 与 add 同一理由。
    """
    target = _norm_code(code)
    with store.state_lock(cfg.root, timeout=10.0):
        return _remove_locked(cfg, target)


def _remove_locked(cfg: AppConfig, target: str) -> dict:
    doc = load_watchlist(cfg)
    items = [
        item for item in (doc.get("items") or []) if _norm_code(item.get("code")) != target
    ]
    return save_watchlist(cfg, {"items": items})


def _has_market_data(ds: DataSource, code: str, asof: str | None) -> bool:
    """该票在 ``asof`` 是否有真实行情（决定「无数据」还是「中性 50 分」）。

    为什么不能只看策略是否抛异常：`strategy` 对**缺数据**的票走「七因子各 50 分（中性）」路径，
    返回一个**看起来正常**的总分（实测 50.24），**不抛异常**。若不做这一步，缺数据的票会被
    当成真实分数混进榜单 —— 正是 INV-38 要禁止的。
    """
    try:
        quote = ds.quote(code, asof) if asof else None
    except Exception:  # noqa: BLE001 - 取不到行情即视为无数据
        return False
    if not isinstance(quote, dict):
        return False
    last = quote.get("last")
    try:
        return last is not None and float(last) > 0
    except (TypeError, ValueError):
        return False


def score_watchlist(
    cfg: AppConfig, *, offline: bool = True, asof: str | None = None
) -> list[dict]:
    """对自选池逐票跑 CANSLIM 七因子（INV-38：缺数据的票标注「无数据」，**不记 0 分**）。

    返回按「有分优先、分数降序」排序的列表，每项::

        {"code","name","ok","score","signal","reason","factors"}

    * ``ok=True``  → ``score`` 为真实总分（**可能真的就是 0.0**）；
    * ``ok=False`` → ``score=None``、``signal="no_data"``、``reason="无数据：…"``。
    """
    items = list_watchlist(cfg)
    if not items:
        return []

    from . import strategy  # 延迟导入：strategy 未就绪时不影响自选池增删查

    ds = DataSource(cfg, offline=offline)
    if not asof:
        try:
            asof = ds.latest_trade_date()
        except Exception:  # noqa: BLE001 - 无交易日历时交给 strategy 报错
            asof = None
    scorer = strategy.CanslimScorer(cfg, ds, offline=offline)

    results: list[dict] = []
    for item in items:
        code = _norm_code(item.get("code"))
        label = str(item.get("name") or code)

        if not _has_market_data(ds, code, asof):
            results.append(
                {
                    "code": code,
                    "name": label,
                    "ok": False,
                    "score": None,
                    "signal": "no_data",
                    "reason": f"无数据：{code} 在 {asof or '最近交易日'} 无行情数据",
                    "factors": {},
                }
            )
            continue

        try:
            scored = scorer.score(code, asof)
        except Exception as exc:  # noqa: BLE001 - 缺数据必须降级为「无数据」而不是 0 分
            results.append(
                {
                    "code": code,
                    "name": label,
                    "ok": False,
                    "score": None,
                    "signal": "no_data",
                    "reason": f"无数据：{type(exc).__name__}（{exc}）",
                    "factors": {},
                }
            )
            continue
        results.append(
            {
                "code": code,
                "name": str(getattr(scored, "name", "") or label),
                "ok": True,
                "score": float(scored.total),
                "signal": str(getattr(scored, "signal", "") or ""),
                "reason": "；".join(str(r) for r in (getattr(scored, "reasons", None) or [])[:2]),
                "factors": {str(f.key): float(f.score) for f in (scored.factors or [])},
            }
        )

    # 有分在前（降序）；无数据置底但保留可见 —— 绝不把「无数据」混入 0 分档
    results.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0.0)))
    return results
