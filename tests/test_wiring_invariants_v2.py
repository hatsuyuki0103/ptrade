"""接线型不变量（wiring invariants）：断言「机制**真的接在生产调用点上**」。

## 为什么需要这个文件（code-reviewer 的系统性建议）

本轮 code-review 抓到一条 CRITICAL：**`INV-54` 的 `industry_map` 守卫是空守卫** ——
它有实现、有单测、有 INV 文档，**唯独没接在唯一的真实调用点上**（`_cached(..., min_rows=)` 没传）。

暴露的是一类**系统性**问题：

> **不变量都在「机制层」有单测，但「生产调用点是否真的启用了该机制」从未被钉住。**

机制层单测只能证明「**参数有效**」，完全不能证明「**生产调用点传了它**」——
本 bug 就是这么漏过去的。本文件把这类断言集中起来，**每条都是一行 AST 或行为断言**。

## 纪律（本项目三种失效形态都出现过）

* 变异没应用（脚本 no-op，表现像「守卫没用」）
* 守卫被自己改弱（Lead 改 A13 时踩过）
* **变异应用了但打偏了**（改的是别处，表现也像「守卫没用」）

→ 每个断言都要**能说清违反时长什么样**；必要时配**非空洞对照**。

## 本文件的范围纪律（architect 第 3 轮建议）

**只放「无 IO / 无线程 / 无 HTTP / 无磁盘」的机制判据。** 需要锁、线程、子进程或网络的
接线判据请放各自域文件（如 `test_inlock_network_guard.py` / `test_store.py`）。
**分裂触发条件**：当第一条需要锁/HTTP 的接线判据出现时，把本文件拆成
`test_wiring_static_v2.py`（纯 AST）与 `test_wiring_semantics_v2.py`（行为）。
—— 这样它不会慢慢长成「半个集成测试文件」。

## 判据清单（`test_file_itself_is_not_vacuous` 会**双向精确校验**这个集合）

| 判据 | 钉住的机制 |
|---|---|
| `test_W1_...` | 守卫错误不被任何 handler 吞掉（顺序 + 语义 + **一跳可达**）|
| `test_W2_...` | 两个网络出口都调 `_net_guard` |
| `test_W3_...` / `test_W3b_...` | 周期清单单一真源（含 webapp 别名 `is` + CLI choices）|
| `test_W4_...` / `test_W4b_...` | 分时容差阈值推导 + URL 的 `scale` 来自常量 |
| `test_W5_...` | `with_today_bar` 的两个前置（**行为判据**）|
| `test_W6_...` | `min_rows` 接在 `industry_map` 的生产调用点 |
| `test_W7_...` | **守卫站点登记表 + 行为覆盖断言**（新站点必须被登记）|
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# AST 小工具
# --------------------------------------------------------------------------- #
def _parse(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def _functions(tree: ast.Module, *, owner: str | None = None) -> dict[str, ast.AST]:
    """`{函数名: 节点}`；给 `owner` 时只取该**类**内的方法。"""
    out: dict[str, ast.AST] = {}
    if owner:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == owner:
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[sub.name] = sub
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _calls(node: ast.AST, name: str) -> list[int]:
    hits: list[int] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            called = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
            if called == name:
                hits.append(sub.lineno)
    return hits


def _handler_types(handler: ast.ExceptHandler) -> tuple[set[str], bool]:
    """返回 `({捕获的异常名}, 是否裸 except)`。"""
    t = handler.type
    if t is None:
        return set(), True
    if isinstance(t, ast.Name):
        return {t.id}, False
    if isinstance(t, ast.Tuple):
        return {e.id for e in t.elts if isinstance(e, ast.Name)}, False
    return set(), False


def _walk_skip_scopes(node: ast.AST):
    """遍历 `node`，但**不下钻进嵌套函数/类/lambda 的作用域**（`Try`/`With`/`If` 仍下钻）。

    ★ 用途（code-reviewer 第 4 轮 L1）：裸 re-raise 可能被**总在执行**的包装包住 ——
      `try: … finally: raise`、`with …: raise` —— 这些都是**无条件重抛**，应当放行；
      而 B1 的「嵌套 `def` 里的 `raise` + 本层 `return None`」必须**仍然**被挡住。
      ⇒ 判据是：**跳过嵌套作用域**，其余照走。
    """
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue          # ★ 不进入嵌套作用域
            stack.append(child)


def _surfaces_error(handler: ast.ExceptHandler) -> tuple[bool, str]:
    """该 handler 是否把守卫错误**原样**呈现出来了。返回 `(是否合规, 不合规的原因)`。

    两条**且只有两条**合规路径（**都要求出现在 handler 自身的语句层级**）：

    * **裸 re-raise** —— 顶层 `raise` 且 **`exc is None`**（重新抛出**原异常**）；
    * **顶层返回 ≥400 的 `Response`** —— HTTP 层把守卫错误映射成 409
      （真实使用者：`webapp.api_search` 的
      `Response(409, _envelope(..., error=f"NETWORK_IN_LOCK: {exc}"))`）。

    ★★ **为什么必须是「顶层」而不是 `ast.walk`（code-reviewer 第 3 轮的两条绕过）**：
      用 `ast.walk` 会**下钻进嵌套 `FunctionDef` 与死分支**，于是
      · B1：`def _helper(): raise` + 本层实际 `return None` → 被当成「呈现」，**实际吞掉**；
      · B2：`if False: return Response(409, ...)` + 本层 `return None` → 同上。
      ⇒ 只认**handler 自身的直接语句**。

    ★ 另加定向硬化（B3，形态⑤假阳性）：`raise exc`（`exc` 是该 handler `as` 绑定的名字）
      是**等价显式重抛**，**应当放行** —— 第一版把它判成「降级成别的异常」，是误报。

    ★ 非空洞：`Response(200, ...)`（业务态）**不满足** —— 那才是把守卫错误当正常结果。
    """
    bound = handler.name            # `except ... as exc:` 的绑定名（无则为 None）
    top = handler.body

    # ── 路径 A：裸 raise（**跳过嵌套作用域**后仍能找到；包装在 Try/With 里也算）──
    #   ★ L1 修复：`try: … finally: raise` / `with …: raise` 是**无条件重抛**，必须放行；
    #     而 B1（嵌套 `def` 里的 raise）因**不下钻函数作用域**仍被挡住。
    bare = [n for n in _walk_skip_scopes(handler)
            if isinstance(n, ast.Raise) and n.exc is None]
    if bare:
        # ★ B1/B2 的形态：一边 raise、一边 return/pass ⇒ 存在吞掉路径
        sinks = [s for s in top if isinstance(s, (ast.Return, ast.Pass))]
        if sinks:
            return False, (
                "顶层同时存在裸 `raise` 与 `return`/`pass` —— "
                "**存在吞掉路径**（B1/B2 形态：呈现代码在死分支/嵌套函数里，实际走吞掉分支）"
            )
        return True, ""

    # ── 路径 B：顶层 `return Response(<≥400>, ...)` ──
    #   ★ architect 第 3 轮 LOW：第一版只看 `args[0]` → **`Response(status=409, ...)`
    #     这种关键字形式会被误报**（§8.3 形态⑤同类）。现同时接受位置与关键字两种形态。
    for stmt in top:
        if not isinstance(stmt, ast.Return) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        called = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
        if called != "Response":
            continue
        candidates: list[ast.expr] = list(call.args)
        candidates += [k.value for k in call.keywords if k.arg == "status"]
        if any(isinstance(c, ast.Constant) and isinstance(c.value, int) and c.value >= 400
               for c in candidates):
            return True, ""

    # ── 不合规：把原因说清楚（便于「红的理由来自预期断言」）──
    for stmt in top:
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            exc = stmt.exc
            # ★ B3：`raise exc`（绑定名）是等价显式重抛 → 放行
            if bound and isinstance(exc, ast.Name) and exc.id == bound:
                return True, ""
            rendered = ast.unparse(exc)
            return False, (
                f"用的是 `raise {rendered[:50]}` —— 把守卫错误**降级成了别的异常**，"
                f"必须裸 `raise`（或 `raise {bound}` 形式的等价显式重抛）"
            )
    return False, "顶层既没有裸 `raise`，也没有 `return Response(≥400)`"


def _is_broad_handler(names: set[str]) -> bool:
    """该 handler 捕获的异常是否**宽到会把守卫错误一起接住**。

    `NetworkForbiddenInLock` 继承自 `DataUnavailable`，再继承 `Exception`，
    所以 `Exception` / `BaseException` / `DataUnavailable` / `OSError` 之类的**宽类**
    若放在 guard handler **之前**，会把守卫错误顺路吞掉。
    ★ ultraqa F-1/M3 实测：第一版只认 `bare` 与 `DataUnavailable` →
      在 guard 之前插 `except Exception: return None` **仍绿**。
    """
    BROAD = {"Exception", "BaseException", "DataUnavailable", "RuntimeError", "OSError"}
    return bool(names & BROAD)


#: 已知**会**抛 `NetworkForbiddenInLock` 的入口（用于「便宜近似」，见 W1）。
#: ★ 这不是完备清单 —— 完整覆盖需要调用图；这里只覆盖**最常见的直接调用**形态。
_GUARD_RAISERS = frozenset({
    "_ak", "_http_get", "_net_guard", "_cached", "_industry_map", "_fetch_intraday",
    "intraday", "chart_asof", "latest_trade_asof", "latest_trade_date", "spot",
    "daily", "quote", "with_today_bar", "trade_dates", "search_stocks",
})


def _calls_known_guard_raiser(try_node: ast.Try) -> bool:
    """`try` 体内是否**直接**调用了已知会抛守卫的函数（只看顶层语句，不下钻嵌套作用域）。"""
    for stmt in try_node.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call):
                called = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                if called in _GUARD_RAISERS:
                    return True
    return False


# --------------------------------------------------------------------------- #
# W1 ★ `NetworkForbiddenInLock` ⊆ `DataUnavailable` → 子类 handler 必须先出现
# --------------------------------------------------------------------------- #
def test_W1_guard_error_is_not_swallowed_by_dataunavailable_handlers():
    """**库内任何** `try` 里，若某个 handler 捕获了 `DataUnavailable`，
    则它**之前**必须有一个 handler 捕获 `NetworkForbiddenInLock`。

    为什么：`NetworkForbiddenInLock` 是 `DataUnavailable` 的**子类**，
    所以「先写 `except DataUnavailable`」会**顺手接住守卫错误**并把它降级成业务结果 ——
    用户/网页端看到的是**错误的正常结果**（业务拒单 / 静默回退），而不是 INV-42/45 要求的明确错。

    ★ 这是本轮 CRITICAL（空守卫）暴露出的**同一类**问题的另一面：
      「守卫抛了」不等于「守卫被尊重」。

    ★★ **本判据第一版的盲区（由 strategy-builder 实测发现，已修）**：
      它原本**只检查 handler 顺序**。于是把 handler 写成
      `except NetworkForbiddenInLock: return ds.intraday(code)`（**原地吞掉**）时，
      顺序仍然「合法」→ **本用例照样绿** —— 一个只检查顺序、不检查语义的**近空守卫**。
      → 现在**同时断言**该 handler 体内**确实 re-raise**（含 `ast.Raise`）。
      **教训**：接线类判据只证明「位置对」，**必须再配一条语义/行为断言**才完整。
    """
    VIOLATIONS: list[str] = []
    for path in sorted((ROOT / "ptrade").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            # ★ **只有当这个 `try` 里确实有 guard handler 时，顺序才有意义。**
            #   我第一版把 `Exception`/`OSError` 一律纳入违规定义 → 报出**一大片误报**：
            #   那些 `try`（`os.makedirs` / `item()` / `_make_ds(...)`）**根本没有** guard handler，
            #   说「except Exception 出现在 except NetworkForbiddenInLock 之前」是**胡说**。
            #   这是 §8.3 形态⑤（不该红乱红）在**本文件里**的第 4 次复现。
            #
            #   ⚠️ **已知残余缺口**（诚实声明）：若某个 `try` 只有 `except Exception`、
            #   且其**函数体真的可能抛守卫**，本判据**不会**报 —— 那需要调用图分析，
            #   超出静态 AST 的能力。该情形由行为判据兜底（见 `test_inlock_network_guard.py`）。
            has_guard = any(
                "NetworkForbiddenInLock" in _handler_types(h)[0] for h in node.handlers
            )
            if not has_guard:
                # ★ **便宜近似**（code-reviewer 第 3 轮 R3 的建议）：完整覆盖需要调用图，
                #   但「`try` 体内**直接**调用了已知会抛守卫的函数」这一最常见形态**不需要**调用图。
                #   → 这种 `try` 里若只有宽 handler，同样是吞守卫。
                broad_only = [
                    h for h in node.handlers
                    if _is_broad_handler(_handler_types(h)[0])
                ]
                if broad_only and _calls_known_guard_raiser(node):
                    handler = broad_only[0]
                    VIOLATIONS.append(
                        f"{path.name}:{node.lineno} 的 `try` **直接调用了已知会抛守卫的函数**，"
                        f"却只有宽 handler（第 {handler.lineno} 行）—— 守卫错误会被吞掉"
                        f"（无 guard handler，需调用图才能全面覆盖，此处覆盖最常见的直接调用形态）"
                    )
                continue
            seen_guard = False
            for handler in node.handlers:
                names, bare = _handler_types(handler)
                if "NetworkForbiddenInLock" in names:
                    seen_guard = True
                    # ★ 收紧（code-reviewer 第 2 轮攻破后）：必须是**裸** re-raise
                    #   或 `Response(≥400)`；`raise <别的异常>` 一律违规（降级成父类业务错误）。
                    _ok, _why = _surfaces_error(handler)
                    if not _ok:
                        VIOLATIONS.append(
                            f"{path.name}:{handler.lineno} 的 `except NetworkForbiddenInLock` "
                            f"{_why}（顺序对 ≠ 语义对）"
                        )
                    continue
                if bare or ("DataUnavailable" in names):
                    if not seen_guard:
                        caught = "<bare>" if bare else "DataUnavailable"
                        VIOLATIONS.append(
                            f"{path.name}:{node.lineno} try 的 except {caught} "
                            f"出现在 except NetworkForbiddenInLock **之前**（第 {handler.lineno} 行）"
                        )
                    seen_guard = True
                    continue
                # ★★ 收紧（ultraqa F-1 / M3）：宽 handler 同样会吞掉守卫 ——
                #   在 guard handler **之前**插 `except Exception: return None` 就能接住它。
                if _is_broad_handler(names) and not seen_guard:
                    VIOLATIONS.append(
                        f"{path.name}:{node.lineno} try 的 except {'/'.join(sorted(names))} "
                        f"出现在 except NetworkForbiddenInLock **之前**（第 {handler.lineno} 行）—— "
                        f"宽 handler 会把守卫错误一起接住（NetworkForbiddenInLock ⊂ Exception）"
                    )
                    seen_guard = True
    assert not VIOLATIONS, (
        "INV-42/45 违规 —— 守卫错误会被吞掉：\n  " + "\n  ".join(VIOLATIONS[:12])
    )


# --------------------------------------------------------------------------- #
# W2 ★ 网络守卫必须**接在两个出口上**（`_ak` 与 `_http_get`）
# --------------------------------------------------------------------------- #
def test_W2_both_network_exits_call_the_single_net_guard():
    """`_ak` 与 `_http_get` **都必须**调用 `_net_guard`。

    为什么：`INV-53` 要求「守卫实现只有一处」。若其中一个出口忘了调，
    离线/锁内禁网就在那条路径上**静默失效** —— 而机制层单测（直接调 `_net_guard`）**照样全绿**。
    """
    tree = _parse("ptrade/datasource.py")
    funcs = _functions(tree, owner="DataSource")
    missing = [n for n in ("_ak", "_http_get") if n not in funcs]
    assert not missing, f"DataSource 缺方法：{missing}"
    for name in ("_ak", "_http_get"):
        assert _calls(funcs[name], "_net_guard"), (
            f"`DataSource.{name}` 没有调用 `_net_guard` —— 该出口会绕过离线/锁内禁网守卫"
        )


# --------------------------------------------------------------------------- #
# W3 ★ 周期清单必须是**单一真源**（不是副本）
# --------------------------------------------------------------------------- #
def test_W3_period_list_has_a_single_source_of_truth():
    """`chart.PERIODS_WITH_INTRADAY` 是唯一真源；`webapp.VALID_PERIODS` 必须是**同一个对象**（别名）。

    ★★ 收紧（ultraqa F-1 / M2）：第一版只断言 `==`（**值相等**），
    而 docstring 却写着「必须是**导入**（别名）而非字面量副本」——
    于是把 `VALID_PERIODS` 换成一个**同值字面量副本**后判据**仍然绿**（实测）。
    → 改为 **`is`（同一对象）**：副本与别名在 `is` 下必然不同。
    """
    from ptrade import chart, webapp

    assert "minute" in webapp.VALID_PERIODS
    assert "minute" not in chart.PERIODS, "minute 绝不能进 K 线的 PERIODS"
    assert webapp.VALID_PERIODS is chart.PERIODS_WITH_INTRADAY, (
        f"`webapp.VALID_PERIODS` 必须**直接引用** `chart.PERIODS_WITH_INTRADAY`（同一对象），"
        f"而不是值相等的副本 —— 否则改一处、另一处静默漂移。"
        f"当前 {type(webapp.VALID_PERIODS).__name__} "
        f"（值相等={webapp.VALID_PERIODS == chart.PERIODS_WITH_INTRADAY}，同一对象="
        f"{webapp.VALID_PERIODS is chart.PERIODS_WITH_INTRADAY}）"
    )


def test_W3b_cli_period_choices_come_from_the_single_source():
    """CLI 的 `--period choices` 必须来自同一真源（而不是第三份硬编码清单）。"""
    from ptrade import chart, cli

    choices = cli._period_choices()          # noqa: SLF001 - 就是给测试看的缝
    assert choices == list(chart.PERIODS_WITH_INTRADAY), (
        f"CLI 的 period choices {choices} 与单一真源 {list(chart.PERIODS_WITH_INTRADAY)} 不一致"
    )


# --------------------------------------------------------------------------- #
# W4 ★ 分时窗口的「完整日」阈值必须与 scale 口径一致（当前 scale 不可达）
# --------------------------------------------------------------------------- #
def test_W4_intraday_min_points_is_derived_from_the_240_minute_session():
    """`INTRADAY_MIN_POINTS` 必须是**推导式**（0.95 × 240），且当前 `scale` 不可达时口径一致。

    为什么：实测真实完整日是 **238** 点（无成交的那一分钟不产生数据点），
    硬阈值 240 会**永远判不出完整日** → 非交易日前一交易日场景直接失效（第 4 次「日历/理想值」类错误）。
    ★ 风险：该值只在 **1 分钟口径**成立；若将来暴露 `scale`，48 点（5 分钟）会恒判 not complete。
    """
    from ptrade import datasource as ds_mod

    assert ds_mod.INTRADAY_MIN_POINTS == int(0.95 * 240), (
        f"INTRADAY_MIN_POINTS={ds_mod.INTRADAY_MIN_POINTS} 不是 0.95×240 的推导值"
    )
    assert ds_mod.INTRADAY_MIN_POINTS <= 238, (
        "阈值必须 ≤ 实测完整日（238），否则真实完整日会被判成不完整"
    )
    # 当前 scale 不可达（常量 + 签名无该参数）⇒ 阈值口径与 1 分钟一致
    assert ds_mod.INTRADAY_SCALE == 1, (
        "INTRADAY_SCALE 变了 —— 必须同步复核 INTRADAY_MIN_POINTS（48 点/日的口径要改）"
    )
    import inspect

    # ★ 收紧（code-reviewer 第 2 轮 B-i 攻击）：不能只查**精确名字** `"scale"` ——
    #   加一个**别名参数**（如 `minute_scale=5`）就能绕过。改为「**任何含 scale 的形参名**」。
    offenders = [n for n in inspect.signature(ds_mod.DataSource.intraday).parameters
                 if "scale" in n.lower()]
    assert not offenders, (
        f"intraday 暴露了 scale 类参数 {offenders} —— 阈值必须随 scale 变，否则 5 分钟会恒判不完整；"
        f"若确实要暴露，必须同时让 INTRADAY_MIN_POINTS 按 scale 推导"
    )
    # ★ 收紧（code-reviewer 第 3 轮 B4）：`**kw` 也能绕过 —— `inspect.signature` 只看**显式形参名**，
    #   而 `def intraday(self, code, *, day=None, **kw)` 里的 `kw["scale"]` 是看不见的。
    kinds = inspect.signature(ds_mod.DataSource.intraday).parameters
    var_kw = [n for n, p in kinds.items() if p.kind is inspect.Parameter.VAR_KEYWORD]
    assert not var_kw, (
        f"`intraday` 有 `**{var_kw[0] if var_kw else 'kw'}` —— 它**能吞下任意关键字参数**"
        f"（含 `scale`），本判据看不见。若确实需要它，必须同时让 `INTRADAY_MIN_POINTS` 按 scale 推导"
    )


def test_W4b_intraday_url_scale_comes_from_the_constant_not_a_literal():
    """分时请求 URL 里的 `scale` 必须**来自常量**，不得写死字面量（code-reviewer 第 2 轮 **B-ii** 攻击）。

    ★ 攻击形态：`INTRADAY_SCALE` 与签名都不动，只在 **URL 里写死** `&scale=5`
      → 纯静态的「常量 + 签名」判据**看不见**，而真实请求已经变成 5 分钟口径
      → 每完整日只剩 48 点 → `INTRADAY_MIN_POINTS = 228` 恒判 `not complete`。

    两条断言（**静态 + 行为**，缺一不可）：
    1. **AST**：`_fetch_intraday` 里拼 URL 的 f-string 中，`scale=` 之后**必须是占位符**（引用常量），
       **不得**是数字字面量；
    2. **行为**：真的抓到请求 URL，断言 `scale=` 的值 **== `INTRADAY_SCALE`**。
       （行为那条是关键 —— 它能抓住「占位符引用了**另一个**常量」这类静态看不出的写法。）
    """
    import re

    from ptrade import datasource as ds_mod

    # ── 1) 静态：URL f-string 里 scale= 后面是占位符还是字面量 ──
    src = (ROOT / "ptrade" / "datasource.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fetch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_fetch_intraday":
            fetch = node
    assert fetch is not None, "DataSource._fetch_intraday 不见了（分时取数入口）"
    # ★ 只取**非 docstring** 的语句 —— `ast.get_source_segment` **包含 docstring**，
    #   而该函数 docstring 里恰好写着「新浪 ``scale=1`` 拉取…」。
    #   ⚠️ 本判据的第一版就因此误报（`literals = ['1']`）——
    #   **这正是 §8.3 形态⑤「判据不区分上下文 → 对说明文字报警」**，
    #   而我在把它写进契约后一小时内、在自己的新代码里复现了它。
    stmts = [s for s in fetch.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]
    body = "\n".join(ast.get_source_segment(src, s) or "" for s in stmts)
    literals = re.findall(r"scale=(\d+)", body)
    assert not literals, (
        f"`_fetch_intraday` 的 URL 里把 scale 写成了字面量 {literals} —— "
        f"必须用 `{{INTRADAY_SCALE}}`，否则改了常量而 URL 不变（B-ii 攻击形态）"
    )
    assert "scale={INTRADAY_SCALE}" in body or "scale={INTRADAY_SCALE}" in body.replace("{{", "{"), (
        "`_fetch_intraday` 的 URL 必须用 `scale={INTRADAY_SCALE}` 占位符拼"
    )

    # ── 2) 行为：抓到真实请求 URL，断言 scale 的值就是常量 ──
    seen: list[str] = []
    ds = ds_mod.DataSource.__new__(ds_mod.DataSource)   # 不跑 __init__，只借方法
    ds._http_get = lambda url, **kw: seen.append(url) or "var _=();"   # type: ignore[method-assign]
    try:
        ds._fetch_intraday("600519")
    except Exception:  # noqa: BLE001 - 空载荷会抛 DataUnavailable，这里只要 URL
        pass
    assert seen, "没有抓到任何请求 URL —— 断言会变成空守卫"
    m = re.search(r"[?&]scale=([^&]+)", seen[0])
    assert m, f"请求 URL 里没有 scale 参数：{seen[0][:120]}"
    assert m.group(1) == str(ds_mod.INTRADAY_SCALE), (
        f"请求 URL 的 scale={m.group(1)} 与常量 INTRADAY_SCALE={ds_mod.INTRADAY_SCALE} 不一致"
    )


# --------------------------------------------------------------------------- #
# W5 ★ `with_today_bar` 的两个前置必须都在（陈旧拒合成的双条件）
# --------------------------------------------------------------------------- #
def test_W5_with_today_bar_refuses_stale_snapshot_behaviorally():
    """`with_today_bar` 的**两个前置**必须都生效 —— 用**行为**证明，不用字符串。

    ★★ 为什么必须是行为判据（ultraqa F-1 / M1 实测，**两版都失败过**）：
      * 第 1 版：`"spot_asof" in body or "asof" in body` —— `asof` 是**形参名** ⇒ **恒真**；
      * 第 2 版：改成 `"spot_asof" in body` —— 仍**恒真**，因为 `spot_asof` 在函数里**还有一处赋值**
        （`spot_frame, spot_asof = self._spot_frame_raw()`）；把**比对**删掉后它照样出现 ⇒ 变异**仍绿**。
      ⇒ **字符串存在性无法表达「做了比对」**。只有真调一次、看结果，才能钉住。

    两条断言（**互为对照**，缺一即可能恒真）：
    1. **陈旧快照**（`spot_asof` ≠ `asof`）→ **不得**合成（`added is False`，行数不变）；
    2. **新鲜快照**（`spot_asof` == `asof` 且未停牌）→ **必须**合成（`added is True`，多一行）。

    为什么这条重要：实测 `quote()` 对**无数据日会返回上一收盘价** ——
    少了第 1 条，就会造出「用 10-09 收盘价做的 10-15 bar」（图上多一根假 bar，数字看不出问题），
    违反 `--offline` 的「不得编造」红线。
    """
    import pandas as pd

    from ptrade import datasource as ds_mod

    ds = ds_mod.DataSource.__new__(ds_mod.DataSource)     # 不跑 __init__，只借方法
    frame = pd.DataFrame([{
        "date": "2026-10-08", "open": 1.0, "high": 1.0, "low": 1.0,
        "close": 1.0, "volume": 1.0, "amount": 1.0,
    }])
    spot_row = pd.DataFrame([{
        "code": "600519", "name": "贵州茅台", "last": 9.9, "prev_close": 9.8, "volume": 100.0,
    }])
    ds.quote = lambda code, asof: {"last": 9.9, "suspended": False, "prev_close": 9.8}  # type: ignore[method-assign]

    # ① 陈旧快照 → **必须拒合成**
    ds._spot_frame_raw = lambda: (spot_row, "2026-10-09")   # type: ignore[method-assign]
    out, added = ds.with_today_bar(frame, "600519", "2026-10-15")
    assert added is False, (
        "陈旧快照（快照 asof=2026-10-09 ≠ asof=2026-10-15）**必须拒合成** —— "
        "否则会造出「用 10-09 收盘价做的 10-15 bar」（INV-57 / `--offline` 不得编造）"
    )
    assert len(out) == len(frame), f"陈旧快照下不得加行：{len(frame)} → {len(out)}"

    # ② 新鲜快照 → **必须合成**（正对照，防「恒不合成」也通过）
    ds._spot_frame_raw = lambda: (spot_row, "2026-10-15")   # type: ignore[method-assign]
    out2, added2 = ds.with_today_bar(frame, "600519", "2026-10-15")
    assert added2 is True, "新鲜快照 + 未停牌 **必须**合成当天 bar（否则这条判据可能恒不合成而假绿）"
    assert len(out2) == len(frame) + 1, f"新鲜快照应加一行：{len(frame)} → {len(out2)}"

    # ③ 停牌 → **不得**合成（第二个前置）
    ds._spot_frame_raw = lambda: (spot_row, "2026-10-15")   # type: ignore[method-assign]
    ds.quote = lambda code, asof: {"last": 9.9, "suspended": True, "prev_close": 9.8}  # type: ignore[method-assign]
    _out3, added3 = ds.with_today_bar(frame, "600519", "2026-10-15")
    assert added3 is False, "停牌（suspended=True）**不得**合成当天 bar"


# --------------------------------------------------------------------------- #
# W6 ★ 缓存写入守卫必须**接在生产调用点上**（本轮 CRITICAL 的直接回归守卫）
# --------------------------------------------------------------------------- #
def test_W6_min_rows_is_passed_at_the_production_call_site():
    """`_cached("industry_map", ...)` 必须真的传 `min_rows`（`INV-54`）。

    ★ 这是本轮 **CRITICAL 空守卫**的直接回归守卫：
      修前 `_cached(..., min_rows=)` 只在**测试里**被显式传参，
      生产调用点没传 → 16 行残缺缓存照样落盘 → **L 因子全量降级**（用户第 6 项）。

      `tests/test_datasource_v2.py::test_T_W1_5_production_call_sites_pass_min_rows`
      是更完整的版本（含检测器自测）；这里**再独立实现一次**（互相独立的两份实现任一漏掉都会被另一条抓到）。
    """
    tree = _parse("ptrade/datasource.py")
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if called != "_cached":
            continue
        args = [a for a in node.args]
        key = None
        if args and isinstance(args[0], ast.Constant):
            key = args[0].value
        if key != "industry_map":
            continue
        kw = {k.arg for k in node.keywords}
        found.append(node.lineno)
        assert "min_rows" in kw, (
            f"datasource.py:{node.lineno} 的 `_cached(\"industry_map\", ...)` **没传 min_rows** —— "
            f"`INV-54` 是空守卫，16 行残缺缓存会落盘并驻留 24h（L 因子全量降级）"
        )
    assert found, (
        "没有找到任何 `_cached(\"industry_map\", ...)` 生产调用点 —— "
        "本用例会因此**恒绿**（空守卫）。industry_map 的取数入口被改名/删了吗？"
    )


# --------------------------------------------------------------------------- #
# W7 ★ 守卫站点的**登记表 + 覆盖断言**（architect A9/A1；code-reviewer 的 WATCH 唯一理由）
# --------------------------------------------------------------------------- #
#: 全仓**全部** `except NetworkForbiddenInLock` 站点的登记表（`模块::函数`）。
#:
#: ★ **为什么需要它**：`test_inlock_network_guard.py` 有 **9 个行为站点**的参数化清单，
#:   而全仓有 **30 个**站点 —— 没有任何东西断言「新增站点必须被**看见**」。
#:   architect 的判据：**「覆盖面没人守」**，而 W1 自己给过反例
#:   （code-reviewer 在 `datasource.py:1653` 实测「判据绿 + 5 文件 100+ 用例全绿」）。
#:   → 本表把「新站点」变成**必须显式登记**的事件（宁可见的红，不要静默的绿）。
REGISTERED_GUARD_SITES: frozenset[str] = frozenset({
    "chart::_intraday_data",
    "chart::_intraday_payload",
    "chart::chart_payload",
    "cli::cmd_sync",
    "datasource::_breadth_counts",
    "datasource::_cached",
    "datasource::_has_quote",
    "datasource::_has_same_day_bar",
    "datasource::_has_window",
    "datasource::_http_get",
    "datasource::_latest_bar_day",
    "datasource::chart_asof",
    "datasource::daily",
    "datasource::index_daily",
    "datasource::industry_constituents",
    "datasource::industry_of",
    "datasource::latest_report_period",
    "datasource::latest_trade_asof",
    "datasource::quote",
    "datasource::universe",
    "datasource::with_today_bar",
    "engine::_backfill_unlock_dates",
    "engine::_evaluate",
    "engine::_float_shares",
    "engine::_is_margin_target",
    "engine::_is_short_target",
    "engine::_mark_prices",
    "engine::_quote",
    "service::resolve_asof",
    "watchlist::_has_market_data",
    "watchlist::add_to_watchlist",
    "watchlist::score_watchlist",
    "webapp::_analysis_background",
    "webapp::_work",
    "webapp::api_name_refresh",
    "webapp::api_search",
    "webapp::start_startup_maintenance",
})


def _scan_guard_sites() -> set[str]:
    """全域扫描 `ptrade/*.py`，返回**含守卫 handler 的函数**集合（`模块::函数`）。"""
    out: set[str] = set()
    for path in sorted((ROOT / "ptrade").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(func):
                if not isinstance(sub, ast.Try):
                    continue
                if any("NetworkForbiddenInLock" in _handler_types(h)[0] for h in sub.handlers):
                    out.add(f"{path.stem}::{func.name}")
                    break
    return out


def _behavioral_sites() -> set[str]:
    """从 `test_inlock_network_guard.py` 的 `parametrize` 清单**反向解析**行为覆盖站点。

    ★ **运行时解析而不是抄一份**：这样两份清单**不可能漂移**（抄一份就是新的复制源）。
    该文件用 `"engine._quote"`（点号）格式，本文件用 `"engine::_quote"` → 归一化。
    """
    src = (ROOT / "tests" / "test_inlock_network_guard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "parametrize":
            continue
        for arg in node.args:
            if not isinstance(arg, (ast.List, ast.Tuple)):
                continue
            for elt in arg.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and "." in elt.value:
                    mod, _, fn = elt.value.partition(".")
                    out.add(f"{mod}::{fn}")
    return out


def test_W7_every_guard_site_is_registered():
    """每个守卫站点都必须**登记在册**；且行为覆盖的站点必须**真的是其中的子集**。

    三条断言（缺一即失去意义）：
    1. **新站点必须登记**（`actual - REGISTERED` 非空 → 红）—— 这是 INV-58 的「覆盖面」那一半；
    2. **消失的站点也要报**（`REGISTERED - actual` 非空 → 红）—— 否则表会慢慢变成死数据；
    3. **非空洞**：站点总数有下限；且 `test_inlock_network_guard.py` 声称覆盖的站点
       **必须真的存在**（否则那张表本身就是空的）。

    ## ★ 站点键的**粒度**（code-reviewer 第 5 轮提出、Lead **实测**后写定）

    键是 **`模块::函数名`**（**不含类名前缀、不含嵌套路径**）。
    code-reviewer 推断「在**已登记函数内部的嵌套 `def`** 里新增 guard，站点名不变 ⇒ 漏检」——
    **Lead 实测（`.omx/probe/measure_w7_granularity.py`）：不成立。**
    在 `latest_trade_asof`（已登记）内部插入一个新的嵌套 guard 函数后：
    ```
    [变异] 1 failed → 「新增了未登记的守卫站点：['datasource::_brand_new_nested']」
    ```
    原因：本函数用 `ast.walk(tree)` 遍历，**嵌套 `FunctionDef` 也会被作为独立项产出**
    （现有 30 个站点里的 `datasource::_has_quote` / `_has_window` 就是这样来的 —— 它们**本身**是
    `latest_trade_asof` 的嵌套函数）。

    ⚠️ **真正没覆盖的窄场景（诚实声明）**：新嵌套函数的名字**恰好与同模块某个已登记键同名**
    （如再加一个 `_has_quote`）→ 键不变、不会红。**判为可接受**：这需要刻意重名，
    且「函数级登记」的语义本身就是「这个模块里有这么个函数在处理守卫错误」。
    若要更严，把键改成限定名（`模块::外层.内层`）即可 —— 已记入 backlog A9/A1。
    """
    actual = _scan_guard_sites()
    registered = set(REGISTERED_GUARD_SITES)

    unregistered = sorted(actual - registered)
    disappeared = sorted(registered - actual)
    assert not unregistered, (
        f"**新增了未登记的守卫站点**：{unregistered} ——\n"
        f"  请把它们加进 `REGISTERED_GUARD_SITES`，并判断是否需要行为判据\n"
        f"  （`test_inlock_network_guard.py` 的参数化清单）。**不加就没人知道它们存在。**"
    )
    assert not disappeared, (
        f"登记的守卫站点**消失了**：{disappeared} ——\n"
        f"  若是重构移走，请同步更新本表；**不要**让表变成死数据。"
    )
    assert len(actual) >= 25, (
        f"守卫站点只剩 {len(actual)} 个（下限 25）—— 扫描坏了，还是守卫被大删了？"
    )

    behavior = _behavioral_sites()
    assert behavior, (
        "`test_inlock_network_guard.py` 的参数化清单**解析为空** —— "
        "这两条判据会因此恒绿（空守卫）。是不是该文件被改名/改结构了？"
    )
    orphan = sorted(behavior - actual)
    assert not orphan, (
        f"行为清单里的站点在代码里**不存在**：{orphan} —— 该清单已经与代码脱节（死数据）"
    )
    # 诚实记录覆盖率（不设阈值断言 —— 覆盖率提升是 backlog A9/A1 的持续项）
    print(f"\n  守卫站点 {len(actual)} 个；其中有行为判据的 {len(behavior)} 个"
          f"（{len(behavior) * 100 // len(actual)}%）")
def test_file_itself_is_not_vacuous():
    """本文件的判据**名单必须精确匹配**，且**不得用 skip 掩盖未实现**。

    ⚠️ **自指陷阱**（第一版就踩了）：我原本写 `getsource(me).count("pytest.skip(")`，
    结果**数到了自己这行源码**（那个字面量就在断言里）→ 永远为 1 → 恒红。
    与 A13 那次「规则正文含被禁字符串」是**同一个坑**。
    → 改为**按 AST 数真正的 `pytest.skip(...)` 调用**，而不是数文本。

    ★★ **收紧（architect 第 3 轮）**：第一版只断言「总数 ≥6」——
      于是**删掉一条判据、再加两条**也能过，「哪个机制被覆盖」**没有任何断言**。
      这正是 `INV-58` 要防的形态**在判据文件自己身上**复发。
      → 改成**精确集合**：名单**必须完全等于**下面这个集合；
      少一条（覆盖丢失）或多一条（新机制没登记）**都会红**。
    """
    import ast as _ast
    import inspect as _inspect
    import sys as _sys

    EXPECTED = {
        "test_W1_guard_error_is_not_swallowed_by_dataunavailable_handlers",
        "test_W2_both_network_exits_call_the_single_net_guard",
        "test_W3_period_list_has_a_single_source_of_truth",
        "test_W3b_cli_period_choices_come_from_the_single_source",
        "test_W4_intraday_min_points_is_derived_from_the_240_minute_session",
        "test_W4b_intraday_url_scale_comes_from_the_constant_not_a_literal",
        "test_W5_with_today_bar_refuses_stale_snapshot_behaviorally",
        "test_W6_min_rows_is_passed_at_the_production_call_site",
        "test_W7_every_guard_site_is_registered",
    }

    me = _sys.modules[__name__]
    actual = {n for n, o in vars(me).items() if n.startswith("test_") and callable(o)}
    actual.discard("test_file_itself_is_not_vacuous")

    missing = EXPECTED - actual
    extra = actual - EXPECTED
    assert not missing, (
        f"接线判据**丢失**：{sorted(missing)} —— 这些机制现在**没有**接线断言了（INV-58 会静默复发）"
    )
    assert not extra, (
        f"新增了未登记的判据：{sorted(extra)} —— 请把它加进本测试的 `EXPECTED` 集合"
        f"（**新增机制必须显式登记**，否则「覆盖不强制」这个弱点会回来）"
    )

    tree = _ast.parse(_inspect.getsource(me))
    skips = [
        node.lineno for node in _ast.walk(tree)
        if isinstance(node, _ast.Call)
        and getattr(node.func, "attr", None) == "skip"
        and getattr(getattr(node.func, "value", None), "id", None) == "pytest"
    ]
    assert not skips, f"本文件不得用 pytest.skip 掩盖未实现（第 {skips} 行）"
