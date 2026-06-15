#!/usr/bin/env python3
"""
Tkinter GUI for cunit_mcdc_tool.py.

The GUI reuses the CLI module's core functions, so both entry points share the
same scanner, LLM call, command runner, and report writer.
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
from typing import Any, Callable

import cunit_mcdc_tool as core


APP_TITLE = "CUnit MC/DC AI Test Generator"


def list_to_lines(items: list[str]) -> str:
    return "\n".join(items)


def lines_to_list(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(980, 640)

        self.config_path: Path | None = None
        self.worker: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()

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
            "per_function": tk.BooleanVar(value=True),
            "auto_build": tk.BooleanVar(value=False),
            "compiler": tk.StringVar(value="gcc"),
            "cunit_include_dir": tk.StringVar(),
            "cunit_lib_dir": tk.StringVar(),
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

        ttk.Label(top, text="配置文件").pack(side=tk.LEFT)
        self.config_entry = ttk.Entry(top)
        self.config_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        ttk.Button(top, text="打开", command=self.open_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="保存", command=self.save_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="另存为", command=self.save_config_as).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="新建示例", command=self.new_example_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="加载 APIKEY.txt", command=self.load_apikey_file).pack(side=tk.LEFT, padx=2)

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
        self._build_log_tab()
        self._build_report_tab()

        actions = ttk.Frame(self, padding=(12, 0, 12, 10))
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="扫描判定", command=self.scan_decisions).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="预览提示词", command=self.preview_prompt).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="生成 CUnit", command=self.generate_tests).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="生成并运行", command=lambda: self.run_pipeline(generate=True)).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="仅运行", command=lambda: self.run_pipeline(generate=False)).pack(side=tk.LEFT, padx=6)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(actions, textvariable=self.status, style="Status.TLabel").pack(side=tk.RIGHT)

    def _build_config_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="基础配置")
        tab.columnconfigure(1, weight=1)

        self._row_entry(tab, 0, "C 工程目录", "project_root", browse=self.browse_project_root)
        self._row_entry(tab, 1, "源码匹配", "source_globs")
        self._row_entry(tab, 2, "Include 目录", "include_dirs")
        self._row_entry(tab, 3, "测试输出文件", "test_output", browse=self.browse_test_output)
        self._row_entry(tab, 4, "LLM Base URL", "llm_base_url")
        self._row_entry(tab, 5, "API Key 环境变量", "llm_api_key_env")
        self._row_entry(tab, 6, "临时 API Key", "llm_api_key_value", show="*")
        self._row_entry(tab, 7, "模型", "llm_model")
        self._row_entry(tab, 8, "最大轮数", "max_llm_rounds")

        hint = (
            "源码匹配和 Include 目录用逗号分隔。临时 API Key 只写入当前进程环境，不保存到配置文件。"
        )
        ttk.Label(tab, text=hint, wraplength=520, foreground="#5d6870").grid(
            row=9, column=0, columnspan=3, sticky=tk.W, pady=(14, 0)
        )

    def _row_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        key: str,
        browse: Callable[[], None] | None = None,
        show: str | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=5)
        entry = ttk.Entry(parent, textvariable=self.vars[key], show=show)
        entry.grid(row=row, column=1, sticky=tk.EW, padx=(10, 8), pady=5)
        if browse:
            ttk.Button(parent, text="选择", command=browse).grid(row=row, column=2, sticky=tk.E, pady=5)
        else:
            ttk.Label(parent, text="").grid(row=row, column=2, sticky=tk.E, pady=5)

    def _build_commands_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="命令")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)
        tab.rowconfigure(5, weight=1)

        ttk.Label(tab, text="构建命令，每行一条").grid(row=0, column=0, sticky=tk.W)
        self.build_text = self._text(tab, height=5)
        self.build_text.grid(row=1, column=0, sticky=tk.NSEW, pady=(4, 10))

        ttk.Label(tab, text="测试命令，每行一条").grid(row=2, column=0, sticky=tk.W)
        self.test_text = self._text(tab, height=4)
        self.test_text.grid(row=3, column=0, sticky=tk.NSEW, pady=(4, 10))

        ttk.Label(tab, text="覆盖率命令，每行一条").grid(row=4, column=0, sticky=tk.W)
        self.coverage_text = self._text(tab, height=4)
        self.coverage_text.grid(row=5, column=0, sticky=tk.NSEW, pady=(4, 0))

        # Auto-build panel: no hand-written commands needed.
        auto_frame = ttk.LabelFrame(tab, text="自动编译运行（无需手写命令）", padding=8)
        auto_frame.grid(row=6, column=0, sticky=tk.EW, pady=(10, 0))
        auto_frame.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            auto_frame, text="启用自动编译运行（自动编译测试文件并执行，跳过手写构建/测试/覆盖率命令）",
            variable=self.vars["auto_build"],
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W)
        self._row_entry(auto_frame, 1, "编译器", "compiler")
        self._row_entry(auto_frame, 2, "CUnit Include 目录", "cunit_include_dir")
        self._row_entry(auto_frame, 3, "CUnit Lib 目录", "cunit_lib_dir")
        ttk.Label(
            auto_frame,
            text="Include/Lib 目录留空将自动探测 MSYS2/MinGW；自动编译会跳过定义了 main() 的源文件。",
            foreground="#5d6870",
        ).grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))

    def _build_prompt_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="提示词补充")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="额外约束，例如指定 public API、stub 策略或编码规范").grid(
            row=0, column=0, sticky=tk.W
        )
        self.extra_prompt_text = self._text(tab)
        self.extra_prompt_text.grid(row=1, column=0, sticky=tk.NSEW, pady=(6, 0))

    def _build_decision_tab(self) -> None:
        tab = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(tab, text="MC/DC 判定")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        columns = ("file", "line", "expression", "conditions")
        self.decision_tree = ttk.Treeview(tab, columns=columns, show="headings")
        self.decision_tree.heading("file", text="文件")
        self.decision_tree.heading("line", text="行")
        self.decision_tree.heading("expression", text="判定表达式")
        self.decision_tree.heading("conditions", text="条件数")
        self.decision_tree.column("file", width=180, anchor=tk.W)
        self.decision_tree.column("line", width=60, anchor=tk.CENTER)
        self.decision_tree.column("expression", width=360, anchor=tk.W)
        self.decision_tree.column("conditions", width=70, anchor=tk.CENTER)
        ybar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.decision_tree.yview)
        self.decision_tree.configure(yscrollcommand=ybar.set)
        self.decision_tree.grid(row=0, column=0, sticky=tk.NSEW)
        ybar.grid(row=0, column=1, sticky=tk.NS)

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(tab, text="日志")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.log_text = self._text(tab, wrap=tk.NONE)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)

    def _build_report_tab(self) -> None:
        tab = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(tab, text="报告")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky=tk.EW, pady=(0, 6))
        ttk.Button(toolbar, text="打开报告", command=self.open_report_file).pack(side=tk.LEFT)
        self.report_path_var = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.report_path_var).pack(side=tk.LEFT, padx=(10, 0))
        self.report_text = self._text(tab, wrap=tk.NONE)
        self.report_text.grid(row=1, column=0, sticky=tk.NSEW)

    def _text(self, parent: ttk.Frame, height: int = 10, wrap: str = tk.WORD) -> tk.Text:
        text = tk.Text(parent, height=height, wrap=wrap, undo=True, font=("Consolas", 10))
        text.configure(borderwidth=1, relief=tk.SOLID)
        return text

    def browse_project_root(self) -> None:
        selected = filedialog.askdirectory(title="选择 C 工程目录")
        if selected:
            self.vars["project_root"].set(selected)

    def browse_test_output(self) -> None:
        initialdir = self.vars["project_root"].get() or os.getcwd()
        selected = filedialog.asksaveasfilename(
            title="选择 CUnit 测试输出文件",
            initialdir=initialdir,
            defaultextension=".c",
            filetypes=[("C source", "*.c"), ("All files", "*.*")],
        )
        if selected:
            project_root = Path(self.vars["project_root"].get() or ".").expanduser().resolve()
            path = Path(selected).expanduser().resolve()
            try:
                self.vars["test_output"].set(str(path.relative_to(project_root)))
            except ValueError:
                self.vars["test_output"].set(str(path))

    def new_example_config(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="保存示例配置",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not selected:
            return
        path = Path(selected).resolve()
        core.ensure_parent(path)
        core.write_example_config(path)
        self.config_path = path
        self.config_entry.delete(0, tk.END)
        self.config_entry.insert(0, str(path))
        self.open_config(path)

    def load_apikey_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 APIKEY.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
        )
        if not selected:
            return
        path = Path(selected).resolve()
        data = core.load_apikey_file(path)
        if not data:
            messagebox.showerror("加载失败", f"未在 {path} 中解析到 URL / MODEL / API-KEY。")
            return
        if data.get("url"):
            self.vars["llm_base_url"].set(data["url"])
        if data.get("model"):
            self.vars["llm_model"].set(data["model"])
        if data.get("key"):
            self.vars["llm_api_key_value"].set(data["key"])
        self.log(
            f"已加载 APIKEY.txt: {path}\n"
            f"  url   = {data.get('url', '(未设置)')}\n"
            f"  model = {data.get('model', '(未设置)')}\n"
            f"  key   = {'(已填入临时 Key)' if data.get('key') else '(未设置)'}\n"
        )

    def open_config(self, path: Path | None = None) -> None:
        if path is None:
            selected = filedialog.askopenfilename(
                title="打开配置文件",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not selected:
                return
            path = Path(selected).resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.config_path = path
            self.config_entry.delete(0, tk.END)
            self.config_entry.insert(0, str(path))
            self._load_raw_config(raw)
            self.log(f"已加载配置: {path}\n")
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _load_raw_config(self, raw: dict[str, Any]) -> None:
        self.vars["project_root"].set(raw.get("project_root", ""))
        self.vars["source_globs"].set(", ".join(raw.get("source_globs", ["**/*.c", "**/*.h"])))
        self.vars["include_dirs"].set(", ".join(raw.get("include_dirs", [])))
        self.vars["test_output"].set(raw.get("test_output", "tests/auto_mcdc_tests.c"))
        self.vars["llm_base_url"].set(raw.get("llm_base_url", "https://api.openai.com/v1"))
        self.vars["llm_api_key_env"].set(raw.get("llm_api_key_env", "OPENAI_API_KEY"))
        self.vars["llm_model"].set(raw.get("llm_model", core.DEFAULT_MODEL))
        self.vars["max_llm_rounds"].set(str(raw.get("max_llm_rounds", 1)))
        self.vars["auto_build"].set(bool(raw.get("auto_build", False)))
        self.vars["compiler"].set(raw.get("compiler", "gcc"))
        self.vars["cunit_include_dir"].set(raw.get("cunit_include_dir", ""))
        self.vars["cunit_lib_dir"].set(raw.get("cunit_lib_dir", ""))
        self._set_text(self.build_text, list_to_lines(raw.get("build_commands", [])))
        self._set_text(self.test_text, list_to_lines(raw.get("test_commands", [])))
        self._set_text(self.coverage_text, list_to_lines(raw.get("coverage_commands", [])))
        self._set_text(self.extra_prompt_text, raw.get("extra_prompt", ""))

    def save_config(self) -> None:
        path_text = self.config_entry.get().strip()
        if path_text:
            self.config_path = Path(path_text).expanduser().resolve()
        if self.config_path is None:
            self.save_config_as()
            return
        try:
            raw = self._raw_config_from_ui()
            core.ensure_parent(self.config_path)
            self.config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log(f"已保存配置: {self.config_path}\n")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def save_config_as(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="另存配置文件",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not selected:
            return
        self.config_path = Path(selected).resolve()
        self.config_entry.delete(0, tk.END)
        self.config_entry.insert(0, str(self.config_path))
        self.save_config()

    def _raw_config_from_ui(self) -> dict[str, Any]:
        return {
            "project_root": self.vars["project_root"].get().strip(),
            "source_globs": [x.strip() for x in self.vars["source_globs"].get().split(",") if x.strip()],
            "include_dirs": [x.strip() for x in self.vars["include_dirs"].get().split(",") if x.strip()],
            "test_output": self.vars["test_output"].get().strip(),
            "build_commands": lines_to_list(self.build_text.get("1.0", tk.END)),
            "test_commands": lines_to_list(self.test_text.get("1.0", tk.END)),
            "coverage_commands": lines_to_list(self.coverage_text.get("1.0", tk.END)),
            "llm_base_url": self.vars["llm_base_url"].get().strip(),
            "llm_api_key_env": self.vars["llm_api_key_env"].get().strip() or "OPENAI_API_KEY",
            "llm_model": self.vars["llm_model"].get().strip() or core.DEFAULT_MODEL,
            "max_llm_rounds": int(self.vars["max_llm_rounds"].get().strip() or "1"),
            "extra_prompt": self.extra_prompt_text.get("1.0", tk.END).strip(),
            "auto_build": self.vars["auto_build"].get(),
            "compiler": self.vars["compiler"].get().strip() or "gcc",
            "cunit_include_dir": self.vars["cunit_include_dir"].get().strip(),
            "cunit_lib_dir": self.vars["cunit_lib_dir"].get().strip(),
        }

    def _config_from_ui(self) -> core.Config:
        raw = self._raw_config_from_ui()
        api_key = self.vars["llm_api_key_value"].get().strip()
        if api_key:
            os.environ[raw["llm_api_key_env"]] = api_key
        project_root = Path(raw.get("project_root", ".")).expanduser().resolve()
        return core.Config(
            project_root=project_root,
            source_globs=raw.get("source_globs", ["**/*.c", "**/*.h"]),
            include_dirs=raw.get("include_dirs", []),
            test_output=(project_root / raw.get("test_output", "tests/auto_mcdc_tests.c")).resolve(),
            build_commands=raw.get("build_commands", []),
            test_commands=raw.get("test_commands", []),
            coverage_commands=raw.get("coverage_commands", []),
            llm_base_url=raw.get("llm_base_url", "https://api.openai.com/v1"),
            llm_api_key_env=raw.get("llm_api_key_env", "OPENAI_API_KEY"),
            llm_model=raw.get("llm_model", core.DEFAULT_MODEL),
            max_llm_rounds=int(raw.get("max_llm_rounds", 1)),
            extra_prompt=raw.get("extra_prompt", ""),
            auto_build=bool(raw.get("auto_build", False)),
            compiler=raw.get("compiler", "gcc"),
            cunit_include_dir=raw.get("cunit_include_dir", ""),
            cunit_lib_dir=raw.get("cunit_lib_dir", ""),
        )

    def scan_decisions(self) -> None:
        self._start_worker("扫描判定", self._scan_worker)

    def preview_prompt(self) -> None:
        self._start_worker("预览提示词", self._prompt_worker)

    def generate_tests(self) -> None:
        self._start_worker("生成 CUnit", self._generate_worker)

    def run_pipeline(self, generate: bool) -> None:
        title = "生成并运行" if generate else "仅运行"
        self._start_worker(title, lambda: self._run_worker(generate))

    def _start_worker(self, label: str, target: Callable[[], None]) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("任务进行中", "当前任务还在运行，请稍后。")
            return
        self.status.set(f"{label}中...")
        self.log(f"\n== {label} ==\n")
        self.worker = threading.Thread(target=self._worker_guard, args=(target, label), daemon=True)
        self.worker.start()

    def _worker_guard(self, target: Callable[[], None], label: str) -> None:
        try:
            target()
            self.events.put(("status", f"{label}完成"))
        except Exception:
            self.events.put(("error", traceback.format_exc()))
            self.events.put(("status", f"{label}失败"))

    def _scan_worker(self) -> None:
        config = self._config_from_ui()
        decisions = core.discover_decisions(config)
        self.events.put(("decisions", decisions))
        self.events.put(("log", f"发现 {len(decisions)} 个判定。\n"))

    def _prompt_worker(self) -> None:
        config = self._config_from_ui()
        decisions = core.discover_decisions(config)
        prompt = core.build_prompt(config, decisions)
        self.events.put(("report", ("LLM Prompt Preview", prompt)))
        self.events.put(("log", f"提示词已生成，包含 {len(decisions)} 个判定。\n"))

    def _generate_worker(self) -> None:
        config = self._config_from_ui()
        used = core.apply_apikey_file(config, None)[1]
        if used:
            self.events.put(("log", f"已自动加载 APIKEY.txt: {used}\n"))
        per_func = self.vars.get("per_function") and self.vars["per_function"].get()
        if per_func:
            decisions = core.discover_decisions(config)
            functions = core.extract_functions_from_source(config)
            if not functions:
                raise RuntimeError("未扫描到函数，无法生成测试。")
            self.events.put(("log", f"正在按函数生成测试（{len(functions)}个函数）...\n"))
            gen_result = core.generate_all_functions(config, decisions)
            if gen_result.get("errors"):
                for err in gen_result["errors"]:
                    self.events.put(("log", f"  WARNING: {err}\n"))
            test_content = core.merge_test_parts(config, gen_result)
            core.ensure_parent(config.test_output)
            config.test_output.write_text(test_content, encoding="utf-8")
            self.events.put(("log", f"已写入按函数生成的 CUnit 测试: {config.test_output}\n"))
        else:
            decisions = core.discover_decisions(config)
            if not decisions:
                raise RuntimeError("未扫描到判定，无法生成 MC/DC 测试。")
            payload = core.call_llm(config, core.build_prompt(config, decisions))
            test_file = payload.get("test_file")
            if not isinstance(test_file, str) or "#include" not in test_file:
                raise RuntimeError("大模型响应中没有有效的 test_file。")
            core.ensure_parent(config.test_output)
            config.test_output.write_text(test_file.rstrip() + chr(10), encoding="utf-8")
            notes = chr(10).join(payload.get("notes", []))
            self.events.put(("log", f"已写入 CUnit 测试: {config.test_output}\n{notes}\n"))

    def _run_worker(self, generate: bool) -> None:
        config = self._config_from_ui()
        used = core.apply_apikey_file(config, None)[1]
        if used:
            self.events.put(("log", f"已自动加载 APIKEY.txt: {used}\n"))
        decisions = core.discover_decisions(config)
        llm_payload = None
        if generate:
            per_func = self.vars.get("per_function") and self.vars["per_function"].get()
            if per_func:
                self.events.put(("log", "正在按函数生成测试...\n"))
                gen_result = core.generate_all_functions(config, decisions)
                if gen_result.get("errors"):
                    for err in gen_result["errors"]:
                        self.events.put(("log", f"  WARNING: {err}\n"))
                test_content = core.merge_test_parts(config, gen_result)
                core.ensure_parent(config.test_output)
                config.test_output.write_text(test_content, encoding="utf-8")
            else:
                if not decisions:
                    raise RuntimeError("未扫描到判定，无法生成 MC/DC 测试。")
                self.events.put(("log", "正在调用大模型生成 CUnit 测试...\n"))
                llm_payload = core.call_llm(config, core.build_prompt(config, decisions))
                core.ensure_parent(config.test_output)
                config.test_output.write_text(llm_payload["test_file"].rstrip() + chr(10), encoding="utf-8")
                self.events.put(("log", f"已写入 CUnit 测试: {config.test_output}\n"))

        if config.auto_build:
            self.events.put(("log", "正在自动编译运行测试...\n"))
            build_results, test_results = core.auto_build_and_run(config, config.test_output)
            coverage_results = []
        else:
            build_results = self._run_command_group("构建", config.build_commands, config.project_root)
            if any(item["returncode"] != 0 for item in build_results):
                test_results = []
                coverage_results = []
            else:
                test_results = self._run_command_group("测试", config.test_commands, config.project_root)
                coverage_results = self._run_command_group("覆盖率", config.coverage_commands, config.project_root)

        coverage_summary = core.collect_coverage_summary(config, coverage_results)
        report = core.write_report(config, decisions, llm_payload, build_results, test_results, coverage_results, coverage_summary)
        self.events.put(("report_file", report))

    def _run_command_group(self, label: str, commands: list[str], cwd: Path) -> list[dict[str, Any]]:
        results = []
        if not commands:
            self.events.put(("log", f"{label}: 没有配置命令，跳过。\n"))
            return results
        for command in commands:
            self.events.put(("log", f"$ {command}\n"))
            code, output = core.run_command(command, cwd)
            self.events.put(("log", output + "\n"))
            results.append({"command": command, "returncode": code, "output": output})
            if code != 0:
                self.events.put(("log", f"{label}失败，停止后续命令。\n"))
                break
        return results

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "error":
                    self.log(str(payload))
                    messagebox.showerror("任务失败", str(payload).splitlines()[-1])
                elif kind == "decisions":
                    self.show_decisions(payload)
                elif kind == "report":
                    title, text = payload
                    self.report_path_var.set(title)
                    self._set_text(self.report_text, text)
                    self.result_notebook.select(self.report_text.master)
                elif kind == "report_file":
                    self.load_report(Path(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def show_decisions(self, decisions: list[core.Decision]) -> None:
        for item in self.decision_tree.get_children():
            self.decision_tree.delete(item)
        for decision in decisions:
            self.decision_tree.insert(
                "",
                tk.END,
                values=(decision.file, decision.line, decision.expression, len(decision.conditions)),
            )
        self.result_notebook.select(self.decision_tree.master)

    def open_report_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="打开 MC/DC 报告",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if selected:
            self.load_report(Path(selected))

    def load_report(self, path: Path) -> None:
        self.report_path_var.set(str(path))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._set_text(self.report_text, format_report_summary(raw))
            self.log(f"已加载报告: {path}\n")
            self.result_notebook.select(self.report_text.master)
        except Exception as exc:
            messagebox.showerror("报告读取失败", str(exc))

    def log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)


def format_report_summary(report: dict[str, Any]) -> str:
    coverage = report.get("coverage_summary", {})
    decisions = report.get("decisions", [])

    def pct(key: str) -> str:
        value = coverage.get(key)
        return "N/A" if value is None else f"{value:.2f}%"

    def status(key: str) -> str:
        return core.command_group_status(report.get(key, []))

    lines = [
        "MC/DC 测试摘要",
        "=" * 72,
        f"工程目录: {report.get('project_root', '')}",
        f"测试文件: {report.get('test_output', '')}",
        f"HTML报告: {report.get('html_report', '')}",
        "",
        "流程状态",
        "-" * 72,
        f"构建: {status('build_results')}",
        f"测试: {status('test_results')}",
        f"覆盖率采集: {status('coverage_results')}",
        "",
        "覆盖率概览",
        "-" * 72,
        f"判定数量: {len(decisions)}",
        f"行覆盖率: {pct('lines_executed_percent')}",
        f"分支覆盖率: {pct('branches_executed_percent')}",
        f"分支至少执行一次: {pct('branches_taken_at_least_once_percent')}",
        f"GCOV文件数: {len(coverage.get('gcov_files', []))}",
        "",
        "MC/DC 判定与义务",
        "-" * 72,
    ]

    for index, decision in enumerate(decisions[:80], start=1):
        lines.append(
            f"{index}. {decision.get('file', '')}:{decision.get('line', '')}  "
            f"{decision.get('expression', '')}"
        )
        conditions = decision.get("conditions", [])
        if conditions:
            lines.append(f"   条件: {', '.join(str(item) for item in conditions)}")
        for obligation in decision.get("mcdc_obligations", []):
            condition = obligation.get("condition", "")
            need = obligation.get("need", "")
            lines.append(f"   - {condition}: {need}")
        lines.append("")

    if len(decisions) > 80:
        lines.append(f"... 还有 {len(decisions) - 80} 个判定未在摘要中展开，请查看 JSON/HTML 报告。")
        lines.append("")

    uncovered = coverage.get("uncovered_line_samples", [])
    lines.extend(["未覆盖行样例", "-" * 72])
    if uncovered:
        for item in uncovered[:30]:
            lines.append(f"{item.get('file', '')}: {item.get('line', '')}")
    else:
        lines.append("未发现样例，或覆盖率命令未生成 .gcov 文件。")

    lines.extend(
        [
            "",
            "说明",
            "-" * 72,
            "行覆盖率和分支覆盖率不能单独证明 MC/DC。",
            "请重点检查每个条件是否都有一对测试：只改变该条件，其他条件保持不变，且判定结果发生变化。",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
