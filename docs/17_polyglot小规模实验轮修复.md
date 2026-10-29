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
