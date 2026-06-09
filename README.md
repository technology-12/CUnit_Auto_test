# CUnit Auto Test — AI-Powered MC/DC Test Generator for C Projects

> 基于深度求索 (DeepSeek) LLM 的全自动 CUnit 单元测试生成器，支持 MC/DC 覆盖率追踪。

## 功能概览

```
递归扫描 .c 文件
  → 解析函数定义 + 提取函数体/调用关系/结构体/宏/头文件
    → 按文件分批调用 LLM 生成测试代码
      → 生成多个 test_*.c + CUnit Runner
        → 自动构建 / 运行测试 / 覆盖率收集
          → 输出函数级覆盖报告 + MC/DC 判定报告
```

## 项目结构

```
├── cunit_mcdc_tool.py          # 核心工具 (CLI)
├── cunit_mcdc_gui.py           # Tkinter 图形界面
├── README.md
├── .gitignore
└── work/                        # 示例 C 项目
    ├── config.json              # 工具配置文件
    ├── Makefile
    ├── include/
    │   └── common.h             # 共享宏/结构体/枚举
    ├── src/
    │   ├── math_ops.h / .c      # 数学函数 (if/else, for, switch, &&, ||)
    │   ├── string_ops.h / .c    # 字符串函数 (while, 嵌套 if, 复合条件)
    │   └── main.c               # 入口
    └── tests/
        ├── auto_function_tests/ # LLM 生成的测试文件
        │   └── test_*.c
        └── custom_runner.c      # 测试 Runner
```

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 标准库（无额外 pip 包） |
| GCC 14+ | MSYS2 UCRT64 |
| CUnit 2.1 | `pacman -S mingw-w64-ucrt-x86_64-cunit` |
| gcov | GCC 自带 |
| DeepSeek API Key | 或任意 OpenAI 兼容 API |

## 快速开始

### 1. 配置

编辑 `work/config.json`：
```json
{
  "project_root": "./work",
  "source_globs": ["src/**/*.c", "include/**/*.h"],
  "llm_base_url": "https://api.deepseek.com/v1",
  "llm_model": "deepseek-v4-pro",
  "max_functions_per_prompt": 4
}
```

### 2. 设置 API Key

```powershell
$env:DEEPSEEK_API_KEY = "sk-your-key"
```

### 3. 扫描源码

```powershell
# 扫描所有判定条件
python cunit_mcdc_tool.py scan -c work/config.json

# 扫描所有函数（含调用关系/结构体/宏/头文件）
python cunit_mcdc_tool.py scan-functions -c work/config.json
```

### 4. 生成测试

```powershell
# 预览提示词（不调 LLM）
python cunit_mcdc_tool.py generate-functions -c work/config.json --dry-run-prompt

# 调 LLM 生成测试文件
python cunit_mcdc_tool.py generate-functions -c work/config.json
```

### 5. 构建 & 运行 & 覆盖率

```powershell
# 一键：生成 + 构建 + 测试 + 覆盖率报告
python cunit_mcdc_tool.py run -c work/config.json --generate-functions
```

### 6. GUI 模式

```powershell
python cunit_mcdc_gui.py
```

## 验证结果 (work/ 项目)

```
╔══════════════════════════════════════════╗
║  CUnit - A unit testing framework for C ║
╠══════════════════════════════════════════╣
║  Suites    4                             ║
║  Tests    56    Passed  56    Failed   0 ║
║  Asserts  74    Passed  74    Failed   0 ║
╠══════════════════════════════════════════╣
║  Coverage:                              ║
║    math_ops.c   →  100.00% (50 lines)   ║
║    string_ops.c →  100.00% (24 lines)   ║
╚══════════════════════════════════════════╝
```

### MC/DC 判定覆盖

| 函数 | 判定条件 | MC/DC |
|------|----------|-------|
| `max_of_three` | `a>=b && a>=c` (2条件) | ✅ |
| `max_of_three` | `b>=a && b>=c` (2条件) | ✅ |
| `str_compare` | `a==NULL \|\| b==NULL` (2条件) | ✅ |
| `divide` / `factorial` / `power` / `find_char` 等 | if/while/switch | ✅ |

## 工具特性

### 结构化函数提取 (Feature 3)

每个函数自动提取：

| 字段 | 示例 |
|------|------|
| `function_body` | 完整函数体 |
| `called_functions` | `["add", "printf", "clamp"]` |
| `used_structs` | `["point", "Result"]` |
| `used_macros` | `["SUCCESS", "ERROR_NEGATIVE"]` |
| `required_headers` | `["<stdio.h>", "common.h"]` |

### 分批策略

- 按源文件分组，同文件函数合批
- 每批最多 `max_functions_per_prompt` 个函数
- 提示词包含完整的结构化上下文 + 原始源码摘录

## 配置项说明

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `project_root` | `.` | C 项目根目录 |
| `source_globs` | `src/**/*.c` | 源文件通配符 |
| `include_dirs` | `["include", "src"]` | 头文件搜索路径 |
| `llm_base_url` | `https://api.openai.com/v1` | LLM API 地址 |
| `llm_model` | `gpt-4.1` | 模型名称 |
| `max_functions_per_prompt` | `8` | 每批最大函数数 |
| `max_llm_rounds` | `1` | LLM 重试次数 |
| `llm_context_char_limit` | `36000` | 上下文上限 |

## License

MIT
