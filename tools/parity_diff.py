"""跨语言对拍：比对两份语料，逐字节报差异。

用法::

    python tools/parity_diff.py parity.go.json parity.py.json

退出码 0 = 完全一致；1 = 有差异（可直接当 CI 门禁）。

会报三类问题，**任何一类都算失败**：

1. **用例集不一致** —— 一边有、另一边没有。多半是有人只在一边加了用例，
   那正是这个工具该拦下的：语料清单是两边共同的规格。
2. **SQL 不一致** —— 逐字符定位第一个差异点，并把上下文打出来。
   一个反引号、一个空格都算。
3. **参数不一致** —— 参数已经按同一套规则归一成字符串（见发射器里的 parity_arg）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "cases" not in data:
        raise SystemExit(f"{path} 不像语料文件（没有 cases 字段）")
    return data


def first_diff(a: str, b: str) -> int:
    """返回第一个不同的字符下标；完全相同返回 -1。"""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    if len(a) != len(b):
        return min(len(a), len(b))
    return -1


def caret_context(a: str, b: str, pos: int, width: int = 60) -> str:
    """把两边在差异点附近的片段对齐打印，并用 ^ 指出位置。"""
    start = max(0, pos - width // 2)
    end = start + width
    pad = pos - start
    return (
        f"      go: ...{a[start:end]}...\n"
        f"      py: ...{b[start:end]}...\n"
        f"          {' ' * (pad + 3)}^ 第 {pos} 个字符起不同"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="比对两份跨语言对拍语料")
    ap.add_argument("go_file", help="Go 侧产物")
    ap.add_argument("py_file", help="Python 侧产物")
    ap.add_argument("--max-report", type=int, default=20, help="最多报告多少条差异")
    args = ap.parse_args(argv)

    go = load(args.go_file)
    py = load(args.py_file)

    if go.get("corpus_version") != py.get("corpus_version"):
        print(f"❌ 语料版本不一致: go={go.get('corpus_version')} py={py.get('corpus_version')}")
        return 1

    go_cases = {c["name"]: c for c in go["cases"]}
    py_cases = {c["name"]: c for c in py["cases"]}

    problems: list[str] = []

    # 1) 用例集
    only_go = sorted(set(go_cases) - set(py_cases))
    only_py = sorted(set(py_cases) - set(go_cases))
    for name in only_go:
        problems.append(f"[用例集] 只有 Go 有: {name}")
    for name in only_py:
        problems.append(f"[用例集] 只有 Python 有: {name}")

    # 2) 顺序（语料是有序规格，顺序不同说明枚举逻辑分叉了）
    go_order = [c["name"] for c in go["cases"] if c["name"] in py_cases]
    py_order = [c["name"] for c in py["cases"] if c["name"] in go_cases]
    if go_order != py_order:
        problems.append("[顺序] 两边共有用例的枚举顺序不同——说明发射逻辑已经分叉")

    # 3) 内容
    for name in [c["name"] for c in go["cases"] if c["name"] in py_cases]:
        g, p = go_cases[name], py_cases[name]
        if g["sql"] != p["sql"]:
            pos = first_diff(g["sql"], p["sql"])
            problems.append(
                f"[SQL] {name}\n{caret_context(g['sql'], p['sql'], pos)}"
            )
        if g["args"] != p["args"]:
            problems.append(f"[参数] {name}\n      go: {g['args']}\n      py: {p['args']}")

    total = len(go_cases | py_cases)
    if not problems:
        print(f"✅ 逐字节一致：{len(go_cases)} 条用例全部相同")
        return 0

    print(f"❌ 发现 {len(problems)} 处差异（共 {total} 条用例）\n")
    for item in problems[: args.max_report]:
        print("  " + item.replace("\n", "\n  "))
        print()
    if len(problems) > args.max_report:
        print(f"  ...还有 {len(problems) - args.max_report} 处未显示（用 --max-report 调大）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
