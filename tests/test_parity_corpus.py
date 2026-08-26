"""对拍语料的覆盖闸：**每个产 SQL 的公开方法都必须在语料里**。

为什么需要它：语料本身也会腐烂。加了新 API 却忘了加对拍用例，对拍照样报绿——
于是新方法上的跨语言分叉可以一路溜到线上。实测过一次：41 个公开方法里有
**20 个**（近一半）当时不在语料里。

这道闸枚举 SQLBuilder 的公开方法，逐个检查方法名有没有出现在语料的用例名里
（去下划线后不分大小写比对）。漏一个就红——**这才是让语料不腐烂的机制**，
光靠"记得加"是不行的。

Go 侧有一份对等的闸（parity_emit_test.go 的 TestParityCorpusCoversEveryPublicAPI）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from parity_emit import build_corpus  # noqa: E402

from proto2mysql import SQLBuilder  # noqa: E402

#: 不产 SQL 的方法，不需要对拍。加进来要写明理由。
EXEMPT = {
    "from_message": "构造器，不产 SQL",
    "table": "返回 MessageTable 本身，不是 SQL",
    "sql_suffix": "QueryOptions 的内部辅助，已被 select_where_paged 间接覆盖",
}


def _public_methods() -> list[str]:
    return sorted(
        name for name in dir(SQLBuilder)
        if not name.startswith("_") and callable(getattr(SQLBuilder, name, None))
    )


def test_corpus_covers_every_public_api():
    corpus = build_corpus()
    covered = " ".join(c["name"] for c in corpus["cases"]).replace("_", "").lower()

    missing = [
        name for name in _public_methods()
        if name not in EXEMPT and name.replace("_", "").lower() not in covered
    ]
    assert not missing, (
        f"以下 {len(missing)} 个公开方法没有对拍用例：\n  "
        + "\n  ".join(missing)
        + "\n\n每个产 SQL 的公开方法都必须在语料里，否则它上面的跨语言分叉没人守。\n"
        "两边都要加：Python 在 tools/parity_emit.py，Go 在 parity_emit_test.go。\n"
        "确实不产 SQL 的，加进本文件的 EXEMPT 白名单并写明理由。"
    )


def test_corpus_case_names_are_unique():
    """用例名是对拍的主键，重名会让一条静默覆盖另一条。"""
    names = [c["name"] for c in build_corpus()["cases"]]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"用例名重复: {sorted(dupes)}"


def test_corpus_is_deterministic():
    """同一份代码跑两次必须产出完全一样的语料。

    不确定的语料会让对拍变成抛硬币——Go 那边就因为 map 迭代随机化栽过
    （见 fixes-2026-08.md 第 10 条）。
    """
    assert build_corpus() == build_corpus()


@pytest.mark.parametrize("case", build_corpus()["cases"], ids=lambda c: c["name"])
def test_every_case_produces_something(case):
    """空 SQL 只允许出现在显式标了 #none 的用例上（表示"本就无需变更"）。"""
    if case["name"].endswith("#none"):
        assert case["sql"] == ""
    else:
        assert case["sql"].strip(), f"{case['name']} 产出了空 SQL"
