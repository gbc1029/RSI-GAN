# 21 — balrog 出题式对抗挑战：设计与实施计划

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
