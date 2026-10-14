"""N7 的行为级验证：**真的在 node 里执行** `renderDivergence`，断言 DOM 变化。

## 为什么必须这么做

第一版我只断言 `webui.py` 里出现 `renderDivergence` / `账实分叉` 等关键字 ——
**变异实验证明那是假守卫**：把 `renderDivergence` 改成 `return;`（no-op）后用例**仍绿**。

本文件的做法：从 `APP_HTML` 里抽出 `$`/`esc`/`renderDivergence`，配一个最小假 DOM，
**真的执行**，断言：

* `diverged: true`  → 横幅显示、class 含 `is-diverged`、文案含「账实分叉」
* `diverged: null`  → 横幅显示、class 含 `is-unknown`、文案含「无法判定」
* `diverged: false` → 横幅 `display: none`

并**反向自检**：断言「三种输入的输出两两不同」（否则一个恒显示的实现也能全过）。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, r"D:\Ai投资")

from ptrade import webui  # noqa: E402

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 才能执行 JS 行为验证")


def _extract_js() -> str:
    """从 `APP_HTML` 抽出脚本，并保留 `$`/`esc`/`renderDivergence` 三个定义。

    **用字符串定位而不是正则**：`$` 在正则里是行尾锚点，
    `rf"function {name}\\(" ` 对 `name="$"` 永远匹配不到（我第一版就踩了这个，
    报「抽取不到 function $」）。这里改成 `js.find("function $(")` + 花括号配平。
    """
    html = webui.APP_HTML
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "APP_HTML 里找不到 <script>"
    js = m.group(1)

    def grab(header: str) -> str:
        start = js.find(header)
        assert start >= 0, f"抽取不到 {header!r}"
        # 花括号配平（跳过字符串里的花括号不必要 —— 这几个函数体内没有含 } 的字符串字面量）
        depth = 0
        for i in range(start, len(js)):
            ch = js[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return js[start:i + 1]
        raise AssertionError(f"{header!r} 的花括号没有配平")

    return "\n".join(
        grab(h) for h in ("function $(id) {", "function esc(", "function renderDivergence(")
    )


RUNNER = textwrap.dedent("""
    "use strict";
    // 假 DOM：页面里的 `$` 就是 `document.getElementById(id)`，所以只需提供它。
    var els = {};
    function mkEl() {
      return {style: {}, className: "", innerHTML: ""};
    }
    var document = {
      getElementById: function (id) { return els[id] || null; }
    };
    %(js)s

    function probe(payload) {
      els["divergence"] = mkEl();
      renderDivergence(payload);
      var e = els["divergence"];
      return {
        display: e.style.display === undefined ? null : e.style.display,
        className: e.className,
        text: e.innerHTML
      };
    }

    console.log(JSON.stringify({
      diverged: probe({ledger_consistency: {diverged: true, ledger_lines: 5, recorded: 4}}),
      unknown:  probe({ledger_consistency: {diverged: null, reason: "拿不到写锁"}}),
      ok:       probe({ledger_consistency: {diverged: false, ledger_lines: 4, recorded: 4}}),
      missing:  probe({})
    }));
""")


def _run(payload_js: str) -> dict:
    src = RUNNER % {"js": payload_js}
    p = subprocess.run([NODE, "-e", src], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert p.returncode == 0, f"node 退出 {p.returncode}\n{p.stderr[:800]}"
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def rendered() -> dict:
    return _run(_extract_js())


def test_n7_banner_shown_on_divergence(rendered):
    """**分叉已确认**：横幅必须显示、标红、文案说清后果。"""
    r = rendered["diverged"]
    assert r["display"] == "block", f"分叉时横幅必须显示（实测 display={r['display']!r}）"
    assert "is-diverged" in r["className"], r
    assert "账实分叉" in r["text"], r["text"][:200]
    # 必须点明「重试可能重复成交」—— 这是用户真正要知道的后果
    assert "重复成交" in r["text"], r["text"][:300]


def test_n7_banner_shown_when_undetermined(rendered):
    """**无法判定**：显示但不是红色，且**不得**说成「一致」。"""
    r = rendered["unknown"]
    assert r["display"] == "block", f"「无法判定」必须显示（实测 {r['display']!r}）"
    assert "is-unknown" in r["className"], r
    assert "无法判定" in r["text"], r["text"][:200]


def test_n7_banner_hidden_when_consistent(rendered):
    """**一致**：横幅必须隐藏（否则一进页面就报错，用户会脱敏）。"""
    r = rendered["ok"]
    assert r["display"] == "none", f"一致时应隐藏（实测 {r['display']!r}）"
    assert r["text"] == "", "一致时不应留下文案"


def test_n7_three_states_are_actually_distinguished(rendered):
    """**反向自检**：三态（+ 缺字段）的输出必须两两不同。

    否则「恒显示同一个红条」也能通过上面那三条各自的部分断言。
    """
    sigs = {
        "diverged": (rendered["diverged"]["display"], rendered["diverged"]["className"]),
        "unknown": (rendered["unknown"]["display"], rendered["unknown"]["className"]),
        "ok": (rendered["ok"]["display"], rendered["ok"]["className"]),
    }
    assert len(set(sigs.values())) == 3, f"三态在 DOM 上不可区分：{sigs}"
    # 缺字段（响应里没有 ledger_consistency）必须按「无法判定」处理，不得静默隐藏
    assert rendered["missing"]["display"] == "block", (
        "响应缺 ledger_consistency 时应按「无法判定」显示，而不是静默隐藏"
    )
    assert rendered["missing"]["display"] == rendered["unknown"]["display"]
