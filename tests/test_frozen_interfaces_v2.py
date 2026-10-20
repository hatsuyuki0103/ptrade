"""A12 冻结接口的**签名快照**（W0-1 / W0-4；PRD §6 A12 的 15 符号表 = 唯一真源）。

本文件是「接口冻结」的机器可查产物：谁改签名 / 改默认值 → 红。
它存在的理由：R1/R2/R3 连续三轮都在「冻结了签名、却没核对调用契约」上翻车
（`use_qfq` → `fixture_key` → `chart_payload` 默认值）。

## 三种状态（`status` 字段）
* ``strict``       —— 符号已落地且**等于冻结目标**：逐字钉形参名 / 种类 / 默认值 / 返回注解。
* ``transitional`` —— 符号已存在但**尚未升到目标**（W1/W2 待实现新增参数）：
  断言签名 ∈ {**当前基线**, **冻结目标**}。任何**第三种**形态 → 红。
  目标落地后本项自然继续绿（届时可把基线一项删掉）。
* ``pending``      —— 符号**尚不存在**：`pytest.skip` 并**把目标签名写进 skip 理由**；
  实现一落地，skip 消失 → 自动转为严格断言（「实现落地即转绿」）。

## 不做什么
* ⚠️ **不钉「调用点数量」**（Architect 第 3 轮）：W2/W5 的合法新增会假红。
  调用点约束改由 AST **允许/禁止位置清单**表达（本文件下半部分）。
* ⚠️ 注解在 ``datasource.py`` 里是**字符串**（文件头有 `from __future__ import annotations`），
  所以一律用 ``str(annotation)`` 比较，**不得**用 ``is str``。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

EMPTY = inspect.Parameter.empty


# --------------------------------------------------------------------------- #
# 冻结表（A12 的 15 个符号）
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Frozen:
    """一个冻结符号。``params`` 为 (名字, 种类, 默认值) 三元组序列。"""

    module: str
    symbol: str
    status: str
    params: tuple[tuple[str, str, object], ...]
    baseline: tuple[tuple[str, str, object], ...] | None = None
    returns: str | None = None
    owner: str | None = None  # 取属性的宿主（datasource 的多数符号挂在 DataSource 类上）
    note: str = ""

    def describe(self) -> str:
        def fmt(items: tuple[tuple[str, str, object], ...]) -> str:
            return "(" + ", ".join(
                f"{n}" if k == "POSITIONAL_OR_KEYWORD" and d is EMPTY else f"{n}={d!r}"
                for n, k, d in items
            ) + ")"

        text = f"{self.module}.{self.symbol}{fmt(self.params)}"
        if self.baseline is not None:
            text += f"  [基线可选: {fmt(self.baseline)}]"
        if self.returns:
            text += f" -> {self.returns}"
        return text


POS = "POSITIONAL_OR_KEYWORD"
KW = "KEYWORD_ONLY"


def _p(*rows: tuple[str, object]) -> tuple[tuple[str, str, object], ...]:
    return tuple((name, kind, default) for name, kind, default in rows)  # type: ignore[misc]


# ── 已在目标形态（strict）────────────────────────────────────────────────── #
_STRICT: tuple[Frozen, ...] = (
    Frozen(
        module="datasource", symbol="_write_entry", owner="DataSource", status="strict",
        params=_p(("directory", POS, EMPTY), ("key", POS, EMPTY), ("payload", POS, EMPTY)),
        returns="str",
        note="3 参 + -> str（PRD F2 明令不得改；实测调用点 14/15 处）",
    ),
)

# ── ★ R2（architect）：基线已**删除** —— 这 4 个符号现在**只接受目标签名** ──── #
#
# 修前它们带 `baseline=`，而消费端的语义是「**基线 ∪ 目标都接受**」（见下方 `allowed = ...`）
# → `chart_payload` 少了 `include_today`、`_cached` 少了 `min_rows` **仍然通过**。
# 而这 4 个（`_cached` / `chart_payload` / `render_chart` / `build_chart`）**恰是 R1/R2 翻车的那批签名** ——
# 冻结机制在它们的对象上**不生效**。实现早已到位，故直接删基线收紧。
_FORMERLY_TRANSITIONAL: tuple[Frozen, ...] = (
    Frozen(
        module="datasource", symbol="_cached", owner="DataSource", status="strict",
        params=_p(("self", POS, EMPTY), ("key", POS, EMPTY), ("ttl", POS, EMPTY),
                  ("fetch", POS, EMPTY), ("fixture_key", POS, None), ("min_rows", KW, None)),
        note="F2：fixture_key 必须留在第 4 位；min_rows 为关键字-only（INV-54）",
    ),
    Frozen(
        module="chart", symbol="chart_payload", status="strict",
        params=_p(("cfg", POS, EMPTY), ("ds", POS, EMPTY), ("code", POS, EMPTY),
                  ("asof", POS, EMPTY), ("use_qfq", POS, True), ("include_today", KW, False)),
        returns="dict",
        note="F3：include_today **默认必须 False**（Architect 第 2 轮 N-2）",
    ),
    Frozen(
        module="chart", symbol="render_chart", status="strict",
        params=_p(("cfg", POS, EMPTY), ("ds", POS, EMPTY), ("code", POS, EMPTY),
                  ("asof", POS, EMPTY), ("period", POS, "daily"), ("use_qfq", POS, True),
                  ("analysis", POS, None), ("include_today", KW, True)),
        returns="tuple[str, dict]",
        note="F3：保留 use_qfq（P0-3，5 个调用点兼容）",
    ),
    Frozen(
        module="chart", symbol="build_chart", status="strict",
        params=_p(("cfg", POS, EMPTY), ("ds", POS, EMPTY), ("code", POS, EMPTY),
                  ("asof", POS, EMPTY), ("period", POS, "daily"), ("out", POS, None),
                  ("use_qfq", POS, True), ("analysis", POS, None), ("include_today", KW, True)),
        returns="str",
        note="F3：必须把 include_today 与 use_qfq 都透传给 render_chart",
    ),
)

# ── 尚未实现（pending：skip + 目标签名写进理由）──────────────────────────── #
_PENDING: tuple[Frozen, ...] = (
    Frozen(module="datasource", symbol="_net_guard", owner="DataSource", status="pending",
           params=_p(("self", POS, EMPTY), ("kind", POS, "akshare")),
           note="INV-53 守卫唯一出口；必须保住 _ak() 的两条原文案（N-7）"),
    Frozen(module="datasource", symbol="_http_get", owner="DataSource", status="pending",
           params=_p(("self", POS, EMPTY), ("url", POS, EMPTY), ("referer", KW, None),
                     ("headers", KW, None), ("encoding", KW, "utf-8"), ("timeout", KW, 10.0)),
           returns="str", note="唯一裸 HTTP 出口；限频 1s、失败不重试超过 1 次"),
    Frozen(module="datasource", symbol="chart_asof", owner="DataSource", status="pending",
           params=_p(("self", POS, EMPTY), ("code", POS, EMPTY)), returns="str",
           note="INV-52：只许 cmd_chart / api_chart 调用（AST 白名单）"),
    Frozen(module="datasource", symbol="with_today_bar", owner="DataSource", status="pending",
           params=_p(("self", POS, EMPTY), ("frame", POS, EMPTY), ("code", POS, EMPTY),
                     ("asof", POS, EMPTY)),
           returns="tuple[pd.DataFrame, bool]", note="INV-57：陈旧快照拒合成"),
    Frozen(module="datasource", symbol="latest_report_period", owner="DataSource", status="pending",
           params=_p(("self", POS, EMPTY)), returns="str",
           note="A6 本体；REPORT_MIN_ROWS=1000，全不达标 → DataUnavailable"),
    Frozen(module="datasource", symbol="intraday", owner="DataSource", status="pending",
           params=_p(("self", POS, EMPTY), ("code", POS, EMPTY), ("day", KW, None)),
           returns="dict", note="唯一新数据源；vol/amount 语义钉死为增量，avg=累计 VWAP"),
    Frozen(module="chart", symbol="render_intraday", status="pending",
           params=_p(("cfg", POS, EMPTY), ("ds", POS, EMPTY), ("code", POS, EMPTY),
                     ("day", POS, None)),
           returns="tuple[str, dict]",
           note="INV-56：独立渲染分支 + 独立落盘命名 intraday_<code>_<day>.html"),
    Frozen(module="chart", symbol="legend_groups", status="pending",
           params=_p(("payload", POS, EMPTY)), note="A5：分组数 == yAxis 面板数"),
    Frozen(module="chart", symbol="legend_budget", status="pending",
           params=_p(("item_width", POS, 14), ("item_gap", POS, 10), ("label_width", POS, None),
                     ("nominal_panel_width", POS, 900)),
           note="A5：常量钉在签名默认值上（14/10/900）；label_width 默认 None 且可推导"),
    Frozen(module="service", symbol="market_open", status="pending",
           params=_p(("now", POS, EMPTY), ("trade_dates", POS, EMPTY)),
           returns="bool | None",
           note="F5：纯计算、绝不自取数；日历不覆盖今天 → None（三态可观测）"),
)

FROZEN_SYMBOLS: tuple[Frozen, ...] = _STRICT + _FORMERLY_TRANSITIONAL + _PENDING

#: A13 的 (f) 检查解析本列表，断言它**等于** PRD §6 A12 表（互为单一真源）。
FROZEN_NAMES: list[tuple[str, str]] = [(f.module, f.symbol) for f in FROZEN_SYMBOLS]

#: A13 用来判定「规范符号」的集合（被豁免区不得出现它们）
NORMATIVE_SYMBOLS: frozenset[str] = frozenset(symbol for _, symbol in FROZEN_NAMES)

#: **PN 补丁（task-33 / PRD R6）新增符号的登记表**：`module → (symbol, 预期签名)`。
#: 用途：新符号**必须**被登记（否则「悄悄新增一个公共符号」无人察觉）；
#: ⚠️ **不并入上面的 A12 15 符号表** —— 那张表的行数与
#: `.omx/plans/prd-ptrade-web-v2.md` §6 A12 表**互为真源**（A13 的 (f) 断言两边相等），
#: 往它里面加一行会让 (f) 变红。所以新符号走这张单独的表。
PN_ADDED_SYMBOLS: dict[str, tuple[str, str]] = {
    # ⚠️ 注解是**字符串**（datasource.py 有 `from __future__ import annotations`）→
    #    期望值必须写成 `'str'`（带引号），否则比对必红。
    "datasource": ("name_of", "(self, code: 'str') -> 'str'"),
}


# --------------------------------------------------------------------------- #
# 解析符号
# --------------------------------------------------------------------------- #
def resolve(frozen: Frozen):
    """模块级优先，其次宿主类（datasource 的多数符号挂在 DataSource 上）。"""
    module = importlib.import_module(f"ptrade.{frozen.module}")
    obj = getattr(module, frozen.symbol, None)
    if obj is not None:
        return obj
    owner = getattr(module, frozen.owner, None) if frozen.owner else None
    if owner is not None:
        return getattr(owner, frozen.symbol, None)
    return None


def actual_params(func) -> tuple[tuple[str, str, object], ...]:
    sig = inspect.signature(func)
    return tuple(
        (name, p.kind.name, p.default) for name, p in sig.parameters.items()
    )


def actual_return(func) -> str:
    return str(inspect.signature(func).return_annotation)


@pytest.mark.parametrize("frozen", FROZEN_SYMBOLS, ids=lambda f: f"{f.module}.{f.symbol}")
def test_frozen_signature(frozen: Frozen):
    """逐符号钉形参名 / 种类 / 默认值（+ 返回注解，若 PRD 有声明）。"""
    func = resolve(frozen)
    if func is None:
        pytest.skip(f"待新增：{frozen.describe()}  —— {frozen.note}")
    params = actual_params(func)
    allowed = [frozen.params] + ([frozen.baseline] if frozen.baseline else [])
    assert params in allowed, (
        f"{frozen.module}.{frozen.symbol} 签名被改动：\n"
        f"    实际 = {params}\n"
        f"    冻结 = {frozen.params}"
        + (f"\n    基线 = {frozen.baseline}" if frozen.baseline else "")
        + f"\n    （{frozen.note}）"
    )
    if frozen.returns:
        assert actual_return(func) == frozen.returns, (
            f"{frozen.module}.{frozen.symbol} 返回注解被改动："
            f"实际 {actual_return(func)!r}，冻结 {frozen.returns!r}"
        )


def test_frozen_table_is_complete_and_consistent():
    """表自身必须自洽：15 个符号、名字唯一、status 合法、FROZEN_NAMES 与表一致。"""
    assert len(FROZEN_SYMBOLS) == 15, f"A12 是 15 符号表，实际 {len(FROZEN_SYMBOLS)}"
    assert len(set(FROZEN_NAMES)) == 15, "符号名重复"
    assert {f.status for f in FROZEN_SYMBOLS} <= {"strict", "transitional", "pending"}
    assert FROZEN_NAMES == [(f.module, f.symbol) for f in FROZEN_SYMBOLS]
    # 每个 datasource 符号要么在类上要么在模块上能找到（或明确 pending）
    for frozen in FROZEN_SYMBOLS:
        if frozen.status != "pending":
            assert resolve(frozen) is not None, f"{frozen.symbol} 标为非 pending 但解析不到"


def test_frozen_names_are_the_a13_source_of_truth():
    """A13 的 (f) 会解析 ``FROZEN_SYMBOLS``；这里保证它可被 AST 解析（名字集合可提取）。

    注意：本文件用的是**带注解的赋值**（``FROZEN_SYMBOLS: tuple[Frozen, ...] = ...``）→
    AST 节点是 ``AnnAssign`` 而非 ``Assign``；A13 必须同时认这两种（否则会静默解析为空列表，
    正是「解析 fail-open」那一类缺陷）。
    """
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    found = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [getattr(t, "id", None) for t in node.targets]
        elif isinstance(node, ast.AnnAssign):
            targets = [getattr(node.target, "id", None)]
        else:
            continue
        if "FROZEN_SYMBOLS" in targets and node.value is not None:
            found = True
    assert found, "A13 依赖模块级 FROZEN_SYMBOLS 赋值（Assign 或 AnnAssign 均可）"

    # 名字集合必须能从 AST 静态提取（不能靠 import 才能拿到）
    names: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Frozen":
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            mod = getattr(kwargs.get("module"), "value", None)
            sym = getattr(kwargs.get("symbol"), "value", None)
            if mod and sym:
                names.append((mod, sym))
    assert len(names) == 15, f"AST 静态提取到 {len(names)} 个符号，期望 15"
    assert tuple(names) == tuple(FROZEN_NAMES), "AST 提取的名字序列与 FROZEN_NAMES 不一致"


# --------------------------------------------------------------------------- #
# AST 允许/禁止位置清单（替代「钉调用点数量」）
# --------------------------------------------------------------------------- #
def _functions(path: pathlib.Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls_in(func: ast.AST, name: str) -> list[int]:
    """函数体内对 ``name(...)`` 的调用行号。"""
    hits: list[int] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        called = getattr(target, "attr", None) or getattr(target, "id", None)
        if called == name:
            hits.append(node.lineno)
    return hits


#: ★ **R3（architect 第 2 轮加固）**：白名单从「按**函数名**放行」改成「**(模块, 函数) 配对**」。
#:
#: 演进过程（每一版都被攻破过，这是第 3 版）：
#: v1 `{"cli.py": {"cmd_chart"}, "webapp.py": {"api_chart"}}` —— **按文件限定扫描范围** →
#:    新增第三个看盘路径若落在**别的模块**就完全不被看见。
#: v2 `{"viewer_asof", "api_chart"}` —— 全域扫描但**只按函数名** →
#:    architect **实测**：在第三个模块里定义一个**同名** `viewer_asof` 就能调 `chart_asof` 而不被检出
#:    （它在 `ptrade/` 副本上用 `sneaky.py::viewer_asof` 验证过）。
#: v3（本版）`{(模块, 函数)}` —— 名字与位置**都要对**。
#:
#: ★ R4 收敛后**只剩一个合法调用点**：`service.viewer_asof`
#:   （`cli.cmd_chart` 与 `webapp.api_chart` 都已改为调用它，`webapp.py` 里 `chart_asof` 现为 **0** 处）。
CHART_ASOF_ALLOWED: set[tuple[str, str]] = {("service.py", "viewer_asof")}

#: 这些函数体内**绝不许**出现 `chart_asof`（下单/策略路径）。全域生效（按函数名）。
CHART_ASOF_FORBIDDEN = {
    "cmd_order", "cmd_settle", "cmd_close", "cmd_plan", "cmd_scan", "cmd_recommend",
    "api_order", "api_close", "api_settle", "_resolve_asof_query", "prewarm_before_write",
}


def test_chart_asof_only_in_viewer_functions():
    """INV-52：`chart_asof(` 只许出现在**看盘**函数体内，且必须是**白名单里的那一个**。

    ★ **全域扫描 `ptrade/*.py`**（R3）：定义处是 `FunctionDef` 而不是 `ast.Call`，
      所以 `datasource.py` 的定义**不会自触发**（`_calls_in` 只看 `Call`）。
    ★ **非空洞对照**：断言白名单函数**确实被找到**。否则「没有任何调用点」也会让本用例通过，
      而「没人调用它了」与「守卫有效」不可区分（= 空守卫）。
    ★ **配对而非名字**（R3 加固）：`(模块, 函数)` 都要匹配 —— 防「换个模块起同名函数」。
    ★ 诚实声明：`getattr(ds, "chart_asof")` / 局部别名可绕过 —— 这是防误用，不是防对抗。
    """
    problems: list[str] = []
    seen: list[tuple[str, str]] = []
    for path in sorted((ROOT / "ptrade").glob("*.py")):
        for func_name, node in _functions(path).items():
            hits = _calls_in(node, "chart_asof")
            if hits:
                seen.append((path.name, func_name))
                if (path.name, func_name) not in CHART_ASOF_ALLOWED:
                    problems.append(
                        f"{path.name}::{func_name} 第 {hits} 行调用了 chart_asof"
                        f"（白名单只允许 {sorted(CHART_ASOF_ALLOWED)}）"
                    )
            if func_name in CHART_ASOF_FORBIDDEN and hits:
                problems.append(
                    f"{path.name}::{func_name}（下单/策略路径）第 {hits} 行出现 chart_asof"
                )
    assert not problems, "INV-52 违规：\n  " + "\n  ".join(problems)

    # ★ 非空洞 + **R4 收敛验收**：必须恰好是 `service.viewer_asof` 这一处。
    assert seen, (
        "INV-52 的白名单是空的：全域扫描没有找到任何 chart_asof 调用点 —— "
        "本用例会因此**恒绿**（空守卫）。看盘路径是不是被删了？"
    )
    assert seen == [("service.py", "viewer_asof")], (
        f"chart_asof 的调用点应**收敛为唯一入口** `service.viewer_asof`，实际 {seen} —— "
        f"出现第二份实现就说明策略又被复制了（architect R4）"
    )
    # 尚未实现时 seen 为空 → 打印（不是失败），实现落地后自动受约束
    print(f"  chart_asof 调用点：{seen or '（尚未实现）'}")


NETWORK_PRIMITIVES = ("urlopen", "HTTPSConnection", "HTTPConnection", "create_connection", "socket.socket", "requests.")


def test_network_primitives_only_inside_http_get():
    """INV-53：`ptrade/*.py` 内的网络原语只允许出现在 `datasource._http_get` 体内。"""
    violations: list[str] = []
    datasource_path = ROOT / "ptrade" / "datasource.py"
    allowed_span = (0, -1)
    funcs = _functions(datasource_path)
    node = funcs.get("_http_get")
    if node is not None and node.end_lineno:
        allowed_span = (node.lineno, node.end_lineno)
    for path in sorted((ROOT / "ptrade").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for sub in ast.walk(tree):
            hit = None
            if isinstance(sub, ast.Call):
                func = sub.func
                hit = getattr(func, "attr", None) or getattr(func, "id", None)
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    chain = f"{func.value.id}.{func.attr}"
                    if chain in NETWORK_PRIMITIVES:
                        hit = chain
            elif isinstance(sub, ast.Name) and sub.id in ("urlopen", "HTTPSConnection", "HTTPConnection", "create_connection"):
                hit = sub.id
            if hit and hit in NETWORK_PRIMITIVES:
                if allowed_span[0] <= sub.lineno <= allowed_span[1]:
                    continue
                violations.append(f"{path.name}:{sub.lineno} {hit}")
    assert not violations, "INV-53 违规（网络原语出现在 _http_get 之外）：\n  " + "\n  ".join(violations)


# --------------------------------------------------------------------------- #
# PN 补丁新增符号的登记判据（PRD R6）
# --------------------------------------------------------------------------- #
def test_pn_added_symbols_are_registered_and_match():
    """**新符号必须登记**（PRD R6）：`PN_ADDED_SYMBOLS` 里的每个符号都要真的存在，
    且签名与登记值一致 —— 防止「悄悄加了一个公共符号」无人察觉。

    ⚠️ 与 A12 的 15 符号表**分开**：那张表与 PRD §6 A12 互为真源（A13 的 (f) 会比对行数）。
    """
    import importlib
    import inspect

    assert PN_ADDED_SYMBOLS, "登记表为空 → 本判据是空的"
    for module_name, (symbol, expected) in PN_ADDED_SYMBOLS.items():
        module = importlib.import_module(f"ptrade.{module_name}")
        obj = getattr(module, symbol, None)
        if obj is None:
            owner = getattr(module, "DataSource", None)
            obj = getattr(owner, symbol, None) if owner is not None else None
        assert obj is not None, f"{module_name}.{symbol} 已登记但实现里找不到"
        assert str(inspect.signature(obj)) == expected, (
            f"{module_name}.{symbol} 签名与登记不符："
            f"实际 {inspect.signature(obj)}，登记 {expected}"
        )
