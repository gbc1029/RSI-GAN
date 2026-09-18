# GAN 主实验 —— 修改记录（R1–R9）

> 依据：对两轮 DGM-H 运行（`generate_20260915_194845_690053` 10 代有提升、
> `generate_20260916_234111_094614` 20 代零提升）的日志/产物分析。
> 约束：**评分标准不变**（`domains/report.py` 判定语义不改）；不刻意拆分 DGM-H/GAN 代码，DGM-H baseline 由外部维护。
> 术语：**冻结** = 默认不可被进化修改；**可解冻** = 记为候选，未来作为独立研究轴。

---

## R1 反馈接地与评估点

**问题**
- evaluator 只收到 `run_summary={genid, score, meta}`，`meta` 只有 `report_path` **没有 report 内容**；
- paper_review 只认小写精确 `accept|reject`，而 seed task agent 输出整篇综述 → 客观分恒 0；
- 评估点是 opt-in 且**初始 `eval_points` 为空** → evaluator 默认没有任何检查项，无法发现"输出不符合契约"。

**原设计**
- `run_summary` 无评测事实；`contract/output_contract` 不存在；评估点默认全关。

**新设计**
- 契约是**框架客观计算**，**不作为评估点**；计算结果作为事实注入 `run_summary`。
- 判断类评估点（trajectory_quality / hard_failure / reward_hacking / rule_violation）**可选但初始默认选中**，保留自进化空间。
- 不设 always-on 评估点（always-on 仅用于工具）。

**具体改动**
- `gan/framework/domains.yaml`：每域新增 `output_contract`（paper_review=`accept|reject`；imo_grading 等；default=freeform）。
- `gan/framework/task_execution.py`（新）：`compute_report_summary()` → `label_distribution / invalid_rate / coverage / contract_ok / sample_n`。
- `gan/framework/loop.py`：`evaluator.evaluate(run_summary=...)` 注入 `report_summary`。
- `gan/build.py`：`_seed_self_designs()` 默认把评论类 `eval_points` 写入 evaluator 初始 design。

---

## R2 / R3 substrate 冻结、容错与重试

**问题**
- substrate/工具序列化缺陷会被 agent 当成"待修问题"，把进化方向带偏（20 代 run 反复改 `llm_withtools.py`/模型名并自伤出 503/model_not_found）；
- 429/503 在应用层**不重试**（backoff 异常元组不含 litellm 异常）→ 一题失败即整批 exit 1 → 整代 N/A；
- `resume` 把空预测当"已完成"，不会重试；无 fallback 模型；
- 冻结文件被拒时的 `DENIED (frozen substrate...)` 会被回传给 agent，反而引导它去动信任锚。

**原设计**
- `agent/llm.py` backoff 仅 `requests/JSONDecode/KeyError`（本地另加 `RateLimitError`）；
- `domains/harness.py: run_agent` 无 try/except；resume 用 `~isna()` 判完成；
- `request_source_access` 回传 `denied` 明细；`deny_paths` 硬编码在 `loop.yaml`。

**新设计**
- substrate = **冻结但可解冻**；拒绝告警**不外泄**（只审计）；
- 重试覆盖 rate-limit/5xx/timeout + `GAN_MODEL_FALLBACK` 兜底；
- harness **逐题隔离** + resume 修正；冻结清单**单一事实源**。

**具体改动**
- `gan/framework/frozen.py`（新）：`FRAMEWORK_GLOB` + `EXTERNAL_FROZEN` + `deny_paths()`（单一事实源）。
- `gan/framework/loop.yaml`：移除硬编码 `deny_paths`；`gan/build.py` 改从 `frozen.py` 取。
- `gan/tools/deep/request_source_access.py`：不再回传 `denied`（仅 `events.jsonl`/`broker.last_result`）。
- `gan/tools/design/set_param.py`（拒绝 `model`）、`set_config.py`（剥离 `params.model`）：模型选择冻结。
- `agent/llm.py`：backoff 扩到 `RateLimitError/ServiceUnavailableError/Timeout/APIConnectionError`（+ openai SDK 错误）；`GAN_MODEL_FALLBACK`。
- `domains/harness.py`：逐题 `try/except`（失败写 `evals/eval_failures.jsonl`、留空预测）；resume 空预测重试。
- `scripts/local/test_frozen.py`（新）：冻结布局不变式。

---

## R4 随机基线

**问题**
- 裸用原始分会把"从坏到瞎猜"当提升（paper_review random≈0.52）；
- 多域 `random_guess` 不同，`analysis` 直接平均原始分不可比；
- `report.py` 的 rg 在**过滤掉空预测后的 df** 上算，分母会漂移。

**原设计**
- `RewardPacket.numeric.benchmark` = 裸分；`analysis/plot_progress.py` 直接平均原始分。

**新设计（仅记录，未实施）**
- 有效分数 `excess = max(0,(acc−rg)/(1−rg))`；`rg` 在**固定请求样本集**上算；
- 图上额外标注随机基线；多域聚合用 excess。

**具体改动**
- 未实施。待办：`gan/framework/reward/packet.py` 增 `random_guess`/`excess`；选择与 `analysis/*` 改用 excess。

---

## R5 未评分、失败轮与版本管理

**问题**
- 全 0 并列时选择无梯度；失败代与真 0 分被混同；
- 评测崩溃（harness exit≠0）直接整代 N/A；
- planner/evaluator 是**单一可变实例**，`self_improve` 原地覆盖、无 archive、无回退。

**原设计**
- `task_runner` 失败返回 `None` → `inner_skip`（不入档）；
- `NodeValue` 仅 `score`；外循环无 checkpoint；角色树只记事件、不参与选择。

**新设计**
- 未评分分四类：pending / failed / partial / invalid；
- **失败轮 invalid**：记档但 `valid_parent=False`；bench 错误也传给 evaluator；
- **分数回退**：partial=有效均值+`coverage`；完全缺失=**同父兄弟中位数，不足 2 个取父代分**；imputed **不作 evaluator 锚点**；
- **外循环 checkpoint**：落盘/回退/接续（含角色设计快照 + RNG seed），不含父代选择；
- **角色设计接纳**：外循环无实测提升则回退角色 self-improve；
- 保留现选择算法（`gan/framework/tree/store.py` UCB）；**任务父代选择策略=冻结且可解冻**。

**具体改动**
- `gan/framework/tree/store.py`：`NodeValue` 增 `score_status`(`ok|partial|imputed|invalid`)/`coverage`。
- `gan/framework/task_runner.py`：产出 `score_status/coverage/report_summary/invalid_reason`。
- `gan/framework/checkpoint.py`（新）：`save/load/restore`（state + trees + designs）。
- `gan/framework/loop.py`：invalid 记档、imputation、`report_summary` 透传、每内循环/外循环 checkpoint、`resume`、`rollback_to_outer()`、角色设计快照+非提升回退；仅实测(ok/partial)计 improved。
- `gan/framework/task_execution.py`：`extract_score()`（NaN→None）、`compute_report_summary()`。

---

## R6 / R7 冻结边界、组件开放度与归属

**问题**
- "冻结"有两个来源（物理位置 + `deny_paths` 名单），已出现悬空引用（移动一半）；
- 组件与测量边界模糊；reward/task_runner 的归属未定；
- 设计落盘/胶水层散落在 `task_runner`。

**原设计**
- 冻结物散落 `gan/` 顶层（`loop.py/context.py/access.py/tree/reward/task_runner.py`）；deny 硬编码。

**新设计**
- **冻结 = `gan/framework/*`（结构性不变量）+ 显式 external**（`agent/llm*.py, agent/base_agent.py, domains/{harness,report}.py`）；external 不移动（避免依赖反转）；
- 组件放宽：`gan/components/`、`gan/registries/`、`gan/tools/` 在深改门控下可新增/编辑；
- `reward` 与 `task_runner` **全部冻结**；design 落盘 + 胶水层并入 `gan/framework/`；
- 目前**不存在**"改变评分口径"的组件 → `affects_scoring` 审批**暂缓**；
- 主实验冻结但**可解冻候选**：`loop.py`、`tree/*`。

**具体改动**
- **布局迁移**：`git mv` `gan/loop.py→gan/framework/loop.py`、`gan/context.py→gan/framework/context.py`、
  `gan/access.py→gan/framework/access.py`、`gan/tree/→gan/framework/tree/`（连同此前 `task_runner.py`、`reward/`）。
- `gan/framework/task_execution.py`（新）：design 落盘 + env/技能装配 + 仓库/patch + harness/report + `report_summary`。
- `gan/framework/frozen.py`（新）+ `gan/build.py`（deny 来自 frozen）+ `scripts/local/test_frozen.py`。
- 文档：`AGENTS.md`、`docs/GAN实现记录.md`（迁移 banner）。

---

## R8 逐条 issue 反馈链路

**问题**
- `judge_fix` **未调用**时 `fixed=False` → 被记成 `accepted_unfixed`（"已接受但未修复"），是对 planner 的**不实指控**，且污染 evaluator 自反馈与 2×2 统计。

**原设计**
- `classify_issue`: `fixed = bool(verdict.fixed) if verdict else False`；2×2 只有 4 格；digest 无"未判定"。

**新设计**
- 引入三态 `fixed ∈ {true,false,unjudged}`；`unjudged` 单列，2×2 只统计**已判定** issue；
- 历史反馈=**整体一条**（无"最近 N 条"上限）；不做框架级"未结项账本"，未结项由 evaluator 自然发现（并入当轮 issue 或升级为评估点）；`self._last_feedback` 单槽足够；遗忘用诊断（`events.jsonl`）而非强制；
- 两轮延迟保留（记录）。

**具体改动**
- `gan/framework/reward/evaluator_reward.py`：新增 `CELL_UNJUDGED`、`IssueOutcome.judged`、`classify_issue` 三态、`_LABEL_TEXT`/digest 单列（"not judged"）。

---

## R9 对照消融归属

**问题**
- 若让系统参与决定"比什么、怎么算"，进化可优化掉对自己不利的对照，结论不可比。

**原设计**
- 无成文归属。

**新设计（仅记录，不改代码）**
- **对外实验协议 + 插桩 = 冻结框架/外部**；**evaluator 内部判据（评估点/阈值）= 自进化**；二者不混在同一可编辑面。

**具体改动**
- 无代码改动；`scripts/local/test_frozen.py` 属框架插桩。

---

## 附：模型配置统一管理（已实施，v2：三部分 + 纯查表）

**问题**
- 模型配置散落 4+ 处（`agent/llm.py` 常量 / `loop.yaml` / `build` / `loop.params` / `task_runner` / domain utils / 多处 env），存在优先级语义重复、漂移、不可追踪；
- 模型名混进**可进化** design 的 `params.model`；
- `scripts/dgmh`、`domains/*/utils.py`、balrog/genesis 工厂各写各的默认。

**原设计（v1）**
- 优先级链 `explicit > env > domain > models[key] > models.task` + `fallback`；
- 域 task 默认放在 `models.yaml: domains.<domain>`。

**新设计（v2，最终）**
- `gan/framework/models.yaml` 分三部分，**唯一解释**：
  - `gan.{task,planner,evaluator}`；
  - `dgmh.{meta,task}`；
  - `domains.<role>`（仅**非 task** 的域专属角色）。
- `gan/framework/models.py` = **纯查表**（`resolve`/`resolve_section`/`describe`），**无 env、无 fallback、无优先级链**。
- 所有 **domain 的 task agent 共用驱动的 task 配置**，由驱动方解析后 **运行时显式传递**（不再是域属性、不再读 env）。
- 角色归并：**proof grader、balrog 策略 agent、genesis reward 合成 → 全部并入 task agent**；polyglot 的 aider CLI 为**独立**域角色（`domains.polyglot_aider`）。

**角色盘点（归并后）**

| 角色 key | 覆盖的 agent | 运行方式 |
|---|---|---|
| `gan.task` | GAN 的 task agent（paper_review/search_arena/imo/polyglot/balrog/genesis 的通用 task） | 驱动解析 → `--model` 显式传给 `domains.harness` |
| `gan.planner` / `gan.evaluator` | GAN 两角色 | GAN build 直接构造 |
| `dgmh.task` | DGM-H 的 task agent（含 polyglot run_task_agent） | 显式传参 |
| `dgmh.meta` | DGM-H meta agent（含原 polyglot meta、DGM baseline coding/diagnose） | 显式传参 |
| `domains.polyglot_aider` | polyglot benchmark 的 aider CLI（外部工具） | CLI 默认值 |

**具体改动**
- `gan/framework/models.yaml`：三部分结构；删除 `fallback`、`domains.<domain>` 的 task 默认。
- `gan/framework/models.py`：重写为纯查表（删除 env 别名 / fallback / 优先级）。
- `gan/build.py`：`resolve("gan.task"/"gan.planner"/"gan.evaluator")`；**删除 explicit 覆盖参数**（`task_model/…`）；写 `model_config` 事件。
- `gan/framework/{task_execution,task_runner}.py`：**不再设 `GAN_TASK_MODEL`**；改为把模型作为 `--model` 传给 `domains.harness`。
- `domains/harness.py`：新增 **必需的 `--model`**；`harness(..., model=)` 无 model 直接报错；删除 env/legacy 解析。
- `domains/report.py`：`report_imo_proof(..., model)` 与 `--model`（proof grader 现为 task 角色，仍显式传模型）。
- `domains/polyglot/harness.py`：`harness(..., model=)` → `process_entry` → `run_task_agent --model`。
- `scripts/dgmh/{run_meta_agent,run_task_agent}.py`：`resolve("dgmh.meta")` / `resolve("dgmh.task")`。
- `scripts/dgmh/generate_loop.py`：eval/report 传 `--model resolve("dgmh.task")`；meta 传 `resolve("dgmh.meta")`；启动写 `[model_config]` 日志。
- `scripts/run_gan.py`：删除 `--task/planner/evaluator-model`（不再有显式覆盖）。
- `domains/balrog/agents`、`domains/genesis/agents`：`AgentFactory` 改用 `config.model`；两份 `config.yaml` 增顶层 `model: null`，由 `domains/harness.py` 注入 `model=<id>`。
- `domains/polyglot/benchmark.py`：aider CLI `--model` 默认取 `resolve("domains.polyglot_aider")`。
- **`agent/llm.py` 清理**：删除全部 `*_MODEL` 常量、`GAN_MODEL_DEFAULT` 覆盖与 fallback 逻辑；`get_response_from_llm(msg, model, ...)` 的 `model` 变为**必填**，仅保留重试（不换模型）。`agent/base_agent.py`、`agent/llm_withtools.py` 同步去掉默认模型；`baselines/dgm/{utils,coding_agent}.py` 改用 `resolve("dgmh.meta")`。

**运行时的模型配置显式记录**
- GAN：`gan/build.py` 每次 build 写 `events.jsonl` 的 `model_config` 事件；每个 `Node.meta["model"]` 记录当次使用的模型。
- DGM-H：`generate_loop()` 启动写 `[model_config] {...}` 到 `generate_loop.log`。
- 域：`domains/harness.py` 收到的 `--model` 即最终值（显式传参，可追踪）。

**待定**
- `preflight()`（启动前模型可用性探测）尚未接入；
- 多域运行统一用单一 task 模型（已定）；若未来某域确需不同 task 模型，需在驱动方显式增加，而不是回到域/环境配置。

**关于 `scripts/local/test_frozen.py`**
- 冻结布局检查已从 `scripts/tests/` 移到 **`scripts/local/`（gitignored）**，作为**本地一次性检查**，不作为需要持续维护的仓库测试。
