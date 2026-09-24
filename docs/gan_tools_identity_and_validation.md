# 工具身份契约与合法性检验：实现记录

> 范围：本轮实现「身份统一 + 合法性检验 + `register_component` + `list_components`」四项工作，以及收尾的 #10/#11 两项重构。
> 承接：`docs/7_遗留问题与待办.md` §3、`gan_tools_ops_review.md`（漏洞梳理）、`gan_tools_ops_impl.md`（实现方案）、`gan_tools_components_structure.md`（结构诊断）。
> 仓库：`/root/HyperAgents`（WSL Ubuntu）。所有改动**未提交**，见文末变更清单。

---

## 1. 身份契约（Identity Contract）

### 1.1 问题

体系里存在**三套互不强制的身份**：

| 身份 | 定义处 | 用途 |
|---|---|---|
| 注册名 | `gan/registries/*.json` 的 `name` | 选择（`select_component`）、装配解析（`module_path`） |
| 文件 stem | 模块文件名 | `load_tools` 的工具键与 `names` 过滤（`agent/tools/__init__.py:45`） |
| `tool_info()["name"]` | 模块内声明 | LLM 实际调用的工具名 |

三者被要求一致，但此前**没有任何代码强制**。不一致的后果是**静默失效**：组件能装配、却永远不会被加载（`load_tools` 按 stem 过滤），且无任何报错。

### 1.2 本轮统一

4 个 evaluator 评估点的**注册名与文件 stem 不一致**（注册名 `trajectory_quality`，文件名 `eval_trajectory_quality.py`）。统一为**短名**（= 原注册名），保持既有 run 的 config 向后兼容：

| 旧文件 | 新文件 | `tool_info().name` |
|---|---|---|
| `eval_trajectory_quality.py` | `trajectory_quality.py` | `trajectory_quality` |
| `eval_hard_failure.py` | `hard_failure.py` | `hard_failure` |
| `eval_reward_hacking.py` | `reward_hacking.py` | `reward_hacking` |
| `eval_rule_violation.py` | `rule_violation.py` | `rule_violation` |

同步点：`gan/registries/evaluator.json` 的 `module`、各文件的 `tool_info()["name"]` 与模块 docstring 首行、`gan/design/seeds/evaluator.md` 的引用。

**验证**：装配 evaluator 工具集后加载，23 个工具的 `stem == tool_info().name` **无一例外**（`/tmp` 端到端脚本）。

### 1.3 契约的执行点

判据落在 `gan/registries/loader.py:entry_reason`（选择时），并由另外两处复用（§2.3）。

---

## 2. 合法性检验

### 2.1 判据

`entry_reason`（`gan/registries/loader.py:93`）在原有 4 条（是 dict、`name`/`kind`/`module` 非空、`kind ∈ KINDS`、`module` 文件存在）之上，新增 3 条身份契约判据：

| # | 判据 | 失败信息 |
|---|---|---|
| V1 | `name` == `module` 的文件 stem | `name '...' != module file stem '...' (tools are keyed by file stem; ...)` |
| V2 | `kind` 须与目录约定一致（`skill`↔`skills/`、`eval_point`↔`eval_points/`） | `kind '...' requires the module directly under a '.../' directory, ...` |
| V3 | 模块须**绑定** `tool_info` + `tool_function` | `module does not expose tool_info/tool_function: ...` |

`validate_registry`（`loader.py:251`）再补两类**条目之外**的问题：

| # | 类型 | 说明 |
|---|---|---|
| V4 | `duplicate` | 合并 `shared + <role>` 后 `(kind, name)` 重复——`ComponentRegistry.get` 返回**第一个**，后者被静默遮蔽 |
| V5 | `orphan` | 暴露 tool API 但**未被任何注册表声明**的文件 |
| — | `unparseable` | 注册表文件本身不可解析（此前被静默 `continue` 丢弃） |

### 2.2 孤儿判定：一次判据修正

**初版判据过宽**：把 `gan/components/**` 下**任何**未注册 `.py` 都算孤儿。它误伤了非组件文件——组件目录合法地存放被多个组件共享的 helper 模块，而 helper 不暴露 tool API、也不可能产生"静默不可选"（`assemble_tools_dir` 只拷贝**已选中**的模块）。

**修正后判据**：只有**暴露 `tool_info`/`tool_function`** 且未被任何注册表声明的文件才算孤儿。即规则**恰好命中它要防的失败模式**。

**实测对照**（同一仓库状态，修正前后）：

```
修正前  code_repo.registry_report → ['task/skills/helper_util.py', 'task/skills/realish.py']
修正后  code_repo.registry_report → ['task/skills/realish.py']
        loader.validate_registry  → ['task/skills/realish.py']      ← 两者一致
        helper_util.py: exposes_tool_api=False  (非组件)
        realish.py:     exposes_tool_api=True   (真组件却没注册)
```

修正的直接收益：项目自有的 `scripts/local/test_kinship.py`（往组件目录写非组件探针文件验证 git 血缘）**无需改动即恢复通过**。

**已知残余缺口**（可接受）：既未注册、又因笔误（如 `tool_infо` 用了西里尔字母）而没绑定 API 的文件不会被报孤儿——它与 helper 本质无法区分，任何判据都做不到；它在被注册时会被 V3 拦下。

### 2.3 三处生效点

同一套判据服务三个时机，语义**故意不同**：

| 时机 | 判据 | 行为 | 落点 |
|---|---|---|---|
| 选择时 | 单条目 | 拒绝该次选择 | `select_component`（既有） |
| 提交时 | **差分**（只拒新增） | 回滚 + `PatchRejected` | `code_repo.apply_code_patch` |
| 启动时 | **严格**（零问题） | fail-fast | `preflight.preflight_tools` |

**提交门控**（`gan/framework/code_repo.py`）：

- `registry_report` 报告四类问题：`invalid` / `unparseable` / `duplicate` / `orphan`；
- 触发条件从"仅补丁触及 `gan/registries/`"**放宽到"或 `gan/components/`"**（`_needs_registry_check`）——此前"新增组件文件却忘了注册表"**完全不触发校验**，正是静默失败来源；
- `_registry_worsened` 为**差分**：只拒"新增"的问题，既存问题不阻塞（否则开启该门控会把历史遗留一次性打回）；
- `invalid` 集合的键**含 reason**（`(file, kind, name, reason)`），使"无效原因变了"也能被识别。

**启动校验**（`gan/framework/preflight.py` + `gan/build.py`）：

- `preflight_tools` 校验每个 role 的注册表 + 装配碰撞，纯本地检查（文件系统 + AST，**无模型调用**）；
- `assemble_collisions` 检查装配会**静默覆盖**的 basename 冲突（`assemble_tools_dir` 按 basename 落盘，后者覆盖前者）；
- 由 `loop.toolset_preflight`（默认 `true`）控制：`true` 时 fail-fast，`false` 时仅记事件（`preflight_tools_warning`）。默认开启的理由：它是纯本地检查，而损坏的注册表否则会静默降级。

### 2.4 与选择静默降级的关系

装配端遇 `module_path` 为 None（未注册/invalid）时**仍**静默跳过（`gan/tools/assembly.py:selected_module_paths`）。本轮通过"启动期 fail-fast + 提交期差分拒绝 + `list_components` 可见"让该状态**不再静默**，但未改装配端的跳过行为（那属结构重构范畴，本轮明确不做）。

---

## 3. 两个新工具

### 3.1 `register_component`（deep）

`gan/tools/deep/register_component.py`，与 `unregister_component` **对称**。

- 归类依据：注册/注销同类，`loader.py` 与 `select_component.py` 均声明"注册新组件是源码级（deep）改动"；
- 行为：定位注册表 → `frozen.is_allowed` 权限门控（注册表、组件源码各一次）→ 校验模块**已存在**（文件创建仍归 `edit_source`）→ 校验身份契约（V1，`entry_reason` 兜底）→ 改 **workspace 副本** → 随会话 patch 提交，走 `allowlist + compile + registry` 校验；
- 默认注册表：`planner → task.json`、`evaluator → evaluator.json`；`_WRITABLE_REGISTRY` 排除 `planner.json`（无任何 role 的写权限）；
- 幂等：同名同 module → `Already registered:`；同名异 module → 错误。

**修复的一处静默失败**：`AccessBroker.grant` 会**无条件**把仓库文件重新拷到 workspace，覆盖会话内的编辑。因此同一会话内第二次 `register_component` 会**丢弃首次登记**（实测：`['alpha','beta']` → `['beta']`）。修法：仅当 workspace 副本**不存在**时才 grant。复测通过（`['alpha','beta']` 均保留）。

> 注：`unregister_component` 有**同样**的框架属性（同会话两次注销，第一次的删除会被还原）。本轮未改它（超出 #10/#11 范围），已记录于此供后续决策。

### 3.2 `list_components`（work/common）

`gan/tools/work/common/list_components.py`，与 `list_editable` 同形（角色无法预先枚举可选项）。

一次只读调用返回五个键：

| 键 | 内容 |
|---|---|
| `role` | 当前角色 |
| `selected` | 当前 design config 各槽位的选择 |
| `registered` | 注册表条目（`name`/`kind`/`module`/`valid`/`reason`/`description`） |
| `problems` | `validate_registry` 的非 orphan 问题 |
| `unregistered` | orphan（"文件已存在但未注册"的候选，供 `register_component`） |

**关键契约**：`registered[].name` 直接报告注册条目 `name`——身份统一后它**就是**可调用名。否则 agent 会按 config 里的名字去调而必然 `Tool not found`。

**闭环**：`edit_source` 建文件 → `list_components` 看到 `unregistered` → `register_component` 登记 → 提交门控放行 → `select_component` 启用。若跳过第三步，提交时按"新增孤儿"打回并提示。

---

## 4. #10 / #11 重构

### 4.1 #10：统一"哪些文件会被装配"

**问题**：`preflight._py_files` 与 `assemble_tools_dir` 的拷贝循环**平行实现**了同一套过滤（顶层 `*.py`、跳过 `__` 前缀），两者会漂移；且 `preflight` 跨模块导入了私有函数 `_gan_roots`。

**改动**（`gan/tools/assembly.py`）：

- `_gan_roots` → **`gan_roots`**（转公开，两个模块共用）；
- 新增 **`py_files_in(src_dir)`**：装配源文件选择的**唯一定义**；
- `assemble_tools_dir` 改为调用 `py_files_in`（删除内联 glob）；
- `gan/framework/preflight.py` 删除 `_py_files`，改从 `gan.tools.assembly` 导入 `py_files_in` / `gan_roots`；顺带删除随之未使用的 `import glob as _glob`。

**验证**（`/tmp/py_files_unify.py`，全部 PASS）：`py_files_in` 存在且被 `assemble_tools_dir` 调用、内联 glob 已消失、`preflight` 不再定义 `_py_files`、不再导入 `_gan_roots`、`__` 前缀被正确排除、真实 evaluator 装配结果 == 各 always-on 目录 `py_files_in` 的并集。

### 4.2 #11：AST 校验覆盖更多绑定形式

**问题**：`_exposes_tool_api` 只识别 `def` 与 `import`，不识别赋值式绑定（`tool_info = _mk`）。

**关键判断——不采纳"接受 `as` 别名"**：审查建议"接受 `from x import tool_info as ti`"。但实测运行时语义相反：

```
Skipping tool .../renamed_import.py: missing tool_info/tool_function
```

Python 在 `as` 存在时绑定的是 `asname`，故该形式**不暴露** `tool_info`；`load_tools` 用 `hasattr(module, "tool_info")` 判定，会跳过它。若 AST 接受该别名，就会出现"**校验通过但运行时静默不加载**"——恰是本套校验要消灭的失败模式。

**改动**（`gan/registries/loader.py`）：判据统一为**绑定名（bound name）**语义——

- `def` / `async def`；
- `from x import tool_info, tool_function`（共享 skill 的 re-export 形式）；
- `import x as tool_info`；
- 顶层赋值 `tool_info = ...` 与带注解赋值 `tool_info: Any = ...`；
- **`as` 别名按 `asname` 计入**（`from x import tool_info as ti` → 绑定 `ti`，不算暴露 `tool_info`）。

**验证**（`/tmp/ast_vs_runtime.py`）：6 种形态下 **AST 与 `load_tools` 运行时判定完全一致**（`def`/`reexport`/`assign`/`ann_assign` → 双方 True；`renamed_import`/`partial` → 双方 False）。

---

## 5. 验证汇总

| 套件 | 结果 |
|---|---|
| `py_compile`（全部改动文件） | `COMPILE_OK` |
| `scripts/local/test_kinship.py` | `ALL KINSHIP CHECKS PASSED` |
| 提交门控集成测试（5 例） | `ALL PASS` |
| 二次注册数据保留 | `PASS both registrations survive` |
| `scripts/local/test_frozen.py` | `FROZEN-LAYOUT INVARIANTS PASSED` |
| AST ↔ 运行时一致性（6 形态） | `AST and runtime AGREE on all forms` |
| #10 装配源选择统一 | `ALL PASS` |
| `preflight_tools`（真实仓库） | `tools_ok=True`，三角色 0 问题 0 碰撞 |
| evaluator 端到端装配 + 加载 | 23 工具，`stem == tool_info().name` 无例外 |

### 回归事故与处置（如实记录）

放宽提交门控触发面后，`test_kinship.py` 从 gen1 起全部 `applied: False`。对照 `git archive HEAD` 的干净副本确认**是本次改动引入的回归**，而非测试脆弱。根因即 §2.2 的孤儿判据过宽（该测试故意新增未注册的非组件探针文件）。判据修正后**测试无需改动即恢复通过**。

---

## 6. 变更清单（未提交）

**新增**
- `gan/tools/deep/register_component.py`
- `gan/tools/work/common/list_components.py`

**修改**
- `gan/registries/loader.py` — 身份契约判据、`validate_registry`、`orphan_modules` / `declared_modules`、`_exposes_tool_api`（#11）
- `gan/framework/code_repo.py` — 四类问题报告、差分门控、放宽触发条件
- `gan/framework/preflight.py` — `preflight_tools` / `assemble_collisions` / `tools_ok`（复用 `py_files_in`，#10）
- `gan/tools/assembly.py` — `gan_roots` / `py_files_in`（#10）
- `gan/build.py` — 接入启动校验 + `loop.toolset_preflight` 开关
- `gan/framework/loop.yaml` — 新增 `loop.toolset_preflight`
- `gan/registries/evaluator.json`、4 个评估点文件（重命名）、`gan/design/seeds/evaluator.md` — 身份统一
- `gan/tools/deep/unregister_component.py` — `_REGISTRY_FILES` 改从 loader 导入
- `AGENTS.md` — deep 清单补 `register_component`、`work/common` 清单补 `list_components`、新增 Identity contract 条目

**文档**
- 本文件；`docs/gan_tools_ops_review.md`、`docs/gan_tools_ops_impl.md`、`docs/gan_tools_components_structure.md`（本轮调查与分析）

---

## 7. 未做（明确边界）

- **结构重构**（`gan/tools` / `gan/components` 目录重划、`roles` 字段、B5 修复）——用户明确"暂不大幅重构"；
- **`unregister_component` 的同会话数据丢失**——与 §3.1 同源的框架属性，本轮未改；
- **装配端静默跳过的行为**——仅使其可见（启动 fail-fast / 提交拒绝 / `list_components`），未改跳过逻辑；
- **`docs/gan_tools_components_structure.md` 中一处已过期描述**：§1.2 记录的"注册名 vs 可调用名不一致"在身份统一后已不成立（该文档为当时快照）。
