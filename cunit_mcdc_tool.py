#!/usr/bin/env python3
"""
AI-assisted CUnit test generator with MC/DC-oriented coverage feedback.

This tool is intentionally dependency-light: it uses the Python standard library
and an OpenAI-compatible chat-completions endpoint. It does not promise formal
certification-grade MC/DC by itself; instead it automates the practical loop:

1. discover C decisions in the target project,
2. ask an LLM to generate CUnit tests for those decisions,
3. run the project's build/test/coverage commands,
4. parse coverage artifacts when available,
5. report remaining MC/DC obligations for review or another generation pass.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MODEL = "gpt-4.1"

MCDC_REQUIREMENTS = textwrap.dedent("""
MC/DC (Modified Condition / Decision Coverage) definition:
- Every condition in a decision must be shown to independently affect the decision outcome.
- For each condition C in a decision D, you must produce at least two test cases where:
  1. Condition C evaluates to True in one and False in the other.
  2. All other conditions in D have the SAME value in both test cases.
  3. The overall decision D evaluates to different outcomes (True vs False) in the two cases.
- This proves C independently controls D.

Short-circuit evaluation rules (C language):
- In `A && B`, if A is False, B is NOT evaluated. Therefore B cannot independently change
  the decision outcome when A is False. You must set A=True to test B's independence.
- In `A || B`, if A is True, B is NOT evaluated. Therefore B cannot independently change
  the decision outcome when A is True. You must set A=False to test B's independence.
- For mixed expressions like `A && (B || C)`, when testing B's independence:
  A must be True, and C must be fixed (e.g., False) so that only B's change flips the result.

Example for `if (a > 0 && b < 10)`:
  Condition 0: a > 0   — pair: (a=5,b=5)→True vs (a=-1,b=5)→False
  Condition 1: b < 10  — pair: (a=5,b=5)→True vs (a=5,b=15)→False
  Note: b<10 can only flip the decision when a>0 is True (short-circuit).

Example for `if (a || (b && c))`:
  Condition 0: a       — pair: (a=1,b=0,c=0)→True vs (a=0,b=0,c=0)→False
  Condition 1: b       — pair: (a=0,b=1,c=1)→True vs (a=0,b=0,c=1)→False
  Condition 2: c       — pair: (a=0,b=1,c=1)→True vs (a=0,b=1,c=0)→False
  Note: b and c can only flip the decision when a is False (short-circuit).

For each decision below, you MUST generate test cases that satisfy ALL of its MC/DC pairs.
""").strip()

DATA_VALUE_TEST_GUIDE = textwrap.dedent("""
Data-value testing guide (for computation functions without decisions):
For functions that perform calculations, data transformations, or state changes
(rather than pure boolean decisions), you must generate tests covering:

1. Normal values: typical inputs in the expected operating range.
2. Boundary values: minimum and maximum valid inputs (e.g., 0, INT_MAX, INT_MIN,
   FLT_MAX, array length 0 vs full length, empty string, etc.).
3. Edge cases: off-by-one values, values just outside valid ranges, zero, negative
   numbers where only positive is expected, etc.
4. Error-path inputs: NULL pointers, invalid enum values, out-of-range indices,
   buffer overflow attempts, etc.

Deriving expected values for computation tests:
- You MUST trace through the source code step by step to compute the expected output
  for each test input. Do NOT guess or assume.
- For each test, add a comment showing the manual trace:
    // Input: x=5, y=3
    // Trace: sum = 5 + 3 = 8; result = 8 & 0xFF = 8
    // Expected: 8
- For boundary values, note why the boundary was chosen:
    // Boundary: len=0 (empty buffer, loop body never executes)
    // Expected: sum=0, result=0
- For functions returning structs or modifying output parameters, verify each field.
- Use CU_ASSERT_EQUAL for integer comparisons, CU_ASSERT_DOUBLE_EQUAL for floats,
  and CU_ASSERT_STRING_EQUAL for strings when available. Fall back to CU_ASSERT
  with a manual comparison expression otherwise.

Example for `int compute_checksum(const uint8_t *buf, int len)`:
  // Test: normal values
  // Input: buf={10, 20, 30}, len=3
  // Trace: i=0: sum=10; i=1: sum=30; i=2: sum=60; result=60&0xFF=60
  // Expected: 60
  void test_checksum_normal(void) {
      uint8_t buf[] = {10, 20, 30};
      CU_ASSERT_EQUAL(compute_checksum(buf, 3), 60);
  }

  // Test: boundary - empty buffer
  // Input: buf={}, len=0
  // Trace: loop condition i<0 is false immediately; sum=0; result=0
  // Expected: 0
  void test_checksum_empty(void) {
      uint8_t buf[] = {0};
      CU_ASSERT_EQUAL(compute_checksum(buf, 0), 0);
  }

  // Test: boundary - overflow (sum exceeds 255)
  // Input: buf={200, 200}, len=2
  // Trace: i=0: sum=200; i=1: sum=400; result=400&0xFF=144
  // Expected: 144
  void test_checksum_overflow(void) {
      uint8_t buf[] = {200, 200};
      CU_ASSERT_EQUAL(compute_checksum(buf, 2), 144);
  }
""").strip()


@dataclasses.dataclass
class Decision:
    file: str
    line: int
    expression: str
    conditions: list[str]


@dataclasses.dataclass
class CFunction:
    file: str
    name: str
    signature: str
    start_line: int
    end_line: int
    is_static: bool
    decisions: list[Decision]
    function_body: str = ""
    called_functions: list[str] = dataclasses.field(default_factory=list)
    used_structs: list[str] = dataclasses.field(default_factory=list)
    used_macros: list[str] = dataclasses.field(default_factory=list)
    required_headers: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Config:
    project_root: Path
    source_globs: list[str]
    include_dirs: list[str]
    test_output: Path
    build_commands: list[str]
    test_commands: list[str]
    coverage_commands: list[str]
    llm_base_url: str
    llm_api_key_env: str
    llm_model: str
    max_llm_rounds: int
    llm_context_char_limit: int
    source_excerpt_radius: int
    max_decisions_per_prompt: int
    function_test_dir: Path
    runner_output: Path
    max_functions_per_prompt: int
    extra_prompt: str
    max_prompt_tokens: int
    max_response_tokens: int
    instrument_dir: Path = dataclasses.field(default_factory=lambda: Path("mcdc_instrumented"))
    mcdc_trace_file: Path = dataclasses.field(default_factory=lambda: Path("mcdc_trace.txt"))
    compile_test_command: str = ""


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    project_root = Path(raw.get("project_root", ".")).expanduser().resolve()
    instrument_dir_raw = raw.get("instrument_dir", "mcdc_instrumented")
    mcdc_trace_raw = raw.get("mcdc_trace_file", "mcdc_trace.txt")
    return Config(
        project_root=project_root,
        source_globs=raw.get("source_globs", ["**/*.c", "**/*.h"]),
        include_dirs=raw.get("include_dirs", []),
        test_output=(project_root / raw.get("test_output", "tests/auto_mcdc_tests.c")).resolve(),
        build_commands=raw.get("build_commands", []),
        test_commands=raw.get("test_commands", []),
        coverage_commands=raw.get("coverage_commands", []),
        llm_base_url=raw.get("llm_base_url", "https://api.openai.com/v1"),
        llm_api_key_env=raw.get("llm_api_key_env", "OPENAI_API_KEY"),
        llm_model=raw.get("llm_model", DEFAULT_MODEL),
        max_llm_rounds=int(raw.get("max_llm_rounds", 1)),
        llm_context_char_limit=int(raw.get("llm_context_char_limit", 36000)),
        source_excerpt_radius=int(raw.get("source_excerpt_radius", 80)),
        max_decisions_per_prompt=int(raw.get("max_decisions_per_prompt", 60)),
        function_test_dir=(project_root / raw.get("function_test_dir", "tests/auto_function_tests")).resolve(),
        runner_output=(project_root / raw.get("runner_output", "tests/auto_cunit_runner.c")).resolve(),
        max_functions_per_prompt=int(raw.get("max_functions_per_prompt", 8)),
        extra_prompt=raw.get("extra_prompt", ""),
        max_prompt_tokens=int(raw.get("max_prompt_tokens", 12000)),
        max_response_tokens=int(raw.get("max_response_tokens", 4096)),
        instrument_dir=(project_root / instrument_dir_raw).resolve(),
        mcdc_trace_file=(project_root / mcdc_trace_raw).resolve(),
        compile_test_command=raw.get("compile_test_command", ""),
    )


def iter_source_files(config: Config) -> list[Path]:
    files: set[Path] = set()
    ignored_parts = {".git", "build", "cmake-build-debug", "cmake-build-release"}
    for pattern in config.source_globs:
        for path in config.project_root.glob(pattern):
            if not path.is_file():
                continue
            if ignored_parts.intersection(path.parts):
                continue
            files.add(path.resolve())
    return sorted(files)


def strip_comments_and_strings(source: str) -> str:
    result: list[str] = []
    i = 0
    state = "code"
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                result.append("  ")
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                result.append("  ")
                i += 2
                continue
            if ch == '"':
                state = "string"
                result.append('""')
                i += 1
                continue
            if ch == "'":
                state = "char"
                result.append("''")
                i += 1
                continue
            result.append(ch)
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
                result.append("\n")
            else:
                result.append(" ")
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                result.append("  ")
                i += 2
                continue
            result.append("\n" if ch == "\n" else " ")
        elif state == "string":
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                state = "code"
            if ch == "\n":
                result.append("\n")
        elif state == "char":
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                state = "code"
            if ch == "\n":
                result.append("\n")
        i += 1
    return "".join(result)


def find_matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_parenthesized_after_keyword(text: str, keyword: str) -> Iterable[tuple[int, str]]:
    pattern = re.compile(r"\b" + re.escape(keyword) + r"\s*\(")
    for match in pattern.finditer(text):
        open_index = text.find("(", match.start())
        close_index = find_matching_paren(text, open_index)
        if close_index is None:
            continue
        yield match.start(), text[open_index + 1 : close_index].strip()


def extract_for_condition(text: str) -> Iterable[tuple[int, str]]:
    for start, body in extract_parenthesized_after_keyword(text, "for"):
        parts = split_top_level(body, ";")
        if len(parts) >= 2 and parts[1].strip():
            yield start, parts[1].strip()


def split_top_level(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


CONDITION_SPLIT_RE = re.compile(r"(\&\&|\|\|)")


def split_conditions(expression: str) -> list[str]:
    tokens = CONDITION_SPLIT_RE.split(expression)
    conditions: list[str] = []
    current: list[str] = []
    depth = 0
    for token in tokens:
        if token in {"&&", "||"} and depth == 0:
            condition = "".join(current).strip()
            if condition:
                conditions.append(trim_outer_parens(condition))
            current = []
            continue
        for ch in token:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        current.append(token)
    final = "".join(current).strip()
    if final:
        conditions.append(trim_outer_parens(final))
    return conditions or [expression.strip()]


def trim_outer_parens(text: str) -> str:
    text = text.strip()
    changed = True
    while changed and text.startswith("(") and text.endswith(")"):
        changed = False
        close = find_matching_paren(text, 0)
        if close == len(text) - 1:
            text = text[1:-1].strip()
            changed = True
    return text


# ── Boolean expression tree parser ──


@dataclasses.dataclass
class BoolExpr:
    """Represents a node in a boolean expression tree."""
    kind: str  # "leaf", "and", "or", "not"
    value: str = ""  # for leaf nodes: the condition text
    children: list[BoolExpr] = dataclasses.field(default_factory=list)
    operator: str = ""  # "&&" or "||" for and/or nodes

    def leaf_conditions(self) -> list[str]:
        """Return all leaf conditions in order."""
        if self.kind == "leaf":
            return [self.value]
        result: list[str] = []
        for child in self.children:
            result.extend(child.leaf_conditions())
        return result

    def is_pure_and(self) -> bool:
        """Check if the tree is a pure conjunction (all && operators)."""
        if self.kind == "leaf":
            return True
        if self.kind in ("not",):
            return True
        if self.kind in ("and", "or"):
            if self.kind == "or":
                return False
            return all(child.is_pure_and() for child in self.children)
        return True

    def is_pure_or(self) -> bool:
        """Check if the tree is a pure disjunction (all || operators)."""
        if self.kind == "leaf":
            return True
        if self.kind in ("not",):
            return True
        if self.kind in ("and", "or"):
            if self.kind == "and":
                return False
            return all(child.is_pure_or() for child in self.children)
        return True

    def evaluate(self, leaf_values: dict[str, bool]) -> bool:
        """Evaluate the boolean expression given values for each leaf condition.

        leaf_values maps leaf condition text to True/False.
        Short-circuit semantics are respected: in A && B, if A is False,
        B is not evaluated; in A || B, if A is True, B is not evaluated.
        """
        if self.kind == "leaf":
            return leaf_values.get(self.value, False)
        if self.kind == "not":
            if not self.children:
                return False
            return not self.children[0].evaluate(leaf_values)
        if self.kind == "and":
            for child in self.children:
                if not child.evaluate(leaf_values):
                    return False
            return True
        if self.kind == "or":
            for child in self.children:
                if child.evaluate(leaf_values):
                    return True
            return False
        return False


def _find_top_level_split(text: str, operator: str) -> list[str]:
    """Split text at top-level occurrences of operator (&& or ||)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif depth == 0 and text[i:i + len(operator)] == operator:
            parts.append("".join(current).strip())
            current = []
            i += len(operator)
            continue
        else:
            current.append(ch)
        i += 1
    final = "".join(current).strip()
    if final:
        parts.append(final)
    return parts


def parse_bool_expr(text: str) -> BoolExpr:
    """Parse a boolean expression string into a BoolExpr tree."""
    text = trim_outer_parens(text)
    if not text:
        return BoolExpr(kind="leaf", value="")

    # Split at top-level || (lowest precedence)
    or_parts = _find_top_level_split(text, "||")
    if len(or_parts) > 1:
        children = [parse_bool_expr(part) for part in or_parts]
        return BoolExpr(kind="or", children=children, operator="||")

    # Split at top-level && (higher precedence)
    and_parts = _find_top_level_split(text, "&&")
    if len(and_parts) > 1:
        children = [parse_bool_expr(part) for part in and_parts]
        return BoolExpr(kind="and", children=children, operator="&&")

    # Handle ! prefix
    stripped = text.lstrip()
    if stripped.startswith("!"):
        inner = stripped[1:].strip()
        if inner:
            child = parse_bool_expr(inner)
            return BoolExpr(kind="not", children=[child])

    # Leaf node
    return BoolExpr(kind="leaf", value=text.strip())


def split_conditions_advanced(expression: str) -> list[str]:
    """Split conditions using the BoolExpr tree parser for correct nested handling."""
    tree = parse_bool_expr(expression)
    return tree.leaf_conditions()


# ── Do-while detection ──


def extract_do_while_conditions(text: str) -> Iterable[tuple[int, str]]:
    """Find do { ... } while(cond); patterns and extract the condition with the line of the while keyword."""
    do_pattern = re.compile(r"\bdo\s*\{")
    for do_match in do_pattern.finditer(text):
        # Find the matching closing brace
        brace_start = text.find("{", do_match.start())
        if brace_start < 0:
            continue
        close_brace = find_matching_brace(text, brace_start)
        if close_brace is None:
            continue
        # After the closing brace, look for 'while'
        after_brace = text[close_brace + 1:].lstrip()
        while_match = re.match(r"while\s*\(", after_brace)
        if not while_match:
            continue
        # Calculate the absolute position of 'while' keyword
        while_abs_pos = close_brace + 1 + (len(text[close_brace + 1:]) - len(after_brace))
        # Find the parenthesized condition after 'while'
        open_paren = text.find("(", while_abs_pos)
        if open_paren < 0:
            continue
        close_paren = find_matching_paren(text, open_paren)
        if close_paren is None:
            continue
        condition = text[open_paren + 1:close_paren].strip()
        yield while_abs_pos, condition


# ── MC/DC pair data structures and matching engine ──


@dataclasses.dataclass
class MCDCPair:
    """Represents an MC/DC independence pair for one condition."""
    decision_file: str
    decision_line: int
    condition_index: int
    condition_text: str
    satisfied: bool = False
    test_true: str = ""   # test name where condition=True and decision=True
    test_false: str = ""  # test name where condition=False and decision=False


@dataclasses.dataclass
class ConditionTrace:
    """Records the evaluation of one condition in one test run."""
    decision_id: str
    condition_index: int
    value: bool
    decision_result: bool
    test_name: str


def generate_mcdc_pairs(decision: Decision) -> list[MCDCPair]:
    """Generate all MC/DC pairs for a decision. For each condition, we need a pair
    of tests where only that condition changes and the decision outcome changes."""
    pairs: list[MCDCPair] = []
    for idx, condition in enumerate(decision.conditions):
        pairs.append(
            MCDCPair(
                decision_file=decision.file,
                decision_line=decision.line,
                condition_index=idx,
                condition_text=condition,
            )
        )
    return pairs


@dataclasses.dataclass
class TruthTableRow:
    """One row of a decision's truth table."""
    condition_values: list[bool]  # value of each condition
    decision_result: bool         # overall decision outcome


@dataclasses.dataclass
class MCDCDemoPair:
    """Two truth-table rows that demonstrate MC/DC independence for one condition."""
    condition_index: int
    condition_text: str
    row_true: TruthTableRow   # row where condition=True and decision=True
    row_false: TruthTableRow  # row where condition=False and decision=False


def compute_truth_table(decision: Decision) -> list[TruthTableRow]:
    """Compute the logical truth table for a decision.

    Uses the BoolExpr tree to evaluate all 2^N combinations of condition
    values, respecting short-circuit semantics.
    """
    conditions = decision.conditions
    n = len(conditions)
    if n == 0:
        return []

    tree = parse_bool_expr(decision.expression)
    rows: list[TruthTableRow] = []

    for combo in range(1 << n):
        cond_vals = [(combo >> (n - 1 - i)) & 1 == 1 for i in range(n)]
        leaf_values = dict(zip(conditions, cond_vals))
        result = tree.evaluate(leaf_values)
        rows.append(TruthTableRow(
            condition_values=cond_vals,
            decision_result=result,
        ))

    return rows


def find_mcdc_demo_pairs(decision: Decision) -> list[MCDCDemoPair]:
    """For each condition, find two truth-table rows that demonstrate MC/DC independence.

    For condition C at index i, we need two rows where:
    1. C is True in one row and False in the other
    2. All other conditions have the SAME value in both rows
    3. The decision result differs between the two rows

    Returns one MCDCDemoPair per condition if such a pair exists.
    """
    conditions = decision.conditions
    n = len(conditions)
    if n == 0:
        return []

    rows = compute_truth_table(decision)
    demos: list[MCDCDemoPair] = []

    for i in range(n):
        # Group rows by the values of all conditions EXCEPT condition i
        groups: dict[tuple[bool, ...], list[TruthTableRow]] = {}
        for row in rows:
            key = tuple(
                row.condition_values[j] for j in range(n) if j != i
            )
            groups.setdefault(key, []).append(row)

        # In each group, look for two rows that differ only in condition i
        # and have different decision results
        found = False
        for key, group_rows in groups.items():
            true_rows = [r for r in group_rows if r.condition_values[i]]
            false_rows = [r for r in group_rows if not r.condition_values[i]]
            for tr in true_rows:
                for fr in false_rows:
                    if tr.decision_result != fr.decision_result:
                        demos.append(MCDCDemoPair(
                            condition_index=i,
                            condition_text=conditions[i],
                            row_true=tr,
                            row_false=fr,
                        ))
                        found = True
                        break
                if found:
                    break
            if found:
                break

    return demos


def format_truth_table_for_prompt(decision: Decision) -> str:
    """Format a decision's truth table and MC/DC demo pairs for inclusion in LLM prompt."""
    conditions = decision.conditions
    n = len(conditions)
    if n == 0:
        return ""

    rows = compute_truth_table(decision)
    demos = find_mcdc_demo_pairs(decision)

    lines: list[str] = []
    lines.append(f"Decision: {decision.expression} (line {decision.line})")
    lines.append(f"Conditions: {conditions}")
    lines.append("")

    # Truth table header
    header = " | ".join(f"C{i}" for i in range(n)) + " | Result"
    sep = "-+-".join("---" for _ in range(n + 1))
    lines.append(header)
    lines.append(sep)

    for row in rows:
        vals = " | ".join(" T" if v else " F" for v in row.condition_values)
        res = " T" if row.decision_result else " F"
        lines.append(f"{vals} |{res}")

    lines.append("")

    # MC/DC demo pairs
    if demos:
        lines.append("MC/DC independence pairs (expected test outcomes):")
        for demo in demos:
            t_vals = ", ".join(
                f"C{j}={'T' if demo.row_true.condition_values[j] else 'F'}"
                for j in range(n)
            )
            f_vals = ", ".join(
                f"C{j}={'T' if demo.row_false.condition_values[j] else 'F'}"
                for j in range(n)
            )
            lines.append(
                f"  Condition {demo.condition_index} ({demo.condition_text}):"
            )
            lines.append(
                f"    Test A: [{t_vals}] → decision = "
                f"{'True' if demo.row_true.decision_result else 'False'}"
            )
            lines.append(
                f"    Test B: [{f_vals}] → decision = "
                f"{'True' if demo.row_false.decision_result else 'False'}"
            )
            lines.append(
                f"    Only C{demo.condition_index} differs; all other conditions are the same."
            )
    else:
        lines.append("WARNING: No MC/DC independence pair found for this decision.")
        lines.append("This may indicate a coupled or redundant condition.")

    return "\n".join(lines)


def parse_mcdc_trace_file(trace_path: Path) -> list[ConditionTrace]:
    """Parse the trace file produced by instrumented code.
    The trace format is one record per line:
    MCDC_TRACE:decision_id:condition_index:value:decision_result:test_name
    """
    traces: list[ConditionTrace] = []
    if not trace_path.exists():
        return traces
    for line in trace_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("MCDC_TRACE:"):
            continue
        parts = line[len("MCDC_TRACE:"):].split(":")
        if len(parts) < 5:
            continue
        try:
            decision_id = parts[0]
            condition_index = int(parts[1])
            value = parts[2].strip().lower() in ("1", "true", "yes")
            decision_result = parts[3].strip().lower() in ("1", "true", "yes")
            test_name = ":".join(parts[4:])
            traces.append(
                ConditionTrace(
                    decision_id=decision_id,
                    condition_index=condition_index,
                    value=value,
                    decision_result=decision_result,
                    test_name=test_name,
                )
            )
        except (ValueError, IndexError):
            continue
    return traces


def evaluate_mcdc_coverage(decisions: list[Decision], traces: list[ConditionTrace]) -> list[MCDCPair]:
    """For each decision and each condition, check if there exist two traces where:
    - Only that condition's value differs
    - All other conditions have the same values
    - The decision outcome differs
    If such a pair exists, mark the MCDCPair as satisfied."""
    all_pairs: list[MCDCPair] = []
    for decision in decisions:
        decision_id = make_decision_id(decision.file, decision.line)
        num_conditions = len(decision.conditions)
        decision_traces = [t for t in traces if t.decision_id == decision_id]

        # Group traces by test name to get per-test condition vectors
        test_vectors: dict[str, list[bool | None]] = {}
        test_decision_results: dict[str, bool] = {}
        for trace in decision_traces:
            if trace.test_name not in test_vectors:
                test_vectors[trace.test_name] = [None] * num_conditions
                test_decision_results[trace.test_name] = trace.decision_result
            if trace.condition_index < num_conditions:
                test_vectors[trace.test_name][trace.condition_index] = trace.value

        pairs = generate_mcdc_pairs(decision)

        for pair in pairs:
            idx = pair.condition_index
            # Find two tests where only condition idx differs and decision outcome differs
            test_names = [name for name in test_vectors if test_vectors[name][idx] is not None]
            found = False
            for i, name_a in enumerate(test_names):
                if found:
                    break
                for name_b in test_names[i + 1:]:
                    vec_a = test_vectors[name_a]
                    vec_b = test_vectors[name_b]
                    # Condition idx must differ
                    if vec_a[idx] == vec_b[idx]:
                        continue
                    # All other conditions must be the same
                    other_same = True
                    for j in range(num_conditions):
                        if j == idx:
                            continue
                        if vec_a[j] is not None and vec_b[j] is not None and vec_a[j] != vec_b[j]:
                            other_same = False
                            break
                    if not other_same:
                        continue
                    # Decision outcome must differ
                    if test_decision_results[name_a] == test_decision_results[name_b]:
                        continue
                    # Found a valid pair
                    pair.satisfied = True
                    if vec_a[idx]:
                        pair.test_true = name_a
                        pair.test_false = name_b
                    else:
                        pair.test_true = name_b
                        pair.test_false = name_a
                    found = True
                    break

        all_pairs.extend(pairs)
    return all_pairs


def compute_mcdc_coverage_percent(pairs: list[MCDCPair]) -> float:
    """Return the percentage of satisfied MCDC pairs."""
    if not pairs:
        return 0.0
    satisfied = sum(1 for p in pairs if p.satisfied)
    return (satisfied / len(pairs)) * 100.0


def make_decision_id(file_rel: str, line: int) -> str:
    """Create a decision_id from file and line, with non-alphanumeric chars replaced by underscores."""
    file_part = re.sub(r"[^A-Za-z0-9_]", "_", file_rel)
    return f"{file_part}_{line}"


# ── Instrumentation code generation ──


def generate_instrumentation_header(output_path: Path) -> Path:
    """Generate a C header file mcdc_instrumentation.h that defines macros for MCDC tracing."""
    ensure_parent(output_path)
    header_content = textwrap.dedent('''\
        #ifndef MCDC_INSTRUMENTATION_H
        #define MCDC_INSTRUMENTATION_H

        #include <stdio.h>
        #include <stdlib.h>

        extern const char *MCDC_TEST_NAME;
        extern FILE *MCDC_TRACE_FILE;

        #define MCDC_INIT() \\
            do { \\
                MCDC_TRACE_FILE = fopen("mcdc_trace.txt", "a"); \\
                if (MCDC_TRACE_FILE == NULL) { \\
                    fprintf(stderr, "MCDC: failed to open trace file\\n"); \\
                    exit(1); \\
                } \\
            } while (0)

        #define MCDC_COND(d_id, c_id, cond) \\
            (__extension__({ \\
                int _mcdc_val = (cond) ? 1 : 0; \\
                int _mcdc_dec = 0; /* filled by outer logic if needed */ \\
                if (MCDC_TRACE_FILE != NULL) { \\
                    fprintf(MCDC_TRACE_FILE, "MCDC_TRACE:%s:%d:%d:%d:%s\\n", \\
                            (d_id), (c_id), _mcdc_val, _mcdc_val, MCDC_TEST_NAME); \\
                } \\
                _mcdc_val; \\
            }))

        #define MCDC_CLOSE() \\
            do { \\
                if (MCDC_TRACE_FILE != NULL) { \\
                    fclose(MCDC_TRACE_FILE); \\
                    MCDC_TRACE_FILE = NULL; \\
                } \\
            } while (0)

        #endif /* MCDC_INSTRUMENTATION_H */
    ''')
    output_path.write_text(header_content, encoding="utf-8")
    return output_path


def instrument_source(source: str, decisions: list[Decision], file_rel: str) -> str:
    """Transform C source code by inserting MCDC_COND macros around each condition in each decision."""
    lines = source.split("\n")
    # Process decisions in reverse line order to avoid offset issues
    sorted_decisions = sorted(decisions, key=lambda d: d.line, reverse=True)
    for decision in sorted_decisions:
        line_idx = decision.line - 1
        if line_idx < 0 or line_idx >= len(lines):
            continue
        line_text = lines[line_idx]
        decision_id = make_decision_id(file_rel, decision.line)
        # Replace each condition with MCDC_COND macro
        new_line = line_text
        # Process conditions in reverse order to preserve positions
        for cond_idx in range(len(decision.conditions) - 1, -1, -1):
            condition = decision.conditions[cond_idx]
            # Use word-boundary-aware replacement for the condition
            # Escape special regex chars in condition
            escaped_cond = re.escape(condition)
            # Try to find and replace the condition text
            pattern = escaped_cond
            replacement = f'MCDC_COND("{decision_id}", {cond_idx}, {condition})'
            new_line = re.sub(pattern, replacement, new_line, count=1)
        lines[line_idx] = new_line

    # Add include for instrumentation header at the top
    result_lines = lines
    # Find the first non-comment, non-blank, non-preprocessor line to insert after includes
    insert_pos = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#include"):
            insert_pos = i + 1
        elif stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
            break
    result_lines.insert(insert_pos, '#include "mcdc_instrumentation.h"')
    return "\n".join(result_lines)


def instrument_project(config: Config, decisions: list[Decision]) -> list[Path]:
    """For each source file that has decisions, create an instrumented copy in
    {project_root}/mcdc_instrumented/ directory. Also generate the instrumentation header there."""
    config.instrument_dir.mkdir(parents=True, exist_ok=True)

    # Generate the instrumentation header
    header_path = generate_instrumentation_header(config.instrument_dir / "mcdc_instrumentation.h")

    # Group decisions by file
    decisions_by_file: dict[str, list[Decision]] = {}
    for decision in decisions:
        decisions_by_file.setdefault(decision.file, []).append(decision)

    generated_paths: list[Path] = [header_path]
    for rel_file, file_decisions in decisions_by_file.items():
        src_path = (config.project_root / rel_file).resolve()
        if not src_path.exists():
            continue
        source = src_path.read_text(encoding="utf-8-sig", errors="ignore")
        instrumented = instrument_source(source, file_decisions, rel_file)
        # Create the same directory structure under instrument_dir
        out_path = config.instrument_dir / rel_file
        ensure_parent(out_path)
        out_path.write_text(instrumented, encoding="utf-8")
        generated_paths.append(out_path)
    return generated_paths


# ── Test compilation verification ──


def try_compile_test(config: Config, test_source_path: Path) -> tuple[bool, str]:
    """Attempt to compile the test file using the project's include dirs.
    Returns (success, error_output). If compilation fails, returns the error message."""
    if not config.compile_test_command:
        return True, ""
    include_flags = " ".join(f"-I{d}" for d in config.include_dirs)
    cmd = config.compile_test_command.replace("{source}", str(test_source_path))
    cmd = cmd.replace("{includes}", include_flags)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(config.project_root),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        if proc.returncode == 0:
            return True, ""
        return False, proc.stdout[-3000:] if len(proc.stdout) > 3000 else proc.stdout
    except subprocess.TimeoutExpired:
        return False, "Compilation timed out"
    except Exception as exc:
        return False, str(exc)


# ── MC/DC feedback prompt ──


def build_mcdc_feedback_prompt(config: Config, uncovered_pairs: list[MCDCPair], decisions: list[Decision]) -> str:
    """Create a prompt specifically asking the LLM to generate tests for the uncovered MC/DC pairs."""
    uncovered_info = []
    for pair in uncovered_pairs:
        decision = None
        for d in decisions:
            if d.file == pair.decision_file and d.line == pair.decision_line:
                decision = d
                break
        uncovered_info.append({
            "file": pair.decision_file,
            "line": pair.decision_line,
            "condition_index": pair.condition_index,
            "condition_text": pair.condition_text,
            "decision_expression": decision.expression if decision else "unknown",
            "all_conditions": decision.conditions if decision else [],
        })

    source_blob = build_source_context(config, decisions)
    include_flags = " ".join(f"-I{item}" for item in config.include_dirs)

    mcdc_requirements = MCDC_REQUIREMENTS

    # Generate truth tables for decisions that have uncovered pairs
    uncovered_decision_files_lines = {(p.decision_file, p.decision_line) for p in uncovered_pairs}
    relevant_decisions = [d for d in decisions if (d.file, d.line) in uncovered_decision_files_lines]
    truth_table_blob = "\n\n".join(
        format_truth_table_for_prompt(d) for d in relevant_decisions
    ) if relevant_decisions else ""

    return textwrap.dedent(
        f"""
        You are generating additional CUnit tests to achieve MC/DC coverage for a C project.
        The following MC/DC independence pairs are NOT yet satisfied. For each pair, we need
        two tests where only that condition changes and the decision outcome changes.

        Return only JSON with this exact schema:
        {{
          "test_file": "complete C source code for a CUnit test file",
          "notes": ["brief note"],
          "assumptions": ["brief assumption"]
        }}

        Requirements:
        - Use CUnit/Basic.h.
        - Include the target headers/source declarations needed by the tests.
        - Create focused tests that exercise the specific uncovered MC/DC pairs listed below.
        - Keep tests deterministic.
        - Do not include Markdown fences.
        - For each uncovered pair, generate a SEPARATE test function that demonstrates
          the MC/DC independence for that specific condition.
        - Name each test to indicate which condition and pair it covers, e.g.:
          test_<file>_<line>_cond<idx>_independence.
        - In each test, use CU_ASSERT to verify the decision outcome.
          The EXPECTED decision outcome for each MC/DC pair is given in the truth tables below.
          You MUST use the expected outcome from the truth table, NOT guess from the source code.
        - Add a comment above each test explaining:
          1. Which condition is being tested for independence.
          2. What values the OTHER conditions are fixed to and why.
          3. How the two test cases in the pair differ only in the target condition.
          4. What the expected decision outcome is for each test case.
        - Pay special attention to short-circuit evaluation: make sure the condition
          being tested is actually reachable (not short-circuited) in both test cases.

        {mcdc_requirements}

        Project root: {config.project_root}
        Include flags: {include_flags}
        Test output path: {config.test_output}
        Extra user prompt: {config.extra_prompt}

        Uncovered MC/DC pairs ({len(uncovered_info)} total):
        {json.dumps(uncovered_info, ensure_ascii=False, indent=2)}

        Truth tables and expected test outcomes (USE THESE for CU_ASSERT expected values):
        {truth_table_blob}

        Source excerpts:
        {source_blob}
        """
    ).strip()


def discover_decisions(config: Config) -> list[Decision]:
    decisions: list[Decision] = []
    keywords = ["if", "while", "switch"]
    for path in iter_source_files(config):
        raw = path.read_text(encoding="utf-8-sig", errors="ignore")
        clean = strip_comments_and_strings(raw)
        rel = str(path.relative_to(config.project_root))
        found: list[tuple[int, str]] = []
        for keyword in keywords:
            found.extend(extract_parenthesized_after_keyword(clean, keyword))
        found.extend(extract_for_condition(clean))
        found.extend(extract_do_while_conditions(clean))
        ternary_candidates = re.finditer(r"([^;\n{}]+?)\?([^:;\n{}]+):", clean)
        for match in ternary_candidates:
            found.append((match.start(), match.group(1).strip()))
        for index, expr in sorted(found, key=lambda item: item[0]):
            if not expr:
                continue
            line = clean.count("\n", 0, index) + 1
            conditions = split_conditions(expr)
            if len(conditions) == 1 and not re.search(r"[<>=!]=?|&&|\|\|", conditions[0]):
                continue
            decisions.append(Decision(rel, line, compact_ws(expr), [compact_ws(c) for c in conditions]))
    return decisions


def discover_functions(config: Config) -> list[CFunction]:
    all_decisions = discover_decisions(config)
    decisions_by_file: dict[str, list[Decision]] = {}
    for decision in all_decisions:
        decisions_by_file.setdefault(decision.file, []).append(decision)

    functions: list[CFunction] = []
    for path in iter_source_files(config):
        if path.suffix.lower() != ".c":
            continue
        raw = path.read_text(encoding="utf-8-sig", errors="ignore")
        clean = strip_comments_and_strings(raw)
        rel = str(path.relative_to(config.project_root))
        for item in extract_functions_from_clean_source(clean, rel, decisions_by_file.get(rel, [])):
            functions.append(item)
    return functions


def extract_functions_from_clean_source(clean: str, rel_file: str, decisions: list[Decision]) -> list[CFunction]:
    functions: list[CFunction] = []
    index = 0
    while index < len(clean):
        brace = clean.find("{", index)
        if brace < 0:
            break
        header = possible_function_header(clean, brace)
        if not header:
            index = brace + 1
            continue
        name = extract_function_name(header)
        if not name:
            index = brace + 1
            continue
        end = find_matching_brace(clean, brace)
        if end is None:
            index = brace + 1
            continue
        start_line = clean.count("\n", 0, brace) + 1
        end_line = clean.count("\n", 0, end) + 1
        body = extract_function_body(clean, brace)
        function_decisions = [
            decision for decision in decisions if start_line <= decision.line <= end_line
        ]
        functions.append(
            CFunction(
                file=rel_file,
                name=name,
                signature=compact_ws(header),
                start_line=start_line,
                end_line=end_line,
                is_static=bool(re.search(r"\bstatic\b", header)),
                decisions=function_decisions,
                function_body=compact_ws(body),
                called_functions=extract_called_functions(clean, body),
                used_structs=extract_used_structs(clean, body),
                used_macros=extract_used_macros(clean, body),
                required_headers=extract_required_headers(clean),
            )
        )
        index = end + 1
    return functions


def possible_function_header(clean: str, brace_index: int) -> str | None:
    prefix = clean[:brace_index].rstrip()
    if not prefix.endswith(")"):
        return None
    close_paren = len(prefix) - 1
    open_paren = find_open_paren_backward(prefix, close_paren)
    if open_paren is None:
        return None
    name_match = re.search(r"([A-Za-z_]\w*)\s*$", prefix[:open_paren])
    if not name_match:
        return None
    name = name_match.group(1)
    if name in {"if", "while", "for", "switch", "return", "sizeof"}:
        return None

    start = max(prefix.rfind(";"), prefix.rfind("}"), prefix.rfind("{"))
    header = prefix[start + 1 :].strip()
    if not header or "\n\n\n" in header:
        return None
    if "=" in header.split("(", 1)[0]:
        return None
    if re.search(r"\btypedef\b", header):
        return None
    if not re.search(r"\b[A-Za-z_]\w*\s*\(", header):
        return None
    return header


def find_open_paren_backward(text: str, close_index: int) -> int | None:
    depth = 0
    for index in range(close_index, -1, -1):
        ch = text[index]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                return index
    return None


def find_matching_brace(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        ch = text[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def extract_function_name(header: str) -> str | None:
    match = re.search(r"([A-Za-z_]\w*)\s*\([^()]*\)\s*$", header, re.S)
    if not match:
        return None
    return match.group(1)


def compact_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ── Structured dependency extraction for CFunction ──

_KW = {"if", "while", "for", "switch", "return", "sizeof", "goto", "case", "default",
       "break", "continue", "struct", "union", "enum", "typedef", "sizeof", "do"}


def extract_function_body(clean: str, brace_index: int) -> str:
    """Return the function body (including braces) from the opening brace."""
    end = find_matching_brace(clean, brace_index)
    if end is None:
        return ""
    return clean[brace_index:end + 1]


def extract_called_functions(clean: str, body: str) -> list[str]:
    """Find function calls inside the body (name + '(' patterns), excluding keywords."""
    calls: set[str] = set()
    # Match identifiers followed by '(' that are not C keywords
    for match in re.finditer(r"\b([A-Za-z_]\w+)\s*\(", body):
        name = match.group(1)
        if name not in _KW:
            calls.add(name)
    return sorted(calls)


_STRUCT_RE = re.compile(r"\bstruct\s+([A-Za-z_]\w*)")
_MACRO_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")


def extract_used_structs(clean: str, body: str) -> list[str]:
    """Find struct type names referenced in the function body."""
    return sorted(set(_STRUCT_RE.findall(body)))


def extract_used_macros(clean: str, body: str) -> list[str]:
    """Find macro-like identifiers (SCREAMING_SNAKE_CASE) in the function body."""
    return sorted(set(_MACRO_RE.findall(body)))


_HEADER_RE = re.compile(r'^\s*#\s*include\s+([<"][^>"]+[>"])', re.MULTILINE)


def extract_required_headers(source: str) -> list[str]:
    """Extract all #include directives from the source file."""
    return _HEADER_RE.findall(source)


def mcdc_obligations(decision: Decision) -> list[dict[str, Any]]:
    obligations = []
    condition_count = len(decision.conditions)
    if condition_count == 1:
        return [{"condition": decision.conditions[0], "need": "exercise true and false outcomes"}]
    for idx, condition in enumerate(decision.conditions):
        obligations.append(
            {
                "condition": condition,
                "need": (
                    "find two tests where only this condition changes, "
                    "all other conditions are held fixed, and the decision outcome changes"
                ),
                "condition_index": idx,
            }
        )
    return obligations


def build_prompt(config: Config, decisions: list[Decision]) -> str:
    selected_decisions = decisions[: config.max_decisions_per_prompt]
    omitted_decision_count = max(0, len(decisions) - len(selected_decisions))
    source_blob = build_source_context(config, selected_decisions)
    decision_payload = [
        {
            "file": d.file,
            "line": d.line,
            "expression": d.expression,
            "conditions": d.conditions,
            "mcdc_obligations": mcdc_obligations(d),
        }
        for d in selected_decisions
    ]

    # Generate truth tables and expected outcomes for each decision
    truth_table_blob = "\n\n".join(
        format_truth_table_for_prompt(d) for d in selected_decisions
    )

    include_flags = " ".join(f"-I{item}" for item in config.include_dirs)

    mcdc_requirements = MCDC_REQUIREMENTS

    return textwrap.dedent(
        f"""
        You are generating CUnit tests for a C project. Target MC/DC coverage for
        every listed decision. Return only JSON with this exact schema:
        {{
          "test_file": "complete C source code for a CUnit test file",
          "notes": ["brief note"],
          "assumptions": ["brief assumption"]
        }}

        Requirements:
        - Use CUnit/Basic.h.
        - Include the target headers/source declarations needed by the tests.
        - Create focused tests that exercise MC/DC pairs.
        - Keep tests deterministic.
        - Do not include Markdown fences.
        - Mention any required stubs in notes if external dependencies block testing.
        - For each decision, generate a SEPARATE test function that clearly demonstrates
          the MC/DC independence pair for each condition.
        - Name each test to indicate which condition it covers, e.g.:
          test_decision_line{selected_decisions[0].line if selected_decisions else 'N'}_cond0_true,
          test_decision_line{selected_decisions[0].line if selected_decisions else 'N'}_cond0_false.
        - In each test, use CU_ASSERT to verify the decision outcome (True or False).
          The EXPECTED decision outcome for each MC/DC pair is given in the truth tables below.
          You MUST use the expected outcome from the truth table, NOT guess from the source code.
        - Add a comment above each test explaining which MC/DC pair it satisfies and
          what the expected outcome is.

        {mcdc_requirements}

        Project root: {config.project_root}
        Include flags: {include_flags}
        Test output path: {config.test_output}
        Extra user prompt: {config.extra_prompt}
        Context limit note:
        - This prompt contains local source excerpts around target decisions, not the full project.
        - Decisions included in this request: {len(selected_decisions)}
        - Decisions omitted from this request because of max_decisions_per_prompt: {omitted_decision_count}
        - If omitted_decision_count is greater than 0, generate tests for the included decisions only.

        Decisions and MC/DC obligations:
        {json.dumps(decision_payload, ensure_ascii=False, indent=2)}

        Truth tables and expected test outcomes (USE THESE for CU_ASSERT expected values):
        {truth_table_blob}

        Source excerpts:
        {source_blob}
        """
    ).strip()


def build_source_context(config: Config, decisions: list[Decision]) -> str:
    return _build_source_context_impl(config, decisions)


def auto_batch_decisions(
    config: Config, decisions: list[Decision]
) -> list[list[Decision]]:
    """Split decisions into batches that fit within max_prompt_tokens.

    Uses a greedy approach: start with an empty batch, add decisions one by
    one, and check the estimated token count of the resulting prompt.  When
    adding the next decision would exceed the limit, start a new batch.

    A minimum of 1 decision per batch is always enforced.
    """
    if not decisions:
        return []

    max_tokens = config.max_prompt_tokens
    batches: list[list[Decision]] = []
    current_batch: list[Decision] = []

    for decision in decisions:
        trial = current_batch + [decision]
        prompt = build_prompt(config, trial)
        if estimate_tokens(prompt) > max_tokens and current_batch:
            # Current batch is full; start a new one
            batches.append(current_batch)
            current_batch = [decision]
        else:
            current_batch = trial

    if current_batch:
        batches.append(current_batch)

    return batches


def auto_batch_functions(
    config: Config, functions: list[CFunction]
) -> list[list[CFunction]]:
    """Split functions into batches that fit within max_prompt_tokens.

    Similar to auto_batch_decisions: build the prompt for each candidate
    batch and split when the token budget would be exceeded.
    """
    if not functions:
        return []

    max_tokens = config.max_prompt_tokens
    # First, apply the existing per-file grouping and max_functions_per_prompt
    pre_batches: list[list[CFunction]] = []
    for batch in batch_functions(config, functions):
        pre_batches.append(batch)

    # Now further split any pre-batch that exceeds the token limit
    result: list[list[CFunction]] = []
    for pre_batch in pre_batches:
        current_batch: list[CFunction] = []
        for func in pre_batch:
            trial = current_batch + [func]
            batch_id = make_batch_id(trial, len(result) + 1)
            prompt = build_function_prompt(config, trial, batch_id)
            if estimate_tokens(prompt) > max_tokens and current_batch:
                result.append(current_batch)
                current_batch = [func]
            else:
                current_batch = trial
        if current_batch:
            result.append(current_batch)

    return result


def merge_test_files(test_sources: list[str], output_path: Path) -> Path:
    """Merge multiple CUnit test source files into one unified file.

    Each input source may contain its own #include directives, test
    functions, and registration functions.  The merged output:
    - Deduplicates #include directives
    - Preserves all test functions
    - Creates a single unified registration function that calls all
      individual registration functions
    - Adds a main() if none of the inputs define one
    """
    if not test_sources:
        raise RuntimeError("no test sources to merge")

    if len(test_sources) == 1:
        ensure_parent(output_path)
        output_path.write_text(test_sources[0].rstrip() + "\n", encoding="utf-8")
        return output_path

    all_includes: list[str] = []
    all_bodies: list[str] = []
    all_register_decls: list[str] = []
    all_register_calls: list[str] = []
    has_main = False

    for idx, source in enumerate(test_sources):
        lines = source.split("\n")
        include_lines: list[str] = []
        body_lines: list[str] = []
        local_register_funcs: list[str] = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#include"):
                include_lines.append(line)
            elif re.match(r"int\s+main\s*\(", stripped):
                has_main = True
                body_lines.append(line)
            elif re.match(r"void\s+(register_\w+)\s*\(\s*void\s*\)", stripped):
                match = re.match(r"void\s+(register_\w+)\s*\(\s*void\s*\)", stripped)
                if match:
                    local_register_funcs.append(match.group(1))
                body_lines.append(line)
            else:
                body_lines.append(line)

        all_includes.extend(include_lines)
        all_bodies.append(f"/* --- Batch {idx + 1} --- */\n" + "\n".join(body_lines))

        for func_name in local_register_funcs:
            all_register_decls.append(f"extern void {func_name}(void);")
            all_register_calls.append(f"    {func_name}();")

    # Deduplicate includes
    seen_includes: set[str] = set()
    unique_includes: list[str] = []
    for inc in all_includes:
        if inc not in seen_includes:
            seen_includes.add(inc)
            unique_includes.append(inc)

    # Build merged file
    parts: list[str] = []
    parts.append("/* === Merged CUnit test file (auto-generated) === */\n")
    parts.append("\n".join(unique_includes))
    parts.append("\n")

    for decl in all_register_decls:
        parts.append(decl + "\n")

    for body in all_bodies:
        parts.append(body)
        parts.append("\n")

    # Add unified main if no main exists
    if not has_main and all_register_calls:
        parts.append(textwrap.dedent(f"""
            void register_all_mcdc_tests(void) {{
            {chr(10).join(all_register_calls)}
            }}

            int main(void) {{
                if (CU_initialize_registry() != CUE_SUCCESS) {{
                    return CU_get_error();
                }}
                register_all_mcdc_tests();
                CU_basic_set_mode(CU_BRM_VERBOSE);
                CU_basic_run_tests();
                unsigned int failures = CU_get_number_of_failures();
                CU_cleanup_registry();
                return failures == 0 ? 0 : 1;
            }}
            """))

    merged = "\n".join(parts)
    ensure_parent(output_path)
    output_path.write_text(merged, encoding="utf-8")
    return output_path


def _build_source_context_impl(config: Config, decisions: list[Decision]) -> str:
    by_file: dict[str, list[int]] = {}
    for decision in decisions:
        by_file.setdefault(decision.file, []).append(decision.line)

    chunks = []
    remaining = max(2000, config.llm_context_char_limit)
    for rel_file in sorted(by_file):
        path = (config.project_root / rel_file).resolve()
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        windows = merge_line_windows(
            by_file[rel_file],
            total_lines=len(lines),
            radius=max(5, config.source_excerpt_radius),
        )
        file_parts = [f"// FILE: {rel_file}"]
        for start, end in windows:
            numbered = []
            for line_no in range(start, end + 1):
                numbered.append(f"{line_no:5d}: {lines[line_no - 1]}")
            file_parts.append(f"// LINES {start}-{end}\n" + "\n".join(numbered))
        chunk = "\n".join(file_parts)
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining] + "\n// CONTEXT TRUNCATED BY llm_context_char_limit")
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    return "\n\n".join(chunks)


def build_function_prompt(config: Config, functions: list[CFunction], batch_id: str) -> str:
    source_blob = build_function_source_context(config, functions)
    function_payload = []
    for function in functions:
        function_payload.append(
            {
                "file": function.file,
                "name": function.name,
                "signature": function.signature,
                "start_line": function.start_line,
                "end_line": function.end_line,
                "is_static": function.is_static,
                "called_functions": function.called_functions,
                "used_structs": function.used_structs,
                "used_macros": function.used_macros,
                "required_headers": function.required_headers,
                "decisions": [
                    {
                        "line": decision.line,
                        "expression": decision.expression,
                        "conditions": decision.conditions,
                        "mcdc_obligations": mcdc_obligations(decision),
                    }
                    for decision in function.decisions
                ],
            }
        )
    register_name = f"register_{batch_id}_tests"
    include_flags = " ".join(f"-I{item}" for item in config.include_dirs)

    mcdc_requirements = MCDC_REQUIREMENTS
    data_value_guide = DATA_VALUE_TEST_GUIDE

    # Classify functions: with decisions vs pure computation
    functions_with_decisions = [f for f in functions if f.decisions]
    functions_without_decisions = [f for f in functions if not f.decisions]

    # Generate truth tables for all decisions in this batch
    all_decisions: list[Decision] = []
    for function in functions:
        all_decisions.extend(function.decisions)
    truth_table_blob = "\n\n".join(
        format_truth_table_for_prompt(d) for d in all_decisions
    ) if all_decisions else ""

    # Build function classification info for the prompt
    func_classification = []
    if functions_with_decisions:
        names = ", ".join(f.name for f in functions_with_decisions)
        func_classification.append(f"Functions with decisions (need MC/DC tests): {names}")
    if functions_without_decisions:
        names = ", ".join(f.name for f in functions_without_decisions)
        func_classification.append(f"Pure computation functions (need data-value tests): {names}")
    classification_text = "\n".join(func_classification)

    return textwrap.dedent(
        f"""
        You are generating CUnit tests for a C project, one batch at a time.
        Return only JSON with this exact schema:
        {{
          "test_file": "complete C source code for this batch, without main()",
          "register_function": "{register_name}",
          "notes": ["brief note"],
          "assumptions": ["brief assumption"]
        }}

        There are TWO types of test in this batch. Handle each differently:

        ═══ TYPE 1: Functions WITH decisions (MC/DC tests) ═══
        For these functions, generate MC/DC independence pair tests.
        - For each decision, generate a SEPARATE test function per condition.
        - Name each test: test_<func>_<line>_cond<idx>_true or test_<func>_<line>_cond<idx>_false.
        - Use CU_ASSERT to verify the decision outcome (True or False).
          The EXPECTED outcome is given in the truth tables below — USE THEM, do NOT guess.
        - Add a comment above each test explaining which MC/DC pair it satisfies,
          why other conditions are held fixed, and what the expected outcome is.

        ═══ TYPE 2: Functions WITHOUT decisions (data-value tests) ═══
        For these functions, generate data-value tests covering normal values,
        boundary values, edge cases, and error paths.
        - Name each test: test_<func>_<scenario> (e.g., test_checksum_normal,
          test_checksum_empty, test_checksum_overflow).
        - You MUST trace through the source code step by step to compute the expected
          output for each test input. Do NOT guess or assume.
        - Add a comment above each test showing the manual trace and expected value.
        - Use CU_ASSERT_EQUAL / CU_ASSERT_DOUBLE_EQUAL / CU_ASSERT_STRING_EQUAL
          when available, otherwise CU_ASSERT with a comparison expression.

        {mcdc_requirements}

        {data_value_guide}

        Function classification:
        {classification_text}

        Requirements (all tests):
        - Use CUnit/Basic.h.
        - Do not define main().
        - Define this registration function exactly: void {register_name}(void)
        - Inside {register_name}, call CU_add_suite and CU_add_test for this batch.
        - Keep tests deterministic.
        - Prefer testing public APIs and headers. If a function is static, state the needed build strategy in notes
          and use the most practical approach for a CUnit test project, such as compiling the source file into
          the test target or including it behind a test-only macro if the project allows that.
        - Mention required stubs or fakes in notes if external dependencies block direct testing.
        - Do not include Markdown fences.

        Project root: {config.project_root}
        Include flags: {include_flags}
        Batch id: {batch_id}
        Extra user prompt: {config.extra_prompt}

        Target functions:
        {json.dumps(function_payload, ensure_ascii=False, indent=2)}

        Truth tables and expected test outcomes for MC/DC tests (TYPE 1 functions):
        {truth_table_blob}

        Source excerpts:
        {source_blob}
        """
    ).strip()


def build_function_source_context(config: Config, functions: list[CFunction]) -> str:
    chunks = []
    remaining = max(2000, config.llm_context_char_limit)
    for rel_file in sorted({function.file for function in functions}):
        path = (config.project_root / rel_file).resolve()
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        file_functions = [function for function in functions if function.file == rel_file]
        windows = []
        for function in file_functions:
            start = max(1, function.start_line - config.source_excerpt_radius)
            end = min(len(lines), function.end_line + config.source_excerpt_radius)
            windows.append((start, end))
        merged = merge_ranges(windows)
        file_parts = [f"// FILE: {rel_file}"]
        for start, end in merged:
            numbered = [f"{line_no:5d}: {lines[line_no - 1]}" for line_no in range(start, end + 1)]
            file_parts.append(f"// LINES {start}-{end}\n" + "\n".join(numbered))
        chunk = "\n".join(file_parts)
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining] + "\n// CONTEXT TRUNCATED BY llm_context_char_limit")
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    return "\n\n".join(chunks)


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
    return merged


def merge_line_windows(line_numbers: list[int], total_lines: int, radius: int) -> list[tuple[int, int]]:
    ranges = []
    for line in sorted(set(line_numbers)):
        ranges.append((max(1, line - radius), min(total_lines, line + radius)))
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
    return merged


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text string.

    Uses a conservative heuristic: ~3.5 chars per token for mixed
    English/code content (GPT-style tokenizers average ~4 chars/token
    for English but less for code with many symbols).
    """
    return max(1, len(text) // 3)


def truncate_prompt_to_token_limit(prompt: str, max_tokens: int, label: str = "") -> str:
    """Truncate a prompt to fit within a token budget.

    If the prompt exceeds max_tokens, it is truncated and a notice is appended.
    The truncation preserves the beginning of the prompt (which contains
    instructions and decision data) and cuts from the end (source excerpts).
    """
    estimated = estimate_tokens(prompt)
    if estimated <= max_tokens:
        return prompt
    # Convert token limit back to char limit (conservative)
    char_limit = max_tokens * 3
    notice = f"\n\n// PROMPT TRUNCATED: original ~{estimated} tokens exceeded limit of {max_tokens}"
    if label:
        notice += f" (context: {label})"
    notice += ". Source excerpts may be incomplete."
    truncated = prompt[:char_limit - len(notice)]
    return truncated + notice


def call_llm(config: Config, prompt: str, label: str = "") -> dict[str, Any]:
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        raise RuntimeError(f"environment variable {config.llm_api_key_env} is not set")

    # Enforce prompt token limit
    prompt = truncate_prompt_to_token_limit(prompt, config.max_prompt_tokens, label)
    prompt_tokens = estimate_tokens(prompt)
    if prompt_tokens > config.max_prompt_tokens:
        print(f"warning: prompt ~{prompt_tokens} tokens still exceeds limit {config.max_prompt_tokens} after truncation",
              file=sys.stderr)

    url = config.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.llm_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a senior embedded C test engineer. You generate compilable "
                    "CUnit tests and reason carefully about MC/DC obligations."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": config.max_response_tokens,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    data = json.loads(body)
    content = data["choices"][0]["message"]["content"].strip()
    content = strip_markdown_json_fence(content)
    return json.loads(content)


def strip_markdown_json_fence(content: str) -> str:
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def generate_function_tests(config: Config, functions: list[CFunction]) -> dict[str, Any]:
    """Generate CUnit tests for all functions, auto-batching by token limit."""
    batches = auto_batch_functions(config, functions)
    return _generate_function_tests_from_batches(config, batches, functions)


def _generate_function_tests_from_batches(
    config: Config,
    batches: list[list[CFunction]],
    all_functions: list[CFunction],
) -> dict[str, Any]:
    """Core implementation: generate CUnit tests for pre-computed batches."""
    ensure_parent(config.function_test_dir / "placeholder")
    generated_files = []
    notes = []
    assumptions = []
    for batch_index, batch in enumerate(batches, start=1):
        batch_id = make_batch_id(batch, batch_index)
        prompt = build_function_prompt(config, batch, batch_id)
        payload = call_llm(config, prompt, label=f"func-batch-{batch_index}")
        test_file = payload.get("test_file")
        if not isinstance(test_file, str) or "#include" not in test_file:
            print(f"warning: batch {batch_id} LLM response invalid, skipping",
                  file=sys.stderr)
            continue
        register_function = payload.get("register_function") or f"register_{batch_id}_tests"
        output = config.function_test_dir / f"test_{batch_id}.c"
        output.write_text(test_file.rstrip() + "\n", encoding="utf-8")

        # Try compilation verification with retries
        max_retries = 2
        for retry in range(max_retries + 1):
            success, error_output = try_compile_test(config, output)
            if success:
                break
            if retry < max_retries:
                retry_prompt = (
                    f"The previously generated test file failed to compile with the following error:\n"
                    f"{error_output}\n\n"
                    f"Please fix the compilation error and return the corrected test file as JSON:\n"
                    f'{{"test_file": "corrected C source code", "register_function": "{register_function}"}}'
                )
                try:
                    retry_payload = call_llm(config, retry_prompt,
                                             label=f"func-batch-{batch_index}-retry-{retry}")
                    retry_test_file = retry_payload.get("test_file")
                    if isinstance(retry_test_file, str) and "#include" in retry_test_file:
                        output.write_text(retry_test_file.rstrip() + "\n", encoding="utf-8")
                        register_function = retry_payload.get("register_function") or register_function
                except Exception:
                    pass  # Keep the original file if retry fails

        generated_files.append(
            {
                "path": str(output),
                "register_function": register_function,
                "functions": [dataclasses.asdict(function) for function in batch],
            }
        )
        notes.extend(payload.get("notes", []))
        assumptions.extend(payload.get("assumptions", []))
    runner = write_cunit_runner(config, generated_files)
    manifest = {
        "project_root": str(config.project_root),
        "function_count": len(all_functions),
        "generated_files": generated_files,
        "runner_output": str(runner),
        "notes": notes,
        "assumptions": assumptions,
    }
    manifest_path = config.function_test_dir / "auto_function_tests_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def batch_functions(config: Config, functions: list[CFunction]) -> Iterable[list[CFunction]]:
    max_per_batch = max(1, config.max_functions_per_prompt)
    by_file: dict[str, list[CFunction]] = {}
    for function in functions:
        by_file.setdefault(function.file, []).append(function)
    for rel_file in sorted(by_file):
        items = sorted(by_file[rel_file], key=lambda item: item.start_line)
        for start in range(0, len(items), max_per_batch):
            yield items[start : start + max_per_batch]


def make_batch_id(functions: list[CFunction], batch_index: int) -> str:
    if functions:
        file_part = Path(functions[0].file).with_suffix("").as_posix()
        names = "_".join(function.name for function in functions[:2])
    else:
        file_part = "empty"
        names = "batch"
    return safe_identifier(f"{batch_index}_{file_part}_{names}")[:80]


def safe_identifier(text: str) -> str:
    value = re.sub(r"\W+", "_", text)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "generated"
    if value[0].isdigit():
        value = "_" + value
    return value


def write_cunit_runner(config: Config, generated_files: list[dict[str, Any]]) -> Path:
    ensure_parent(config.runner_output)
    declarations = []
    calls = []
    for item in generated_files:
        register_function = safe_identifier(str(item.get("register_function", "")))
        declarations.append(f"extern void {register_function}(void);")
        calls.append(f"    {register_function}();")
    declaration_block = "\n".join(declarations)
    call_block = "\n".join(calls)
    source = textwrap.dedent(
        f"""
        #include <stdio.h>
        #include <CUnit/Basic.h>

        {declaration_block}

        int main(void)
        {{
            if (CU_initialize_registry() != CUE_SUCCESS) {{
                return CU_get_error();
            }}

        {call_block}

            CU_basic_set_mode(CU_BRM_VERBOSE);
            CU_basic_run_tests();
            unsigned int failures = CU_get_number_of_failures();
            CU_cleanup_registry();
            return failures == 0 ? 0 : 1;
        }}
        """
    ).lstrip()
    config.runner_output.write_text(source, encoding="utf-8")
    return config.runner_output


def run_command(command: str, cwd: Path) -> tuple[int, str]:
    started = time.time()
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    elapsed = time.time() - started
    header = f"$ {command}\n# exit={proc.returncode}, elapsed={elapsed:.1f}s\n"
    return proc.returncode, header + proc.stdout


def run_commands(commands: list[str], cwd: Path) -> list[dict[str, Any]]:
    results = []
    for command in commands:
        code, output = run_command(command, cwd)
        results.append({"command": command, "returncode": code, "output": output})
        if code != 0:
            break
    return results


def parse_gcov_text(text: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    line_match = re.search(r"Lines executed:([0-9.]+)% of (\d+)", text)
    branch_match = re.search(r"Branches executed:([0-9.]+)% of (\d+)", text)
    taken_match = re.search(r"Taken at least once:([0-9.]+)% of (\d+)", text)
    if line_match:
        summary["lines_executed_percent"] = float(line_match.group(1))
        summary["lines_total"] = int(line_match.group(2))
    if branch_match:
        summary["branches_executed_percent"] = float(branch_match.group(1))
        summary["branches_total"] = int(branch_match.group(2))
    if taken_match:
        summary["branches_taken_at_least_once_percent"] = float(taken_match.group(1))
        summary["branches_taken_total"] = int(taken_match.group(2))
    return summary


def collect_coverage_summary(config: Config, command_results: list[dict[str, Any]]) -> dict[str, Any]:
    combined = "\n".join(item["output"] for item in command_results)
    summary = parse_gcov_text(combined)
    gcov_files = list(config.project_root.rglob("*.gcov"))
    uncovered_lines = []
    for gcov in gcov_files[:200]:
        for line in gcov.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lstrip().startswith("#####"):
                uncovered_lines.append({"file": str(gcov.relative_to(config.project_root)), "line": line})
    summary["gcov_files"] = [str(path.relative_to(config.project_root)) for path in gcov_files]
    summary["uncovered_line_samples"] = uncovered_lines[:50]
    return summary


def write_report(
    config: Config,
    decisions: list[Decision],
    llm_payload: dict[str, Any] | None,
    build_results: list[dict[str, Any]],
    test_results: list[dict[str, Any]],
    coverage_results: list[dict[str, Any]],
    coverage_summary: dict[str, Any],
    functions: list[CFunction] | None = None,
    function_manifest: dict[str, Any] | None = None,
    mcdc_pairs: list[MCDCPair] | None = None,
) -> Path:
    report_path = config.test_output.with_suffix(".mcdc_report.json")
    html_path = report_path.with_suffix(".html")
    decision_reports = []
    for decision in decisions:
        item = dataclasses.asdict(decision)
        item["mcdc_obligations"] = mcdc_obligations(decision)
        decision_reports.append(item)
    report = {
        "project_root": str(config.project_root),
        "test_output": str(config.test_output),
        "html_report": str(html_path),
        "decision_count": len(decisions),
        "decisions": decision_reports,
        "function_count": len(functions or []),
        "functions": [dataclasses.asdict(function) for function in (functions or [])],
        "function_test_manifest": function_manifest or {},
        "llm_notes": (llm_payload or {}).get("notes", []),
        "llm_assumptions": (llm_payload or {}).get("assumptions", []),
        "build_results": summarize_results(build_results),
        "test_results": summarize_results(test_results),
        "coverage_results": summarize_results(coverage_results),
        "coverage_summary": coverage_summary,
        "mcdc_status": (
            "needs_manual_review: branch/line coverage does not prove MC/DC; "
            "confirm listed independent-condition pairs with generated tests"
        ),
    }
    # Add MCDC pair results
    if mcdc_pairs is not None:
        pair_dicts = [dataclasses.asdict(p) for p in mcdc_pairs]
        report["mcdc_pairs"] = pair_dicts
        report["mcdc_coverage_percent"] = compute_mcdc_coverage_percent(mcdc_pairs)
        satisfied_count = sum(1 for p in mcdc_pairs if p.satisfied)
        report["mcdc_pairs_satisfied"] = satisfied_count
        report["mcdc_pairs_total"] = len(mcdc_pairs)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html_report(report), encoding="utf-8")
    return report_path


def command_group_status(results: list[dict[str, Any]]) -> str:
    if not results:
        return "skipped"
    if any(item.get("returncode") != 0 for item in results):
        return "failed"
    return "passed"


def render_html_report(report: dict[str, Any]) -> str:
    coverage = report.get("coverage_summary", {})
    decisions = report.get("decisions", [])
    functions = report.get("functions", [])
    function_manifest = report.get("function_test_manifest", {})
    mcdc_pairs = report.get("mcdc_pairs", [])
    mcdc_coverage_pct = report.get("mcdc_coverage_percent", 0.0)
    mcdc_pairs_satisfied = report.get("mcdc_pairs_satisfied", 0)
    mcdc_pairs_total = report.get("mcdc_pairs_total", 0)
    rows = []
    for item in decisions:
        obligations = item.get("mcdc_obligations", [])
        obligation_text = "<br>".join(
            html.escape(str(obligation.get("condition", ""))) for obligation in obligations
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('file', '')))}</td>"
            f"<td>{html.escape(str(item.get('line', '')))}</td>"
            f"<td><code>{html.escape(str(item.get('expression', '')))}</code></td>"
            f"<td>{html.escape(str(len(item.get('conditions', []))))}</td>"
            f"<td>{obligation_text}</td>"
            "</tr>"
        )

    uncovered_rows = []
    for item in coverage.get("uncovered_line_samples", []):
        uncovered_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('file', '')))}</td>"
            f"<td><code>{html.escape(str(item.get('line', '')))}</code></td>"
            "</tr>"
        )

    def pct(name: str) -> str:
        value = coverage.get(name)
        return "N/A" if value is None else f"{value:.2f}%"

    build_status = command_group_status(report.get("build_results", []))
    test_status = command_group_status(report.get("test_results", []))
    coverage_status = command_group_status(report.get("coverage_results", []))
    function_rows = []
    for item in functions:
        function_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('file', '')))}</td>"
            f"<td>{html.escape(str(item.get('name', '')))}</td>"
            f"<td>{html.escape(str(item.get('start_line', '')))}-{html.escape(str(item.get('end_line', '')))}</td>"
            f"<td>{'yes' if item.get('is_static') else 'no'}</td>"
            f"<td>{len(item.get('decisions', []))}</td>"
            "</tr>"
        )
    generated_rows = []
    for item in function_manifest.get("generated_files", []):
        generated_rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(item.get('path', '')))}</code></td>"
            f"<td><code>{html.escape(str(item.get('register_function', '')))}</code></td>"
            f"<td>{len(item.get('functions', []))}</td>"
            "</tr>"
        )

    # MC/DC pair rows
    mcdc_pair_rows = []
    for pair in mcdc_pairs:
        status_class = "passed" if pair.get("satisfied") else "failed"
        status_text = "已满足" if pair.get("satisfied") else "未满足"
        mcdc_pair_rows.append(
            "<tr>"
            f"<td>{html.escape(str(pair.get('decision_file', '')))}</td>"
            f"<td>{html.escape(str(pair.get('decision_line', '')))}</td>"
            f"<td>{html.escape(str(pair.get('condition_index', '')))}</td>"
            f"<td><code>{html.escape(str(pair.get('condition_text', '')))}</code></td>"
            f"<td class='{status_class}'>{status_text}</td>"
            f"<td>{html.escape(str(pair.get('test_true', '')))}</td>"
            f"<td>{html.escape(str(pair.get('test_false', '')))}</td>"
            "</tr>"
        )

    mcdc_section = ""
    if mcdc_pairs:
        mcdc_section = f"""
  <h2>MC/DC 独立对覆盖</h2>
  <div class="grid">
    <div class="card"><div class="label">MC/DC 覆盖率</div><div class="value">{mcdc_coverage_pct:.1f}%</div></div>
    <div class="card"><div class="label">已满足对数</div><div class="value">{mcdc_pairs_satisfied}</div></div>
    <div class="card"><div class="label">总对数</div><div class="value">{mcdc_pairs_total}</div></div>
  </div>
  <table>
    <tr><th>文件</th><th>行</th><th>条件索引</th><th>条件文本</th><th>状态</th><th>True测试</th><th>False测试</th></tr>
    {"".join(mcdc_pair_rows)}
  </table>
"""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>MC/DC 测试报告</title>
  <style>
    body {{ font-family: "Segoe UI", Arial, sans-serif; margin: 24px; color: #20242a; }}
    h1, h2 {{ margin: 0 0 12px; }}
    h2 {{ margin-top: 28px; }}
    .grid {{ display: table; border-spacing: 12px; margin-left: -12px; }}
    .card {{ display: table-cell; min-width: 160px; padding: 12px 14px; border: 1px solid #ccd3da; border-radius: 6px; }}
    .label {{ color: #5d6870; font-size: 12px; }}
    .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
    .passed {{ color: #16703a; }}
    .failed {{ color: #a12626; }}
    .skipped {{ color: #6a5b20; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
    th, td {{ border: 1px solid #d9dee4; padding: 7px 8px; vertical-align: top; text-align: left; }}
    th {{ background: #eef2f5; }}
    code {{ font-family: Consolas, monospace; }}
    .note {{ background: #fff8dc; border: 1px solid #e1cf80; padding: 10px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>MC/DC 测试报告</h1>
  <p>工程：<code>{html.escape(str(report.get("project_root", "")))}</code></p>
  <p>测试文件：<code>{html.escape(str(report.get("test_output", "")))}</code></p>

  <div class="grid">
    <div class="card"><div class="label">判定数量</div><div class="value">{len(decisions)}</div></div>
    <div class="card"><div class="label">函数数量</div><div class="value">{len(functions)}</div></div>
    <div class="card"><div class="label">行覆盖率</div><div class="value">{pct("lines_executed_percent")}</div></div>
    <div class="card"><div class="label">分支覆盖率</div><div class="value">{pct("branches_executed_percent")}</div></div>
    <div class="card"><div class="label">分支至少执行一次</div><div class="value">{pct("branches_taken_at_least_once_percent")}</div></div>
    <div class="card"><div class="label">MC/DC 覆盖率</div><div class="value">{mcdc_coverage_pct:.1f}%</div></div>
  </div>

  <h2>流程状态</h2>
  <table>
    <tr><th>阶段</th><th>状态</th></tr>
    <tr><td>构建</td><td class="{build_status}">{build_status}</td></tr>
    <tr><td>测试</td><td class="{test_status}">{test_status}</td></tr>
    <tr><td>覆盖率采集</td><td class="{coverage_status}">{coverage_status}</td></tr>
  </table>

  <h2>MC/DC 判定与义务</h2>
  <div class="note">行覆盖和分支覆盖不能单独证明 MC/DC。请确认每个条件都有独立影响判定结果的测试对。</div>
  <table>
    <tr><th>文件</th><th>行</th><th>判定表达式</th><th>条件数</th><th>需确认的条件</th></tr>
    {"".join(rows)}
  </table>

  {mcdc_section}

  <h2>函数级测试文件</h2>
  <p>Runner: <code>{html.escape(str(function_manifest.get("runner_output", "")))}</code></p>
  <table>
    <tr><th>生成的测试文件</th><th>注册函数</th><th>函数数</th></tr>
    {"".join(generated_rows) if generated_rows else "<tr><td colspan='3'>未生成函数级测试文件</td></tr>"}
  </table>

  <h2>发现的函数</h2>
  <table>
    <tr><th>文件</th><th>函数</th><th>行号</th><th>static</th><th>判定数</th></tr>
    {"".join(function_rows) if function_rows else "<tr><td colspan='5'>报告中没有函数信息</td></tr>"}
  </table>

  <h2>未覆盖行样本</h2>
  <table>
    <tr><th>GCOV 文件</th><th>内容</th></tr>
    {"".join(uncovered_rows) if uncovered_rows else "<tr><td colspan='2'>未发现样本或未生成 .gcov 文件</td></tr>"}
  </table>
</body>
</html>
"""

def summarize_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized = []
    for item in results:
        output = item["output"]
        summarized.append(
            {
                "command": item["command"],
                "returncode": item["returncode"],
                "output_tail": output[-6000:],
            }
        )
    return summarized


def write_example_config(path: Path) -> None:
    example = {
        "project_root": "C:/path/to/your/c/project",
        "source_globs": ["src/**/*.c", "include/**/*.h"],
        "include_dirs": ["include"],
        "test_output": "tests/auto_mcdc_tests.c",
        "build_commands": [
            "cmake -S . -B build -DCMAKE_C_FLAGS=\"--coverage\"",
            "cmake --build build",
        ],
        "test_commands": ["build/your_cunit_runner"],
        "coverage_commands": ["gcov -b -c src/*.c"],
        "llm_base_url": "https://api.openai.com/v1",
        "llm_api_key_env": "OPENAI_API_KEY",
        "llm_model": DEFAULT_MODEL,
        "max_llm_rounds": 1,
        "llm_context_char_limit": 36000,
        "source_excerpt_radius": 80,
        "max_decisions_per_prompt": 60,
        "function_test_dir": "tests/auto_function_tests",
        "runner_output": "tests/auto_cunit_runner.c",
        "max_functions_per_prompt": 8,
        "extra_prompt": "Prefer testing public APIs; create local stubs only when necessary.",
        "max_prompt_tokens": 12000,
        "max_response_tokens": 4096,
        "instrument_dir": "mcdc_instrumented",
        "mcdc_trace_file": "mcdc_trace.txt",
        "compile_test_command": "",
    }
    path.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def cmd_init(args: argparse.Namespace) -> int:
    out = Path(args.output).resolve()
    ensure_parent(out)
    write_example_config(out)
    print(f"wrote example config: {out}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    decisions = discover_decisions(config)
    print(json.dumps([dataclasses.asdict(d) for d in decisions], ensure_ascii=False, indent=2))
    return 0


def cmd_scan_functions(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    functions = discover_functions(config)
    print(json.dumps([dataclasses.asdict(function) for function in functions], ensure_ascii=False, indent=2))
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    decisions = discover_decisions(config)
    if not decisions:
        raise RuntimeError("no decisions found in configured source files")

    # Auto-batch decisions to fit within token limits
    batches = auto_batch_decisions(config, decisions)

    if args.dry_run_prompt:
        for idx, batch in enumerate(batches, start=1):
            prompt = build_prompt(config, batch)
            print(f"\n===== DECISION TEST PROMPT: batch {idx}/{len(batches)} "
                  f"({len(batch)} decisions, ~{estimate_tokens(prompt)} tokens) =====\n")
            print(prompt)
        return 0

    if len(batches) == 1:
        # Single batch: direct output
        prompt = build_prompt(config, batches[0])
        llm_payload = call_llm(config, prompt, label="decision-batch-1")
        test_file = llm_payload.get("test_file")
        if not isinstance(test_file, str) or "#include" not in test_file:
            raise RuntimeError("LLM response did not contain a valid test_file")
        ensure_parent(config.test_output)
        config.test_output.write_text(test_file.rstrip() + "\n", encoding="utf-8")
        print(f"wrote generated CUnit tests: {config.test_output}")
    else:
        # Multiple batches: call LLM per batch, then merge
        print(f"prompt exceeds token limit, splitting into {len(batches)} batches")
        test_sources: list[str] = []
        for idx, batch in enumerate(batches, start=1):
            prompt = build_prompt(config, batch)
            print(f"  batch {idx}/{len(batches)}: {len(batch)} decisions, "
                  f"~{estimate_tokens(prompt)} tokens")
            llm_payload = call_llm(config, prompt, label=f"decision-batch-{idx}")
            test_file = llm_payload.get("test_file")
            if not isinstance(test_file, str) or "#include" not in test_file:
                print(f"  warning: batch {idx} LLM response invalid, skipping",
                      file=sys.stderr)
                continue
            test_sources.append(test_file)

        if not test_sources:
            raise RuntimeError("all LLM batches failed to produce valid test files")

        merge_test_files(test_sources, config.test_output)
        print(f"wrote merged CUnit tests from {len(test_sources)} batches: {config.test_output}")

    return 0


def cmd_generate_functions(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    functions = discover_functions(config)
    if not functions:
        raise RuntimeError("no C functions found in configured .c source files")

    # Auto-batch functions to fit within token limits
    batches = auto_batch_functions(config, functions)

    if args.dry_run_prompt:
        for idx, batch in enumerate(batches, start=1):
            batch_id = make_batch_id(batch, idx)
            prompt = build_function_prompt(config, batch, batch_id)
            print(f"\n===== FUNCTION TEST PROMPT: {batch_id} "
                  f"(batch {idx}/{len(batches)}, ~{estimate_tokens(prompt)} tokens) =====\n")
            print(prompt)
        return 0

    # Use generate_function_tests which already handles batching internally,
    # but pass the auto-batched result
    manifest = _generate_function_tests_from_batches(config, batches, functions)
    print(f"wrote {len(manifest['generated_files'])} function test file(s)")
    print(f"wrote CUnit runner: {manifest['runner_output']}")
    print(f"wrote manifest: {config.function_test_dir / 'auto_function_tests_manifest.json'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    decisions = discover_decisions(config)
    llm_payload = None
    functions: list[CFunction] | None = None
    function_manifest: dict[str, Any] | None = None
    mcdc_pairs: list[MCDCPair] | None = None

    if args.generate:
        batches = auto_batch_decisions(config, decisions)
        if len(batches) == 1:
            prompt = build_prompt(config, batches[0])
            llm_payload = call_llm(config, prompt, label="decision-batch-1")
            ensure_parent(config.test_output)
            config.test_output.write_text(llm_payload["test_file"].rstrip() + "\n", encoding="utf-8")
        else:
            print(f"prompt exceeds token limit, splitting into {len(batches)} batches")
            test_sources: list[str] = []
            for idx, batch in enumerate(batches, start=1):
                prompt = build_prompt(config, batch)
                print(f"  batch {idx}/{len(batches)}: {len(batch)} decisions, "
                      f"~{estimate_tokens(prompt)} tokens")
                try:
                    batch_payload = call_llm(config, prompt, label=f"decision-batch-{idx}")
                    test_file = batch_payload.get("test_file")
                    if isinstance(test_file, str) and "#include" in test_file:
                        test_sources.append(test_file)
                    else:
                        print(f"  warning: batch {idx} LLM response invalid, skipping",
                              file=sys.stderr)
                except Exception as exc:
                    print(f"  warning: batch {idx} LLM call failed: {exc}", file=sys.stderr)
            if test_sources:
                merge_test_files(test_sources, config.test_output)
                llm_payload = {"test_file": config.test_output.read_text(encoding="utf-8"),
                               "notes": [], "assumptions": []}
            else:
                llm_payload = None
    if args.generate_functions:
        functions = discover_functions(config)
        function_manifest = generate_function_tests(config, functions)
    elif args.include_functions:
        functions = discover_functions(config)

    build_results = run_commands(config.build_commands, config.project_root)
    if any(item["returncode"] != 0 for item in build_results):
        test_results: list[dict[str, Any]] = []
        coverage_results: list[dict[str, Any]] = []
    else:
        test_results = run_commands(config.test_commands, config.project_root)
        coverage_results = run_commands(config.coverage_commands, config.project_root)
    coverage_summary = collect_coverage_summary(config, coverage_results)

    # Iterative MC/DC coverage improvement loop
    if config.max_llm_rounds > 1:
        # Instrument the source files
        instrument_project(config, decisions)

        for round_num in range(1, config.max_llm_rounds):
            # Build and run tests with instrumented code
            build_results_round = run_commands(config.build_commands, config.project_root)
            if any(item["returncode"] != 0 for item in build_results_round):
                break
            test_results_round = run_commands(config.test_commands, config.project_root)

            # Parse MCDC trace to evaluate coverage
            traces = parse_mcdc_trace_file(config.mcdc_trace_file)
            mcdc_pairs = evaluate_mcdc_coverage(decisions, traces)
            coverage_pct = compute_mcdc_coverage_percent(mcdc_pairs)

            print(f"MC/DC round {round_num}: coverage = {coverage_pct:.1f}%")

            if coverage_pct >= 100.0:
                break

            # Find uncovered pairs
            uncovered_pairs = [p for p in mcdc_pairs if not p.satisfied]
            if not uncovered_pairs:
                break

            # Generate additional tests for uncovered pairs
            feedback_prompt = build_mcdc_feedback_prompt(config, uncovered_pairs, decisions)
            try:
                llm_payload = call_llm(config, feedback_prompt)
                test_file = llm_payload.get("test_file")
                if isinstance(test_file, str) and "#include" in test_file:
                    ensure_parent(config.test_output)
                    config.test_output.write_text(test_file.rstrip() + "\n", encoding="utf-8")
            except Exception as exc:
                print(f"MC/DC feedback round {round_num} LLM call failed: {exc}", file=sys.stderr)
                break

            # Clear trace file for next round
            if config.mcdc_trace_file.exists():
                config.mcdc_trace_file.write_text("", encoding="utf-8")

        # Final evaluation
        traces = parse_mcdc_trace_file(config.mcdc_trace_file)
        mcdc_pairs = evaluate_mcdc_coverage(decisions, traces)
        final_pct = compute_mcdc_coverage_percent(mcdc_pairs)
        print(f"MC/DC final coverage: {final_pct:.1f}%")

    report = write_report(
        config,
        decisions,
        llm_payload,
        build_results,
        test_results,
        coverage_results,
        coverage_summary,
        functions=functions,
        function_manifest=function_manifest,
        mcdc_pairs=mcdc_pairs,
    )
    print(f"wrote MC/DC report: {report}")
    failed = any(item["returncode"] != 0 for item in build_results + test_results + coverage_results)
    return 1 if failed else 0


def cmd_instrument(args: argparse.Namespace) -> int:
    """Instrument source files for MCDC tracing."""
    config = load_config(Path(args.config))
    decisions = discover_decisions(config)
    if not decisions:
        print("no decisions found in configured source files")
        return 0
    generated = instrument_project(config, decisions)
    print(f"instrumented {len(generated)} file(s) into {config.instrument_dir}")
    for path in generated:
        print(f"  {path}")
    return 0


def cmd_analyze_mcdc(args: argparse.Namespace) -> int:
    """Analyze MCDC trace file and report coverage."""
    config = load_config(Path(args.config))
    decisions = discover_decisions(config)
    trace_path = config.mcdc_trace_file
    if args.trace_file:
        trace_path = Path(args.trace_file).resolve()
    traces = parse_mcdc_trace_file(trace_path)
    if not traces:
        print(f"no traces found in {trace_path}")
        return 1
    pairs = evaluate_mcdc_coverage(decisions, traces)
    coverage_pct = compute_mcdc_coverage_percent(pairs)
    satisfied = sum(1 for p in pairs if p.satisfied)
    print(f"MC/DC coverage: {coverage_pct:.1f}% ({satisfied}/{len(pairs)} pairs satisfied)")
    for pair in pairs:
        status = "SATISFIED" if pair.satisfied else "UNCOVERED"
        print(f"  {pair.decision_file}:{pair.decision_line} cond[{pair.condition_index}] "
              f"'{pair.condition_text}' -> {status}")
        if pair.satisfied:
            print(f"    true_test={pair.test_true}, false_test={pair.test_false}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and run CUnit tests for C projects with MC/DC-oriented feedback."
    )
    sub = parser.add_subparsers(required=True)

    init = sub.add_parser("init", help="write an example JSON configuration")
    init.add_argument("-o", "--output", default="cunit_mcdc_config.json")
    init.set_defaults(func=cmd_init)

    scan = sub.add_parser("scan", help="scan configured C files and print decisions")
    scan.add_argument("-c", "--config", required=True)
    scan.set_defaults(func=cmd_scan)

    scan_functions = sub.add_parser("scan-functions", help="scan configured .c files and print functions")
    scan_functions.add_argument("-c", "--config", required=True)
    scan_functions.set_defaults(func=cmd_scan_functions)

    gen = sub.add_parser("generate", help="call the LLM and write a CUnit test file")
    gen.add_argument("-c", "--config", required=True)
    gen.add_argument("--dry-run-prompt", action="store_true")
    gen.set_defaults(func=cmd_generate)

    gen_functions = sub.add_parser("generate-functions", help="generate batched CUnit tests for discovered C functions")
    gen_functions.add_argument("-c", "--config", required=True)
    gen_functions.add_argument("--dry-run-prompt", action="store_true")
    gen_functions.set_defaults(func=cmd_generate_functions)

    run = sub.add_parser("run", help="optionally generate tests, then build/test/collect coverage")
    run.add_argument("-c", "--config", required=True)
    run.add_argument("--generate", action="store_true", help="generate tests before running commands")
    run.add_argument("--generate-functions", action="store_true", help="generate batched function tests before running")
    run.add_argument("--include-functions", action="store_true", help="include discovered functions in the report")
    run.set_defaults(func=cmd_run)

    instrument = sub.add_parser("instrument", help="instrument source files for MCDC tracing")
    instrument.add_argument("-c", "--config", required=True)
    instrument.set_defaults(func=cmd_instrument)

    analyze_mcdc = sub.add_parser("analyze-mcdc", help="analyze MCDC trace file and report coverage")
    analyze_mcdc.add_argument("-c", "--config", required=True)
    analyze_mcdc.add_argument("--trace-file", default=None, help="path to MCDC trace file (overrides config)")
    analyze_mcdc.set_defaults(func=cmd_analyze_mcdc)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
