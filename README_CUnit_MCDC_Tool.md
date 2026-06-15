# Python CUnit MC/DC 自动化测试工具

本目录包含一个可运行的 Python CLI：

- `cunit_mcdc_tool.py`：扫描 C 工程中的判定条件，连接 OpenAI 兼容大模型生成 CUnit 测试，执行构建/测试/覆盖率命令，并输出 MC/DC 义务报告。
- `cunit_mcdc_gui.py`：基于 Tkinter 的桌面界面，可编辑配置、扫描判定、预览提示词、生成 CUnit 测试、运行构建/测试/覆盖率并查看报告。

> **全自动化（推荐）**：读取 `APIKEY.txt` 配置大模型、按函数拆分上传避免 token 超限、对数值计算生成边界值与预期值测试、自动编译运行无需手写命令。完整步骤见 [`全自动操作指南.md`](./全自动操作指南.md)。OSS-Fuzz-Gen 的互补作用见 [`OSS_Fuzz_Gen_Integration.md`](./OSS_Fuzz_Gen_Integration.md)。

## 快速开始

```powershell
python .\cunit_mcdc_tool.py init -o .\cunit_mcdc_config.json
```

编辑生成的 `cunit_mcdc_config.json`，把 `project_root`、源码路径、构建命令、测试命令、覆盖率命令改成你的 C 工程实际情况。

设置 API Key：

```powershell
$env:OPENAI_API_KEY="你的 Key"
```

扫描判定条件：

```powershell
python .\cunit_mcdc_tool.py scan -c .\cunit_mcdc_config.json
```

只查看发给大模型的提示词：

```powershell
python .\cunit_mcdc_tool.py generate -c .\cunit_mcdc_config.json --dry-run-prompt
```

生成 CUnit 测试：

```powershell
python .\cunit_mcdc_tool.py generate -c .\cunit_mcdc_config.json
```

生成并运行构建、测试、覆盖率采集：

```powershell
python .\cunit_mcdc_tool.py run -c .\cunit_mcdc_config.json --generate
```

运行后会在 `test_output` 同目录生成 `*.mcdc_report.json`。

同时会生成同名 HTML 报告，例如：

```text
auto_mcdc_tests.mcdc_report.json
auto_mcdc_tests.mcdc_report.html
```

JSON 适合程序读取，HTML 适合人工查看。HTML 会展示流程状态、覆盖率概览、MC/DC 判定列表、每个条件的独立影响义务，以及未覆盖行样例。

## GUI 界面

启动界面：

```powershell
python .\cunit_mcdc_gui.py
```

界面包含几个区域：

1. `基础配置`：填写 C 工程目录、源码匹配、Include 目录、测试输出文件、大模型地址和模型名。
2. `命令`：每行填写一条构建、测试或覆盖率命令。
3. `提示词补充`：添加项目专属约束，例如只测试 public API、如何 stub 外设依赖。
4. `MC/DC 判定`：显示扫描出的判定表达式和条件数量。
5. `日志`：显示生成、构建、测试和覆盖率命令输出。
6. `报告`：查看提示词预览或 `*.mcdc_report.json`。

`临时 API Key` 只会写入当前 GUI 进程环境变量，不会保存到 JSON 配置文件。

## “生成并运行”是否全自动

在配置正确的前提下，`生成并运行` 是自动流程：

1. 扫描 C 源码中的判定表达式。
2. 构造 MC/DC 测试生成提示词。
3. 调用大模型生成 CUnit 测试文件。
4. 写入 `test_output`。
5. 执行 `build_commands`。
6. 执行 `test_commands`。
7. 执行 `coverage_commands`。
8. 生成 JSON 和 HTML 报告。

但它不是“无需工程适配”的魔法按钮。你仍需要提前配置：

- C 工程能在当前 Windows 机器上编译。
- CUnit 已安装或已随工程提供。
- 测试 runner 能把生成的测试文件编进测试目标。
- 外设、OS、网络、文件系统等依赖有 fake/stub 或可测试替身。
- 覆盖率工具能生成 `gcov` 输出或等价覆盖率输出。

如果大模型生成的测试第一次编译失败，通常需要根据日志调整 `extra_prompt`、工程 stub、头文件 include 或测试 runner。

## Windows 7 运行说明

可以在 Windows 7 上运行，建议使用 Python 3.8.x。不要使用 Python 3.9 或更高版本作为 Win7 目标环境，因为官方 Windows 7 支持通常停留在 Python 3.8 系列。

推荐环境：

- Windows 7 SP1
- Python 3.8.10，安装时勾选 `tcl/tk and IDLE`，这样 GUI 所需的 Tkinter 会一起安装
- MinGW-w64 或其他可用的 C 编译工具链
- CMake，版本需选择仍支持 Windows 7 的版本
- CUnit、gcov/lcov 或对应工具链自带的覆盖率工具
- 可访问大模型 API 的网络环境，或内网 OpenAI 兼容接口
- 若要打包为 exe，需要在目标兼容环境安装 PyInstaller

启动 GUI：

```bat
cd /d C:\path\to\outputs
python cunit_mcdc_gui.py
```

如果双击启动，可以新建 `start_gui.bat`：

```bat
@echo off
cd /d %~dp0
python cunit_mcdc_gui.py
pause
```

如果要在没有 Python 的 Win7 机器上运行，可以用 PyInstaller 在同类环境中打包：

```bat
pip install pyinstaller
pyinstaller --onefile --windowed cunit_mcdc_gui.py
```

注意：大模型接口需要 HTTPS 连接。若 Win7 机器访问 API 失败，请先确认系统已安装 TLS 1.2 相关更新，或使用公司内网代理/兼容网关。

## 配置示例

```json
{
  "project_root": "C:/path/to/your/c/project",
  "source_globs": ["src/**/*.c", "include/**/*.h"],
  "include_dirs": ["include"],
  "test_output": "tests/auto_mcdc_tests.c",
  "build_commands": [
    "cmake -S . -B build -DCMAKE_C_FLAGS=\"--coverage\"",
    "cmake --build build"
  ],
  "test_commands": ["build/your_cunit_runner"],
  "coverage_commands": ["gcov -b -c src/*.c"],
  "llm_base_url": "https://api.openai.com/v1",
  "llm_api_key_env": "OPENAI_API_KEY",
  "llm_model": "gpt-4.1",
  "max_llm_rounds": 1,
  "extra_prompt": "Prefer testing public APIs; create local stubs only when necessary."
}
```

## 关于 MC/DC

MC/DC 要求每个原子条件都能独立影响判定结果。工具会列出每个判定表达式及其条件义务，并要求大模型生成能形成独立影响对的 CUnit 测试。

需要注意：`gcov`/`lcov` 的行覆盖、分支覆盖不能单独证明 MC/DC。此工具输出的报告会把发现的判定和 MC/DC obligations 固化下来，方便你审查生成测试是否满足独立影响对。如果你的项目有安全认证要求，建议把本工具作为测试生成与回归辅助，而不是唯一证明材料。

## 工程接入建议

1. 让 C 工程支持 coverage flags，例如 GCC/Clang 的 `--coverage`。
2. 准备一个 CUnit runner，并把 `test_output` 加入测试目标。
3. 对外设、OS、网络、文件系统依赖提供 fake/stub。
4. 先运行 `scan` 检查工具识别出的判定是否合理。
5. 运行 `generate --dry-run-prompt`，必要时通过 `extra_prompt` 约束大模型使用指定 API 或 stub 策略。
6. 运行 `run --generate`，根据编译错误和报告迭代。
