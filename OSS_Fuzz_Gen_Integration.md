# OSS-Fuzz-Gen Integration for Automated C Unit Testing

## Overview

[OSS-Fuzz-Gen](https://github.com/google/oss-fuzz-gen) is an open-source fuzzing test auto-generation tool developed by Google. It uses LLMs to automatically generate fuzz targets for C/C++ projects. This document explains how OSS-Fuzz-Gen can complement and enhance our cunit_mcdc_tool.

## Core Capabilities of OSS-Fuzz-Gen

- **Fuzz Target Auto-Generation**: Generates libFuzzer/AFL-compatible fuzz targets based on project APIs
- **Harness Building**: Automatically handles build system integration (CMake/Make/Bazel)
- **Mutation-Driven Testing**: Discovers boundary conditions and exception paths through fuzzy inputs
- **Continuous Integration**: Integrates with OSS-Fuzz infrastructure for continuous fuzzing

## How OSS-Fuzz-Gen Enhances This Tool

### 1. Boundary Value Auto-Discovery - Complementing LLM

Our tool uses prompts to ask LLMs to generate boundary test cases (max, min, normal, abnormal values), but LLMs may miss certain boundary conditions. OSS-Fuzz-Gen fuzzing can:

- **Auto-discover integer overflow/underflow**: Through random input mutation, find overflows that LLM did not consider
- **Discover unexpected input combinations**: Fuzzing randomness covers parameter combinations hard for LLMs to foresee
- **Validate LLM-generated expected values**: Fuzzing can verify whether expected values in LLM-generated tests are correct

**Integration**: After our tool generates CUnit tests, run OSS-Fuzz-Gen fuzz targets on the same functions, and convert discovered crashes/anomalies into new CUnit test cases.

### 2. Build System Auto-Integration

Our auto_compile_and_run() requires users to configure compiler, cunit_include_dir, cunit_lib_dir. OSS-Fuzz-Gen provides:

- **Auto-detect project build system** (CMake, Make, Bazel)
- **Auto-parse compilation options and dependencies**
- **Generate project-specific Docker build environments**

**Integration**: Use OSS-Fuzz-Gen build analysis to auto-generate cunit_mcdc_config.json settings for include_dirs, compiler, cunit_lib_dir, achieving zero-configuration out-of-the-box usage.

### 3. MC/DC Test Enhancement via Fuzzing Coverage Feedback

After running fuzzing, OSS-Fuzz-Gen produces coverage data that can:

- **Identify MC/DC uncovered paths**: Fuzzing coverage supplements gcov branch coverage data
- **Generate targeted tests**: Feed fuzzing-discovered new paths back to LLM for targeted MC/DC test generation
- **Closed-loop optimization**: Form a loop of  LLM generates tests -> compile/run -> fuzzing explores -> feedback new paths -> LLM supplements tests

### 4. Reference Implementation and Templates

OSS-Fuzz-Gen has generated fuzz targets for hundreds of open-source projects, which can serve as:

- **LLM prompt examples**: Use existing project fuzz targets as few-shot examples to improve LLM generation quality
- **Stub/Mock templates**: Harness code in fuzz targets can be referenced for generating test stubs
- **Error handling patterns**: Error patterns discovered by fuzzing can enhance computation boundary test prompts

## Recommended Integration Architecture

```
+---------------------------------------------+
|           cunit_mcdc_tool (This Tool)        |
|  +----------+  +----------+  +-----------+  |
|  | Per-func  |  | Compute  |  | Auto      |  |
|  | CUnit Gen |  | Boundary |  | Compile   |  |
|  +-----+----+  +-----+----+  +-----+-----+  |
|        |             |             |         |
|        v             v             v         |
|  +--------------------------------------+    |
|  |         LLM (GPT-4.1 etc)            |    |
|  +------------------+-------------------+    |
|                     |                        |
+---------------------+------------------------+
                      |
          +-----------+-----------+
          |  OSS-Fuzz-Gen Layer   |
          | +-----------------+   |
          | | Build Analysis  |---|--> Auto-configure compiler/include/lib
          | +-----------------+   |
          | | Fuzz Target Gen |---|--> Supplement boundary test cases
          | +-----------------+   |
          | | Coverage Feed   |---|--> Identify uncovered MC/DC paths
          | +-----------------+   |
          | | Crash Replay    |---|--> Convert to CUnit regression tests
          | +-----------------+   |
          +-----------------------+
```

## Implementation Suggestions

1. **Short-term**: Integrate OSS-Fuzz-Gen build analysis in auto_compile_and_run() to auto-detect CUnit library paths and compilation options
2. **Medium-term**: In generate_all_functions() loop, simultaneously generate fuzz targets per function, use fuzzing results to supplement CUnit tests
3. **Long-term**: Build closed-loop feedback - LLM generates tests -> compile/run -> fuzzing explores -> discover new paths -> LLM supplements tests -> re-verify

## Summary: Complementary Relationship

| Dimension | This Tool | OSS-Fuzz-Gen |
|-----------|-----------|--------------|
| Test Type | Unit Testing (CUnit) | Fuzzing (libFuzzer) |
| Coverage Goal | MC/DC Decision Coverage | Code Path/Crash Discovery |
| Input Generation | LLM-inferred Boundary Values | Random Mutation + Coverage-Guided |
| Expected Values | LLM-computed Assertions | No expected values (detect crashes) |
| Strength | Precise, assertable, auditable | Discovers unknown vulnerabilities, covers long-tail paths |

The combination achieves: **LLM handles precise validation of known logic, while Fuzzing handles exploration of unknown paths**, ultimately reaching higher MC/DC coverage and stronger safety assurance.