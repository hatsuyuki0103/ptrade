"""ptrade.store —— 数据根写入的跨进程互斥 + 原子落盘（PRD §5.1 / INV-32..34）。

网页端与 CLI 会**并发写**同一数据根的 ``data/state.json`` 与 ``data/ledger.jsonl``，
因此所有写路径都必须经由本模块：

* :func:`state_lock` —— 跨进程 + 跨线程写锁，**同进程同线程可重入**；
* :func:`atomic_write_text` / :func:`atomic_write_json` —— 临时文件 → ``fsync`` → ``os.replace``
  （整文件替换，不存在部分写入的中间态）；
* :func:`append_jsonl` —— 单次 ``write`` 追加一行（行级原子，供 ledger 使用）；
* :func:`read_state` —— **不取写锁**的只读路径，容忍读取瞬间的重命名（重试一次）；
* :func:`save_state` —— 原子写账户状态（``allow_nan=False``，NaN 一律拒绝），并把 ``source``
  记入 ledger 之外的审计流 ``data/state_audit.jsonl``；
* :func:`locked_update` —— ``读 → mutate → 原子写`` 的临界区，**所有写命令必须走这里**。

锁后端（自动选择，均失败时退化）：

1. Windows ``msvcrt.locking``（对 ``.ptrade.lock`` 的第 0 字节加锁）；
2. POSIX ``fcntl.flock``；
3. 退化方案：``O_CREAT|O_EXCL`` 创建 ``.ptrade.lock.excl`` + 过期回收
   （超过 :data:`STALE_LOCK_SECONDS` 视为持有者已崩溃）。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from . import core
from .config import AppConfig
from .core import AccountState

try:  # pragma: no cover - 平台分支
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore

try:  # pragma: no cover - 平台分支
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

__all__ = [
    "LOCK_FILE_NAME",
    "STATE_LOCK_ERROR_CODE",
    "STATE_WRITE_ERROR_CODE",
    "WRITE_RETRY_BUDGET_SECONDS",
    "READ_RETRY_BUDGET_SECONDS",
    "StateLockTimeout",
    "StateWriteTimeout",
    "cleanup_stale",
    "state_lock",
    "lock_path",
    "atomic_write_text",
    "atomic_write_json",
    "append_jsonl",
    "read_state",
    "save_state",
    "check_ledger_state_consistency",
    "locked_update",
    "NO_WRITE",
    "state_path",
    "ledger_path",
    "audit_path",
]

LOCK_FILE_NAME = ".ptrade.lock"
EXCL_LOCK_SUFFIX = ".excl"
STATE_LOCK_ERROR_CODE = "STATE_LOCK_TIMEOUT"
STATE_WRITE_ERROR_CODE = "STATE_WRITE_TIMEOUT"

STATE_DIR = "data"
STATE_FILE = "state.json"
LEDGER_FILE = "ledger.jsonl"
AUDIT_FILE = "state_audit.jsonl"
TMP_PREFIX = ".ptrade.tmp-"

POLL_INTERVAL = 0.05  # 抢锁轮询间隔（秒）
STALE_LOCK_SECONDS = 60.0  # 退化锁文件的过期回收阈值（秒）

# 原子替换失败时的**写方**有界重试（B2）：Windows 上读者持句柄时 os.replace 会
# PermissionError(errno=13)，紧循环读者下失败率实测 24%~98.5% —— 不重试就会
# 「ledger 有单、state 无单」造成账实永久分叉。
WRITE_RETRY_BUDGET_SECONDS = 0.40  # 总预算（PRD §9.2 要求 200–500ms）
WRITE_RETRY_INTERVAL = 0.005  # 退避间隔（0.40 / 0.005 = 最多 80 次尝试）
# 读侧预算与写侧**对齐**（S11）：不得一侧重试、另一侧裸读
READ_RETRY_BUDGET_SECONDS = WRITE_RETRY_BUDGET_SECONDS
READ_RETRY_INTERVAL = WRITE_RETRY_INTERVAL

# 可重试的 errno：EACCES/PermissionError(13)、EAGAIN/EWOULDBLOCK(11)、EBUSY(16)、ETXTBSY(26)
RETRYABLE_ERRNOS = frozenset({11, 13, 16, 26})

# W-9：强杀进程会残留临时文件，超过该年龄的残留会被回收
TMP_MAX_AGE_SECONDS = 3600.0

# 当前可用的 OS 级后端：'msvcrt' | 'flock' | None（None → 退化到 O_EXCL 锁文件）
_OS_BACKEND: str | None = "msvcrt" if msvcrt is not None else ("flock" if fcntl is not None else None)


class StateWriteTimeout(OSError):
    """原子替换在预算内反复失败（B2）。``error_code == 'STATE_WRITE_TIMEOUT'``。

    继承 ``OSError`` 以兼容「按 OSError 捕获」的调用方；**绝不退回非原子写**。
    """

    error_code = "STATE_WRITE_TIMEOUT"

    def __init__(self, message: str, *, path: str | None = None, attempts: int = 0):
        super().__init__(message)
        self.path = path
        self.attempts = attempts


class StateLockTimeout(RuntimeError):
    """获取写锁超时。``error_code == 'STATE_LOCK_TIMEOUT'``。

    超时**绝不继续写**：调用方必须把它当成硬失败处理（HTTP 端建议映射为 503/409）。
    """

    error_code = STATE_LOCK_ERROR_CODE

    def __init__(self, message: str, *, root: str | None = None, timeout: float | None = None):
        super().__init__(message)
        self.root = root
        self.timeout = timeout


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _resolve_root(root_or_cfg: Any) -> str:
    """接受 ``cfg``（有 ``.root``）或数据根路径（W-1：两种写法都支持）。"""
    candidate = getattr(root_or_cfg, "root", None)
    if candidate is None:
        candidate = root_or_cfg
    return os.fspath(candidate)


def _normalize_root(root: str | os.PathLike[str]) -> str:
    """数据根归一化（绝对路径 + Windows 大小写归一），作为锁注册表键。"""
    return os.path.normcase(os.path.abspath(_resolve_root(root)))


def lock_path(root: str | os.PathLike[str] | Any) -> str:
    return os.path.join(_resolve_root(root), STATE_DIR, LOCK_FILE_NAME)


def state_path(cfg: AppConfig) -> str:
    """``<root>/data/state.json``（与 ``cli._state_path`` 完全一致）。"""
    return os.path.join(cfg.root, STATE_DIR, STATE_FILE)


def ledger_path(cfg: AppConfig) -> str:
    return os.path.join(cfg.root, STATE_DIR, LEDGER_FILE)


def audit_path(cfg: AppConfig) -> str:
    """写操作审计流（**不是**成交流水 ledger）。"""
    return os.path.join(cfg.root, STATE_DIR, AUDIT_FILE)


# --------------------------------------------------------------------------- #
# 锁：进程内注册表 + OS 级 fd 锁
# --------------------------------------------------------------------------- #
class _RootLock:
    """单数据根的锁状态：线程级 RLock（可重入 + 跨线程互斥）+ OS 级 fd 锁。"""

    __slots__ = ("key", "thread_lock", "depth", "fd", "mode", "excl_file")

    def __init__(self, key: str) -> None:
        self.key = key
        self.thread_lock = threading.RLock()
        self.depth: dict[int, int] = {}
        self.fd: int | None = None
        self.mode: str = ""
        self.excl_file: str = ""


_REGISTRY: dict[str, _RootLock] = {}
_REGISTRY_GUARD = threading.Lock()


def _registry_lock(key: str) -> _RootLock:
    with _REGISTRY_GUARD:
        lock = _REGISTRY.get(key)
        if lock is None:
            lock = _RootLock(key)
            _REGISTRY[key] = lock
        return lock


def _reset_registry_after_fork() -> None:  # pragma: no cover - POSIX fork 保护
    """fork 后子进程不继承父进程的锁状态（否则会因继承的 RLock 误判为"已持有"）。

    OS 级 fd 锁仍由父进程持有（flock 跟随 open file description），子进程重新打开后照样会冲突。
    """
    global _REGISTRY_GUARD
    _REGISTRY.clear()
    _REGISTRY_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):  # pragma: no cover - 平台分支
    os.register_at_fork(after_in_child=_reset_registry_after_fork)


def _ensure_lock_file(path: str) -> int:
    """打开（必要时创建并写入 1 字节）锁文件，返回 fd。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.fsync(fd)
    return fd


def _try_os_lock(fd: int) -> bool:
    """非阻塞尝试加锁；已被占用返回 False。"""
    if _OS_BACKEND == "msvcrt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[union-attr]
            return True
        except OSError:
            return False
    if _OS_BACKEND == "flock":
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
            return True
        except OSError:
            return False
    return False


def _unlock_os(fd: int) -> None:
    with contextlib.suppress(OSError):
        if _OS_BACKEND == "msvcrt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
        elif _OS_BACKEND == "flock":
            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]


def _write_owner_hint(fd: int) -> None:
    """把 pid/时间写在第 1 字节之后，便于诊断（第 0 字节是锁区，不动它）。"""
    with contextlib.suppress(OSError):
        payload = f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}\n".encode("utf-8")
        os.lseek(fd, 1, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)


def _reclaim_stale(path: str) -> bool:
    """过期的退化锁文件回收（持有者崩溃后遗留）。"""
    with contextlib.suppress(OSError):
        if time.time() - os.path.getmtime(path) > STALE_LOCK_SECONDS:
            os.unlink(path)
            return True
    return False


def _acquire_os(lock: _RootLock, path: str, deadline: float, timeout: float) -> None:
    if _OS_BACKEND is None:
        _acquire_excl(lock, path + EXCL_LOCK_SUFFIX, deadline, timeout)
        return
    fd = _ensure_lock_file(path)
    while True:
        if _try_os_lock(fd):
            _write_owner_hint(fd)
            lock.fd = fd
            lock.mode = _OS_BACKEND
            return
        if time.monotonic() >= deadline:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise StateLockTimeout(
                f"获取写锁超时（{timeout}s，后端 {_OS_BACKEND}）：{path} 被其他进程持有",
                root=os.path.dirname(os.path.dirname(path)),
                timeout=timeout,
            )
        time.sleep(POLL_INTERVAL)


def _acquire_excl(lock: _RootLock, path: str, deadline: float, timeout: float) -> None:
    """退化方案：``O_CREAT|O_EXCL`` 锁文件（排他创建即持锁），支持过期回收。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with contextlib.suppress(OSError):
                os.write(fd, f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}\n".encode())
                os.fsync(fd)
            os.close(fd)
            lock.mode = "excl"
            lock.excl_file = path
            return
        except FileExistsError:
            if _reclaim_stale(path):
                continue
            if time.monotonic() >= deadline:
                raise StateLockTimeout(
                    f"获取写锁超时（{timeout}s，后端 O_EXCL）：{path} 被其他进程持有",
                    root=os.path.dirname(os.path.dirname(path)),
                    timeout=timeout,
                )
            time.sleep(POLL_INTERVAL)


def _release_os(lock: _RootLock) -> None:
    if lock.mode == "excl":
        with contextlib.suppress(OSError):
            os.unlink(lock.excl_file)
        lock.mode = ""
        lock.excl_file = ""
        return
    if lock.fd is not None:
        _unlock_os(lock.fd)
        with contextlib.suppress(OSError):
            os.close(lock.fd)
        lock.fd = None
        lock.mode = ""


@contextmanager
def _locked_file(lock_file: str, timeout: float) -> Iterator[None]:
    """对**任意锁文件**加互斥锁（state 与 jsonl 追加共用同一套原语）。

    同进程同线程可重入（depth 计数）；跨线程经 ``thread_lock`` 串行；跨进程经 OS fd 锁。
    """
    key = os.path.normcase(os.path.abspath(lock_file))
    lock = _registry_lock(key)
    budget = max(0.0, float(timeout))
    deadline = time.monotonic() + budget
    ident = threading.get_ident()

    if not lock.thread_lock.acquire(timeout=budget):
        raise StateLockTimeout(
            f"获取写锁超时（{timeout}s）：同进程内其他线程正持有 {key}",
            root=key,
            timeout=timeout,
        )
    first = lock.depth.get(ident, 0) == 0
    lock.depth[ident] = lock.depth.get(ident, 0) + 1
    try:
        if first:
            cleanup_stale_artifacts(os.path.dirname(key))  # W-9：每次启动扫一次
            _acquire_os(lock, key, deadline, budget)
        yield
    finally:
        remaining = lock.depth.get(ident, 1) - 1
        if remaining <= 0:
            lock.depth.pop(ident, None)
            if first:
                _release_os(lock)
        else:
            lock.depth[ident] = remaining
        lock.thread_lock.release()


@contextmanager
def state_lock(root: str | os.PathLike[str] | Any, timeout: float = 10.0) -> Iterator[None]:
    """跨进程写锁（**同进程同线程可重入**；跨线程/跨进程互斥）。

    :param root: 数据根目录（锁文件 ``<root>/data/.ptrade.lock``）；也接受 ``cfg``（W-1）。
    :param timeout: 获取锁的总预算（秒）。超时抛 :class:`StateLockTimeout`，**绝不继续写**。
    """
    with _locked_file(lock_path(root), timeout):
        yield


# --------------------------------------------------------------------------- #
# 原子写
# --------------------------------------------------------------------------- #
def _fsync_dir(directory: str) -> None:
    """目录 fsync（POSIX 持久化目录项；Windows 不支持则忽略）。"""
    with contextlib.suppress(OSError, AttributeError, NotImplementedError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _is_retryable_replace_error(exc: OSError) -> bool:
    """替换失败是否可重试（读者持句柄 / 瞬时占用）。"""
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "errno", None) in RETRYABLE_ERRNOS


def _replace_with_retry(src: str, dst: str) -> None:
    """``os.replace`` + **有界重试**（B2）。

    仅重试可重试错误（``PermissionError``/errno 11/13/16/26）；预算耗尽抛
    :class:`StateWriteTimeout`。**绝不退回非原子写**——失败就失败，绝不假装成功。
    """
    deadline = time.monotonic() + WRITE_RETRY_BUDGET_SECONDS
    attempts = 0
    while True:
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if not _is_retryable_replace_error(exc):
                raise
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateWriteTimeout(
                    f"原子替换在预算 {WRITE_RETRY_BUDGET_SECONDS * 1000:.0f}ms 内失败 "
                    f"（重试 {attempts} 次）：{dst} 长期被占用（{exc.__class__.__name__}: {exc}）",
                    path=dst,
                    attempts=attempts,
                ) from exc
            time.sleep(min(WRITE_RETRY_INTERVAL, remaining))


def atomic_write_text(path: str, text: str) -> None:
    """写临时文件 → ``fsync`` → ``os.replace``（同盘原子替换，INV-25）。

    替换阶段带**有界重试**（B2）：并发读者持句柄导致的瞬时 ``PermissionError`` 会被吸收；
    预算耗尽则抛 :class:`StateWriteTimeout`，**不落半截、不退化为非原子写**。
    任何异常（含注入的异常）都会清理临时文件并**不触碰**目标文件原有内容。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    cleanup_stale_artifacts(directory)
    fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
        tmp = ""  # 已被 replace 消费，不再删除
    except BaseException:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise
    _fsync_dir(directory)


_CLEANED_DIRS: set[str] = set()
_CLEANED_GUARD = threading.Lock()


def cleanup_stale_artifacts(directory: str, *, max_age_seconds: float = TMP_MAX_AGE_SECONDS) -> list[str]:
    """回收强杀进程留下的残留（W-9）：过期 ``.ptrade-tmp-*`` 与过期退化锁文件。

    每个目录**每进程只扫一次**（锁与写路径热调用，避免反复 listdir）。
    注意：**永不删除** ``.ptrade.lock`` 本体 —— 它是常驻锁文件，删掉会让两个进程
    各自锁到不同 inode，互斥失效。
    """
    key = os.path.abspath(directory)
    with _CLEANED_GUARD:
        if key in _CLEANED_DIRS:
            return []
        _CLEANED_DIRS.add(key)
    removed: list[str] = []
    with contextlib.suppress(OSError):
        now = time.time()
        for name in os.listdir(key):
            if not (name.startswith(TMP_PREFIX) or name == LOCK_FILE_NAME + EXCL_LOCK_SUFFIX):
                continue
            path = os.path.join(key, name)
            with contextlib.suppress(OSError):
                if now - os.path.getmtime(path) > max_age_seconds:
                    os.unlink(path)
                    removed.append(path)
    return removed


def cleanup_stale(root_or_cfg: Any, *, max_age_seconds: float = TMP_MAX_AGE_SECONDS) -> list[str]:
    """公开入口：启动时回收某数据根的陈旧残留（供服务启动/维护脚本调用）。"""
    directory = os.path.join(_resolve_root(root_or_cfg), STATE_DIR)
    return cleanup_stale_artifacts(directory, max_age_seconds=max_age_seconds)


def atomic_write_json(path: str, obj: Any, *, indent: int = 2) -> None:
    """``json.dumps(obj, allow_nan=False)`` + :func:`atomic_write_text`。

    NaN/Infinity 一律抛 ``ValueError``（绝不落下非法 JSON）。
    """
    text = json.dumps(obj, ensure_ascii=False, indent=indent, allow_nan=False)
    atomic_write_text(path, text + "\n")


def _append_lock_file(path: str) -> str:
    """jsonl 追加所用的锁文件：数据目录内复用常驻 ``.ptrade.lock``，否则用 ``<path>.lock``。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if os.path.basename(directory) == STATE_DIR:
        return os.path.join(directory, LOCK_FILE_NAME)
    return path + ".lock"


def append_jsonl(path: str, obj: Any, *, timeout: float = 10.0) -> None:
    """追加一行（单次 ``write`` + ``flush`` + ``fsync``），跨进程**不丢行**。

    为什么必须加锁：Windows 的 CRT ``O_APPEND`` 是「seek 到末尾 + write」两步，
    多进程并发会互相覆盖（实测 3 进程 × 60 行丢 3 行）。锁取该文件所在数据根的常驻锁
    （``<dir>/.ptrade.lock``）—— 在 ``locked_update`` 临界区内调用时可重入，无额外开销。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    with _locked_file(_append_lock_file(path), timeout):
        os.makedirs(directory, exist_ok=True)
        line = json.dumps(obj, ensure_ascii=False, allow_nan=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


# --------------------------------------------------------------------------- #
# 状态读写
# --------------------------------------------------------------------------- #
def _load_json_file(path: str) -> Any:
    """读取并解析一个 JSON 文件（单次尝试；重试策略在 :func:`_read_json_with_retry`）。"""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_json_with_retry(path: str, budget: float, interval: float) -> Any:
    """读侧有界重试（S11：预算与写侧**对齐**）。

    容忍读取瞬间的 ``os.replace``（Windows 上是瞬时 ``PermissionError``）；
    文件不存在直接抛 ``FileNotFoundError``（由调用方转成 ``None``）。
    """
    deadline = time.monotonic() + budget
    while True:
        try:
            return _load_json_file(path)
        except FileNotFoundError:
            raise
        except (json.JSONDecodeError, PermissionError, OSError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(interval, remaining))


def read_state(cfg: AppConfig) -> AccountState | None:
    """读账户状态；文件不存在返回 ``None``（INV-26：**不获取写锁**）。

    读侧重试预算与写侧一致（``READ_RETRY_BUDGET_SECONDS``）。
    """
    path = state_path(cfg)
    try:
        doc = _read_json_with_retry(path, READ_RETRY_BUDGET_SECONDS, READ_RETRY_INTERVAL)
    except FileNotFoundError:
        return None
    return core.state_from_document(doc, cfg)


def _append_audit(cfg: AppConfig, record: dict[str, Any]) -> None:
    """写操作审计（best-effort：审计失败不影响已经落盘的 state）。"""
    with contextlib.suppress(OSError, ValueError):
        append_jsonl(audit_path(cfg), record)


def _count_jsonl_lines(path: str) -> int | None:
    """数 ``.jsonl`` 的非空行数；文件不存在返回 0，读不了返回 ``None``（不谎报）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def check_ledger_state_consistency(cfg: AppConfig, *, timeout: float = 2.0) -> dict[str, Any]:
    """检出「ledger 有单、state 无单」的分叉（**B3：失败原子性可检出**）。

    写一单的时序是「先 ``append_jsonl`` 落 ledger，再原子写 state」。若 state 落盘失败
    （重试预算耗尽、进程被杀），ledger 已多一行而 state 仍是旧快照 —— **账实分叉**，
    且用户重试会导致重复成交。INV-25 只管 state 单文件、INV-33 只管写到哪，
    **没有任何一条覆盖这一对文件**，所以这里用 state 文档的 ``app.ledger_lines``
    水位与实际 ledger 行数比对。

    **在写锁内读取**（code-review M4）：不取锁时，两侧会基于**不同时刻**的快照，
    事务进行中必然误报 ``diverged=True``（实测：事务中 ``{ledger_lines:1, recorded:0}``，
    事务后一致）。这也是 M3 能安全接入 ``status``/``health`` 的前提 —— **顺序不能反**，
    否则用户会看到随机告警。锁是**可重入**的，故在 ``locked_update`` 内调用不会自锁。

    ``timeout`` 故意**短**（默认 2s）：本函数服务于**只读**命令，不该被一个长写事务
    拖住。拿不到锁时返回 ``diverged=None``（不可判定），而不是排队等待或误报。

    返回 ``{"consistent", "ledger_lines", "recorded", "diverged", "reason"?}``。
    ``diverged=False`` 表示一致；``diverged=None`` 表示**无法判定**（任一侧不可读，
    或拿不到锁）—— 此时 ``consistent=False``，**不得**把「不知道」当成「没问题」。

    **只报告，不改数据**：修复入口由调用方决定（例如提示用户跑 ``init --reset``
    或在确认无未落账成交后重建 state）。
    """
    try:
        with state_lock(cfg.root, timeout=timeout):
            return _check_consistency_locked(cfg)
    except StateLockTimeout:
        return {
            "consistent": False,
            "ledger_lines": -1,
            "recorded": None,
            "diverged": None,
            "reason": f"无法判定（{timeout:g}s 内未取得写锁：有写事务在进行，请稍后重试）",
        }


def _check_consistency_locked(cfg: AppConfig) -> dict[str, Any]:
    """``check_ledger_state_consistency`` 的锁内实现（调用方已持锁）。"""
    ledger_lines = _count_jsonl_lines(ledger_path(cfg))
    recorded: int | None = None
    path = state_path(cfg)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            app = doc.get("app") if isinstance(doc, dict) else None
            if isinstance(app, dict) and isinstance(app.get("ledger_lines"), int):
                recorded = app["ledger_lines"]
        except (OSError, ValueError):
            recorded = None
    if ledger_lines is None or recorded is None:
        return {
            "consistent": False,
            "ledger_lines": ledger_lines if ledger_lines is not None else -1,
            "recorded": recorded,
            "diverged": None,
            "reason": "无法判定（ledger 或 state 不可读，或其缺少 ledger_lines 水位）",
        }
    diverged = ledger_lines != recorded
    return {
        "consistent": not diverged,
        "ledger_lines": ledger_lines,
        "recorded": recorded,
        "diverged": diverged,
    }


def save_state(cfg: AppConfig, state: AccountState, *, source: str = "cli") -> None:
    """原子写账户状态（``<root>/data/state.json``，schema_version=3）。

    * ``json.dumps(allow_nan=False)`` —— 状态里出现 NaN/Infinity 直接 ``ValueError``；
    * ``source`` 记入 ``<root>/data/state_audit.jsonl``（**不改变 state schema**，也不是成交流水）；
    * ``app.ledger_lines`` 记录落盘时刻的 ledger 行数（**水位**），供
      :func:`check_ledger_state_consistency` 检出 ledger/state 分叉（B3）。
      **只在 ``app`` 段追加字段**，不动 ``state`` 段 —— 后者受 ``core.restore`` 的严格校验。
    """
    path = state_path(cfg)
    doc = core.state_document(state)
    ledger_lines = _count_jsonl_lines(ledger_path(cfg))
    if isinstance(doc.get("app"), dict) and ledger_lines is not None:
        doc["app"]["ledger_lines"] = ledger_lines
    atomic_write_json(path, doc)
    _append_audit(
        cfg,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "save_state",
            "source": str(source),
            "pid": os.getpid(),
            "trade_date": state.trade_date,
            "net_assets": round(float(state.net_assets), 6),
            "ledger_lines": ledger_lines,
        },
    )


class _NoWrite:
    """哨兵：``mutate`` 返回它表示「本次**明确不落盘**」。

    为什么需要它（code-review H2，Lead 已复现）：原契约是「返回值不是 AccountState
    → 保存被就地修改的入参状态」，于是 ``return None`` 被解释成「写回入参」——
    **错误路径（本意是别写）仍然落盘**。而 ``save_state`` 会顺手把
    ``app.ledger_lines`` 水位刷成当前 ledger 行数，于是一次**失败**的 ``init``
    就能把已经检出的账实分叉信号抹掉（复现：``diverged`` 由 True 变 False）。

    要表达「别写」需要**显式信号**，不能靠「返回值长得不像 AccountState」来暗示。
    """

    __slots__ = ()


NO_WRITE = _NoWrite()


def locked_update(
    cfg: AppConfig,
    mutate: Callable[[AccountState | None], Any],
    *,
    source: str = "cli",
    timeout: float = 10.0,
) -> Any:
    """``读 → mutate(state) → 原子写`` 的临界区（INV-32）。

    契约（v2，code-review H2 后收紧）：

    * ``mutate`` 收到当前状态（账户未初始化时为 ``None``，便于实现 init 类操作）；
    * 返回 :class:`~ptrade.core.AccountState` → **保存它**；
    * 返回 :data:`NO_WRITE` → **明确不落盘**（连审计也不写）—— 错误/放弃路径用它；
    * 返回其它任何值（含 ``None``）→ 保存**被就地修改的入参状态**；
    * 返回值：``mutate`` 的返回值；但当返回值是 ``None`` 或 ``NO_WRITE`` 时，
      **返回实际被持久化的那个 AccountState**（供调用方继续使用）；
    * 超时由 :class:`StateLockTimeout` 抛出，**不会**继续写。
    """
    with state_lock(cfg.root, timeout=timeout):
        state = read_state(cfg)
        result = mutate(state)
        if result is NO_WRITE:
            return state
        target = result if isinstance(result, AccountState) else state
        if isinstance(target, AccountState):
            save_state(cfg, target, source=source)
        return result if result is not None else target
