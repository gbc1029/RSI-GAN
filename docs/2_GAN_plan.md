# 三角色自进化框架（GAN 式）—— 完整设计与实现方案

> v1.1 · 基于 HyperAgents(DGM-H) 代码调研定稿 · 2026-09-05
> 原始需求原文见文末「附录 A」；本文回答原始需求中的全部开放问题，并给出代码级实现方案。

---

## 0. 决策记录

| 决策点 | 定稿 | 备注 |
|---|---|---|
| 端到端验证域 | **paper_review 先行** | 数据集在仓库内、无镜像依赖；polyglot 需评测镜像，延后 |
| evaluator 源码可见性 | **按需授权**（默认只看轨迹+结果+diff 摘要；作弊检测点激活时授予源码） | 与 v1.1 源码门控统一为同一机制 |
| planner 优化目标 | **长期最优为主**，短期进展靠 staged eval + 每代账本可见 | 树管理/剪枝/算子预算全为长期搜索设计 |
| 修改深度（v1.1 修订） | **默认面=配置+算子；源码查看/修改需 agent 显式声明后传递**（门控机制） | 见 §2.4 |
| evaluator 外循环奖惩（v1.2 定稿） | **采纳/修复 2×2 矩阵 + 盲评校准协议**（替代 v1.1 占位） | 见 §2.3.3 与 §6 |

---

## 1. 总体架构与数据流

三个角色，全部构建在 DGM-H 的 `AgentSystem + chat_with_agent`（文本协议工具循环）之上：

```
                    ┌────────────────────────────────────────────┐
                    │              gan/loop.py（双循环调度）        │
                    └────────────────────────────────────────────┘
   外循环（每轮=元层一代）            内循环（每轮=task agent 一代）
   ┌──────────────────────┐        ┌─────────────────────────────────────┐
   │ evaluator.self_improve│        │ 1. tree.select(task树) → 父节点      │
   │ planner.self_improve  │        │ 2. planner.plan(父, 上轮反馈)         │
   └──────────┬───────────┘        │    → 配置写入/算子调用/[门控后]代码修改 │
              │ 触发: 预算尽 ∨       │ 3. 构建子节点容器+编译检查+评测        │
              │ 停滞 ∨ hard_failure │    (harness→predictions.csv→report)  │
              └──────────────────→ │ 4. evaluator.evaluate → RewardPacket │
                                   │ 5. tree.commit + 停滞判定             │
                                   └─────────────────────────────────────┘
```

- **task agent**：DGM-H 原生 `task_agent.py`，其进化树沿用 `archive.jsonl + gen_<id>/` 布局与 patch 链机制。
- **planner**：由 DGM-H `meta_agent.py` 演化，工作对象从"裸改代码"改为"配置+算子为主、代码为门控兜底"。
- **evaluator**：**全新角色**（DGM-H 中不存在 agent 形态的评估者，只有 harness 数值分），产出结构化文本反馈与评估点结论。
- 两元角色各自持有独立版本树、各自自改进化；**修改者=自身**（区别于 DGM-H 的 meta-agent "他者"模式）。

---

## 2. 设计→实现映射（设计侧：每个设计如何实现）

### 2.1 版本管理（三棵树）

**设计**：三角色均树状管理；task 树限分叉与深度；节点价值=score+potential+cost 三元组。

**实现**：
- 布局（在 DGM-H `outputs/generate_<run_id>/` 基础上扩展）：
  ```
  outputs/run_<id>/
  ├── archive.jsonl                 # 保留：task 树索引（向后兼容 gl_utils）
  ├── task_tree.jsonl / planner_tree.jsonl / evaluator_tree.jsonl
  ├── events.jsonl                  # 全局事件（含源码门控审计、限流、剪枝）
  ├── gen_initial/, gen_<id>/       # task 节点：容器工作目录+patch+评测（原样复用）
  ├── planner/gen_<pid>/            # config 快照 + patch + 自改记录
  ├── evaluator/gen_<eid>/          # 评估点快照 + patch + 自改记录
  └── runs/<inner_id>/              # 轨迹(chat_history)、RewardPacket、成本
  ```
- **task 树约束**：分叉 ≤ B=2、深度 ≤ D=5；停滞剪枝（连续 2 代 Δscore<ε → 标记 `stagnant`，禁选为父——复用现有 `valid_parent` 标志位）；达深度上限的分支做"血统重置"（从浅层祖先派生新链）。
- **父代选择**：改造 `score_child_prop`（其子代惩罚 `exp(-(n/8)³)` 的 8→B），加深度惩罚与停滞过滤。
- **节点价值**：`score`（report.json 数值分，确定性、防操纵）+ `potential`（evaluator 打分 1~10，只用于破平局与写入 planner 上下文，**不进选择公式**——防对抗污染）+ `cost`（token/墙钟/失败率）。
- **代码落点**：新增 `gan/tree/store.py` 包装 `utils/gl_utils.py`（读 `get_score/get_saved_score/select_parent/update_and_save_archive`，写扩展 metadata 字段）；`utils/git_utils.py` 的 `diff_versus_commit/commit_repo/reset_paths_to_commit` 用于全部角色的 patch 生成与版本推进。

### 2.2 双循环匹配

**设计**：外 1 次对应内几次？→ 预算制+停滞触发混合。

**实现**（参数进 `gan/config/gan_loop.yaml`）：
- 内循环预算 `I_max=3`；触发外循环条件（任一）：①预算用尽；②连续 `S_in=2` 次内循环 Δscore<ε；③evaluator 报 `hard_failure`（提前跳外）。
- 外循环一轮顺序：`evaluator.self_improve()` → `planner.self_improve()`（评估器先升级；两角色间反馈用 `schema_version` 解耦，允许乱序）。
- **代码落点**：`gan/loop.py` 主循环；停滞判定在 `tree/store.py`。

### 2.3 奖惩：统一 RewardPacket

**实现**（`gan/reward/packet.py`，dataclass+jsonschema）：
```json
{ "numeric": {"benchmark":0.42, "delta_vs_parent":+0.03, "cost_tokens":45000,
              "wallclock_s":900, "fail_rate":0.1},
  "textual": {"summary":"…", "weaknesses":[…], "suggestions":[…]},
  "penalties": {"cannot_run":0, "reward_hacking_suspect":0, "rule_violation":0},
  "schema_version":"v1" }
```
- **planner·内循环**："梯度"=weaknesses/suggestions；"学习率"=算子预算（每代 ≤`N_op` 个算子 + ≤1 次门控代码修改）。落点：planner 的 system prompt 注入上轮 RewardPacket.textual。
- **planner·外循环**：advantage 信号——其近 K 个孩子的 (score−parent) 滚动均值/方差；孩子 `cannot_run` 率高 → 惩罚其修改激进程度（收缩 `N_op`/提示词中的"保守模式"开关）。
- **evaluator·定位**：找出 benchmark"只看结果"无法发现的问题——**过程评价、作弊检查、细节问题发现、防止 task agent 过拟合 benchmark**；目标是"评判的准确与有用"，**而非拟合 benchmark 得分**。因此 benchmark 客观分**不在评估开始时给出**，只在 evaluator 完成盲评后揭示。
- **evaluator·外循环奖励（v1.2 定稿）** = 三部分：
  1. **问题采纳/修复 2×2 矩阵**（核心，见下表）——衡量 evaluator 提出的问题是否被 planner 接纳、是否被修复；
  2. **评估准确性校准**——盲评预测分与随后 benchmark 客观分的误差；
  3. **作弊检测误报惩罚**——`reward_hacking`/`rule_violation` 的误报计入负奖励。

  **2×2 矩阵**（"是否接纳"由**检查步骤**判定；"是否修复"由 **evaluator 从轨迹/代码判断**，不是"是否有改动"）：

  | 接纳 \ 修复 | 已修复 | 未修复 |
  |---|---|---|
  | **接纳** | `accepted_fixed`：有效发现+有效修复 → **强正 (+2.0)** | `accepted_unfixed`：问题有效、planner 认可但未修复 → **中 (+1.0)** |
  | **不接纳 + planner 有反馈** | 统一视作 evaluator 的**负反馈 (-1.0)** | 同左 |
  | **不接纳 + planner 无反馈** | 检查步骤发现并反馈 → evaluator 的**正反馈 (+2.0)** | 同左 |

  **检查步骤（内循环内执行）**：接收 evaluator 的评估 + planner 的反馈，向 evaluator 反馈"接纳/修复"结论。evaluator 在**内循环评估时可见上一轮反馈**；外循环据累积轨迹进化。
- **校准协议（内循环一轮）**：① evaluator **盲评**（不看 benchmark 客观分）给出评估分与问题清单 → ② **揭示 benchmark 客观分** → ③ 深入评估并产出 RewardPacket。校准奖励 = `-error_scale * |盲评分 - 客观分|`（滚动窗口可升级为相关性）。
- **成本反馈**：`agent/llm.py` 加 usage→cost 记录 hook（litellm `response.usage` 已有）；成本进 RewardPacket.numeric，并与两角色的算子预算挂钩。

### 2.4 修改深度与源码门控（v1.1 修订）

**设计**：agent 修改时基础允许**配置+算子**；**当 agent 声明需要查看/修改源码时再传递源码**。

**实现**——三级修改面 + 门控协议：

| 层级 | 内容 | 可用性 |
|---|---|---|
| L0 配置 | 节点 config（含 `custom:` 段）、提示词文件 | **默认可用**（读写） |
| L1 算子 | 预定义参数化操作（见 §2.5） | **默认可用**（调用） |
| L2 源码 | 指定路径的源码查看/修改 | **需声明后传递** |

- **声明机制**：工具 `request_source_access(paths, intent, reason)`。agent 必须先调用它声明要哪些路径、查看还是修改、为什么。
- **"传递"语义（真隔离，非提示词约定）**：角色工作区默认**不含任何源码文件**（只有配置树快照+算子注册表+[evaluator]轨迹副本）。grant 后由 `gan/access.py` 把申请的路径**复制进工作区 `src/`**（查看=只读副本；修改=可写副本，编辑完成后由 loop.py 对"授权快照 vs 修改后副本"做 diff 生成 patch 应用回节点仓库）。bash 逃逸口天然失效——文件不在工作区就 cat 不到。
- **审计**：每次 grant 的 (paths, intent, reason, 节点, 时间) 写入 `events.jsonl` + 节点 `metadata.source_access_log`；metadata 另记本代"最大修改深度"（0/1/2）与授权次数，作为价值判断特征（深修改需更高收益，供 planner 外循环参考）。
- **与 evaluator 决策的统一**：evaluator 的 `reward_hacking`/`rule_violation` 评估点激活时，评估点运行时自动发起该次评估的源码授权申请——"按需授权"成为门控协议的一个策略预设，而非独立机制。
- **策略门控**（`gan_loop.yaml`）：v0 自动批准+全量审计；可配置为限制（如每代代码修改 ≤1 次、某些路径黑名单）。
- **代码落点**：新增 `gan/access.py`（AccessBroker：grant 会话、工作区复制、审计）；`agent/tools/__init__.py` 的 `load_tools` 增加 `tools_dir` 参数（让角色能装载自己的工具集——唯一触碰上游 tools 机制处）。

### 2.5 算子化

**设计**：预定义改进操作，代码执行实际修改；允许进化中提出新算子；避免与配置修改重复。

**实现**：
- **算子即工具**：每个算子是 `op_info() + op_function()` 的 py 文件，注册机制完全复刻 `agent/tools/__init__.py` 的自动发现（新增 `gan/operators/registry.py`，同一套约定）。agent 侧只需知道算子签名与说明，无需读算子源码。
- **避免重复**：凡"纯参数改动"一律实现为"写配置"的薄封装（`apply_config(key,value)`）；真正做结构性代码操作只有 `swap_module`（模块重组）与 `code_edit`（门控兜底）。
- **新算子提出**：planner 自改时可以往 `gan/config/op_proposals.yaml` 写提案（名称+语义+参数 schema）；加载器扫描 proposals，若匹配到已有算子则映射参数，否则标记为待人工/后续实现的占位（v0 不自动生成代码，避免算子与配置重复膨胀）。
- planner 算子 v0：`set_prompt(section,text)`、`tune_param(key,value)`、`apply_config`、`add_config(key,value,rationale)`、`set_tool_enabled(name,bool)`、`swap_module(a,b)`、`code_edit`（门控后）。
- evaluator 评估点 v0（注册表 `gan/config/eval_points.yaml`）：`benchmark_score`（数值透传）、`trajectory_quality`（过程打分）、`hard_failure`（强惩罚）、`reward_hacking`（对照规则检查，触发源码授权）、`rule_violation`。evaluator 自进化=增删改评估点+改评估提示词。

### 2.6 可见范围隔离

**设计**：planner/evaluator 互不可见配置与源码；planner 收 evaluator 的"评估理由"；evaluator 只收 diff 摘要。

**实现**：
- **物理隔离 = 工作区复制**：`gan/loop.py` 为每次调用装配角色工作区（目录隔离）：
  - planner 工作区 = task 节点配置树 + 算子注册表 + 上轮 RewardPacket（**无 evaluator 配置/源码**）；
  - evaluator 工作区 = 轨迹(chat_history 副本) + predictions/report + **diff 摘要**（过滤后）+ 自己的评估点配置（**无 planner 配置/源码、默认无 task 源码**）；
  - P1-P2 在 host 上以目录隔离实现；P3 升级为独立容器 + mount 边界（复用 `utils/docker_utils.py` 的 volume_mount），文件不挂载即不可达。
- **diff 摘要生成**：`diff_versus_commit` 输出经 `filter_patch_by_files`（已有）+ 文件清单/算子清单摘要，**剥离自由文本 rationale**（防 prompt-injection 操纵通道）。
- **反馈 schema 版本号**：RewardPacket.schema_version 让 evaluator 的评估点演进不破坏 planner 的解析。

### 2.7 工具共享矩阵

| 工具 | planner→task | planner→self | evaluator→self |
|---|---|---|---|
| bash/edit（底层"手"） | 共享（门控后） | 共享 | 共享（门控后） |
| 算子池（"脑"） | planner 专属 | planner 自改复用 | **不共享**（评估点算子） |
| 轨迹/结果读 | 可 | 不可读 evaluator 侧 | 可 |
| 对方配置/源码 | 不可 | 不可 | 不可 |

**原则：共享手、不共享脑**——底层工具不携带设计信息；算子池才是设计知识。实现上：`base_role.py` 按角色装配工具集（`load_tools(names, tools_dir=…)`），两元角色的 operators 目录分开。

### 2.8 自进化与能力对齐论证

- **与 DGM-H 的区别**：DGM-H 是 meta-agent（他者）改 task_agent.py 与 meta_agent.py；本设计三角色各自"用工具修改自己的下一版"（与 DGM baseline 的 coding_agent.py 改自身完全同构），且 planner/evaluator 各自持有独立版本树、独立 reward；**evaluator 还拥有独立可验证的元 reward 候选（校准/覆盖）**——比"被别人改"更符合自进化定义。
- **能力对齐**：DGM 的论证是"改任务=改自己=改代码"。本设计中：planner 的"设计/修改 agent"与"改自己"同为改 agent 代码（对齐成立）；evaluator 的"评估（轨迹+代码+数据的批判分析）"与"自改（自己的评估代码+评估点+历史评估记录）"是**同类对象的同类操作**（对齐成立）。
- **实用性**：长期最优为主（见决策记录）；"从零一步规划出较优"作为树中特例分支保留（血统重置）。

---

## 3. 代码清单（代码侧：文件功能与复用情况）

### 3.1 复用不改（14 项）

| 文件 | 现有功能 | 在本设计中的复用点 |
|---|---|---|
| `utils/gl_utils.py` | archive.jsonl 追加、metadata 读写、`select_parent`（5 策略）、`setup_initial_gen`（仓库快照+排除规则）、`apply_diffs_container`（patch 应用+domains/ 过滤）、编译检查 | task 树存储与节点生命周期核心；`tree/store.py` 在其上包装 |
| `utils/git_utils.py` | `diff_versus_commit`（含未跟踪文件）、`commit_repo`、`reset_paths_to_commit`、`apply_patch` | 全部角色的 patch 生成/血统管理/门控副本 diff |
| `utils/docker_utils.py` | 容器构建/启停/复制/清理/volume_mount | task 评测容器；P3 的角色容器隔离 |
| `utils/common.py`、`utils/constants.py`、`utils/thread_logger.py` | 读文件、REPO_NAME、线程安全日志 | 通用；chat_history 即轨迹载体 |
| `agent/base_agent.py` | `AgentSystem(model, chat_history_file)` 抽象基类 | 三角色 Role 的父类 |
| `agent/llm_withtools.py` | 文本协议（`<json>{tool_name,tool_input}</json>`）工具循环，`max_tool_calls=40` | 三角色 agent 循环引擎（**不改**：它已接受任意 tools_available 列表） |
| `agent/tools/bash.py`、`edit.py` | bash 持久会话、edit 工具（`tool_info/tool_function`） | 三角色通用"手"（L2 授权后才进工具列表） |
| `task_agent.py` | TaskAgent 初始实现（chat→JSON 提取） | 进化起点；保持 `from task_agent import TaskAgent` 编译门兼容 |
| `domains/harness.py` | 动态加载 TaskAgent、逐题评测（线程池）、predictions.csv、resume | 内循环评测执行器（仅改 model 注入点） |
| `domains/report.py` | 数值打分 → report.json（overall_accuracy 等） | score 信号源，原样复用 |
| `domains/paper_review/*` | 50MB 本地数据集 + `curate_subsets`（纯本地 train/val/test 切分）+ utils（format_input_dict/QUESTION_ID/MODEL） | 端到端验证域（无镜像/HF 依赖） |
| `generate_loop.py` | DGM-H 原生单循环（容器编排/staged eval/归档） | 保留为对照基线；`loop.py` 参考其编排（不接入新框架） |
| `run_meta_agent.py` | meta-agent 单次执行→patch 落盘 | planner 角色执行的参考模板 |
| `Dockerfile`/`Dockerfile.orig`/`.dockerignore`/`.env` | 容器与配置基建 | 镜像重建时纳入 gan/ 包 |

### 3.2 改动（6 项）

| 文件 | 现有功能 | 改动内容 | 原因 |
|---|---|---|---|
| `agent/llm.py` | litellm 封装；模型常量硬编码；backoff(只捕 RequestException/JSONDecodeError/KeyError)；MAX_TOKENS=16384 | ①模型常量支持 env 覆盖（`GAN_MODEL_*`）②usage→cost 记录回调 ③backoff 增补 `litellm.exceptions.RateLimitError`（实测 Zhipu 429 直接抛） | 三角色用 glm；限流重试；成本进 RewardPacket |
| `domains/harness.py:79` | `model = utils_module.MODEL`（硬编码 gpt-4o） | 改为优先读注入的节点 config，缺省回退原值 | 节点配置决定 task agent 模型 |
| `utils/domain_utils.py` | 域注册全 if/else（score_key/splits/stagedeval/ensembled） | 函数体改为读 `gan/config/registry.yaml`，**函数签名不变** | 配置化首步；零风险兼容迁移 |
| `agent/tools/__init__.py` | `load_tools` 仅扫 `agent/tools/` | `load_tools(logging, names, tools_dir=None)` 增加可选目录参数 | 角色需装载自己的算子/门控工具集（唯一触碰上游工具机制处） |
| `.env` | 已有 OPENAI_API_BASE/KEY（glm） | 增补 `GAN_*` 运行参数（角色模型、门控策略开关） | 运行配置统一入口 |
| `Dockerfile` | 构建镜像（COPY 整仓） | 补充确认 gan/ 包与 configs 进入镜像（COPY . . 已覆盖，主要是镜像重建） | P4 前重建 `hyperagents:latest` |

### 3.3 新增（15 项）

| 文件 | 功能 | 依赖 |
|---|---|---|
| `gan/config/loader.py` | 默认+节点配置合并加载、jsonschema 校验、`custom:` 段读写 | registry.yaml, gan_loop.yaml |
| `gan/config/registry.yaml` | 域注册表（从 domain_utils 迁移：score_key/splits/stagedeval/ensembled/数据路径） | — |
| `gan/config/gan_loop.yaml` | 双循环参数：I_max/S_in/B/D/N_op/成本预算/门控策略/schema_version | — |
| `gan/config/prompts/{planner,evaluator}.md` | 三角色提示词（从代码抽出，成为可被算子修改的"配置"） | — |
| `gan/config/eval_points.yaml` | evaluator 评估点注册表（v0 五个评估点） | evaluator_ops |
| `gan/config/op_proposals.yaml` | planner 提出的新算子提案池 | planner 自改 |
| `gan/access.py` | **源码门控**：`request_source_access` 工具实现、grant 会话、工作区源码复制/回收、审计写 events.jsonl | git_utils, v1.1 修订 |
| `gan/tree/store.py` | 三棵树读写（jsonl 追加）、约束检查（B/D/停滞）、UCB+惩罚选择、节点价值三元组、兼容读 archive.jsonl | gl_utils |
| `gan/reward/packet.py` | RewardPacket schema（dataclass+校验）、聚合、版本化、落盘 | llm.py cost hook |
| `gan/reward/metrics.py` | **【占位待重评】** 校准/覆盖/公平元指标（仅数据落盘，不闭环） | packet, runs 记录 |
| `gan/roles/base_role.py` | `Role(AgentSystem)`：工作区装配、工具集按角色+授权级别装配、RewardPacket 收发、事件日志 | base_agent, access, loader |
| `gan/roles/planner.py` | `plan(parent_summary, last_packet)`→算子/配置/[门控]代码修改；`self_improve(history)`→改自身配置/提示词/算子提案 | base_role, operators |
| `gan/roles/evaluator.py` | `evaluate(run_artifacts)`→RewardPacket（跑评估点注册表）；`self_improve(SelfReview)`→评估点/提示词调整 | base_role, evaluator_ops |
| `gan/operators/registry.py` + `planner_ops/*.py` + `evaluator_ops/*.py` | 算子自动发现（复刻 tools 机制）+ v0 算子实现 | loader, access |
| `gan/loop.py` | 双循环调度：内循环（select→plan→build/compile/eval→evaluate→commit→停滞判定）、外循环触发、容器/工作区编排、事件日志 | 全部 |

---

## 4. 关键机制详设

### 4.1 树存储 schema（task 节点 metadata 扩展）
```json
{ "genid":12, "parent_genid":9, "planner_genid":3, "evaluator_genid":5,
  "prev_patch_files":[…], "curr_patch_files":[…],
  "valid_parent":true, "run_full_eval":false,
  "depth":4, "children":2, "status":"alive|stagnant|pruned",
  "value":{"score":0.42, "potential":7, "cost_tokens":45000},
  "modify_depth":2, "source_access_log":[{paths,intent,reason,ts}] }
```
planner/evaluator 节点同构（去掉评测字段，加 config_snapshot/prompt_snapshot/patch/self_report）。

### 4.2 源码门控协议（v1.1 核心）
```
agent 工作区（默认） = configs/ + operators 注册表 + [evaluator] runs 只读副本
1. agent 调用 request_source_access(paths=["task_agent.py"], intent="modify", reason="…")
2. AccessBroker 校验策略（路径白名单/每代次数）→ grant
3. 复制路径到 workspace/src/（查看=只读标记；修改=可写）→ 写审计
4. agent 在 workspace/src/ 内用 bash/edit 修改
5. loop.py：git diff(授权快照 vs src/) → patch → 应用到节点仓库容器（apply_patch）
6. 节点 metadata 记 modify_depth 与授权日志
```
（v0 host 目录隔离即可实现"文件不在即不可见"；P3 升级容器 mount。）

### 4.3 RewardPacket 流转
```
llm.py usage hook ──cost──┐
harness/report ──benchmark┤
evaluator 评估点 ──textual+penalties ──→ RewardPacket ──→ planner prompt（内循环）
                                        └→ runs/<id>/packet.json（账本）
planner advantage（外循环） ← 历史 packets + 树 score
evaluator 外循环奖励 ← 历史 packets（采纳/修复 2×2 矩阵 + 盲评校准）
```

### 4.4 loop.py 主流程（伪代码）
```
for outer in 1..G:
    for inner in 1..I_max:
        parent = tree.select(task_tree)                # UCB+约束
        workspace = assemble(planner, parent.config, last_packet)
        plan_out = planner.plan(parent_summary, last_packet.textual)   # 算子/配置/门控代码
        child = build_node(parent, plan_out)           # 复用 setup_initial_gen/apply_patch 逻辑
        compile_check(child)                           # 复用 run_commands_to_check_compilation
        scores, artifacts = run_eval(child)            # 复用 harness/report（paper_review host 直跑）
        packet = evaluator.evaluate(artifacts)         # 评估点注册表驱动
        tree.commit(task_tree, child, packet); events.log(...)
        if stagnated(): break
    evaluator.self_improve(collect_reports())          # 占位信号（v1.1 保留）
    planner.self_improve(collect_outcomes())           # advantage
    commit planner_tree / evaluator_tree
```

### 4.5 模型与密钥
- `.env`：`OPENAI_API_BASE=https://open.bigmodel.cn/api/paas/v4`、`OPENAI_API_KEY=<用户已填>`、`GAN_MODEL_TASK/PLANNER/EVALUATOR=openai/glm-4.7-flash`。
- 已知结论：glm-4.7-flash 为推理模型（content 需足额 max_tokens；管线 16384 足够）；429 限流频发 → llm.py backoff 修补 + "算子优先、代码门控"策略本身就是省 token 设计。

---

## 5. 分阶段实施计划（含验收标准）

| 阶段 | 内容 | 验收标准 | 量级 |
|---|---|---|---|
| P0 基建 | tree/store.py、config loader+registry.yaml（domain_utils 迁移）、llm.py 三处改造、tools/__init__ 加 tools_dir | 现有 paper_review harness 在改动后行为不变（回归） | ~0.5 天 |
| P1 骨架 | base_role/planner/evaluator（仅 L0+L1，无源码层）、RewardPacket、loop.py（1 外×2 内）、三棵树落盘 | paper_review 上产出 task 树 3 节点 + 两元层各 1 节点，events.jsonl 全程可追溯 | ~1 天 |
| P2 算子池 | planner_ops v0 六个 + evaluator 评估点 v0 五个（注册表驱动） | ≥3 个算子真实生效且 diff/配置变更落盘；评估点结论进 packet | ~1 天 |
| P3 门控与隔离 | access.py 源码门控（声明→传递→审计）、diff 摘要脱敏、（可选）容器 mount 边界 | 未声明时工作区无源码文件；声明后授权/审计完整；默认配置下 patch 可回放 | ~0.5 天 |
| P4 端到端 | paper_review 1 外×2 内全链路冒烟（glm 限流感知小样本，stagedeval=10） | archive.jsonl 与三树一致；每个节点 RewardPacket 完整；停滞/预算触发路径各演示一次 | ~1 天 |
| P5 元闭环 | planner advantage + evaluator 外循环奖励（2×2 矩阵 + 盲评校准）接入 self_improve | 两元层各产出 1 个新版本且可回滚；evaluator 奖励分解可按 issues 复算 | ~1 天 |

## 6. 风险与开放问题

1. **evaluator 外循环奖励（v1.2 定稿：采纳/修复 2×2 + 盲评校准）**：核心风险在于 Goodhart——evaluator 可能学会"提易修复的问题"或"给保守盲评分"来刷矩阵/校准。缓解：(a) `是否修复`由 evaluator **从轨迹/代码判断**且需给证据；(b) 采纳判定走**独立检查步骤**，planner 的反驳（有反馈）会带来负奖励，抑制 evaluator 乱报；(c) 校准用**误差**而非奖励拟合；(d) 作弊误报单列惩罚。后续可补充：人工抽检、跨 evaluator 版本 A/B。**内循环所需字段（textual/penalties）独立于外循环奖励**，可单独运行。
2. **源码门控的强度**：v0 host 目录复制隔离已满足"不传递即不可见"；容器化后 mount 边界更硬。bash 在工作区内仍可写 workspace/src（授权后的合法行为），不构成逃逸。
3. **glm 限流**：多轮 agent 循环在限流期不稳定——llm.py 补 RateLimitError 重试 + 算子优先（0-LLM 配置修改）双重缓解；`N_op` 预算可动态收缩。
4. **polyglot 评测镜像**：仍为后续网络任务（Docker Hub 语言镜像），不影响 paper_review 主线。
5. **算子提案到实现的鸿沟**：v0 只登记不自动实现新算子代码，避免"算子化"退化为"让 LLM 写代码"的原路；后续可评估让 planner 在门控+审计下实现自己的提案。

---

## 附录 A：原始需求（plan.md v1 原文）

> 主要任务：复用Hyperagent的代码，完成以下设计。
>
> # 总体框架
> 包含执行者task agent、规划者planner、评估者evaluator三个agent，以GAN为设计出发点，planner作为"生成"，evaluator作为"评判"，task agent视为生成的结果
> 执行者负责具体任务的执行，可由规划者修改；评估者的任务是对执行者的执行过程、结果甚至执行者本身设计进行评估和反馈，规划者根据反馈修改执行者；planner和evaluator均包含自进化能力，即可以修改自身设计，但是视为"自修改"而非hyperagent；
>
> # 细节设计
> ## 版本管理：三者均需要树状管理，taskagent的树应该限制分叉和深度；具体版本管理设计？节点的"价值"判断：直接的打分/评估内容，潜力判断，？
> ## 双循环结构：外循环为planner和evaluator根据轨迹进行一次自我改进，循环一轮即为planner和evaluator的"一代"；内循环是planner改进task agent、任务执行、evaluator反馈，一次循环即为task agent的"一代"；需要考虑外循环和内循环的匹配问题：外层一次改进应该对应内层几次改进？或者内层"进化停滞"后进行外层改进？
> ## 奖惩设计：借鉴"奖惩"设计，在实际执行中由数值化的损失函数与梯度下降转化为文字反馈与自主调整。规划者收到评估者评价和benchmark原始打分，内循环改进task agent，外循环改进自身。evaluater的奖惩：用于外循环改进自身。如何设计？需要纳入成本反馈；
> ## 修改深度：面向代码和面向配置（将harness设计整合为配置文件）并行：允许查看和修改源代码，但保留"配置文件"设计以便管理和浅层修改；允许添加新配置项和配置内容。操作算子化：预定义一些改进agent的操作，并允许进化过程中提出新的操作（新的算子），传入参数由代码完成实际修改；需要避免"算子操作"与修改配置完全重复；配置和算子作为触及代码层前的浅层修改。planner侧需要预定义一些算子操作并允许添加新算子（提示词修改/参数调整/拓扑/将两个harness的模块随机重组）。evaluator侧需要预定义一些评估点并允许在evaluator进化中添加新的评估点（benchmark得分/轨迹评价/强惩罚/奖励黑客/作弊）。
> ## 可见范围隔离：planner和evaluator互不可见对方的配置/源代码，以防作弊；planner接收evaluator的"评估理由"；evaluator是否应接收planner的修改/设计理由？evaluator是否应可见task agent的源码并对其评价？或仅针对task agent的执行轨迹和结果评价？
> ## 共享内容判断：planner修改task agent和修改自身的工具共享：能力对齐；planner修改自身与evaluator修改自身的工具是否共享：考虑hyperagent嫌疑和evaluator能力对齐问题；工具共享并不直接触及核心能力，但也会透露信息。
> ## 设计合理性论证：与hyperagent的区别：如何使planner和evaluator的进化是"自进化"？能力对齐问题：planner的"修改、设计agent"能力与自修改对齐，evaluator的自进化如何解释？实用性判断：规划者是在"持续迭代进化"中表现好，还是可以单次就规划出较好agent？
