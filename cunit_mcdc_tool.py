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
import platform
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
    return_type: str
    params: str
    body: str
    start_line: int
    end_line: int
    is_computation: bool


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
    extra_prompt: str
    auto_build: bool
    compiler: str
    cunit_include_dir: str
    cunit_lib_dir: str


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    project_root = Path(raw.get("project_root", ".")).expanduser().resolve()
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
        extra_prompt=raw.get("extra_prompt", ""),
        auto_build=bool(raw.get("auto_build", False)),
        compiler=raw.get("compiler", "gcc"),
        cunit_include_dir=raw.get("cunit_include_dir", ""),
        cunit_lib_dir=raw.get("cunit_lib_dir", ""),
    )


# --------------------------------------------------------------------------
# APIKEY.txt support
#
# APIKEY.txt is a simple 3-line file holding the LLM connection settings so
# users do not have to scatter the base URL / model / secret across shell env
# vars or the JSON config. Format (one key per line, order-independent):
#
#     URL: https://open.bigmodel.cn/api/paas/v4
#     MODEL: glm-5.1
#     API-KEY: <your key>
#
# When present it wins over llm_base_url / llm_model from JSON for those two
# fields, and the key is injected into the process env var named by
# config.llm_api_key_env (it is never written back to disk).
# --------------------------------------------------------------------------

APIKEY_ALIASES = {
    "url": "url", "baseurl": "url", "base_url": "url", "apibase": "url",
    "api_base": "url", "endpoint": "url", "base": "url",
    "model": "model", "modelname": "model",
    "api-key": "key", "api_key": "key", "apikey": "key", "key": "key", "secret": "key",
}


def load_apikey_file(path: Path) -> dict[str, str] | None:
    """Parse an API key file. Two formats are accepted, mixed or pure:

    1. Prefixed (recommended), order-independent:
         URL: https://open.bigmodel.cn/api/paas/v4
         MODEL: glm-5.2
         API-KEY: <key>

    2. Bare / positional (one token per line, no prefix):
         sk-...                       # key (OpenAI/DeepSeek/MiMo style)
         https://api.xiaomimimo.com/v1 # base URL
         mimo-v2.5-pro                # optional model id

    Returns a dict with optional 'url' / 'model' / 'key', or None.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return None
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Prefixed form: "FIELD: value"
        if ":" in line:
            head, _, value = line.partition(":")
            norm = head.strip().lower().replace(" ", "").replace("-", "")
            field = APIKEY_ALIASES.get(norm)
            if field and value.strip():
                result.setdefault(field, value.strip())
                continue
        # Bare positional form
        if line.startswith(("http://", "https://")):
            result.setdefault("url", line)
        elif line.startswith("sk-") or re.fullmatch(r"[0-9a-fA-F]{32}\.[A-Za-z0-9]+", line):
            result.setdefault("key", line)
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-/]{1,63}", line):
            result.setdefault("model", line)
    return result or None


def find_apikey_file(config_path: Path | None, config: Config) -> Path | None:
    """Search for APIKEY.txt beside the config, in project_root, and in cwd."""
    candidates: list[Path] = []
    if config_path is not None:
        candidates.append(Path(config_path).resolve().parent)
    candidates.append(config.project_root)
    candidates.append(Path.cwd())
    seen: set[Path] = set()
    for base in candidates:
        base_resolved = base.resolve()
        if base_resolved in seen:
            continue
        seen.add(base_resolved)
        candidate = base_resolved / "APIKEY.txt"
        if candidate.is_file():
            return candidate
    return None


def apply_apikey_file(
    config: Config, config_path: Path | None, apikey_file: Path | str | None = None
) -> tuple[Config, Path | None]:
    """Apply APIKEY.txt to config in place. Returns (config, used_file_or_None)."""
    path = Path(apikey_file) if apikey_file else None
    if path is None:
        path = find_apikey_file(config_path, config)
    if path is None:
        return config, None
    data = load_apikey_file(path)
    if not data:
        return config, None
    if data.get("url"):
        config.llm_base_url = data["url"]
    if data.get("model"):
        config.llm_model = data["model"]
    if data.get("key"):
        os.environ[config.llm_api_key_env] = data["key"]
    return config, path


def autodetect_cunit_dirs() -> tuple[str, str]:
    """Probe common MSYS2/MinGW install roots for CUnit headers and lib.

    Returns (include_dir, lib_dir); either may be '' if not found. Looks for
    <root>/include/CUnit/Basic.h and <root>/lib/libcunit*.
    """
    candidate_roots: list[Path] = []
    for envname in ("CUNIT_HOME", "CUNIT_ROOT"):
        envval = os.environ.get(envname)
        if envval:
            candidate_roots.append(Path(envval))
    for drive in ("C:", "D:"):
        for sub in ("msys64/ucrt64", "msys64/mingw64", "msys32/ucrt64", "msys32/mingw64"):
            candidate_roots.append(Path(f"{drive}/{sub}"))
    for root in candidate_roots:
        include_dir = root / "include"
        if (include_dir / "CUnit" / "Basic.h").is_file():
            lib_dir = root / "lib"
            if not any(lib_dir.glob("libcunit*")):
                lib_dir = root  # fall back: some layouts put libs under root
            return str(include_dir), str(lib_dir) if any(Path(lib_dir).glob("libcunit*")) else ""
    return "", ""


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


def discover_decisions(config: Config) -> list[Decision]:
    decisions: list[Decision] = []
    keywords = ["if", "while", "switch"]
    for path in iter_source_files(config):
        raw = path.read_text(encoding="utf-8", errors="ignore")
        clean = strip_comments_and_strings(raw)
        rel = str(path.relative_to(config.project_root))
        found: list[tuple[int, str]] = []
        for keyword in keywords:
            found.extend(extract_parenthesized_after_keyword(clean, keyword))
        found.extend(extract_for_condition(clean))
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


def extract_functions_from_source(config: Config) -> list[CFunction]:
    functions: list[CFunction] = []
    for src_path in iter_source_files(config):
        raw = src_path.read_text(encoding='utf-8', errors='ignore')
        clean = strip_comments_and_strings(raw)
        rel = str(src_path.relative_to(config.project_root))
        # Skip the entry translation unit (defines main): it is excluded from
        # the auto-compile so its symbols are unavailable, and its entry point
        # plus helpers are not unit-testable from a separate translation unit.
        if re.search(r"\bint\s+main\s*\(", clean):
            continue
        func_pattern = re.compile(
            r"(?P<ret>[\w][\w\s\*]*?)\s+"
            r"(?P<name>[a-zA-Z_]\w*)\s*"
            r"\((?P<params>[^)]*)\)\s*\{"
        )
        for match in func_pattern.finditer(clean):
            name = match.group('name')
            if name in ('if', 'while', 'for', 'switch', 'return', 'sizeof', 'typedef', 'main'):
                continue
            ret_type = match.group('ret').strip()
            # static functions have internal linkage and cannot be called from
            # the test translation unit, so skip them.
            if "static" in ret_type.split():
                continue
            params = match.group('params').strip()
            start_line = clean[:match.start()].count(chr(10)) + 1
            brace_start = match.end() - 1
            depth = 0
            body_end = brace_start
            for i in range(brace_start, len(clean)):
                if clean[i] == '{':
                    depth += 1
                elif clean[i] == '}':
                    depth -= 1
                    if depth == 0:
                        body_end = i
                        break
            body = clean[match.start():body_end + 1]
            end_line = clean[:body_end].count(chr(10)) + 1
            is_comp = _is_computation_function(body)
            functions.append(CFunction(
                file=rel, name=name, return_type=ret_type, params=params,
                body=body, start_line=start_line, end_line=end_line,
                is_computation=is_comp,
            ))
    return functions


def _is_computation_function(body: str) -> bool:
    comp_patterns = [
        r"\+=", r"-=", r"\*=", r"/=",
        r"\bsqrt\b", r"\babs\b", r"\bfabs\b", r"\bpow\b",
        r"\bsin\b", r"\bcos\b", r"\btan\b", r"\bceil\b",
        r"\bfloor\b", r"\bround\b",
        r"return\s+\w+\s*[+\-*/]",
        r"\b(?:int|float|double|long)\b.*=.*[+\-*/]",
    ]
    for pat in comp_patterns:
        if re.search(pat, body):
            return True
    return False


def compact_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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
    source_summaries = []
    for path in iter_source_files(config):
        rel = path.relative_to(config.project_root)
        text = path.read_text(encoding="utf-8", errors="ignore")
        snippet = text[:7000]
        source_summaries.append(f"// FILE: {rel}\n{snippet}")

    source_blob = "\n\n".join(source_summaries)
    decision_payload = [
        {
            "file": d.file,
            "line": d.line,
            "expression": d.expression,
            "conditions": d.conditions,
            "mcdc_obligations": mcdc_obligations(d),
        }
        for d in decisions
    ]
    include_flags = " ".join(f"-I{item}" for item in config.include_dirs)
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

        Project root: {config.project_root}
        Include flags: {include_flags}
        Test output path: {config.test_output}
        Extra user prompt: {config.extra_prompt}

        Decisions and MC/DC obligations:
        {json.dumps(decision_payload, ensure_ascii=False, indent=2)}

        Source excerpts:
        {source_blob}
        """
    ).strip()



def build_function_prompt(config: Config, func: CFunction, decisions: list[Decision]) -> str:
    """Build a per-function prompt for LLM, sending only this function source and its decisions."""
    func_decisions = [
        d for d in decisions
        if d.file == func.file and func.start_line <= d.line <= func.end_line
    ]
    decision_payload = [
        {
            "file": d.file,
            "line": d.line,
            "expression": d.expression,
            "conditions": d.conditions,
            "mcdc_obligations": mcdc_obligations(d),
        }
        for d in func_decisions
    ]
    include_flags = " ".join(f"-I{item}" for item in config.include_dirs)

    computation_section = ""
    if func.is_computation:
        computation_section = textwrap.dedent("""\
            IMPORTANT - Computation/Boundary Value Testing:
            This function involves numerical computation. You MUST generate test cases
            that cover the following boundary value categories for EACH numeric parameter:
            - Maximum value (e.g., INT_MAX, FLT_MAX, or the largest valid input)
            - Minimum value (e.g., INT_MIN, FLT_MIN, or the smallest valid input)
            - Normal/typical values (representative middle-range values)
            - Abnormal/edge values (zero, negative if applicable, near-overflow, NaN for floats)
            - Boundary values at decision points (values exactly at comparison boundaries)
            For each test case, you MUST compute and assert the expected return value.
            Use explicit CU_ASSERT_EQUAL, CU_ASSERT_DOUBLE_EQUAL, or equivalent assertions
            with the precise expected value. Do NOT use CU_PASS for computation tests;
            always verify the actual computed result matches the expected value.
            If the function uses floating-point, use CU_ASSERT_DOUBLE_EQUAL with tolerance.
        """)

    return textwrap.dedent(
        f"""\
        You are generating CUnit tests for a SINGLE function in a C project.
        Target MC/DC coverage for every listed decision within this function.
        Return only JSON with this exact schema:
        {{
          "test_functions": "C source code containing ONLY the test functions (no include, no main, no CU_setup)",
          "test_function_names": ["list of test function names generated"],
          "notes": ["brief note"],
          "assumptions": ["brief assumption"]
        }}

        Requirements:
        - Use CUnit/Basic.h assertions (CU_ASSERT_*, CU_FAIL, etc.).
        - Create focused tests that exercise MC/DC pairs.
        - Keep tests deterministic.
        - Do NOT include include directives, main(), or CU_registry setup.
        - Output ONLY the test function bodies.
        - Do not include Markdown fences.
        - Mention any required stubs in notes if external dependencies block testing.
        {computation_section}
        Project root: {config.project_root}
        Include flags: {include_flags}
        Extra user prompt: {config.extra_prompt}

        Function: {func.name}
        File: {func.file}:{func.start_line}-{func.end_line}
        Return type: {func.return_type}
        Parameters: {func.params}
        Is computation function: {func.is_computation}

        Decisions and MC/DC obligations for this function:
        {json.dumps(decision_payload, ensure_ascii=False, indent=2) if decision_payload else "No MC/DC decisions found in this function."}

        Function source code:
        {func.body}
        """
    ).strip()


def generate_all_functions(config: Config, decisions: list[Decision] | None = None) -> dict[str, Any]:
    """Generate CUnit tests for each function individually and merge results."""
    if decisions is None:
        decisions = discover_decisions(config)
    functions = extract_functions_from_source(config)
    if not functions:
        raise RuntimeError("no functions found in configured source files")

    all_test_functions = []
    all_test_names = []
    all_notes = []
    all_assumptions = []
    errors = []

    for func in functions:
        print(f"  Generating tests for {func.file}::{func.name} (lines {func.start_line}-{func.end_line})...")
        prompt = build_function_prompt(config, func, decisions)
        try:
            payload = call_llm(config, prompt)
            test_code = payload.get("test_functions", "")
            func_names = payload.get("test_function_names", [])
            if not test_code.strip():
                errors.append(f"{func.file}::{func.name}: LLM returned empty test_functions")
                continue
            all_test_functions.append(f"// ---- Tests for {func.file}::{func.name} ----\n{test_code}")
            all_test_names.extend(func_names)
            all_notes.extend(payload.get("notes", []))
            all_assumptions.extend(payload.get("assumptions", []))
        except Exception as exc:
            errors.append(f"{func.file}::{func.name}: {exc}")

    return {
        "test_functions_parts": all_test_functions,
        "test_function_names": all_test_names,
        "notes": all_notes,
        "assumptions": all_assumptions,
        "errors": errors,
    }


def merge_test_parts(config: Config, gen_result: dict[str, Any]) -> str:
    """Merge per-function test code into a single compilable CUnit test file."""
    parts = gen_result.get("test_functions_parts", [])
    test_names = gen_result.get("test_function_names", [])
    if not parts:
        raise RuntimeError("no test functions were generated")

    source_files = set()
    for path in iter_source_files(config):
        rel = path.relative_to(config.project_root)
        source_files.add(rel)

    lines = [
        "/* Auto-generated CUnit tests - per-function generation */",
        "#include <CUnit/Basic.h>",
        "#include <stdio.h>",
        "#include <stdlib.h>",
        "#include <string.h>",
        "#include <limits.h>",
        "#include <math.h>",
    ]
    # Include every header reachable via the configured -I dirs (include_dirs),
    # using the path relative to that -I dir with forward slashes. This resolves
    # correctly regardless of the test file's location or OS path separators,
    # and picks up headers (e.g. src/*.h) that the source_globs may not match.
    seen_headers: set[str] = set()
    for inc_dir_str in config.include_dirs:
        inc_dir = (config.project_root / inc_dir_str).resolve()
        if not inc_dir.is_dir():
            continue
        for hdr in sorted(inc_dir.rglob("*.h")):
            rel = hdr.relative_to(inc_dir).as_posix()
            if rel not in seen_headers:
                seen_headers.add(rel)
                lines.append(f"#include \"{rel}\"")

    lines.append("")
    lines.append("// ---- Test functions ----")

    for part in parts:
        lines.append(part)
        lines.append("")

    lines.append("// ---- CUnit registry and suite setup ----")
    lines.append("static int suite_init(void) { return 0; }")
    lines.append("static int suite_clean(void) { return 0; }")
    lines.append("")
    lines.append("int main(void) {")
    lines.append("    if (CU_initialize_registry() != CUE_SUCCESS)")
    lines.append("        return CU_get_error();")
    lines.append("    CU_pSuite suite = CU_add_suite(\"AutoMCDC\", suite_init, suite_clean);")
    lines.append("    if (!suite) { CU_cleanup_registry(); return CU_get_error(); }")
    lines.append("")

    for name in test_names:
        lines.append(f"    CU_add_test(suite, \"{name}\", {name});")

    lines.append("")
    lines.append("    CU_basic_set_mode(CU_BRM_VERBOSE);")
    lines.append("    CU_basic_run_tests();")
    lines.append("    unsigned int failures = CU_get_number_of_failures();")
    lines.append("    CU_cleanup_registry();")
    lines.append("    return failures > 0 ? 1 : 0;")
    lines.append("}")
    lines.append("")

    return chr(10).join(lines)


def _file_defines_main(path: Path) -> bool:
    """True if the C file defines a top-level main() entry point."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    clean = strip_comments_and_strings(raw)
    return re.search(r"\bint\s+main\s*\(", clean) is not None


def auto_compile_and_run(config: Config, test_source_path: Path) -> tuple[int, str]:
    """Automatically compile the generated test file and run it.

    Excludes any project .c that defines main() (e.g. src/main.c) so the
    generated test file's own main() does not clash at link time. Auto-detects
    CUnit include/lib dirs from common MSYS2/MinGW roots when not configured.
    """
    compiler = config.compiler or "gcc"

    cunit_include_dir = config.cunit_include_dir
    cunit_lib_dir = config.cunit_lib_dir
    if not cunit_include_dir or not cunit_lib_dir:
        det_inc, det_lib = autodetect_cunit_dirs()
        cunit_include_dir = cunit_include_dir or det_inc
        cunit_lib_dir = cunit_lib_dir or det_lib
        if cunit_include_dir:
            print(f"  Auto-detected CUnit include: {cunit_include_dir}")
        if cunit_lib_dir:
            print(f"  Auto-detected CUnit lib:     {cunit_lib_dir}")

    include_flags = [f"-I{d}" for d in config.include_dirs]
    if cunit_include_dir:
        include_flags.append(f"-I{cunit_include_dir}")

    lib_flags: list[str] = []
    if cunit_lib_dir:
        lib_flags.extend(["-L", cunit_lib_dir])
    lib_flags.extend(["-lcunit", "-lm"])

    test_source_resolved = test_source_path.resolve()
    source_files: list[str] = []
    for path in iter_source_files(config):
        rel = str(path.relative_to(config.project_root))
        if not rel.endswith(".c"):
            continue
        if path.resolve() == test_source_resolved:
            continue
        if _file_defines_main(path):
            print(f"  Skipping (defines main): {rel}")
            continue
        source_files.append(str(path))

    ext = ".exe" if platform.system() == "Windows" else ""
    binary_path = test_source_path.with_suffix(ext)

    compile_argv = [
        compiler,
        *include_flags,
        str(test_source_path),
        *source_files,
        *lib_flags,
        "-o", str(binary_path),
    ]

    print(f"  Compiling: {' '.join(compile_argv)}")
    compile_rc, compile_output = run_command_argv(compile_argv, config.project_root)

    if compile_rc != 0:
        return compile_rc, f"COMPILATION FAILED:\n{compile_output}"

    print(f"  Running: {binary_path}")
    run_rc, run_output = run_command_argv([str(binary_path)], config.project_root)
    return run_rc, f"COMPILATION OK:\n{run_output}"


def auto_build_and_run(config: Config, test_source_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Auto compile+run, return build_results and test_results in standard format."""
    rc, output = auto_compile_and_run(config, test_source_path)
    is_compile_fail = "COMPILATION FAILED" in output
    build_result = {"command": "auto_compile", "returncode": rc if is_compile_fail else 0, "output": output}
    test_result = {"command": "auto_run", "returncode": 0 if is_compile_fail else rc, "output": output}
    return [build_result], [test_result]

def call_llm(config: Config, prompt: str) -> dict[str, Any]:
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        # Fallback: auto-discover APIKEY.txt near cwd / project root.
        used = apply_apikey_file(config, None)[1]
        api_key = os.environ.get(config.llm_api_key_env)
        if not api_key:
            hint = f" (found {used}, but no API-KEY line)" if used else ""
            raise RuntimeError(
                f"environment variable {config.llm_api_key_env} is not set and no APIKEY.txt found{hint}"
            )
    base = config.llm_base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/chat/completions"
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


def run_command_argv(argv: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command given as an argv list (no shell). Safer on Windows where
    paths frequently contain spaces, and avoids quoting pitfalls."""
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        return 127, f"$ {' '.join(argv)}\n# command not found: {exc}\n"
    elapsed = time.time() - started
    header = f"$ {' '.join(argv)}\n# exit={proc.returncode}, elapsed={elapsed:.1f}s\n"
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



def parse_cunit_output(output: str) -> dict[str, Any]:
    """Parse CUnit verbose output.

    Returns {"test_cases": [...], "summary": {...}}. The summary captures the
    CUnit Run Summary totals (tests/asserts: total, ran, passed, failed).
    """
    test_cases = []
    pattern = re.compile(r"\s+Test: (.+?)\s+\.{3}\s*(passed|FAILED)")
    for m in pattern.finditer(output):
        name = m.group(1).strip()
        status = m.group(2).strip().lower()
        func_name = _infer_tested_function(name)
        test_cases.append({"name": name, "status": status, "function": func_name})
    summary: dict[str, Any] = {}
    sm = re.search(r"Run Summary:.*?tests\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", output, re.DOTALL)
    if sm:
        summary.update({
            "total": int(sm.group(1)),
            "ran": int(sm.group(2)),
            "passed": int(sm.group(3)),
            "failed": int(sm.group(4)),
        })
    am = re.search(r"asserts\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", output)
    if am:
        summary.update({
            "asserts_total": int(am.group(1)),
            "asserts_ran": int(am.group(2)),
            "asserts_passed": int(am.group(3)),
            "asserts_failed": int(am.group(4)),
        })
    return {"test_cases": test_cases, "summary": summary}

def _infer_tested_function(test_name: str) -> str:
    """Infer which C function a test function tests based on naming conventions."""
    name = test_name.lower()
    for prefix in ["test_", "test"]:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    parts = name.split("_")
    if parts:
        return parts[0] if parts[0] else (parts[1] if len(parts)>1 else test_name)
    return test_name

def write_report(
    config: Config,
    decisions: list[Decision],
    llm_payload: dict[str, Any] | None,
    build_results: list[dict[str, Any]],
    test_results: list[dict[str, Any]],
    coverage_results: list[dict[str, Any]],
    coverage_summary: dict[str, Any],
) -> Path:
    report_path = config.test_output.with_suffix(".mcdc_report.json")
    html_path = report_path.with_suffix(".html")
    decision_reports = []
    for decision in decisions:
        item = dataclasses.asdict(decision)
        item["mcdc_obligations"] = mcdc_obligations(decision)
        decision_reports.append(item)
    # Parse CUnit test output for per-case results and run summary
    test_case_results = []
    cunit_summary: dict[str, Any] = {}
    for item in test_results:
        parsed = parse_cunit_output(item.get("output", ""))
        test_case_results.extend(parsed["test_cases"])
        for key, value in parsed["summary"].items():
            if isinstance(value, (int, float)):
                cunit_summary[key] = cunit_summary.get(key, 0) + value
            else:
                cunit_summary[key] = value
    
    report = {
        "project_root": str(config.project_root),
        "test_output": str(config.test_output),
        "html_report": str(html_path),
        "decision_count": len(decisions),
        "decisions": decision_reports,
        "llm_notes": (llm_payload or {}).get("notes", []),
        "llm_assumptions": (llm_payload or {}).get("assumptions", []),
        "build_results": summarize_results(build_results),
        "test_results": summarize_results(test_results),
        "coverage_results": summarize_results(coverage_results),
        "coverage_summary": coverage_summary,
        "test_case_results": test_case_results,
        "cunit_summary": cunit_summary,
        "mcdc_status": (
            "needs_manual_review: branch/line coverage does not prove MC/DC; "
            "confirm listed independent-condition pairs with generated tests"
        ),
    }
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
    rows = []
    test_cases = report.get("test_case_results", [])
    cunit_sum = report.get("cunit_summary", {})
    tc_rows = []
    for tc in test_cases:
        s = tc.get("status", "unknown")
        tc_rows.append(
            '<tr><td>' + html.escape(tc.get("name","")) + '</td>'
            + '<td class=' + chr(34) + s + chr(34) + '>' + s + '</td>'
            + '<td>' + html.escape(tc.get("function","")) + '</td></tr>')
    if not tc_rows:
        tc_rows = ["<tr><td colspan=3>No test case results parsed</td></tr>"]
    tc_passed = sum(1 for t in test_cases if t.get("status")=="passed")
    tc_failed = sum(1 for t in test_cases if t.get("status")=="failed")

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
    <div class="card"><div class="label">行覆盖率</div><div class="value">{pct("lines_executed_percent")}</div></div>
    <div class="card"><div class="label">分支覆盖率</div><div class="value">{pct("branches_executed_percent")}</div></div>
    <div class="card"><div class="label">分支至少执行一次</div><div class="value">{pct("branches_taken_at_least_once_percent")}</div></div>
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

  <h2>测试用例结果</h2>
  <table>
    <tr><th>用例名称</th><th>状态</th><th>被测函数</th></tr>
    {"".join(tc_rows)}
  </table>

  <h2>测试汇总</h2>
  <div class="grid">
    <div class="card"><div class="label">总用例数</div><div class="value">{len(test_cases)}</div></div>
    <div class="card"><div class="label">通过</div><div class="value passed">{tc_passed}</div></div>
    <div class="card"><div class="label">失败</div><div class="value failed">{tc_failed}</div></div>
    <div class="card"><div class="label">断言数</div><div class="value">{cunit_sum.get("asserts_total", 0)}</div></div>
  </div>

  <h2>未覆盖行样例</h2>
  <table>
    <tr><th>GCOV 文件</th><th>内容</th></tr>
    {"".join(uncovered_rows) if uncovered_rows else "<tr><td colspan='2'>未发现样例或未生成 .gcov 文件</td></tr>"}
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
        "extra_prompt": "Prefer testing public APIs; create local stubs only when necessary.",
        "auto_build": False,
        "compiler": "gcc",
        "cunit_include_dir": "",
        "cunit_lib_dir": "",
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


def cmd_generate(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    used = apply_apikey_file(config, Path(args.config), getattr(args, "apikey_file", None))[1]
    if used:
        print(f"loaded LLM settings from: {used}")
    decisions = discover_decisions(config)
    if not decisions:
        raise RuntimeError("no decisions found in configured source files")
    if args.per_function:
        return _cmd_generate_per_function(config, dry_run=args.dry_run_prompt)
    prompt = build_prompt(config, decisions)
    if args.dry_run_prompt:
        print(prompt)
        return 0
    llm_payload = call_llm(config, prompt)
    test_file = llm_payload.get("test_file")
    if not isinstance(test_file, str) or "#include" not in test_file:
        raise RuntimeError("LLM response did not contain a valid test_file")
    ensure_parent(config.test_output)
    config.test_output.write_text(test_file.rstrip() + chr(10), encoding="utf-8")
    print(f"wrote generated CUnit tests: {config.test_output}")
    return 0


def _cmd_generate_per_function(config: Config, dry_run: bool = False) -> int:
    """Generate tests per function (avoids token limit issues)."""
    decisions = discover_decisions(config)
    functions = extract_functions_from_source(config)
    if not functions:
        raise RuntimeError("no functions found in configured source files")

    if dry_run:
        for func in functions:
            prompt = build_function_prompt(config, func, decisions)
            print(f"=== PROMPT FOR {func.file}::{func.name} ===")
            print(prompt)
            print()
        return 0

    print(f"Generating tests for {len(functions)} functions...")
    gen_result = generate_all_functions(config, decisions)

    if gen_result.get("errors"):
        for err in gen_result["errors"]:
            print(f"  WARNING: {err}", file=sys.stderr)

    test_content = merge_test_parts(config, gen_result)
    ensure_parent(config.test_output)
    config.test_output.write_text(test_content, encoding="utf-8")
    print(f"wrote per-function CUnit tests: {config.test_output}")
    tfn_key = "test_function_names"
    print(f"  total test functions: {len(gen_result.get(tfn_key, []))}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    used = apply_apikey_file(config, Path(args.config), getattr(args, "apikey_file", None))[1]
    if used:
        print(f"loaded LLM settings from: {used}")
    decisions = discover_decisions(config)
    llm_payload = None
    gen_result = None

    if args.generate:
        if args.per_function:
            print("Generating tests per function...")
            gen_result = generate_all_functions(config, decisions)
            if gen_result.get("errors"):
                for err in gen_result["errors"]:
                    print(f"  WARNING: {err}", file=sys.stderr)
            test_content = merge_test_parts(config, gen_result)
            ensure_parent(config.test_output)
            config.test_output.write_text(test_content, encoding="utf-8")
        else:
            prompt = build_prompt(config, decisions)
            llm_payload = call_llm(config, prompt)
            ensure_parent(config.test_output)
            tf=llm_payload["test_file"]
            config.test_output.write_text(tf.rstrip()+chr(10), encoding="utf-8")

    if config.auto_build or args.auto_build:
        print("Auto-compiling and running tests...")
        build_results, test_results = auto_build_and_run(config, config.test_output)
        coverage_results = []
    else:
        build_results = run_commands(config.build_commands, config.project_root)
        if any(item["returncode"] != 0 for item in build_results):
            test_results = []
            coverage_results = []
        else:
            test_results = run_commands(config.test_commands, config.project_root)
            coverage_results = run_commands(config.coverage_commands, config.project_root)

    coverage_summary = collect_coverage_summary(config, coverage_results)
    report = write_report(config, decisions, llm_payload, build_results, test_results, coverage_results, coverage_summary)
    print(f"wrote MC/DC report: {report}")
    failed = any(item["returncode"] != 0 for item in build_results + test_results + coverage_results)
    return 1 if failed else 0


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

    gen = sub.add_parser("generate", help="call the LLM and write a CUnit test file")
    gen.add_argument("-c", "--config", required=True)
    gen.add_argument("--dry-run-prompt", action="store_true")
    gen.add_argument("--per-function", action="store_true", help="generate tests one function at a time to avoid token limits")
    gen.add_argument("--apikey-file", default=None, help="path to APIKEY.txt; auto-discovers if omitted")
    gen.set_defaults(func=cmd_generate)

    gen_funcs = sub.add_parser("generate-functions", help="generate tests per-function (avoids token limit)")
    gen_funcs.add_argument("-c", "--config", required=True)
    gen_funcs.add_argument("--dry-run-prompt", action="store_true")
    gen_funcs.add_argument("--apikey-file", default=None, help="path to APIKEY.txt; auto-discovers if omitted")
    gen_funcs.set_defaults(func=lambda args: _cmd_generate_per_function(
        apply_apikey_file(load_config(Path(args.config)), Path(args.config), args.apikey_file)[0],
        dry_run=args.dry_run_prompt))

    run = sub.add_parser("run", help="optionally generate tests, then build/test/collect coverage")
    run.add_argument("-c", "--config", required=True)
    run.add_argument("--generate", action="store_true", help="generate tests before running commands")
    run.add_argument("--per-function", action="store_true", help="use per-function generation")
    run.add_argument("--auto-build", action="store_true", help="auto-compile and run instead of using configured commands")
    run.add_argument("--apikey-file", default=None, help="path to APIKEY.txt; auto-discovers if omitted")
    run.set_defaults(func=cmd_run)

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
