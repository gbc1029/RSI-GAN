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
