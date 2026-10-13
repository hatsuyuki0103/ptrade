"""tests/test_store.py —— W1 地基：跨进程写锁 + 原子写（PRD §5.1，S1–S8）。

纪律（test-spec §0）：
* 每条验收谓词都配**非空守卫**——去掉守卫后必须变红（S2 丢更新 / S4 出现 JSONDecodeError）；
* 跨进程用例一律用 ``spawn`` 上下文（Windows 默认真实进程；POSIX 下避免 fork 继承锁状态）；
* 只读路径（S7）用"禁止取锁"的插桩证明 INV-34；
* 不联网（conftest 的 autouse socket 拦截守卫对本文件同样生效）。
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing as mp
import os
import pathlib
import threading
import time

import pytest

from ptrade import core, store
from ptrade.config import load_config

CTX = mp.get_context("spawn")
COUNTER_KEY = "__counter__"


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #
def make_cfg(tmp_path):
    return load_config(str(tmp_path))


def init_account(cfg) -> None:
    store.save_state(cfg, core.new_state(cfg), source="test")


def counter_of(cfg) -> float:
    state = store.read_state(cfg)
    if state is None:
        return 0.0
    return float((state.last_prices or {}).get(COUNTER_KEY, 0.0))


def join_all(procs, timeout=180):
    for proc in procs:
        proc.join(timeout)
    codes = [proc.exitcode for proc in procs]
    assert codes == [0] * len(procs), f"子进程异常退出: {codes}"


def drain(queue, count, timeout=30):
    return [queue.get(timeout=timeout) for _ in range(count)]


# --------------------------------------------------------------------------- #
# 子进程 worker（必须模块级：spawn 按限定名导入）
# --------------------------------------------------------------------------- #
def _patch_no_lock():
    """把 state_lock 换成"什么都不做"的上下文管理器（非空守卫专用）。"""

    @contextlib.contextmanager
    def _noop(root, timeout=10.0):
        yield

    store.state_lock = _noop


def _worker_increment(root, iterations, no_lock, sleep_in_critical, barrier, queue):
    from ptrade.config import load_config as _load

    if no_lock:
        _patch_no_lock()
    cfg = _load(root)
    barrier.wait()
    for _ in range(iterations):
        def mutate(state):
            value = float((state.last_prices or {}).get(COUNTER_KEY, 0.0))
            if sleep_in_critical:
                time.sleep(sleep_in_critical)  # 放大读-改-写窗口
            state.last_prices[COUNTER_KEY] = value + 1

        store.locked_update(cfg, mutate, source="test", timeout=60.0)
    queue.put("done")


def _worker_try_lock(root, timeout, queue, force_fallback=False):
    from ptrade.config import load_config as _load

    if force_fallback:
        store._OS_BACKEND = None  # 与父进程使用**同一**后端，否则锁的不是同一个文件
    _load(root)
    try:
        with store.state_lock(root, timeout=timeout):
            queue.put(("ACQUIRED", "", ""))
    except store.StateLockTimeout as exc:
        queue.put((type(exc).__name__, exc.error_code, str(exc)))
    except Exception as exc:  # noqa: BLE001 - 任何其它异常都要被看见
        queue.put((type(exc).__name__, "", str(exc)))


def _worker_hold_lock(root, hold_seconds, queue):
    with store.state_lock(root, timeout=30.0):
        queue.put("held")
        time.sleep(hold_seconds)


def _worker_atomic_writer(path, versions, barrier, queue):
    barrier.wait()
    try:
        for i in range(versions):
            store.atomic_write_json(path, {"version": i, "blob": "x" * 200000})
        queue.put("done")
    except BaseException as exc:  # noqa: BLE001 - 让父进程看见失败原因而不是空等
        queue.put(f"{type(exc).__name__}: {exc}")
        raise


def _worker_nonatomic_halfwrite(path, queue):
    """非原子写对照：先写半截并落盘，等读方确认后再补完（确定性制造半截文件）。"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"blob": "')
        fh.flush()
        os.fsync(fh.fileno())
    queue.put("half")
    queue.get(timeout=30)  # 等读方读完半截文件
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('tail"}')
    queue.put("done")


def _worker_append_lines(path, count, barrier, queue):
    barrier.wait()
    for i in range(count):
        store.append_jsonl(path, {"pid": os.getpid(), "i": i, "pad": "y" * 120})
    queue.put("done")


def _worker_tight_reader(path, stop_event, queue):
    """紧循环读文件（制造 B2 的 os.replace 争用）。"""
    reads = 0
    while not stop_event.is_set():
        try:
            with open(path, "rb") as fh:
                fh.read()
            reads += 1
        except OSError:
            pass
    queue.put(reads)


def _worker_excl_roundtrip(root, iterations, barrier, queue):
    """退化后端（O_EXCL 锁文件）跨进程互斥。"""
    import ptrade.store as store_mod

    store_mod._OS_BACKEND = None
    cfg = load_config(root)
    barrier.wait()
    for _ in range(iterations):
        def mutate(state):
            value = float((state.last_prices or {}).get(COUNTER_KEY, 0.0))
            time.sleep(0.01)
            state.last_prices[COUNTER_KEY] = value + 1

        store_mod.locked_update(cfg, mutate, source="test", timeout=60.0)
    queue.put("done")


# --------------------------------------------------------------------------- #
# S1 同进程可重入 / 跨线程互斥
# --------------------------------------------------------------------------- #
def test_S1_nested_state_lock_is_reentrant(tmp_path):
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    with store.state_lock(str(tmp_path), timeout=5.0):
        with store.state_lock(str(tmp_path), timeout=1.0):      # 嵌套不得自锁
            with store.state_lock(str(tmp_path), timeout=1.0):
                store.locked_update(cfg, lambda s: None, source="test", timeout=1.0)
    # 释放后仍可再次获取
    with store.state_lock(str(tmp_path), timeout=1.0):
        pass


def test_S1_other_thread_is_excluded(tmp_path):
    """同进程不同线程必须互斥（否则网页多线程会并发写）。"""
    outcome: dict[str, object] = {}

    def other_thread():
        try:
            with store.state_lock(str(tmp_path), timeout=0.3):
                outcome["entered"] = True
        except store.StateLockTimeout as exc:
            outcome["timeout"] = exc.error_code

    with store.state_lock(str(tmp_path), timeout=5.0):
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(10)
    assert outcome.get("entered") is None
    assert outcome.get("timeout") == "STATE_LOCK_TIMEOUT"


def test_S1_reentrant_depth_survives_until_outermost_exit(tmp_path):
    """内层退出后外层仍持锁 —— 其他线程依旧拿不到。"""
    outcome: dict[str, object] = {}
    with store.state_lock(str(tmp_path), timeout=5.0):
        with store.state_lock(str(tmp_path), timeout=1.0):
            pass  # 内层退出

        def other_thread():
            try:
                with store.state_lock(str(tmp_path), timeout=0.2):
                    outcome["entered"] = True
            except store.StateLockTimeout:
                outcome["timeout"] = True

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(10)
    assert outcome.get("entered") is None and outcome.get("timeout") is True


# --------------------------------------------------------------------------- #
# S2 跨进程互斥（+ 非空守卫：去掉锁必须丢更新）
# --------------------------------------------------------------------------- #
def test_S2_cross_process_locked_update_is_lossless(tmp_path):
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    workers, per_worker = 2, 15
    barrier = CTX.Barrier(workers)
    queue = CTX.Queue()
    procs = [
        CTX.Process(
            target=_worker_increment,
            args=(str(tmp_path), per_worker, False, 0.005, barrier, queue),
        )
        for _ in range(workers)
    ]
    for proc in procs:
        proc.start()
    join_all(procs)
    assert drain(queue, workers) == ["done"] * workers
    assert counter_of(cfg) == workers * per_worker


def test_S2_guard_without_lock_loses_updates(tmp_path):
    """**非空守卫**：短路 state_lock 后必须丢更新（证明 S2 的绿不是假的）。"""
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    workers, per_worker = 2, 8
    barrier = CTX.Barrier(workers)
    queue = CTX.Queue()
    procs = [
        CTX.Process(
            target=_worker_increment,
            args=(str(tmp_path), per_worker, True, 0.04, barrier, queue),
        )
        for _ in range(workers)
    ]
    for proc in procs:
        proc.start()
    join_all(procs)
    drain(queue, workers)
    total = workers * per_worker
    lost = counter_of(cfg)
    assert lost < total, f"去掉锁后仍然无丢失（{lost}/{total}）——说明 S2 是空守卫"


# --------------------------------------------------------------------------- #
# S3 锁超时
# --------------------------------------------------------------------------- #
def test_S3_lock_timeout_raises_state_lock_timeout(tmp_path):
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    before = store.state_path(cfg)
    before_text = open(before, encoding="utf-8").read()
    queue = CTX.Queue()
    with store.state_lock(str(tmp_path), timeout=5.0):
        proc = CTX.Process(target=_worker_try_lock, args=(str(tmp_path), 0.5, queue))
        proc.start()
        kind, code, message = queue.get(timeout=90)  # 持锁等待结论（spawn 启动约 0.3s）
    join_all([proc])
    assert kind == "StateLockTimeout", f"实际异常 {kind}: {message}"
    assert kind != "RuntimeError", "不得退化为泛 RuntimeError"
    assert code == "STATE_LOCK_TIMEOUT"
    # 超时后**绝不继续写**
    assert open(before, encoding="utf-8").read() == before_text


def test_S3_timeout_zero_when_lock_held_in_other_thread(tmp_path):
    """timeout=0 的边界：另一线程持锁时立即抛 StateLockTimeout（不空等）。"""
    errors: list[store.StateLockTimeout] = []
    done = threading.Event()

    def attempt():
        try:
            with store.state_lock(str(tmp_path), timeout=0.0):
                pass
        except store.StateLockTimeout as exc:
            errors.append(exc)
        finally:
            done.set()

    with store.state_lock(str(tmp_path), timeout=5.0):
        thread = threading.Thread(target=attempt)
        started = time.monotonic()
        thread.start()
        thread.join(10)
        elapsed = time.monotonic() - started
    assert done.is_set()
    assert len(errors) == 1, errors
    assert errors[0].error_code == "STATE_LOCK_TIMEOUT"
    assert elapsed < 5.0


def test_S3_locked_update_timeout_does_not_write(tmp_path):
    """locked_update 抢锁超时 → 抛错且**没有**任何写入。

    注意：同线程已持锁时按设计**可重入**，所以必须由**另一线程**持锁才能测到超时。
    """
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    before = open(store.state_path(cfg), encoding="utf-8").read()
    audit_before = os.path.getsize(store.audit_path(cfg))
    holding = threading.Event()
    release = threading.Event()

    def holder():
        with store.state_lock(str(tmp_path), timeout=10.0):
            holding.set()
            release.wait(30)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert holding.wait(10)
        with pytest.raises(store.StateLockTimeout):
            store.locked_update(cfg, lambda s: setattr(s, "available_cash", 1.0), timeout=0.2)
    finally:
        release.set()
        thread.join(10)
    assert open(store.state_path(cfg), encoding="utf-8").read() == before
    assert os.path.getsize(store.audit_path(cfg)) == audit_before


# --------------------------------------------------------------------------- #
# S4 原子写：整文件替换（+ 非空守卫：非原子写必须出现 JSONDecodeError）
# --------------------------------------------------------------------------- #
def test_S4_atomic_write_is_never_observed_partial(tmp_path):
    path = str(tmp_path / "big.json")
    store.atomic_write_json(path, {"version": -1, "blob": "x" * 200000})
    barrier = CTX.Barrier(1)
    queue = CTX.Queue()
    errors: list[str] = []
    stop = threading.Event()

    transient = []

    def reader():
        while not stop.is_set():
            try:
                with open(path, encoding="utf-8") as fh:
                    json.load(fh)
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                # Windows：读者持句柄与 os.replace 竞态 → 瞬时 PermissionError
                # （读侧重试是 PRD 规定行为，不算"读到半截"）
                transient.append(repr(exc))
            except json.JSONDecodeError as exc:
                errors.append(repr(exc))  # ← 只有解析失败才是"半截"
                return
            except OSError as exc:
                transient.append(repr(exc))

    threads = [threading.Thread(target=reader)]  # 1 个紧循环读者（PRD §9.2 的标定场景）
    proc = CTX.Process(target=_worker_atomic_writer, args=(path, 25, barrier, queue))
    try:
        for thread in threads:
            thread.start()
        proc.start()
        join_all([proc])
        assert queue.get(timeout=10) == "done"
    finally:
        stop.set()
        for thread in threads:
            thread.join(10)
    assert errors == [], f"原子写被读到半截: {errors[:3]}"
    # 最终内容是完整合法 JSON
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["version"] == 24


def test_S4_guard_nonatomic_write_yields_jsondecodeerror(tmp_path):
    """**非空守卫**：同样的读法对"非原子写"必须抓到 JSONDecodeError。"""
    path = str(tmp_path / "guard.json")
    store.atomic_write_json(path, {"blob": "complete"})
    queue = CTX.Queue()
    proc = CTX.Process(target=_worker_nonatomic_halfwrite, args=(path, queue))
    proc.start()
    assert queue.get(timeout=30) == "half"
    with pytest.raises(json.JSONDecodeError):
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
    queue.put("ack")
    join_all([proc])
    assert queue.get(timeout=10) == "done"
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


# --------------------------------------------------------------------------- #
# S5 崩溃不留半截
# --------------------------------------------------------------------------- #
def test_S5_injected_failure_keeps_original_and_leaves_no_tmp(tmp_path, monkeypatch):
    path = tmp_path / "state_like.json"
    store.atomic_write_json(str(path), {"v": "original"})
    before = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure before os.replace")

    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(RuntimeError, match="injected"):
        store.atomic_write_json(str(path), {"v": "new"})
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_S5_failure_in_save_state_keeps_previous_state(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    before = open(store.state_path(cfg), encoding="utf-8").read()

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(store.os, "replace", boom)
    state = store.read_state(cfg)
    state.available_cash = 123.0
    with pytest.raises(RuntimeError):
        store.save_state(cfg, state, source="test")
    monkeypatch.undo()
    assert open(store.state_path(cfg), encoding="utf-8").read() == before
    assert [p.name for p in (tmp_path / "data").iterdir() if ".tmp" in p.name] == []


# --------------------------------------------------------------------------- #
# S6 往返 + NaN 拒绝
# --------------------------------------------------------------------------- #
def test_S6_save_read_roundtrip_matches_field_by_field(tmp_path):
    cfg = make_cfg(tmp_path)
    state = core.new_state(cfg)
    state = core.apply_fill(state, core.Order(
        order_id="O0001", ts="2026-10-09T15:00:00", trade_date="2026-10-09",
        code="000001", name="平安银行", side=core.Side.BUY.value, qty=1000, price=11.78,
    ), cfg.margin, cfg.fee)
    state = core.settle_t1(state, "2026-10-09")
    state = core.mark_to_market(state, {"000001": 12.0})
    store.save_state(cfg, state, source="test")

    back = store.read_state(cfg)
    assert back is not None
    assert core.snapshot(back) == core.snapshot(state)
    assert back.net_assets == pytest.approx(state.net_assets)

    doc = json.loads(open(store.state_path(cfg), encoding="utf-8").read())
    assert doc["schema_version"] == core.SCHEMA_VERSION
    assert set(doc.keys()) == {"schema_version", "app", "state"}


def test_S6_nan_is_rejected(tmp_path):
    cfg = make_cfg(tmp_path)
    state = core.new_state(cfg)
    state.available_cash = float("nan")
    with pytest.raises(ValueError):
        store.save_state(cfg, state, source="test")
    assert store.read_state(cfg) is None  # 没有落下任何文件
    assert not os.path.exists(store.state_path(cfg))
    data_dir = tmp_path / "data"
    leftovers = [p.name for p in data_dir.iterdir() if ".tmp" in p.name] if data_dir.is_dir() else []
    assert leftovers == []


def test_S6_infinity_is_rejected_by_atomic_write_json(tmp_path):
    target = tmp_path / "x.json"
    with pytest.raises(ValueError):
        store.atomic_write_json(str(target), {"a": float("inf")})
    with pytest.raises(ValueError):
        store.atomic_write_json(str(target), {"a": float("-inf")})
    assert not target.exists()


def test_S6_read_state_returns_none_when_missing(tmp_path):
    assert store.read_state(make_cfg(tmp_path)) is None


def test_S6_read_state_rejects_old_schema(tmp_path):
    cfg = make_cfg(tmp_path)
    path = store.state_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 1, "app": {}, "state": {}}, fh)
    with pytest.raises(ValueError):
        store.read_state(cfg)


# --------------------------------------------------------------------------- #
# S7 只读路径不取写锁（INV-34）
# --------------------------------------------------------------------------- #
def test_S7_read_state_never_calls_state_lock(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    init_account(cfg)

    def forbidden(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("只读路径不得获取写锁（INV-34）")

    monkeypatch.setattr(store, "state_lock", forbidden)
    state = store.read_state(cfg)
    assert state is not None


def test_S7_read_completes_while_lock_held_elsewhere(tmp_path):
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    queue = CTX.Queue()
    proc = CTX.Process(target=_worker_hold_lock, args=(str(tmp_path), 2.0, queue))
    proc.start()
    assert queue.get(timeout=30) == "held"
    started = time.monotonic()
    state = store.read_state(cfg)          # 他人持锁时读取不阻塞
    elapsed = time.monotonic() - started
    proc.join(60)
    assert state is not None and elapsed < 1.0


def test_S7_cli_status_reads_store_written_state(tmp_path):
    """互通性：store 写出的 state.json 必须能被 CLI `status --json --root` 直接读。"""
    import subprocess
    import sys

    cfg = make_cfg(tmp_path)
    init_account(cfg)
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "ptrade", "status", "--json", "--root", str(tmp_path)],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["data"]["initial_cash"] == 500000.0


# --------------------------------------------------------------------------- #
# S8 append_jsonl 行完整性
# --------------------------------------------------------------------------- #
def test_S8_concurrent_append_keeps_lines_intact(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    workers, per_worker = 3, 60
    barrier = CTX.Barrier(workers)
    queue = CTX.Queue()
    procs = [
        CTX.Process(target=_worker_append_lines, args=(path, per_worker, barrier, queue))
        for _ in range(workers)
    ]
    for proc in procs:
        proc.start()
    join_all(procs)
    drain(queue, workers)

    lines = open(path, encoding="utf-8").read().splitlines()
    assert len(lines) == workers * per_worker
    for line in lines:
        record = json.loads(line)          # 每行必须是完整合法 JSON（守卫：撕裂行会抛错）
        assert len(record["pad"]) == 120   # 守卫：内容不得被截断/拼接


# --------------------------------------------------------------------------- #
# 退化后端：O_EXCL 锁文件 + 过期回收
# --------------------------------------------------------------------------- #
def test_fallback_backend_excludes_across_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_OS_BACKEND", None)
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    queue = CTX.Queue()
    with store.state_lock(str(tmp_path), timeout=5.0):
        proc = CTX.Process(
            target=_worker_try_lock, args=(str(tmp_path), 0.5, queue, True)
        )
        proc.start()
        kind, code, _ = queue.get(timeout=90)
    join_all([proc])
    assert kind == "StateLockTimeout" and code == "STATE_LOCK_TIMEOUT"
    # 释放后锁文件必须被清掉
    assert not os.path.exists(store.lock_path(str(tmp_path)) + store.EXCL_LOCK_SUFFIX)


def test_fallback_backend_is_lossless_across_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_OS_BACKEND", None)
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    workers, per_worker = 2, 6
    barrier = CTX.Barrier(workers)
    queue = CTX.Queue()
    procs = [
        CTX.Process(target=_worker_excl_roundtrip, args=(str(tmp_path), per_worker, barrier, queue))
        for _ in range(workers)
    ]
    for proc in procs:
        proc.start()
    join_all(procs)
    drain(queue, workers)
    assert counter_of(cfg) == workers * per_worker


def test_fallback_backend_reclaims_stale_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_OS_BACKEND", None)
    excl = store.lock_path(str(tmp_path)) + store.EXCL_LOCK_SUFFIX
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    with open(excl, "w", encoding="utf-8") as fh:
        fh.write("pid=999999\n")
    stale = time.time() - (store.STALE_LOCK_SECONDS + 60)
    os.utime(excl, (stale, stale))
    with store.state_lock(str(tmp_path), timeout=5.0):
        assert os.path.exists(excl)          # 回收后由本进程持有
    assert not os.path.exists(excl)


# --------------------------------------------------------------------------- #
# locked_update 的契约细节
# --------------------------------------------------------------------------- #
def test_locked_update_supports_init_from_none(tmp_path):
    cfg = make_cfg(tmp_path)

    def mutate(state):
        assert state is None
        return core.new_state(cfg)

    result = store.locked_update(cfg, mutate, source="web", timeout=5.0)
    assert isinstance(result, core.AccountState)
    assert store.read_state(cfg) is not None


def test_locked_update_none_in_none_out_does_not_save(tmp_path):
    """lead 的 `cmd_init` 依赖这条：入参 None 且返回 None → **不落盘**。

    （未初始化的账户 + mutate 没有创建状态 → 不得凭空写出一个 state.json）
    """
    cfg = make_cfg(tmp_path)
    seen = {}

    def mutate(state):
        seen["state"] = state
        return None

    result = store.locked_update(cfg, mutate, source="cli", timeout=5.0)
    assert seen["state"] is None
    assert result is None
    assert store.read_state(cfg) is None
    assert not os.path.exists(store.state_path(cfg))


def test_locked_update_returns_mutate_result_and_persists_in_place_change(tmp_path):
    cfg = make_cfg(tmp_path)
    init_account(cfg)

    def mutate(state):
        state.available_cash = 424242.0
        return "sentinel"

    assert store.locked_update(cfg, mutate, timeout=5.0) == "sentinel"
    assert store.read_state(cfg).available_cash == pytest.approx(424242.0)


def test_locked_update_writes_audit_stream_not_ledger(tmp_path):
    """source 记入 ledger **之外**的审计流；ledger.jsonl 不被写操作触碰。"""
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    store.locked_update(cfg, lambda s: None, source="web", timeout=5.0)
    records = [json.loads(line) for line in open(store.audit_path(cfg), encoding="utf-8")]
    assert [r["source"] for r in records] == ["test", "web"]
    assert all(r["event"] == "save_state" for r in records)
    assert not os.path.exists(store.ledger_path(cfg))


def test_state_lock_timeout_error_code_contract():
    exc = store.StateLockTimeout("boom", root="x", timeout=1.0)
    assert exc.error_code == "STATE_LOCK_TIMEOUT"
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "boom"


# --------------------------------------------------------------------------- #
# B2（S9–S11）：原子写必须有**写方**有界重试
#   背景：Windows 上读者持句柄时 os.replace → PermissionError(errno=13)，
#   紧循环读者下失败率实测 24%~98.5%。engine.submit 先落 ledger 再写 state，
#   写失败 = 「ledger 有单、state 无单」→ 账实永久分叉。
# --------------------------------------------------------------------------- #
def _drive_writes_under_tight_reader(cfg, writes=200):
    """在紧循环读者进程存在时连写 N 次 save_state，返回 (失败列表, 读者读次数)。"""
    stop = CTX.Event()
    queue = CTX.Queue()
    reader = CTX.Process(target=_worker_tight_reader, args=(store.state_path(cfg), stop, queue))
    reader.start()
    failures: list[str] = []
    try:
        time.sleep(0.3)  # 让读者进入紧循环
        state = store.read_state(cfg)
        for i in range(writes):
            state.last_prices[COUNTER_KEY] = float(i)
            try:
                store.save_state(cfg, state, source="test")
            except Exception as exc:  # noqa: BLE001 - 失败要计数
                failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        stop.set()
        reader.join(60)
    reads = queue.get(timeout=15)
    return failures, reads


def test_S9_all_writes_succeed_under_tight_reader(tmp_path):
    """S9：1 个紧循环读者 + 写方 200 次 save_state → **成功率 100%**。"""
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    failures, reads = _drive_writes_under_tight_reader(cfg, writes=200)
    assert reads > 100, f"读者没有真的在紧循环（只读了 {reads} 次），本用例不成立"
    assert failures == [], f"{len(failures)}/200 次写失败（前 3 条）: {failures[:3]}"
    assert counter_of(cfg) == 199.0
    assert [p.name for p in (tmp_path / "data").iterdir() if ".tmp" in p.name] == []


def test_S9_guard_without_retry_fails_under_tight_reader(tmp_path, monkeypatch):
    """**非空守卫**：去掉写方重试（直接裸 os.replace）→ 必须出现 PermissionError。"""
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    monkeypatch.setattr(store, "_replace_with_retry", lambda src, dst: os.replace(src, dst))
    failures, reads = _drive_writes_under_tight_reader(cfg, writes=200)
    assert reads > 100, f"读者没有真的在紧循环（只读了 {reads} 次）"
    assert failures, "去掉重试后 200 次写竟然全部成功 —— 说明 S9 是空守卫"
    assert any("PermissionError" in item for item in failures), failures[:3]


def test_S10_retry_is_bounded_and_raises_explicit_error(tmp_path, monkeypatch):
    """S10：持续失败时在预算内放弃并抛明确异常；不退化为非原子写。"""
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    before = open(store.state_path(cfg), encoding="utf-8").read()
    attempts: list[int] = []

    def always_fail(src, dst, *args, **kwargs):
        attempts.append(1)
        raise PermissionError(13, "injected: file in use by reader")

    monkeypatch.setattr(store.os, "replace", always_fail)
    state = store.read_state(cfg)
    state.available_cash = 1.0
    started = time.monotonic()
    with pytest.raises(store.StateWriteTimeout) as excinfo:
        store.save_state(cfg, state, source="test")
    elapsed = time.monotonic() - started
    monkeypatch.undo()

    assert excinfo.value.error_code == "STATE_WRITE_TIMEOUT"
    assert isinstance(excinfo.value, OSError)  # 兼容按 OSError 捕获的调用方
    assert store.WRITE_RETRY_BUDGET_SECONDS <= 0.5, "预算必须 <= 500ms（PRD §9.2）"
    assert elapsed <= store.WRITE_RETRY_BUDGET_SECONDS + 0.25, f"耗时 {elapsed:.3f}s 超出预算"
    assert len(attempts) >= 2, "只尝试了一次 —— 没有重试"
    # 底线：绝不退回非原子写 → 原文件完好、无 tmp 残留
    assert open(store.state_path(cfg), encoding="utf-8").read() == before
    assert [p.name for p in (tmp_path / "data").iterdir() if ".tmp" in p.name] == []


def test_S11_read_and_write_retry_budgets_are_aligned():
    """S11：两侧预算一致，且都在 PRD 规定的 200–500ms 区间内。"""
    assert store.READ_RETRY_BUDGET_SECONDS == store.WRITE_RETRY_BUDGET_SECONDS
    assert store.READ_RETRY_INTERVAL == store.WRITE_RETRY_INTERVAL
    assert 0.2 <= store.WRITE_RETRY_BUDGET_SECONDS <= 0.5


def test_S11_read_state_retries_transient_permission_error(tmp_path, monkeypatch):
    """S11：读侧不是裸读 —— 瞬时 PermissionError 会被重试吸收。"""
    cfg = make_cfg(tmp_path)
    init_account(cfg)
    real = store._load_json_file
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(13, "transient rename race")
        return real(path)

    monkeypatch.setattr(store, "_load_json_file", flaky)
    state = store.read_state(cfg)
    assert state is not None
    assert calls["n"] == 4, f"期望重试 3 次后成功，实际尝试 {calls['n']} 次"


def test_S11_read_gives_up_within_the_same_budget(tmp_path, monkeypatch):
    """S11：读侧持续失败时也在同一预算内放弃（用满预算但不裸读、不无限重试）。"""
    cfg = make_cfg(tmp_path)
    init_account(cfg)

    def always(path):
        raise PermissionError(13, "persistent")

    monkeypatch.setattr(store, "_load_json_file", always)
    started = time.monotonic()
    with pytest.raises(PermissionError):
        store.read_state(cfg)
    elapsed = time.monotonic() - started
    assert elapsed <= store.READ_RETRY_BUDGET_SECONDS + 0.25
    assert elapsed >= store.READ_RETRY_BUDGET_SECONDS * 0.5, "读侧几乎没有等待 —— 疑似裸读"


# --------------------------------------------------------------------------- #
# W-9：启动清理陈旧残留
# --------------------------------------------------------------------------- #
def test_W9_cleanup_removes_only_stale_tmp_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    stale = data / (store.TMP_PREFIX + "stale")
    stale.write_text("x", encoding="utf-8")
    old = time.time() - (store.TMP_MAX_AGE_SECONDS + 60)
    os.utime(stale, (old, old))
    fresh = data / (store.TMP_PREFIX + "fresh")
    fresh.write_text("x", encoding="utf-8")

    removed = store.cleanup_stale_artifacts(str(data), max_age_seconds=store.TMP_MAX_AGE_SECONDS)
    assert str(stale) in removed and not stale.exists()
    assert fresh.exists(), "新鲜残留不得删除（可能是别的进程正在写的临时文件）"


def test_W9_cleanup_never_deletes_the_persistent_lock_file(tmp_path):
    """常驻锁文件 `.ptrade.lock` **永不删除**：删了会让两个进程锁到不同 inode。"""
    lock = store.lock_path(str(tmp_path))
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write("\0")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    store.cleanup_stale_artifacts(os.path.dirname(lock), max_age_seconds=1.0)
    assert os.path.exists(lock)


def test_W9_first_lock_acquisition_sweeps_stale_artifacts(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    stale = data / (store.TMP_PREFIX + "leftover")
    stale.write_text("x", encoding="utf-8")
    old = time.time() - (store.TMP_MAX_AGE_SECONDS + 60)
    os.utime(stale, (old, old))
    store._CLEANED_DIRS.discard(os.path.abspath(str(data)))
    with store.state_lock(str(tmp_path), timeout=5.0):
        pass
    assert not stale.exists()


def test_W9_cleanup_public_entry_accepts_cfg_or_root(tmp_path):
    cfg = make_cfg(tmp_path)
    assert store.cleanup_stale(cfg) == []          # cfg 形式
    assert store.cleanup_stale(str(tmp_path)) == []  # root 形式


# --------------------------------------------------------------------------- #
# S12–S14（B3）：ledger 水位与「ledger 有单、state 无单」的分叉可检出
# --------------------------------------------------------------------------- #
def _seed(cfg, ledger_rows: int = 0, recorded: int | None = None):
    """造一个干净数据根：可选写入 N 行 ledger，并把 state 的水位固定为 ``recorded``。"""
    (pathlib.Path(cfg.root) / "data").mkdir(parents=True, exist_ok=True)
    state = core.new_state(cfg)
    store.save_state(cfg, state, source="probe")   # 先落一次，让水位 = 当时的 ledger 行数
    for i in range(ledger_rows):
        store.append_jsonl(store.ledger_path(cfg), {"order_id": f"O{i + 1:04d}", "side": "buy", "status": "filled"})
    if recorded is not None:
        doc = json.loads(pathlib.Path(store.state_path(cfg)).read_text(encoding="utf-8"))
        doc["app"]["ledger_lines"] = recorded
        pathlib.Path(store.state_path(cfg)).write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8"
        )


def test_S12_divergence_is_detected_when_ledger_ahead_of_state(tmp_path):
    """**S12（B3）**：ledger 比 state 的水位多行（state 落盘失败）→ 必须报分叉。

    这是 B3 的核心：写一单是「先 append ledger，再原子写 state」，state 落盘失败就
    留下「ledger 有单、state 无单」。没有这条水位比对，这种分叉会**永远静默**。
    """
    cfg = make_cfg(tmp_path)
    _seed(cfg, ledger_rows=2, recorded=1)          # ledger 2 行、水位只认 1 行
    r = store.check_ledger_state_consistency(cfg)
    assert r["diverged"] is True, r
    assert r["consistent"] is False, r
    assert r["ledger_lines"] == 2 and r["recorded"] == 1, r


def test_S12_consistent_when_equal(tmp_path):
    """**S12 的对照**：水位与实测行数相等 → 一致（否则上面那条「总能红」就没意义）。"""
    cfg = make_cfg(tmp_path)
    _seed(cfg, ledger_rows=3, recorded=3)
    r = store.check_ledger_state_consistency(cfg)
    assert r["diverged"] is False, r
    assert r["consistent"] is True, r


def test_S12_undecidable_is_not_reported_as_consistent(tmp_path):
    """**S12 的关键边界**：无法判定时**不得**谎报一致。

    缺水位（旧版 state.json，没有 ``app.ledger_lines``）时 ``diverged`` 必须是
    ``None`` 且 ``consistent`` 为 ``False`` —— 把「不知道」当成「没问题」正是
    这类一致性检查最容易犯的错。
    """
    cfg = make_cfg(tmp_path)
    (pathlib.Path(cfg.root) / "data").mkdir(parents=True, exist_ok=True)
    store.save_state(cfg, core.new_state(cfg), source="probe")
    doc = json.loads(pathlib.Path(store.state_path(cfg)).read_text(encoding="utf-8"))
    doc["app"].pop("ledger_lines", None)           # 模拟没有水位的旧 state
    pathlib.Path(store.state_path(cfg)).write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )
    r = store.check_ledger_state_consistency(cfg)
    assert r["diverged"] is None, r
    assert r["consistent"] is False, r
    assert "reason" in r, r


def test_S12_watermark_tracks_real_writes(tmp_path):
    """**S12 的端到端**：每次 ``save_state`` 都应把当时 ledger 行数写进水位。"""
    cfg = make_cfg(tmp_path)
    (pathlib.Path(cfg.root) / "data").mkdir(parents=True, exist_ok=True)
    store.save_state(cfg, core.new_state(cfg), source="probe")
    assert store.check_ledger_state_consistency(cfg)["recorded"] == 0

    store.append_jsonl(store.ledger_path(cfg), {"order_id": "O0001"})
    store.save_state(cfg, core.new_state(cfg), source="probe")
    r = store.check_ledger_state_consistency(cfg)
    assert r["recorded"] == 1 and r["diverged"] is False, r

    store.append_jsonl(store.ledger_path(cfg), {"order_id": "O0002"})
    # 不 save_state → ledger 领先，应检出分叉
    r2 = store.check_ledger_state_consistency(cfg)
    assert r2["diverged"] is True, r2


def test_S13_engine_ledger_goes_through_store(tmp_path, monkeypatch):
    """**S13（B3）**：``Engine._append_ledger`` 必须经 ``store.append_jsonl``，不得裸写。

    裸 ``open("a")`` 在 Windows 上不是行原子（CRT 的 ``O_APPEND`` 是「seek + write」两步，
    实测 3 进程 × 60 行丢 3 行），且绕过了行锁与 fsync。

    用 **AST** 取函数体源码 —— 字符串查 ``def `` 会在「该方法是类里最后一个」时失效
    （第一版就是这么失败的）。
    """
    import ast

    from ptrade import engine as engine_mod

    src = pathlib.Path(engine_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    body_src: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_append_ledger":
            body_src = ast.get_source_segment(src, node)
            break
    assert body_src is not None, "找不到 Engine._append_ledger"

    assert "store.append_jsonl" in body_src, "_append_ledger 未走 store.append_jsonl"
    assert '.open("a"' not in body_src and ".open('a'" not in body_src, (
        '_append_ledger 仍在使用裸 open("a") —— 会绕过行锁与 fsync'
    )

    # 行为面：真跑一次，确认确实调到 store.append_jsonl
    calls: list[str] = []
    real = store.append_jsonl

    def spy(path, obj, **kw):
        calls.append(str(path))
        return real(path, obj, **kw)

    monkeypatch.setattr(store, "append_jsonl", spy)
    cfg = make_cfg(tmp_path)
    (pathlib.Path(cfg.root) / "data").mkdir(parents=True, exist_ok=True)
    store.append_jsonl(store.ledger_path(cfg), {"order_id": "O0001"})
    assert calls, "spy 未被调用（注入点无效）"
    assert calls[0].endswith("ledger.jsonl")


def test_S14_append_jsonl_is_line_atomic_across_processes(tmp_path):
    """**S14（跨进程）**：多进程并发追加 N 行，最终必须**一行不少**且每行可解析。

    Windows 上 ``O_APPEND`` 不是行原子，core-builder 实测 3 进程 × 60 行丢 3 行。
    """
    path = str(tmp_path / "data" / "ledger.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    per_worker = 20
    workers = 3
    barrier = CTX.Barrier(workers)
    queue = CTX.Queue()
    procs = [
        CTX.Process(target=_worker_append_lines, args=(path, per_worker, barrier, queue))
        for _ in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=300)
    for _ in procs:
        assert queue.get(timeout=60) == "done"      # _worker_append_lines 汇报裸字符串

    lines = [l for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == per_worker * workers, f"丢行：{len(lines)} != {per_worker * workers}"
    for line in lines:                              # 每行必须完整可解析
        json.loads(line)


def test_meta_only_store_may_call_os_replace():
    """**源码级元测试**（Critic N-4 建议）：除 ``store.py`` 外，禁止任何模块直接调用 ``os.replace``。

    为什么需要这条：``watchlist.py`` 曾自带一份**无重试**的原子写实现，在 4 个读者线程下
    实测 60 次写**失败 50 次**（``PermissionError errno=13``）。写方重试必须只有一处落点，
    否则每新增一个「写文件」的模块就会把同一个 bug 重新引入一次 —— 而且单线程下它工作正常，
    普通单元测试不会红。

    用 AST 判定，不用字符串匹配：docstring / 注释里提到 ``os.replace`` 是合法的
    （``store.py`` 自己的说明与 ``watchlist.py`` 的解释性注释都要能过）。
    """
    import ast
    import pathlib

    package_dir = pathlib.Path(store.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_dir.glob("*.py")):
        if path.name == "store.py":
            continue  # 唯一允许的落点
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "replace"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "os"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "除 store.py 外不得直接调用 os.replace（会绕过写方重试，见 Critic N-4）：" + ", ".join(offenders)
    )


def test_meta_guard_would_catch_a_violation():
    """**非空守卫**：上面那条元测试必须真的能红 —— 用假源码验证判定逻辑本身有效。

    否则「没有违规」与「检测器失效」不可区分（本项目铁律：空守卫比没有守卫更糟）。
    """
    import ast

    def _os_replace_calls(src: str) -> list[int]:
        tree = ast.parse(src)
        return [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ]

    violating = "import os\n\ndef f(a, b):\n    os.replace(a, b)\n"
    assert _os_replace_calls(violating) == [4], "检测器未识别违规调用 → 元测试是空的"

    harmless = '"""文档里提到 os.replace 是合法的。"""\n\nX = {"os.replace": 1}\n'
    assert _os_replace_calls(harmless) == [], "不得把 docstring / 字符串键误判为调用"


# --------------------------------------------------------------------------- #
# H2（code-review 发现，Lead 复现并修复）：失败的写路径不得落盘、不得刷新水位
#
# 修之前：`locked_update` 把 mutate 的 `return None` 解释成「写回入参状态」，
# 于是 `cmd_init` 的错误路径（本意"别写"）**仍然落盘**，而 `save_state` 顺手把
# `app.ledger_lines` 刷成当前 ledger 行数 → 一次失败的 init 就能抹掉已检出的账实分叉。
# Lead 复现：注入幽灵行后 `diverged=True`；跑一次失败的 init（「账户已存在」）后
# `diverged=False`（水位 1→2），而账户里仍没有那笔成交。
# --------------------------------------------------------------------------- #
def test_H2_no_write_sentinel_skips_persist(tmp_path):
    """**H2 的机制断言**：mutate 返回 `NO_WRITE` 时，state 与审计都**不得**变化。"""
    cfg = make_cfg(tmp_path)
    (pathlib.Path(cfg.root) / "data").mkdir(parents=True, exist_ok=True)
    store.save_state(cfg, core.new_state(cfg), source="probe")
    state_path = pathlib.Path(store.state_path(cfg))
    audit_path = pathlib.Path(store.audit_path(cfg))
    before_state = state_path.read_bytes()
    before_audit = audit_path.read_bytes() if audit_path.is_file() else b""

    store.locked_update(cfg, lambda state: store.NO_WRITE, source="probe")

    assert state_path.read_bytes() == before_state, "NO_WRITE 之后 state 仍被改写了"
    after_audit = audit_path.read_bytes() if audit_path.is_file() else b""
    assert after_audit == before_audit, "NO_WRITE 之后仍追加了审计行"


def test_H2_failed_init_does_not_mask_divergence(tmp_path):
    """**H2 的端到端断言**：一次**失败**的 init 不得刷新 ledger 水位。

    步骤：init → 下单 → 注入幽灵 ledger 行（模拟 state 落盘失败，应检出分叉）
         → 跑一次会失败的 init（无 --reset）→ 分叉**必须仍被检出**。
    """
    cfg = make_cfg(tmp_path)
    (pathlib.Path(cfg.root) / "data").mkdir(parents=True, exist_ok=True)
    store.save_state(cfg, core.new_state(cfg), source="probe")
    store.append_jsonl(store.ledger_path(cfg), {"order_id": "O0001", "side": "buy"})
    store.save_state(cfg, core.new_state(cfg), source="probe")
    assert store.check_ledger_state_consistency(cfg)["diverged"] is False

    # 注入幽灵行 → 应检出分叉
    store.append_jsonl(store.ledger_path(cfg), {"order_id": "O9999", "_injected": True})
    assert store.check_ledger_state_consistency(cfg)["diverged"] is True

    # 模拟「失败的 init」：存在性检查不过 → 返回 NO_WRITE → 不落盘、不刷水位
    def failing_init(state):
        return store.NO_WRITE

    store.locked_update(cfg, failing_init, source="probe")
    after = store.check_ledger_state_consistency(cfg)
    assert after["diverged"] is True, (
        f"一次失败的 init 抹掉了已检出的账实分叉：{after} —— B3 的检出能力被自毁（H2 复发）"
    )


# --------------------------------------------------------------------------- #
# M4（code-review 发现，Lead 修复）：一致性检查必须在写锁内读取
#
# 修之前：不取锁 → 两侧基于**不同时刻**的快照 → 事务进行中必然误报 diverged=True。
# 这是 M3（把它接进 status/health 让用户看见）的**前提** —— 顺序不能反，
# 否则用户会看到随机告警。
# --------------------------------------------------------------------------- #
def test_M4_no_false_positive_during_transaction(tmp_path):
    """**M4**：写事务进行中调用一致性检查，不得误报分叉。

    做法：另起线程持锁并在锁内追加 ledger 行（模拟事务进行中），
    主线程用**短超时**调用检查器 —— 应返回 ``diverged=None``（不可判定），
    而不是基于「ledger 已多一行、state 还没写」的中间态误报 ``True``。
    """
    import threading
    import time

    cfg = make_cfg(tmp_path)
    (pathlib.Path(cfg.root) / "data").mkdir(parents=True, exist_ok=True)
    store.save_state(cfg, core.new_state(cfg), source="probe")
    store.append_jsonl(store.ledger_path(cfg), {"order_id": "O0001"})
    store.save_state(cfg, core.new_state(cfg), source="probe")
    assert store.check_ledger_state_consistency(cfg)["diverged"] is False

    inside = threading.Event()

    def long_txn() -> None:
        with store.state_lock(cfg.root, timeout=10):
            inside.set()
            store.append_jsonl(store.ledger_path(cfg), {"order_id": "O0002"})
            time.sleep(1.2)          # 事务进行中：ledger 多一行、state 未更新

    t = threading.Thread(target=long_txn, daemon=True)
    t.start()
    assert inside.wait(timeout=10), "未能进入事务"

    mid = store.check_ledger_state_consistency(cfg, timeout=0.3)
    t.join(timeout=10)

    assert mid["diverged"] is None, (
        f"事务进行中误报分叉：{mid} —— M4 复发（两侧快照不同时刻）"
    )
    assert mid["consistent"] is False, "不可判定时 consistent 必须为 False（不得谎报一致）"
    assert "reason" in mid

    # 事务结束后：应检出**真实**分叉（ledger 2 行、水位 1）
    after = store.check_ledger_state_consistency(cfg)
    assert after["diverged"] is True, f"事务结束后应检出真实分叉：{after}"
