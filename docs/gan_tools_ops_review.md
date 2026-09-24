# 工具与算子管理：漏洞梳理与修改建议

> 出发点：`docs/7_遗留问题与待办.md` §3（流程分段：实现和注册 → 选配 → 装配 → 提示词注入 → 调用 → 返回 → 信息传递 → 继承）
> 方法：只读代码调查（仓库 `\\wsl.localhost\Ubuntu\root\HyperAgents`），逐环节给出 `文件:行号` 证据。未执行仓库代码。
> 说明：本轮新增 9 项事实（编号 N1–N9），其中 5 项为 `docs/7` §3 未登记；已有条目（T1/T2/R4/F1/G5/B5）在末尾对照。

---

## 0. 结论摘要

**§3 提出的"系统性梳理"清单，逐项现状：**

| 梳理项 | 现状 | 漏洞 |
|---|---|---|
| 格式合法性检验 | 部分有（条目字段 + module 文件存在） | 无工具 schema 校验；无 `tool_info`/`tool_function` 存在性校验；无启动期校验 |
| 重名检验 | **无** | T1 + N1（覆盖面比 T1 更广）+ N2（名称契约） |
| 注册表↔代码实现统一检验 | **单向**（仅 patch 触及注册表时） | N6 无孤儿检测；无 import 失败检测 |
| 是否需要在其他位置也检验 | 选择时有（`select_component`）；装配/加载时静默 | N7 缺装配期 preflight |
| 是否需要自动注册 | **否**，全手工 | 新增工具需改 2+ 处，且无一致性守卫 |
| always on / opt in 抉择 | task 全 opt-in；planner/evaluator 的 design/deep 强制常驻 | B5（task 可取 bash）；planner 无 opt-in 面 |
| 装配 | basename 覆盖，静默跳过 | T1、N1、选择静默降级 |
| 提示词注入 | schema 拼进 user message；无长度约束 | N9 双写 prompt；set_prompt 无上限 |
| 调用 | 错误统一转字符串；`max_toolcall` 兜底 | task 侧无独立预算（N8）；参数不校验 |
| 返回 | 无统一 cap / 清洗 | 不可信内容原样回填 |
| 信息传递 | `build_diff_summary` 是唯一收敛点 | R4 死分支；**4 条未脱敏通道**（N3）；receipt 内策略不一致（N4） |
| 继承 | 注册表/代码在 git；**config 不在 git** | N5 |

**优先级建议（本轮新增项）：**
- **高**：N3（信息传递 4 条未脱敏通道，含 `set_param.value`/`respond_issue.feedback`）、N5（config 未被 git 覆盖，继承面不成立）
- **中**：N1（覆盖面修正 T1 的修法范围）、N2（名称契约校验）、N6（孤儿检测）、N7（装配期 preflight）
- **低**：N4（receipt 字段策略统一）、N8（task 预算键）、N9（prompt 双写去重）

---

## 1. 实现与注册

### 1.1 注册表结构与合法性检验（现状）

- 数据结构：`gan/registries/<role>.json` + `shared.json`，条目字段 `name/kind/module`（必需）+ `description/params_schema`（可选）。`KINDS = ("skill","eval_point")`（`gan/registries/loader.py:26-27`）。
- 合法性判据：`entry_reason`（`loader.py:50-63`）校验 ①是 dict ②`name/kind/module` 非空 ③`kind ∈ KINDS` ④`module` 指向的文件**存在**。返回 None 即 valid。
- 容错加载：`load_registry_for_role(tolerant=True)`（`loader.py:115-129`）——非法条目**保留但标记 invalid**，永不被使用（选择/装配/加载都跳过），**不抛异常**。
- 合并：`entries = shared + specific`（`loader.py:128`）→ **`shared.json` 对所有角色生效**。

### 1.2 漏洞：检验是单向的，且缺三类校验

**N6（新发现）— 无"孤儿组件"检测（有文件但未注册）**

现有 `registry_report`（`gan/framework/code_repo.py:283-301`）只做**注册表 → 代码**方向：遍历 4 个 json，对每个条目跑 `entry_reason`。它**不扫描** `gan/components/**` 找出"文件存在但注册表无条目"的组件。这类组件永远无法被 `select_component` 选中，是静默死资产（例如 `gan/components/task/skills/` 为空目录、`shared/skills/editor.py` 已注册但需核对）。

**缺失项 1 — 无工具 schema 合法性校验**
`tool_info()` 返回的 dict 由模块手写（如 `gan/tools/design/set_prompt.py:5-14`），框架**不校验**其结构、`name` 与文件名的关系、`input_schema` 合法性。装配/加载时若 `tool_info` 抛异常，只记一行日志（`agent/tools/__init__.py:52-54`）。

**缺失项 2 — 无"注册了但 import 失败"检测**
`entry_reason` 只检查文件**存在**，不检查文件**可导入**、也不检查模块真的暴露 `tool_info`/`tool_function`。一个语法正确但运行时 import 失败的组件，注册表判 valid，装配时静默跳过（见 §1.3）。

**缺失项 3 — 无启动期 preflight**
`gan/framework/preflight.py:15-38` **只探模型可用性**（对每个 model key 发 1-token 请求）。工具集/注册表/组件在启动期**零校验**：`build_gan_loop`（`gan/build.py:65-144`）只做 domains 形状检查与 model resolve，不校验注册表一致性、不校验组件可加载。

### 1.3 漏洞：无重名检验（T1 的完整版）

**T1（已登记）+ N1（新发现，覆盖面修正）**

- 装配按 `os.path.basename` 复制、直接 `shutil.copy2`，**无任何同名判断**（`gan/tools/assembly.py:111,114`）。`module_path` 返回**完整相对路径**（`loader.py:97-102`），装配时被压平为 basename。
- 复制顺序（后写覆盖先写）：
  ```
  work/<role> → work/common → design → deep → 选中的 opt-in 组件
     低优先级                                    高优先级
  ```
  always-on 段：`assembly.py:106-111`；opt-in 段：`assembly.py:112-114`。
- **N1（覆盖面修正）**：`docs/7:239` 与 `docs/6:370` 把 T1 描述为"`work/<role>` 新工具与 `work/common` 同名时被后者静默覆盖"。但**opt-in 段在所有 always-on 之后执行**（`assembly.py:112-114` 在 `:106-111` 之后），因此**任何 always-on 目录（含 `design`、`deep`）都能被 opt-in 组件静默覆盖**——包括冻结的 design 算子与 deep 门控工具。修法范围应据此扩大：不仅要查 `work/<role>` vs `work/common`，而要对**整个目标目录**做 basename 唯一性检查。

**N2（新发现）— 名称契约隐含耦合，装配端无校验**

工具名 = 文件 stem（`agent/tools/__init__.py:45` `tool_name = tool_file.stem`），而加载过滤用的是 config 里的组件名（`__init__.py:46` `names == 'all' or tool_name in names`）。这意味着：

> **注册表条目 `name` 必须等于其 `module` 的 basename，否则该组件装配后被 `names` 过滤掉、静默不加载。**

装配端（`assembly.py`）与注册表（`loader.py`）都**不校验**这条隐含契约。一个 `name: "my_skill"` / `module: "task/skills/my_skill_v2.py"` 的条目会装配成功却永不生效，且无任何提示。

### 1.4 是否需要自动注册（现状）

新增一个工具/组件当前需手工改：
1. 新增 `gan/components/<area>/<file>.py`（或 `gan/tools/work/<role>/<file>.py`）；
2. 在 `gan/registries/{shared,<role>}.json` 加条目（`name` 必须 = 文件名 stem，见 N2）；
3. 若要让实例**默认启用**，还需在 design config 的 `skills`/`eval_points` 里选中（或由 agent `select_component`）。

**无自动注册机制**，也**无一致性守卫**（改完代码忘改注册表 → 静默失效；改完注册表忘改代码 → invalid 但只在 patch 校验时抓，见 §1.5）。

### 1.5 注册表↔代码一致性校验：唯一入口 + 触发条件过窄

- 唯一校验点：`code_repo.registry_report` + `_registry_worsened`（`code_repo.py:283-316`），在 `check_patch`（`:323-344`）与 `apply_code_patch`（`:347-373`）里被调用。
- **触发条件过窄**：仅当 patch 触及 `gan/registries/` 前缀才跑（`_patch_touches_registry`，`code_repo.py:319-320`）。**只改组件代码、不动注册表**的 patch 不做注册表校验。
- 校验语义是"**变差不许**"（新增 unparseable 或新增 invalid 即拒绝），不要求"全量有效"。

### 1.6 其他位置的合法性检验（现状：仅选择时）

| 位置 | 是否校验 | 证据 |
|---|---|---|
| `select_component` 选择时 | **是**（`has` + `is_valid`） | `gan/tools/design/select_component.py:41-45` |
| `deselect_component` | 否（只查是否在 config 列表） | `deselect_component.py:40-43` |
| 装配时（`assembly.py`） | 否（`p=None` / 文件不存在 → 静默跳过） | `assembly.py:59,113` |
| 加载时（`load_tools`） | 否（缺 `tool_info` → 记日志跳过） | `agent/tools/__init__.py:42-44,52-54` |

即：**校验只发生一次（选择那一刻），此后不再复核**——这正是 §1.7 静默降级的根因。

### 1.7 漏洞：选择静默降级（已登记 deficiency，此处补全链路）

`selected_module_paths` 对未注册/invalid 组件 `p=None` 后直接跳过，**无 log、无 event、无错误**（`assembly.py:59`；`loader.py:97-102`）。第二重静默：即使 `module_path` 有值，`assembly.py:113` 的 `if os.path.isfile(p)` 为假也静默跳过。

**跨 outer 分支可达性（补全）**：
1. `unregister_component` 扫描**全部 4 个注册表**（`gan/tools/deep/unregister_component.py:17`），门控只看"该注册表文件是否对本角色可写"（`:58-59`），**不限定"own registry"**。删 `shared.json` 条目 → 波及所有角色。
2. 改动随 session patch 提交进 code_root（`:86-87`），成为该分支代码树的**全局状态**。
3. 每 outer 角色实例重建 → `refresh_tools()` 以 `clear=True` 从 code_root 重新加载并装配（`gan/framework/loop.py:663`、`:320-344`；`gan/roles/base_role.py:49,82-95`）。
4. 代码基线按所选 parent 切换（`loop.py:685,577-598`；`code_repo.py:170-181`）→ 不同分支携带不同注册表状态。
5. 若另一分支已深删被本设计选中的组件 → `module_path` 返 None 或文件已删 → **本实例下一 outer 静默失去该能力**，receipt / diff summary **无 visibility**。

---

## 2. 选配（config）

### 2.1 always-on vs opt-in 的现行边界

- `always_on_dirs`（`assembly.py:37-47`）：**task 返回 `[]`**（无常驻工具）；planner/evaluator 得 `work/<role>` + `work/common` + `design` + `deep`。
- opt-in（`assembly.py:50-67`）：task 用 `skills`；evaluator 用 `eval_points`；**planner 无 opt-in 工作能力**（源码注释 "planner has no opt-in work capabilities yet"）。
- 设计原则（`docs/5_v3改动.md:23`、`docs/7:340`）：always-on 仅用于工具，不用于评估点；`gan/tools/deep/` 强制常驻 → 这就是 `deny_deep` 消融开关必须存在（无法靠"不选组件"关闭）的理由。

### 2.2 漏洞：task 的可选面含 bash/editor（B5）

- `gan/components/task/skills/` **为空**（仅空 `__init__.py`；`task.json` `"components": []`）。
- 但 `shared.json` 注册的 `bash`/`editor` 对**所有角色**合并生效（`loader.py:128`）→ task 可通过 `select_component("skills","bash")` 拿到 bash。
- 后果链（`docs/6` §5.2、`docs/7:142`）：task 子进程内 LLM + 工具同进程，bash 可读 `.env`（API key）、可读 `GAN_DATASET_ROOT` 指向的数据集 → **标签泄漏 + 凭证外传**。
- 现状：`docs/6` §5.5 已明确"**立即以 B5 止血**"，但**未实施**。

**N8（新发现）— task 侧无独立工具预算，且无失败回执**

- task agent 走同一 `chat_with_agent` 且**未传** `max_tool_calls`（`task_agent.py:58-66`）→ 用默认 40（`agent/llm_withtools.py:104`）。`loop.yaml` 只有 `plan_max_tool_calls` / `self_improve_max_tool_calls`（`:14-15`），**无 task 键**。
- task 侧装配/加载对无效组件静默跳过（同 §1.7），而 task 的 design 快照（`ckpt/design/task/<genid>/config.json`）仍记着该组件 → **design 声称有、实际没有**，无任何回执字段暴露这一差异。

---

## 3. 装配（assemble）

（T1/N1/N2 与选择静默降级已见 §1.3、§1.7，此处补产物落点与 git 归属，供"继承"面使用。）

- planner/evaluator 工具集：`<output_dir>/toolsets/<role>/<instance>/`（`base_role.py:78-80`，`instance` 形如 `outer_3`）。
- task skills：`<output_dir>/work/<genid>/repo/.gan_runtime/skills/`（`gan/framework/task_execution.py:71-79`；`GAN_TASK_SKILLS_DIR` 沙箱内路径 `:79`）。
- **装配产物不进 git**：`outputs/` 被 `.gitignore:26` 忽略，且 `code_repo._EXCLUDE_DIRS` 含 `"outputs"`（`code_repo.py:27-28`）；task 的 `work/<genid>/` 是临时目录、run 后删除（`gan/framework/paths.py:32`；删除点 `loop.py:255,790,820`）。

---

## 4. 提示词注入

### 4.1 工具 schema 的生成与注入（现状）

- 生成：**显式 `tool_info()` 字典**，非 docstring、非类型标注。`load_tools` 要求模块同时暴露 `tool_info` + `tool_function`（`agent/tools/__init__.py:42-44`）。
- 注入位置：**每次 user message 前缀**（不是 system prompt）。`llm_withtools.py:119-129` 生成 `system_msg`（变量名叫 system，实际拼进 `msg` → `{"role":"user"}`，`agent/llm.py:95`）；每个工具回合都重新注入（`llm_withtools.py:182-183`）。
- 无 docstring 长度/内容约束；`input_schema.required` 只是文本，**运行时不校验**（见 §5.2）。

### 4.2 漏洞：`set_prompt` 无任何约束（N9 关联）

`gan/tools/design/set_prompt.py:17-23` 全文校验**只有** `ctx is not None`：无长度上限、无必填（`text` 默认 `""`，可写成空串）、无内容检查、无"禁止覆盖框架级注入"的守卫。

**N9（新发现）— `prompt` 有两条写入路径，无唯一性与审计标签**
- `set_prompt(text=...)`（`set_prompt.py:21`）与 `set_config(key="prompt", value=...)`（`set_config.py:29-35`，`prompt` 在三角色 schema 内，`gan/design/schema.py:15-30`）写**同一个 config key**。
- 后果：审计/diff 无法区分"用哪个算子改的 prompt"；R4 白名单方案若只覆盖其中一个算子，另一条会漏。

**自改写风险**：角色 prompt 完全由 agent 可写的 `config["prompt"]` 决定（`base_role.py:97-99,109`）。框架注入项（`task_brief`/`receipt`/`issues`…）是**追加**到 instruction 的（`planner.py:28-79`、`evaluator.py:20-54`），`set_prompt` **删不掉**它们，但可写入"忽略后续注入 / 停止调用工具 / 直接给分"等指令削弱约束。

### 4.3 提示词注入面（枚举，供契约锚定用）

| 角色 | 注入项 | 行号 |
|---|---|---|
| planner | `task_brief`、`receipt`(render)、parents/parent_summary、`evaluator_issues`、`diff_summary`、`parent_predicted/benchmark_score` | `gan/roles/planner.py:28-79` |
| evaluator | `task_brief`、BLIND 文本、`run_summary`、`prev_feedback.digest`、`prev_feedback.issues`、`diff_summary`；reveal 阶段加 `benchmark_score`/`penalties_hint` | `gan/roles/evaluator.py:20-54,56-66` |
| task | design prompt + `task_brief` + inputs + 输出 JSON schema | `task_agent.py:44-56` |
| 所有角色 | `current_prompt()` + `# Task` + instruction | `base_role.py:109` |

---

## 5. 调用

### 5.1 完整链路与错误路径

模型返回（`agent/llm.py:101`）→ 正则提取 `<json>`（`llm_withtools.py:63-79`）→ 分发（`:81-88`）→ 执行 `tools_dict[name]['function'](**tool_input)`（`:84`）→ 回填（`:167-173`）。

| 错误情形 | 处理 | 证据 |
|---|---|---|
| 工具抛异常 | 转 `"Error executing tool ..."` 字符串回填，**不中断** | `llm_withtools.py:87-88` |
| 返回 None / 非字符串 | 无类型检查，f-string 强制 `str()`（None → `"None"`） | `:171` |
| 未知工具名 | 返回 `"Error: Tool '...' not found"` | `:85-86` |
| 参数不合法/缺失 | `required` 不执行；缺参 → `TypeError` → 被 `:87` 兜底成 Error 字符串；多余参数被 `**kwargs` 吞掉 | `:87-88`；`set_prompt.py:17` |
| JSON 块畸形 | `json.JSONDecodeError` → 静默 `continue` | `:77-78` |
| 输出截断（半个工具调用） | 追加 `"Error: Output context exceeded..."` | `:36-61,178-179` |

### 5.2 `max_toolcall` / 轮次上限

- 默认 40（`agent/llm_withtools.py:104`；`base_role.py:106`），self-improve 30（`loop.yaml:14-15`，消费于 `loop.py:83-84`；调用点 `:713,884,894`）。
- **超出行为**：不是报错也不是硬截断，而是"**强制收尾一次**"——追加 `# Tool budget exhausted / Do NOT call any tool` 后再要一次回答，然后 `break`（`llm_withtools.py:137-152`），返回 `{"truncated": True, "tool_calls": n}`（`:197-198`）。
- 对所有角色生效（含 task，见 N8）。**算子预算 `N_op` 暂不做**，靠 `max_toolcall` 兜底（`docs/7:266`、`docs/6:74`）。

### 5.3 角色自我深改（self patch）

- 位置：`loop._apply_self_patch`（`gan/framework/loop.py:373-398`）→ `code_repo.apply_self_patch`（`code_repo.py:376-378`）→ `apply_code_patch`（`:347-373`，allowlist + compile + registry 校验 + commit）。
- 失败：回滚到 `prev` 提交并记 `self_improve_apply_failed`（`loop.py:395-398`）；in-session 重试 `patch_retry_k`（`planner.py:218-238`、`evaluator.py:150-170`）。
- **提示词改动 vs 工具/算子改动走不同路径**：prompt = 浅层 design config，`save_self_config` → `design_store.save`（JSON 文件，**非 git**）；工具/算子逻辑 = 深改 code patch，走 git commit。两者**无统一审计/回滚**。

---

## 6. 返回

**漏洞：工具返回值无统一长度上限或清洗**

- 回填在 `llm_withtools.py:167-173`，`tool_output` **原样**进字符串。
- 仅个别工具自带 char cap：`read_file`（`max_chars=8000`，`gan/tools/work/common/read_file.py:42-43`）、`read_session_trajectory`（6000，`:47,59`）。`grep`/`list_dir` 的 cap **未能确证**（读取被基础设施抖动阻断）。
- **不可信内容原样回填**：`read_file` 读到的文件内容、`bash` 技能输出（`agent/tools/bash.py:139-148`）、`read_session_trajectory` 渲染的轨迹原文（`gan/framework/trajectory.py:117-123`）都会直接进入模型上下文。
- 现有清洗只作用于**归档**而非回填：`trajectory.py:27-46` 的 `_SENSITIVE` 行级替换为 `[REDACTED:sensitive]`，`base_role.py:125-129` 对 session 文件 `redact_file`。**不改变** `process_tool_call` 的即时回填。

---

## 7. 信息传递（planner → evaluator 的脱敏）

### 7.1 现状：`build_diff_summary` 是唯一收敛点，且已失效

- `gan/summary.py:15-43` 只识别**已废弃算子**：`tune_param`/`apply_config`（`:24`）、`set_prompt` 读旧键 `section`（`:26`，record 实无该键 → 恒 `None`）、`set_tool_enabled`（`:28`）、`swap_module`（`:31`）、`add_config`（`:34`）、`code_edit`（`:36`）。这些工具在 `gan/tools` 下**均不存在**。
- 对现行 op 几乎不提取字段：`select_component`/`set_config`/`set_param`/`deselect_component` **全部只留 `{"op": op}`**，`slot/name/key/value` 全丢。
- **无"未知 op 透传"分支**（`summary.py:19-40` 只有 `if/elif`，无 `else`）→ 就本函数而言**不构成泄漏面**，但也**不提供**"未来新算子可见但不可泄漏"的能力（R4 想补的正是这个）。

### 7.2 漏洞（N3）：实际有 4 条未脱敏通道进入 evaluator

| # | 源字段 | 载体 | 注入点 | 是否脱敏 |
|---|---|---|---|---|
| A | `request_source_access.reason`（自由文本）、`set_param.value`（任意） | `records` → `task_runner` `meta["records"]`（`gan/framework/task_runner.py:207`） | `meta_view`（`loop.py:762-763` 只剔 `report_summary`/`delta_vs_parent`）→ `evaluator.py:36` | **否** |
| B | `request_source_access.reason`（自由文本） | `broker.grants[].reason`（`access.py:212`）→ `_source_access_log`（`loop.py:346-355`）→ `receipt["grants"]`（`receipt.py:92`） | `loop.py:774` raw receipt → `evaluator.py:36` | **否** |
| C | `set_param.value` | 同 A | 同 A | **否** |
| D | `respond_issue.feedback`（planner 自述自由文本） | `responses` → `build_feedback_digest`（`evaluator_reward.py:197-200` `said: {fb}`）→ `digest` | `loop.py:1827`/`:776` → `evaluator.py:37-41` | **否** |

**与 `docs/7` 记录的差异**：`docs/6:86` 的 A1 只登记了"`meta.records` / `receipt.grants` 夹带 planner 的 `reason`"（= 通道 A、B）。本轮新发现 **C（`set_param.value`，无 schema 约束，`set_param.py:9-12` `"value": {}`）** 与 **D（digest 里的 planner `feedback` 自由文本）** 同样未经脱敏抵达 evaluator。

### 7.3 漏洞（N4）：同一 receipt 内字段策略不一致

- `receipt._ops_summary` 对**所有** op 用白名单 `("slot","name","key","intent","paths")` 复制（`receipt.py:20`）——**不透传 `reason`**。
- 但 `receipt["grants"]`（`receipt.py:92`）**含 `reason`**。
- 且 `render_receipt`（`receipt.py:98-125`）**不渲染 grants**，而 raw dict 注入（`loop.py:774`）**会**→ 两条消费路径脱敏程度不同。

### 7.4 漏洞：两套并存的 planner 语义

- **planner 自述**：`respond_issue.feedback`（结构化字段，内容为自由文本）→ 通道 D。
- **planner 行为记录**：`records`（含 `reason`/`value`）→ 通道 A/B/C。
- `summary.py:1-6` 的声明"evaluator 不得接收 planner 的自由文本 rationale"**只覆盖 `build_diff_summary` 一条路**，与 A/B/C/D 实际并存 → 宣称与实现不一致。

---

## 8. 继承

### 8.1 现状：三者的持久化机制**不同**

| 资产 | 持久化位置 | 是否在 git 内 |
|---|---|---|
| 工具/组件**代码** | `ckpt/code/`（`code_repo.materialize` 拷贝 `gan/agent/utils/domains/task_agent.py`，`code_repo.py:25`） | **是**（`_git_commit`，`:52-71`；`apply_code_patch` 校验后 commit，`:347-373`） |
| **注册表** `gan/registries/*.json` | 同上（`gan/` 在 `_COPY_TOP`） | **是** |
| **design config**（每 role 的 prompt/skills/eval_points） | `ckpt/design/<role>/config.json`、`ckpt/design/task/<genid>/config.json`（`gan/framework/paths.py:66-67`；`gan/design/store.py:15-18`） | **否**——走 checkpoint 的 `designs` 快照（`gan/framework/checkpoint.py:110-129`） |

### 8.2 漏洞（N5）：§3 要求的"config、注册表、代码均被 git commit 覆盖"**只有后两者成立**

- `ckpt/` 与 `outputs/` 一样**不进 git**（`.gitignore` 未列 `ckpt/`，但 `outputs/` 被忽略且 `code_repo._EXCLUDE_DIRS` 含 `outputs`；`ckpt/` 是 `output_dir` 子目录，随 run 目录一起在 git 之外）。
- design config 的继承靠 **checkpoint 快照**（`snapshot_designs`/`restore_designs`，`checkpoint.py:110-129`），而 `snapshot_designs` **只对已存在 design 文件的 role 生效**（`:111-121`），且**不覆盖 task 的 per-genid design**（`store.path` 对 task 用 `task/<genid>/config.json`）。
- 后果：
  1. design config **无 git 血缘**——无法用 `task_<genid>` 分支式的血缘审计追溯"某代的 prompt/skills 从哪来"；
  2. 恢复路径依赖 checkpoint（G2-lite 只存 `code.commit`，`docs/6` §四 G2），design 与 code 的回退一致性靠 `restore_designs` + `checkout` 两个独立动作拼合，非原子。

### 8.3 关联：两条改动路径的持久化不对等

- 工具/算子代码改动：走 git commit，**可回滚、有血缘**（`apply_code_patch`）。
- 提示词/选配改动：走 JSON 落盘，**仅靠 ckpt 快照**（§8.1）。
- 这使"继承"面在两类改动上强度不同，与 §3 的期望不一致。

---

## 9. 修改建议

按 §3 流程分段给出，标注落点与优先级。

### 9.1 实现与注册（建议新建"注册期统一校验"）

| # | 建议 | 落点 | 优先级 |
|---|---|---|---|
| R-a | **扩展 `entry_reason` → 完整校验**：除现有 4 项外，增加 ①`name` 必须等于 `module` basename（N2 契约）②`module` 可 import 且暴露 `tool_info`/`tool_function` ③`input_schema` 结构合法 | `gan/registries/loader.py:50-63` | 中 |
| R-b | **双向一致性检验**：`registry_report` 增加"孤儿组件"扫描（`gan/components/**` 有文件但无注册条目）→ N6 | `gan/framework/code_repo.py:283-301` | 中 |
| R-c | **放宽校验触发条件**：不再要求 patch 触及 `gan/registries/`，只要 patch 触及 `gan/components/**` 或 `gan/tools/work/**` 也跑注册表一致性校验 | `code_repo.py:319-320` | 中 |
| R-d | **启动期 preflight 扩展**：`preflight()` 增加工具集/注册表校验（对每 role 跑 `load_registry_for_role` + `assemble_tools_dir` 到临时目录 + 校验无同名、无 invalid、无孤儿），失败即 abort | `gan/framework/preflight.py:15-38`；`gan/build.py:137-142` | 中 |
| R-e | **自动注册（可选）**：新增组件时由 `edit_source`/patch 落点自动追加注册条目（或提供 `register_component` 算子），减少手工不一致 | 新增算子 + `frozen.py` 白名单 | 低 |

### 9.2 选配

| # | 建议 | 落点 | 优先级 |
|---|---|---|---|
| S-a | **B5 止血**：`bash`/`editor` 移出 task 可选面（从 `shared.json` 拆出 task 专用注册表，或 `select_component` 对 task 加 kind/name 黑名单） | `gan/registries/shared.json`、`select_component.py:41-45` | **高** |
| S-b | task 侧新增独立工具预算键（如 `loop.task_max_tool_calls`）并在 `task_agent.py` 传入 → N8 | `loop.yaml:14-15`、`task_agent.py:58-66` | 低 |
| S-c | task 的 design 快照与实际装配结果做一致性回执（哪些 skills 实际加载/被跳过） | `task_execution.py:76`、`task_runner.py` | 低 |

### 9.3 装配

| # | 建议 | 落点 | 优先级 |
|---|---|---|---|
| A-a | **同名检测（T1 完整版）**：装配时对目标目录做 basename 唯一性检查，冲突即报错（或加命名空间前缀）。注意按 N1 扩大到**整个目标目录**（含 design/deep 与 opt-in 之间） | `gan/tools/assembly.py:106-114` | 中 |
| A-b | **选择静默降级 → 显式化**：`selected_module_paths` 遇 `p=None` 时记 `events.jsonl` 事件 + 写入 receipt 字段（"design 声称 X，实际未装配"） | `assembly.py:50-67`；`receipt.py` | 中 |

### 9.4 提示词注入 / 调用 / 返回

| # | 建议 | 落点 | 优先级 |
|---|---|---|---|
| P-a | `set_prompt` 增加长度上限与空值校验（与 `set_config` 共用同一写入函数，消除 N9 双写） | `set_prompt.py:17-23`、`set_config.py:29-35` | 低 |
| P-b | 参数按 `input_schema.required` 在 `process_tool_call` 前置校验（缺失/类型不符 → 明确 Error 而非 `TypeError` 兜底） | `agent/llm_withtools.py:81-88` | 低 |
| P-c | 工具返回值统一 cap（在回填处对 `tool_output` 做全局截断，而非依赖各工具自带） | `llm_withtools.py:167-173` | 低 |

### 9.5 信息传递（与 F1/R4/G5 同批）

| # | 建议 | 落点 | 优先级 |
|---|---|---|---|
| I-a | **F1 完整版**：`meta_view` 排除 `records`；`_source_access_log`/receipt 剥离 `reason`（通道 A、B）→ 并**补上本轮新发现的 C、D** | `loop.py:762-763`、`:346-355`、`receipt.py:92`、`evaluator_reward.py:197-200` | **高** |
| I-b | **R4 落地**：删 `summary.py` 死分支 + 改"未知 op 透传 + 安全字段白名单"（`slot/name/key/paths/intent/chars/count`），并**统一 receipt 内两处字段策略**（N4） | `gan/summary.py:15-43`、`receipt.py:13-24,92` | 中 |
| I-c | **G5 结构化加固**：evaluator 可见信息白名单**投影**（覆盖未来新算子的任意 record 字段），作为 A/B/C/D 的统一兜底 | 新增投影层；`loop.py:767-782` | 中 |
| I-d | `set_param.value` 加 schema 约束（或从 records 中只记 key 不记 value）→ 通道 C | `set_param.py:9-12,27` | 中 |
| I-e | **T2 约定落地**：`AGENTS.md` + `seeds/*.md` 写入"新工具必须 `ctx.record(op, **结构化字段)`，不记录 rationale"，并与"检测-回退-回执"机制绑定 | `AGENTS.md`、`gan/design/seeds/*.md` | 中 |

### 9.6 继承

| # | 建议 | 落点 | 优先级 |
|---|---|---|---|
| H-a | **把 design config 纳入 git**（或至少在 checkpoint 里补 task per-genid design 的快照），使 §3 的"config、注册表、代码均被 git 覆盖"成立 → N5 | `gan/design/store.py`、`code_repo.py`、`checkpoint.py:110-129` | **高** |
| H-b | 统一两条改动路径的持久化与回滚（prompt 改动也留 git 或结构化审计标签），消除 §8.3 的不对等 | `base_role.py:143-144`、`loop.py:373-398` | 中 |

---

## 10. 与既有编号的对照

| 既有编号 | 本轮结论 |
|---|---|
| **T1** | 确认；**N1 修正其触发面**（opt-in 可覆盖任意 always-on 目录，不只 `work/common`）→ 修法范围见 A-a |
| **T2** | 确认未落地（`AGENTS.md`/`seeds` 无该约定）→ I-e |
| **R4** | 确认（死分支 + 现行 op 不提取字段 + 无透传）→ I-b；**N4 补充 receipt 内策略不一致** |
| **F1（A1）** | 确认；**N3 补充两条通道**（C `set_param.value`、D `respond_issue.feedback`）→ I-a |
| **G5** | 确认 → I-c |
| **B5** | 确认未实施，且 task 可选面实际来自 `shared.json` → S-a |
| **选择静默降级** | 确认；补全跨 outer 分支可达性链路（§1.7）→ A-b |
| **`source_access.deny_deep`** | 与本轮无关，接线方案见 `docs/7` §3.2.1 |
| **N_op / 算子预算** | 确认"暂不做"，靠 `max_toolcall`；**N8 指出 task 侧无独立键** → S-b |

**本轮新增（docs/7 §3 未登记）**：N1（覆盖面）、N2（名称契约）、N3（通道 C/D）、N4（receipt 策略不一致）、N5（config 未进 git）、N6（孤儿检测缺失）、N7（装配期 preflight 缺失）、N8（task 预算键缺失）、N9（prompt 双写）。

---

## 11. 验证缺口

| # | 建议新增断言 | 对应 |
|---|---|---|
| V-a | 装配后目标目录无同名冲突（构造 `work/common/grep.py` + opt-in `grep.py` → 应报错而非静默覆盖） | A-a |
| V-b | 组件 `name` ≠ module basename 时，注册期即报错（而非装配后静默不加载） | R-a |
| V-c | `meta_view` 不含 `records`；receipt 不含 `reason`；digest 不含 planner 自由文本 `feedback`（三条通道各一断言） | I-a |
| V-d | evaluator 会话实际注入字段枚举断言（F1 扩充的 feedback 契约锚定） | I-a/I-c |
| V-e | `unregister_component` 后，本设计仍选中该组件的实例在下一 outer 产生显式事件/回执（而非静默失去能力） | A-b |
| V-f | design config 的 git 血缘：`task_<genid>` 分支可追溯该代的 prompt/skills（若采纳 H-a） | H-a |
