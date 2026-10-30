# 17 — 小规模实验轮：运行记录、上限审计、三项修复、死键清理与新键消费者登记

> 批次：2026-10-28。对应实验：`/opt/rsi-gan/runs/polyglot/small_1028_0024`（暂停留档）、
> `small_1028_0046`（完成）。两 run 同 `base_seed=20261027`，锚核/评估集/每代抽题逐字节
> 一致，可直接对比。
> 本文档承载：①实验运行与结果；②全 seat 工具调用上限审计（"tools_max_call"澄清）；
> ③三项代码修复；④domains.yaml 死键清理；⑤问题登记（未修）；⑥"新键与消费者"的
> 文档记录方案与简单规划（代码侧候选留待讨论）；⑦锚核按语言比例抽样（§9，第四批）。

## 0. 摘要

- 小规模实验完整跑通：3 outer × 3 inner、9 主链代 + 3 届固定根评估，`driver_done ok`、
  核验 15/15、良性 ACL 噪声 1 行。主链 0.2→0.3，val_mean 0.4/0.2/0.3（峰值在第 1 届）。
- 全 seat 工具调用上限审计：`plan/evaluate/self_improve = 6/8/5`（loop.yaml 收紧值，
  代码回退默认 40/30/30）是真实瓶颈——§12.7 的 plan 会话 1/1 被 `max_tool_calls` 截断；
  task 侧 64/题按 launch 池化（×num_samples），实测峰值仅用 14%。经批准调至
  **16/20/12/96**（`--set`，未改 loop.yaml），截断率 1/1 → 1/9。
- 三项修复：①`respond_issue` 误标深改（gan/patch.py 浅集合扩展）；②`gan_cost_report.py`
  对评估轮 scope 崩溃的适配（评估成本 30–44% 首次可见）；③冒烟脚本校验前移 + 惰性
  `--subset` 移除。
- domains.yaml 死键清理：`splits` 全删（**9 处**，含 families 段）；`stagedeval_*`
  **保留**——澄清为 DGM-H 原生路径的活键（修正 docs/15 §9 的口径）。
- 问题登记（未修，见 §5）：`anchor_correct` 全程 null（polyglot 报告 schema 与 R4
  读取器不匹配）；新键无消费者（`cost_cm_mode` 案例，§6 给出文档记录方案与代码侧候选）；
  评估轮空 patch 拖累 val_mean。
- 锚核按语言比例抽样（第四批，§9）：主链 + 固定根评估两套锚核改**最大余数分配**
  （k=9 → js2/java2/go2/py1/rust1/cpp1，每语言保底 ≥1；原均匀抽取下 cpp 锚为空概率
  32%）；CSV 域均匀回退不变；`anchor_alloc`/`eval_anchor_alloc` 落 seed 文件与
  `sampling_seed_ready` 事件。离线测试 28+54+25 全过，工具回归七套全过。

## 1. 实验运行与结果

### 1.1 首启 fail-fast（参数耦合的教训）

首启（small_1028_0020）被 `gan/driver.py` L694-704 拒绝：`eval.anchor_k (18) exceeds
--num_samples (10)`。机制：root-side task broker 对**每次 harness 发射**（含固定根评估
轮）钉死 `num_samples`，超尺寸评估集会被 harness 重切片静默截断，故 fail-closed。
即 `--num_samples` 是**双重角色**：主链每代抽题数 + 评估轮规模上限。用户裁决改为
k=10 / N=10。

### 1.2 暂停的 small_1028_0024

运行 13 分钟（gen-0 完成：1 resolved / 9 unresolved），应用户"暂停实验"要求终止。
无 outer 检查点（outer 边界才落盘），不可增量续跑；工件留存作对比。进程树清理彻底
（driver/outer_worker/role worker/bwrap 全退，无残留容器）。

### 1.3 完成的 small_1028_0046

| 项 | 值 |
|---|---|
| 参数 | N=10 O=3 I=3 EVAL_SUBSET=val EVAL_ANCHOR_K=10 base_seed=20261027；上限 16/20/12/96（`--set`） |
| 判定 | `driver_done ok`；outer_done ×3；supernode_evaluated ×3；核验 15/15 |
| 主链逐代 | 0.2, 0.2, 0.2 / 0.3, 0.2, 0.3 / 0.3, 0.2, 0.3（coverage 全 1.0） |
| 固定根 val_mean | outer1 **0.4** → outer2 0.2 → outer3 0.3（评估轮空 patch 2/3/2，coverage 0.8/0.7/0.8，拉低数值） |
| 停滞 break | 0（patience=3 未触发） |
| 总量 | 1142 次调用 / 19,565,258 tokens（修正口径，见 §3.2）；LLM 墙钟 13,025s |
| 已知噪声 | teardown ACL reclaim [ERROR] ×1（docs/15 §12.8 良性竞态，summary 自动标注） |

## 2. 全 seat 工具调用上限审计（"tools_max_call"澄清）

无字面 `tools_max_call` 键；控制各 seat 调用次数的参数全集：

| Seat / 阶段 | 键（loop.yaml） | 原值 | 新值 | 代码回退默认 | 强制点 |
|---|---|---|---|---|---|
| planner·plan 会话 | `loop.plan_max_tool_calls` | 6 | **16** | 40 | `llm_withtools` L625 累计计数→`truncated`→receipt `budget.kind` |
| evaluator·blind | `loop.evaluate_max_tool_calls` | 8 | **20** | 30 | 同上（按阶段计数；reveal=max(6,blind//3)） |
| evaluator·reveal | 派生 | 6 | 6 | — | 同上 |
| 双角色 self-improve | `loop.self_improve_max_tool_calls` | 5 | **12** | 30 | `base_role.self_improve` |
| task 子进程 | `loop.task_llm_max_calls_per_question` | 64 | **96** | 64 | **按 launch 池化**：`64×num_samples`（driver L855 与 task_execution L2130 两处同式）→ proxy token `max_calls` 硬拒 |
| 防抖（不动） | `tool_repeat_limit=3` / `tool_call_timeout_s=600` | — | — | — | 连续相同调用熔断 / 单调用超时 |

实证（small_1028_0046）：主链每代 42–85 次、评估轮 91–134 次（池 960，峰值 14%）；
token 每代 296k–846k（每题 ~30–85k，远低于 300k 软停）——**call 上限与 token 上限均未
绑定**，截断率 1/9（§12.7 为 1/1）。旧值 6/8/5 是真实瓶颈：§12.7 唯一的 plan 会话即被
截断。附带发现：`inner_done.anchor_correct` 全程 null（§5.1），R4 判据在本 run 实际
退化为 benchmark.mean 口径。

## 3. 三项代码修复

### 3.1 `respond_issue` 深度误标（gan/patch.py）

- **现象**：9 代中 6 代 `modify_depth=2`（深改意图）但全部 no_commit、plan-seat 授权
  全部 `intent=view`、receipt 零 `edit_source`——"深改意图"与事实矛盾。
- **根因**：`has_deep_write` 对未知操作默认按深改计（fail-safe，防未注册深工具漏网）；
  planner 响应评估器反馈的簿记操作 `respond_issue`
  （gan/tools/work/planner/respond_issue.py L63 经 `ctx.record` 落账）不是设计算子，
  落入未知类 → 只要有评估反馈的会话必被判深改 → 调度空补丁 → no_commit。
- **修复**：新增 `_ALWAYS_SHALLOW = frozenset({"respond_issue"})`，`_shallow_design_ops()`
  返回 `(stems - _DEEP_BY_NATURE) | _ALWAYS_SHALLOW`（fallback 语义不变，文档串同步）。
  全库 `ctx.record` 操作名清点（13 个）确认这是唯一未分类操作。
- **验证**：分类单测 8/8；`run_tool_regression.py` 七套件全 PASS。

### 3.2 `gan_cost_report.py` 评估轮适配

- **根因**：固定根评估轮以独立 scope 发射（`eval_outer<N>_s<M>`，a8ce2dd 命名），其
  `scope_type` 仍为 `genid`；per-gen 表对 scope_id 做 `int()` 排序 → `ValueError`。
- **修复**：按 scope-id 形状分区（regex `^eval_outer(\d+)_s(\d+)$`）；gen 表只收主链代 +
  混合排序；评估轮归并**所属 outer**（task 桶 + `task_eval` 子桶，per-outer 总量补全）；
  新增独立评估轮表 + CSV 行；outer 表新增"其中 eval"列。
- **验证**：small_1028_0046 与 smoke_eval_20261027_170157 双 run 通过。
- **收益**：评估轮成本首次可见——1.52M / 1.38M / 2.79M tokens，占每届 task 成本
  **30–44%**（此前被静默丢弃，role 总量却含它们，口径曾自相矛盾）。全 run 修正口径
  1142 调用 / 19.56M tokens。

### 3.3 冒烟脚本（scripts/local/run_polyglot_smoke.sh，gitignore 不入库）

- `--subset` 移出命令（polyglot GAN 路径惰性：主链全池−val 抽题，subsets/*.json 只喂
  eval.subset 与 DGM-H CLI）；run_gan.py 的 flag 保留给 CSV 域。
- 空 `EVAL_ANCHOR_K`（EVAL_SUBSET 非空时）pre-sudo 可读拒绝（原会沿用 loop.yaml 默认
  30，撞 driver fail-fast）；`anchor_k ≤ NUM_SAMPLES` 镜像校验前移到 sudo 前。
- 默认值自洽修复：`EVAL_ANCHOR_K` 默认改随 `NUM_SAMPLES`（旧默认 2 vs N 默认 1 本就
  会被 driver 拒）。

## 4. domains.yaml 死键清理

| 键 | 出处 | 消费者 | 原始意图 | 处置 |
|---|---|---|---|---|
| `stagedeval_samples/frac` | default + 6 域段（balrog 未声明） | **活的**：`utils/domain_utils.py` L132-138 → `scripts/dgmh/generate_loop.py` L40/L652（DGM-H 原生多域循环） | docs/9（L78/L100/L122-133）两阶段评估：未跑全量时 `get_saved_score ×= stagedeval_frac` | **保留**。docs/15 §9"占位键无消费者"系 GAN 路径口径，已修正 |
| `splits` | default + 6 域段 + **families（balrog/genesis）共 9 处** | **全仓零消费**（两轮扫描；实际切分走 `--subset`/`eval.subset` → `load_dataset_frame` 按子集名拼路径） | docs/9 L78：每域自描述注册表的声明面 | **全部删除（用户裁决）**；balrog 两段本就未声明，"自描述"约定早已不完整 |
| `stagnation_epsilon`（loop.yaml） | — | loop.py L2423 有消费，R4 整题步长下退化 | 连续分数时代遗产 | **保留**（不在 domains.yaml 范围；判据回连续分即复活） |

清理后验证：YAML 解析正常、8 域 + 3 family 结构不变、`resolve_domain` 五条路径冒烟
（exact/family/longest-prefix）全过、`stagedeval` 值逐域完好；`balrog_minihack` 的
`score_key=None` 经 git stash 前后对照确认为**既有形态**（该 family 段本就不带，
resolve 只取最长前缀不叠 balrog 段——yaml L254 注释明言），与本次无关。

## 5. 问题登记（未修）

### 5.1 `anchor_correct` 全程 null（R4 在 polyglot 上失明）——**已修复（2026-10-28 第二批，方案 A）**

- 链路：`measured=True` → `_anchor_correct(genid)` 被调 → `self.anchor_ids` 非空
  （driver L949 读 sample_seed → build L296 → loop L288）→ `_read_per_question`
  （loop.py L1160-1174）返回 `{}` → None。
- **根因**：读取器期望 report.json 的 **`question_ids_passed`/`question_ids_failed`**
  （CSV 域/DGM-H schema）；polyglot 报告只有 `resolved/unresolved/empty_patch/...`。
- **影响**：R4 锚点停滞判据在 polyglot 上静默失效（本轮 improved 实为 benchmark.mean
  口径 + 抽题轮换噪声——gen8 "improved +0.1" 即轮换红利而非补丁效果）。
- **实施（方案 A：生产端补别名）**：`run_polyglot_eval` 的 report dict 增
  `question_ids_passed = resolved`、`question_ids_failed = unresolved ∪ empty_patch ∪
  incomplete`（未答按错计，排序确定性）——读取器保持单一 schema 契约，所有期望 CSV 域
  报告形状的读者一并受益（方案 B 的读取器回退不采纳，避免 schema 分歧进入框架）。
- **验证**：离线端到端（polyglot 报告形状 → `_read_per_question` 10 条每题记录 →
  `_anchor_correct=1`；empty_patch 计 failed 语义断言）；`run_tool_regression.py`
  七套件全绿。**待下一真实 run 确认**：`inner_done.anchor_correct` 非空且
  `improved_metric="anchor_correct"`。

### 5.2 新键无消费者（`cost_cm_mode` 案例）

- 事实：outer 2/3 planner self-improve 经 `add_config_key` 在
  `gan/design/schema_ext/planner.json` 声明 `cost_cm_mode`（enum 与保留键 `cm_mode`
  完全一致，consumer 填 `ctx_fetch`），补丁落 run 树；但**全树零 Python 引用**，
  9 个 receipt 的 `cm.mode` 全程 passthrough——键行为性死亡。
- 门禁为何放行：`add_config_key` 校验的是**注册存在性**（consumer 是已注册工具），
  非**行为性**（consumer 真的读它）；`heal_design_keys` 同谓词，故 heal 也放行。
- 意图重复：正确的既有通道是**保留 cost 键 `cm_mode`**（loop.py L2597
  `plan_ck.get("cm_mode")` → `session_cm_mode`）；planner 把值写在了顶层动态键上，
  且其自身 planning_policy 明文"键必须有真实读取者"——自我违反。
- **文档记录方案**：见 §6（本轮落地）。代码侧候选（§6.2）留待讨论。

### 5.3 评估轮空 patch 拖累 val_mean（观察）

每届评估 2–3 个 val 题无预测（empty_patch），按失败计入 resolved_rate，coverage
0.7–0.8。成因未查（child patch 未覆盖那些题的 solution_paths？预测行缺失？）。

## 6. 新键与消费者：文档记录方案（本轮落地）与简单规划

**约束（用户裁定）**：消费者检测不启用 AST 检测、不做运行时行为探针；以文档记录承载。
代码文件相关的一切留待本节完成后另行给出方案讨论（§6.2 仅列候选）。

### 6.1 台账（登记处，即本节）

| 键名 | 角色 | 声明来源 | 注册消费者 | 读取点 | 状态 | 备注 |
|---|---|---|---|---|---|---|
| `cost_cm_mode` | planner | small_1028_0046 outer2/3 self-improve（run 树 schema_ext/planner.json；repo 源为 `{}`） | `ctx_fetch`（仅注册存在性） | **无**（全树零引用） | **死键** | 意图与保留键 `cm_mode` 重复；建议清退或改用 `cost: {"cm_mode": …}`（loop.py L2597 消费） |

维护规则：今后每个经 `add_config_key` 声明的动态键在此追加一行；清退/接线时更新状态。

### 6.2 声明约定（已由算子强制，见 §6.5）

1. 声明键必须给出**读取点**——已从"约定"升级为 `consumer` 复合格式的必填段
   （缺读取点当场拒绝）；读取点真伪仍走 §6.3 手动审计；
2. 意图命中保留 cost 子键时**禁止声明**——已由名字碰撞硬拒强制（精确名与
   `cost_` 前缀影子都拦），正道 = `set_config(key="cost", value={…})`；
3. 声明前先查 §6.1 台账避免重复登记（纯约定）。

### 6.3 手动审计流程（grep 级，非脚本非 AST）

1. 收集各 run 树 `gan/design/schema_ext/*.json` 的全部键名；
2. 对 committed 树 `grep -rn "<key>" gan/ --include="*.py"`；
3. 零命中 → 死键候选，对照 `cost_keys.py` 保留键表查意图重复；
4. 裁决：清退（heal/手动）或接线（补消费者）；
5. 结果回写 §6.1 台账。

### 6.4 局限声明（接受）

`heal_design_keys` 谓词维持**注册存在性**——行为性死键不会被 heal 清除，只能靠本节
审计流程人工发现。这是"仅文档记录"约束下的已知残余风险，接受并留档。

### 6.5 实施记录（2026-10-28 第二批，代码已落地）

定稿方案 = **复用 `consumer` 字段承载读取点**（放弃独立 `consumer_read_site` 字段——
`heal_design_keys`/preflight 均不解析 consumer，只查 committed 树的 schema_ext 目录，
复合格式零下游兼容负担；receipt 白名单原样透传，无需改投影）：

| 文件 | 改动 |
|---|---|
| `gan/tools/design/add_config_key.py` | `consumer` 升级为 **`"<已注册工具>@<读取点>"`** 复合格式（两段必填、全长 ≤200）；工具名段走既有注册存在性检查；**保留子键名字碰撞硬拒**（`key` 精确命中或剥 `cost_` 前缀后命中 `cost_keys.spec_keys(role)`，消息引用保留键 consumer 并指路 `set_config(key="cost", …)`）；**enum 重合告警不拦**（与保留 str-enum 子键 Jaccard ≥0.75，返回文本附 WARNING）；spec/record 落复合串 |
| `gan/design/schema.py` | `_valid_spec` 拒绝 `tool@`（尾随空读取点）条目；**裸名（历史条目）容忍**——加载层零迁移 |
| `gan/design/seeds/planner.md` | 键扩展 bullet 重写：复合格式、碰撞规则、修正"会被拒绝/被 heal 清除"的失实承诺（heal 只清注册失效键） |
| `gan/tools/design/set_config.py` | tool_info 提示保留 cost 子键经 `cost` 槽设置 |

**用户裁定**：名字碰撞硬拒 + enum 重合告警不拦；不含 AST/运行时探针/提交期剥离
（候选 C/E 继续挂起）；离线审计脚本不做（§6.3 手动流程承载）。

**验证**：`regress_dynamic_keys.py` 62→**71 用例**（+9：裸名拒绝、空读取点拒绝、
`per_question_token_budget` 精确碰撞、`cost_` 前缀影子、`reasoning_effort` 精确名、
enum 告警放行、复合串落盘、record 断言、尾随空段加载拒绝）；`run_tool_regression.py`
七套件全绿。

行为变化（对 agent）：声明不带读取点 / 意图撞保留子键 → 当场拒绝并可读；合法复合
声明照常通过且读取点进 spec/事件/receipt（台账 §6.1 的"读取点"列改为从 `consumer`
`@` 后段转录）。对框架：heal/preflight/投影/加载老条目行为零变化。

#### 6.5.1 第三批：提交期静态引用门（2026-10-28，用户批准）

- **机制**：`schema_ext_report` 增 `unconsumed` 问题类——对每个已声明动态键，在
  **apply 后整棵 committed 树**的全部 `.py` 源（排除 `__pycache__`/.git，一次拼接）
  做**子串扫描**，零命中 → `(role, key)`；`_schema_ext_worsened` 增差分——**新增**
  unconsumed → 拒绝补丁（消息含键列表、动态拼名局限注记与出路）。两个调用点
  （check_patch / apply_code_patch）流程零改动（`_needs_registry_check` 恒真自动覆盖）。
- **用户裁定**：处置 = **拒绝补丁**（与 schema_ext 门 corrupt/invalid 语义一致；
  剥离重提实现重，不采纳）。
- **刻意取舍（简单检验边界）**：子串命中即算引用（注释提及也算，fail-open 向通过）；
  只扫 `.py`（读者=代码，json/注册表/knowledge 不算自我满足）；动态拼键名误拒
  （响亮可恢复，消息注明出路=写字面量常量）；**只拦新增**（存量不拦，差分原则）。
- **验证**：regress_dynamic_keys 71→**77 用例**（S5.1 六断言：零引用报告/拒绝含消息/
  存量不拦/引用清除/引用不拒/删读者重标记）；七套件全绿。

### 6.6 已知边界与残余消解分析（静态门落地后）

| 原残余 | 消解状态 |
|---|---|
| R1 读取点真伪 | **部分消解**：无读者类死键清零（声明+提交两道拦截）；指针指错降级为文档瑕疵（行为活键），核对仍走 §6.3 手动审计 |
| R2 事后死键 | **基本消解**：删读取者/改读取者致字面量消失 → 差分拒绝；余角 = 键名仅以注释形式残留（子串法分不清提及与真读，概率低，审计可查） |
| R3 意图重复但 enum 不同/无 enum | **不消解（正交）**——引用门管"有没有人读"，不管"意图是否重复"；缓解 = 声明前查台账 + 优先 cost 槽约定 |
| R4 候选 C/E | **销项**：C 已实现为"拒绝"形态；E 主用途被持续提交门覆盖（历史 run 树核查手动即可） |

静态门自身的两个新角（子串法固有，消除需 AST 级"复杂检验"，划界不做）：
**假通过**（键名仅以注释出现）；**假拒绝**（运行时拼键名，响亮可恢复——写字面量
常量即出路）。

净结论：残余从"全部靠手动审计兜底"收缩为 **两个低概率子串角落 + R3 语义层开放项
+ 指针核对手动流程**。

## 7. 遗留待批清单

1. §5.1 `anchor_correct` 修复（方案 A/B 二选一）；
2. §6.5 挂起候选：提交期剥离+事件（候选 C）、离线审计脚本（候选 E）——均非本轮约束内；
3. §5.3 评估轮空 patch 成因排查；
4. 下一轮实验定位：按本 run 结果迭代（如关停 planner 对 schema_ext 的再声明、观察
   prompt 精修线是否继续），或直接放大（docs/15 §12.7 阶段 3 档）；
5. 工作区遗留（docs/15、proofgrader_repo/、AGENTS.md.bak 未跟踪）维持待决策。

## 8. polyglot 补丁链路追查与本轮修复(2026-10-28 第三批)

### 8.1 追查结论:分数平台期的主因是补丁通道,不是任务能力

对 small_1028_0046 任务轮 90 次作答逐题审计(`runs/<genid>/predictions.csv` +
`work/<genid>/repo/outputs/gan_polyglot_0/*_docker.log` + `report.json`):

| 层面 | 数量 | 说明 |
|---|---|---|
| 补丁被 eval 容器 `git apply` 整体拒绝 | **31** | 无条件 0 分;docker log 各一行 error,9 代无人察觉 |
| 真空白补丁 | **1** | gen5 go__kindergarten-garden(§8.2 类④) |
| 补丁落地 | 58 | 其中 22 解决(38%) |

按语言的未落地分布:python 19/25(pyc)、rust 5/6(cargo target/)、cpp 4/6、
java 2/10(无尾换行拼连)、javascript 1/26(package.json 冲突)、go 1/17(空白)。
锚题 js__book-store 7/9、js__space-age 8/9 决定了 0.2 底座;`go__beer-song`
9 落地 0 解决为纯任务短板。若 31 个补丁按落地补丁 38% 解决率折算,期望总分
≈0.35 而非实测 ≈0.24。

### 8.2 四类根因(全部实证到源码级)

1. **运行副产物混入补丁**(python `.pyc`×19、rust `target/`×5、js
   `package.json`×1):child 沙箱有部分工具链(python/cargo/npm),agent 跑测试
   属合理自查;生成器对整棵工作区做 diff,排除名单只有 `.gan_runtime`;二进制
   经 `errors="replace"` 解码后 NUL 字节存活(NUL 是合法 UTF-8),`git apply`
   按 binary 拒绝 → corrupt patch。
2. **无尾换行骨架 → difflib 拼行**(cpp×4、java×2):`space_age.h` 等 starter
   无尾换行,difflib 无 no-newline 概念,裸发末行与下一物理行拼连,hunk 行数
   与声明失步;同题跨代错误行号恒定(space-age L70、bank-account L63)——
   骨架特征,与 agent 行为无关。**归属:框架序列化缺陷**(agent 新文件均以
   换行结尾,内容合法;同一内容经正确生成器 100% 可落地)。
3. **评测端原子拒绝 + 静默吞错**:`git apply` 原子性本身合理(拒收畸形、防
   部分应用),但唯一校验点在评测容器内,失败仅一行 docker log;生成器侧
   从不预检。
4. **空白补丁**:gen5 kindergarten-garden,5 次写文件(含 bash heredoc)全部
   触发 `should_retry_tool_use` 的 "Output context exceeded"(旧 len≥2000
   启发式把"闭合但含花括号内容的 json 块"误判为截断),重试耗尽后无任何
   文件写入 → 空 diff。

反事实验证:31 个被拒补丁经"滤产物 stanza + 按 hunk 起始行重放重建(逐行
`\ No newline at end of file`)"后,**31/31 通过与评测容器同款的
`git apply --check`**(种子仓库中原始补丁全部复现被拒)。

### 8.3 本轮修复(全部已落地并测试)

决策(已确认):git 建仓序列化 / 只 diff solution / 预检+自动救援 / 分母不变
+独立列 / 仅本仓框架代码 / 收紧判定+分段写工具。

| 修改 | 文件 | 内容 |
|---|---|---|
| 序列化重写 | `domains/task_worker.py` | `_polyglot_prepare` 建 patch-base 仓;`_polyglot_workspace_patch` 改 `git diff --cached`(pathspec 限定 solution_paths);NUL 哨兵 + binary stanza 丢弃;`.git` 缺失响亮降级到硬化 difflib 路径(逐行 marker) |
| 预检+救援 | `gan/framework/polyglot_patch_repair.py`(新)+ `task_execution.py:run_polyglot_eval` | 父侧种子仓(每练习缓存,`--check` 只读全 run 复用)预检;救援=滤非 solution stanza + 重放重建;ok/repaired 才分发;rejected 不分发照旧裸评 |
| 读取层 | 同上桥 | **predictions.csv 从 pandas 换 stdlib csv**——pandas C 解析器在字段首个 NUL 处静默截断(field gen0 三个 python 补丁被读丢 ~97% 字节) |
| 报告+投影 | report.json + `compute_report_summary` + `task_runner` | 每题 `patch_apply{status,gate_error,dropped_files,rebuilt_files}` + 聚合 `patch_apply_summary`;run_summary 投影显式放行聚合(默认拒式扩列,一事件 `question_patch_apply`);resolved/unresolved/分母语义零变更 |
| 截断判定 | `agent/llm_withtools.py` | 结构化判定:未闭合标签或 depth>0(未消费左括号)才判截断;闭合块交 malformed 路径;连续 5 次强制收尾 + `no_answer` 事件 |
| 分段写 | `gan/components/task/tools/write_file.py` | `mode='append'`(冻结层 `resolve_writable` 收紧 confinement,可写集不扩) |

测试(全绿):`test_polyglot_patch_builder.py` 14(三类损坏 + 字节回环 + 删除
+ 降级路径)、`test_polyglot_patch_repair.py` 9(含 field replay **31/31**)、
`test_truncation_and_append.py` 17、`test_polyglot_gate_wiring.py`(gen0 实况
回放:ok=5 repaired=5 rejected=0,分派集=清关集);`run_tool_regression.py`
七套件全过。

### 8.4 刻意取舍

- **eval 端不用 `--reject`**:部分应用会污染归因;救援只在父侧、只含清关补丁。
- **不改基准 starter**(补尾换行):数据保真,git 序列化天然消化。
- **patch_apply 不进 resolved_rate 分母**:跨 run 可比性优先,归因靠独立列。
- **repair 只做可证修复**:old 侧重放与 starter 逐字节不符即弃权转 rejected,
  不发明内容;救援次数本身是生成器健康报警器。

### 8.5 遗留与消解

- §7.3(评估轮空 patch 成因)**部分消解**:任务轮空 patch 根因已定位并修复
  (§8.2 类④);val 轮空 patch 是否同机理待下一 run 的
  `question_patch_apply` 事件与 no_answer 事件确认。
- ~~go 工具链缺失~~(**已执行,2026-10-29**):主机烘焙 go 1.21.5(与评测镜像
  同版本,aliyun 镜像)+ openjdk-17(java 同病同治,javac 此前也缺失);沙箱为
  主机只读投影,`/usr` bind 自动覆盖,零沙箱配置。沙箱内离线冒烟:`go test ./...`
  与 `javac` 全链路通过(stdlib-only 练习,`GOTOOLCHAIN=local`/`GOPROXY=off`
  钉死离线行为,go 缓存落沙箱私有 tmpfs 不进补丁);E2E:agent 真跑 go test 后
  补丁仍 solution-only、eval 侧字节级一致。部署说明进 README §3b+。**待下一
  run 验证 go/java 落地解决率变化**(基线:go 2/17=12%,java 3/10=30%)。
- 过程事故留档:接线测试曾误把 field `runs/0/` 当输出目录,`report.json` 被
  覆盖后已按会话留档原文恢复(内容/属主/权限;mtime 无法复原),
  `predictions.csv` 等其余证据未受影响。

## 9. 锚核按语言比例抽样（2026-10-29 第四批）

### 9.1 背景与量化

- 锚题机制盘点：两套锚核——**主链** `sampling.anchor_k`（`ensure_sampling_seed`
  每 run 一次抽入 `ckpt/sample_seed.json`，`select_seeded_positions` 每代先占位、
  不轮换）与**固定根评估** `eval.anchor_k`（`eval_anchor_ids`，每届 `fixed_ids`
  全量钉值）。同一"锚先占 + 半额 cap + 余量重抽"策略存在两份运行时拷贝
  （`task_execution.select_seeded_positions` 与 `domains/harness.py:_select_samples`；
  后者运行在无 `gan/` 的沙箱 run-dir 内，物理隔离不可互相 import，归一化 diff
  确认当日逐行等价）。
- 主链锚核抽取原本**语言盲**（207 池 = 225 − val18 上均匀按 id；池构成
  js45/java43/go36/py31/rust28/cpp24）。标准命令 `num_samples=18, anchor_k=10`
  → k=9 冻结锚核。蒙特卡洛（2 万次）：**P(cpp 锚=0)=32%、rust 27%、python 23%、
  go 18%**；关键不对称在轮换半区按 genid 重抽、跨代平均抵消，而锚半区偏差
  `(A_l−k·p_l)/18` **逐代恒定、永不消退**——3000 个 20 代 run 中 100% 至少一个
  语言 run 级份额偏离名义 >3pp（最大 ~15pp）。下游 `_anchor_correct`（R4 停滞
  判据，未答计 0）与 `benchmark.mean` 分母继承同一偏斜。
- 对照：评估侧 val.json 本身按比例构造（docs/15 §12.6：go3/rust2/cpp2/java4/js4/
  python3，语言内按 instance_id 等距取样）——"按语言配比"在评估集上已是明确设计
  意图，主链锚核未继承，本批补齐。

### 9.2 设计（本批落地）

- **策略**：两套锚核抽取改为**按类别比例分配**（最大余数法，`proportional_allocate`）：
  各语言先取 `floor(k·n_l/N)`，余票按 `(k·n_l) mod N` 降序、同票按语言名字典序分配
  （全确定性）；语言内用同一 `random.Random(f"{base_seed}:anchor")` /
  `f"{base_seed}:eval_anchor"` 均匀抽取——**种子契约字符串不变**，resume 确定性保持。
  k=9 时分配 js2/java2/go2/py1/rust1/cpp1，**每语言 ≥1 有构造保证**。
- **类目解析**：`_pool_categories`——polyglot 走 `gan_utils.load_language_map`
  （新公开访问器）；其余域无类目列，**保持均匀抽取回退**（CSV 域行为不变，
  `anchor_alloc={}`）。评估侧 val 子集 id 经同一 metadata 解析，一个解析器服务两核。
- **审计**：`anchor_alloc` / `eval_anchor_alloc` 落盘 `sample_seed.json` 并进
  `sampling_seed_ready` 事件（gan/driver.py）。
- **不变式**：`sum(alloc)=k`；`alloc[c]≤n_c`；`|alloc[c]−k·n_c/N|<1`；k 超池钳制；
  k=0 → 空。`select_seeded_positions` 的 `num_samples//2` cap 在 k≤cap 时为 no-op。

### 9.3 实现与测试

- 改动文件：`domains/polyglot/gan_utils.py`（`load_language_map`）；
  `gan/framework/task_execution.py`（`proportional_allocate` / `_pool_categories` /
  `_stratified_sample` 三个原语 + `ensure_sampling_seed` 双核重接 + docstring）；
  `gan/driver.py`（事件字段）。`harness._select_samples` **不改**——它只消费
  `anchor_ids`，不执行锚核抽取。
- 离线测试（无网络/无 LLM/无 docker，`venv_nat/bin/python`）：
  - 新 `scripts/local/test_anchor_proportional.py`（gitignore 不入库）：**28/28**——
    分配单元（精确分配/同票字典序/k 钳制/k=0..29 不变式）；主链核（207 池、k=9
    手推期望精确匹配、无 val 泄漏、跨 run 目录确定性、cap no-op、幂等）；评估核
    （k=10 比例、k=18 全集且分配==构成、k=30 钳制、缺文件空核）；CSV 回退
    （子集内 + alloc={}）；小 k；均匀回退**逐位等于**历史抽法；seed 文件审计字段。
  - 回归：`test_polyglot_sampling.py` **54/54**、`test_polyglot_val_eval.py`
    **25/25**、`run_tool_regression.py` 七套全过（deep-tools / batch6 / batch8 /
    c-dispatch / dynamic-keys / ledger-gate / session-universe）。

### 9.4 边界与遗留

- **既有 run 不受影响**：seed 文件 skip-if-exists，`small_1028_0024/0046` 的锚核
  原样保留；比例抽取只对**新建** run 目录生效。混用新旧策略目录做跨 run 对比时
  锚核集合不同——与任何 anchor_k/num_samples 变更同一性质，对比时需声明。
- **cap 截断池序问题仍在**（两份拷贝同构；仅"同目录缩小 num_samples 重跑"触发）：
  比例分配给了每语言 `floor(k·n_l/N)` 保底，截断的语言系统性被大幅缓解；彻底修复
  （随机截断序 + 两实现等价合同测试）另行批次。
- **类目推广**：`_pool_categories` 当前只接 polyglot language；CSV 域将来若引入
  类别列（如 accept/reject 配比），沿同一 `_stratified_sample` 通道扩展即可。

## 10. 设计面读取与写闸修正批（2026-10-30）

> 起因：`small_1029_231632` outer_1 的 plan 会话（`trajectory/outer_1/roles/planner/
> plan/gen_0.jsonl`）三次 `set_prompt` 全部被 R2 闸门拒绝（0.11 → 0.10 → 0.08，阈 0.50），
> planner 为"找到被比较的 incumbent 文本"耗尽工具预算，prompt 改进推迟到下一代。
> 离线复核确认：三次拒绝的判定**实质正确**（真实保留率仅 0.29/0.19/0.15），但机制存在
> 三处缺陷——参照文本对写入者不可见、`ratio()` 与长度耦合（385 字符基线下新文
> ≥1156 字符数学上必败）、报错文案与度量语义错位。本批一次提交修复四项。

### 10.1 `get_config` 设计读取工具（新增 `gan/tools/design/get_config.py`）

- 返回本会话所编辑设计的完整 config（读 `DesignContext`，**与闸门比较的 `prev` 同源**，
  构造性消除"测 config、读 seed 文件"的错位）；返回 gen-0 seed 全文与
  `prompt_equals_seed`——"逐字写回 seed 永远放行"的逃生通道首次对 plan 席位可执行；
- 返回 **keys 视图**：静态键 + 动态键目录（经 `session_overlay_root` workspace-first
  解析，与 `set_config` 校验语义对齐，本会话新声明键即刻可见）+ allowed/present/
  unset_allowed；config 面首次可枚举；
- 作用域由 `ctx.role` 天然决定（plan→task、self_improve→自身、evaluate 无 ctx 自然拒绝）；
  纯读：不过 shallow 写闸、不落 record（读不是变更）；无文件/授权/ledger 面。

### 10.2 工具报错四态分类（仅报错分支，行为零变更）

- 分类器 `gan/framework/workspace.py:path_class/access_hint`：与 `AccessBroker.grant`
  同序同源（`deny_paths` → `is_allowed(view)` → `is_allowed(modify)`），四态 =
  **确实不存在 / 可读可写未授权 / 仅可读未授权 / 角色读集外**；读集外一律不回答
  仓库存在性（保留"不披露"决定的意图，只描述请求路径本身的类别）；
- 接线：`read_file` / `list_dir` / `grep`（含空工作区显式声明，消灭静默 `(no matches)`）/
  `edit_source(view)` 未命中拦截（`agent/tools/editor.py` 基座文案不动）；
  `write_auth.authorize_write` 的"未被授权覆盖"分支保留 "not covered by any grant"
  事实前缀并追加席位可写性说明（消除对仅可读席位"必然失败的 modify 重试指引"）；
  `request_source_access` 分桶回显（outside / readable-not-writable-here / no repo
  match，来自既有 `last_result` 审计面，零新 RPC）。

### 10.3 R2 闸门改保留率（survival）语义

- `prompt_rewrite_error`（`gan/tools/design/set_prompt.py`）主判据改为
  **M/len(prev)**（同一 SequenceMatcher，`ratio()` 内部即由 matching blocks 计算，
  零额外成本）；旧键 `prompt_rewrite_min_similarity` 作回退；新增显式增长上限
  `design.prompt_rewrite_max_growth`（默认 4，0=不限，把旧 ratio 隐式的防膨胀副作用
  显式化）；
- 实测对照：本 run 三次尝试 survival 0.29/0.19/0.15 仍拒（判定不变）；"逐字保留 seed
  + 追加"从 ratio 0.409 拒 → survival 1.0 过（被误杀的唯一一类 = 闸门自己建议的追加）；
- 镜像链同步（`build.py` → `AccessBroker` → `broker_info` → 校验集 → `_RemoteBroker`
  → `context.design_limits`）——**精确字段集校验**，漏一处子进程读 0.0 闸门静默失效
  （B1 教训同型）；
- `prompt_change_facts` 新增 `char_survival` 数字事实并**加入 `PROMPT_FACT_KEYS` 白名单**
  （默认拒绝投影：不加白则决策面与离线分析器不可见）。

### 10.4 seed 收权：出厂默认值归框架

- `frozen.py` 从 `PLANNER_SELF`/`EVALUATOR_SELF` 删除自家 seed 文件（2 行）；三个
  seed 统一为**任何角色不可读不可写**；框架侧读取（build 装配 / gen-0 / 回退参照）
  直读代码树、不经 broker，不受影响；
- 关闭的洗白路径：self_improve 席位曾可 `edit_source` 改自家 seed 文件（deep 通道无
  相似度闸）→ `set_prompt(改后文本)` 被"写回 seed"豁免放行；seed 运行内不可变后该
  引用稳定。既有设计 config 权威、永不重置（`_seed_self_designs` 仅补缺失），
  seed 文本修改只影响未来新 run 的 gen-0；
- 层位语义：seed = 框架所有的出厂默认值（变更走维护者 PR），config = 角色所有的
  可演化状态（set_prompt/set_config 唯一写入口）；角色侧 seed 唯一投影 =
  `get_config` 的 `seed` 字段。

### 10.5 配套与定性记录

- 回归适配：`check_record_contract` roster 将 `design/get_config.py` 归入 EXEMPT
  （纯读面；并修正 REQUIRED 前缀匹配先于 EXEMPT 的判定顺序），B19 断言更新为分类
  后消息；六套件全绿 + `test_frozen.py` 通过；
- **CM 块定性**：seed 中的"上下文管理与预算"块经逐环核实为 **seed 文件内的初始设计
  内容**（f6e4248 提交写入），**不是**运行时代码注入（运行时无任何往 prompt/消息拼
  CM 文本的代码；CM 机制只在消息层改写、永不回写 config）。按定义不属漏洞；其
  "保留税"是闸门对 incumbent 的一致判定，且该块自描述为条件性（未装配工具时
  "直接按任务作答"），自中和；
- **既有接受项记录**：任务输出格式契约块（`task_agent.py` 的 JSON schema 段）位于
  被演化工件源码内、plan 席位可经 deep 修改——防护为解析失败→无效→score null 的
  结构性自罚 + 评分器在信任锚内 + 补丁过程可见，定性为有意接受，不加闸；
- **已知残留**（有意保留）：会话内链式漂移边界（逐写保留 ≥50% 的渐进重写合法，现行
  ratio 下同样可行）；`list_editable` 不加 read_only_here 分节（读写不对称靠 C 类
  报错兜底）。

## 9. small_1030_095410 审计与四处修复（2026-10-30 第四批）

全量审查 polyglot 小规模轮 `small_1030_095410`（1 outer × 3 inner + 固定根评测 +
双角色 self-improve）的运行记录，确认四处框架级缺陷并修复；其余发现归因为
agent 侧使用错误或设计行为（审查会话记录），本轮不动。

### 9.1 渲染视图截断无标记（F1）

- **表现**：`read_trajectory` 默认 `max_chars=6000`，`trajectory._render` 对整条
  轨迹渲染流按字符数硬切、切口无任何标记——"视图被切"与"数据就这么多"不可区分。
  评估器在单次默认 cap 读取上得出 6 条伪问题（3 条 severity=high）：把 6000 切口
  逐字读成"文件被截断"（i-4 "forth.go 截断于 `func applyBuiltin(...)`"、i-9
  "记录终止于第 4 个 bash 调用"内部）、"只有 1 次工具调用"、"写后无 bash 验证"、
  "3 个工具调用的会话"。原始轨迹全部完整（write_file 4502 字符完整收尾；真实会话
  13–22 次工具调用）。模拟验证：cap=6000 的切口与评估器引用逐字吻合；30k/120k
  渲染远超该点——其"30k/120k 复核切口相同"的声明为假。
- **修改**：`_render` 切断时追加 `...[render truncated at max_chars=N]`（只标注
  视图上限，不改脱敏语义；`read`/`read_session`/`read_trajectory`/
  `read_session_trajectory` 各决策面同享）。

### 9.2 outer-eval 任务轨迹不归档（F3）

- **表现**：固定根评测轮（`eval_outer<N>_s<M>`）的 task 子轨迹从未归档——
  `run_once` 不调 `trajectory.collect`，过程证据只存在于临时 `work/` 的未脱敏
  `chat_history`（侥幸存活：`keep_workdirs` 清理分支不覆盖 eval genid）。评测轮
  `python__phone-number` 空补丁（malformed 链）的现场因此只能在 work/ 取证。
- **修改**：`loop._evaluate_supernode.run_once` 在拿到 child 后走与主线相同的
  `trajectory.collect`（落 `trajectory/outer_<O>/eval_outer<N>_s<M>/task.jsonl`
  + `.llm_calls.jsonl` sidecar，脱敏一致）。评测轮不是决策代：失败 `soft_fail`
  （事件 `eval_task_trajectory_archive_failed`），不像主线那样硬失败。
- **未动**：eval 记录保存位置（`outer_<N>_eval` 独立树方案）与 eval planner
  会话/成本隔离，留待下一轮决策。

### 9.3 events.jsonl `time` 字段双时区（F6）

- **表现**：同一 events.jsonl 混用两种时区——loop/沙箱侧进程看到合成 `/etc`
  （`Etc/UTC` 设计），`localtime` 产出 UTC（如 `outer_start 01:54:45`）；driver
  侧进程在宿主机（+08），产出本地时间（如 `driver_done 10:45:42`、
  `eval_subset_resolved 09:54:21`）。`ts` 一致、`time` 各说各话。
- **修改**：`utils/trajectory_log._readable` 与 `llm_proxy._audit` 的可读时间固定
  `time.gmtime`（UTC）——不依赖任何进程的 TZ 视图，driver/loop/沙箱三个写出方
  统一；格式不变，`ts` 仍为权威。沙箱 `/etc` 的 `Etc/UTC` 设计保持不动（不向
  凭据无关的合成 /etc 投影宿主时区）。

### 9.4 任务沙箱 JDK 不可用（W4 框架部分）

- **表现**：polyglot java 题在任务沙箱内无法自检——`java -version` 报
  `InternalError: Error loading java.security file`。实证机制：Debian 的 OpenJDK
  把 `conf/security/{java.security,java.policy,nss.cfg}` 做成指向
  `/etc/java-17-openjdk/security/` 的符号链接，而合成 `/etc` 不含该子树 → 悬空
  链接。任务 agent 因此进入 apt 安装螺旋（13×120s bash 超时的间接推手之一；
  评测侧 docker 的 java 是好的，两侧能力不对齐）。`/usr/lib/jvm` 里 JDK 完整，
  只差 /etc 侧 5 个真实文件。
- **修改**：`sandbox_etc.create_sandbox_etc` 可选投影 `/etc/java-*-openjdk`
  子树：逐文件解引用、`_trusted_file_bytes` 信任校验（root 属主、限定 /etc 根）、
  0444 root-owned 重建；目标逃出 /etc 或信任失败的源**跳过不跟进**（可选投影
  不得拖垮沙箱，validate 门仍兜底）；`validate_sandbox_etc` 放行
  `java-\d+-openjdk(/.*)?` 形状的目录与文件（含末尾聚合复查的同步修正），其余
  一切仍 fail-closed。实测：投影 35 个文件，`java.security` 与宿主逐字节一致，
  validate 通过。已知同类（未修）：rust 链接器 `cc` 缺失。
- **配套**：`run_tool_regression.py` 七套件 + `test_frozen.py` 改动前后各跑一次
  全绿；AGENTS.md 同步四处表述（沙箱 /etc 投影、评测轮归档、渲染标记 + UTC
  时钟、回归套件数 six→seven）。

## 10. 第二批修复：清理策略 / 工具调用分类 / outer-eval 归档树 / 单 score 分析（2026-10-30）

承接 §9（第一四处修复，commit 5f95d1b）。本轮四项 + 一项留档；全部离线验证，
run_tool_regression 七套件 + test_frozen 改动后全绿。

### 10.1 清理策略修正（F7）

- **表现与根因**：`work/<genid>/` 残留 `gan_polyglot_0/` root 属主载荷（root 0700/0600），
  loop 进程（ganop）永远删不掉。机制：`domains/polyglot/utils.py:copy_from_container`
  的**目录级**提取走 `tar.extractall`——tarfile 保留成员 uid/gid，root 相位提取时容器内
  root 属主被原样带到宿主；单文件路径（`open().write()`）不受影响（agent_evals 为
  ganop 属主即证）。旧清理两端都错：主路径 `rmtree(ignore_errors=True)` 静默留残渣；
  `prepare_run_dir` 的裸 `rmtree` 会直接杀死任务运行。
- **修改（两层）**：
  1. 写入侧——`copy_from_container` 提取后，root 相位（`euid==0`）把提取树属主归一到
     最近非 root 祖先（即 run 属主），`_normalize_extracted_ownership`，advisory；
     非 root 提取为 no-op。此后新世代不再产生不可清载荷。
  2. 清理侧——新增 `utils/prune.py:safe_rmtree`：整树 rmtree；失败则自深向浅尽力清扫
     可删部分，幸存路径写入记录（有事件路径时 `soft_fail`，事件 `rmtree_failed`，
     列前 10 个幸存者；否则 stderr）——**永不 raise**。替换三处调用点：
     `loop._finalize_invalid`、主成功路径（`keep_workdirs=false`）、
     `task_execution.prepare_run_dir`（记录后继续重建，逐文件冲突走既有任务失败记账）。

### 10.2 工具调用分类与反馈（W2）

- **表现**：`"tool_name": null`（JSON null 值）穿透旧的**键存在性**检查被当作合法调用
  分发 → `Tool 'None' not found` ×7 → repeat-break。同一响应里 `{response:…, tool_name:null,
  tool_input:null}`（正常答案）与 `{tool_name:null, tool_input:{…}}`（有意图无目标）被
  混为一谈。
- **修改**（`agent/llm_withtools.py`，新增 `_nullish` 空值判定：None / 缺键 / 空串 /
  空容器 / `null|none|nil|n/a|na|nan|undefined|未指定|无` 大小写不敏感同义词）：
  - name 与 input **均 nullish** → 答案载荷（不计数不分发）；
  - name nullish 而 **input 有内容** → 计 malformed，块级原因（"tool_input present but
    tool_name is null/missing — name the tool you intend to call"）随三元组返回；
  - name 非空而 **input nullish/缺失** → **正常调用**（input={}）：无参工具直接执行；
    有必参工具得到 dispatch 级结构化缺参反馈（`missing required parameter(s): …`），
    不占 malformed 击数；
  - unwrap（response 包裹调用）容错保留，同规则适配；JSON 解析失败 → malformed。
  - 反馈消息携带首个块级原因 + 期望形态示例（`"tool_name": "...", "tool_input": {...}}`），
    有界（≤2 次）与三击终局结构不变。附带效果：`Tool 'None' not found` 对 null 名不可达。
- **验证**：分类 12 例 + 分发两级（无参执行 / 缺参结构化报错）单测全过；
  `scripts/local/test_d123_fixes.py` 的旧规则断言（"嵌套缺 input 不解包"）已更新为新规则。

### 10.3 outer-eval 记录归档树修正（D1，接 §9.2）

- §9.2 把评测轮 task 轨迹归档进了**主线** `outer_<N>` 树（`trajectory/outer_1/
  eval_outer1_s1/`），planner 会话也一直落在主线 seat 目录——与"评测轮记录完整保存在
  `outer_N_eval` 且可解析"的期望不符，且主线 seat 目录被 eval 会话污染（W5 隔离瑕疵）。
- **修改**：轨迹根改为**实例标签派生**（`outer_<标签>`，标签 = 实例名去掉 `outer_` 前缀）：
  - 主线实例 `outer_N` → `outer_N`（布局不变）；评测实例 `outer_N_eval` → `outer_N_eval`
    ——`role_process._prepare_paths` 的预建目录行为**本来就按此规则**（此前被视为解析
    分叉 F2，本修使其成为规范，预建树由幻影空树变为真实树）；
  - 三个写出方对齐同一标签：`BaseRole.traj_outer`（会话写入 `session_trajectory`）、
    `BaseRole` 自席读面（`session_outers` 以标签声明；带后缀实例只读自己的树）、
    `role_broker.launch`（ACL 写根 `trajectory/<traj_root>/roles/<role>/<seat>`）；
  - `loop.run_once` 的评测轮 collect 以 `f"{outer}_eval"` 为 outer 标签 → 落
    `trajectory/outer_<N>_eval/eval_outer<N>_s<M>/task.jsonl`（+ `.llm_calls.jsonl`
    sidecar），与主线同管线同 schema，**可解析性不变**；软失败事件不变；
  - 消费方核对：`paths` 层对 outer 为纯字符串拼接；`session_universe`/
    `outer_session_index`/`session_label`/`_declared_outers` 全部字符串安全；
    `read_session_trajectory` 仅自席可达（self_improve 声明是唯一消费点）；
    genid 命名空间不相交，主线消费者无感。

### 10.4 得分分析工具：单 score（F8 本轮范围）

- **修改**（仅 `analysis/gan_scores_extract.py`；**在线评分零改动**）：
  - 删除本地 a/b 级联（八条正则 + `lenient_verdict`）与 strict/lenient 双轨；
    每 run 单一 `score`（列/变量/曲线只称 score），`method` 列保留作逐行溯源；
  - score 按 `report.json.score_mode`（运行时自述）分派：`docker_resolved` → 容器
    resolved；`lenient` → **与在线同一** `gan.framework.verdict.parse_verdict`
    （labels=accept/reject）重算——离线==在线由构造保证；其余（`strict`，即 search
    与其他 label 域现状）→ 逐字硬匹配；未解析计错、分母不动；
  - 输出对齐：CSV 列 `run,genid,question_id,truth,score,method,raw_head`；REPORT 删
    "严格 vs 宽松重判"节（改单行"总体"+ 二项检验），逐题表/每代表/跨运行表/结论/图例
    全部去 strict/lenient 措辞；significance 仅 `{run}:score` 两两对比（全零占位伪显著
    p 值消失）；控制台 `[OK] … score k/n (pct)`；
  - `report_crosscheck` 语义升级：由"paper 已知漂移诊断"变为全域**离线==在线真探针**
    （失配即实现漂移报警）。
- **验证**：polyglot 实跑 `small_1030_095410`——逐 gen score == len(report.resolved)
  （1/5、2/5、1/5，总 4/15 与 broker 一致），产物零 strict/lenient 字样；paper 分支以
  `_lenient_verdicts`（在线实现）为参照的逐行等价单测通过；search 分支逐字匹配单测通过。
- **命名纪律说明**：工具内残留的 "lenient"/"strict" 字样仅为 `score_mode` 运行时值的
  比对与注释（数据名，非本工具的标注）。

### 10.5 search 在线评分规则变更（延后实施，留档）

本轮不改在线代码。下一轮：`search_arena` 在线评分由逐字硬匹配改为正则提取（与 paper
方法同构）。落地素材：
- 已核实事实：数据集 winner 词表恰为 `{model_a, model_b}`（train 50/50，无平局标签）；
  平局/未解析 → 计错、分母/coverage 不动。
- 级联草稿（原离线实现，本轮删除后在此留档；顺序即优先级，先短输入精确后全文检索）：
  1. `model_exact` `^\s*model[_\s-]*([ab])\s*\.?\s*$`（≤8 字符输入）
  2. `bare` `^\s*([ab])\s*\.?\s*$`（≤8 字符输入）
  3. `dict_verdict` `['\"]?verdict['\"]?\s*[:=]\s*['\"]?([ab])\b`
  4. `verdict_line` `\bverdict\b\s*[:\-]\s*(?:response\s*)?([ab])\b`
  5. `response_better` `\bresponse\s+([ab])\b(?:\s+is)?\s+(?:the\s+)?(?:clearly\s+|significantly\s+|substantially\s+|more\s+)?better`
  6. `wins` `\b([ab])\s+wins\b`
  7. `answer` `\b(?:answer|choice)\s*[:\-]?\s*(?:is\s*)?(?:response\s*)?([ab])\b`
  8. 兜底 `model_mention` `\bmodel[_\s-]*([ab])\b`；否则 unparsed（计错）。
- 落地步骤：① `gan/framework/verdict.py` 增 `parse_choice(text)`（移植上述级联，返回
  字母+method）；② `domains/report.py` verdict 钩子按域分派，search 解析字母 L →
  prediction 置 `model_{L}` 对接 winner 词表；③ 离线工具**零改动**（score_mode 分派
  自动跟随）；④ AGENTS.md "Offline analysis tools" 一节届时同步刷新。
- 已知同类未修：rust 链接器 `cc` 缺失（沙箱）；`scripts/local/test_d123_fixes.py` 的
  D2 pure-runtime 断言为存量漂移（与本轮无关，未动）。

## 11. 第三批修复：沙箱 alternatives 投影 / 兜底补丁口径 / 成本接线 seat 缺失（2026-10-30）

### 11.1 沙箱 `cc` 缺失（W4 同类，接 §9.4）

- **机制（实机验证）**：`/usr/bin/cc → /etc/alternatives/cc → /usr/bin/gcc`（Debian
  alternatives 符号链）。沙箱 `/usr` 只读绑定宿主 → 链接存在；合成 `/etc` 无
  `/etc/alternatives` → 悬空 → rustc/cargo 报 linker `cc` not found。宿主有
  build-essential——纯投影缺口，与 `java.security → /etc/java-17-openjdk` 同型。
- **修改**：`sandbox_etc.create_sandbox_etc` 在 java 投影块后追加 alternatives 投影：
  仅复制**符号链接本体**（目标在沙箱 `/usr` 视图内可解析）、悬空项与指向目录的项跳过；
  `validate_sandbox_etc` 新增 `_ALT_RE`（`alternatives(/.*)?`）放行：目录层、
  **符号链接条目专用分支**（lstat 必须 S_ISLNK + root 属主——普通文件视为投影被篡改）、
  末尾聚合复查同步。负例（rogue 目录）仍 fail-closed。
- **验证**：以真实身份（ganop/rsi_gan_task/planner/evaluator）构建投影 → validate 通过，
  189 条 alternatives 条目、`cc → /usr/bin/gcc` 经共享 /usr 视图可解析。

### 11.2 兜底补丁口径错误（本地 D2b 暴露，代码 bug 而非旧断言）

- **根因**：`_polyglot_workspace_patch_difflib` 内两处 solution_paths 过滤**口径不一致**
  ——步行收集 `if rel not in solution_paths`（无空值豁免）vs diff 循环
  `if solution_paths and rel not in ...`（有豁免）。空 solution_paths（= 不限定，与 git
  路径 `git add -A --` 无 pathspec 同义）时收集为空 → 每个 starter 文件被读成"已删除"
  → 产出删除补丁。同 commit（ab71580）引入。
- **连带发现（自污染）**：`_cm_emit({"type": "patch_difflib_fallback"})` 先于 diff 落盘
  `cm_events.jsonl`（`_cm_events_path` 兜底落 cwd；沙箱内绑定在 workspace 视图），
  被不限定遍历收进补丁。git 路径在空 scope 下同理可泄漏。
- **修改**：① 步行过滤补空值豁免（与 diff 循环、git 路径三方一致）；② 兜底的 audit
  emit **后置**到补丁构建之后；③ 新增 `_RUNTIME_SIDECARS = (cm_events.jsonl,
  cm_events_failed.jsonl)`：兜底步行排除 + git 路径 pathspec `:(exclude)`（两种 scope
  下 sidecar 都永不进补丁）。本地 leg1 断言同步收紧（要求"修改而非删除"，旧断言对删除
  补丁空洞成立）。套件 27/27 全绿。
- **判性说明**：D2（`diff_versus_commit`）两腿本就正确；失败属 **run_d2b 的代码实现
  错误**，非旧断言未更新——测试期望与 git 主路径语义一致。

### 11.3 成本记录接线 seat 缺失（F5）

- **现象**：`logs/cost_sessions.jsonl` 整场不存在、`cost_session` 事件 ×0、cost.json 的
  `planner_plan`/`evaluator_evaluate` 三代全 null；task 侧（proxy_audit + 轨迹聚合）
  正常——半边有数掩盖角色侧断线。
- **根因（同根四处）**：角色会话自 fix3 起落盘 seat 键控布局
  `trajectory/outer_<O>/roles/<role>/<seat>/…`，成本接线却按 legacy 非 seat 形状解析：
  ① `loop._record_session_cost` genid 分支 `session_traj_file(...)` 漏 `seat=` →
  解析到不存在的 `outer_1/<genid>/<role>.jsonl` → rec None 直接 return（append 分支
  永不可达）；② `cost_ledger.gen_session_costs` 同漏 `seat=`；③
  `cost_ledger.self_session_costs` 用 `outer_traj_dir` 裸拼 `{role}.jsonl`；④ 同函数
  glob `{role}__*.jsonl` 与写入方实际名 `outer__<attempt>.jsonl`（seat 分支命名）不匹配，
  且 `outer__*.jsonl` 会误吞 `.llm_calls.jsonl` sidecar（修正：匹配写入方命名 + sidecar
  过滤，sidecar 指标在 `session_cost_from_file` 内合并）。
- **修改**：四处对齐——① 补 `seat=seat`；② 补 `seat=seat`；③ 目录改
  `paths.role_traj_dir(output_dir, outer, role, "self_improve")`；④ glob 改
  `outer.jsonl` + `outer__*.jsonl` 并过滤 `.llm_calls.jsonl`。
- **验证（对 real run small_1030_095410）**：`gen_session_costs(d,1,"0")` →
  planner_plan 109,317 tok（6 calls）/ evaluator_evaluate 1,039,372 tok（19 calls，
  sidecar 指标已合并）；`self_session_costs` → planner self 100,652 tok、evaluator
  self 45,923 tok（attempt=1ef065f2 正确提取）。七套件 + test_frozen + 本地 D123 全绿。
- **残项**：eval 轮会话（`gen_eval_outer1_s1`）仍不归属任何 gen 的 cost.json（eval gen
  无 cost.json）——成本归属口径另行决策；历史 run 的账本可用 `cost_ledger.reconcile`
  回填重建（现成接口，未对本 run 执行以免改写产物）。

## 12. 修复轮契约域化与输出契约显式化（F4/C1，2026-10-30）

### 12.1 设计裁定（两处修正后定稿）

- **不新增 `response_contract` 键**：label 域由既有 `output_contract.allowed_labels`
  派生；search_arena **填充既有键**（null → label 契约，词表 = 在线 exact match 词表
  model_a/model_b；`compute_report_summary` 对 search 仅新增 `contract_ok` 测量事实，
  无打分行为变化）；polyglot/imo_proof 的特殊句来自**框架预测来源映射**（"prediction
  从哪来"是框架事实，非逐域运营参数）。零新 yaml 键。
- **不变性范围收窄**：硬约束仅"role 无法直接修改契约表述/注入代码"（契约不在任何可写
  面——非 set_config 键、domains.yaml 不在写根、env 白名单只读）；**role 在自己的
  prompt 里重写一份不同契约不被禁止也不检测**——软约束"已知存在输出契约"（作者在写作
  现场经 get_config 看到契约；在线打分恒按框架规则执行，竞争契约自败）。无内容审查
  （shape-only 政策保持）。
- **AGENTS.md 对 roles 不可见** → 可见性全部带内（工具面/指令文本/轨迹），无文档层通道。

### 12.2 实现

- **单一派生**：`task_execution.derive_response_contract(domain, domain_cfg)` —
  label 派生（"exactly one of: …"）/ polyglot（"files are the deliverable — response
  is a 1-3 sentence summary; do NOT paste code or diffs"，直接消除旧通用句诱导全量
  重贴 → finish_reason=length 的失效路径）/ imo_proof（"complete proof solution
  text"）/ fallback 通用句。
- **三个消费点**：① task 子指令显式契约段（装配于 role 撰写 prompt 之后、JSON 形状段
  之前，头行声明冲突时以契约为准——写给任务 agent 的读序指令，非 role 侧强制）；②
  修复轮 `response_requirement` 插值（旧 "Return the COMPLETE final answer" 移除）；
  ③ `get_config` 无参视图的 `output_contract` 框架字段。
- **两条注入链**（各对齐既有先例）：task 子进程 = driver broker config →
  `task_broker` env → `GAN_RESPONSE_CONTRACT`（镜像 GAN_TASK_BRIEF，白名单
  `_TASK_ENV_NAMES` 增项）；role seat = `loop → planner.plan(response_contract=…)`
  显式参数 → `DesignContext` 框架盖章字段（先例：record 的 target_role 后置盖章）→
  get_config 读 ctx 转发。
- **get_config 双态**：无参 = 仅键视图（`_keys_view` 现成）+ 框架字段；带参 = 单键值
  ——`prompt` 附 gen-0 seed 与 prompt_equals_seed（改写门上下文随 prompt 读取走，
  不再每次全量附带）；枚举型 dynamic key 附 `enum`（schema 现成）；未知键报错串。
  prompt 全文（≤20k）从"每次顺带 dump"变为显式按键读取。兼容面核查：无程序化消费方
  依赖旧全量形状（套件仅 AST 文件级豁免引用）。
- **loop 传值**：主线与 eval 轮两个 `planner.plan` 调用点均传
  `self.task_runner.response_contract`（init 时派生一次）。

### 12.3 验证

- `derive_response_contract` 矩阵（4 域 + 空 labels 回退 + 无契约回退）通过；
- get_config 双态单测（无参仅键 + 契约字段 / keyed prompt 附 seed / keyed cost /
  未知键报错 / 无契约 ctx 字段缺席）通过；
- task_agent 两处插值静态形状验证（契约段存在、旧 "complete final answer" 措辞移除）；
- 七套件 + test_frozen + 本地 D123 全绿。
- **实跑确认（smoke_1030_204345，2026-10-30）**：①未通过——契约段未出现在真实 child
  指令、修复轮退回旧通用句，断链根因与修复见 §14；②polyglot 修复轮未诱发重贴 diff
  （该轮仅 2 个修复轮，均以摘要收尾），但这是 planner 把契约文本内嵌进自撰 task prompt
  （经 get_config 的 `output_contract` 字段，§12.2③ 实证有效）后的行为掩盖，非注入链生效。

## 13. 冒烟 smoke_1030_195145 归因与修复：/etc 投影子级镜像漂移 + prune 清理崩溃（2026-10-30 第五批）

### 13.1 现象

`NUM_SAMPLES=5 OUTER=1 INNER=3 EVAL_ANCHOR_K=5` 冒烟 rc=1：
`trajectory.collect` 抛 "no task trajectory sources … (no records parsed)"，随后
`prune.safe_rmtree` 处 `UnboundLocalError: cannot access local variable
'first_err'`；verify_smoke_eval 4 FAIL（harness n=1 / 无 supernode val_mean /
无 driver_done / 无 scores 行）。

### 13.2 归因链（四层，全部有实证）

1. **任务子代理全部即死（根因）**：`runs/0/eval_failures.jsonl` + 5 个
   `failure_*.log` 显示 5 题同一异常 `TaskAgent sandbox /etc contains an unsafe
   directory: alternatives`（抛在 harness 进程 `_sandbox_command` 预检，bwrap
   未启动；每题 ~4s，零 task `llm_call`、cm sidecar "no seam events"、5 空
   patch、`chat_history_*` 存在但零记录）。机制：`5f95d1b`（java-N-openjdk
   投影）与 `1ee3a48`（alternatives 投影）只扩展了 root 侧
   `sandbox_etc.validate_sandbox_etc`（`_JAVA_ETC_RE`/`_ALT_RE` 分支），
   **未同步** `domains/harness.py:_require_projected_etc` 的子级镜像
   （白名单仍 `{"ssl","ssl/certs"}`）——沙箱刻意不含 `gan/`（docs/10 §11.0.1），
   镜像必须双份维护；probe（root 侧校验）通过、子级预检 fail-closed。
   `1ee3a48` 的本地验证只跑了 root 侧 validator，镜像从未被真实投影过一遍。
2. **归档失败显式化（设计行为，非 bug）**：`trajectory.collect` 冻结契约——
   chat_history 文件存在但零记录 = 评测者失去证据 = pipeline 故障 → genid 0
   走 `_finalize_invalid_node`。
3. **单代失败放大为 rc=1（次生 bug，304f61d 引入）**：root-owned docker 载荷
   `gan_polyglot_0/`（既存状态，095410 run 同样存在，曾被
   `ignore_errors=True` 静默容忍）使 `shutil.rmtree` 抛异常；
   `prune.py:33` `except Exception as first_err: pass` 在块尾按 Python 语义
   解绑 `first_err` → 第 46 行 UnboundLocalError → "never raises" 契约破坏 →
   outer worker 崩溃，scores/inner_invalid/checkpoint/supernode 评测/
   driver_done 全部未达；work/ 诊断现场被部分销毁（eval_failures 靠
   `runs/0/` 归档副本幸存——`_dump_failure_log` 的 runs/<genid>/ 拷贝是本轮
   定罪的唯一完整证据源）。
4. **P3 遗留（未修，卫生项）**：polyglot docker-broker 载荷提取后无属主
   归一化（imo_proof 路径 task_execution.py:2844 有先例），每个 run 留
   root-owned work/ 残留；P2 修复后降级为 `rmtree_failed` 记录，不再致命。

### 13.3 修复

- **P1 `domains/harness.py`**：`_require_projected_etc` 拆出可离线测试的
  `_audit_projected_etc_tree(root, root_uid)`；镜像扩展 `_SANDBOX_ETC_JAVA_RE`
  （java-N-openjdk 子树：0444 普通文件）与 `_SANDBOX_ETC_ALT_RE`
  （alternatives 条目：**强制符号链接** + uid + nlink=1），目录 0555 root。
  注释明示：任何 builder 侧新投影必须同步此镜像。
- **P2 `utils/prune.py`**：except 块内定格 `first_err_desc =
  f"{type(exc).__name__}: {exc}"`（附 Python as-name 解绑 gotcha 注释），
  detail 行引用该字符串——恢复 never-raises 契约。
- **新本地守卫（gitignored）**：`scripts/local/test_sandbox_etc_mirror.py`
  ——真实 `create_sandbox_etc` 构建投影（本轮 189 条 + java-17-openjdk）→
  子级镜像审计必须通过（5f95d1b/1ee3a48 均会被它拦下）+ rogue 目录 /
  alternatives 混入普通文件 / 缺必需文件三组拒绝；
  `scripts/local/test_prune_safe_rmtree.py` —— clean sweep / file-as-path
  （NotADirectoryError 记录不抛）/ mock 部分清扫（survivors 入记录）/
  缺路径 no-op。

### 13.4 过程事故与修正（gcc-13）

漂移守卫初版向 `alternatives/` 种名为 `cc` 的普通文件——该名字在真实注册表
中**已存在且为符号链接**（cc → /usr/bin/gcc → gcc-13 →
x86_64-linux-gnu-gcc-13），`open(...,"w")` 跟随三层链接把宿主机编译器驱动
截断为 14 字节并 chmod 0444（root DAC_OVERRIDE 无视 0555 目录权限位）。
经 `apt-get install --reinstall gcc-13-x86-64-linux-gnu`（网络源，非 docker
镜像）还原，gcc/cc 链功能验证通过。测试改为**非碰撞名** +
`O_CREAT|O_EXCL|O_NOFOLLOW` 创建——测试代码自身遵守构建器的防跟随纪律。

### 13.5 验证与关联记录

- 新守卫 2 套件 8/8 PASS；test_frozen PASS；test_d123_fixes 27/27；
  test_venv_bin_path ALL PASS。
- `test_polyglot_sandbox_write` V4 为**既有**环境依赖失败：`b213b31` 给
  `_sandbox_command` 加挂载检查后，V4 在沙箱外构造 bwrap 命令必触发
  "requires the sanitized /etc projection"（对照 095410 基线 ckpt 同样失败），
  与本批改动无关，留待单独处理。
- 复跑确认点：task 阶段出现 task-role `llm_call` 事件 / `[timing] question`
  行、且全程无 "unsafe directory"（本节修复的端到端判据）。

## 14. smoke_1030_204345 审计：F4/C1 契约注入断链修复（2026-10-30 第六批）

> 起因：对 `runs/polyglot/smoke_1030_204345`（1 outer × 3 inner + 固定根评估）的全量
> 审查发现 §12 的框架输出契约未按预期呈现——本节只记录该断链的根因与修复；本轮其余
> 审计发现（评估器 6000 钳制、rmtree 残留、空补丁机理等）另行登记。

### 14.1 现象与根因（三份白名单、两处漏键）

- **现象**：4 个任务 launch 的 child 指令均无 `Output contract (framework-owned...)`
  段；2 个 format-repair 轮全部退回 `GAN_RESPONSE_CONTRACT` 缺席时的通用兜底句
  （"The response must contain the complete final answer"）——§12.2② 的域感知要求
  与 §12.2① 的注入段同时失效。行为面被 planner 掩盖：它经 `get_config` 的
  `output_contract` 框架字段（§12.2③，实证有效）把契约句内嵌进了自撰 task prompt。
- **根因**：任务 child env 链有**三份** `_TASK_ENV_NAMES` 白名单——
  `gan/framework/task_execution.py`（源，已含 `GAN_RESPONSE_CONTRACT`）→
  `domains/harness.py`（**hop 1：harness → task_worker，漏**）→
  `domains/task_worker.py`（**hop 2：worker → TaskAgent child，漏**）。driver 派生
  契约并经 broker env 注入成功，但两跳子过滤器各自把键静默丢弃，TaskAgent 的
  `os.environ` 里该键为空 → `contract_block` 空串、`response_requirement` 走兜底。
  与 §13.2 P1（/etc 投影子级镜像漂移）同型：**镜像双份维护第三次失步**，且这次一
  失失两处。

### 14.2 修复

| 文件 | 改动 |
|---|---|
| `domains/harness.py` | `_TASK_ENV_NAMES` 增 `GAN_RESPONSE_CONTRACT`（hop 1），注释写明镜像规则与守卫 |
| `domains/task_worker.py` | 同上（hop 2） |
| `gan/framework/task_execution.py` | 源白名单处注释补 **MIRROR RULE**（新增 GAN_* 键必须同步两跳镜像） |

- **新本地守卫（gitignored）**：`scripts/local/test_task_env_mirror.py` ——
  ①三份白名单的 GAN_* 超集关系（框架侧 ⊆ 两跳镜像）；②探针 env 端到端过两跳过滤
  （`GAN_RESPONSE_CONTRACT`/`GAN_TASK_BRIEF` 存活）；③`GAN_DATASET_ROOT` 在三份
  白名单中均缺席（基准标签边界不变式）。修复前 4 FAIL、修复后全 PASS。
- **刻意不做**：非 GAN 键不强行对齐（`LITELLM_LOCAL_MODEL_COST_MAP` 不过 broker
  边界、`GAN_SECURE_CAPABILITY_HOLDER` 是下游自设）；不做 import 期断言（避免
  把镜像漂移升级成启动崩溃，守卫测试承载）。

### 14.3 验证

- `test_task_env_mirror.py` 全 PASS（修复前红）；
- `run_tool_regression.py` 七套件全 PASS、`test_frozen.py` PASS、
  `test_venv_bin_path.py` / `test_sandbox_etc_mirror.py` 全 PASS、三文件
  `py_compile` 通过；
- **待下一真实 run 确认**：child 指令出现 `Output contract (framework-owned...)`
  段（位于 Task input 之后、JSON schema 段之前）；polyglot 修复轮的
  `response_requirement` 为 "Your written solution files are the deliverable..."
  而非通用兜底句。

### 14.4 附带登记（本轮审计暴露、未在本批修）

- 同一转移缺口（`RoleProcess` 包装上的属性盖章不跨进程，`role_state`/`broker_info`
  精确键集）使**席位 CM 栈在 driver 模式整体休眠**：`ctx_index`/`ctx_fetch`/
  `read_trajectory` 的 slice 面对角色席位不可达（实证：evaluator 调 `ctx_index`
  得 "no context-management session is active"）；`cost.cm_mode` 对席位是死旋钮
  （planner 本轮给自己设的 `compact_conservative` 不会生效）；`session_token_budget`
  疑似同批休眠，待核查。复活属协议变更（非小改动），另批处理。
- `AccessBroker.read_trajectory`/`read_session_trajectory` 的
  `min(int(max_chars), 6000)` 硬钳制（4e5e73a，早于 fix3，无文档化理由）叠加
  `trajectory.read` 无 offset 分页：评估器请求 60k 仍得 6000 切口，gen_2 评估器
  以 issue 形式投诉（i-5），gen_1 评估器 blind 阶段因 6000 视图进入退化推理循环
  （finish_reason=length，32k tokens）后 blind 预测被迫落到 reveal 阶段。
  修复方向（上限可配 / RPC 透传 offset）另行批次。
