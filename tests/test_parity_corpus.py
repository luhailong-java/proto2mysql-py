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

import json
import os
import shutil
import subprocess
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


def _write_corpus(path: Path, lang: str, cases: list[dict]) -> None:
    path.write_text(
        json.dumps({"corpus_version": 1, "lang": lang, "cases": cases}),
        encoding="utf-8",
    )


def test_diff_rejects_wrong_language_and_duplicate_case_names(tmp_path):
    from parity_diff import load

    wrong = tmp_path / "wrong.json"
    _write_corpus(wrong, "py", [])
    with pytest.raises(SystemExit, match="lang"):
        load(str(wrong), "go")

    duplicate = tmp_path / "duplicate.json"
    case = {"name": "same", "sql": "SELECT 1", "args": []}
    _write_corpus(duplicate, "go", [case, {**case, "sql": "SELECT 2"}])
    with pytest.raises(SystemExit, match="重复"):
        load(str(duplicate), "go")


@pytest.mark.parametrize("script", ["parity_emit.py", "parity_diff.py", "parity_run.py"])
def test_parity_help_survives_legacy_windows_stdout_encoding(script):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / script), "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("ascii", errors="replace")
    assert b"UnicodeEncodeError" not in proc.stderr


def test_parity_emit_runs_from_a_checkout_without_generated_stubs(tmp_path):
    """模拟 fresh clone：只复制受版本控制的源码，刻意不复制 tests/gen。"""
    checkout = tmp_path / "checkout"
    shutil.copytree(ROOT / "src", checkout / "src")
    shutil.copytree(ROOT / "tools", checkout / "tools")
    shutil.copytree(ROOT / "tests" / "proto", checkout / "tests" / "proto")
    (checkout / "tests").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "tests" / "_gen_stubs.py", checkout / "tests" / "_gen_stubs.py")

    output = checkout / "parity.json"
    proc = subprocess.run(
        [sys.executable, str(checkout / "tools" / "parity_emit.py"), "-o", str(output)],
        cwd=checkout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["cases"]
