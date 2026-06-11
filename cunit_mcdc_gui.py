#!/usr/bin/env python3
"""
Tkinter GUI for cunit_mcdc_tool.py.
The GUI reuses the CLI module core functions.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

import cunit_mcdc_tool as core


APP_TITLE = "CUnit MC/DC AI Test Generator"


def list_to_lines(items: List[str]) -> str:
    return "\n".join(items)


def lines_to_list(text: str) -> List[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]




class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x780")
        self.minsize(980, 640)

        self.config_path: Optional[Path] = None
        self.worker: Optional[threading.Thread] = None
        self.events: queue.Queue[Tuple[str, Any]] = queue.Queue()

        self.vars = {
            "project_root": tk.StringVar(),
            "source_globs": tk.StringVar(value="**/*.c, **/*.h"),
            "include_dirs": tk.StringVar(),
            "test_output": tk.StringVar(value="tests/auto_mcdc_tests.c"),
            "llm_base_url": tk.StringVar(value="https://api.openai.com/v1"),
            "llm_api_key_env": tk.StringVar(value="OPENAI_API_KEY"),
            "llm_api_key_value": tk.StringVar(),
            "llm_model": tk.StringVar(value=core.DEFAULT_MODEL),
            "max_llm_rounds": tk.StringVar(value="1"),
            "llm_context_char_limit": tk.StringVar(value="36000"),
            "source_excerpt_radius": tk.StringVar(value="80"),
            "max_decisions_per_prompt": tk.StringVar(value="60"),
            "function_test_dir": tk.StringVar(value="tests/auto_function_tests"),
            "runner_output": tk.StringVar(value="tests/auto_cunit_runner.c"),
            "max_functions_per_prompt": tk.StringVar(value="8"),
            "max_prompt_tokens": tk.StringVar(value="12000"),
            "max_response_tokens": tk.StringVar(value="4096"),
        }

        self._build_style()
        self._build_layout()
        self.after(100, self._drain_events)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TButton", padding=(10, 5))
        style.configure("Accent.TButton", padding=(12, 6))
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", foreground="#31566d")

    def _build_layout(self) -> None:
        top = ttk.Frame(self, padding=(12, 10, 12, 6))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Config File").pack(side=tk.LEFT)
        self.config_entry = ttk.Entry(top)
        self.config_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        ttk.Button(top, text="Open", command=self.open_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Save", command=self.save_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Save As", command=self.save_config_as).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="New Example", command=self.new_example_config).pack(side=tk.LEFT, padx=2)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=3)
        main.add(right, weight=4)

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self._build_config_tab()
        self._build_commands_tab()
        self._build_prompt_tab()

        self.result_notebook = ttk.Notebook(right)
        self.result_notebook.pack(fill=tk.BOTH, expand=True)
        self._build_decision_tab()
        self._build_function_tab()
        self._build_log_tab()
        self._build_report_tab()

        actions = ttk.Frame(self, padding=(12, 0, 12, 10))
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="Scan Decisions",
                   command=self.scan_decisions).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="Scan Functions",
                   command=self.scan_functions).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Preview Prompt",
                   command=self.preview_prompt).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Generate CUnit",
                   command=self.generate_tests).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Gen Func Tests",
                   command=self.generate_function_tests).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Func Tests + Run",
                   command=lambda: self.run_function_pipeline()).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Gen + Run",
                   command=lambda: self.run_pipeline(generate=True)).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Run Only",
                   command=lambda: self.run_pipeline(generate=False)).pack(side=tk.LEFT, padx=6)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(actions, textvariable=self.status_var,
                  style="Status.TLabel").pack(side=tk.RIGHT)



    def _build_config_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Config")
        tab.columnconfigure(1, weight=1)
        self._row_entry(tab, 0, "Project Root", "project_root",
                        browse=self.browse_project_root)
        self._row_entry(tab, 1, "Source Globs", "source_globs")
        self._row_entry(tab, 2, "Include Dirs", "include_dirs")
        self._row_entry(tab, 3, "Test Output", "test_output",
                        browse=self.browse_test_output)
        self._row_entry(tab, 4, "LLM Base URL", "llm_base_url")
        self._row_entry(tab, 5, "API Key Env Var", "llm_api_key_env")
        self._row_entry(tab, 6, "API Key Value", "llm_api_key_value", show="*")
        self._row_entry(tab, 7, "Model", "llm_model")
        self._row_entry(tab, 8, "Max LLM Rounds", "max_llm_rounds")
        self._row_entry(tab, 9, "Context Char Limit", "llm_context_char_limit")
        self._row_entry(tab, 10, "Source Excerpt Radius", "source_excerpt_radius")
        self._row_entry(tab, 11, "Max Decisions/Prompt", "max_decisions_per_prompt")
        self._row_entry(tab, 12, "Function Test Dir", "function_test_dir")
        self._row_entry(tab, 13, "Runner Output", "runner_output")
        self._row_entry(tab, 14, "Max Functions/Batch", "max_functions_per_prompt")
        self._row_entry(tab, 15, "Max Prompt Tokens", "max_prompt_tokens")
        self._row_entry(tab, 16, "Max Response Tokens", "max_response_tokens")
        hint = (
            "Source globs / Include dirs: comma-separated.\n"
            "API Key Value writes to process env, not saved to config.\n"
            "If model reports token shortage, reduce max_prompt_tokens or context char limit.\n"
        )
        ttk.Label(tab, text=hint, wraplength=420, foreground="#555555").grid(
            row=15, column=0, columnspan=3, sticky=tk.W, pady=(12, 0)
        )

    def _row_entry(
        self,
        tab: ttk.Frame,
        row: int,
        label: str,
        var_name: str,
        browse: Optional[Callable[[], None]] = None,
        show: str = "",
    ) -> None:
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky=tk.W, pady=2)
        entry = ttk.Entry(tab, textvariable=self.vars[var_name])
        if show:
            entry.configure(show=show)
        entry.grid(row=row, column=1, sticky=tk.EW, pady=2, padx=(8, 4))
        if browse:
            ttk.Button(tab, text="Browse", command=browse).grid(row=row, column=2, padx=2)

    def _build_commands_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Commands")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)
        tab.rowconfigure(5, weight=1)
        ttk.Label(tab, text="Build commands (one per line)").grid(row=0, column=0, sticky=tk.W)
        self.build_text = self._text(tab, height=4)
        self.build_text.grid(row=1, column=0, sticky=tk.NSEW, pady=(4, 10))
        ttk.Label(tab, text="Test commands (one per line)").grid(row=2, column=0, sticky=tk.W)
        self.test_text = self._text(tab, height=4)
        self.test_text.grid(row=3, column=0, sticky=tk.NSEW, pady=(4, 10))
        ttk.Label(tab, text="Coverage commands (one per line)").grid(row=4, column=0, sticky=tk.W)
        self.coverage_text = self._text(tab, height=4)
        self.coverage_text.grid(row=5, column=0, sticky=tk.NSEW, pady=(4, 0))

    def _build_prompt_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Extra Prompt")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="Extra constraints, e.g. public API, stub strategy.").grid(
            row=0, column=0, sticky=tk.W
        )
        self.extra_prompt_text = self._text(tab)
        self.extra_prompt_text.grid(row=1, column=0, sticky=tk.NSEW, pady=(6, 0))

    def _build_decision_tab(self) -> None:
        tab = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(tab, text="MC/DC Decisions")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        columns = ("file", "line", "expression", "conditions")
        self.decision_tree = ttk.Treeview(tab, columns=columns, show="headings")
        self.decision_tree.heading("file", text="File")
        self.decision_tree.heading("line", text="Line")
        self.decision_tree.heading("expression", text="Expression")
        self.decision_tree.heading("conditions", text="Conditions")
        self.decision_tree.column("file", width=180, anchor=tk.W)
        self.decision_tree.column("line", width=60, anchor=tk.CENTER)
        self.decision_tree.column("expression", width=360, anchor=tk.W)
        self.decision_tree.column("conditions", width=70, anchor=tk.CENTER)
        ybar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.decision_tree.yview)
        self.decision_tree.configure(yscrollcommand=ybar.set)
        self.decision_tree.grid(row=0, column=0, sticky=tk.NSEW)
        ybar.grid(row=0, column=1, sticky=tk.NS)

    def _build_function_tab(self) -> None:
        tab = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(tab, text="Functions")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        columns = ("file", "name", "lines", "static", "decisions",
                   "calls", "structs", "macros", "headers")
        self.function_tree = ttk.Treeview(tab, columns=columns, show="headings")
        self.function_tree.heading("file", text="File")
        self.function_tree.heading("name", text="Function")
        self.function_tree.heading("lines", text="Lines")
        self.function_tree.heading("static", text="Static")
        self.function_tree.heading("decisions", text="Decisions")
        self.function_tree.heading("calls", text="Calls")
        self.function_tree.heading("structs", text="Structs")
        self.function_tree.heading("macros", text="Macros")
        self.function_tree.heading("headers", text="Headers")
        self.function_tree.column("file", width=140, anchor=tk.W)
        self.function_tree.column("name", width=130, anchor=tk.W)
        self.function_tree.column("lines", width=65, anchor=tk.CENTER)
        self.function_tree.column("static", width=55, anchor=tk.CENTER)
        self.function_tree.column("decisions", width=70, anchor=tk.CENTER)
        self.function_tree.column("calls", width=150, anchor=tk.W)
        self.function_tree.column("structs", width=120, anchor=tk.W)
        self.function_tree.column("macros", width=120, anchor=tk.W)
        self.function_tree.column("headers", width=140, anchor=tk.W)
        ybar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.function_tree.yview)
        self.function_tree.configure(yscrollcommand=ybar.set)
        self.function_tree.grid(row=0, column=0, sticky=tk.NSEW)
        ybar.grid(row=0, column=1, sticky=tk.NS)

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(tab, text="Logs")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.log_text = self._text(tab, wrap=tk.NONE)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)

    def _build_report_tab(self) -> None:
        tab = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(tab, text="Report")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky=tk.EW, pady=(0, 6))
        ttk.Button(toolbar, text="Open Report",
                   command=self.open_report_file).pack(side=tk.LEFT)
        self.report_path_var = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.report_path_var).pack(side=tk.LEFT, padx=(10, 0))
        self.report_text = self._text(tab, wrap=tk.NONE)
        self.report_text.grid(row=1, column=0, sticky=tk.NSEW)

    def _text(self, parent: ttk.Frame, height: int = 10,
              wrap: str = tk.WORD) -> tk.Text:
        text = tk.Text(parent, height=height, wrap=wrap, undo=True,
                       font=("Consolas", 10))
        text.configure(borderwidth=1, relief=tk.SOLID)
        return text



    def browse_project_root(self) -> None:
        selected = filedialog.askdirectory(title="Select C project directory")
        if selected:
            self.vars["project_root"].set(selected.replace("\\", "/"))

    def browse_test_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Test output file",
            defaultextension=".c",
            filetypes=[("C source", "*.c")],
        )
        if selected:
            try:
                proot = self.vars["project_root"].get() or "."
                self.vars["test_output"].set(
                    str(Path(selected).relative_to(Path(proot)))
                )
            except ValueError:
                self.vars["test_output"].set(selected)

    def new_example_config(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Save example config",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if selected:
            path = Path(selected)
            core.write_example_config(path)
            self.open_config(path)

    def open_config(self, path: Optional[Path] = None) -> None:
        if not path:
            selected = filedialog.askopenfilename(
                title="Open config",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not selected:
                return
            path = Path(selected)
        self.config_path = path.resolve()
        self.config_entry.delete(0, tk.END)
        self.config_entry.insert(0, str(self.config_path))
        self.title(f"{APP_TITLE}  [{path.name}]")
        self._load_raw_config(json.loads(path.read_text(encoding="utf-8-sig")))

    def _load_raw_config(self, raw: Dict[str, Any]) -> None:
        proot = raw.get("project_root", ".")
        self.vars["project_root"].set(proot)
        self.vars["source_globs"].set(
            ", ".join(raw.get("source_globs", ["**/*.c", "**/*.h"])))
        self.vars["include_dirs"].set(
            ", ".join(raw.get("include_dirs", [])))
        self.vars["test_output"].set(
            raw.get("test_output", "tests/auto_mcdc_tests.c"))
        self.vars["llm_base_url"].set(
            raw.get("llm_base_url", "https://api.openai.com/v1"))
        self.vars["llm_api_key_env"].set(
            raw.get("llm_api_key_env", "OPENAI_API_KEY"))
        self.vars["llm_api_key_value"].set("")
        self.vars["llm_model"].set(raw.get("llm_model", core.DEFAULT_MODEL))
        self.vars["max_llm_rounds"].set(str(raw.get("max_llm_rounds", 1)))
        self.vars["llm_context_char_limit"].set(
            str(raw.get("llm_context_char_limit", 36000)))
        self.vars["source_excerpt_radius"].set(
            str(raw.get("source_excerpt_radius", 80)))
        self.vars["max_decisions_per_prompt"].set(
            str(raw.get("max_decisions_per_prompt", 60)))
        self.vars["function_test_dir"].set(
            raw.get("function_test_dir", "tests/auto_function_tests"))
        self.vars["runner_output"].set(
            raw.get("runner_output", "tests/auto_cunit_runner.c"))
        self.vars["max_functions_per_prompt"].set(
            str(raw.get("max_functions_per_prompt", 8)))
        self.vars["max_prompt_tokens"].set(
            str(raw.get("max_prompt_tokens", 12000)))
        self.vars["max_response_tokens"].set(
            str(raw.get("max_response_tokens", 4096)))
        self._set_text(self.build_text,
                       list_to_lines(raw.get("build_commands", [])))
        self._set_text(self.test_text,
                       list_to_lines(raw.get("test_commands", [])))
        self._set_text(self.coverage_text,
                       list_to_lines(raw.get("coverage_commands", [])))
        self._set_text(self.extra_prompt_text, raw.get("extra_prompt", ""))
        self.log("Config loaded")

    def save_config(self) -> None:
        if not self.config_path:
            self.save_config_as()
            return
        raw = self._raw_config_from_ui()
        self.config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"Saved: {self.config_path}")

    def save_config_as(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Save config as",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if selected:
            self.config_path = Path(selected).resolve()
            self.config_entry.delete(0, tk.END)
            self.config_entry.insert(0, str(self.config_path))
            self.save_config()

    def _raw_config_from_ui(self) -> Dict[str, Any]:
        def _read(widget):
            return [ln.strip()
                    for ln in widget.get("1.0", tk.END).splitlines()
                    if ln.strip()]
        return {
            "project_root": self.vars["project_root"].get(),
            "source_globs": (
                lines_to_list(self.vars["source_globs"].get())
                or ["**/*.c", "**/*.h"]),
            "include_dirs": lines_to_list(self.vars["include_dirs"].get()),
            "test_output": self.vars["test_output"].get(),
            "build_commands": _read(self.build_text),
            "test_commands": _read(self.test_text),
            "coverage_commands": _read(self.coverage_text),
            "llm_base_url": self.vars["llm_base_url"].get(),
            "llm_api_key_env": self.vars["llm_api_key_env"].get(),
            "llm_model": self.vars["llm_model"].get(),
            "max_llm_rounds": int(self.vars["max_llm_rounds"].get() or 1),
            "llm_context_char_limit": int(
                self.vars["llm_context_char_limit"].get() or 36000),
            "source_excerpt_radius": int(
                self.vars["source_excerpt_radius"].get() or 80),
            "max_decisions_per_prompt": int(
                self.vars["max_decisions_per_prompt"].get() or 60),
            "function_test_dir": self.vars["function_test_dir"].get(),
            "runner_output": self.vars["runner_output"].get(),
            "max_functions_per_prompt": int(
                self.vars["max_functions_per_prompt"].get() or 8),
            "max_prompt_tokens": int(
                self.vars["max_prompt_tokens"].get() or 12000),
            "max_response_tokens": int(
                self.vars["max_response_tokens"].get() or 4096),
            "extra_prompt": self.extra_prompt_text.get(
                "1.0", tk.END).strip(),
        }

    def _config_from_ui(self) -> core.Config:
        raw = self._raw_config_from_ui()
        api_key = self.vars["llm_api_key_value"].get().strip()
        if api_key:
            os.environ[raw["llm_api_key_env"]] = api_key
        return core.load_config_from_raw(raw)



    def scan_decisions(self) -> None:
        self._start_worker("Scanning decisions...", lambda: self._scan_worker())

    def scan_functions(self) -> None:
        self._start_worker("Scanning functions...", lambda: self._scan_functions_worker())

    def preview_prompt(self) -> None:
        self._start_worker("Building prompt...", lambda: self._prompt_worker())

    def generate_tests(self) -> None:
        self._start_worker("Generating tests...", lambda: self._generate_worker())

    def generate_function_tests(self) -> None:
        self._start_worker("Generating function tests...",
                           lambda: self._generate_functions_worker())

    def run_pipeline(self, generate: bool) -> None:
        label = "Generating + Running..." if generate else "Running..."
        self._start_worker(label, lambda: self._run_worker(generate))

    def run_function_pipeline(self) -> None:
        self._start_worker("Function pipeline...",
                           lambda: self._run_functions_worker())

    def _start_worker(self, label: str, target: Callable[[], None]) -> None:
        self.status_var.set(label)
        self.worker = threading.Thread(
            target=lambda: self._worker_guard(target, label), daemon=True)
        self.worker.start()

    def _worker_guard(self, target: Callable[[], None], label: str) -> None:
        try:
            target()
        except Exception:
            self.events.put(("error", f"{label}\n{traceback.format_exc()}"))
        else:
            self.events.put(("done", label))

    def _scan_worker(self) -> None:
        config = self._config_from_ui()
        decisions = core.discover_decisions(config)
        self.events.put(("decisions", decisions))

    def _scan_functions_worker(self) -> None:
        config = self._config_from_ui()
        functions = core.discover_functions(config)
        self.events.put(("functions", functions))

    def _prompt_worker(self) -> None:
        config = self._config_from_ui()
        decisions = core.discover_decisions(config)
        prompt = core.build_prompt(config, decisions)
        self.events.put(("prompt", prompt))

    def _generate_worker(self) -> None:
        config = self._config_from_ui()
        decisions = core.discover_decisions(config)
        if not decisions:
            raise RuntimeError("no decisions found in configured source files")
        prompt = core.build_prompt(config, decisions)
        llm_payload = core.call_llm(config, prompt)
        core.ensure_parent(config.test_output)
        config.test_output.write_text(
            llm_payload["test_file"].rstrip() + "\n", encoding="utf-8")
        self.events.put(("log", f"Generated: {config.test_output}"))

    def _generate_functions_worker(self) -> None:
        config = self._config_from_ui()
        functions = core.discover_functions(config)
        if not functions:
            raise RuntimeError("no C functions found in .c source files")
        manifest = core.generate_function_tests(config, functions)
        n = len(manifest["generated_files"])
        self.events.put(("log", f"Generated {n} test file(s)"))
        self.events.put(("log", f"CUnit runner: {manifest['runner_output']}"))
        self.events.put(("log",
                         f"Manifest: {config.function_test_dir}/auto_function_tests_manifest.json"))

    def _run_worker(self, generate: bool) -> None:
        config = self._config_from_ui()
        decisions = core.discover_decisions(config)
        llm_payload = None
        functions = None
        function_manifest = None
        if generate:
            prompt = core.build_prompt(config, decisions)
            llm_payload = core.call_llm(config, prompt)
            core.ensure_parent(config.test_output)
            config.test_output.write_text(
                llm_payload["test_file"].rstrip() + "\n", encoding="utf-8")
            self.events.put(("log", f"Generated: {config.test_output}"))
            functions = core.discover_functions(config)
            function_manifest = core.generate_function_tests(config, functions)
            n = len(function_manifest["generated_files"])
            self.events.put(("log", f"Function tests: {n} file(s)"))

        build_results = core.run_commands(config.build_commands, config.project_root)
        self.events.put(("log", f"Build: {core.command_group_status(build_results)}"))
        if any(item["returncode"] != 0 for item in build_results):
            test_results: List[Dict[str, Any]] = []
            coverage_results: List[Dict[str, Any]] = []
            self.events.put(("log", "Build failed, skipping tests"))
        else:
            test_results = core.run_commands(config.test_commands, config.project_root)
            self.events.put(("log", f"Test: {core.command_group_status(test_results)}"))
            coverage_results = core.run_commands(
                config.coverage_commands, config.project_root)
            self.events.put(("log", f"Coverage: {core.command_group_status(coverage_results)}"))
        coverage_summary = core.collect_coverage_summary(config, coverage_results)
        report = core.write_report(
            config, decisions, llm_payload,
            build_results, test_results, coverage_results, coverage_summary,
            functions=functions, function_manifest=function_manifest,
        )
        self.events.put(("report", str(report)))
        self.events.put(("log", f"Report: {report}"))

    def _run_functions_worker(self) -> None:
        config = self._config_from_ui()
        functions = core.discover_functions(config)
        decisions = core.discover_decisions(config)
        if not functions:
            raise RuntimeError("no C functions found in .c source files")
        function_manifest = core.generate_function_tests(config, functions)
        n = len(function_manifest["generated_files"])
        self.events.put(("log", f"Generated {n} test file(s)"))
        build_results = core.run_commands(config.build_commands, config.project_root)
        self.events.put(("log", f"Build: {core.command_group_status(build_results)}"))
        if any(item["returncode"] != 0 for item in build_results):
            test_results: List[Dict[str, Any]] = []
            coverage_results: List[Dict[str, Any]] = []
            self.events.put(("log", "Build failed, skipping tests"))
        else:
            test_results = core.run_commands(config.test_commands, config.project_root)
            self.events.put(("log", f"Test: {core.command_group_status(test_results)}"))
            coverage_results = core.run_commands(
                config.coverage_commands, config.project_root)
            self.events.put(("log", f"Coverage: {core.command_group_status(coverage_results)}"))
        coverage_summary = core.collect_coverage_summary(config, coverage_results)
        report = core.write_report(
            config, decisions, None,
            build_results, test_results, coverage_results, coverage_summary,
            functions=functions, function_manifest=function_manifest,
        )
        self.events.put(("report", str(report)))
        self.events.put(("log", f"Report: {report}"))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "error":
                    self.log(payload)
                    messagebox.showerror("Error", payload[:800])
                elif kind == "decisions":
                    self.show_decisions(payload)
                elif kind == "functions":
                    self.show_functions(payload)
                elif kind == "prompt":
                    self.result_notebook.select(self.log_text.master)
                    self._set_text(self.log_text, payload)
                elif kind == "report":
                    self.load_report(Path(payload))
                elif kind == "done":
                    self.status_var.set(f"Done: {payload}")
        except queue.Empty:
            pass
        self.after(200, self._drain_events)

    def show_decisions(self, decisions: List[core.Decision]) -> None:
        for item in self.decision_tree.get_children():
            self.decision_tree.delete(item)
        for decision in decisions:
            self.decision_tree.insert(
                "", tk.END,
                values=(decision.file, decision.line,
                        decision.expression, len(decision.conditions)),
            )
        self.result_notebook.select(self.decision_tree.master)

    def show_functions(self, functions: List[core.CFunction]) -> None:
        for item in self.function_tree.get_children():
            self.function_tree.delete(item)
        for function in functions:
            self.function_tree.insert(
                "", tk.END,
                values=(
                    function.file,
                    function.name,
                    f"{function.start_line}-{function.end_line}",
                    "yes" if function.is_static else "no",
                    len(function.decisions),
                    ", ".join(function.called_functions[:10]),
                    ", ".join(function.used_structs[:8]),
                    ", ".join(function.used_macros[:8]),
                    ", ".join(function.required_headers[:8]),
                ),
            )
        self.result_notebook.select(self.function_tree.master)

    def open_report_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Open MC/DC Report",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if selected:
            self.load_report(Path(selected))

    def load_report(self, path: Path) -> None:
        if not path.exists():
            self.log(f"Report not found: {path}")
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        html_path = raw.get("html_report", str(path.with_suffix(".html")))
        self.report_path_var.set(str(path))
        self._set_text(self.report_text, format_report_summary(raw))
        self.log(f"Loaded: {path}  HTML: {html_path}")

    def log(self, text: str) -> None:
        self.log_text.insert(tk.END, f"{text}\n")
        self.log_text.see(tk.END)

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)




def format_report_summary(report: Dict[str, Any]) -> str:
    coverage = report.get("coverage_summary", {})

    def pct(key: str) -> str:
        value = coverage.get(key)
        if isinstance(value, (int, float)):
            return f"{value:.1f}%"
        return "N/A"

    def group_status(results_key: str) -> str:
        results = report.get(results_key, [])
        if not results:
            return "SKIPPED"
        if any(item.get("returncode", 1) != 0 for item in results):
            return "FAIL"
        return "PASS"

    lines = [
        f"Project: {report.get('project_root', '')}",
        f"Decisions: {report.get('decision_count', 0)}",
        f"Functions: {report.get('function_count', 0)}",
        "",
        "--- Build ---",
        f"  Status: {group_status('build_results')}",
    ]
    for item in report.get("build_results", []):
        lines.append(f"  $ {item.get('command', '')} [exit={item.get('returncode', '?')}]")
    lines.append("")
    lines.append("--- Test ---")
    lines.append(f"  Status: {group_status('test_results')}")
    for item in report.get("test_results", []):
        lines.append(f"  $ {item.get('command', '')} [exit={item.get('returncode', '?')}]")
    lines.append("")
    lines.append("--- Coverage ---")
    lines.append(f"  Lines: {pct('lines_executed_percent')}")
    lines.append(f"  Branches: {pct('branches_executed_percent')}")
    lines.append(f"  Taken at least once: {pct('branches_taken_at_least_once_percent')}")
    for item in report.get("coverage_results", []):
        lines.append(f"  $ {item.get('command', '')} [exit={item.get('returncode', '?')}]")
    lines.append("")
    lines.append("--- LLM ---")
    lines.append(f"  Notes: {report.get('llm_notes', [])}")
    lines.append(f"  Assumptions: {report.get('llm_assumptions', [])}")
    if report.get("function_test_manifest"):
        m = report["function_test_manifest"]
        lines.append("")
        lines.append("--- Function Tests ---")
        lines.append(f"  Functions: {m.get('function_count', 0)}")
        lines.append(f"  Test files: {len(m.get('generated_files', []))}")
        lines.append(f"  Runner: {m.get('runner_output', '')}")
    return "\n".join(lines)


if not hasattr(core, "load_config_from_raw"):
    def _load_config_from_raw(raw: Dict[str, Any]) -> core.Config:
        root = Path(raw.get("project_root", ".")).expanduser().resolve()

        def _csv(name, fallback):
            val = raw.get(name, fallback)
            if isinstance(val, str):
                return [s.strip() for s in val.split(",") if s.strip()]
            return val

        return core.Config(
            project_root=root,
            source_globs=_csv("source_globs", "**/*.c, **/*.h") or ["**/*.c", "**/*.h"],
            include_dirs=_csv("include_dirs", ""),
            test_output=(root / raw.get(
                "test_output", "tests/auto_mcdc_tests.c")).resolve(),
            build_commands=raw.get("build_commands", []),
            test_commands=raw.get("test_commands", []),
            coverage_commands=raw.get("coverage_commands", []),
            llm_base_url=raw.get("llm_base_url", "https://api.openai.com/v1"),
            llm_api_key_env=raw.get("llm_api_key_env", "OPENAI_API_KEY"),
            llm_model=raw.get("llm_model", core.DEFAULT_MODEL),
            max_llm_rounds=int(raw.get("max_llm_rounds", 1)),
            llm_context_char_limit=int(raw.get("llm_context_char_limit", 36000)),
            source_excerpt_radius=int(raw.get("source_excerpt_radius", 80)),
            max_decisions_per_prompt=int(raw.get("max_decisions_per_prompt", 60)),
            function_test_dir=(
                root / raw.get(
                    "function_test_dir",
                    "tests/auto_function_tests")).resolve(),
            runner_output=(
                root / raw.get(
                    "runner_output",
                    "tests/auto_cunit_runner.c")).resolve(),
            max_functions_per_prompt=int(
                raw.get("max_functions_per_prompt", 8)),
            max_prompt_tokens=int(
                raw.get("max_prompt_tokens", 12000)),
            max_response_tokens=int(
                raw.get("max_response_tokens", 4096)),
            extra_prompt=raw.get("extra_prompt", ""),
        )

    core.load_config_from_raw = _load_config_from_raw


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
