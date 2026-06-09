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


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
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
        llm_context_char_limit=int(raw.get("llm_context_char_limit", 36000)),
        source_excerpt_radius=int(raw.get("source_excerpt_radius", 80)),
        max_decisions_per_prompt=int(raw.get("max_decisions_per_prompt", 60)),
        function_test_dir=(project_root / raw.get("function_test_dir", "tests/auto_function_tests")).resolve(),
        runner_output=(project_root / raw.get("runner_output", "tests/auto_cunit_runner.c")).resolve(),
        max_functions_per_prompt=int(raw.get("max_functions_per_prompt", 8)),
        extra_prompt=raw.get("extra_prompt", ""),
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
        Context limit note:
        - This prompt contains local source excerpts around target decisions, not the full project.
        - Decisions included in this request: {len(selected_decisions)}
        - Decisions omitted from this request because of max_decisions_per_prompt: {omitted_decision_count}
        - If omitted_decision_count is greater than 0, generate tests for the included decisions only.

        Decisions and MC/DC obligations:
        {json.dumps(decision_payload, ensure_ascii=False, indent=2)}

        Source excerpts:
        {source_blob}
        """
    ).strip()


def build_source_context(config: Config, decisions: list[Decision]) -> str:
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

        Requirements:
        - Use CUnit/Basic.h.
        - Do not define main().
        - Define this registration function exactly: void {register_name}(void)
        - Inside {register_name}, call CU_add_suite and CU_add_test for this batch.
        - Generate tests for every listed function.
        - For functions with decisions, target MC/DC pairs for each listed decision.
        - For functions without decisions, generate input/output, boundary, and error-path tests where possible.
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


def call_llm(config: Config, prompt: str) -> dict[str, Any]:
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        raise RuntimeError(f"environment variable {config.llm_api_key_env} is not set")
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
    ensure_parent(config.function_test_dir / "placeholder")
    generated_files = []
    notes = []
    assumptions = []
    for batch_index, batch in enumerate(batch_functions(config, functions), start=1):
        batch_id = make_batch_id(batch, batch_index)
        prompt = build_function_prompt(config, batch, batch_id)
        payload = call_llm(config, prompt)
        test_file = payload.get("test_file")
        if not isinstance(test_file, str) or "#include" not in test_file:
            raise RuntimeError(f"LLM response for batch {batch_id} did not contain a valid test_file")
        register_function = payload.get("register_function") or f"register_{batch_id}_tests"
        output = config.function_test_dir / f"test_{batch_id}.c"
        output.write_text(test_file.rstrip() + "\n", encoding="utf-8")
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
        "function_count": len(functions),
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
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>MC/DC 娴嬭瘯鎶ュ憡</title>
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
  <h1>MC/DC 娴嬭瘯鎶ュ憡</h1>
  <p>宸ョ▼锛?code>{html.escape(str(report.get("project_root", "")))}</code></p>
  <p>娴嬭瘯鏂囦欢锛?code>{html.escape(str(report.get("test_output", "")))}</code></p>

  <div class="grid">
    <div class="card"><div class="label">鍒ゅ畾鏁伴噺</div><div class="value">{len(decisions)}</div></div>
    <div class="card"><div class="label">鍑芥暟鏁伴噺</div><div class="value">{len(functions)}</div></div>
    <div class="card"><div class="label">琛岃鐩栫巼</div><div class="value">{pct("lines_executed_percent")}</div></div>
    <div class="card"><div class="label">鍒嗘敮瑕嗙洊鐜?/div><div class="value">{pct("branches_executed_percent")}</div></div>
    <div class="card"><div class="label">鍒嗘敮鑷冲皯鎵ц涓€娆?/div><div class="value">{pct("branches_taken_at_least_once_percent")}</div></div>
  </div>

  <h2>娴佺▼鐘舵€?/h2>
  <table>
    <tr><th>闃舵</th><th>鐘舵€?/th></tr>
    <tr><td>鏋勫缓</td><td class="{build_status}">{build_status}</td></tr>
    <tr><td>娴嬭瘯</td><td class="{test_status}">{test_status}</td></tr>
    <tr><td>瑕嗙洊鐜囬噰闆?/td><td class="{coverage_status}">{coverage_status}</td></tr>
  </table>

  <h2>MC/DC 鍒ゅ畾涓庝箟鍔?/h2>
  <div class="note">琛岃鐩栧拰鍒嗘敮瑕嗙洊涓嶈兘鍗曠嫭璇佹槑 MC/DC銆傝纭姣忎釜鏉′欢閮芥湁鐙珛褰卞搷鍒ゅ畾缁撴灉鐨勬祴璇曞銆?/div>
  <table>
    <tr><th>鏂囦欢</th><th>琛?/th><th>鍒ゅ畾琛ㄨ揪寮?/th><th>鏉′欢鏁?/th><th>闇€纭鐨勬潯浠?/th></tr>
    {"".join(rows)}
  </table>

  <h2>鍑芥暟绾ф祴璇曟枃浠?/h2>
  <p>Runner: <code>{html.escape(str(function_manifest.get("runner_output", "")))}</code></p>
  <table>
    <tr><th>鐢熸垚鐨勬祴璇曟枃浠?/th><th>娉ㄥ唽鍑芥暟</th><th>鍑芥暟鏁?/th></tr>
    {"".join(generated_rows) if generated_rows else "<tr><td colspan='3'>鏈敓鎴愬嚱鏁扮骇娴嬭瘯鏂囦欢</td></tr>"}
  </table>

  <h2>鍙戠幇鐨勫嚱鏁?/h2>
  <table>
    <tr><th>鏂囦欢</th><th>鍑芥暟</th><th>琛屽彿</th><th>static</th><th>鍒ゅ畾鏁?/th></tr>
    {"".join(function_rows) if function_rows else "<tr><td colspan='5'>鎶ュ憡涓病鏈夊嚱鏁颁俊鎭?/td></tr>"}
  </table>

  <h2>鏈鐩栬鏍蜂緥</h2>
  <table>
    <tr><th>GCOV 鏂囦欢</th><th>鍐呭</th></tr>
    {"".join(uncovered_rows) if uncovered_rows else "<tr><td colspan='2'>鏈彂鐜版牱渚嬫垨鏈敓鎴?.gcov 鏂囦欢</td></tr>"}
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
    prompt = build_prompt(config, decisions)
    if args.dry_run_prompt:
        print(prompt)
        return 0
    llm_payload = call_llm(config, prompt)
    test_file = llm_payload.get("test_file")
    if not isinstance(test_file, str) or "#include" not in test_file:
        raise RuntimeError("LLM response did not contain a valid test_file")
    ensure_parent(config.test_output)
    config.test_output.write_text(test_file.rstrip() + "\n", encoding="utf-8")
    print(f"wrote generated CUnit tests: {config.test_output}")
    return 0


def cmd_generate_functions(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    functions = discover_functions(config)
    if not functions:
        raise RuntimeError("no C functions found in configured .c source files")
    if args.dry_run_prompt:
        for batch_index, batch in enumerate(batch_functions(config, functions), start=1):
            batch_id = make_batch_id(batch, batch_index)
            print(f"\n===== FUNCTION TEST PROMPT: {batch_id} =====\n")
            print(build_function_prompt(config, batch, batch_id))
        return 0
    manifest = generate_function_tests(config, functions)
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
    if args.generate:
        prompt = build_prompt(config, decisions)
        llm_payload = call_llm(config, prompt)
        ensure_parent(config.test_output)
        config.test_output.write_text(llm_payload["test_file"].rstrip() + "\n", encoding="utf-8")
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
    )
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
