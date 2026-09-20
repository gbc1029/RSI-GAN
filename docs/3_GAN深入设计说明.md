# RSI-GAN 深入设计说明

> 本文面向实现层，说明三角色自进化框架的设计点与**具体代码定位**（`文件:行`）。
> 设计背景见 `docs/plan.md`，实现过程与问题见 `docs/GAN实现记录.md`，环境见 `README.md` / `docs/部署记录.md`。
> 基础代码为 HyperAgents(DGM-H)，GAN 框架位于 `gan/`。

---

## v2 设计修订（本版）

在 v1 基础上按讨论结论做了四处修订（代码已同步）：

1. **版本管理升级为 DAG**（支持“交叉”算子，两个父本→一个子代）
   - `gan/tree/store.py`：`Node.parents: List`（`parent_genid` 保留兼容，`__post_init__` 双向归一）；`add_node` 对每个父代记 `children`、`depth=max(parents)+1`；新增 `select_parents(k)`（`k>1` 供交叉）。
   - 交叉**只作用于浅层设计**（config 合并），源码不做三路合并。

2. **evaluator 反馈文字化**（去掉 2×2 的数值权重与标量奖励）
   - `gan/reward/evaluator_reward.py`：`classify_issue` 仅产出**诊断标签**；新增 `build_feedback_digest(...)` 产出**纯文本** digest（逐条 issue→planner 回应→修复判定+证据，以及盲评偏差的文字说明）。
   - `gan/loop.py`：`_settle_feedback_digest` 落盘 `runs/<genid>/feedback_digest.md`，并传给 evaluator（`prev_feedback["digest"]`）与 `self_improve(recent={"digests":...})`。
   - `gan/config/gan_loop.yaml`：删除 `evaluator_reward.matrix` 等数值项；`feedback.mode=text_digest`。

3. **“配置化”= agent 的浅层设计（单一 config JSON），非框架配置**
   - 新增 `gan/design/`：`schema.py`（各角色**极小** schema：task={prompt,skills,memory,params}、planner={prompt,memory,params}、evaluator={prompt,eval_points,memory,params}）、`store.py`（`design/<role|task-node>/config.json`）、`registry.py`（分角色 `registries/<role>_registry.json` + `components/` 代码）、`composer.py`、`context.py`（`DesignContext`）。
   - **浅层边界=代码定义的 schema**：只能改**已有键的值** + 从注册表**选择组件**；**新增配置键/新增或修改组件实现 = 源码级（深）**。
   - `gan/config/gan_loop.yaml` 保留为**隐藏超参**（进化不可见）。
   - task agent 变为**设计驱动**：`task_agent.py` 从 `GAN_TASK_DESIGN` 读取 config（prompt+skills），技能由 `GAN_TASK_SKILLS_DIR` 加载。

4. **算子分层：工作 tools vs 设计算子**
   - `gan/operators/work_tools/{planner,evaluator}/`：工作能力（planner 的 `respond_issue`；evaluator 的 `report_issue`/`record_predicted_score`/`judge_fix`/`eval_*` **逐项评分与检查**）。
   - `gan/operators/design_ops/`：**浅层**设计修改算子——`set_prompt`/`set_config`（仅已有键）/`select_component`（仅已注册）/`set_param`/`mint_operator`（组合式）；以及**深层**逃生口 `code_edit`（门控）。
   - 明确：evaluator 的“逐项细则评分”是**工作 tools**；“新增评分项/新组件”才是**深层**（`code_edit`）。

## v3 目录规划（本版落地）

按「性质 × 可变性」重组了配置/算子/tools 的文件管理：

| 概念 | 目录 | 可变性 | 说明 |
|---|---|---|---|
| framework | `gan/framework/` | 冻结 | `loop.yaml`(超参)、`domains.yaml`(域注册表)、`loader.py` |
| context | `gan/framework/context.py` | 冻结 | 统一 Plan/Eval/Design/Access 四个 contextvar |
| tools（**总是启用**） | `gan/tools/` | 深改 | `work/{planner,evaluator}/`、`design/`(设计算子)、`deep/`(深度门控)、`assembly.py` |
| components（**可选启用**） | `gan/components/` | 深改 | `shared/{skills,memory}/`、`task/skills/`、`evaluator/eval_points/` |
| registries | `gan/registries/` | 深改 | `shared.json` + `<role>.json` + `loader.py` |
| design（**浅层可进化**） | `gan/design/` | **浅** | `schema.py`/`store.py`/`composer.py`/`seeds/` |

**归属规则（一句话）**：`总是启用→tools`、`可选启用→components`；`tools` 内按功能分 `work/design/deep`；`components` 内按通用性分 `shared/<role>`；注册表登记可选组件，设计 config 选择。

**要点**：
- **基础手** `agent/tools/`（bash/editor）保持不动（框架基础）；`gan/components/shared/skills/` 以 re-export 方式将其登记为**通用可选技能**。
- **task agent 无常驻工具**：能力全部是 `components`（skills），空种子起步，由设计 config 选择；不足时经 `tools/deep/request_source_access` 深层新增。
- **evaluator 评估点拆两半**：核心 `report_issue/record_predicted_score/judge_fix` 在 `tools/work/evaluator`（总是）；可选 `trajectory_quality/hard_failure/reward_hacking/rule_violation` 在 `components/evaluator/eval_points`（注册+选择）。
- **`common` 与 `code_edit` 合并**为唯一深度算子 `tools/deep/request_source_access.py`（view/modify + 记录）。
- `eval_points.yaml` 与 doc-only `*.md` 已删除；评估点为**单一来源**（registry + 实现）。
- schema 的 `operators` 槽与 `mint_operator` **已删除**（见 v4：工具增删改 = 浅层 `select/deselect_component` + 深层源码编辑）。
- **访问边界（v4 改为按角色 allowlist）**：`gan/framework/frozen.py` 声明各角色可读/可写的路径白名单（task 无权限；planner 读写 t∪p；evaluator 读 t、读写 e），**默认其余全部冻结**；`AccessBroker` 按 `(role,intent)` 判定并拒绝 repo 根/自包含/超限授权。共享运行时（`agent/llm*.py`、`agent/base_agent.py`、`gan/framework/*`、`domains/{harness,report}.py`）与 plumbing 工具（`gan/tools/{design,deep,work/common}`）不在任何白名单内，因此不可修改；拒绝原因仅审计不外泄。

> 下文 §0–§7 保留 v1 的框架性描述；目录与实现以本节、`v2` 节与代码为准。

---

## 0. 代码地图

| 模块 | 文件 | 职责 |
|---|---|---|
| 双循环调度 | `gan/loop.py` | 内外循环、节点提交、奖励结算 |
| 三种角色 | `gan/roles/base_role.py` `planner.py` `evaluator.py` | 会话、工具集装配、plan/evaluate/self_improve |
| 会话上下文 | `gan/operators/context.py` | `PlanContext` / `EvalContext`（contextvar） |
| 算子与评估点 | `gan/operators/{common,planner_ops,evaluator_ops}/*.py` | 可被 LLM 调用的工具 |
| 算子注册 | `gan/operators/registry.py` | 目录装配与加载 |
| 版本树 | `gan/tree/store.py` | `Node`/`NodeValue`/`TreeStore` |
| 源码门控 | `gan/access.py` | `AccessBroker`（工作区、grant、审计） |
| 奖励 | `gan/reward/packet.py` `evaluator_reward.py` | RewardPacket、2×2 矩阵 + 校准 |
| 配置 | `gan/config/{gan_loop,registry,eval_points}.yaml`、`loader.py`、`prompts/*.md` | 配置体系与提示词 |
| 任务执行 | `gan/task_runner.py` | 跑 `domains.harness`+`domains.report` |
| 隔离辅助 | `gan/summary.py` `gan/patch.py` | diff 摘要脱敏、补丁生成/应用 |
| 装配入口 | `gan/build.py`、`scripts/run_gan.py` | 工厂 + CLI |
| 基础（复用/改造） | `agent/llm.py` `agent/llm_withtools.py` `agent/tools/__init__.py` `domains/harness.py` `utils/domain_utils.py` | 见 §2、§6 |

---

## 1. 内外循环流程（含每一步传递的具体内容）

### 1.1 三个角色

- **Task agent**：`task_agent.py`。被进化的对象；`domains/harness.py:26 run_agent` 逐题实例化并 `forward()`。
- **Planner**：`gan/roles/planner.py:15`。产出对 task agent 的修改（配置/算子/门控代码）。
- **Evaluator**：`gan/roles/evaluator.py:13`。盲评→揭示 benchmark→深入评估，产出 `RewardPacket` 与问题清单。

### 1.2 总览

```
GanLoop.run()  (gan/loop.py:149)
├─ 外层 for outer in 1..G           # 元层“一代”
│   ├─ 内层 for inner in 1..I_max   # task agent“一代”
│   │   ├─ select_parent            # §3.3
│   │   ├─ planner.plan(...)        # 1.3
│   │   ├─ task_runner(...)         # 1.3
│   │   ├─ evaluator.evaluate(...)  # 1.3
│   │   ├─ 建包 + 结算 2×2 奖励     # 1.3 / §5
│   │   ├─ commit 节点 + 停滞判定   # §3
│   │   └─ 组装下一轮反馈           # 1.3
│   └─ evaluator.self_improve / planner.self_improve + 元层树推进  # 1.4
```

### 1.3 内循环逐步传递的内容

以下 `(…)` 为“传入”，`→` 为“传出”，均标注代码位置。

**(0) 选父代** `gan/loop.py` 内
- 传入：`branch_limit, depth_limit, method, ucb_c, depth_penalty, cost_penalty`（来自 `gan_loop.yaml: tree.*`）。
- 调用 `TreeStore.select_parent`（`gan/tree/store.py:163`）→ 传出：`Node`（父代）。

**(1) 构造父代配置** `gan/loop.py:93 _parent_config`
- 取 `parent.meta["config_dict"]`（血统快照）→ `Config`；若无则 `_initial_task_config()`（`gan/loop.py:85`，含 `models.task`）。
- 传出：`Config`（**该节点的可变配置**，planner 将对其增改）。

**(2) 规划** `gan/roles/planner.py:50 plan`
- 传入：
  - `parent_summary` = `{genid, depth, children, score, scores, modify_depth}`（`gan/loop.py:75`）
  - `last_feedback`（上一轮 `make_feedback` 结果，见 (7)）
  - `evaluator_issues` = `last_feedback["issues"]`（上一轮 evaluator 提出的问题）
  - `config`（步骤 (1) 的 Config）、`node_id`（新 genid）、`broker`（`AccessBroker`，可为 None）
- 内部：`set_plan_context(PlanContext)` + `set_access_context(broker,"planner",node_id)`，然后 `Role.run(instruction)`（`gan/roles/base_role.py:40`）。
- prompt（`gan/roles/planner.py:20 _plan_instruction`）包含：任务说明 + `## Parent node summary` + `## Evaluator issues to respond to`（要求逐条 `respond_issue`）+ `## Diff summary`（上轮变更，已脱敏）+ “优先配置/算子，必要时 `request_source_access`/`code_edit`”。
- 传出：`{records, responses, config, patch}`：
  - `records`：算子调用记录（`PlanContext.record`）
  - `responses`：对每条问题的 `{issue_id, accepted, feedback}`（`respond_issue` 算子写入 `PlanContext.add_response`）
  - `config`：被修改后的 `Config`
  - `patch`：若调用了 `code_edit`，由 `build_patch_from_workspace`（`gan/patch.py:32`）生成的工作区 diff

**(3) 执行任务** `gan/task_runner.py:84 DomainTaskRunner.__call__`
- 传入：`plan={records,config,patch}`、`parent`、`genid`。
- 行为：
  - 保存配置快照到 `nodes/<genid>/config.json`
  - 任务模型：`_resolve_model`（`gan/task_runner.py:67`）读 `config["models.task"]`→写环境变量 `GAN_TASK_MODEL`
  - 若有 `patch`：复制仓库副本到 `nodes/<genid>/repo` 并 `apply_patch`（`gan/patch.py:62`）
  - 子进程运行 `python -m domains.harness --domain <d> --run_id gan_<genid> --subset <s> --num_samples <n>`，再 `python -m domains.report`
  - `domains/harness.py:80` 用 `GAN_TASK_MODEL` 覆盖模型
  - 读 `outputs/gan_<genid>/report.json[score_key]`（`score_key` 由 `registry.yaml` 解析）
- 传出：`Node`（含 `value.score`、`scores{domain:score}`、`modify_depth`（`gan/task_runner.py:33`）、`meta={report_path,delta_vs_parent,records,harness_rc,...}`）。

**(4) 评估** `gan/roles/evaluator.py:59 evaluate`
- 传入：`run_summary`、`prev_feedback`（上一轮 `last_feedback`）、`benchmark_score`（=`child.value.score`）、`node_id`、`broker`。
- 两阶段（同一次会话，`msg_history` 传递）：
  - **盲评** `_blind_instruction`（`:18`）：传入 `run_summary`（轨迹/结果摘要）+ 上一轮 issues（要求 `judge_fix`）+ 上轮 diff 摘要；**不含 benchmark 分**。评估者用 `record_predicted_score` 给出盲评预测。
  - **揭示** `_reveal_instruction`（`:46`）：传入 `benchmark_score`；深入评估，跑 `eval_*` 评估点。
- 传出：`EvalContext`，字段见 `gan/operators/context.py:54`：
  `predicted_score`、`issues`、`fix_verdicts`、`penalties`、`eval_point_results`、`summary/weaknesses/suggestions`。

**(5) 建包** `gan/loop.py:97 _build_packet`
- 传出：`RewardPacket`（`gan/reward/packet.py:17`）：
  `numeric={benchmark, delta_vs_parent, cost_tokens, cost_wallclock_s}`、
  `textual={summary, weaknesses, suggestions}`、`penalties`。
- 落盘 `runs/<genid>/packet.json`；`child.value.potential = ctx.predicted_score`。

**(6) 提交节点**：`task_tree.add_node(child)`（`gan/tree/store.py:111`）——维护父代 `children` 与子节点 `depth`。

**(7) 结算 evaluator 奖励（上一轮）** `gan/loop.py:120 _settle_evaluator_reward`
- 传入：上一轮 `issues`、**本轮** planner 的 `responses`、**本轮** evaluator 的 `fix_verdicts`、上一轮 `predicted_score`/`benchmark`。
- 计算：`compute_evaluator_reward`（`gan/reward/evaluator_reward.py:233`），配置取 `gan_loop.yaml: evaluator_reward.*`。
- 传出并落盘 `runs/<genid>/evaluator_reward.json`，并记 `events.jsonl`（类型 `evaluator_reward`）。详见 §5。

**(8) 停滞与 advantage**
- `improved = cs is not None and (ps is None or cs-ps > ε)`；`no_improve` 累加；两者均有分时 `_advantages.append(cs-ps)`。
- 组下一轮反馈 `_last_feedback = make_feedback(issues, diff_summary, penalties)`（`gan/summary.py:46`）；`diff_summary` 由 `build_diff_summary`（`gan/summary.py:15`）从本轮 `records` 生成（**剥离 rationale/reason**）。
- 更新 `_prev_predicted/_prev_benchmark` 供下一轮结算。
- `no_improve ≥ patience` → `break`（记 `stagnation_break`）。

### 1.4 外循环传递的内容
`gan/loop.py`（`run()` 末尾）
- `evaluator.self_improve(recent={evaluator_rewards: 最近 I_max 条, last_feedback})`（`gan/roles/evaluator.py:85`）：设 `EvalContext` 后 `Role.run`，`max_tool_calls=8`，返回 `{issues, eval_points}`。
- `planner.self_improve(recent={advantages: 最近 I_max 条, task_tree_size})`（`gan/roles/planner.py:86`）：设 `PlanContext(self.load_self_config())`，运行后 `save_self_config`（`gan/roles/base_role.py:74`）→ 落盘 `<role>_self_config.json`。
- 元层树推进：`evaluator_tree.add_node(Node(genid="eval_<outer>", meta={rewards}))`、`planner_tree.add_node(Node(genid="plan_<outer>", meta={advantages}))`。

---

## 2. 配置化与算子化的具体实现

### 2.1 配置体系
- **文件**：`gan/config/gan_loop.yaml`（循环/树/预算/门控/成本/奖励/反馈/模型）、`gan/config/registry.yaml`（域注册表）、`gan/config/eval_points.yaml`（评估点）、`gan/config/prompts/{planner,evaluator}.md`（提示词即配置）。
- **加载**：`gan/config/loader.py`
  - `Config`（`:36`）：点路径 `get/set`（`:50/:58`）、`merge` 深合并（`:65`）、配置池 `custom()/add_custom()`（`:72/:75`）。
  - 顶层加载：`load_gan_loop_config`（`:87`）、`load_registry`（`:92`）、`load_eval_points`（`:96`）、`load_prompt`（`:100`）。
  - 域解析：`resolve_domain`（`:109`）按 **精确 domains > 最长前缀 families > default** 合并。
  - 可选校验：`validate`（`:142`，有 jsonschema 才生效）。
- **域配置迁移**：`utils/domain_utils.py` 改为**注册表优先 + legacy 回退**（`_load_registry:55`、`_field:66`、`_LEGACY:9`），函数签名不变（`get_domain_score_key:80` 等）。
- **可配置点注入任务模型**：`gan/task_runner.py:67` → 环境变量 `GAN_TASK_MODEL` → `domains/harness.py:80`。

### 2.2 算子化
- **算子即工具**：工具发现沿用 `agent/tools/__init__.py:7 load_tools(..., tools_dir=)`（自定义目录用 `spec_from_file_location` 加载，`:31`）。
- **注册与装配**：`gan/operators/registry.py`
  - `default_dirs(role)`（`:22`）：planner → `operators/planner_ops` + `operators/common`；evaluator → `evaluator_ops` + `common`。
  - `assemble_tools_dir`（`:28`）：把 shared+role 的 `*.py` 复制到会话目录 `<output_dir>/toolsets/<role>/`。
  - `load_operators`（`:50`）。
- **约定**：每个算子模块暴露 `tool_info()` + `tool_function(**kwargs)`（并给出 `op_info/op_function` 别名）。示例见 `gan/operators/planner_ops/tune_param.py`。
- **上下文（无参调用）**：工具函数由 `chat_with_agent` 的 `process_tool_call` 以 `**tool_input` 调用，故状态经 contextvar 传递：
  - `PlanContext`（`gan/operators/context.py:19`）：`config` + `records` + `responses`；`set/get/reset_plan_context`（`:39/:43/:47`）。
  - `EvalContext`（`:54`）：`predicted_score/issues/fix_verdicts/penalties/eval_point_results/summary...`；`set/get/reset_eval_context`（`:77/:81/:85`）。
- **planner 算子（8 个）**：`set_prompt` / `tune_param` / `apply_config` / `add_config` / `set_tool_enabled` / `swap_module` / `code_edit` / `respond_issue`（目录 `gan/operators/planner_ops/`）。
- **evaluator 评估点（7 个）**：`report_issue` / `record_predicted_score` / `judge_fix` / `eval_trajectory_quality` / `eval_hard_failure` / `eval_reward_hacking` / `eval_rule_violation`（目录 `gan/operators/evaluator_ops/`）。
- **共享工具**：`gan/operators/common/request_source_access.py`（门控，见 §4）。
- **避免“算子=配置修改”重复**：纯参数改动统一实现为“写配置”的薄封装（`tune_param`/`apply_config`/`add_config` 均写 `Config`）；`swap_module` 为结构性记录；只有 `code_edit` 触碰源码（且走门控）。

---

## 3. 版本管理：存储与母本选择

### 3.1 存储
- **数据结构**：`gan/tree/store.py`
  - `NodeValue`（`:19`）：`score`（benchmark）、`potential`（evaluator 盲评分，仅破平局/入上下文）、`cost_tokens/cost_wallclock_s`。
  - `Node`（`:30`）：`genid, parent_genid, planner_genid, evaluator_genid, prev/curr_patch_files, valid_parent, run_full_eval, depth, children, status, value, scores, modify_depth, source_access_log, meta`。
  - `TreeStore`（`:66`）：`load`（`:75` 事件回放）、`_apply`（`:87`）、`_append`（`:105`）、`add_node`（`:111`，维护父代 `children`/子 `depth`）、`update`（`:126`）、`mark_stagnant`（`:140`）、`get/__len__`（`:143/:146`）。
- **落盘格式（追加式 JSONL 事件日志）**：
  - `task_tree.jsonl` / `planner_tree.jsonl` / `evaluator_tree.jsonl`：每行 `{"op":"add","node":{...}}` 或 `{"op":"update","genid":...,"fields":{...}}`。
  - `events.jsonl`（`gan/loop.py:71 log_event`）：`init/outer_start/inner_done/evaluator_reward/stagnation_break/self_improve_error/outer_done`。
  - 每次运行：`runs/<genid>/packet.json`、`runs/<genid>/evaluator_reward.json`、`nodes/<genid>/config.json`。
- **与 DGM-H 兼容**：`import_from_dgm_archive`（`:194`）可从 DGM-H 的 `archive.jsonl` 引导 task 树；`utils/gl_utils.py` 的 archive/score 读取仍可用。

### 3.2 节点价值
- `score`：来自 `report.json`（确定性）。
- `potential`：evaluator 盲评分（`child.value.potential = ctx.predicted_score`），**不进入选择公式**（防对抗污染）。
- `cost`：token/墙钟（当前未接入 usage，字段预留）。

### 3.3 母本（父代）选择
- 候选过滤 `_candidates`（`:150`）：`valid_parent` 且 `status=="alive"` 且 `children < branch_limit` 且 `depth < depth_limit`。
- `select_parent`（`:163`）方法：
  - `latest` / `best`（按 score）。
  - `ucb`（默认）：`metric = score + c·sqrt(ln(N+1)/visits) − depth_penalty·depth − cost_penalty·cost_norm`，`visits = children+1`（`:163-192`）。
- 参数来源：`gan_loop.yaml: tree.{branch_limit,depth_limit,selection,ucb_c,depth_penalty,cost_penalty}`。
- 停滞：`no_improve ≥ stagnation_patience` → `break`（`gan/loop.py`），并可 `mark_stagnant` 禁选。

### 3.4 血统
- 配置血统：`child.meta["config_dict"]` 由父代 + 本轮 planner 修改得到；`_parent_config`（`gan/loop.py:93`）沿树回溯。
- 补丁血统：`prev_patch_files/curr_patch_files`（与 DGM-H 的 `apply_diffs_container` 机制兼容）。
- 元层版本：`planner_genid/evaluator_genid`（字段预留）；`planner_tree/evaluator_tree` 每外层一节点。

---

## 4. 隔离的具体实现

隔离分四层：**修改深度门控**、**工作区隔离**、**信息隔离**、**审计**。

### 4.1 修改深度与源码门控
- 默认面（无需声明）：**配置 + 算子**（`PlanContext.config` + planner_ops）。
- 源码面（需声明）：调用 `request_source_access(paths,intent,reason)` 或 `code_edit(paths,reason)`。
- 实现 `gan/access.py`：
  - `AccessBroker`（`:43`）：`workspace`（`:63`）、`src_dir`（`:68`）、`_safe_join`（`:74`，拒 `..`）、`_is_denied`（`:81`）、`grant`（`:85`，把请求路径**复制**到 `<workspace>/src/`）、`granted_paths`（`:133`）、`log_event`（`:140`）。
  - 关键点：**授权前工作区无源码文件** → 普通 `bash` 也读不到（真隔离，而非提示词约定）。
  - 工具：`gan/operators/common/request_source_access.py`（`intent=view|modify`）；`gan/operators/planner_ops/code_edit.py`（`intent=modify` 并记录以便生成补丁）。
  - 补丁回流：`build_patch_from_workspace`（`gan/patch.py:32`）对“工作区副本 vs 源仓库”做 difflib 统一 diff；`DomainTaskRunner` 在副本上 `apply_patch`（`gan/patch.py:62`）。

### 4.2 工作区隔离
- `AccessBroker.workspaces_dir = <output_dir>/workspaces`，按 `<role>/<node_id>` 分目录。
- 角色工具集目录 `<output_dir>/toolsets/<role>/`（`gan/operators/registry.py:28`）。
- 每次会话前 `set_access_context(broker, role, node_id)`（planner: `gan/roles/planner.py`；evaluator: `gan/roles/evaluator.py`）。

### 4.3 信息隔离（planner ↔ evaluator 互不可见）
- 角色间只交换结构化反馈，**不共享配置/源码**。
- **给 evaluator 的 planner 变更**是**脱敏摘要**：`build_diff_summary`（`gan/summary.py:15`）只保留算子清单与文件列表，**剥离 `rationale`（`add_config`）与 `reason`（`code_edit`）**（对应实现：`:38` 与 `:41`）。
- evaluator 在**默认**可见：轨迹摘要（`run_summary`）、上一轮 issues、diff 摘要；**源码**仅在 `reward_hacking/rule_violation` 等评估点需要时经门控申请。
- **反馈 schema 版本**：`make_feedback`（`gan/summary.py:46`）/`validate_feedback`（`:60`）；planner 侧 `plan()` 校验（`gan/roles/planner.py:60` 之前）不兼容则抛错，保证两角色可独立演进。

### 4.4 审计
- 每次 grant 记 `events.jsonl`（`type="source_access_grant"`，含 role/node/paths/intent/reason/ts）。
- 节点 `modify_depth`（0=配置 / 1=算子 / 2=源码，`gan/task_runner.py:33`）与 `source_access_log`（字段）可供价值判断与 planner advantage 参考。

### 4.5 当前边界（如实）
- 隔离在 **host 目录级**；容器 `mount` 边界尚未启用（基础代码的容器化见 `utils/docker_utils.py`，后续可把角色放进独立容器）。
- `bash` 在工作区内可写“已授权的 `src/`”，属合法；未授权文件不在工作区，故不可达。

---

## 5. evaluator 奖励（2×2 + 校准）实现

- **数据结构**（`gan/reward/evaluator_reward.py`）：`EvaluatorIssue`（`:44`）、`PlannerResponse`（`:53`）、`FixVerdict`（`:60`）、`IssueOutcome`（`:73`）、`CalibrationRecord`（`:95`）、`EvaluatorReward`（`:112`）。
- **2×2 判定** `classify_issue`（`:160`）：
  - `accepted & fixed → accepted_fixed (+2)`；`accepted & !fixed → accepted_unfixed (+1)`；
  - `!accepted & feedback → rejected_with_feedback (−1)`；`!accepted & no feedback → rejected_no_feedback (+2)`。
- **检查步骤** `run_check_step`（`:192`）：缺失 planner 回应视为“无反馈地忽略”。
- **校准** `compute_calibration_reward`（`:222`）：`−error_scale·|predicted−benchmark|`。
- **聚合** `compute_evaluator_reward`（`:233`）：issue + 校准 + 误报惩罚。
- **回流给 evaluator** `render_feedback`（`:295`）。
- **时序**：第 t 轮 issues/predicted → 第 t+1 轮 planner `responses` 与 evaluator `fix_verdicts` → `_settle_evaluator_reward`（`gan/loop.py:120`）结算第 t 轮。
- 配置：`gan_loop.yaml: evaluator_reward.{matrix,calibration,penalty}`。

---

## 6. 基础代码的改造点（最小）

| 文件 | 改造 | 位置 |
|---|---|---|
| `agent/llm.py` | 模型 env 覆盖 `GAN_MODEL_DEFAULT`；usage 成本钩子 `USAGE_HOOKS/register_usage_hook/_run_usage_hooks`；backoff 增补 `RateLimitError` | `:30`、`:35-49`、`:57-62`、`:114` |
| `agent/llm_withtools.py` | `chat_with_agent(..., tools_dir=)` 透传自定义工具目录 | `:91`、`:99`、`:109` |
| `agent/tools/__init__.py` | `load_tools(..., tools_dir=)` 支持自定义目录 | `:7`、`:17`、`:31` |
| `domains/harness.py` | 任务模型可由 `GAN_TASK_MODEL` 注入 | `:80` |
| `utils/domain_utils.py` | 域配置改为读 `registry.yaml`（legacy 回退） | `:55`、`:66` |
| `gan/roles/base_role.py` | 实现 `forward()`（满足 `AgentSystem` ABC）；自配置读写 | `:59`、`:63` |

---

## 7. 一次运行的产物与自查

运行：`python scripts/run_gan.py --task-domain paper_review --subset _filtered_100_train --num_samples 2 --outer 1 --inner 2`

产物：`task_tree.jsonl`（`initial→0→1`，含 `config_dict` 血统）、`planner_tree.jsonl`/`evaluator_tree.jsonl`、`runs/<genid>/packet.json`、`runs/<genid>/evaluator_reward.json`、`nodes/<genid>/config.json`、`events.jsonl`；子进程评测见 `outputs/gan_<genid>/report.json`。

自查清单：
- [ ] `events.jsonl` 是否含 `outer_start/inner_done/evaluator_reward/outer_done`
- [ ] `evaluator_reward.json` 的 `outcomes[].cell` 是否覆盖预期 2×2 语义
- [ ] `task_tree.jsonl` 的 `parent_genid`/`depth`/`children` 是否符合约束（分叉≤`branch_limit`、深度≤`depth_limit`）
- [ ] `nodes/<genid>/config.json` 是否体现本轮配置变更（血统）
- [ ] `workspaces/` 中授权前是否**无** `src/`；`events.jsonl` 是否有 `source_access_grant`
