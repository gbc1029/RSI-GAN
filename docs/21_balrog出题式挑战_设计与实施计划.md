# 21 — balrog 出题式对抗挑战：设计与实施计划

> 最新增量见 §22：Balrog challenge 保障对齐，IMO 出题暂缓。

> **状态**：**待确认，未开工**（2026-11-19）
> **上游参考**：`origin/challenge_design` 分支的 IMO challenge 实现（只读借鉴，**本分支不合并**）
> **前置成果**：`docs/20_L1行为事实与死局判据改造.md`（阶段 1–4 已完成：循环退化、无进展分型、
> 动作类别占比、`process.dead-end` 机械判据）
> **已确认决策**：D1 出题权 = L1（生成器采样）｜D2 同族题对 + 方案 b｜D3 修复判据 = 缺陷消失且
> 进度不降｜D4 challenge 一代生命周期｜D5 evaluator 对抗项 = precision × actionability × calibration

---

## 1. 要解决的问题

现有闭环 `report_issue → respond_issue → judge_fix` 有三个结构性弱点（均在 balrog 上实测到）：

1. **issue 是宽泛的自然语言**，planner 只能整体重写 prompt（3 臂实测：12 个有效代里 12 代全是浅改，
   且越改越差，A0 留出分 0.27 < A1 0.60）。
2. **修复真值缺失**：`judge_fix` 是 evaluator 自己的预测，`process.dead-end` 的机械判据直到 docs/20
   才有，且此前 6/6 代提出却 0 次判决。
3. **无法定向考**：缺陷出现在哪一局，只能等下一轮抽到相似的题才可能复现。

本设计加一条**出题式对抗通道**：evaluator 依据自己看到的缺陷，要求框架**采样新实例**（同族的另一局），
用"父子两代在同一缺陷上的机械状态"裁决是否真被修复，第三方（固定 adjudicator）独占真值权。

---

## 2. 三权分离（本设计的地基）

| 权力 | 归属 | 说明 |
|---|---|---|
| **出题权**（提什么缺陷、考哪一族、往哪个难度档采样） | evaluator（blind 阶段锁定） | 只是**参数化采样权**，不写题面 |
| **判题权**（"planner 修好了吗"） | evaluator，但只是**预测**（`judge_fix` 沿用） | 不参与真值 |
| **真值权**（缺陷在父/子两代是否存在） | **固定 adjudicator（不可进化）** | 机械适配器，见 §5 |
| **准入权**（这题能不能进批次） | framework + 域校验器（机械，fail-closed） | **不交给 adjudicator**，见 §6 |
| **记账/结算权** | framework（`WriterToken` + 账本状态机） | 唯一写入者 |

**为什么"出题 + 判题"不是逻辑矛盾**：判题权不在 evaluator 手上。真正的风险是两点，必须结构性堵住：
1. **题目准入**（能不能考）→ §6 的三层校验，机械前置；
2. **可修性**（这题原则上有没有解）→ L1 用生成器采样，可解性**由构造保证** + solver 档位校验。

> 反面教材：IMO 的 `evaluator_adversarial_score = (b-a-c-d)/…` 给"真但未修复"记 +1。
> 在"从固定题库选题"下安全；**一旦允许出题，它会诱导出"不可修的题"**。
> 故本设计**不采用**该公式（决策 D5）。

---

## 3. 数据模型

### 3.1 challenge 记录（账本 `ckpt/challenges/ledger.json`）

```jsonc
{
  "challenge_id": "ch-<outer>-<producer_node>-<i>",   // 框架盖章
  "defect_class": "policy.cycle-degeneracy",          // 闭合词表，见 §3.2
  "family": "BabyAI-MixedTrainLocal-v0/pickup",       // 题目家族 = 缺陷出现的 task
  "evidence_ref": "g7/ep04",                          // 父代那一局（父代真实跑过的旧实例）
  "repair_criterion": "该族新实例上缺陷消失且进度不降",
  "parent_episode_id": "pickup#7386714",              // 旧实例（诊断来源，方案 b）
  "instances": {                                       // 框架物化，不可由模型指定
    "train":    {"episode_id": "pickup#…", "seed": …, "solver_steps": …},
    "eval":     {"episode_id": "pickup#…", "seed": …, "solver_steps": …}
  },
  "producer_node": "7", "producer_evaluator_version": "…",
  "status": "locked | materialized | reserved | consumed | settled",
  "consumer_edge": "7->8", "consumer_planner_version": "…",
  "adjudication": { … }                                // 不可变，见 §5
}
```

**不变量**（与 IMO 一致，直接复用记账纪律）：
- `locked` 之后 payload 不可改；重跑幂等同 payload 返回同 ids，不同 payload 报错；
- 一次预留只给一个 child（`reserved_child_node` / `consumed_child_node` 互斥）；
- `settled` 的 `adjudication` 不可变；所有写入要 `run_writer`；
- **一代生命周期**（D4）：消费即结案，未修复由子代 evaluator 重新提一条新 challenge。

### 3.2 缺陷词表（闭合，3 个起步）

| `defect_class` | 机械判定（单局） | 依据字段（docs/20 已实现） |
|---|---|---|
| `policy.cycle-degeneracy` | `revisit_share ≥ 0.80` | `episodes[].revisit_share` |
| `policy.no-interaction` | `interact 步数 = 0` 且 `progression < 1.0` | `no_progress_action_counts` + 动作分布 |
| `policy.blocked-forward` | `no_progress_action_counts['go forward'] / steps ≥ 0.40` | 同上 |

> ⚠️ **禁止**用 `turn` 占比定义"转圈"：阶段 4 实测 A0 gen1 turn 62.7% 仍解出 6/10（docs/20 §10）。

未在词表内的缺陷 → `submit_challenge` 直接拒收（返回错误给 evaluator，不消耗配额）。

### 3.3 单局缺陷线**单一来源**（阶段 0 必须收掉）

同一个“循环退化”现在有两层不同粒度的机械线：

| 消费者 | 粒度 | 今天的位置 |
|---|---|---|
| `issue_checks._chk_dead_end`（普通 issue 真值） | **代级**（多少比例的局退化） | `loop.yaml: issues.check.dead_end_revisit_share=0.40` |
| challenge adjudicator（docs/21 新增） | **单局** | 需新声明 |
| 单局线本身 | 单局 | `domains/balrog/utils.py: DEGENERATE_REVISIT_SHARE=0.80` + `loop.yaml` 里**仅作显示**的镜像 ⇒ **双源，会漂** |

不统一的后果很具体：**同一个缺陷可能“普通 issue 说修好了、challenge 说没修好”**。
解决方式（阶段 0 一并完成）：

1. 单局缺陷线下沉到 `domains.yaml` **一处声明**（`challenge_space.defect_lines`）；
2. `domains/balrog/utils.py` 与 `gan/framework/issue_checks.py` **都读这一处**（框架本来就拥有
   `domains.yaml`，不违反“框架不 import 域代码”的约束）；
3. 两条通道走**同一组单局原语**，代级判据只是“单局原语的聚合”（>= 0.5 的局命中）；
4. 加回归断言：同一代同一缺陷在两条通道上的结论**必须一致**（见 §9 阶段 0 的门）。

---

## 4. 出题权 = L1（生成器采样）

evaluator 通过新工具 `propose_challenge` 提交**参数**而非题面：

```
propose_challenge(defect_class, family, evidence_ref, repair_criterion)
```

- `family` 必须是"该缺陷实际出现过的 task"（框架用父代报告校验）；
- 框架据此物化 **同族题对**：
  - `instances.train` — 进**训练批**（占旋转位），planner 看得见，必须应对；
  - `instances.eval` — 进**评估批**（`challenge_val` 层），planner 看不见，用于泛化确认；
- 实例由 `domains/balrog/challenge_space.py` 的生成器采样 `(task, seed, params)`，
  `params` 取值域在 `domains.yaml` 声明，模型**不能**直接指定 seed 或题面文本。

### 4.1 批次预算（决策 D1，已确认）

```
train 批（每代，不变）: anchor 5 + challenge ≤ 2 + 随机 ≥ 3 = 10
eval  批（outer 边界）: 固定 core 5（不可动） + challenge_val ≤ 2×K
```

启动检查（fail-closed，违反即拒绝启动而不是静默截断）：
`0 ≤ K ≤ num_samples − anchor_k − random_min(=1)`；
challenge 实例必须属于 train 池、且与 `eval_episode_ids` 及固定 anchor **无交集**。

> **固定 anchor 统计只读 `A_base`**：challenge 实例绝不进入 anchor 指标、LCB、stagnation，
> 否则选父语义被破坏（这是 IMO §2.4 的硬约束，balrog 同样适用）。

---

## 5. 真值：机械 adjudicator（决策 D2 方案 b）

### 5.1 父子证据怎么取

| 侧 | 取哪一局 | 为什么 |
|---|---|---|
| `parent_present` | **父代在旧实例 `parent_episode_id` 上的行** | 父代真实跑过；不补跑（省一次 episode） |
| `child_present` | **子代在 `instances.train` 上的行** | 同代内自然产出，零额外成本 |
| `generalization` | 子代在 `instances.eval` 上的行 | outer 边界跑一次，planner 不可见 |

三者都是**同一 task 家族**的实例，`solver_steps` 档位相近才可比。

### 5.2 状态映射（沿用框架契约的 5 状态，一行不改）

| parent | child(train) | 状态 | 记账 |
|---|---|---|---|
| present | absent（且进度不降） | `VERIFIED_REPAIR` | planner +1 |
| present | present | `VERIFIED_UNRESOLVED` | 诊断（**不进 evaluator 收益**，D5） |
| absent | absent | `UNSUPPORTED_CHALLENGE` | evaluator −1（假挑战） |
| absent | present | `REGRESSION_AFTER_FALSE` | planner −1 |
| 任一不可判 | — | `UNDECIDABLE` | 不进分母 |

**修复判据（D3）**：`child_present == absent` **且** `progression(instances.train) ≥ progression(parent 旧实例) − tol`。
→ 防止"用降级换指标"。`tol` 初始 0（同族同档，严格不降）。

**`UNDECIDABLE` 的触发条件**（必须保守，宁可判不了）：
实例物化失败 / 该 episode `aborted` / 父代旧实例缺数据 / `solver_steps` 档位差超限 /
adjudicator 抛错。

### 5.3 机械适配器 `domains/balrog/challenge_adjudication.py`

与 IMO 适配器**同签名**，但纯函数、零 LLM：

```python
def adjudicate(challenge, parent_evidence, child_evidence) -> dict:
    # 1) 按 defect_class 从词表取判定器，算 present/absent
    # 2) 进度不降检查（D3）
    # 3) actionable = True, actionability_basis = "transferable_strategy"
    #    （缺陷类来自闭合词表 = 类缺陷；实例来自生成器 ⇒ 可迁移性由构造保证）
    # 4) modification_scope 由硬编码审计给（patch/config 含具体 mission 文本或 seed 分支
    #    ⇒ instance_specific_memory / answer_leakage → 框架硬门）
    # 5) 交给 framework.challenge_adjudication.adjudicate() 出最终状态
```

> 因此框架契约 `challenge_adjudication.py` **一行不改**，IMO 那套 `actionability_basis` /
> `modification_scope` 字段照常填，只是 balrog 用机械规则而非模型判断填。

### 5.4 与 `issue_checks` 共用同一组单局原语

```
domains.yaml: challenge_space.defect_lines
  cycle_degenerate_revisit_share : 0.80      # 单局
  blocked_forward_share          : 0.40      # 单局
  no_interaction_max_steps       : 0         # 单局
        ↓ 两个消费者都读这一处
  domains/balrog/utils.py    → 按此聚合出 behavior.episodes_degenerate_revisit_share
  gan/framework/issue_checks → _chk_dead_end 的代级判据（单局命中率 >= 0.5）
  domains/balrog/challenge_adjudication.py → 直接对指定 episode_id 调原语
```

**一致性断言**（必须进回归）：给定同一代的 report，两条通道对同一个 `defect_class` 的
结论必须相同——若 `issue_checks` 判 PRESENT 而 challenge 对每一局都判 absent（或反之），
回归失败。这条断言是“两套机械真值不打架”的唯一保障。

---

## 6. 准入校验：三层，且**不归 adjudicator**

| 层 | 判什么 | 谁做 | 时机 | 失败处理 |
|---|---|---|---|---|
| **A 机械合规** | K 上限、family 必须是缺陷出现过的 task、与 val/固定 anchor 无交集、去重、参数在声明域内 | `gan/framework/challenge_questions.py` + `domains/balrog/challenge_space.py` | **物化后、进批次前** | 拒收并退回 evaluator（记事件 `challenge_rejected`），不消耗配额 |
| **B 可解性/非退化** | `solver_steps ∈ [lo, hi]`（防 1 步可解 / 不可解），实例确实生成了可复现 episode | 同上（域侧，机械） | 同上 | 拒收或重采样（最多 N 次），仍失败 → 该 challenge 作废 |
| **C 真值** | 缺陷在父子两侧是否存在 | 机械 adjudicator | child 评估后 | `UNDECIDABLE` |

**为什么不塞进 adjudicator**（写进代码注释，防止后来者"顺手合并"）：
1. 归因不可分：判宽了无法区分"缺陷判错"还是"题目判错"（IMO 的 72/72 `actionable=true` 就是先例）；
2. 时机冲突：准入必须在采样前，真值必须在评估后；
3. 成本错配：能机械判的事用模型判，误差与成本双输。

---

## 7. evaluator 的自进化信号（决策 D5）

```
F_e_adv = w_prec · z(precision_smoothed)
        + w_act  · z(actionability_rate)
        + w_cal  · z(challenge_calibration)
```

| 信号 | 定义 | 真值来源 | 为什么安全 |
|---|---|---|---|
| `precision_smoothed` | `(a+b+1)/(a+b+c+d+2)`（a=修复, b=未修复, c=假挑战, d=假挑战后回归） | adjudicator | 不可伪报 |
| `actionability_rate` | 词表内 + 实例合规的 challenge 占比 | 机械校验 | 机械可数 |
| `challenge_calibration` | evaluator 的 `judge_fix` 预测与 adjudicator 真值的一致率 | adjudicator | 复用既有 `verdict_stability` 思路 |

**明确不进 fitness**：`VERIFIED_UNRESOLVED` 计数、`repair_rate` 单项。
理由：前者诱导"出不可修的题"，后者诱导"出软题"。两者只进诊断栏。

**两阶段启用**（沿用 IMO 纪律）：`supported ≥ 30` 且 `determinate_rate ≥ 0.8` 才激活；
未达标时三项均为中性 0，权重初值 `w_prec=0.10 / w_act=0.05 / w_cal=0.05`。

---

## 8. 代码落点

### 8.1 直接复用（来自 IMO 分支，只读移植，不改语义）

| 文件 | 处理 |
|---|---|
| `gan/framework/challenge_ledger.py` | 移植；`DOMAIN` 改为参数化（`imo_proof` 写死处抽出） |
| `gan/framework/challenge_metrics.py` | 移植；**追加** `calibration` 项 |
| `gan/framework/challenge_adjudication.py` | **原样**（5 状态机 + 证据可用性校验） |
| `gan/framework/paths.py: challenge_ledger_path` | 原样（`ckpt/challenges/ledger.json`） |
| `gan/tools/work/planner/respond_challenge.py` | 原样（5 种 stance + 逐条回应契约） |
| `gan/framework/context.py` 的 `challenges` / `challenge_responses` 字段 | 原样 |

### 8.2 新增

| 文件 | 内容 | 估行 |
|---|---|---|
| `gan/framework/challenge_questions.py` | proposal 校验、K 预算、与 val/anchor 去重 | ~120 |
| `gan/framework/challenge_adjudication.py`（追加） | `register_adjudicator(domain, fn)` + `for_domain()` | ~30 |
| `domains/balrog/challenge_space.py` | `materialize()` / `validate_episode()` / `family_siblings()` | ~180 |
| `domains/balrog/challenge_adjudication.py` | 机械适配器（§5.3） | ~150 |
| `gan/tools/work/evaluator/propose_challenge.py` | 新工具（blind-only，词表校验） | ~60 |
| `gan/framework/domains.yaml` | `challenge_space:` 声明 + `challenge:` 配置块 | ~25 |

### 8.3 改动

| 文件 | 改动 |
|---|---|
| `utils/episode_seed.py: plan_episodes` | 接收 `challenge_instances`，组成 `A_base + A_challenge + 旋转补足`，`A_base`/`A_challenge` 分开返回 |
| `gan/framework/loop.py` | 五个钩子（lock / available / consume / settle / metrics）；**把 `is_imo_proof` 硬编码改成 `for_domain()` 查表**；outer 入口冻结 role 版本后再结算 |
| `gan/framework/loop.yaml` | `challenge:` 块（`enabled/max_k/...`，默认 `enabled: false`） |
| `gan/framework/task_execution.py` | `select_seeded_positions(challenge_ids=…)`（若走 CSV 路径）；balrog 走 `plan_episodes`，主要改上一行 |

---

## 9. 实施阶段与验收

| 阶段 | 内容 | 行为影响 | 验收（硬门） |
|---|---|---|---|
| **0 骨架** | 注册表重构 + ledger 参数化 + 机械适配器 + **单局缺陷线单源化（§3.3）** + `challenge.enabled: false` | **零** | 现有 balrog 12 套（389 断言）+ `test_behaviour_facts`/`test_action_classes`/`test_deadend_revisit`（35/26/48）+ 框架 5 套全绿；**默认关闭时报告逐字段不变**（与阶段 4 跑出的 6 代 diff）；**两条通道结论一致性断言绿** |
| **1 pilot** | `propose_challenge` 工具 + L1 物化 + 三层准入 + 采样注入；challenge 正常锁定/消费/结算，**不进 fitness** | 每代 +≤2 challenge 实例（挤占旋转位） | 离线回放：K 上限/去重/交集/档位校验均有测试；一代真实跑确认 `instances.train` 出现在 `question_points`、`instances.eval` 出现在 `challenge_val`；`UNDECIDABLE` 率 < 20% |
| **2 激活** | `challenge_metrics` 接 `F_e_adv` + 两阶段 gate | 改变 evaluator 选择压力 | `supported ≥ 30` 且 `determinate ≥ 0.8`；A0/A1 对照看是否缓解"越改越差" |

阶段 0→1 之间不跑付费实验；阶段 1 的一次验证约 3 小时 / ~$2（判定不调 LLM，成本主要在新增 episode）。

---

## 10. 已知风险与待办

1. **同族可比性假设**：跨实例比 `progression` 只在"同 task + 相近 `solver_steps` 档"下成立。
   缓解：物化时记录档位，档差超限 → `UNDECIDABLE`（不硬比）。
2. **`solver_steps` 可得性（已验证）**：见 §11。`max_steps` 是**族常量**（goto/pickup=64，open/putnext/seq=128），
   `time_limit` 为 None，**不能当难度档**；最优步数用 `BabyAIBot` 逐步 rollout 得到（0.00–0.04 s/实例）。
3. **缺陷集中在单一 task**：若缺陷是全局的（如"所有 task 都不动手"），challenge 会集中在少数
   family。缓解：`family` 允许从"出现该缺陷的 task 集合"中选，且 `challenge_max_k` 限制集中度。
4. **evaluator 自进化目标漂移**：出题权会让它倾向"制造难题"。缓解：§7 的 fitness 组合不含
   unresolved 计数；`precision` 与 `calibration` 都是"关于自己工作质量"的信号。
5. **challenge_val 破坏可比性**：`challenge_val` 每代变，**必须单列**，不进 `val_mean`、不参与选父；
   core val（5 条冻结）保持不动。

---

## 11. 阶段 0 技术验证结果（2026-11-19，已跑，未改代码）

验证脚本：`scripts/local/probe_stage0_feasibility.py` / `probe_stage0_v2.py` / `probe_stage0_v3.py`

| 问题 | 结论 |
|---|---|
| **Q1 同族新实例可采样吗** | ✅ 5/5 家族稳定：`make_env("babyai", "…-MixedTrainLocal-v0/<goal>")` 在构造时用拒绝采样把 `action_kinds[0]` 固定成 goal；之后任意 `reset(seed=S)` 都落在同一家族。20 个实例全部成功构造/重置 |
| **Q2 “是否新关卡”用什么判** | ⚠️ **不能用 mission 文本**：`open` 族 3 个 seed 里有 2 个都叫 "open the door"，但 grid 指纹不同（3/3 distinct）。判据用 `env.unwrapped.grid.encode()` 的 sha1 |
| **Q3 最优步数怎么拿** | `BabyAIBot` 只暴露 `replan()`（返回**下一个**动作），所以 = 逐步 rollout 到 done。实测 3–21 步，全部 solved，**0.00–0.04 s/实例** |
| **Q4 `max_steps` 能当难度档吗** | ❌ 是**族常量**：goto/pickup=64，open/putnext/seq=128；`time_limit` 为 None |
| **Q5 生成器自带退化防护吗** | ✅ 有：`putnext` 采样时会打印 "Sampling rejected: objs already next to each other"，即内部拒绝平凡实例 |

**对 B 层（可解性/非退化）的影响**：难度档定义在 `optimal_steps` 上，而不是 `max_steps`。
初步取值：`optimal_steps >= 4`（避免近乎平凡）且落在该族参考集的 [P10, P90] 区间内；
参考分布实现时用 20 个 seed 离线统计一次，写进 `domains.yaml: challenge_space.params`。

**对 A 层（去重/新颖性）的影响**：重复检测用 grid 指纹（而非 mission/seed），因为不同 seed
可能生成同一布局（概率低但存在），而同一 mission 文本对应当不同布局。

> 追加（2026-11-19）：本节之后是阶段 0 的落地记录，见 §12。

---

## 12. 阶段 0 落地记录（2026-11-19，已实现，零行为影响）

三个提交：`4e1f1bc`（契约层）/ `41d9e9a`（题面空间 + 机械适配器）/ 本节。

### 12.1 落了什么

| 文件 | 内容 |
|---|---|
| `gan/framework/challenge_ledger.py` | 盲锁生命周期；**域参数化**（IMO 把 `"imo_proof"` 写死） |
| `gan/framework/challenge_adjudication.py` | 5 状态机 + 证据可用性；**新增** `register_adjudicator` / `adjudicator_for`（取代 `is_imo_proof` 硬分支） |
| `gan/framework/challenge_metrics.py` | 移植；**去掉** `evaluator_adversarial_score`（它给"出不可修的题"发钱）；**新增** `challenge_calibration`；两阶段激活闸门 |
| `gan/framework/defect_lines.py` | 单局缺陷线的唯一读者（闭词表 + 缺声明 = `{}` 而非 0 + 非法值直接抛） |
| `domains/balrog/challenge_space.py` | L1 物化：同族**题对**（train/eval）+ 指纹去重 + 难度档 + 确定性 |
| `domains/balrog/challenge_adjudication.py` | 零 LLM 机械适配器（`process.dead-end`） |
| `gan/framework/task_execution.py` | `compute_report_summary` 用声明线**重算**循环份额，仅在**不一致**时写 `behavior.defect_line_drift` |
| `gan/framework/loop.yaml` | `challenge:` 块，`enabled: false` |

### 12.2 实现时测得的三件事实（都改过代码）

1. **指纹必须在 reset 时取。** 先 rollout 再取指纹会把不同实例压成同一个哈希 ——
   每个解出来的局最后都是"机器人站在目标上"同一个姿势。实测：pickup 24 个 seed
   得到 **1** 个"唯一布局"，真相是 **23** 个。所以 `materialize()` 先 reset→取指纹，
   再 rollout 算难度。mission 文本同样不能当身份（两个不同 `open` 关卡都叫
   "open the door"）。
2. **`adjudicator_for` 不能直接拼 `domains.<域名>`。** 域名是 `balrog_babyai`，
   包是 `domains.balrog`（与 `domains.yaml` 的**最长前缀**规则一致）。按朴素写法，
   每次 balrog 结算都会 import 失败 → 永远 `UNDECIDABLE`，而且看起来像"证据不足"。
   现在按 `_` 前缀逐级回退，`test_challenge_ledger.py` 断言 `balrog_babyai` 解析成功。
3. **D3（进度不降）只能在父代确实有病时生效。** 无条件套用会把
   `UNSUPPORTED_CHALLENGE`（假挑战）变成 `REGRESSION_AFTER_FALSE_CHALLENGE` ——
   等于因为一次普通进度波动去惩罚 planner 一个它从来没有的缺陷。真实数据回放
   直接抓到了这个 bug。

### 12.3 难度档（实测，24 seed/族）

| family | min | p10 | p50 | p90 | max | 采纳档 `[max(4,p10), p90]` |
|---|---|---|---|---|---|---|
| goto | 1 | 2 | 5 | 7 | 8 | **[4, 7]** |
| pickup | 3 | 4 | 5 | 8 | 10 | [4, 8] |
| open | 7 | 12 | 16 | 19 | 24 | [12, 19] |
| putnext | 6 | 7 | 12 | 15 | 16 | [7, 15] |
| pick_up_seq_go_to | 6 | 9 | 13 | 16 | 17 | [9, 16] |

布局去重率 23–24/24（pickup 有一个 seed 撞布局，被正确拒收）。
`goto` 的 p10=2 说明该族十分之一近乎平凡，所以 4 步地板在这里是**上调**而非装饰。

### 12.4 两条通道的一致性（真数据）

阶段 4 的 6 代真报告回放：`issue_checks._chk_dead_end` 的代级判决与
challenge 通道的逐局判决**无一矛盾**。

| 代 | checker | 命中局/有轨迹局 |
|---|---|---|
| A0 gen0 | present | 9/10 |
| A0 gen1/2 | absent | 2/10, 3/10 |
| A1 gen0/1/2 | absent | 3/10, 2/10, 3/10 |

同时验证：框架用声明线重算的 `episodes_degenerate_revisit_share` 与 harness 值
**逐代相等**（不报 `defect_line_drift`），故意污染一局后必须报警。

### 12.5 故意没做的

- `policy.no-interaction` / `policy.blocked-forward` **不接**：它们需要**单局动作类计数**，
  而报告行目前只带代级的 `action_class_shares`。靠代级份额反推单局 = 编一个与另一
  通道"偶然一致"的代理指标，比诚实的"暂时判不了"更坏。阶段 1 把单局计数加进报告后，
  在 `SUPPORTED_DEFECT_CLASSES` 里加一行即可。
- `challenge_questions.py`（A 层校验 / K 预算 / 去重）、`context.py` 字段、
  `propose_challenge` / `respond_challenge` 工具、采样注入：全部属于**阶段 1**
  （它们只有在接线后才有可验证语义）。

### 12.6 阶段 0 硬门

| 门 | 结果 |
|---|---|
| balrog 12 套（389 断言）+ 三个新回归（35/26/48） | ✅ 全绿 |
| 框架 5 套 + `compileall gan domains agent utils` | ✅ 全绿 |
| 默认 `challenge.enabled: false` ⇒ 报告逐字段不变 | ✅ `compute_report_summary` 只在**不一致**时加字段；循环链路无人读 `challenge.*` |
| 两条通道结论一致性断言 | ✅ `test_challenge_adjudication.py`（39 断言） |
| 新增套件 | `test_challenge_ledger` 40 / `test_challenge_space` 36 / `test_challenge_adjudication` 39 |

---

## 13. 阶段 1a 落地记录（2026-11-19，已实现，默认仍关闭）

提交 `f2da9b1`。evaluator 能**提**、框架能**收**、下一代能**真跑**；真值结算与
eval 侧同族实例见 §13.5（尚未实现）。

### 13.1 链路

```
evaluator 盲期 propose_challenge(defect_class, family, evidence_ref, repair_criterion)
  -> EvalContext.challenges                      (工具不写账本，只记账)
  -> loop._challenge_lock_from_eval              (评估后立刻；盲锁)
       A 层：闭词表 / 族必须真出过该缺陷 / K 上限 / 去重 / 不引冻结 id / 多余字段拒
       B 层：domains/balrog/challenge_space.materialize（同族题对 + 难度档 + 去重）
       -> ckpt/challenges/ledger.json: locked -> materialized
  -> loop._challenge_available(parent, genid)    (下一代的计划前)
       reserve_for_child（只能被一个子代消费）
       tx.register_challenge_episodes(genid, train_id) -> ckpt/sample_seed.json
  -> task_broker（root 侧）把 challenge 实例并进 anchor 列表
       plan_episodes 先填 anchor ⇒ challenge 占自己家族的旋转位，冻结 anchor 核心不动
  -> planner 收到 challenge（无实例、无 seed、无题面）-> respond_challenge(stance)
  -> loop._challenge_consume                     (计划会话后；一代生命周期)
  -> loop._challenge_settle(parent, child)       (子代报告落盘后；机械判真值)
```

### 13.2 三条"不越界"的边界

1. **模型不碰题面**：proposal 只有 4 个字段，多余字段直接拒（防泄漏后门）；
   实例 seed 由框架派生（`challenge-ep` 独立 kind，与 anchor/rotate/eval 种子
   构造性不相交）。
2. **采样权仍在 root 侧**：challenge 实例走 `ckpt/sample_seed.json`，broker 只
   多做一次并集；**没有新增传输通道**，worker 仍然无法决定自己考哪几题。
3. **结算权在机械适配器**：planner 的 `respond_challenge` 只记立场（自由文本
   不进 record），是否修好由 `domains/balrog/challenge_adjudication` 从子代自己
   那一局判。

### 13.3 写这条链路时被抓到的两个 bug

- **episode 域的 anchor 核心不是 `sampling.anchor_k`**：那是**题目**计数（默认 10），
  episode 域用 `anchor_episodes_per_task: auto = min(n, max(1, n//2))`。按 10 算，
  预算检查会把健康运行拒掉（anchor_k(10) + max_k(2) > num_samples(10)）。现在
  `GanLoop._challenge_anchor_k` 按域类型分支，并有断言。
- **账本缓存属性与访问器同名**：`self._challenge_ledger = None` 遮蔽了
  `_challenge_ledger()`，每次调用都抛 `'dict' object is not callable`。改名
  `_challenge_cache` + `_challenge_data()`。

### 13.4 离线验收（`scripts/local/test_challenge_flow.py`，57 断言）

A 层 12 条（含"族里只有 aborted 局也要拒"）、B 层物化 4 条（真环境）、
采样注入 5 条（含"同一代不得偷换实例"）、两个工具的 10 条拒绝路径、
IPC 契约 3 条（`_validated_result` 的 exact-field-set 必须两侧同时含新字段）、
循环级 6 条（关闭时零副作用、开预算检查 fail-closed）。
另加 `test_challenge_tools_load.py`（loader 身份/模式门 + 归属分类）。

回归：balrog 12 套（389 断言）、`test_behaviour_facts` 35、
`test_action_classes` 26、`test_deadend_revisit` 48、框架 5 套、
`compileall`、阶段 0 三套（40/39/36）全绿。

### 13.5 阶段 1b（未做，需要决策）

**`instances.eval` -> `challenge_val`**。§4.1 写的是"outer 边界额外一轮，
固定 core 5 不可动 + challenge_val ≤ 2×K，单列不进 val_mean"。

两种落地形状，代价差很多：

| 形状 | 做法 | 代价 | 语义 |
|---|---|---|---|
| A（按原文） | outer 边界单独一轮 `cval_outer<N>`，broker 视作固定评估；只跑 challenge 实例（其它 task 用固定的 core 那一局占位） | 每个 outer +5 局（约 +1 个评估轮） | 泛化 = 超节点代码在**没见过的同族实例**上的表现 |
| B（更省） | 与 train 同批，报告里单列 `challenge_val` | 每代 +≤2 局 | 同上，但与被测代码同批同配置 |

A 需要改 `fixed_eval_launch_plan` + broker 的 scope 判定 + `DomainTaskRunner`
的 per-call override + 计划校验，属于**固定评估完整性**那条高信任路径；
B 只需在 `challenge_episodes` 里多放一个 id 并在报告侧分流。

**推荐 A**（与决策 D2 的"outer 边界泛化确认"一致，且不改变每代的批次构成），
但先把决策写进文档再动代码。

---

## 14. 阶段 1b 落地记录（2026-11-19，形状 A 已实现）

提交 `42b14d3`。**`instances.eval` 现在真的会跑**：每个 outer 在
`_evaluate_supernode` 之后多跑一轮 `cval_outer<N>`，用超节点代码打本 outer
所有被消费 challenge 的 eval 同族实例，结果进 `benchmark.challenge_val`。

### 14.1 数据流（零新增传输）

```
loop._challenge_val_round(outer)
  -> tx.register_challenge_val_episodes(ckpt/sample_seed.json: challenge_val_episodes["cval_outer3"])
  -> eval_runner(..., fixed_episode_override={"episode_ids": [...], "per_task": n})
  -> task_broker: scope_id.startswith(("eval_outer","cval_outer")) -> fixed_eval
  -> tx.fixed_eval_launch_plan(..., scope_id) 先查 challenge_val 形状
  -> 报告 episodes[] 按 episode_id 分流 -> benchmark.challenge_val
```

`per_task` 取"该 task 上 challenge 实例数的最大值"；**没有 challenge 的 task 用
冻结 core 的第一局占位**（否则 harness 会给它抽一局时间种子的旋转题——正是
`balrog_feval_1105` 那个坑）。

### 14.2 四个原本会静默出错的地方

1. **`cval_` scope 没有注册时必须报错**，不能退回核心评估计划——那会拿核心考卷
   冒充"泛化结果"。现在 `fixed_eval_launch_plan` 显式抛。
2. **runner 的共享 `fixed_episode_ids` 不动**：本轮走 `fixed_episode_override`，
   episode 分支、计划 tripwire、fail-closed 抛错三处都读 per-call 值，同一 outer
   的两轮不可能互换考卷。
3. **没有 challenge 的 task 必须给确定性的占位局**（见上）。
4. **超节点写入必须带 WriterToken**：诊断列也是 decision-grade 产物。

### 14.3 与 val_mean 的关系（硬约束）

| 数 | 来源 | 进 fitness / 选父 |
|---|---|---|
| `benchmark.mean` | 主线 train | 是（历史） |
| `benchmark.val_mean` | 冻结 core 评估轮 | 是（F_p） |
| `benchmark.challenge_val` | `cval_outer<N>` 独立轮 | **否**（诊断 + 泛化确认） |

`challenge_val` 里每道题三个字段：`defect_present`（同族新实例上缺陷是否仍在）、
`progression`、`generalized`（= `not defect_present`，`None` 表示行不可判/aborted）。
它**不折回**账本的 5 状态（adjudication 不可变，那一步在代边界就已结算）。

### 14.4 验收

`test_challenge_flow.py` 72 断言（新增 H 段 15 条：注册/占位/不可偷换、两种
launch plan 形状、fail-closed 拒、broker scope 谓词、runner override 钩子）。
全量回归：balrog 12 套（389 断言）+ 三个行为回归 35/26/48 + 框架 5 套 +
`compileall` + 五套 challenge（40/36/39/tools/72）全绿。

### 14.5 阶段 1 剩余的验收（要花钱，需批准）

§9 阶段 1 的硬门里还差两条，都需要**一次真实跑**（约 3 小时 / ~$2）：
- 一代真实跑确认 `instances.train` 出现在 `question_points`、`instances.eval`
  出现在 `challenge_val`；
- `UNDECIDABLE` 率 < 20%。

---

## 15. 阶段 1 验收记录（2026-11-21，**PASS**，八次真实跑）

### 15.1 八次跑各自的结论

| 轮 | 结果 | 根因 | 修法 |
|---|---|---|---|
| #1 | 出题永不成立 | 缺陷类/问题族两个闭集只写在提示词里，工作进程内不可达；随后发现工具在沙箱子进程里 import 域模块失败时**放行**（fail-open） | 两套闭集在进程侧算好、经 broker 镜像、逐字打印；工具对未知值 fail-closed |
| #2 | 全代 abort | 任务模型部署被限速（429），非逻辑问题 | 换 `openai/gpt-4.1-mini`（判定用 litellm 探针，直连 HTTP 的 503 是假信号） |
| #3 | 健康但无可出题 | 可引用集只按"域支持什么"算，没按"**这一代**真存在可证伪的差"算 | 每代评估前发布本代 citable 表；无题时明写"不要调用" |
| #4 | 出题成功、子代崩溃 → 题悬空 | 生命周期只有正向（出→用），没有失败归还 | `release_for_child`（保留→材料化，幂等，不碰已消费行） |
| #5 | 出题→锁定→消费→机械结算**全链真跑通**；泛化轮被拒 | `task_broker._SCOPE_RE` 安全白名单没有 `cval_outer<N>` 命名空间 | 显式加分支；`cost_ledger` 归因前缀同步 |
| #6 | 同上，且裁决首次给出 `VERIFIED_REPAIR` | 每 outer 启动预算只算 inner+eval，泛化轮是第 4 次启动 | `_launch_budget` 加 cval 项（预算=上界，多留一格零代价） |
| #7 | 泛化轮**真的启动并跑完 5 局**，仍被拒 | 传给 runner 的 override 是"只有挑战实例"，而 broker 钉的是"注册计划（含占位）" | 用 `register_challenge_val_episodes` 的返回值做 override 与 per_task |
| #8 | **五条判据全过** | — | — |

四轮失败里**没有一次是出题算法错**，全部是"与仓库其它层的接线"：词汇可达性、
沙箱 fail-open、限速、命名空间、启动预算、计划清单。这类缺陷离线套件抓不到，
因为它们只在跨进程/跨层时出现。

⚠️ 一个自造测量错误（记录在此以免重犯）：审计脚本 `audit_challenge_run.py` 早先
扫描 `ckpt/supernode_*.json`，而真实路径是 `ckpt/supernodes/outer_<N>.json`，
于是**对每一轮都报 `challenge_val` 为空**——包括第 8 轮列已落盘的情况。审计脚本
读错路径 = 把"我没看见"当成"不存在"，与 §12 的字段教训同类。已修。

### 15.2 第 8 轮验收证据（`/home/legion/outputs/challenge_stage1_verify8/A0`）

| 判据 | 证据 |
|---|---|
| ① `instances.train` 进 `question_points` | `gen 1: pickup#1784266009 -> progression=1.0`（**而且解出了**） |
| ② `instances.eval` 进 `challenge_val` | `challenge_val_ready {challenges:1, generalized:1, undecidable:0}`；supernode `benchmark.challenge_val["ch-1-0-0"] = {defect_present:false, progression:1.0, aborted:false, generalized:true}` |
| ③ UNDECIDABLE < 20% | `0.0 (0/1)` |
| ④ 默认关闭零影响 | 九套 challenge（40/36/39/tools/77/10/17/6/11）+ balrog 12 套 + 行为三套全绿 |
| ⑤ 不污染 `val_mean` | `mean=0.7`、`val_mean=0.4`，`challenge_val` 独立成列 |

同轮数值：主线 0.60 → **0.80**（两代均值 0.70，八次跑里最高）；分族 gen1 全部
≥ 1/2（goto 2/2、seq 2/2、pickup 2/2、open 1/2、putnext 1/2）；退化 revisit 局
占比 0.25 → **0.00**，`revisit_share` 0.588 → 0.305；泛化轮 4/5 解出；固定评估
2/5 = 0.40。花费 $5.51。

### 15.3 现在还不能说的

- **效用**没有数据：这是单臂（A0，challenge 开），没有 challenge 开/关对照，
  所以"出题让任务变强"仍未验证。
- 八轮合计只有 **4 道题被消费并结算**（locked 7 / consumed 4 / settled 4：`VERIFIED_UNRESOLVED` ×2、
  `VERIFIED_REPAIR` ×2，每轮各 1 道；第 1–4 轮一次都没有结算过，且它们的 run 目录已被
  早期启动脚本删除），任何比率（precision / calibration）都没有统计意义；三项指标仍
  **不接 fitness**，且 `_challenge_stats()` 连诊断都还没有调用点。
- 泛化结论只有 2 个可用样本（#7 训练实例仍在打转但兄弟实例正常；#8 两处一致）。


---

## 16. 出题指标接入（2026-11-21，实现完成，未做付费对照）

### 16.1 为什么只接一项

`F_e` 已经有三项：`-w1·z(calibration_error)`（w1=0.6，最大）、`w2·z(verdict_stability)`
（w2=0.4）、`λ·novelty`。在此之上再挂"三项出题指标"会犯两个错：

| 项 | 判定 | 理由 |
|---|---|---|
| `precision` → 改名 `claim_accuracy`（"我声称的缺陷真的存在吗"） | **接**，但是**混入**不是新增轴 | 它与 `verdict_stability`（"我的判定经得起机械核对"）是**同一个潜在变量**的两种仪器；相加 = 同一份证据付两次钱 |
| `actionability` | **不接** | 分母是 planner 的响应，记在 evaluator 头上是归类错误；且会奖励"挑好修的题"，与"浅层无效就升级深层"的规则冲突 |
| `challenge_calibration` | **不接** | `F_e` 已用 w1=0.6 读 `calibration_error`，这是它在"有题子集"上的重测 |

### 16.2 落的四个东西

1. **观测（此前完全缺失）**：`_challenge_stats()` 以前**没有任何调用点**。现在
   `_record_supernode` 调它，写进 `evaluator_stats.challenge`：
   `supported / undecidable / outer_count / claim_accuracy / actionability /
   calibration / transferable_share / modification_scopes / state_counts /
   authoring_rounds / authoring_missed / blend`。纯观测，零行为影响。
2. **一个混合项**：`challenge_metrics.blend_verdict_stability(vs_issue, metrics=…)`
   → `evaluator_stats.verdict_stability_effective`；`fitness_e` 优先读它，
   旧 supernode（无该键）回落到 `verdict_stability`。**分子分母共用一个读取器**
   （`fitness._verdict_stability`），z-score 两侧口径不会混。
3. **退火而非开关**：`w = weight_max · n/(n+shrink_n0)`，n=4→0.029、n=30→0.075、
   n=100→0.091；三道门（`n>0`、`independent_outers >= min_outer`、
   `determinate_rate >= determinate_min`）任一不过即 `w=0`。
4. **合规用规则**（`challenge_authoring_round`）：本代 citable 非空但没有任何题被
   接纳 → `challenge_authoring_missed` 事件 + 计数器（"rounds / missed"）落在
   **ledger** 上（resume 安全），并经 broker 镜像回灌给作者自己的提示词。
   区分三种情形：没提案（`no_proposal_despite_citable`）、全被拒
   （`no_admitted_proposal`）、框架自身失败（`judge=False`，**不算在作者头上**）。

### 16.3 惰性（这是硬要求，也是唯一的行为风险）

`blend` 不适用时返回的**就是** `vs_issue` 本身（同一个 float，不做任何再计算），
因此：challenge 关闭的运行、以及 §16 之前写的 supernode，`fitness_e` **逐位不变**。
用真实归档数据验证（`scripts/local/s16_preview.py`，第 8 轮）：
`outer_count=1 < min_outer=3` → `w=0.0000, applied=False, effective=1.0`（= `vs_issue`）。

### 16.4 顺带修掉的两个"从未被发现"的读取 bug

因为 `_challenge_stats()` 从来没有调用点，`challenge_metrics.compute` 的两个读取错误
在归档数据上一直是错的，也没人看见：

- `actionability` 读的是行的**顶层**键，而真实 ledger 把它放在 `adjudication` 里 →
  **每个归档 run 都报 0.0**。已改为两级回退（`_actionable_of`），并注明它读的是
  "裁决是否**可判**"，不是"这条建议是否可修"——后者正确的口径是
  `transferable_share`（`modification_scope == "transferable_strategy"` 的占比），
  本轮一并加入。
- `challenge_calibration` 依赖行上的 `prediction.judge_fix`，而**没有任何代码写这个键**
  → 恒为 `None`。保留 `None`（诚实的"缺失"）并在 docstring 里写明它还没有样本，
  而不是用 outer 级 `predicted_score`（另一个量）冒充。

### 16.5 回归

新增 `scripts/local/test_s16_wiring.py`（52 断言）：`outer_count` 语义、惰性（n=0 逐位
相等）、三道门各自独立生效、退火的单调与上界、`fitness_e` 两侧同口径、旧 supernode
回落、合规规则四种情形、以及计数器落在 ledger 上可 resume。
十套 challenge（40/36/39/tools/77/10/17/6/11/52）+ balrog 12 套 + 框架 5 套 +
`test_fitness_vs_norm` + `compileall` 全绿。

**未做（明确留待）**：§9 阶段 2 的付费 A/B（challenge 开 vs 关、各 3 次重复）。在拿到
那组数据之前，本篇只能说"指标接进去了且默认行为不变"，不能说"出题提升了能力"。
---

## 17. 补判定器：10 → 13 个（2026-11-21）

### 17.1 先量了一次：真值覆盖率比想象的低

对四个归档 run 的 `ckpt/issues/ledger.json` 做了一次普查（`scripts/local/issue_truth_census.py`）：

| 口径 | 数量 |
|---|---|
| 闭合词表 | **18** 个 key |
| 有机械判定器 | **10** 个（9 单代 + 1 配对） |
| 真跑里被 raise 过的 | **5** 种 |
| 真正产生过「配对真值」的 | **1** 种（`cost.tokens`，4 轮各 1 次） |

被 raise 的 5 种里，**3 种完全没有判定器**（`calibration.under-prediction`、
`design.prompt-ineffective`、`policy.under-discrimination`），它们在账本上的
`status: fixed` 是 **evaluator 自己说的**（`judged_fixed=1` 但 `verified=0`）。
另一件值得记的事实：`process.dead-end` 在 4 轮里**全部被判 `unsupported`**（父代本就
没有该缺陷）——evaluator 最常提的那类 issue，在机械口径下不成立。

### 17.2 补的三个（全部由已记录的事实决定，零 LLM）

| key | 判据 | 线是怎么定的 |
|---|---|---|
| `calibration.under-prediction` | `score - predicted_score > calibration_tol` | tol=0.15 = 一个 episode 的分辨率（10 局时 0.1）加余量。**provisional**：四轮实测误差最大 0.05，全部在 5 倍线内 |
| `calibration.over-prediction` | 镜像 | 同上 |
| `process.observability` | `trajectory_rows < steps_total × observability_min_rows_per_step` | 实测健康 run 的 rows/step = 2.00 / 2.02 / 2.67（#8）、3.00×3（#7）、3.00×3（#6）；线取 1.0 ⇒ 健康侧 2 倍余量。而它要抓的历史事故（2026-11-08 `balrog_verify_1108a`：10 局 / 98 个 step 子进程只归档 10 行 ≈ 0.1）在**一个数量级以下** |

两条硬规则照旧：

- 缺证据 = `UNKNOWN`，**永不**当 `ABSENT`。`calibration.*` 在 `score_status` 不是
  `ok/partial` 时一律 UNKNOWN——否则每个未评分的代都白拿一次「预测没跑偏」的命中；
  `process.observability` 在缺少归档或缺少 `steps_total`（题目域没有这个概念）时同样 UNKNOWN。
- 两条新线都是**参数**（`issues.check.calibration_tol` /
  `observability_min_rows_per_step`），不是写死在谓词里的常数。

### 17.3 仍然刻意没有判定器的 5 个（附理由）

| key | 为什么不补 |
|---|---|
| `design.prompt-ineffective` | **旧决定，继续遵守**：这是「这次编辑有没有用」的判断，锚点/分数对已经量了它；再造一个机械孪生只会让「未修复」变得trivially 正确 |
| `policy.under-discrimination` | 需要读观测里的**文字**（"是否出现了明确的拒绝信号"）——语义判断，是 LLM-judge 的首选候选 |
| `design.toolset-mismatch` | 同上：装配的工具是否服务这个任务，需要任务语义 |
| `generalization.overfit` | 目前**没有 per-node 的 val 度量**（val 只到 supernode 级），配不出配对真值。等某轮开始逐代留 val 再补 |
| `other.new` | 逃生口，按定义不判 |

### 17.4 顺带修掉的本地测试陈旧断言（不是本次逻辑改动引起的）

- `test_llm_retry_budget.py` 的 `_behaviour_summary(spin, len(spin))`：该函数早已
  增到 `(state_keys, actions, num_steps)`，测试仍是旧签名 → 直接 TypeError。
- 同一文件的 R10d 期望 gen5 为 PRESENT，依据的正是 **2026-11-17 已被删除**的
  `obs_repeat_step_share >= 0.35`（符号反了）。改成 ABSENT 并把 docs/20 §8 的结论
  写进注释：gen5 现在是那次错误的**反例标本**。
- `test_verdict_stability_v2.py` 有一条 `AccessBroker.grant` 断言失败（59 里 58），
  **已用 `git stash` 复核：改前改后同样失败**，属环境/权限问题，与本次改动无关。

### 17.5 回归

新增 `scripts/local/test_issue_checks_new3.py`（**28 断言**：真实归档值 0.55/0.60、
0.80/0.80、913/452、2184/728 全在健康侧；历史事故 10/98 必须 PRESENT；容差与 floor
可被 `issues.check.*` 覆盖；四种缺失情形一律 UNKNOWN；`pair_truth` 的
present→absent = 修复、父代本就 absent = unsupported；loop 的两个落盘助手在真实 run 上
读出 913 / 452）。登记表断言同步到 13 个。
十套 challenge（40/36/39/tools/77/10/17/6/11/52）+ balrog 12 套 + 行为三套
（35/26/48）+ 框架 5 套 + `test_fitness_vs_norm` + `test_llm_retry_budget`（45/45）
+ `compileall` 全绿。

### 17.6 现在能不能引入大模型 judge

**还不能下结论，但这次的普查给出了判据**：13/18 之后，剩下 5 个里真正值得 LLM 判的只有
`policy.under-discrimination` 与 `design.toolset-mismatch`（都需要读观测文字）。
是否引入应当取决于：这两类 issue 在新跑里出现的频率，以及机械侧能否为 judge 提供
可对齐的重叠样本（`process.dead-end` 是现成的校准类）。在此之前，judge 只会多一个
漂移源而没有可验证的增益。

---

## 19. issue ↔ challenge 软绑定（2026-11-22）

### 19.1 缺口

`propose_challenge` 与 `report_issue` 是**两条互不相连的通道**：工具没有 `issue_id`
参数，账本行里也没有这个字段，loop 锁题时从不读 `ctx.issues`。唯一的重叠是
`defect_class` 与 `issue_key` 共用同一套名字。

后果是三个，都会让后续的判读失真：

1. **§16 的混合项变成"两把尺子加权平均"**：`claim_accuracy` 的样本是"出的题"，
   `verdict_stability` 的样本是"报的 issue"，两者身份不同却被混在一起；
2. **分不清"issue 是真的但题没出成"和"issue 本身是假的"**——前者该改准入，后者该改判据；
3. **回答不了"出题能力是否以挑错能力为地基"**，因为算不出"报过的 issue 里有多少被出成了题"。

### 19.2 软绑定（为什么不是硬绑定）

硬绑定（必须引用已报 issue 且 key 一致）在实测里会把唯一的题也拒掉：3-outer 那一轮
只有 1 代可引用（gen1，putnext），而该代报的 issue 是 `cost.tokens` / `process.dead-end`，
族与 key 都对不上。所以：

| 情形 | 行为 |
|---|---|
| `issue_id` 缺省/空 | **接受**，记为未绑定，留 `challenge_unlinked` 事件 |
| `issue_id` 指向不存在的 issue | **拒绝**（悬空引用比没有引用更糟），`challenge_rejected: unknown_issue_id:<id>` |
| `issue_id` 存在但 `issue_key != defect_class` | **拒绝**，理由里点名两边（`issue_key_mismatch`） |
| 一致 | 通过，账本记 `issue_id` + **解析出的** `issue_key`（不采信作者自报） |

引用比较是**代际无关**的：作者写 `i-1`，而 `_qualify_issue_ids` 已把 issue 改写成
`g<genid>/i-1`，两者视为同一条。

### 19.3 落了什么

| 层 | 改动 |
|---|---|
| 工具 | `propose_challenge` 增参 `issue_id`（**非必填**）；内部解析 + key 校验；答复里明说 `linked` / `UNLINKED` |
| 形状校验 | `challenge_questions.LINK_FIELDS`（`issue_id`/`issue_key`）被允许并原样保留；其他多余字段仍被拒 |
| 账本 | `challenge_ledger._OPTIONAL` 把两个字段写进账本行（空串也写，不是缺失）；`lock_payload` 包含它们，所以"重锁时换了 issue"能被检出 |
| loop | 新增 `_challenge_resolve_issue(...)`：工具可被绕过 / 提案可能来自旧子进程，所以**父侧复核一遍**；`challenge_locked` 事件带 `issue_id`/`issue_key` |
| 指标 | `compute()` 增 `linked_challenges` / `unlinked_challenges` / `join_rate` / `by_issue_key`（按缺陷身份分解状态） |
| supernode | `evaluator_stats.challenge` 增 `linked` / `unlinked` / `join_rate` / `by_issue_key` |
| 提示词 | 盲评阶段的出题说明新增一条 `issue_id` 用法（不引用仍合法） |

### 19.4 判读方式

- `join_rate` = 已结算的题里"带了与 key 一致的 issue 引用"的比例。它是"出题是否以挑错为地基"
  的**直接测量**；累积几轮后再决定要不要升级成硬绑定。
- `by_issue_key` 让 `claim_accuracy` 能按缺陷身份读，这正是将来把 §16 的混合项
  从"两个样本集平均"改成"同一变量更严的测量"的前提。
- 未绑定不是错误：它是**被观测到的状态**。若某轮 `join_rate = 1.0` 且 `unlinked = 0`，
  说明作者已经在按 issue 出题，那时再谈硬约束才有意义。

### 19.5 回归

新增 `scripts/local/test_issue_challenge_link.py`（**31 断言**）：工具四种情形
（未引用/匹配/未知 handle/key 不一致）+ `g<N>/` 与裸 handle 等价；`parse_proposal`
放行 link 字段但继续拒未知字段；账本行两个键必在（空串语义）；`lock_payload` 含 link；
`compute` 的 join 率与 `by_issue_key` 分桶；loop 侧四种复核路径与事件。
十一套 challenge（40/36/39/tools/77/10/17/6/11/52/31）+ balrog 12 套 + 行为三套 +
框架 5 套 + 13 个 checker（28 断言）+ `test_llm_retry_budget`（45/45）+ `compileall` 全绿。

## 20. P2：出清不再能废掉一轮（2026-11-22）

### 20.1 缺口（实测，不是推测）

三轮验证运行 `a0_o3i3n2`（outer=3 / inner=3 / n=2，9 代）里 **2 代被判无效**
（`score: null`），另有 2 代被插补。日志里的原因是一条看起来跟权限毫无关系的
异常：

```
PermissionError: [Errno 13] Permission denied: 'cm'
  at role_process._purge_dir_fd   (在 _worker_main 的 finally 里)
→ 父进程看到 "role worker PermissionError" → planner_failed → inner_invalid
```

根因两段，缺一不可：

1. **父进程拷贝目录时没有归一模式。** `_safe_copy` 会给**文件**补 `0o660`
   （2026-11-02 的 ACL mask 教训），但**目录**一直是 `os.makedirs` 在
   `umask 0o077` 下建出来的 `0700`。`gan/components/task/cm` 就是这样被 stage
   成 `drwx------`（owner 是 driver，不是角色 uid）。
2. **出清（purge）会抛异常，而它在 `finally` 里。** 递归删除走到那个目录时
   `os.open(... O_DIRECTORY)` 直接 `EACCES`；`finally` 里抛出的异常**覆盖了已经
   算好的返回值**，于是一次纯清理问题把一代人的工作判死。

关键点：**这不是模型的问题，也不是判定的问题**，是框架自己把好的一代丢掉。
只要不清掉，P1（扩缺陷类）跑出来的任何数据都会被这种噪声污染，每轮还要白烧
约 \$2.5。

### 20.2 修法（三处，最小改动）

| # | 变更 | 落点 |
|---|---|---|
| P2-1 | 出清**永不抛**：每一步单独 try/except，收集 `skipped` 列表；外层再包一层，任何异常都只进 `skipped` | `role_process._purge_dir_fd` / `_purge_role_workspace`（返回值 `None` → `List[str]`） |
| P2-2 | 拷贝后**归一目录模式**：`AccessBroker.ensure_traversable(root, mode=0o770)`，`grant()` 末尾在可读性自检**之前**跑一遍 | `access.py`（新增静态方法 + grant 接线） |
| P2-3 | 残留**必须可见**：一行有界的 `[purge] role/instance: N entr(y/ies) left in place: ...` 进 stderr（arm 日志），并 append 一条 `workspace_purge_skipped{role,seat,instance,count,entries}` 到 `events.jsonl`（走既有的"父进程持有审计写权"通道） | `_worker_main` 的 finally 尾部 |

两个刻意的取舍：

- **顺序**：归一在 `unreadable` 自检**之前**。自检必须报告角色**真的**能读到什么；
  先自检就等于把一个我们自己能修的 `access.grant-broken` 缺陷挂到账上。修了几个
  目录这件事本身记成事实字段 `dirs_traversable`（干净拷贝为 0）。
- **`0o770` 而不是 `0o750`**：继承给角色的 ACL 条目是 `u:<role>:rwx`，它的
  `effective` 由 **mask**（= group 位）决定；把 group 写位摘掉会让角色重新变成
  `#effective:---`。这与 `_safe_copy` 用 `0o660` 是同一条规矩。所有权仍不动
  （chmod 只改 mask，从不改 owner）。

### 20.3 验收（全离线，零花费）

新增 `scripts/local/test_purge_failsoft.py`（**6 组 / 18 断言**）：

1. 锁死的子目录 `0000` → **不抛**、返回 skip 列表、stderr 只有一行摘要、
   能删的都删了、删不掉的那个**保留**（不静默丢）；
2. 干净工作区 → skip 为空、根目录被清空（根本身保留）、无 stderr 噪声；
3. 工作区不存在 → no-op；
4. **`finally` 契约**：按 `_worker_main` 的形状跑一遍"先算出结果、再在 finally
   里出清"，结果对象原样返回——这正是当初杀掉两代的机制；
5. `ensure_traversable`：三个 `0700` 目录 + 根被修成 `0770`、文件不动、
   第二遍是 no-op（幂等）；
6. 修完之后目标目录可列出、文件可读。

`test_ws_acl_mask.py` 从 11 → **20 断言**，新增真实 uid 的目录版：

- **D1/D1b** 复现线上签名：目录 `0700` + `u:rsi_gan_planner:rwx` + `mask::---`
  → 角色 `ls` 被拒、`getfacl` 显示 `#effective:---`；
- **D2–D5** 归一之后角色能进、能读、mask 不再为零；
- **D6** 幂等；
- **E1/E2** 修好之后，当初那次失败的真实路径（`_purge_role_workspace` 扫 0700
  目录）现在**清得干净**，一个残留都没有。

### 20.4 为什么值得单独记一条

失败的表现是"规划器失败"，真因却在清理代码里——一层完全不该有副作用的代码。
这也解释了 `a0_o3i3n2` 的 `citable` 只有 1/9 的一部分原因：**有两代根本没跑完**。
所以 P2 必须先修，P1（扩缺陷类）的测量才有意义。

### 20.5 回归

十三套 challenge（40/36/39/tools/77/10/17/6/11/52/31/**18**）+ balrog 12 套 +
框架 9 套（20/18/35/26/48/45/28/60/repo_split）+ `compileall` 全绿。
顺带修掉三处**陈旧本地断言**（都不是本次逻辑改动引起的，与 §17.4 同类）：

- `regress_repo_split.test_evaluate_supernode`：仍假设 `eval_runner` 就是
  `task_runner`（docs/14 §5.5 之后是**两个** runner），且把固定根评测的批次均值
  断到 `benchmark.mean` 上（现在的契约是 `val_mean` / `val_n_valid`）；
- `regress_repo_split.test_cooldown_and_verdict_stability`：仍断言旧的"共享
  一位"版 `verdict_stability == 1.0`（现在是按 `issue_key` 机械绑定的，没有可
  验证的声明就是 `None`）；
- `test_verdict_stability_v2` C2 段：拿 `gan/design/seeds/planner.md` 当可授权
  路径（种子**按设计**不在任何角色的读写根里），改为授权 `gan/roles/planner.py`，
  并把"种子不可授权"加成一条正面断言（58/59 → **60/60**）。

## 21. P1-B 投影 + `process.step-exhaustion`（2026-11-23）

### 21.1 缺口：事实被算出来了，但没留下来

harness 每局都算好了 `done`、`action_frequency`、
`no_progress_action_counts`，写进了 episode JSON；但 `report.json` 的
`episodes[]` 只保留**精简投影**（`steps` / `progression` /
`invalid_actions` / `llm_calls` / 四个行为数字），而 `work/<genid>` 是临时的、
train/eval 的 episode JSON 跑完就没了——整个 `a0_o3i3n2` 只剩下 cval 那 10 个。

后果：某个事实**在写入时存在，跑完之后不可恢复**。任何依赖它的判据
（checker / adjudicator / 离线审计）只能"运行时看得到、审计时看不到"，这正是
把机械真值做厚的路上最不该有的形状。

顺带暴露第二个问题：**步数上限根本不在行里**。`steps` 有，cap 没有，于是离线
读者只能凭族常量去猜（goto/pickup = 64，其余 = 128）。一个要用来判"预算用完了"
的判据，不能建立在"猜上限"之上。

### 21.2 落点（纯新增，不删不改）

| 层 | 字段 | 出处 |
|---|---|---|
| episode JSON | `env_done`、`step_cap`、`stop_reason`（`env_done`/`step_cap`/`loop_stop`，中断局 = `aborted`） | `domains/balrog/evaluator._play_episode` / `_record_aborted` |
| `report.json` 行 | 上面三个 + `action_counts` + `no_progress_action_counts` | `domains/balrog/utils.collect_and_summarize_results` |
| `report.json.behavior` | `stop_reason_counts`、`outcome_decided_episodes`、`episodes_step_exhausted`、`step_exhausted_share` | 同上 |

两条规矩写进代码注释：

- **`stop_reason` 是"循环怎么结束的"，`progression` 是"结果如何"**。两者必须
  分开："预算用完"和"没解开"是两种缺陷，只有这一对能分开它们。
- **中断局永远是 `stop_reason: "aborted"`，绝不能被读成 `step_cap`**——否则上游
  一个 502 会被记成 agent 的行为缺陷。

`behavior` 逐字透传（`compute_report_summary` 是 `dict(behavior)` 拷贝），所以
框架侧不需要额外接线。

### 21.3 新增可判缺陷类 `process.step-exhaustion`

- **词表**：`gan/framework/issue_keys.py` 18 → **19**，`owner=planner`，
  含义 "spent the whole step budget without finishing the task"。
- **逐局判据**（`domains/balrog/challenge_adjudication.episode_step_exhaustion`）：
  **绝对判据，无阈值**。
  - 有 `stop_reason`：`step_cap` → `progression < 1`；`env_done` / `loop_stop` → 否；
    `aborted` → `None`（不可判，不冤枉 agent）。
  - 旧报告（无 `stop_reason`）：`step_cap` + `steps` 都在时回退到
    `steps >= step_cap and progression < 1`；两者缺一则 `None`——**不猜族常量**。
- **不需要 `defect_lines`**：`adjudicate` 原本"没有声明就拒判"，现在改成按类判断
  （`_NEEDS_LINES = {"process.dead-end"}`）。一个绝对类必须能在没声明任何缺陷线的
  域里被回答；`process.dead-end` 仍然必须声明（行为逐字节不变）。
- `SUPPORTED_DEFECT_CLASSES` → `("process.dead-end", "process.step-exhaustion")`，
  出题侧（`citable` / `defect_classes`）自动跟上，`issue_key == defect_class` 的
  软绑定约定自然满足。

### 21.4 为什么**刻意没有** generation 级 checker

`issue_checks` 一个都没加。理由是量出来的，不是省事：

对归档的 **117 代 / 28 个 run·arm** 逐代算"步数耗尽且未解开"的占比：

```
min 0.000   p25 0.250   median 0.400   p75 0.667   max 1.000   mean 0.455
share == 0 的代数：20/117 (17.1%)
```

- **各臂几乎不分离**：`ablation_babyai_arms_1112b` A0 ≈ 0.60–0.90、
  A1 ≈ 0.40–0.60、A2 ≈ 0.30–0.70，重叠严重；
- **族偏斜严重**：putnext 84.2%、open 69.0%、pick_up_seq_go_to 33.2%、
  pickup 26.4%、goto 19.7%。

`verdict_stability` 要的是"父代有 → 子代没有"的**固定根**比较；在这个基线和方差
下它会变成抛硬币，并且会把一个"每代都在"的东西钉成永远 PRESENT——那是把真值
做脏，不是做厚。所以这一类**只走 challenge 通道**（单实例、父子对照），
generation 级留空（`UNKNOWN`，`F_e` 里中性）。

### 21.5 先证明"不是题目太难"

如果 cap 太紧，这类就是基准属性而不是 agent 缺陷。用
`domains/balrog/challenge_space.optimal_steps` 对**同 (family, seed)** 重放
`BabyAIBot`：

| 族 | 耗尽预算且未解开 | 最优策略只需 | `optimal >= cap` 的实例 |
|---|---|---|---|
| putnext | 12 / 14（全烧 128 步） | 9–15 步 | 0 |
| open | 5 / 15（烧 128） | 10–14 步 | 0 |
| goto | 1 / 18（烧 64） | 4 步 | 0 |
| pickup | 1 / 18（烧 64） | 7 步 | 0 |
| pick_up_seq_go_to | 1 / 15（烧 128） | 18 步 | 0 |

没有一个实例的最优步数接近上限，所以**上限完全不是瓶颈**，这是真实的不收敛。

### 21.6 与 `process.dead-end` 的分工

`dead-end` 看的是**绕圈**（同一 `(state, action)` 反复重试、零位移）。putnext
这批恰好相反：12 个耗尽局里 8 个 `revisit_share < 0.40`——**一直在到达新状态
却始终收不了尾**。方向相反的两种病，修法也不同（"别再打转" vs "收敛/规划"），
所以是两个键而不是一个键的两个变体。

### 21.7 验收与回归

新增 `scripts/local/test_step_exhaustion.py`（**44 断言**，四段）：

- **A 词表**：新键在闭集、owner=planner、总数 18→19、旧键不变、未知键仍归
  framework；
- **B 真值表**：`step_cap`/`env_done`/`loop_stop`/`aborted` 四种取值，旧报告
  回退，缺字段 → `None`；`defect_families` 的分母里 aborted 不计；
- **C 端到端**：父耗尽/子完成 → `VERIFIED_REPAIR`（**空 `defect_lines` 也能判**）；
  双 absent → `UNSUPPORTED`；父 absent 子 present → `REGRESSION_AFTER_FALSE_CHALLENGE`；
  父为中断局 → `UNDECIDABLE`；同时验证 `dead-end` 在无声明时**仍然**拒判；
  A 层 `citable_families` 用**同一条规则**（框架侧新增
  `classes_without_lines_for`，豁免由域自己声明，框架不猜），否则会出现
  "可判但不可引"的不对称；
- **D 投影**：合成 episode JSON（含一条无 `stop_reason` 的旧行 + 一条中断局）走
  真实的 `collect_and_summarize_results`，断言行字段、`behavior` 聚合、
  `step_exhausted_share = 1/2`，且 `progression` / `question_points` 一字不变。

校准与普查脚本（本地，`scripts/local/`，不入库）：
`census_candidate_classes.py`（8 个候选类的 liveness 门槛）、
`census_episode_json.py`（只有 episode JSON 才有的动作分布）、
`check_optimal_steps.py`（§21.5）、`calibrate_step_exhaustion.py`（§21.4 的 117 代）。

回归：13 套 challenge（40/36/39/tools/77/10/17/6/11/52/31/18/**44**）+
balrog 12 套 + 框架 9 套 + `compileall` 全绿。`test_challenge_flow.py` 里
"可判缺陷类必须只有 `process.dead-end`" 这条断言随词表一起更新为两类。


## 22. Balrog challenge 保障对齐（2026-11-24）

- **准入**：旧诊断实例与新 train/eval 题对使用同一可解性和难度档校验；修复条件冻结为机械判据的规范义务，原提案另存；排除旧布局及固定题集 seed。题对生成成功后再原子落账，失败不留可消费空锁；issue 仍软绑定，带代号的错误引用拒收。
- **消费与门禁**：实际 episode plan 含唯一 train 实例才消费。未调度则释放预留；中断、缺证据、重复 ID 和非法 progression 不授修复信用。task 运行前检查旧题、train、eval 标识及受保护答案字段，命中则拒绝修改；逐条响应、有限补录和实际修改分开审计，补录仅开放 respond_challenge。
- **机械裁决**：保留两个缺陷类及进度不降约束；记录 checker 摘要、父子证据摘要、实例身份和进度变化。缺旧题难度证据的历史记录保守判未知；已结算结果不重写。机械可判性不等于修改具备语义可迁移性，未命中硬编码扫描不再自动标成 transferable_strategy。
- **验证对象**：固定集父子比较与隐藏题使用同一实际候选。固定集保持原评测题目；隐藏题仅取该候选实际消费的挑战，不扫描历史挑战。使用持久化代码及配置，结束恢复主线；多题同族时补齐各族固定槽位，避免随机填充。缺失、aborted 或执行失败的隐藏结果保留未知。
- **信用**：evaluator 仍只使用原 claim-accuracy blend。planner gate 复用现有开关（默认关闭），按 progression 口径读取候选挑战；正向信用要求同候选的固定集父子完整覆盖并改善，partial 均分保留但不足以证明改善。隐藏结果单列，不覆盖训练裁决或重复奖励。启动预算和成本归因覆盖 taskval/cval。
- **范围**：Balrog 仍采用旧实例对新实例的同族验收，不能视为同题因果配对或全面语义可信证明。IMO 模型、v3 协议和原题流程保持；IMO 出题和 planner 指导探索另议。

