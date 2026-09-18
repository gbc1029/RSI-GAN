# GAN 三角色自进化框架 —— 实现记录

> 记录实现过程中的文件改动、作用、遇到的问题与解决。
> 关联文件：`plan.md`（设计与实现方案）、`部署记录.md`（HyperAgents 部署）。
> 代码位置：WSL `/root/HyperAgents`（Windows 路径 `\\wsl$\Ubuntu\root\HyperAgents`）。
>
> **布局变更（后加）**：冻结物已统一迁入 `gan/framework/`：
> `gan/loop.py`→`gan/framework/loop.py`、`gan/context.py`→`gan/framework/context.py`、
> `gan/access.py`→`gan/framework/access.py`、`gan/tree/`→`gan/framework/tree/`、
> `gan/task_runner.py`→`gan/framework/task_runner.py`、`gan/reward/`→`gan/framework/reward/`。
> 本记录下方出现的旧路径为**迁移前**的历史快照；现行布局见 `GAN设计决策.md` 与 `AGENTS.md`。

---

## 一、阶段总览

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 配置层、RewardPacket、evaluator 奖励（2×2 + 校准）、三棵树存储、llm/tools/domain_utils 改造 | ✅ 完成（冒烟全绿 + 上游回归通过） |
| P1 | 三角色 Role、算子注册表、源码门控 access.py、双循环 loop.py | ✅ 完成（冒烟全绿 + 上游回归通过） |
| P2 | planner 算子 v0 + evaluator 评估点 v0 | ✅ 已随 P1 落地（见 §6） |
| P3 | 门控硬化/容器隔离/摘要脱敏 | ✅ 摘要脱敏+schema 校验完成；容器 mount 隔离待增强（见 §7.6） |
| P4 | 真实 TaskRunner（paper_review） + 工厂/CLI | ✅ 代码完成且**已跑最小端到端**（1外×2内，见 §八） |
| P5 | planner advantage + evaluator 外循环奖励接入 self_improve | ✅ 代码完成并冒烟（检查步骤跨轮结算、三树版本化） |

---

## 二、P0 文件清单

### 2.1 新增文件

| 文件 | 作用 |
|---|---|
| `gan/__init__.py` | 包声明与子包导出 |
| `gan/config/__init__.py` | 配置层 API 导出 |
| `gan/config/gan_loop.yaml` | 双循环参数（`outer_generations`/`inner_max`/`stagnation_patience`）、树约束（`branch_limit`/`depth_limit`/`selection`/`ucb_c`）、算子预算（`max_ops_per_gen`/`max_code_edits_per_gen`）、源码门控（`source_access`）、成本、**evaluator 奖励（2×2 矩阵 + 校准 + 误报惩罚）**、反馈 schema、三角色模型 |
| `gan/config/registry.yaml` | 域注册表：从 `utils/domain_utils.py` 的 if/else 迁移而来（score_key/splits/ensembled/子集/stagedeval/potential）。解析顺序：精确 `domains` > 最长前缀 `families` > `default` |
| `gan/config/eval_points.yaml` | evaluator 评估点注册表：`benchmark_score`/`trajectory_quality`/`hard_failure`/`reward_hacking`/`rule_violation`（后两者 `requires_source_access: true`） |
| `gan/config/prompts/planner.md` | planner 提示词：默认只改配置+算子、源码需声明、对 evaluator 问题逐条回应、预算与自进化 |
| `gan/config/prompts/evaluator.md` | evaluator 提示词：定位（找 benchmark 看不到的问题）、**盲评协议**、问题清单格式、上一轮反馈（2×2 落点） |
| `gan/config/loader.py` | YAML 加载、深合并、`Config`（点路径 get/set/merge/`custom()` 配置池）、域解析、prompt 加载、可选 jsonschema 校验 |
| `gan/reward/__init__.py` | 奖励层 API 导出 |
| `gan/reward/packet.py` | `RewardPacket` 数据类（numeric/textual/penalties/schema_version/evaluator_reward），含序列化/落盘 |
| `gan/reward/evaluator_reward.py` | **evaluator 外循环奖励核心**：`classify_issue`（2×2）、`run_check_step`（接纳/修复判定，缺失回应→`rejected_no_feedback`）、`compute_calibration_reward`、`compute_evaluator_reward`、`render_feedback` |
| `gan/tree/__init__.py` | 树层 API 导出 |
| `gan/tree/store.py` | `TreeStore`：三棵树 JSONL 事件回放、`Node`/`NodeValue`、分叉/深度/停滞约束、UCB 父代选择、从 DGM archive 导入 |
| `scripts/smoke_gan.py` | P0 冒烟测试（配置/注册表/reward/树/llm 钩子/自定义工具目录） |

### 2.2 改动文件
 
| 文件 | 原功能 | 改动内容 | 原因 |
|---|---|---|---|
| `agent/llm.py` | litellm 封装；模型常量硬编码；backoff 仅捕 RequestException/JSONDecodeError/KeyError | ①新增 `GAN_MODEL_DEFAULT` 环境覆盖 `OPENAI_MODEL`；②新增 `USAGE_HOOKS`/`register_usage_hook`/`_run_usage_hooks`（每次调用后回调 usage 供成本核算）；③backoff 增补 `litellm.exceptions.RateLimitError` | 三角色用 glm；成本进 RewardPacket；实测 Zhipu 429 限流需重试 |
| `agent/tools/__init__.py` | `load_tools` 仅扫描 `agent/tools/` | 新增 `tools_dir` 参数：自定义目录时用 `spec_from_file_location` 加载并临时加入 `sys.path`；默认行为不变 | 角色需装载自己的算子/门控工具集 |
| `utils/domain_utils.py` | 域配置全 if/else | 改为优先读 `gan/config/registry.yaml`，失败回退内置 legacy 值；函数签名与返回值不变 | 配置化；零回归风险 |
| `plan.md` | v1.1 evaluator 奖励为占位 | 替换为 v1.2 定稿（采纳/修复 2×2 + 盲评校准协议）并更新风险/P5 | 采纳你给出的 evaluator 奖励设计 |

---

## 三、evaluator 奖励设计落地说明

### 3.1 2×2 矩阵（`classify_issue`）

| 接纳 \ 修复 | 已修复 | 未修复 |
|---|---|---|
| **接纳** | `accepted_fixed` (+2.0) | `accepted_unfixed` (+1.0) |
| **不接纳 + planner 有反馈** | `rejected_with_feedback` (−1.0) | 同左 |
| **不接纳 + planner 无反馈** | `rejected_no_feedback` (+2.0，检查步骤确认) | 同左 |

- 判定逻辑：`accepted` 取自 `PlannerResponse.accepted`；`has_feedback` 取 feedback 非空；只有 **accepted** 分支才由 `fixed` 决定 cell，否则不接纳分支只看有无反馈（与你的描述一致）。
- `fixed` 由 `FixVerdict` 承载，字段含 `evidence` 与 `judged_by="evaluator"`，强调"由 evaluator 从轨迹/代码判断"而非"是否有改动"。
- `run_check_step`：缺失 planner 回应等价于"无反馈地忽略" → `rejected_no_feedback`（对 evaluator 正反馈）。

### 3.2 盲评校准（`compute_calibration_reward`）

内循环一轮：evaluator **先盲评**（不传 benchmark）给出 `predicted_score` 与问题清单 → **揭示** `benchmark_score` → 深入评估。
校准奖励 = `-error_scale * |predicted_score - benchmark_score|`（`error_scale`/`weight` 可配）。

### 3.3 误报惩罚

`reward_hacking_false_positive` / `rule_violation_false_positive` 计入 `penalty_reward`。

### 3.4 反馈回流

`render_feedback(outcomes)` 生成"每个问题在 2×2 中的落点 + 奖励"的结构化反馈，内循环下一轮 evaluator 可见（配置 `feedback.evaluator_sees_prev_feedback: true`）。

---

## 四、遇到的问题与解决

| # | 问题 | 现象 | 解决 |
|---|---|---|---|
| 1 | `eval_points.yaml` YAML 解析失败 | `yaml.scanner.ScannerError: mapping values are not allowed here`（line 21） | 描述文本含 `: `（冒号+空格）被 YAML 当作映射分隔；给含冒号的 `description` 值加双引号 |
| 2 | 冒烟测试 `usage hook captured` 失败 | `TypeError: 'NoneType' object is not subscriptable` | 测试把 usage dict 当 response 传入；`_run_usage_hooks` 期望带 `.usage` 的 response 对象。修正测试构造 `_Resp(usage=...)` |
| 3 | WSL 内联命令引号被吞 | `warnings.warn: command not found`、`datasets: command not found`（`wsl ... bash -c '...grep -viE "a|b|c"...'`） | PowerShell→WSL 多层引号转义脆弱；改为把命令写入临时 `.sh` 脚本文件再执行（稳定） |
| 4 | 模型硬编码无法用 glm | 常量 `OPENAI_MODEL="openai/gpt-4o"` | 新增 `GAN_MODEL_DEFAULT` 环境覆盖，配合 `.env` 的 `OPENAI_API_BASE/KEY` |
| 5 | 限流导致 agent 循环中断 | `litellm.exceptions.RateLimitError: 该模型当前访问量过大`（Zhipu 429） | backoff 增补 `RateLimitError`；并按设计以"算子优先"减少 LLM 往返 |
| 6 | domain_utils 迁移回归风险 | 现有 `generate_loop`/`harness` 依赖其返回值 | 采用"注册表优先 + legacy 回退"，签名不变；回归脚本验证 8 项取值与旧实现一致 |
| 7 | 自定义算子目录无法被 `load_tools` 加载 | 原实现硬编码 `agent.tools.*` 模块名 | 新增 `tools_dir` 参数，用 `spec_from_file_location` 按文件加载 |

---

## 五、验证证据（P0）

- `python scripts/smoke_gan.py` → `ALL P0 SMOKE TESTS PASSED`，含：
  - 2×2 四格：`i1→accepted_fixed`、`i2→accepted_unfixed`、`i3→rejected_with_feedback`、`i4→rejected_no_feedback`；`issue_reward=4.0`、`calibration=-0.1`、`total=3.9`
  - 树：分叉上限排除、`best` 选择、深度追踪、持久化重载、停滞排除
  - `RateLimitError` 已进入 backoff 元组；usage 钩子可用；自定义 `tools_dir` 可加载
- 上游回归：`generate_loop/meta_agent/task_agent/run_meta_agent/utils.gl_utils/utils.domain_utils/agent.base_agent/agent.llm/agent.llm_withtools/agent.tools/domains.harness/domains.report` 全部导入 OK；`domain_utils` 行为与 legacy 一致。

---

## 六、P1（已实现）

### 6.1 新增文件

| 文件 | 作用 |
|---|---|
| `gan/access.py` | `AccessBroker` + `AccessContext`（contextvar）：工作区管理、路径安全校验（拒绝 `..`）、grant 时把请求路径复制到 `<workspace>/src/`、审计写 `events.jsonl`、`granted_paths()`。未 grant 前工作区无源码 → 真隔离 |
| `gan/operators/context.py` | `PlanContext`（配置 + 操作记录）与 `EvalContext`（盲评分/issues/penalties/评估点结果），用 contextvar 承载，供工具函数无参调用 |
| `gan/operators/common/request_source_access.py` | 共享门控工具：声明 `paths/intent/reason` → 触发 `AccessBroker.grant` |
| `gan/operators/planner_ops/*.py` | 7 个算子：`set_prompt`/`tune_param`/`apply_config`/`add_config`/`set_tool_enabled`/`swap_module`/`code_edit`（后者走门控） |
| `gan/operators/evaluator_ops/*.py` | 6 个：`report_issue`/`record_predicted_score`/`eval_trajectory_quality`/`eval_hard_failure`/`eval_reward_hacking`/`eval_rule_violation` |
| `gan/operators/registry.py` | `default_dirs(role)`、`assemble_tools_dir()`（把 common+role 算子复制到会话工具目录）、`load_operators()` |
| `gan/roles/base_role.py` | `Role(AgentSystem)`：装配会话工具目录、`run()` 注入角色提示词并调 `chat_with_agent(tools_dir=...)` |
| `gan/roles/planner.py` | `Planner.plan()`（构指令、注入 PlanContext/AccessContext、产出操作记录）、`self_improve()` |
| `gan/roles/evaluator.py` | `Evaluator.evaluate()`（**两阶段：盲评 → 揭示 benchmark 深入评估**）、`self_improve()` |
| `gan/loop.py` | `GanLoop`：内循环（select→plan→task_runner→evaluate→建包→commit→停滞判定）、外循环（两角色 self_improve）、事件日志、RewardPacket 落盘 `runs/<genid>/packet.json`；协作方为鸭子类型，便于离线测试 |
| `scripts/smoke_p1.py` | P1 冒烟测试 |

### 6.2 改动文件（P1）

| 文件 | 改动 | 原因 |
|---|---|---|
| `agent/llm_withtools.py` | `chat_with_agent` 新增 `tools_dir=None` 并透传给 `load_tools` | 角色需装载自己的算子/门控工具集 |

### 6.3 P1 冒烟验证（`scripts/smoke_p1.py`，全绿）

- **门控**：grant 前无 `src/`；grant 后文件（含嵌套）复制进 `workspace/src/`；`../../etc/passwd` 被拒；审计写入 `events.jsonl`；`granted_paths` 正确。
- **planner 算子**：6 类算子均写入配置/`custom`/`tools.*.enabled` 并产生记录。
- **evaluator 评估点**：`predicted_score`、`issues`、4 个评估点结果、`reward_hacking` 惩罚正确。
- **工具集装配**：planner 8 个工具、evaluator 7 个工具全部可发现。
- **检查步骤 + evaluator 奖励**：`i1 accepted_fixed`、`i2 rejected_with_feedback` → issue_reward=1.0，校准 −0.1，total=0.9。
- **双循环**（离线桩件）：2 外×≤4 内；`stagnation_break` 触发；`planner/evaluator.self_improve` 各调用 2 次；`events.jsonl` 含 `outer_start`/`inner_done`；`runs/*/packet.json` 落盘；盲评分写入节点 `potential`。

### 6.4 P1 遇到的问题与解决

| # | 问题 | 解决 |
|---|---|---|
| 1 | `gan/operators/{common,planner_ops,evaluator_ops}` 非包，`from gan.operators.planner_ops import ...` 失败 | 补空 `__init__.py`（`printf "" >` 时因参数报错但重定向已建文件；`find` 确认存在） |
| 2 | 冒烟断言"5 个评估点结果"失败 | `report_issue` 记入 `issues` 而非 `eval_point_results`；修正断言为 4 |
| 3 | 算子/门控工具无参上下文 | 用 `contextvars`（PlanContext/EvalContext/AccessContext）在角色会话前后 set/reset |
| 4 | `chat_with_agent` 硬编码工具目录 | 新增 `tools_dir` 参数透传（同 P0 的 `load_tools` 改造） |

### 6.5 全量回归（P0 + P1 + 上游）

`smoke_gan.py` ✅、`smoke_p1.py` ✅、21 个模块导入回归 ✅（含 `gan.loop`/`gan.roles.*`/`gan.access`）。

---

## 七、P3/P4/P5 实现（补全全部代码）

### 7.1 新增文件

| 文件 | 作用 |
|---|---|
| `gan/summary.py` | `build_diff_summary`（**剥离 rationale/reason**，只给算子清单+文件）、`make_feedback`、`validate_feedback`（schema 版本校验） |
| `gan/patch.py` | `build_patch_from_workspace`（用 difflib 对"授权工作区副本 vs 源仓库"生成统一 diff）、`apply_patch`（`git apply` 失败回退 `patch -p1`） |
| `gan/operators/planner_ops/respond_issue.py` | planner 对每条 evaluator 问题回应（`accepted`+`feedback`），供 2×2 的"接纳"判定 |
| `gan/operators/evaluator_ops/judge_fix.py` | evaluator 判定上一轮问题是否**真正修复**（要求 evidence） |
| `gan/task_runner.py` | `DomainTaskRunner`：真实跑 `domains.harness`+`domains.report`（host），以 `GAN_TASK_MODEL` 注入模型；有代码补丁时复制仓库副本并应用补丁；读 `report.json` 得 score，返回 `Node` |
| `gan/build.py` | `build_gan_loop(...)` 工厂：装配 Config/Planner/Evaluator/DomainTaskRunner/AccessBroker/GanLoop |
| `scripts/run_gan.py` | GAN 双循环 CLI 入口（`--task-domain/--subset/--num_samples/--outer/--inner/--*-model`） |

### 7.2 改动文件

| 文件 | 改动 | 原因 |
|---|---|---|
| `gan/operators/context.py` | `PlanContext.responses`、`EvalContext.fix_verdicts` 及 add 方法 | 承载检查步骤所需数据 |
| `gan/roles/planner.py` | `plan()` 返回 `{records, responses, config, patch}`；校验 feedback schema；生成工作区补丁 | 检查步骤 + 门控代码修改 |
| `gan/roles/evaluator.py` | 提示词加入 `judge_fix` 指令；`evaluate` 返回含 `fix_verdicts` 的 ctx | 2×2 的"修复"判定 |
| `gan/roles/base_role.py` | 实现 `forward()`（满足 `AgentSystem` 抽象基类） | 否则无法实例化（ABC） |
| `gan/loop.py` | 接入检查步骤结算、evaluator 奖励、advantage、三树版本化、config 血统快照、`_last_feedback` 用 `make_feedback` | P5 闭环 |
| `gan/reward/evaluator_reward.py` | 增加 `issues_from_dicts`/`responses_from_dicts`/`verdicts_from_dicts` | loop 用 dict 传递 |
| `gan/config/prompts/{planner,evaluator}.md` | 加入 `respond_issue` / `judge_fix` 指令 | 让模型调用新工具 |
| `gan/config/gan_loop.yaml` | 三角色默认模型改为 `gpt-4o-mini`（匹配当前 API） | 可直接运行 |
| `domains/harness.py` | `model = os.environ.get("GAN_TASK_MODEL") or utils_module.MODEL` | 按节点配置注入任务模型 |

### 7.3 检查步骤（2×2）在 loop 中的结算时序
```
第 t 轮:  evaluator 盲评 → issues_t, predicted_t
第 t+1 轮: planner.plan(issues_t) → responses_t+1
           task_runner 执行 → benchmark_t+1（记录 t 的 benchmark=benchmark_t）
           evaluator.evaluate(prev issues_t) → fix_verdicts_t+1
           compute_evaluator_reward(issues_t, responses_t+1, fix_verdicts_t+1,
                                    predicted_t, benchmark_t)
           → 写 runs/<genid>/evaluator_reward.json + events.jsonl + 累积供 outer self_improve
```
外循环：`evaluator.self_improve(recent={evaluator_rewards,...})`、`planner.self_improve(recent={advantages,...})`；并分别推进 `evaluator_tree`/`planner_tree`。

### 7.4 本期问题与解决

| # | 问题 | 解决 |
|---|---|---|
| 1 | `TypeError: Can't instantiate abstract class Planner ... 'forward'` | `AgentSystem` 是 ABC；在 `Role` 实现 `forward()` 委托给 `run()` |
| 2 | 检查步骤数据缺失（无 planner 回应/修复判定） | 新增 `respond_issue`/`judge_fix` 工具与 context 字段，loop 跨轮结算 |
| 3 | 评测者可能收到 planner 的自由文本理由（注入风险） | `build_diff_summary` 剥离 rationale/reason，仅传算子+文件清单 |
| 4 | 代码补丁需在隔离环境生效 | `patch.py` 生成工作区 diff；`DomainTaskRunner` 用仓库副本应用补丁后运行 |

### 7.5 验证

- `scripts/smoke_gan.py`（P0）✅
- `scripts/smoke_p1.py`（含门控/补丁/算子/评估点/工具集/摘要脱敏/检查步骤/双循环结算）**全绿** ✅
- `build_gan_loop` 装配验证 ✅（planner/evaluator 模型、工具集、task_runner.score_key、broker）
- 新模块导入回归 ✅

### 7.6 已知限制 / 未接线项
- evaluator 外循环的**作弊误报惩罚**尚未做误报检测（`compute_evaluator_reward` 的 `penalties` 传入 `{}`）；接口已留。
- 容器化隔离（P3 硬化）未做：当前角色在 host 目录隔离运行（`AccessBroker` 工作区）；容器 mount 边界为后续增强。
- 真实 `generate_loop` 式多域/容器编排未接入 GAN loop（`DomainTaskRunner` 走 host harness）。

### 7.7 如何运行（最小化）
```bash
# 依赖 .env 中的 OPENAI_API_BASE/KEY（当前为 az.gptplus5.com / gpt-4o-mini）
python scripts/run_gan.py --task-domain paper_review --subset _filtered_100_train \
  --num_samples 2 --outer 1 --inner 2 --output_dir outputs/gan_paper_review
```

---

## 八、最小化端到端运行（本期）

### 8.1 运行
```bash
python scripts/run_gan.py --task-domain paper_review --subset _filtered_100_train \
  --num_samples 2 --outer 1 --inner 2 --output_dir outputs/gan_paper_review
```
API：`az.gptplus5.com/v1` + `gpt-4o-mini`。约 20+ 分钟（含 agent 工具循环与 harness 子进程）。

### 8.2 结果（全部产物已生成）
`events.jsonl`：
```
init → outer_start
inner_done genid=0 score=0.0 modify_depth=1
evaluator_reward genid=1 reward=7.0 cells=[accepted_unfixed ×7]
inner_done genid=1 score=0.0 parent_score=0.0 improved=false no_improve=1
  penalties={cannot_run:1, rule_violation:1, reward_hacking_suspect:1}
outer_done
GAN loop done. task tree size=3
```
产物：
- `task_tree.jsonl`：`initial → 0 → 1`，含 `parent_genid`/`depth`/`children`/`value{score,potential}`/`scores`/`modify_depth`/`meta.config_dict`（配置血统）/`records`。
- `planner_tree.jsonl` / `evaluator_tree.jsonl`：各 1 个外层版本节点。
- `runs/0/packet.json`、`runs/1/packet.json`：RewardPacket（benchmark/delta/penalties/textual）。
- `runs/1/evaluator_reward.json`：**2×2 结算**——`issue_reward=7.0`（7× `accepted_unfixed`）、`calibration_reward≈0`、`total=7.0`。
- `nodes/{0,1}/config.json`：每代任务配置快照。
- 子进程 harness 产物：`outputs/gan_{0,1}/report.json`（`overall_accuracy=0.0`）。

### 8.3 结论
- **整条 GAN 双循环代码路径跑通**：planner 规划→task_runner 真实评测→evaluator 盲评+揭示→检查步骤 2×2 结算→三树版本化→外循环 self_improve。
- evaluator 正确地将 planner 的"重复调用同一工具"识别为 `hard_failure`/`reward_hacking_suspect`，并给出 `accepted_unfixed`（问题被接纳但未修复）——2×2 语义符合设计。
- task 分数 0.0：种子通用 TaskAgent（同 §十一 的 DGM-H 测试），属预期，靠迭代改进。

### 8.4 运行中发现的问题与修复

| # | 问题 | 现象 | 修复 |
|---|---|---|---|
| 1 | 规划者重复调用同一工具 | `set_tool_enabled(add_config,true)` 连调 40 次触顶 | 提示词加"停止条件/禁止重复调用"；`self_improve` 限 `max_tool_calls=8` |
| 2 | `self_improve` 工具无上下文 | evaluator 自改时 `Error: no eval context`（评估工具缺 EvalContext） | `Planner.self_improve` 设 `PlanContext` 并把自身配置持久化到 `<role>_self_config.json`；`Evaluator.self_improve` 设 `EvalContext` |
| 3 | 规划者把配置写进 `config.config.*` 前缀 | 因算子说明不够明确 | 记录为后续提示词优化项（不影响运行） |

### 8.5 遗留（下一轮优化项）
- 重复调用型退化：更强的模型或"算子去重/预算"硬约束可缓解。
- `self_improve` 目前对 evaluator 的自改只落到内存（未落地评估点文件）；planner 已落地 `planner_self_config.json`。
- 真实多代（outer>1）与多域未跑；作弊误报惩罚、容器隔离仍为 §7.6 的遗留项。

---

## 九、v2 设计重构（P0 执行）

按讨论结论对设计做四处修订并落地（详见 `docs/深入设计说明.md` 的「v2 设计修订」）。

### 9.1 文件改动

| 变更 | 文件 | 说明 |
|---|---|---|
| **DAG** | `gan/tree/store.py` | `Node.parents: List`（兼容 `parent_genid`）；`add_node` 多父代记 children、`depth=max+1`；`select_parents(k)` |
| **反馈文字化** | `gan/reward/evaluator_reward.py` | 删除 `compute_evaluator_reward`/`render_feedback`/矩阵权重；`classify_issue` 仅出标签；新增 `build_feedback_digest`（纯文本） |
|  | `gan/reward/__init__.py` | 导出更新 |
|  | `gan/loop.py` | `_settle_feedback_digest` 落 `runs/<genid>/feedback_digest.md`；`self_improve(recent={digests})`；DAG `parents` 传递 |
|  | `gan/roles/evaluator.py` | 盲评指令注入 digest；`self_improve` 改为读文本 digest 的叙事式自省 |
|  | `gan/config/gan_loop.yaml` | 删除 `evaluator_reward.matrix/calibration.weight`；新增 `feedback.mode=text_digest`；`tree.num_parents` |
| **设计包** | `gan/design/{schema,context,registry,store,composer}.py` | 各角色**极小** schema；`design/<role|task-node>/config.json`；分角色注册表 + 组件目录；`DesignContext` |
|  | `gan/design/registries/{task,planner,evaluator}_registry.json`、`gan/design/components/{skills/echo,memory/naive}.py` | 种子注册表与组件 |
|  | `task_agent.py` | 改为**设计驱动**（`GAN_TASK_DESIGN` + `GAN_TASK_SKILLS_DIR`） |
|  | `gan/task_runner.py` | 用 `DesignStore` 写节点设计并注入环境变量；`_modify_depth` 适配新算子 |
|  | `gan/build.py` | 装配时种子化 planner/evaluator 自身设计 prompt |
| **算子分层** | `gan/operators/work_tools/{planner,evaluator}/` | 工作 tools（`respond_issue`；评分/检查/judge_fix）——**从旧 planner_ops/evaluator_ops 迁移** |
|  | `gan/operators/design_ops/*.py` | 浅层设计算子 `set_prompt`/`set_config`/`select_component`/`set_param`/`mint_operator` + 深层 `code_edit` |
|  | `gan/operators/registry.py` | `default_dirs(role)` 改为 work_tools+design_ops+common（task→skills） |
|  | `gan/roles/base_role.py` | 工具集装配适配；`current_prompt()` 从自身设计读取；自设计经 `DesignStore` |

### 9.2 关键裁定
- **“新增配置项”不是算子**：新键必须改代码才能被消费 → 归为**源码级（deep）**，经 `code_edit` 门控。浅层算子只能改**已有键的值** + 从注册表选择。
- **evaluator 的逐项评分是工作 tools**；“新增评分项/组件”才是深层。
- `mint_operator` 仅限**组合式**（声明式）——浅层；需新逻辑则深层。

### 9.3 验证
- `scripts/local/smoke_p1.py`（v2）**全绿**：设计包/浅层算子（含“新键被拒”）/工作 tools/工具集/文字 digest/DAG/双循环（digest 结算与落盘）。
- `scripts/local/smoke_gan.py`（P0）**全绿**（去掉数值矩阵断言）。
- 模块导入、`build_gan_loop` 装配（设计种子化、工具集内容、任务 skills 目录）均通过。

### 9.4 遗留
- 未做真实运行（需 API）；交叉算子的**实际启用**（`tree.num_parents>1` + config 合并执行）留待 P2。
- evaluator 的 `eval_points` 选择已由 `gan/tools/assembly.py` **按选择裁剪**（仅装载被选中的可选评估点）。

---

## 十、v3 目录规划（配置/算子/tools 文件管理重构）

按「性质 × 可变性」重组目录，落地“总是启用=tools / 可选启用=components”的划分。

### 10.1 目录变动
| 旧 | 新 |
|---|---|
| `gan/config/gan_loop.yaml` | `gan/framework/loop.yaml` |
| `gan/config/registry.yaml` | `gan/framework/domains.yaml` |
| `gan/config/loader.py` | `gan/framework/loader.py` |
| `gan/config/prompts/*.md` | `gan/design/seeds/*.md`（+ `task.md`） |
| `gan/config/eval_points.yaml` | 删除（单一来源：registries + 实现） |
| `gan/operators/context.py`、`gan/design/context.py`、access ctx | `gan/context.py` |
| `gan/operators/registry.py` | `gan/tools/assembly.py` |
| `gan/operators/common/request_source_access.py` + `design_ops/code_edit.py` | `gan/tools/deep/request_source_access.py`（合并） |
| `gan/operators/design_ops/*` | `gan/tools/design/*` |
| `gan/operators/work_tools/planner/*` | `gan/tools/work/planner/*` |
| `gan/operators/work_tools/evaluator/{report_issue,record_predicted_score,judge_fix}` | `gan/tools/work/evaluator/*` |
| `gan/operators/work_tools/evaluator/eval_*` | `gan/components/evaluator/eval_points/*`（可选） |
| `gan/design/registry.py` | `gan/registries/loader.py` |
| `gan/design/registries/*_registry.json` | `gan/registries/{shared,task,planner,evaluator}.json` |
| `gan/design/components/{skills,memory}` | `gan/components/{shared,task}/…` |

### 10.2 语义规则
- **总是启用 → `gan/tools/`**；**可选启用 → `gan/components/`**（注册 + 选择）。
- **task agent 无常驻工具**：能力全为可选 skills（空种子起步），深层新增经 `tools/deep`。
- **evaluator 评估点**：核心 3 个常驻（`tools/work/evaluator`），可选 4 个入 `components/evaluator/eval_points`（按 config 装载）。
- `agent/tools/`（bash/editor）为框架基础；以 `components/shared/skills/*.py` re-export 登记为通用可选技能。
- schema 增加 `operators` 槽；`mint_operator` 写该槽。

### 10.3 验证
- `scripts/local/smoke_gan.py` ✅、`scripts/local/smoke_p1.py`（v3：设计包/浅层算子/工作 tools/可选评估点/工具集按需装配/摘要脱敏/DAG/双循环）✅。
- 导入回归、`build_gan_loop` 装配（planner/evaluator 工具集内容、设计种子化）✅。

### 10.4 遗留
- 未跑真实 API 端到端；交叉算子的**实际启用**（`tree.num_parents>1` + config 合并）与 memory 策略的**运行期接入**留待后续。
