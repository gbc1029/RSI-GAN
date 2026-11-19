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
