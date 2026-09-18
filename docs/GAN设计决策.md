# GAN 主实验设计决策记录

依据：对两轮 DGM-H 运行（`generate_20260915_194845_690053` 10 代有提升、
`generate_20260916_234111_094614` 20 代零提升）的日志/产物分析。

全局约束：
- **评分标准保持不变**（不修改 `domains/report.py` 的判定语义，含"有效部分求平均"）。
- 不刻意拆分 DGM-H 与 GAN 的代码；DGM-H baseline 由外部维护，本记录只约束 GAN 主实验。

术语：**冻结** = 默认不可被进化修改；**可解冻** = 记为候选，未来作为独立研究轴解冻，
解冻需在实验设计中显式标注，以免污染主实验结论。

---

## 1. evaluator 的输入与评估点（R1）

### 1.1 现状：evaluator 实际能拿到什么
- `run_summary = {genid, score, meta}`（`gan/framework/loop.py`，`evaluator.evaluate(run_summary=...)`）。
- `meta`（来自 `gan/framework/task_runner.py`）只含：`run_dir, report_path, design_path, harness_rc, model,
  delta_vs_parent, patch_applied, records`。**只有 report 的路径，没有内容。**
- `prev_feedback = {schema_version, issues, diff_summary, penalties, digest}`。
- 自有设计 prompt（`gan/design/seeds/evaluator.md`）与 always-on 工具（`report_issue` / `judge_fix` /
  `record_predicted_score` + design 算子 + deep gate）。
- 可选评估点来自 `gan/registries/evaluator.json`，但**初始 design 的 `eval_points` 为空**
  （`_seed_self_designs` 只 seed prompt，不 seed `eval_points`）→ 默认没有任何评估点生效。

### 1.2 缺失（导致"预测是综述不是标签"无法被发现）
- benchmark 的**输出契约/要求**（如 paper_review 要求小写精确 `accept|reject`）。
- 评测结果的**结构化事实**：`label_distribution`、`invalid_rate`/合法标签率、`sample_n`。
- task agent 实际使用的提示词/设计与 benchmark 要求的**对照**。

### 1.3 决策：`output_contract` 是**框架客观计算**，**不作为评估点**
- 由框架客观计算：契约是否满足、合法标签率、`invalid_rate`、覆盖率，作为**测量事实**
  注入 `run_summary`（以及反馈给 planner/evaluator 的 `issues`/`penalties`）。
- 因此它**不是评估点**：评估点是 evaluator 用来"判断"的工具；契约检查是对事实的计算，二者不要混。
  （若把它做成评估点，就会出现"事实由框架算却要 evaluator 判"的悖论；若"由 evaluator 判"，
  则事实又进不了 `run_summary`。二选一 → 选客观计算。）
- 待实现：`gan/framework/loop.py` 组装 `run_summary` 时增加 `report_summary`
  （`label_distribution` / `invalid_rate` / `contract_ok` / `sample_n`）；
  领域契约写入 `gan/framework/domains.yaml`。**不新增 `eval_output_contract.py`，不注册评估点。**

### 1.4 评估点的开启策略（判断类）
| 类别 | 评估点 | 处置 |
|---|---|---|
| 判断类（主观、需推理） | `trajectory_quality`、`hard_failure`、`reward_hacking`、`rule_violation` | **可选，但初始 design 默认选中** |
| 契约/测量类 | （无） | 见 §1.3：由框架客观计算，不是评估点 |

- 因此**不设 always-on 评估点**；修正"默认零评估点"的方式是扩展 `_seed_self_designs`
  把 `eval_points` 也 seed 进 evaluator 的初始 design。
- always-on 只保留给**工具**（work/design/deep），不用于评估点。

**涉及文件**：`gan/framework/loop.py`、`gan/framework/task_runner.py`、`gan/framework/domains.yaml`、
`gan/design/seeds/evaluator.md`、`gan/build.py`（`_seed_self_designs`）。

---

## 2. substrate 冻结与隔离告警（R2 / R3）

### 2.1 结论：substrate **冻结但可解冻**
- `agent/llm.py`、`agent/llm_withtools.py`、`agent/base_agent.py`、`gan/framework/*`、`gan/framework/context.py`
  默认**不允许在进化中修改**（信任锚）。理由（DGM-H 实证）：substrate/工具序列化缺陷会被 agent
  当成"待修问题"，把方向带偏（20 代 run 反复改 `llm_withtools`/模型名并自伤出 503/model_not_found）。
- 记为**可解冻候选**（同 R6/R7）：未来研究"系统自修复 substrate"须作为独立实验轴并单独标注。
- 传输层重试/回退/preflight 属于**框架**。

### 2.2 告警不外泄（已实现）
- `gan/tools/deep/request_source_access.py` **不再**把 `denied` 回传 agent；只返回实际获得的路径/中性提示。
  拒绝明细仅写 `events.jsonl` 与 `broker.last_result`。
- 物理隔离仍生效：被拒路径不拷入 workspace `src/`，`bash` 读不到，报错为普通"文件不存在"。

### 2.3 冻结清单（单一事实源：`gan/framework/frozen.py`）
冻结不再写死在 `loop.yaml`，而是由物理布局 + 显式外部清单派生：
```
gan/framework/*                         # 冻结即“在 framework 下”，结构性不变量
agent/llm.py, agent/llm_withtools.py, agent/base_agent.py   # 外部共享基底
domains/harness.py, domains/report.py   # 域测量/评分定义
```
- `gan/build.py` 用 `gan.framework.frozen.deny_paths()` 构造 AccessBroker 的 deny 列表。
- 不变式由 `scripts/tests/test_frozen.py` 守护：旧路径不得存在；deny 列表 == 单一事实源；
  框架不得 import `gan.roles`（角色靠注入）。
- 目录变更：`gan/{loop,context,access}.py`、`gan/tree/`、`gan/framework/{task_runner,reward/}` 现有内容
  统一位于 `gan/framework/`（`loop.py`、`context.py`、`access.py`、`tree/`、`task_runner.py`、`reward/`）。

### 2.3.1 为什么外部两项不移动
`agent/*` 是全 DGM-H 共享的 LLM/工具运行时，`domains/harness.py`+`report.py` 是域测量装置；
移入 framework 会造成 framework→agent/domains 的依赖反转，并破坏“不拆分 DGM-H/GAN 代码”的现状。
故保留原位，仅在 `frozen.py` 中显式登记为 external frozen。

### 2.4 模型选择已冻结（已实现）
- `gan/tools/design/set_param.py`：拒绝 `name == "model"`。
- `gan/tools/design/set_config.py`：`params` 中的 `model` 键被剥离。
- 角色模型只由 `gan/build.py` 从框架配置/环境解析。

### 2.5 R2/R3 改动完成度（诚实状态）
- **已完成**：substrate 冻结（deny_paths）、隔离告警不外泄、模型选择冻结。
- **未完成（待落实）**：
  1. **逐题容错**：`domains/harness.py: run_agent` 无 try/except，任一题异常即整批 exit 1。
  2. **断点续跑**：`harness.py` 的 resume 把 `""`（空预测）当作"已完成"，不会重试空题。
  3. **重试扩展**：`agent/llm.py` 目前只把 `litellm.RateLimitError` 纳入 backoff，
     缺 `ServiceUnavailableError` / `Timeout` / `OpenAIError`。
  4. **fallback 模型**：完全不存在。

---

## 3. 随机基线（R4，仅记录，暂不实现）

- 结论：**有效分数用相对增益** `excess = max(0, (acc - rg) / (1 - rg))`，并在图上标注随机基线。
- `rg`（`random_guess_accuracy`）必须在**固定请求样本集**上计算，不随空预测丢弃而漂移。
- 多域聚合（`analysis/plot_progress.py` together 模式）必须用 excess。
- 待办：`gan/framework/reward/packet.py` 增 `random_guess`/`excess`；选择与 `analysis/*` 改用 excess。

---

## 4. "未评分"、失败轮次与版本管理（R5）

### 4.1 未评分的类型（仅内循环 task agent 考虑）
1. **partial**：部分样本有效（0 < 有效样本 < 请求样本）。
2. **failed**：评测崩溃 / 全部样本无效（无有效客观分）。
3. 说明：外循环（planner/evaluator）**不纳入"未评分"考虑**，只做 checkpoint/回退（§4.4）。

### 4.2 一次内循环 = `select parent → plan → task → bench_eval → evaluator → save`（现设计）

### 4.3 失败与分数规则
- **planner / evaluator / task agent 执行失败**（模型调用重试后仍未恢复、程序无法运行等）：
  该轮次**无效**（invalid），**不能作为 parent**，但**仍记入档案**（含失败原因）。
- **bench_eval 出错也传递给 evaluator**（作为其可用的过程信息/issue 依据）。
- **bench 分数部分缺失**：用**有效部分的平均分**（`domains/report.py` 现有语义，评分标准不变），
  附 `coverage = 有效/请求`，`score_status = "partial"`。
- **bench 分数完全缺失**：取 imputed 分，`score_status = "failed"` 且 `imputed = true`：
  - 规则（选择）：**优先同父兄弟节点的中位数；若不足 2 个兄弟，则取父代分数**
    （父代分数是保守回退：保证失败子代不高于其来路，避免"失败被当成提升"）。
- **是否作为 evaluator 锚点**：**否**。只有**真实测得**的客观分作为 evaluator 的"揭示锚点"；
  imputed 值以 `imputed_score` 单独标注传递，并明确其"估计值"属性，避免污染 blind-vs-actual 校准。

### 4.4 外循环 checkpoint（新增；不含父代选择、不含未评分）
- 在**每轮内循环结束后**与**每个外循环结束后**落盘 checkpoint：task/planner/evaluator 三棵树、
  `_digests`、`_last_feedback`、计数器、**角色设计快照**（planner/evaluator 的 `design/*` 配置）、RNG seed。
- 允许**回退**（restore 到上一个外循环边界）与**接续**（从最后完成的轮次继续）。
- 动机：planner/evaluator 目前是**单一可变实例**（`self_improve` 原地覆盖 `design/<role>/config.json`，
  无 archive/无回退）；checkpoint 是它们唯一的"版本/回退"手段。
- **保留当前选择算法**（不改 task tree 的 UCB/候选/公式）。

### 4.5 版本管理现状与归属
- **task agent**：DAG + UCB 选择（`gan/framework/tree/store.py`），有版本与父代选择。
  - 候选：`valid_parent and status=="alive" and children<branch_limit and depth<depth_limit`；
  - `ucb`: `metric = score + ucb_c·sqrt(log(N+1)/(children+1)) − depth_penalty·depth − cost_penalty·cost`；
    另有 `best`/`latest`；`k>1` 支持 crossover；初始节点 `score=None`。
  - **任务父代选择策略 = 冻结且可解冻**（`gan/framework/tree/store.py` 已在 `deny_paths`）。
- **planner / evaluator**：单个可变实例；其 `planner_tree`/`evaluator_tree` 仅**记事件**，无选择/回滚；
  版本控制依赖 §4.4 的外循环 checkpoint。

---

## 5. 冻结边界与组件开放度（R6 / R7）

### 5.1 冻结（主实验）
- **必须**：模型选择（§2.4）、`gan/framework/access.py`、`gan/framework/*`、`gan/framework/context.py`、
  测量与评分（`domains/report.py`、`domains/harness.py`）、substrate（§2.1）。
- **全部冻结**：`gan/framework/reward/*`、`gan/framework/task_runner.py`（已加入 `deny_paths`）。
- **主实验冻结，记为可解冻候选**：`gan/framework/loop.py`、`gan/framework/tree/*`（含 task 父代选择策略）。

### 5.2 组件开放度（放宽）
- `gan/components/`、`gan/registries/`、`gan/tools/`：允许在深改门控下**新增/编辑组件与目录项**，
  不只是"从目录中选择"；所有深改经 `request_source_access`（`auto_approve=true` + 全量审计）。

### 5.3 "会改变评分口径"的组件
- **现状不存在**：客观分只来自已冻结的 `domains/report.py`；评估点只产出
  `issues`/`penalties`/`eval_point_results`，而 UCB 只用 `score` + cost/depth，penalties 不进入选择。
- **暂不处理**：不实现 `affects_scoring` 审批机制（留待未来若 penalties 接入选择时再引入）。
  原则仍记录：**组件可产生反馈，但不能改变客观分，也不能借此抬高自身位次。**

### 5.4 `gan/framework/reward/` 的功能与归属
- 功能：`packet.py` = 三角色交换的统一结构；`evaluator_reward.py` = issue↔response↔verdict 的
  2×2 定性分类与文字 digest（无数值奖励）。
- 归属：**冻结**（跨角色协议，防止角色弱化自身问责）。

### 5.5 `gan/framework/task_runner.py`：功能与归属（"design 落盘与胶水层"并入 framework）
- **design 落盘**：把 planner 的 task 设计写盘成节点快照（`DesignStore.save(config,"task",genid)`）。
- **胶水层**：装配 task 技能目录、设环境（`GAN_TASK_DESIGN`/`GAN_TASK_SKILLS_DIR`/`GAN_TASK_MODEL`）、
  复制仓库并 `apply_patch`、调用 harness/report、封装 `Node`。
- 决策：**design 落盘与胶水层并入 `gan/framework/`**；`gan/framework/task_runner.py` 整体冻结。
  实现为待落实项（§9）。

---

## 6. 逐条 issue 反馈链路（R8）

### 6.1 现状（链路完整）
`report_issue` → `EvalContext.issues` → `make_feedback(issues)` → `planner.plan(evaluator_issues)`
→ `respond_issue` → `PlanContext.responses` → `_settle_feedback_digest(prev_issues, responses, verdicts)`
→ `build_feedback_digest` → `runs/<genid>/feedback_digest.md` + `_digests`
→ 下一轮 evaluator 的 `prev_feedback.issues + digest` → `judge_fix` → `ctx.fix_verdicts`。

### 6.2 已确认、暂不修改（记录）
- **两轮延迟**：第 k 轮 evaluator 收到的 digest 是关于第 k−2 轮 issue（k−1 轮才结算）。保留。
- **planner 不接收 fix verdicts**：digest 只喂 evaluator。

### 6.3 双向反馈：采用"evaluator 自然发现"，不做框架级未结项账本
- 历史反馈 = **整体一条，不设"最近 N 条"上限**。
- **不做框架级"未结项 issue 专项"**：未结项由 evaluator 自己发现（自然并入当轮 issue，或升级为新评估点），
  以此保留 evaluator 的自进化/发现空间。
- 该设计下 `self._last_feedback` **单槽足够**。
- 对冲遗忘：用**诊断非强制**（所有 issue 与是否被重提记入 `events.jsonl`，作为遗忘率过程指标）；
  不靠 prompt 约束，不设框架硬上限。

### 6.4 `judge_fix` 未调用 → `fixed=False` 的问题（展开）
- 路径：`gan/framework/reward/evaluator_reward.py: classify_issue` 中
  `fixed = bool(verdict.fixed) if verdict is not None else False`；planner `accepted=True` 时
  `cell = accepted_fixed if fixed else accepted_unfixed`。
- 后果：evaluator 未调用 `judge_fix` 的 issue 被**默认记成 `accepted_unfixed`**，
  digest 写成"accepted but NOT fixed"。
- 危害：(1) 把"未判定"误报为"已接受未修复"，对 planner 不实指控；
  (2) 污染 evaluator 的自我反思信号；(3) 2×2 统计被系统性偏移。
- 建议（概念）：引入三态 `fixed ∈ {true,false,unjudged}`，`unjudged` 在 digest 单列并计数；
  2×2 只统计"已判定"，另设 `unjudged` 桶。

---

## 7. 对照消融的归属（R9）
- **实验协议**（哪些臂/seed/指标/显著性）= **冻结框架/外部**；**插桩**（events.jsonl 等）= **框架**；
  **evaluator 内部判据**（评估点/阈值）= **自进化**。二者不混在同一可编辑面。（仅记录，不改代码。）

---

## 8. 本次已实施改动
- **布局迁移（冻结物统一入 framework）**：`git mv` `gan/loop.py`→`gan/framework/loop.py`、
  `gan/context.py`→`gan/framework/context.py`、`gan/access.py`→`gan/framework/access.py`、
  `gan/tree/`→`gan/framework/tree/`；连同此前的 `gan/{task_runner.py,reward/}` 一并位于 `gan/framework/`。
  全局 import 已同步；新增**单一事实源** `gan/framework/frozen.py` 与守卫测试 `scripts/tests/test_frozen.py`；
  `loop.yaml` 不再写死 deny 列表；`gan/build.py` 从 `frozen.py` 取 deny 列表。
- `gan/tools/deep/request_source_access.py`：拒绝告警不外泄。
- `gan/tools/design/set_param.py`：拒绝 `model`；`gan/tools/design/set_config.py`：剥离 `params.model`。
- `gan/framework/loop.yaml`：`deny_paths` 扩展（access/loop/tree/reward/task_runner/report/harness）。
- `gan/framework/domains.yaml`：每域 `output_contract`（paper_review=accept/reject、imo_grading 等）。
- `gan/framework/task_execution.py`（新）：design 落盘 + env/工具装配 + 仓库/patch + harness/report + `report_summary`。
- `gan/framework/checkpoint.py`（新）：外循环快照（state/trees/designs），save/load/restore。
- `gan/framework/task_runner.py`：改为薄胶水，委托 framework；产出 `score_status`/`coverage`/`report_summary`。
- `gan/framework/tree/store.py`：`NodeValue` 增 `score_status`/`coverage`。
- `gan/framework/loop.py`：失效轮语义、分数回退（兄弟中位数→父代）、imputed 不作 evaluator 锚点、
  外循环 checkpoint、角色设计快照+非提升回退、`report_summary` 透传、`resume`/`rollback_to_outer`。
- `gan/build.py`：`_seed_self_designs` 默认 seed evaluator 的评论类 `eval_points`。
- `gan/framework/reward/evaluator_reward.py`：`judge_fix` 三态（`unjudged`）+ 2×2 分桶，digest 单列。
- `agent/llm.py`：backoff 扩到 RateLimit/ServiceUnavailable/Timeout/APIConnectionError 等，
  并支持 `GAN_MODEL_FALLBACK` 兜底模型。
- `domains/harness.py`：逐题 try/except 隔离（失败记 `eval_failures.jsonl` 并留空预测）；
  resume 修正（空预测不再算"已完成"）。

---

## 9. 实施状态
| # | 事项 | 状态 | 落点 |
|---|---|---|---|
| T1 | `run_summary` 增 `report_summary` | ✅ 完成 | `gan/framework/loop.py`, `gan/framework/task_execution.py` |
| T2 | `domains.yaml` 每域 `output_contract`（不加评估点） | ✅ 完成 | `gan/framework/domains.yaml` |
| T3 | seed `eval_points`（判断类默认选中） | ✅ 完成 | `gan/build.py` |
| T4 | harness 逐题容错隔离 | ✅ 完成 | `domains/harness.py` |
| T5 | resume 空预测修正 | ✅ 完成 | `domains/harness.py` |
| T6 | 重试扩展 + fallback 模型 | ✅ 完成 | `agent/llm.py` |
| T7 | 外循环 checkpoint（落盘/回退/接续） | ✅ 完成 | `gan/framework/checkpoint.py`, `gan/framework/loop.py` |
| T8 | 失败轮语义（invalid 记档不可作 parent；bench 错误传 evaluator） | ✅ 完成 | `gan/framework/loop.py` |
| T9 | 分数回退（partial/coverage；imputed=兄弟中位数→父代；不作锚点） | ✅ 完成 | `gan/framework/loop.py`, `gan/framework/task_execution.py` |
| T10 | 角色设计版本/接纳判据 | ✅ 完成（外循环快照+非提升回退） | `gan/framework/loop.py`, `gan/framework/checkpoint.py` |
| T11 | design 落盘 + 胶水层迁入 `gan/framework/` | ✅ 完成 | `gan/framework/task_execution.py`, `gan/framework/task_runner.py` |
| T12 | `judge_fix` 三态 + 2×2 分桶 | ✅ 完成 | `gan/framework/reward/evaluator_reward.py` |
| T13 | random_guess/excess 指标 + `analysis/*` 改用 excess | ⏳ 待做（仅记录） | `gan/framework/reward/packet.py`, `analysis/*` |
| T14 | 组件 `affects_scoring` 审批机制 | ⏸ 暂缓 | — |

验证：`scripts/local/smoke_gan.py`、`scripts/local/smoke_p1.py` 全通过；
新增行为（三态、report_summary、checkpoint 回退、seed eval_points、imputed、invalid 轮、角色回退）已用临时脚本逐项验证通过。
