# IMO proof 对抗性 Challenge 测试设计

> 状态：已实现；本文保留设计约定及增量修复记录。2026-11-22 实验复核与本轮变更见 §8。
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

## 3. 当前实现状态

`local_test` 已实现本文首版范围。下表是最终核对结果；“完成”指相应契约已有代码路径和针对性
测试，不表示真实模型实验已经达到足够统计功效。

| 项 | 状态 | 实现摘要 |
|---|---|---|
| 1 Challenge 契约 | 完成 | blind-only `submit_challenge`、context、IPC 和 feedback 均为加法兼容 |
| 2 外部根因裁判 | 完成 | framework 通过 task broker 调固定父子联合裁判；模型看私有参考证据，角色不可修改其 prompt |
| 3 grader 证据封存 | 完成 | report、predictions、失败日志及 `challenge_adjudication.json` 在 workdir 清理前归档 |
| 4 动态 anchors | 完成 | base/challenge/random 分离，强制 challenge 入样并执行 `random_min` 启动检查 |
| 5 Challenge ledger | 完成 | WriterToken、blind lock、直接边预留、一次消费、幂等 settlement |
| 6 Proof 真值检查 | 完成 | 固定裁判输出父/子 present 三态及 actionable；缺失或冲突统一 `UNDECIDABLE` |
| 7 Task 成对 val | 完成 | outer 边界锁定 candidate/direct parent，同一 hidden val 计算 `task_val_delta` |
| 8 Broker 预算 | 完成 | 预算覆盖 inner、fixed-root eval、paired val、no-op 及每 inner 至多一批裁判调用 |
| 9 对抗 fitness | 完成 | 严格按 §2.12 两阶段 gate、权重、planner 正项钳制和 evaluator 延迟动态归因 |
| 10 版本顺序 | 完成 | outer 入口冻结版本，完成 inner/val/归档后才 self-improve |
| 11 代码版本恢复 | 首版完成 | version ID 同时含 design hash 与 role commit；不兼容 commit 拒绝恢复。完整跨 commit 源码恢复不属于首版 |
| 12 防硬编码 | 完成 | 只审计新增 patch/config，拒绝 qid、参考答案及 grading guideline 泄漏 |
| 13 分析输出 | 完成 | `analysis/imo_challenge_report.py` 直接打印 challenge、两类 val、delta 与 fitness gate |
| 14 Pilot 基线 | 完成 | 可配置 no-op repeats，记录 delta 分布并以 `Q75(abs(delta))` 抬高 val gate |
    
## 4.跨领域兼容边界

可复用：账本、盲锁定、一次性消费、直接父子绑定、恢复幂等、WriterToken 权限。
imo_proof 专用：`qid`、proof 缺陷字段、0–7 grader、proof adjudicator。
paper-review search 后续应绑定 review item/证据片段。
polyglot 后续应绑定测试用例/失败断言。
不会把 IMO 的评分公式或字段强行放进其他领域。

## 5.首轮实现问题
1.Challenge 在 planner prompt 中被截断，导致planner 并没有稳定地“完整看到所有 challenge”
2.UNDECIDABLE 被 generation 级 coverage 错误放大，导致undeciable率显著提高，直接导致challenge gate无法满足，challenge fitness一直没有启动，导致本轮challenge始终没有对planner产生实质性选择压力
其他结构性问题
- 普通 issue 必须逐条调用 respond_issue，但 challenge 没有对应的 respond_challenge 契约。framework 无法区分 planner 是接受、争议、推迟，还是根本忽略某条 challenge。
- task agent 只收到题目，不收到 challenge 内容。planner 必须把最多 5 个异构、题目特定的缺陷压缩成一个全局通用设计修改。
- hardcoding audit 又禁止把 qid 或具体答案写进 task 修改。这是正确的防作弊措施，但意味着诸如“用 CRT 完成这道题”或“处理这个特定几何构型”实际上很难通过通用 prompt 修复。
- fixed adjudicator 的 actionable 校准可疑：当前 72 个有 verdict 的 challenge 全部是 actionable=true，没有一个 false。原 prompt 没有区分“具体但可迁移的类别策略”和“题目答案/构造”，因此这个判定可能过宽。
- challenge repair 没有 no-op 对照。同一个 task design 重新生成同一道题，也可能随机从有缺陷变成无缺陷，或反过来。因此不能把每个 VERIFIED_REPAIR/UNRESOLVED 完全归因于 planner 修改。

## 6. Challenge response 行为审计的增量修复（2026-11）

### 6.1 发现的问题

普通 issue 有逐条 `respond_issue` 契约，但 parent challenge 没有逐条响应接口。
因此 framework 只能看到最终 task diff 和外部 adjudication，不能区分 planner 是明确行动、
争议、推迟、超出范围，还是遗漏了某条 challenge；也不能判断 planner 的自报行动是否与实际
task 变化一致。把“调用过工具”直接当作行动同样不可靠，因为 patch 可能被拒绝或 hardcoding
audit 清空。

### 6.2 方案

新增 framework-owned `respond_challenge` 工具。planner 对每个直接父 challenge 最多记录一次：

```text
acted | acted_differently | disputed | deferred | out_of_scope
```

`acted` / `acted_differently` 只表示 planner 声称尝试修改，不表示 challenge 已被修复；
后者仍只由 fixed adjudicator 的 `VERIFIED_REPAIR` 等状态决定。

若主 planner session 结束时有缺失响应，framework 追加一次 response-only pass，要求只补齐
缺失的 `challenge_id`。仍缺失的响应保留为缺失记录，generation 不因此失败；非法或缺失响应
不会改变 proof adjudication，也不会改变 task val。

### 6.3 实现与记录

planner 返回并写入 child metadata 的三个审计指标为：

```text
claimed_acted_rate          planner 声称 acted/acted_differently 的比例
observed_action_rate        产生有效 task config 变化或被接受 source patch 的比例
response_consistency_rate   自报行动与 observed_action 一致的比例
```

framework 还记录每个 challenge 的 `claimed_response_kind`、`response_received`、
`observed_action`、`response_consistent`，以及 response retry 和 `challenge_response_deficit`。
其中 observed action 以 child 的最终 `patch_applied` 或父子 task config 差异为准，不能由 planner
自报覆盖。

### 6.4 选择压力边界

上述三个指标只进入 child metadata、outer supernode 的 `planner_stats.challenge_response` 和
事件日志，用于审计、诊断和协议补录；不进入 planner/evaluator fitness，不改变 challenge 的
真值状态，也不单独淘汰 parent。原因是 claimed 行为可伪报，observed action 只能证明发生修改
而不能证明修复，consistency 是协议可靠性而不是 proof 能力。

选择压力仍严格使用 §2.9 的 fixed-adjudicator 状态计数：planner 使用
`planner_adversarial_score`，evaluator 使用 `evaluator_adversarial_score` 与
`challenge_precision_smoothed`，且只有 §2.12 的 supported/determinate gate 达标后才激活。

### 6.5 约束与边界

response contract 不完整时只允许一次补录机会；低 observed action 比例不触发第二次 substantive
task 修改。`challenge_response_deficit` 仅是诊断标记，不能把“是否修改”误当作“是否修好”。

## 7. Actionable 判定收紧（2026-11）

### 7.1 发现的问题

旧的 fixed adjudicator 只要求输出 `actionable: true|false`，prompt 中的标准是“specific、relevant、
can be addressed by a general proof-generation strategy”。它没有结构化区分：

1. 具体但可迁移的类别策略；
2. 当前题目专属的答案、构造或 lemma；
3. 证据不足、无法判断的情况。

此前观测到 72 个有 verdict 的 challenge 全部为 `actionable=true`，因此旧字段没有提供有效 gate。

### 7.2 最终判别标准

不新增角色，也不新增一次模型调用。继续使用同一个 framework-owned fixed adjudicator，并要求它只在
以下条件全部满足时输出 `actionable=true`：

1. 缺陷是可从 parent/child proof 观察到的 proof-generation 或 proof-verification 行为缺陷；
2. `repair_criterion` 描述的是某个结构类别上的可迁移策略，而不是“更认真”“检查一下”之类空泛要求；
3. 把该策略复制到 qid、数字和答案不同的同类问题上仍然有意义；
4. 该策略可以通过现有 planner -> task 的 prompt、config 或可复用 component 行为表达；
5. 不要求当前题目的具体答案、唯一构造、reference-solution 文字或 qid 分支。

具体数学方法本身不构成答案泄露。例如，下面的 CRT 策略是 `actionable=true` 的允许示例：

```text
当问题涉及多个同余约束时：
1. 检查模数是否两两互素；
2. 互素时考虑中国剩余定理；
3. 不互素时检查同余方程组是否相容；
4. 求通解并验证原题限制；
5. 不要直接假设模数互素。
```

它虽然具体，但针对的是“多个同余约束”这一结构类别，包含适用条件、失败分支和验证步骤；
具体数字只有在它们被用来编码当前题目的私有答案或唯一构造时才构成泄露。

### 7.3 结构化输出与 framework 校验

fixed adjudicator 现在额外输出：

```text
actionability_basis =
  transferable_strategy
  | instance_specific_content
  | uncertain
```

framework 强制一致性：

```text
actionable=true  <=>  actionability_basis=transferable_strategy
```

parent/child 两侧的 `actionable` 和 `actionability_basis` 必须同时一致，否则为
`UNDECIDABLE`。`instance_specific_content` 和 `uncertain` 不计入 supported challenge 分母，
也不被误计为 evaluator 的虚假 challenge。

### 7.4 与其他审计边界的关系

`actionable` 判断的是 challenge 是否适合由可迁移的 task 策略修复；它不判断 planner 本轮是否真的
修改。实际 planner patch 仍只用于 hardcoding audit、`observed_action` 和 response consistency。
这样不会因为 planner 没有行动而反过来改变 challenge 的 actionability，也不会把具体但合法的数学
策略误判为答案泄露。

### 7.5 Planner 修改的实例依赖 gate（2026-11）

#### 发现的问题

`actionability_basis` 只判断 challenge 是否适合用可迁移策略修复，不能判断 planner 本轮实际提交的
task 修改是否把当前题目的答案或唯一构造写进了 prompt/config/source patch。旧的 hardcoding audit
只会拒绝显式 qid 和 protected answer/grader 字段，并清空 patch；child 仍可能被当作有效 parent。

#### 最终方案

沿用现有 fixed adjudicator，不新增角色或第二次模型调用。每个 adjudication question 额外携带一个
framework 生成的、有长度上限的 `Task Change` 摘要（新增 patch 行和相对持久化 parent config 的 changed
keys），并要求 adjudicator 输出独立的：

```text
modification_scope =
  transferable_strategy
  | instance_specific_memory
  | answer_leakage
  | uncertain
```

判定规则如下：

1. 方法名称、具体数字、结构识别条件、条件分支和可复用步骤可以是
   `transferable_strategy`；task 仍必须识别当前结构、选择参数、完成主要推导并验证结果。
2. 明确依赖当前 qid、当前题目的唯一对象/构造或仅对当前实例有用的信息是
   `instance_specific_memory`。
3. 当前答案、答案等价的关键中间结果、reference-solution 文本或 qid-specific branch 是
   `answer_leakage`。
4. 证据不足时为 `uncertain`。

#### parent 安全边界

`instance_specific_memory` 和 `answer_leakage` 是 framework hard gate：child 记录为
`challenge_modification_hard_gate`、设置 `valid_parent=False`，不进入 parent selection 或 challenge
repair credit。显式 qid/protected token 由 task 执行前的机械 audit 立即拦截；语义实例依赖由 fixed
adjudicator 在 settlement 后拦截。`uncertain` 不授予 repair credit，但不抛出全局异常。

gate 使用现有 invalid-child 归档路径，单个违规 child 不会中断其 sibling 或整代；若整代没有可晋升
child，保留上一有效 parent 并结束该 outer。challenge 本身的 `actionability_basis` 与 planner 修改的
`modification_scope` 始终分开记录，避免把“challenge 不可迁移”误当成 planner 作弊。

#### 本次修改

- 扩展 fixed adjudicator、CSV parser 和 IMO adapter 的结构化输出与校验；旧 artifact 缺少
  `modification_scope` 时保持保守的 `UNDECIDABLE`。
- 在 task runner 记录 bounded `task_change` 摘要，并在 challenge adjudication 输入中传递。
- 对机械 hardcoding 和 adjudicator 语义 gate 分别接入 child invalid-parent 路径。
- 增加 CRT/通用策略不触发机械 gate、实例记忆不计 repair、transferable strategy 正常 repair 的回归测试。

## 8. 10×4 实验复核：计分口径说明与重叠授权补丁修复（2026-11-22）

实验：`imo-proof-challenge-4.1mini-10x4-30q-20261120-234051`。
修复重叠源码授权造成的重复文件 diff；计分逻辑保留，其他裁判和 challenge 生命周期问题不在本轮修改范围。

### 8.1 部分评分与数学能力指标：保留原逻辑

**最终决定：不将 429、超时或 grader 失败造成的未评分题按 0 分计入选择分，
不改成以原定题数为分母的交付得分。** 指标继续描述成功评分题上的数学表现：

```text
points_percentage = sum(成功评分题的 0..7 分数) / (7 * 成功评分题数)
coverage = 成功评分题数 / 原定题数
```

未评分题保持未知；成功评分题上的真实 0 分仍计入上述分母。已有 partial 结果继续按原逻辑使用，
不新增 coverage 淘汰门槛，不要求每个 outer 成功评分题数相等。固定根 val、task 父子 val 差、
no-op 波动估计及 fitness 消费逻辑均不修改。

这是面对不可避免的题数波动所保留的实验取舍，不代表不同成功题子集的均分完全等价。
若失败题并非随机缺失，不同子集的难度构成会影响跨 outer 或父子比较；分析时需同时给出
`total`、`requested_total`、`coverage` 和 `question_ids_ungraded`，不能仅凭均分上升认定能力提高。
也不能将未评分题数增加直接解释为数学能力退化。

本轮实例：outer 5 的 task candidate 评出 17/20 题、parent 评出 20/20 题，原始均分差为
`+0.0571428571`；outer 9 分别为 14/20 与 19/20，均分差为 `+0.0445757250`。
这些结果保留有效，但正差值不单独构成完整固定题集上的数学能力提升证据。
换用 gpt-4.1-mini 后运行中仍有 429，因此上述波动同时包含接口失败因素。


### 8.2 重叠授权导致重复 diff：已修复

gen 11 同时持有目录授权和目录内的精确文件授权。`runs/11/patch_proposed.diff` 对
`gan/components/task/skills/{bash,editor}.py` 和
`gan/components/task/tools/{knowledge,write_file}.py` 各生成两份完全相同的文件 diff；
`runs/11/patch_receipt.json` 记录补丁因 `patch does not apply` 被拒绝。

根因位于 `gan/patch.py:build_patch_from_workspace`：旧 `seen` 只对授权路径字符串去重，
不能识别目录展开后的文件与另一条精确授权指向同一文件。重复修改/删除 hunk 被依次应用时，
后一个 hunk 面对的已不是原始基线，导致拒绝；这不是数学能力或模型随机性问题。

最小修复：保留授权层的 `seen`，新增函数内的 `processed_files`，按展开后的仓库相对文件路径
去重，新增、修改、整路径删除及目录内删除四条分支共用。同一文件只进行一次差异/合并处理，
不通过删除授权或对 diff 文本去重来规避问题。目录扫描的 `b_files` 必须先记录文件存在，
再跳过已处理文件，避免另一条授权已处理的文件被误判为删除。


## 9. 修复信用校正与独立裁判模型试验（2026-11-23）

范围仅限固定 adjudicator 的数学核查、信用契约、模型授权及原实验重放；不改 planner、proof grader
评分公式、fitness 公式或 challenge 消费生命周期。

> 当前实现已升级为 §9.8 的 protocol 3。§9.1–9.7 保留版本演变记录；其中 v2 引文格式及 `--variants v2` 命令仅描述当时实现，当前复测请用 §9.8 的 `--variants v3`。

### 9.1 原问题与 v2 核查协议

实验 `imo-proof-challenge-4.1mini-10x4-30q-20261120-234051` 中，`ch-2-5-0` 的旧裁判理由承认
父子证明均未满足修复标准，却输出 `child_present=false`；`ch-6-21-0` 则将补回遗漏解视为修好，
忽略正文继续使用错误 LTE 并排除该解。单独检查六个返回字段的类型不能消除这种误信用。

新版 `baselines/imo_grading/challenge_adjudicator.py` 按顺序执行：完整拆解 repair criterion 的
数学义务、定位提交证明的实际支持、独立核查数学、尝试反证、检查整份输出中与该缺陷相关的
未解决矛盾。换术语、引用未证明的另一条定理、添加正确公式或最终答案都不自动构成修复。
不得从参考解补齐提交证明未给出的关键步骤；接受满足原义务的替代证明，不要求照抄参考解。
已明确撤回且被完整独立正确证明替代的错误尝试不作为仍存缺陷；无关错误也不否定局部修复。

模型输出 `obligations[]`，每项包含 criterion 原文引用、具体义务及 parent/child 的
`status=satisfied|violated|uncertain`、原证明引用、简短数学核查依据；另有 parent/child 的
whole-output consistency 检查。actionability 与修改是否泄露答案仍分开判断，泄露证据只能
引用 bounded Task Change，不能取自题目、证明或参考解。

`domains/imo/challenge_contract.py` 为 frozen stdlib-only 共享契约。它机械核对字段、枚举、
引用来源和结论一致性，从检查结果派生 presence，不再接受模型独立填写一个可能与检查矛盾的
presence 布尔值。某项明确 violated => 缺陷仍在；无 violated 但有 uncertain => 不确定；
全部 satisfied 且无相关未解决矛盾 => 缺陷消失。成功义务必须有支持引用；缺失论证的 violated
可无引用，但需说明缺失。引用只做空白规范化后的原文匹配，不声称机械证明数学正确、引文足以
支持结论或模型没有遗漏义务，这些仍需真实样本验收。

### 9.2 独立复核与格式修复

只有首次检查拟授予 repair credit（parent 缺陷存在、child 消失、actionable、修改可迁移）才追加
一次独立数学复核。新会话只收到原始全部输入与同一核查协议，不收到首次判定和首次选择的证据。
两次都支持 repair 才 confirmed；分歧为 `UNDECIDABLE`，复核调用/格式失败为 unavailable，
均不给修复信用，不把分歧自动算成 VERIFIED_UNRESOLVED。

格式/证据契约失败仍最多补一次格式修复；它沿用该次会话，并明确不算独立数学复核。
最终 envelope 包含 `protocol_version=2`、两份核查记录、派生 presence、review_status、实际
model 和包含主 prompt/复核/格式指令的 SHA256。report parser 使用原输入重新检查引文与派生
结论，模型标识也须匹配 broker 固定模型。旧六字段输出不再被新 report parser 接收；旧归档
artifact 的适配读取保留，不改写历史账本或历史成绩。

### 9.3 原实验重放与验收

`scripts/replay_imo_challenge.py` 读取原实验 `work/challenge*/repo/input/questions.csv`。
已离线检查找到 26 批、47 个唯一 challenge；原题、参考解、评分细则、父子证明和当时 Task Change
原样重放，不重新生成 task。原实验目录只读，输出必须另置。

pilot 使用 `tests/fixtures/imo_challenge_acceptance.json` 中预先声明的 8 个真实案例，覆盖
错误更名、遗漏论证、结论与正文矛盾及一个真实局部修复正例 `ch-5-16-1`，防止全部判失败假过关。
这些是定向人工复核标签，不是全部 47 个案例的金标准，标签和理由不得送给 LLM。
同一输入比较 4.1/原 prompt、Gemini/原 prompt、4.1/v2、Gemini/v2。
原 prompt 固定存于只含提示词的 fixture；v2 执行实际 `TaskAgent.forward`，替换的仅是交互读入
凭据的 OpenAI-compatible transport。

### 9.5 验证状态与手动命令

WSL 仓库根目录（Python 使用项目可信环境；如名为 venv_nat，则用其解释器）：

```bash
PYTHONDONTWRITEBYTECODE=1 venv_nat/bin/python -B scripts/check_imo_adjudicator.py \
  --output /path/outside-live-run/adjudicator-offline

PYTHONDONTWRITEBYTECODE=1 venv_nat/bin/python -B scripts/replay_imo_challenge.py \
  --run /path/to/imo-proof-challenge-4.1mini-10x4-30q-20261120-234051 \
  --output /path/outside-live-run/adjudicator-pilot --phase pilot
```

pilot 命令会依次比较两模型、两版本，启动时交互读入 URL 和 key。模型不可用写单独状态文件，
不偷偷回退。pilot 通过后选择模型做 full（此例仅表示候选，不预断 Gemini 胜出）：

```bash
PYTHONDONTWRITEBYTECODE=1 venv_nat/bin/python -B scripts/replay_imo_challenge.py \
  --run /path/to/imo-proof-challenge-4.1mini-10x4-30q-20261120-234051 \
  --output /path/outside-live-run/adjudicator-full --phase full \
  --models openai/gemini-3.5-flash --variants v2
```

正式生产前需把新版裁判安装进实际运行解释器。可用最小白名单打包，避免原 setup 全树复制
将本机配置带入包；该命令只暂存可安装包，不安装、不删旧包、不触碰 git：

```bash
venv_nat/bin/python -B scripts/check_imo_adjudicator.py --build-package \
  --output /path/outside-live-run/adjudicator-package
venv_nat/bin/python -m pip install --no-deps --no-build-isolation --force-reinstall \
  /path/outside-live-run/adjudicator-package/package
```

本批未执行 add、commit、merge，保留已有暂存和未暂存改动。模型最终选择和数学验收尚待重放结果，
不能据此宣布 planner 指导问题已解决。

### 9.6 首次 pilot 的错误分类与定向补录修正（2026-11-23）

adjudicator-review-20261123/pilot` 保存的原始响应中，
离线重检全部 27 次 v2 调用（含格式/证据补录）：没有真正的 JSON 语法错误。
4.1 的 16 次响应全部因引文不属于指定原文而被拒；Gemini 有 5 次合法 assessment、2 次引文不匹配、
1 次 scope_reason 为空、3 次 HTTP 503 无响应。该计数是调用级协议结果，不是数学验收通过数。

原因：

1. 模型把 claim 句子放进 criterion_quote，或把证明改写、拼接、增加省略号后作为原文引文。
   如 `m0_v2/ch-1-0-0` 的 criterion_quote 来自 claim/证明而不是 Repair Criterion，child 引文
   还缩写了正文。该拒绝符合原文溯源契约，不能通过模糊匹配或忽略引用校验来发放信用。
2. `_assessment` 捕获校验失败时仅记录通用 `format_or_evidence`，补录不告知失败字段；两次
   失败后返回空字符串。重放端随后执行 `json.loads("")`，把已知的证据契约拒绝误报为
   `JSONDecodeError`。这属于本次新增代码的诊断缺陷，并非 4.1 返回内容全是坏 JSON。

修正：原校验规则不放宽，错误记录仅含安全 code、field、phase、attempt，不含私有引用或异常正文；
补录收到实际失败字段，要求重检全部引文，用短小连续原文，不改符号、不翻译、不拼接，不跨
parent/child/claim/criterion 字段取证。仅做格式/证据补录，不把它当独立数学复核，也不为通过
校验改写数学结论。prompt hash 随主/补录指令更新。

重放端对裁判空返回记录 `assessment_invalid`（或无判定记录 `judge_no_verdict`），不再解码空串；
逐例 JSON 保留 `judge_diagnostics`，comparison.csv 与 REPORT.md 显示 validation_code/field。
空 scope_reason 单独标为 `empty_scope_reason`，避免误报为 modification_scope 枚举错误。
HTTP 503 与协议错误、数学上的 UNDECIDABLE 继续分开；不增加自动 provider 重试或改变评分逻辑。

新增 `--phase audit --source-output <旧输出>`，可无 API、只读重检保存的响应，另存
`diagnostics.csv`、`diagnostics.json`、`REPORT.md`；不改旧结果和原实验。首次 pilot 已通过此方式
核验，生成诊断产物。旧 v2 结果因协议失败仍不能计入模型数学表现比较。


### 9.7 GLM-5.3-flash 替代候选与调用预算（2026-11-23）

第二轮 `pilot-v2-r2` 中 Gemini 有 4 个 HTTP 503、1 个 HTTP 429，仅 3 个案例成功返回且均为
VERIFIED_UNRESOLVED；局部修复正例仍缺测，不足以判定 pilot PASS。4.1 有 4 例最终引用校验
失败、3 例最终 invalid_json、1 例复核不可用；不能将协议失败解释为数学能力结果。

前两次结果支持判定gemini-5.3-flash接口不稳定（大量503中只有少数200，还会出现429）


新候选的定向复测命令（沿用 RUN/OUT，另开目录）：

```bash
python -B scripts/replay_imo_challenge.py --run "$RUN" \
  --output "$OUT/pilot-glm-v2" --phase pilot --variants v2 \
  --models openai/glm-5.3-flash --glm-reasoning-effort high \
  --glm-max-tokens 32768 --timeout 600
```


### 9.8 4.1 协议失败根因与来源编号协议 v3（2026-11-23）

**根因与边界。** 对第二轮 `pilot-v2-r2/m0_v2` 保存的 18 次原始响应重检：6 次真实
JSON 语法错误、11 次合法 JSON 的引用溯源失败、1 次完整合法 assessment。8 个案例最终为
7 次首评契约失败、1 次独立复核不可用。全部调用 status=ok、finish_reason=stop，输出仅
733–1320 tokens，未触及 12000 上限；本轮不是限流或截断导致。`ch-1-0-0`、`ch-6-21-0`、
`ch-6-23-0` 把字符串边界写成反斜杠转义的双引号；`ch-2-5-0` 拼接不同原文段落，
`ch-6-21-1` 添加原文没有的单引号。主/补录 prompt 的 JSON 示例本身合法，transport 未额外
改写反斜杠。此前 §9.6 修正了解码空返回的误报和字段定位，没有消除这些模型输出错误。

移除“数学判断时还要逐字抄录并转义证明”的协议负担，保持修复义务、
actionability、泄露判断、独立复核、原 proof grader、fitness 和 partial outer 计分口径不变。
默认生产裁判仍为 4.1-mini；不在本批解决 planner 指导问题，也不据离线通过宣称数学验收成功。

**模型输出与存储接口。** `challenge_contract.PROTOCOL_VERSION` 与裁判部署字面标记均为 3。
框架按原始行顺序编号，保留内容、空行和行尾；四种来源分别为 R=Repair Criterion、P=Parent
Proof、C=Child Proof、T=Task Change。模型只返回 `criterion_refs`、检查对象的 `refs`、
`scope_refs`，元素如 `P0042` 或闭区间 `P0042-P0044`；不返回引文字符串。
跨来源、越界、倒序、格式错误、全空白证据均拒绝。成功义务仍必须有证据；缺失论证的 violated
可以无 refs，并在 verification 说明缺失。原义务拆解和全篇一致性检查继续存在。

`parse_assessment` 是 wire→canonical 的唯一转换入口：检查编号，按原文还原
`criterion_quotes`、检查对象 `quotes`、`scope_quotes`。模型不得自行提供这些引文字段；
canonical artifact 同时保存 refs 与程序生成的引文。报告解析器使用未编号的原始 questions.csv
重新解析来源，逐项检查引文与编号解析结果完全相等，再复验派生 presence、review_status 和
固定模型；不能只凭编号存在或格式合法认定数学修好。缺失/矛盾的证据继续不给信用。

**JSON 约束与重试。** 生产裁判及 v3 replay 共用能力探测函数和同一完整 assessment schema。
每个生产 TaskAgent 实例首次 forward 探测一次，之后沿用；replay 在每个模型开始 v3 前探测一次，
再把结果传给实际生产 forward。探测只用合成数据，不带原题、参考解或人工标签：先请求严格
json_schema，以要求额外字段的冲突请求检查实际行为；格式被拒或未执行时，再探测 json_object。
两者均未验证则不运行 v3 数学判断。HTTP 400/422/501 才允许尝试下一格式，401/403/429/503、
超时等调用错误不作为不支持格式处理。选择与探测状态明确记录，不退回无约束文本。
一次探测是能力证据，不保证网关以后每次永远遵守约束，所有实际响应仍须通过本地完整校验。

通用客户端仅新增默认 None 的可选 response_format 参数，既有位置参数顺序保留；仅固定裁判
传入。经 chat_with_agent → get_response_from_llm → LiteLLM extra_body 发送，避免
全局 drop_params 静默剔除；与 reasoning_effort 合并，不覆盖它。现有父代理原样透传请求体，
模型/token/UID/目录授权规则不变。回放 HTTP transport 接收同一参数并记录所选格式；GLM 的
high effort、32768 tokens 和非 GLM 的原预算均保留。

首评、一次格式补录、fresh-history 独立复核使用同一格式。JSON 错误提供安全的行/列/位置和
预定义错误类别；编号错误提供字段路径、正确来源前缀与行数，不附带原文、异常正文或 API 信息。
补录仍仅一次，仍不是数学复核。两次拟修复结论均通过才给信用；复核不可用或分歧仍无信用。

**版本与评分链兼容。** broker 的任务说明、响应契约及 driver/broker 共三处安装预检都使用共享版本常量，
拒绝旧部署标记；最终结算 provenance 为 `imo_proof_fixed_v3`。最终 envelope 新增实际
`response_format=json_schema|json_object`。新报告入口不接收 v1/v2 输出，也不把旧引用自动转为
新编号；旧 v2 原始回放仅由 `scripts/imo_challenge_v2_audit.py` 的冻结只读解析器审计，且不打包
到生产。既有 v1 归档 artifact 的历史适配读取保留，不改写历史 ledger/成绩。v3 replay arm
明确命名为 mN_v3，CLI 当前变体为 v1/v3，防止旧结果与新 prompt 混在同一个 arm。

最小安装包仍只纳入共享 stdlib contract 和必要运行时代码，不纳入原实验、数据集、配置或密钥。
引文/参考解只留私有裁判 artifact；public settlement 和角色投影不扩展私有证据。
```
```
### 9.9 WSL 代理集成测试接口补齐（2026-11-23）

运行 §9.8 检查后，`adjudicator-v3-20261123-194803/offline/test_llm_proxy.py.log`
的三个错误均发生在 setUp,已修


### 9.10 GLM 独立裁判部署选择、标签勘误与原实验全量复核（2026-11-23）

`models.yaml:domains.imo_challenge_adjudication` 改为
`{model: openai/glm-5.3-flash, reasoning_effort: high}`。task 与 proof grader 保持 gan.task，
不改数学 prompt、v3 契约、独立复核、32768 生产输出预算或评分/fitness。



**已核验8例的真实统计基线。** 原正式实验4.1账本不是后来的4.1/v1或4.1/v3重放：

| 来源 | 有明确判定/已核验 | 错判 | 数学误判率 | 错发修复信用/授予修复数 |
|---|---:|---:|---:|---:|
| 原正式实验4.1账本 | 8/8 | 5 | 62.50% | 5/7（71.43%） |
| 后续4.1/v3 pilot | 4/8 | 3 | 75.00% | 3/4（75.00%） |
| GLM/high/v3 pilot | 8/8 | 0 | 0.00% | 0/2（0.00%） |

数学误判率分母是已人工核验且裁判作出明确判断的案例。4.1/v3 另有3例 UNDECIDABLE 和1例契约拒绝

在原47题上重新判定后认定adjudicator目前基本可信
```

