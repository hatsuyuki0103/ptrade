"""锁内禁网开关的回归测试（架构评审 S-1 第 2 条，2026-10-13 落地）。

**它解决什么**：BLOCK-1 的「锁外预热」只把**已知**的取数挪出临界区。万一还有漏网的取数，
它仍会在持有写锁时联网 —— 那是 **fail-slow**：写者互相饿死、网页端返回 503，
而账户本身毫无问题，排查极难。有了这个开关，漏网会**立刻响亮报错**并指明修法。

**实测（`.omx/probe/detect_inlock_network2.py`）**：数据源可用时，写命令的
锁外预热 4/1/3 次、锁内联网 **0** 次 —— 即当前没有漏网；
开关的作用是**防未来回归**，而不是修一个现存缺口。
"""

from __future__ import annotations

import types

import pytest

from ptrade import store
from ptrade.config import load_config
from ptrade.datasource import DataSource, DataUnavailable


@pytest.fixture
def online_ds(tmp_path):
    """在线 DataSource + 假 akshare 模块（**不真联网**）。"""
    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=False)
    fake = types.ModuleType("akshare")
    ds._ak_module = fake
    return ds, cfg


def test_in_critical_section_marker_tracks_depth(tmp_path):
    """**标记本身**：锁内为 True、锁外为 False，且**重入**不会提前清除。"""
    assert store.in_critical_section() is False, "锁外不应标记为持锁"
    with store.state_lock(str(tmp_path)):
        assert store.in_critical_section() is True
        with store.state_lock(str(tmp_path)):          # 同线程重入
            assert store.in_critical_section() is True
        assert store.in_critical_section() is True, "退出内层后仍应持锁（标记被提前清除）"
    assert store.in_critical_section() is False, "退出后标记未清除"


def test_ak_is_allowed_outside_lock(online_ds):
    """**对照**：锁外取 akshare 必须正常（否则上面那条「锁内被拒」没有鉴别力）。"""
    ds, _ = online_ds
    assert ds._ak().__class__ is types.ModuleType or ds._ak() is ds._ak_module


def test_ak_is_refused_inside_lock(online_ds):
    """**核心断言**：锁内取 akshare 必须被拒，且错误信息指明修法。"""
    ds, cfg = online_ds
    with store.state_lock(cfg.root):
        with pytest.raises(DataUnavailable) as ei:
            ds._ak()
    msg = str(ei.value)
    assert "写锁临界区内禁止联网取数" in msg, msg
    assert "prewarm" in msg or "锁外" in msg, f"错误信息未指明修法：{msg}"

    # 退出锁后应恢复可用（证明它只是**临界区**内的限制）
    assert ds._ak() is not None


def test_offline_error_message_not_hijacked(online_ds, tmp_path):
    """**不混淆语义**：离线模式的错误信息必须仍是原来那条，不被新守卫接管。"""
    cfg = load_config(str(tmp_path))
    ds = DataSource(cfg, offline=True)
    with pytest.raises(DataUnavailable) as ei:
        ds._ak()
    msg = str(ei.value)
    assert "offline=True" in msg and "禁止访问 akshare" in msg, msg
    assert "写锁临界区内禁止" not in msg, "离线错误被锁内守卫顶替了"


def test_guard_is_per_thread_not_process_wide(tmp_path):
    """**按线程**：一个线程持锁，不得让**另一个**线程的锁外取数被拒。

    否则会把「同进程内并发工作」误伤成全局禁令。
    """
    import threading

    errors: list[str] = []
    done = threading.Event()

    def other_thread():
        try:
            with store.state_lock(str(tmp_path)):
                pass                       # 另一个线程也在持锁
        except Exception as exc:           # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            done.set()

    with store.state_lock(str(tmp_path)):
        t = threading.Thread(target=other_thread, daemon=True)
        t.start()
        # 持锁期间，**本线程**在锁内 → 标记为 True；另一个线程在等锁，标记应为 False
        assert store.in_critical_section() is True
        done.wait(timeout=30)
        t.join(timeout=30)
        assert store.in_critical_section() is True, "另一个线程的活动不应影响本线程标记"
    assert store.in_critical_section() is False
