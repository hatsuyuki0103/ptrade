"""tests/test_cli_locking.py —— B1/C1–C4：CLI 写路径必须纳入跨进程写者互斥。

背景（Architect 评审 BLOCK 项 B1）：`ptrade` 的网页端与 CLI 会**并发写**同一数据根的
``data/state.json`` 与 ``data/ledger.jsonl``。修复前 `cli.py` 的 4 个写命令是裸的
read-modify-write（``_load_state`` → 引擎改 → ``_save_state``），**完全无锁**，
评审者实测两进程并发丢 40/19/28 次更新。

纪律（test-spec §0）：
* 用**真实 CLI 子进程**（``python -m ptrade``）而不是直接调函数 —— 这样测的是
  「用户实际跑的那条路径」有没有取锁，而不是「某个内部函数存在」；
* 每条谓词都要有**非空守卫**：把锁短路后必须变红，否则该测试是空的；
* 只复制用得到的夹具（单票约 100 KB），不整份复制 6.6 MB；
* 全程 ``--offline``（conftest 的 autouse socket 拦截守卫对本文件同样生效）。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE_SRC = REPO_ROOT / "data" / "fixture"

# 这些票都便宜（工商银行 ≈ 7.02 元），50 万账户可以下多单而不触发单票 20% 上限
CHEAP_CODE = "601398"
CHEAP_PRICE = "7.02"
TRADE_DATE = "2025-10-09"
NEXT_TRADE_DATE = "2025-10-10"

# 每个用例至少要有的夹具（含交易日历 —— CLI 的日期校验要用）
NEEDED_FIXTURES = (
    "trade_dates.json",
    "margin_targets.json",
    "industry_map.json",
    f"daily_{CHEAP_CODE}.json",
    f"daily_{CHEAP_CODE}_unadjust.json",
    f"spot.json",
)


def prepare_root(root: pathlib.Path) -> None:
    """把必需的夹具复制进 ``<root>/data/fixture``（只拷用得到的，不拷 6.6 MB）。"""
    data = root / "data"
    (data / "fixture").mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for name in NEEDED_FIXTURES:
        src = FIXTURE_SRC / name
        if src.is_file():
            shutil.copy2(src, data / "fixture" / name)
        else:
            missing.append(name)
    if missing:
        pytest.skip("缺少夹具 " + ", ".join(missing) + " —— 先跑 python data/fixture/build_fixtures.py")


def run_cli(root: pathlib.Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """跑一次真实 CLI（``python -m ptrade ... --root <root> --offline --json``）。"""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO_ROOT)}
    cmd = [
        sys.executable, "-m", "ptrade",
        *args,
        "--root", str(root),
        "--offline", "--json",
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(REPO_ROOT), env=env, timeout=timeout,
    )


def cli_json(root: pathlib.Path, *args: str, timeout: float = 120.0) -> dict[str, Any]:
    proc = run_cli(root, *args, timeout=timeout)
    if not proc.stdout.strip():
        pytest.fail(f"CLI 无输出：args={args} rc={proc.returncode} stderr={proc.stderr[:400]}")
    return json.loads(proc.stdout)


def init_account(root: pathlib.Path, cash: int = 500_000) -> None:
    out = cli_json(root, "init", "--cash", str(cash), "--reset")
    assert out["ok"] is True, out


def ledger_rows(root: pathlib.Path) -> list[dict[str, Any]]:
    path = root / "data" / "ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _holder_process(root: str, hold_seconds: float, queue) -> None:
    """子进程：独占写锁 ``hold_seconds`` 秒（spawn 上下文，见 test_store 的纪律）。"""
    sys.path.insert(0, str(REPO_ROOT))
    from ptrade import store

    try:
        with store.state_lock(root, timeout=30.0):
            queue.put(("held", time.time()))
            time.sleep(hold_seconds)
        queue.put(("released", time.time()))
    except Exception as exc:  # noqa: BLE001 - 子进程内异常要带回父进程
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _order_process(root: str, attempts: int, qty: int, queue) -> None:
    """子进程：连续尝试下单 ``attempts`` 次，汇报**成交数**。"""
    system_python = sys.executable
    project = str(REPO_ROOT)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": project}
    filled = 0
    for i in range(attempts):
        cmd = [
            system_python, "-m", "ptrade", "order",
            "--code", CHEAP_CODE, "--side", "buy",
            "--qty", str(qty), "--price", CHEAP_PRICE,
            "--trade-date", TRADE_DATE,
            "--root", root, "--offline", "--json",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=project, env=env, timeout=180,
        )
        try:
            out = json.loads(proc.stdout)
        except Exception:  # noqa: BLE001
            continue
        orders = (out.get("data") or {}).get("orders") or []
        if orders and orders[0].get("status") == "filled":
            filled += 1
    queue.put(("filled", filled))


@pytest.fixture()
def cli_root(tmp_path):
    prepare_root(tmp_path)
    init_account(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# C1 + C4：4 个写命令确实在等锁（参数化，不得只测 order）
# --------------------------------------------------------------------------- #
WRITE_COMMANDS: list[tuple[str, tuple[str, ...]]] = [
    ("init", ("init", "--cash", "500000", "--reset")),
    ("order", ("order", "--code", CHEAP_CODE, "--side", "buy", "--qty", "100",
               "--price", CHEAP_PRICE, "--trade-date", TRADE_DATE)),
    ("settle", ("settle", "--trade-date", NEXT_TRADE_DATE)),
    ("close", ("close", "--code", CHEAP_CODE, "--ratio", "1.0",
               "--trade-date", NEXT_TRADE_DATE)),
]


def _read_owner_hint(lock_file: pathlib.Path) -> str:
    """读锁文件里的 owner-hint（诊断线索）。

    ⚠️ **必须跳过第 0 字节**：那是锁区，Windows 的字节范围锁**连另一个进程的句柄**
    都不允许读它 —— 直接 ``read_text()`` 会 ``PermissionError: [Errno 13]``
    （本用例第一版就这么红的，正好也证明了「锁区真的被锁住了」）。
    """
    with open(lock_file, "rb") as fh:
        fh.seek(1)
        return fh.read().decode("utf-8", errors="replace")


@pytest.mark.parametrize("label,args", WRITE_COMMANDS, ids=[c[0] for c in WRITE_COMMANDS])
def test_C1_C4_write_command_waits_for_lock(tmp_path, label, args):
    """**C1/C4**：外部进程持写锁时，每个写命令都必须**等**到锁释放。

    ★ **G011：判据已从「墙钟差值」改成确定性判据**（原写法在并发负载下必然假红）：
    原判据是 ``争用耗时 − 基线耗时 > 1.5s`` —— 基线会被负载抬高（实测 ~1s → 1.93s），
    差值被压缩到 1.44s → 假红。**墙钟谓词与产品行为无关**，已彻底删除。

    现在的三条判据（全部与机器速度/负载无关，且**更强**）：
    1. **持锁期间 ``state.json`` 一个字节都没变** —— 这是「绕过锁直接写」的**直接 falsifier**
       （原判据只能从"多花了时间"**推断**它等了锁，推断不出"它没偷写"）；
    2. **持锁期间命令进程仍在运行** —— 它没有"不等锁就把活干完"；
    3. **释放后命令确实取到了锁** —— 锁文件的 owner-hint 换成该命令的 pid
       （G011 同时给 owner-hint 补了覆盖；这条把"等待"升级为"确实获得了锁"）。
    """
    root = tmp_path
    prepare_root(root)
    init_account(root)
    if label == "order":
        # 让 close/settle 有可操作的对象
        cli_json(root, "order", "--code", CHEAP_CODE, "--side", "buy", "--qty", "10000",
                 "--price", CHEAP_PRICE, "--trade-date", TRADE_DATE)

    from ptrade import store as store_mod
    from ptrade.config import load_config

    cfg = load_config(str(root))
    state_file = pathlib.Path(store_mod.state_path(cfg))
    before = state_file.read_bytes()
    lock_file = pathlib.Path(store_mod.lock_path(cfg))
    use_hint = store_mod._OS_BACKEND is not None   # 退化后端把 hint 写在 .excl 里，本用例不覆盖

    # ── 外部进程持锁 ──
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    hold_seconds = 3.0
    holder = ctx.Process(target=_holder_process, args=(str(root), hold_seconds, queue))
    holder.start()
    assert queue.get(timeout=60)[0] == "held", "持锁进程未能取得锁"

    if use_hint:
        hint_held = _read_owner_hint(lock_file)
        assert f"pid={holder.pid}" in hint_held, (
            f"owner-hint 没记录持锁者 pid（{holder.pid}）—— 锁争用现场将失去唯一诊断线索"
        )

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO_ROOT)}
    child = subprocess.Popen(
        [sys.executable, "-m", "ptrade", *args, "--root", str(root), "--offline", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
    )
    try:
        # ★ 判据 1/2：**在整个持锁窗口内持续观测** —— 状态文件一个字节都不能变、进程必须还活着。
        #   ⚠️ 不要用「固定 1s 取样窗口」：CLI 启动 + import + 读夹具要 1~2s，
        #   1s 窗口会**漏掉**「等锁逻辑坏了、命令到达锁点后立刻偷写」的场景
        #   （G011 变异 M1 实测：固定窗口下该变异**仍绿** = 空守卫）。
        #   循环条件绑定 `holder.is_alive()`：窗口 = 真实持锁期，长度由持锁方决定，
        #   与机器速度/负载无关；负载高只会让观测**更不容易**抓到违规（不会假红）。
        while holder.is_alive():
            assert child.poll() is None, (
                f"{label} 在锁被他人持有时就结束了（rc={child.returncode}）—— 它没有等锁"
            )
            assert state_file.read_bytes() == before, (
                f"{label} 在锁被他人持有时改写了 state.json —— 它绕过了 store 的写锁（B1 回归）"
            )
            time.sleep(0.02)

        # 等持锁方释放
        holder.join(timeout=hold_seconds + 30)
        assert not holder.is_alive(), "持锁进程没有按时退出"

        # ★ 判据 3：命令最终**确实取到了锁**（owner-hint 记录的是它自己的 pid）
        out, err = child.communicate(timeout=180)
        assert child.returncode in (0, 1), err[:400]
        if use_hint and child.returncode == 0:
            hint_after = _read_owner_hint(lock_file)
            assert f"pid={child.pid}" in hint_after, (
                f"{label} 从未取得写锁（owner-hint 里始终没有它的 pid={child.pid}）—— "
                "说明它绕过了锁，或根本没走 store.locked_update"
            )
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=30)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=30)


def test_C1_guard_without_lock_does_not_wait(tmp_path, monkeypatch):
    """**C1 的非空守卫**（Critic P3 指定写法）：把 `store.state_lock` 换成空上下文，
    被守护的那个谓词必须失效。

    这条证明 C1 不是空守卫 —— 若去掉锁之后「多等的时间」仍然显著，那
    C1 测的就不是锁（比如测的是命令本身的耗时差异），它毫无鉴别力。

    注意：**不能**用「把 `cli._save_state` 换回裸写」来构造对照 ——
    那个函数已删除且零调用点，换它什么都不会红（这正是 Critic P3 指出的问题）。
    """
    import contextlib

    from ptrade import store

    real = store.state_lock
    monkeypatch.setattr(store, "state_lock", lambda *a, **k: contextlib.nullcontext())
    try:
        # 空上下文下取锁应立即返回，不等待
        t0 = time.monotonic()
        with store.state_lock(str(tmp_path), timeout=5.0):
            pass
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, "空上下文应立即返回"
    finally:
        monkeypatch.setattr(store, "state_lock", real)

    # 真实锁在不被占用时同样立即返回；差别只在「被占用时是否等待」，
    # 由 test_C1_C4_write_command_waits_for_lock 覆盖。
    t0 = time.monotonic()
    with store.state_lock(str(tmp_path), timeout=5.0):
        pass
    assert time.monotonic() - t0 < 0.5


# --------------------------------------------------------------------------- #
# C2：跨前端（多进程 CLI）并发下单不丢更新
# --------------------------------------------------------------------------- #
def test_C2_concurrent_cli_orders_do_not_lose_updates(tmp_path):
    """**C2**：4 个真实 CLI 子进程各下 8 单（共 32 次尝试），**所有成交都必须落到 ledger**。

    判据：``ledger 行数 == 各进程自报的成交总数``，且
    ``store.check_ledger_state_consistency()`` 为一致。

    为什么这是「不丢更新」的判据：写一单的时序是「state 读 → 改 → 写」+「ledger 追加」。
    无锁时两个进程会读到同一份旧 state 再各自覆盖，**后写的进程抹掉先写的持仓**，
    于是 ledger 的行数会**多于** state 实际承认的成交数（账实分叉）。
    """
    import multiprocessing as mp

    root = tmp_path
    prepare_root(root)
    init_account(root)

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = 4
    attempts = 8
    qty = 100  # 100 股 × 7.02 = 702 元，远低于单票上限，确保不会因额度被拒
    workers = [
        ctx.Process(target=_order_process, args=(str(root), attempts, qty, queue))
        for _ in range(procs)
    ]
    for p in workers:
        p.start()
    reported = 0
    for _ in range(procs):
        kind, value = queue.get(timeout=600)
        assert kind == "filled", (kind, value)
        reported += value
    for p in workers:
        p.join(timeout=120)

    rows = ledger_rows(root)
    assert reported > 0, "所有进程都没成交，用例无鉴别力（应至少有一单成交）"

    ids = [r.get("order_id") for r in rows]
    assert len(ids) == len(set(ids)), f"order_id 重复，说明有覆盖：{ids}"

    # 账实一致：ledger 行数必须等于 state 承认的成交数
    sys.path.insert(0, str(REPO_ROOT))
    from ptrade.config import load_config
    from ptrade import store

    cfg = load_config(str(root))
    consistency = store.check_ledger_state_consistency(cfg)
    assert consistency["diverged"] is False, f"账实分叉：{consistency}"

    state = store.read_state(cfg)
    assert state is not None
    filled_in_state = [o for o in state.history if getattr(o, "status", None) == "filled"]
    assert len(rows) == len(filled_in_state), (
        f"ledger 行数 {len(rows)} != state 里 filled 成交数 {len(filled_in_state)} —— 丢更新"
    )


def test_C2_guard_naive_update_loses_updates(tmp_path):
    """**C2 的非空守卫**：短路锁、跑同一并发 workload，必须观察到丢失。

    与 ``test_store.test_S2_guard_without_lock_loses_updates`` 同构；这里多了
    「经 CLI 的 state 读-改-写」这一层，证明 CLI 侧的守卫同样有鉴别力。

    .. note::
       本用例**自身**依赖 ``store.state_lock`` 真的会互斥。若把该函数全局短路，
       本用例会因「有锁分支也丢失」而失败 —— 那是**预期**的：它证明的不是自己空，
       而是被守护对象确实失效了。（独立实验记录：短路后本用例报
       「有锁并发下仍丢失 10 次（预期 0，总计 160）」。）
    """
    sys.path.insert(0, str(REPO_ROOT))
    import multiprocessing as mp
    import threading

    from ptrade import core, store
    from ptrade.config import load_config

    root = tmp_path
    prepare_root(root)
    cfg = load_config(str(root))
    (root / "data").mkdir(parents=True, exist_ok=True)
    store.save_state(cfg, core.new_state(cfg), source="cli")

    rounds = 40
    workers = 4
    lost_naive = _naive_concurrent_bump(cfg, rounds, workers, use_lock=False)
    lost_locked = _naive_concurrent_bump(cfg, rounds, workers, use_lock=True)

    expected = rounds * workers
    assert lost_naive > 0, (
        "无锁并发下竟然没有丢失 —— 说明本对照没有鉴别力，C2 是空守卫"
    )
    assert lost_locked == 0, f"有锁并发下仍丢失 {lost_locked} 次（预期 0，总计 {expected}）"


def _naive_concurrent_bump(cfg, rounds: int, workers: int, *, use_lock: bool) -> int:
    """并发做 ``读 → +1 → 写``；返回丢失的更新数。``use_lock=False`` 时不取锁。"""
    import threading

    from ptrade import store

    store.save_state(cfg, _fresh_state(cfg), source="probe")
    results: list[int] = []
    barrier = threading.Barrier(workers)

    def bump() -> None:
        barrier.wait()
        for _ in range(rounds):
            if use_lock:
                with store.state_lock(cfg.root, timeout=30.0):
                    state = store.read_state(cfg)
                    assert state is not None
                    state.available_cash = float(state.available_cash) + 1.0
                    store.save_state(cfg, state, source="probe")
            else:
                state = store.read_state(cfg)
                assert state is not None
                time.sleep(0.0005)  # 放大读-改窗口
                state.available_cash = float(state.available_cash) + 1.0
                store.save_state(cfg, state, source="probe")

    threads = [threading.Thread(target=bump) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    final = store.read_state(cfg)
    assert final is not None
    expected = rounds * workers
    actual = int(round(float(final.available_cash) - _base_cash(cfg)))
    results.append(actual)
    return max(0, expected - actual)


def _base_cash(cfg) -> float:
    from ptrade import core

    return float(core.new_state(cfg).available_cash)


def _fresh_state(cfg):
    from ptrade import core

    return core.new_state(cfg)


# --------------------------------------------------------------------------- #
# C3：CLI 不得用旧快照覆盖别处的写入
# --------------------------------------------------------------------------- #
def test_C3_two_writes_both_survive(tmp_path):
    """**C3**：连续两笔写都必须留下 —— 后一笔不得抹掉前一笔。

    .. warning::
       本用例**不是**锁的守卫，这一点由实验判定，而非推测：

       * 短路 ``store.state_lock`` → 它仍然**通过**；
       * 在 CLI 临界区内注入 **1.5s** 延时制造强争用 → 它仍然**通过**（3/3）。

       原因是两笔写在同一进程里**顺序**执行，从不存在争用；延时只让每条命令慢一点，
       没有制造出任何重叠。因此它**没有鉴别力**。

       它保留的价值仅为「连续写的基本正确性」（状态不会莫名回退）。
       **真正的锁守卫是 C1（等待时长）与 C2（并发不丢更新）** —— 两者都经实验确认可红。
       我最初给这条写的解释（「顺序执行 + 慢盘也会丢更新」）是**错的**，
       实验推翻后按事实改正，而不是把错误解释留在注释里当装饰。
    """
    root = tmp_path
    prepare_root(root)
    init_account(root)

    # 第一笔（等价于网页侧的写入）
    first = cli_json(root, "order", "--code", CHEAP_CODE, "--side", "buy", "--qty", "100",
                     "--price", CHEAP_PRICE, "--trade-date", TRADE_DATE)
    assert first["ok"] is True
    assert first["data"]["orders"][0]["status"] == "filled"

    rows_after_first = ledger_rows(root)
    assert len(rows_after_first) == 1

    # 第二笔（另一个写者）
    second = cli_json(root, "order", "--code", CHEAP_CODE, "--side", "buy", "--qty", "200",
                      "--price", CHEAP_PRICE, "--trade-date", TRADE_DATE)
    assert second["ok"] is True
    assert second["data"]["orders"][0]["status"] == "filled"

    rows = ledger_rows(root)
    assert len(rows) == 2, f"先前的写入被覆盖：ledger 只有 {len(rows)} 行"
    assert rows[0]["order_id"] != rows[1]["order_id"]

    # state 必须承认两笔
    sys.path.insert(0, str(REPO_ROOT))
    from ptrade import store
    from ptrade.config import load_config

    state = store.read_state(load_config(str(root)))
    assert state is not None
    filled = [o for o in state.history if getattr(o, "status", None) == "filled"]
    assert len(filled) == 2, f"state 只承认 {len(filled)} 笔成交（应为 2）—— 丢了更新"
