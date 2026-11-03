# IMO proof 对抗性 Challenge 测试设计

> 状态：设计定稿，尚未实现。
>
> 适用范围：`imo_proof`。本设计保持现有 task / planner / evaluator 三角色、inner / outer
> 双循环和外部 proof grader，不引入第四个可进化角色。
>
> 前提：context-manager 已能让 evaluator 完整、稳定地读取其被授权的 task trajectory。
> context-manager 的修复与验收不属于本文范围。

---

## 1. 整体设计：由外部证据裁决的 Challenge-Response 对抗

### 1.1 现有设计为什么还不够“对抗”

当前框架已经存在如下闭环：

```text
evaluator.report_issue
        -> planner.respond_issue
        -> planner 修改 task
        -> evaluator.judge_fix
```

这个闭环提供了角色隔离、反馈和复查，但仍主要是 reviewer 工作流：evaluator 可以在看到结果后
提出宽泛 issue，planner 也可以用宽泛修改回应；双方没有在结果揭示前提交一个可证伪的主张，
`judge_fix` 又是 evaluator 自己的判断，不能作为其自身正确性的真值。

本设计不替换上述机制，而是在其中增加一类更严格的 **challenge**。普通 issue 继续处理格式、
成本、过程质量和无法形成严格挑战的缺陷；challenge 只用于 evaluator 愿意在 blind 阶段押注、
且能够由外部证据裁决的 proof 缺陷。

### 1.2 核心流程

一次 task inner generation 仍执行原来的 planner -> task -> evaluator 流程，但增加 challenge 的
产生、消费与结算：

```text
父节点 p 已封存的 challenge
        |
        |  p 被选为直接父代时，一次性绑定到边 p -> c
        v
planner 在不知道 grader 分数、参考解和 val 的前提下响应 challenge
        |
        v
task 在“固定 train anchors + challenge anchors + 随机 train 题”上运行
        |
        v
evaluator blind：
  1. 判断上一批 challenge 是否看起来已修复；
  2. 对当前节点 c 提交最多 K 个新的、可证伪 challenge；
        |
        | challenge 在 benchmark / grader 结果揭示前锁定
        v
外部 proof grader 给父子证明打 0..7 分；
framework-owned challenge adjudicator 判定缺陷在父子证明中是否存在
        |
        v
framework 结算 planner / evaluator 的对抗状态与指标
        |
        v
outer 边界：在隐藏的 held-out val 上检查修复是否泛化
```

challenge 的最小结构为：

```json
{
  "challenge_id": "框架生成",
  "qid": "当前 train 题目的 ID",
  "defect_type": "missing_lemma",
  "claim": "一个可判断真假的缺陷陈述",
  "evidence_ref": "当前 task trajectory / 最终证明中的定位证据",
  "repair_criterion": "什么变化才算消除该缺陷"
}
```

`challenge_id`、生产者 evaluator 版本、父节点和 outer 均由 framework 盖章。evaluator 不能提交
val/test 题、未在当前 generation 中实际运行的题，或没有可定位证据的 challenge。

### 1.3 对抗性体现在哪里

对抗性不来自“evaluator 给 planner 更多建议”，而来自以下三个约束：

1. **先押注，后揭示**：evaluator 必须在不知道 grader 分数时锁定 challenge；不能根据低分结果
   事后编写一个永远正确的解释。
2. **同一主张、相反目标**：对于外部裁判确认真实且可修复的缺陷，planner 的目标是让缺陷在
   子代消失；evaluator 的对抗目标是找到经 planner 尝试后仍然成立的真实缺陷。
3. **第三方裁决**：evaluator 的 `judge_fix` 只是预测。challenge 真伪和是否修复由固定、不可进化
   的 grader/adjudicator 与父子同题分数共同裁决，任何一方都不能自证成功。

因此，容易且被 planner 修复的 challenge 使 planner 获胜；虚假的 challenge 使 evaluator 受罚；
真实且在修复尝试后仍存在的 challenge 才是 evaluator 的对抗胜利。

### 1.4 与以前设计的区别

| 项目 | 原设计 | Challenge 设计 |
|---|---|---|
| evaluator 输出 | 普通 issue，可宽泛、可事后细化 | blind 阶段锁定的、题目绑定、可证伪 challenge；普通 issue 仍保留 |
| planner 响应 | 表态并修改 task | 表态、修改 task，并在同一道 challenge 题上接受外部复测 |
| 修复真值 | evaluator 的 `judge_fix`；部分通用 issue 有机械 checker | `judge_fix` 仍记录，但 proof challenge 真值来自固定 adjudicator |
| 对抗目标 | issue 是否被接受/复查，目标总体协作 | 真实 challenge 上：planner 消除缺陷，evaluator 寻找能抵抗修复的缺陷 |
| 泛化检查 | outer fixed-root val 主要衡量 planner | challenge anchor 检查指定修复，独立 task val 检查是否泛化 |
| 分支处理 | issue 跟随选中的父节点反馈 | challenge 绑定生产节点，只有成为直接父代时才一次性消费 |
| 版本归因 | outer 结束后的新设计可能继承旧版本指标 | 指标归属于实际产生/消费 challenge 的版本 ID |

---

## 2. 实现细节与问题裁决

### 2.1 不采用 `predicted_max_points`

第一版删除 `predicted_max_points`。它只能与该题实际得分比较：

```text
point_prediction_error = abs(predicted_max_points - actual_points) / 7
```

但它与 evaluator 已有的 blind calibration 高度重复；一题的实际分数同时受多个缺陷、proof
grader 噪声和 task 随机性影响，不能证明 evaluator 对某个根因的判断正确。小 K 下这个误差也很
不稳定。challenge 的定量信号改为“缺陷是否被外部裁判支持”与“父子缺陷状态是否变化”。若后续
实验确认点数预测有独立信息，再作为诊断字段加入，不进入首版 fitness。

### 2.2 Challenge anchors 与过拟合防线

challenge 题必须进入子代训练批次，否则 planner 的修复没有同题验收；但 challenge anchor 只
证明“指定缺陷被处理”，不能证明泛化。因此采用两个互不替代的检查：

1. **Challenge anchors**：来自直接父节点 evaluator 锁定的 challenge qid，检查指定缺陷及同题
   分数变化。
2. **Independent held-out val**：来自 `_filtered_100_val` 的固定隐藏题，检查 task 改动是否泛化。

planner 不能看到 val 的题号、题面、逐题结果或 grader 反馈。val 不进入 task 的主线 train
批次，也不进入 planner/evaluator 的可读 trajectory。

仅通过 challenge 检查的修改叫“局部修复”；同时通过 challenge 和 val 检查的修改才叫
“泛化修复”。

### 2.3 两类检查的初始定量标准

#### Challenge gate

小 K 不适合直接使用未经平滑的百分比。指标在一个 outer 内按“实际执行 inner 的 planner
版本”聚合，并采用拉普拉斯平滑：

```text
repair_rate_raw      = verified_repair / supported_challenges
repair_rate_smoothed = (verified_repair + 1) / (supported_challenges + 2)
```

首轮实验的 challenge gate 为：

```text
supported_challenges >= 2
repair_rate_smoothed > 0.5        # 等价于已修复数严格多于未修复数
mean_anchor_point_gain >= 0
false_challenge_regression == 0
```

若 `supported_challenges < 2`，状态为 `insufficient_evidence`，既不算通过，也不算失败，不产生
正负 repair fitness。这样一题不会决定整个 outer。

#### Independent val gate

对 outer 结束前、在读取 val 前按现有 train LCB 选定的 task candidate 及其直接父代，在同一组
隐藏 val 题上做成对评估：

```text
task_val_delta = task_val_mean(candidate) - task_val_mean(parent)
```

当前 val 有 20 题，每题 0..7。首轮最小有效提升暂定为“总计至少增加 2 个原始 IMO 分”：

```text
val_min_delta = 2 / (7 * val_n_valid)
              = 2 / 140
              = 0.0142857...       # val_n_valid = 20 时
```

最终的泛化修复验收条件为：

```text
challenge_gate == passed
task_val_delta >= val_min_delta
```

这是 pilot 阈值，不宣称统计显著。首轮同时安排相同 task/config 的 no-op 重复评估，得到空效应
噪声分布；样本足够后改为：

```text
val_min_delta = max(2 / (7 * val_n_valid), Q75(abs(no_op_val_delta)))
```

在累计至少 30 个 `supported_challenge` 前，不根据少量数据重新调 challenge gate。

### 2.4 每代题目组成与当前 train/val 的关系

当前文件不是运行时把 train 临时砍半：

- `_filtered_100_train` 实际有 40 题；
- `_filtered_100_val` 实际有 20 题；
- 常用配置为 `num_samples=30`、`sampling.anchor_k=10`、`eval.anchor_k=20`。

新主线 train 批次固定为 30 题，按以下顺序去重组成：

```text
A_base      = run 开始时固定抽取的 train anchors（当前 10）
A_challenge = 直接父节点尚未消费的 challenge qids（最多 K）
R_random    = 从其余 train pool 按原 seeded 规则无放回补足到 num_samples

B_train = unique(A_base + A_challenge + R_random)
```

首轮取 `K=5`。在当前参数下，最坏情况为 `10 + 5 + 15 = 30`，仍保留至少 15 道随机题。
challenge qid 与固定 anchor 重合时只占一个位置。

`A_base` 与 `A_challenge` 必须分别记录：现有 LCB、stagnation 和跨代可比的 anchor 指标仍只读取
`A_base`。动态变化的 challenge 题绝不能混入固定 anchor 统计，否则会破坏现有选父语义。

启动时执行以下约束：

```text
0 <= K <= num_samples - sampling.anchor_k
random_min <= num_samples - sampling.anchor_k - K
challenge qid 属于 train subset
train IDs 与 val IDs 不相交
```

首轮 `random_min=10`，当前配置满足约束。

### 2.5 什么算一代，以及 val 是否会被反复使用

一代仍等于一个现有 inner generation：

```text
选直接父代 -> planner 响应/修改 -> task 执行 -> evaluator 评估并产生新 challenge
```

challenge 有一代延迟：节点 `p` 的 evaluator 产生 challenge；当 `p` 后来被选为直接父代时，
其 challenge 才在边 `p -> c` 上由 planner 消费和结算。这不新增第三层循环。

**不在每个 inner 上运行 val。** challenge anchors 在 inner 中结算；hidden val 只在 outer 边界对
预先锁定的 task 父子 candidate 运行一次。这样避免 planner 通过反复观察同一 20 题的聚合分数
进行黑盒爬坡。

同一组 20 道 val 会跨 outer 重用，以保证不同 outer 可比；所有角色均看不到 val 内容，且
self-improve 在该版本的 val 结果产生之前已经结束其被计分的 inner 工作。outer-select 对同一
隐藏标量的长期适应仍可能造成 meta-overfitting，这是小数据集下无法彻底消除的残余风险。
在没有额外 test 集前，控制方式是：不提供逐题 val 反馈、不在 inner 中使用 val、限制评估次数，
并把最终结论明确限定为“在该固定 val 上”。

保留两个不同的 outer 指标：

- `planner_root_val_mean`：从共同 root 评估实际完成本 outer inner 的 planner，继续作为 planner
  的主要 outer fitness。
- `task_val_mean` / `task_parent_val_mean`：预先选定的 task candidate 与其直接父代的成对 val，
  只用于判断本 outer 的修复是否泛化。

### 2.6 现有 anchor 数量与 challenge 上限兼容性

首轮固定参数为：

```text
num_samples       = 30
base_anchor_k     = 10
challenge_max_k   = 5
random_min        = 10
实际随机题下限    = 15
val_anchor_k      = 20（独立 val subset，不占 train 的 30 题）
```

如果以后增加 `sampling.anchor_k`、`challenge_max_k` 或减少 `num_samples`，启动检查必须失败而
不是静默截断 challenge。若 evaluator 提交少于 K 个 challenge，空余位置全部交给随机题。

### 2.7 验收不通过时如何处理

验收失败不回滚、不删除 task 节点，也不把普通修复失败当成基础设施失败：失败节点是 DAG 中
有价值的负证据，现有 benchmark/LCB 仍可自然降低它被选中的概率。

具体处理为：

| 情况 | 处理 |
|---|---|
| challenge 证据不足 | 标记 `insufficient_evidence`；不产生 repair 奖惩 |
| challenge gate 失败 | 标记 `local_repair_failed`；无正 repair credit |
| challenge 通过但 val 未提高 | 标记 `local_only`；允许保留节点，但无泛化 repair bonus |
| val 明显下降 | 记录负的 planner correction score；仍由现有 task 选父规则决定是否淘汰 |
| 检出题号硬编码、答案泄漏或 val 泄漏 | 走现有 rule-violation / invalid-parent 硬门，节点不得成为父代 |
| grader/adjudicator 失败或 imputed | 状态 `undecidable`；不计分，不能伪装成成功或失败 |

challenge 失败后如果缺陷仍存在，子节点 evaluator 可以重新提交一个新 challenge；旧 challenge
不会被第二个 child 重复消费或重复计分。

### 2.8 谁判断 challenge 真伪与修复状态

职责严格分开：

| 组件 | 职责 | 是否是真值 |
|---|---|---|
| evaluator | blind 提出 challenge；对父 challenge 调 `judge_fix` | 否，是待校准的预测 |
| planner | `respond_issue` 并修改 task | 否，是行为与立场记录 |
| proof grader | 分别给父子证明打 0..7 分 | 是结果证据，但不能单独解释根因 |
| challenge adjudicator | 使用题面、参考解、grading guidelines、父子证明和 challenge 作结构化判定 | 是 proof 缺陷真值来源 |
| framework | 校验权限、组合证据、计算状态和指标 | 是状态机唯一写入者 |

challenge adjudicator 是 framework-owned 的固定调用模式，不能自进化，也不属于第四个角色。它
输出父子两侧的三态：`present / absent / uncertain`，以及 `actionable`。只有可定位、与 task 行为
有关且可通过一般策略修复的 challenge 才是 `actionable=true`。

对可判定项形成四个主状态：

| 父代缺陷 | 子代缺陷 | 状态 | 含义 |
|---|---|---|---|
| present | absent | `VERIFIED_REPAIR` | challenge 为真，planner 修复成功 |
| present | present | `VERIFIED_UNRESOLVED` | challenge 为真，修复失败；evaluator 对抗获胜 |
| absent | absent | `UNSUPPORTED_CHALLENGE` | evaluator 的 challenge 原本就是假的 |
| absent | present | `REGRESSION_AFTER_FALSE_CHALLENGE` | 原 challenge 为假，修改后反而引入该缺陷 |

任一侧为 `uncertain`、`actionable=false`、grader 缺失、coverage 不足或分数 imputed 时统一为
`UNDECIDABLE`，不进入分母。planner 是否声称 `acted` 不改变真值，只作为行为审计字段。

### 2.9 新状态的公式

定义一个版本统计窗口内的状态计数：

```text
a = count(VERIFIED_REPAIR)
b = count(VERIFIED_UNRESOLVED)
c = count(UNSUPPORTED_CHALLENGE)
d = count(REGRESSION_AFTER_FALSE_CHALLENGE)
u = count(UNDECIDABLE)
```

`u` 只报告，不进入以下分母。

Evaluator：

```text
challenge_precision_raw      = (a + b) / (a + b + c + d)
challenge_precision_smoothed = (a + b + 1) / (a + b + c + d + 2)

evaluator_adversarial_score  = (b - a - c - d) / (a + b + c + d)
```

Planner：

```text
repair_rate_raw      = a / (a + b)
repair_rate_smoothed = (a + 1) / (a + b + 2)

planner_adversarial_score = (a - b - d) / (a + b + d)

mean_anchor_point_gain = mean((child_points - parent_points) / 7)
                         over challenges with parent defect = present
```

`evaluator_adversarial_score` 明确形成对抗：真实但被修复的 challenge 记 `-1`，真实且未被修复记
`+1`，虚假 challenge 记 `-1`。planner 在真实修复上记 `+1`，未修复及因错误响应引入缺陷记
`-1`。`challenge_precision` 单独存在，避免 evaluator 通过大量虚假难题扩大“难度”。

### 2.10 版本错配的解决

supernode 必须保存并评价**实际完成该 outer inner 的 active planner/evaluator 版本**，而不是 outer
结束后刚刚 self-improve、尚未运行的新版本。

执行顺序调整为：

```text
outer 入口：冻结 active planner/evaluator version_id
    -> active 版本完成 inner
    -> 结算 challenge 与 hidden val
    -> 记录/评价/归档 active 版本
    -> planner/evaluator self-improve，产出 next candidate
    -> next candidate 只有在后续 outer 实际运行后才获得 fitness
```

版本 ID 至少包含 design hash 与 role code commit。首版 outer-select 只恢复 design；因此当归档版本
的 role code commit 与当前代码不一致时必须拒绝恢复，不能把旧 design 与新代码拼成一个从未运行
过的“版本”。完整跨 commit 恢复需要 split code layout 或单独的 role patch 恢复机制，属于本文
列出的后续实现缺口。

### 2.11 非连续 DAG 中 challenge 如何绑定

challenge 与产生它的 task 节点绑定，不与“下一次循环序号”绑定：

```text
challenge.producer_node       = p
challenge.producer_evaluator  = evaluator_version_id
```

当且仅当 `p` 后来成为某个 child `c` 的直接父代时，framework 原子地预留其尚未消费的 challenge：

```text
challenge.consumer_edge       = p -> c
challenge.consumer_planner    = planner_version_id
```

一次预留只允许一个 child 消费。即使 `p` 几轮后才被重新选中，challenge 仍有效；即使 `p` 还能
产生第二个分支，也不会重复领取旧 challenge。若第一个 child 未修复，child evaluator 可生成
新的 challenge，从而形成新的、可归因的债务。

这也解决延迟 credit：challenge 真伪归因给 `producer_evaluator`，修复结果归因给
`consumer_planner`。challenge ledger 保存版本级累计值；不得把后来结算的结果写给当前碰巧活跃
的角色。

### 2.12 是否把纠错能力放进选择指标

需要，否则 challenge 只是一种提示词格式，没有进化压力。但由于首轮 K 小且指标存在延迟结算，
采用两阶段启用：

1. **Pilot 阶段**：状态、公式、val gate 全部真实计算，但 adversarial score 仅诊断，不改变现有
   `F_p / F_e` 和选父。用于估计 adjudicator 的 `UNDECIDABLE` 率及 no-op val 噪声。
2. **激活阶段**：累计至少 30 个 supported challenge 且 adjudicator 可判定率达到 80% 后，才把
   对抗项以小权重加入 fitness：

```text
F_p_adv = F_p_base + lambda_p * z_ref(planner_adversarial_score)
           仅当 generalized_repair 通过；未通过时正项钳制为 0，负项保留

F_e_adv = F_e_base
          + lambda_e * z_ref(evaluator_adversarial_score)
          + lambda_precision * z_ref(challenge_precision_smoothed)
```

初始建议 `lambda_p=0.10`、`lambda_e=0.05`、`lambda_precision=0.05`。缺少足够样本时各项为中性
0，而不是当成完美表现。

planner 指标在 child 产生时即可归因；evaluator 的 challenge 可能以后才被消费，因此 evaluator
对抗项从 challenge ledger 在 outer-select 查询时动态计算，不回写或伪造旧 supernode 的基础
`fitness_e`。基础 calibration/verdict fitness 仍按原规则保持不可变。

### 2.13 权限与可见信息

| 信息 | task | planner | evaluator blind | evaluator reveal/self | proof grader | challenge adjudicator | framework |
|---|---:|---:|---:|---:|---:|---:|---:|
| 当前 train 题面 | 单题 | 可见授权父轨迹 | 可见授权轨迹 | 可见 | 可见 | 可见 | 可见 |
| train qid | 当前单题可带 | 可见 | 可见 | 可见 | 可见 | 可见 | 可见 |
| task 生成证明 | 自身当前输出 | 可见父轨迹 | 可见父/当前轨迹 | 可见 | 可见 | 可见父子 | 可见 |
| train 0..7 分数 | 否 | 否 | 否 | challenge 锁定后可见公开结果 | 产生 | 可见 | 可见 |
| grader 公开理由 | 否 | 仅在 challenge 结算后通过 evaluator issue 摘要 | 否 | 锁定后可见 | 产生 | 可见 | 可见 |
| 参考解 / grading guidelines | 否 | 否 | 否 | 否 | 可见 | 可见 | 可见 |
| challenge adjudication 真值 | 否 | 结算后只见状态和公开证据 | 否 | 结算后可见 | 否 | 产生 | 可见 |
| val 题号/题面/逐题分 | 否 | 否 | 否 | 否 | val 调用时单题可见 | 否 | 可见 |
| val 聚合分 | 否 | 否 | 否 | 否 | 否 | 否 | 可见并用于选择 |

planner 看见 train qid 不等于看见答案，但仍可能进行单题硬编码。framework 对 challenge response 的
task config/patch 做机械审计：禁止出现 challenge qid 的显式分支、参考答案或 grading guidelines
长片段、以及只对指定题触发的组件；命中后进入 `rule_violation`。一般化的数学检查策略或由案例
启发的通用 proof 流程允许保留。hidden val 是最终防线，但不能替代机械防泄漏检查。

---

## 3. 当前实现缺口

以下均是相对当前 `local_test` 的缺口；本文只定义方案，不声称已经实现。

1. **Challenge 契约缺失**：`EvalContext`、role process 返回契约、evaluator 工具和 feedback schema
   目前只有普通 issue / fix verdict，没有 blind-lock 的 challenge。
2. **外部根因裁判缺失**：proof grader 只产生 0..7 分和普通 grading trajectory，没有固定的
   parent/child challenge adjudication 调用与三态结构化输出。
3. **正常 grader 证据未完整封存**：task runner 目前主要保留 report、predictions 和 failure logs；
   正常 proof-grading 的公开判定需在临时 workdir 删除前归档到 framework-owned evidence。
4. **动态 challenge anchors 缺失**：现有 sampler 只有固定 `anchor_ids` 和随机补题；尚无独立的
   `challenge_ids`、一次性预留、去重和 `random_min` 启动检查。
5. **Challenge ledger 缺失**：当前 issue ledger 不能表达 qid、生产/消费版本、延迟结算和四状态，
   也不能保证分支上的一次性消费与 resume 幂等。
6. **Proof challenge 真值检查缺失**：现有 `issue_checks.py` 只覆盖可由节点聚合事实机械判定的
   closed issue keys，不能判断数学证明中的某个具体根因。
7. **Task 成对 val 缺失**：现有 `benchmark.val_mean` 是 fixed-root planner evaluation；还没有在
   val 读取前锁定 task candidate，并对 candidate / direct parent 产生 `task_val_delta`。
8. **Broker 预算不足**：新增 task parent/candidate val 和 challenge adjudication 后，task launch / LLM
   budget 必须显式增加，否则会再次触发 `task launch budget exhausted` 或 role-seat 重启耗尽。
9. **Fitness 尚无对抗项**：现有 `F_p` 是 held-out benchmark，`F_e` 是 calibration + verdict
   stability；没有版本级 challenge precision、repair 或 adversarial score。
10. **版本记录顺序错配**：当前 self-improve 先于 supernode 记录和 fixed-root eval；新设计可能继承
    旧设计完成 inner 所产生的指标。必须按 §2.10 调整归因顺序。
11. **跨 role code commit 的恢复缺失**：outer-select 当前只恢复 design，不恢复 planner/evaluator
    源码。首版必须拒绝 commit 不兼容的归档版本，完整恢复另行实现。
12. **防单题硬编码规则缺失**：现有 reward-hacking/rule-violation 检查没有 challenge qid 和题面
    片段的专用审计。
13. **分析输出缺失**：需要直接打印每 outer 的 challenge 状态计数、planner/evaluator 对抗指标、
    `planner_root_val_mean`、`task_val_mean`、`task_parent_val_mean` 和 `task_val_delta`。
14. **Pilot 基线缺失**：尚无 no-op parent/child val 重复数据，不能估计当前 proof 生成与 grader 的
    空效应噪声；首轮阈值只能使用 §2.3 的临时值。
    
## 4.跨领域兼容边界

可复用：账本、盲锁定、一次性消费、直接父子绑定、恢复幂等、WriterToken 权限。
imo_proof 专用：`qid`、proof 缺陷字段、0–7 grader、proof adjudicator。
paper-review search 后续应绑定 review item/证据片段。
polyglot 后续应绑定测试用例/失败断言。
不会把 IMO 的评分公式或字段强行放进其他领域。