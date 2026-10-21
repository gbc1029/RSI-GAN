# 里程碑 B 计划：outer 级父代选择（MOME 档案 + 锦标赛 + novelty）

> **状态**：定稿；实施中（B1–B6b 完成并回归全绿；真实 LLM 运行待 Linux 部署环境验证）
> **前置**：里程碑 A 已完成（拆仓库 + supernode 记录 + 设计戳 + inner 子树内选，见 `docs/13`）。
> **本计划只描述 outer 级选择**：inner 的 task 父代继续使用现有 UCB + 子树内选（P6），**不改**。

---

## 1. 目标与边界

**一句话**：把 outer 从"单链"改成"每届从历史 supernode 档案里选一套 P/E 设计作为父代"，用 MOME 格子 + 锦标赛 + novelty 驱动选择；每个 supernode 入档前在**固定 root 基线上评估**，保证分数永久可比。

**在范围内**

- supernode 内嵌设计快照，使其**可被恢复**。
- MOME 档案（格子 + 每格 Pareto 前沿 + 容量上限）。
- 适应度 $F_p$ / $F_e$ + novelty（novelty 自身运行 z-score）。
- 锦标赛（两层采样）+ 参考伙伴（赛季化 M=10，作为 z-score 与 novelty 的基准）。
- 违规 supernode 的硬门（排除）+ 档案接口层强隔离。
- outer 开头的"选父代 → 恢复设计 → heal → 幅度校验"闭环。
- 入档时的**固定 root 评估**（旁路，不污染主线）。

**明确不在范围内**

- 不改 inner 的选择算法（保持 UCB / best / latest + P6 子树内选）。
- 不回溯代码 commit：选中的父代只恢复**设计**（`config.json`），roles/task 代码继续当前 HEAD。
- 不引入 crossover。
- 不拆 roles 仓库（"只恢复设计 + heal"不需要）。

---

## 2. 已确认决策（含 4 个高危漏洞的修复）

| 项 | 决策 |
|---|---|
| 选择层级 | **仅 outer 级**（inner 不动） |
| 父代恢复粒度 | **只恢复设计 `config.json`**；代码用当前 roles HEAD |
| 评估口径（漏洞 1） | **入档时在固定 root 基线上评估** 2–3 次，分数一次测好、永久可比；选择阶段**只读档案、不再评估** |
| 恢复后对齐（漏洞 2） | 恢复设计 → `heal_design_keys/slots` 适配当前 roles HEAD → 标记 `restored_then_healed` → **幅度校验**：差异过大则放弃该父代、换下一个（最多 N 次，全失败退回链式） |
| $F_e$ 目标（漏洞 3） | **不挂 bench**，改为评估器自身的 **校准误差 + 判准稳定性**（方向统一为"越高越好"，校准误差取负） |
| 档案隔离（漏洞 4） | **接口层强隔离**：archive/supernode 读 API 仅 framework 内部；不注册任何 role 工具；`AccessBroker`/`frozen` deny `ckpt/**` |
| MOME 分桶 | **默认固定边界**（`mome.*_buckets` 可覆盖，冷启动也稳定） |
| 冷启动（漏洞 5） | 档案 < `cold_start_min` → **完全链式**（不选父、直接返回 None），不只关 novelty |
| 锦标赛采样（漏洞 6） | **两层采样**：先均匀抽格 → 再在格内抽精英 |
| 档案容量（漏洞 7） | 每格 `max_elites_per_cell` 上限，超出按 Pareto + fitness 淘汰 |
| novelty 归一化（漏洞 8） | novelty **自身也运行 z-score**（相对参考伙伴） |
| 评估副作用（漏洞 9） | 固定 root 评估**旁路运行**：独立 run_dir、不写 task tree、不写 `scores.log`、不进角色可见轨迹、设预算上限 |
| 参考伙伴 | 赛季化 M=10；作为 **novelty 距离参考集** + **z-score 基准** |
| 设计快照存储 | **supernode 内嵌** planner/evaluator 设计快照 |
| task 起点 | **继续当前 task HEAD**（task 连续演化，outer 只换 P/E 策略） |
| 违规处理 | 当前代码：`rule_violation=true` 且 `evidence_ref` **非空** → 排除（"可定位"校验与第二意见尚未实现，见 §11） |
| 冷却（漏洞 10） | 最近 `cooldown_window` 届被选过的父代移出候选池；空池则放宽 |
| verdict_stability 口径 | **客观事实命中率**：`fix_verdict`（evaluator 判"修好没"）与 benchmark 相对父代是否真的提升，一致率 |

沿用 `docs/13` §6 的既有决策：联合 MOME、运行 z-score、重复评估 2–3 次。

---

## 3. 数据模型

### 3.1 supernode 字段扩展（`ckpt/supernodes/outer_<N>.json`）

在 P7 已有字段基础上**追加**：

```json
{
  "designs": {
    "planner":   { ...该 outer 结束时生效的 planner 设计 config... },
    "evaluator": { ...该 outer 结束时生效的 evaluator 设计 config... }
  },
  "evaluator_stats": {
    "...": "已有字段",
    "rule_violation": {"triggered": false, "evidence_ref": null},
    "accepted_fixed_rate": 0.67,
    "calibration_error": 0.12,
    "verdict_stability": null
  },
  "benchmark": {
    "...": "已有字段",
    "eval_baseline": "eval_base",
    "reevaluated": true,
    "mae": null
  }
}
```

- `designs`：选择父代后写回 `ckpt/design/<role>/config.json` 的唯一来源（自包含、不可变；是**该届结束时已对当时代码 heal 过**的设计）。
- `rule_violation`：从该届各 `runs/<genid>/eval.json` 的 `eval_point_results` 聚合（`point == "rule_violation"`）。
- `accepted_fixed_rate`：evaluator 提出的 issues 中被 planner 接受并修复的比例（从 `feedback.issues` + `fix_verdicts` + `respond_issue` 聚合）。
- `calibration_error`：$\lvert\text{predicted}-\text{actual}\rvert$ 均值（越低越好，$F_e$ 主指标；数据来自 `eval.json` 的 `predicted_score` + `benchmark_score`）。
- `verdict_stability`：客观事实命中率 = `fix_verdict` 与 **benchmark 相对父代是否真的提升**的一致率；数据不足（无 fix_verdict / 无父代 benchmark）时 `null`，$F_e$ 自动退化为只用 `calibration_error`。
- `benchmark.eval_baseline`：本次评估使用的固定 root ref（所有条目相同 → 可比）。
- `benchmark.reevaluated`：**有 ≥1 轮有效时为 `true`**；`eval_base` 缺失、全部轮次失败或评估异常时为 `false`，此时**保留该届 inner 分数**（绝不写 `None` 把唯一分数抹掉），并记 `supernode_eval_degraded`。
- **无 benchmark 的 supernode 不入档**（`supernode_not_archived: no_benchmark`）：$F_p$ 缺失的条目永不被支配裁剪、却会占满每格容量，还可能在锦标赛中胜出。

### 3.2 MOME 档案（`ckpt/archive/mome.json`）

```json
{
  "version": 2,
  "cells": {
    "x2|y3|z1": {
      "elite": [
        {"outer": 3, "fitness_p": 0.71, "fitness_e": 0.44, "design_hash": "..."}
      ]
    }
  },
  "reference": [2, 3, 5, 6, 7, 8, 9, 10, 11, 12]
}
```

- `cells`：键为 `x|x_bucket|y_bucket|z_bucket`，值为该格的 **Pareto 前沿**（按 $(F_p, F_e)$ 二维支配关系维护；两维方向统一为"越高越好"）。
- `reference`：参考伙伴窗口（最近 M=10 个已入档 supernode 的 outer id，按入档顺序）；**fitness 在入档时用"入档前"的 reference 计算，不含本届**。
- 每格 `max_elites_per_cell` 上限，超出按 $(F_p, F_e)$ 支配 + fitness 淘汰。
- 档案是**派生索引**，可从 `ckpt/supernodes/*.json` 重建（提供 `rebuild` 以便修复）。

### 3.3 格子（默认边界见 `archive.DEFAULT_*`，由 `mome.*_buckets` 控制）

| 轴 | 定义 | 固定桶 |
|---|---|---|
| **X** = op 类型构成 | `planner_stats.op_dist` 的 **top-1 op** 映射到固定类别 | 6 类：`set_prompt` / `set_config` / `set_param` / `select_component` / `deselect_component` / `other`（含 `add_config_key` / `add_config_value` / `respond_issue` 及未识别） |
| **Y** = 平均 issue 数量（`issues_per_task`） | `该届 issue 总数 ÷ task 节点数`（纯计数均值：不做 severity 加权、不做跨届归一化） | 5 桶：`0` / `(0,0.5]` / `(0.5,1]` / `(1,2]` / `>2` |
| **Z** = eval_point 覆盖 | `len(evaluator_stats.eval_points_run)` | 3 桶：`0–1` / `2` / `3–4` |

> 冷启动（档案条目 < `cold_start_min`）：**完全链式**，不选父代、不分桶、不计算 fitness（见 §5.1）。
>
> **配置形式**（`mome.{x,y,z}_buckets`）：给 **int** = 使用上表内置边界，并**断言桶数**（不一致直接启动报错，避免“改了不生效”的哑键）；给 **list** = 显式边界（消融用）。X 是 op 名顺序表（末尾自动 `other`），Y/Z 是闭区间上界（桶数 = `len(edges)+1`）。

---

## 4. 适应度与 novelty

### 4.1 特征向量（用于 novelty 距离）

从 supernode 提取，**分块**处理：

- **分布块**：`op_dist` 归一化后的直方图（Ruzicka 距离）。
- **标量块**：`benchmark.mean`、`benchmark.std`、`evaluator_stats.issues_per_task`、`evaluator_stats.calibration_error`、`benchmark.n_valid`（各自 z-score 后取绝对差）。

### 4.2 novelty（自身 z-score 化）

$$\text{novelty}(s) = \frac{1}{k}\sum_{r \in \mathrm{kNN}_k(s,\, R)} d(s, r)$$

- $R$ = 参考伙伴窗口（M=10），不是全档案。
- $k$ = `mome.novelty_k`（默认 5）。
- 距离 $d$ = 分布块 Ruzicka 距离 + 标量块归一化绝对差（加权和）。
- **novelty 再对参考伙伴做一次运行 z-score**，使它与 $z(\text{bench})$ 同尺度，$\lambda$ 权重才有意义。
- 冷启动（档案 < `cold_start_min`）：直接**完全链式**（见 §5.1），novelty 不参与。

### 4.3 适应度（两维方向统一为"越高越好"）

$$F_p = z_{\text{ref}}(\overline{\text{bench}})\;-\;\beta\, z_{\text{ref}}(\sigma_{\text{bench}})\;+\;\lambda\cdot\text{novelty}$$

$$F_e = -\;w_1\cdot z_{\text{ref}}(\text{calibration\_error})\;+\;w_2\cdot\text{verdict\_stability}\;+\;\lambda\cdot\text{novelty}$$

- $z_{\text{ref}}$ = 相对参考伙伴窗口的运行 z-score（bench 与 calibration_error 共用同一基准）。
- $F_e$ **不挂 bench**（漏洞 3）：只由"校准误差（取负）"与"判准稳定性"构成；`verdict_stability` 缺数据时该项取 0 并标记 `fitness_partial`。
- 两维都是"越高越好"，Pareto 支配方向一致（漏洞 3 的方向问题）。
- 权重：$\beta$=`fitness.beta_variance`(0.1)，$\lambda$=`fitness.lambda_novelty`(0.2)，$w_1$=`fitness.w1_calibration`(0.6)，$w_2$=`fitness.w2_verdict_stability`(0.4)（`loop.yaml` 可调）。

---

## 5. 算法流程

### 5.1 每届 outer 的生命周期

```
outer N 开始
  ├─ 1. select_outer_parent(N)            # 锦标赛，两层采样（见 5.2）
  │     ├─ 冷启动（档案 < cold_start_min）→ 返回 None（完全链式）
  │     └─ 选中 supernode S
  │           → 把 S.designs 写回 config.json（ledger 守卫）
  │           → heal_design_keys/slots 适配当前 roles HEAD（见 5.6）
  │           → 幅度校验：差异过大 → 放弃 S，换下一个（最多 N 次，全失败 → 链式）
  ├─ 2. _refresh_roles(N)                 # 加载（heal 后）的设计
  ├─ 3. 跑 inner（UCB + P6 子树内选，不变）
  ├─ 4. self-improve（不变）
  ├─ 5. _record_supernode(N)              # P7 + 本计划字段扩展
  ├─ 6. evaluate_on_root(N)               # 固定 root 旁路评估 2–3 次（见 5.5）
  ├─ 7. compute_fitness(N)                # 用"入档前"的 reference 算 F_p/F_e + novelty
  └─ 8. archive_upsert(N)                 # 硬门 → MOME 格子 + Pareto 前沿 + 容量淘汰 + 参考伙伴窗口
outer N 结束
```

### 5.2 锦标赛（两层采样）

- **第 1 层**：在所有**非空格子**里均匀随机抽一格。
- **第 2 层**：在该格的 Pareto 前沿里随机抽一个精英。
- **冷却（漏洞 10）**：候选中排除最近 `cooldown_window`（默认 3）届被选过的 outer；若排除后候选池空，则**放宽**（允许复用）。
- 重复抽 `tournament_k` 个（默认 3），取 $F_p$ 最高者（同分用 $F_e$ 破平）。
- 候选池为空 / 冷启动 → 返回 None（链式）。

### 5.3 硬门与隔离（漏洞 4）

- 当前代码的排除条件：`rule_violation.triggered == true` 且 `evidence_ref` **非空**。
  （“可定位”校验、独立第二意见、`rule_violation_unconfirmed` 事件、申诉通道**均未实现**，已记入 §11 已知风险。）
- 结果：不入档案、不进 population、不可被选中（事件 `supernode_excluded`）。
- 隔离落点：archive/supernode 读函数仅 framework 内部；不注册任何 role 工具；`AccessBroker`/`frozen` deny `ckpt/**`；回归断言 evaluator 无法触达 `ckpt/supernodes`、`ckpt/archive`。

### 5.4 参考伙伴（赛季化）

- `reference` = 最近 M=10 个已入档 supernode（滑动窗口，按入档顺序）。
- $z_{\text{ref}}$ 与 novelty 都只在 `reference` 内计算。
- 窗口不足 M 时按实际数量计算。

### 5.5 固定 root 评估（漏洞 1 + 漏洞 9）

- 每个 supernode 入档前，在**固定 root 基线**（`eval_base` ref，初始 = run 的 initial task commit）上跑 `eval_samples`（默认 3）次独立 **plan+task 轮次**（不含 evaluator 环节），取均值写入 `benchmark.mean/std`。
- **A1 一致性**：`ablation.disable_planner=true` 时评估轮次同样走空 `plan_result`（否则评估会偷偷把“被消融掉的 planner”装回来，A0 vs A1 不可比）。
- 评估**旁路运行**：独立 run_dir；**不写 task tree**、**不写 `scores.log`**、不进角色可见轨迹；设 token/时间预算上限。
- 主线 task 代码照常连续演化，评估与主线解耦。

### 5.6 恢复设计 + heal 闭环（漏洞 2）

1. 写回 `S.designs` 到 `ckpt/design/<role>/config.json`（ledger 守卫，失败回滚）。
2. 立即 `heal_design_keys/slots`（对当前 roles HEAD）。
3. 记录 `restored_then_healed`（含 heal 前后 diff）。
4. **幅度校验**：heal 后 `tools` 与父代 `tools` 的 **Jaccard 重叠 < `heal_tool_overlap`（默认 0.5）且确实发生了 heal** → 判定差异过大，放弃 S。
5. 放弃后从锦标赛**重新抽下一个**，最多 `restore_max_tries`（默认 3）次；全失败 → 本轮不选父代（链式）。

---

## 6. 文件改动清单

| 文件 | 改动 |
|---|---|
| `gan/framework/paths.py` | 新增 `archive_dir` / `archive_path`（`eval_base` 是 loop 配置值，不是 paths 函数） |
| `gan/framework/supernode.py` | payload 字段扩展；读取兼容缺字段 |
| `gan/framework/archive.py` | **新增**：格子映射、Pareto 插入（方向统一）、容量淘汰、参考伙伴窗口、`rebuild` |
| `gan/framework/fitness.py` | **新增**：特征提取、$z_{\text{ref}}$、novelty（含自身 z-score）、$F_p$/$F_e$ |
| `gan/framework/outer_select.py` | **新增**：锦标赛（两层采样）+ 恢复设计 + heal + 幅度校验（ledger 守卫） |
| `gan/framework/eval_on_root.py` | **新增**：固定 root 旁路评估（独立 run_dir、副作用隔离） |
| `gan/framework/loop.py` | outer 开头选父 + 恢复 + heal；`_record_supernode` 字段扩展；outer 末尾 evaluate_on_root + 入档；事件 |
| `gan/framework/access.py` / `frozen.py` | deny `ckpt/**`（隔离纵深防御） |
| `gan/framework/loop.yaml` | 新增 `outer_select.*` / `mome.*` / `fitness.*` / `eval.*` 配置段 |

---

## 7. 配置键（`loop.yaml` 新增）

```yaml
outer_select:
  enabled: false          # 总开关；false = 现有单链行为（完全不变）
  tournament_k: 3         # 锦标赛抽签数
  restore_max_tries: 3    # 恢复父代设计 + heal 的最大尝试数（全失败退回链式）
  heal_tool_overlap: 0.5  # 幅度校验：heal 后 tools 与父代 tools 的 Jaccard 重叠阈值
  cooldown_window: 3      # 漏洞 10 冷却：最近 N 届选过的父代移出候选池（空池则放宽）

mome:
  x_buckets: 6            # int = 内置边界+断言桶数；list = 显式 op 名顺序表
  y_buckets: 5            # int = 断言 5 桶；list = 上界 [0.0,0.5,1.0,2.0]
  z_buckets: 3            # int = 断言 3 桶；list = 上界 [1.0,2.0]
  reference_window: 10    # 赛季 M（novelty 参考集 + z-score 基准）
  novelty_k: 5
  cold_start_min: 10      # 低于此数 → 完全链式
  max_elites_per_cell: 3  # 每格 Pareto 前沿容量上限

fitness:
  beta_variance: 0.1      # F_p: z_mean - beta*z_std + lambda*novelty
  lambda_novelty: 0.2     # F_p / F_e 共用
  w1_calibration: 0.6     # F_e: -w1*z_calibration_error
  w2_verdict_stability: 0.4  # F_e: +w2*verdict_stability

eval:
  eval_base: eval_base    # 固定 root ref（初始 = initial task commit）
  eval_samples: 3         # 每个 supernode 入档评估次数
  eval_timeout_s: 1800    # 评估批次墙钟预算（轮次之间检查；0 = 不限）
```

> `outer_select.enabled=false` 时，本计划的所有新逻辑**短路**，行为与里程碑 A 完全一致。
>
> **CLI 覆盖**（无需改 `loop.yaml`）：`--ablation` 只写 `ablation.*`，里程碑 B 的键走 `--set`，例如
> `--set outer_select.enabled=true --set eval.eval_samples=2`，分桶可用 JSON：`--set 'mome.y_buckets=[0.0,1.0]'`。
> 覆盖通过 driver 的 `--cfg-overrides` 显式 argv 下发（不走 env），并记录在运行产物里。

---

## 8. 实施顺序

| 阶段 | 内容 | 验证 |
|---|---|---|
| **B1** | supernode 字段扩展（`designs` / `rule_violation` / `calibration_error` / `verdict_stability` / `eval_baseline`） | 回归：字段齐全 + 旧 supernode 兼容 |
| **B2** | `archive.py`：格子 + Pareto（方向统一）+ 容量淘汰 + 参考伙伴窗口 + rebuild | 单测：支配裁剪、容量上限、重建一致 |
| **B3** | `fitness.py`：特征 + $z_{\text{ref}}$ + novelty（含自身 z-score）+ $F_p$/$F_e$ | 单测：冷启动、距离、方向、权重 |
| **B4** | `eval_on_root.py`：固定 root 旁路评估 + 副作用隔离 | 断言：不写 tree/scores、独立 run_dir、预算 |
| **B5** | `outer_select.py`：锦标赛（两层采样）+ 恢复 + heal + 幅度校验 | 单测：两层采样、破平、heal 幅度、fallback 终止 |
| **B6** | `loop.py` 接线 + `access/frozen` deny `ckpt/**` + 事件 | 断言：`enabled=false` 不变；隔离触达不到 |
| **B7** | 全量回归 + 文档更新 | `regress_repo_split.py` 扩展 + heal/smoke 全绿 |

---

## 9. 测试计划（`scripts/local/regress_repo_split.py`）

- **格子映射**：top-1 op / issues 阈值 / eval_point 覆盖 → 固定桶正确。
- **Pareto 前沿**：方向统一后同格被支配条目被裁剪，非支配保留；容量上限生效。
- **novelty**：手工构造 3 个 supernode，kNN 距离符合手算；novelty 自身 z-score 化。
- **冷启动**：档案 < 10 → 完全链式（返回 None），不选父。
- **两层采样**：先抽格再抽格内精英；空格跳过；空池返回 None。
- **硬门与隔离**：rule_violation 不入档、不被选中；evaluator `editable_paths`/`grant` 触达不到 `ckpt/supernodes`、`ckpt/archive`。
- **恢复 + heal**：`config.json` 内容 == supernode.designs；heal 差异超阈值 → 放弃并换下一个；全失败 → 链式。
- **固定 root 评估**：不写 tree/scores、独立 run_dir；均值覆盖。
- **enabled=false**：与里程碑 A 行为逐字段一致（回归保护）。

---

## 10. 风险与取舍

| 风险 | 说明 | 缓解 |
|---|---|---|
| **每届固定评估成本** | 评估成功时每届跑 2–3 次 plan+task，成本稳定但更高 | `eval_samples` 可调；`enabled=false` 可整体关闭；`eval_base` 缺失/异常时跳过 |
| **heal 可能放弃父代** | 历史设计与当前代码不兼容时被放弃 | `restore_max_tries` 限制尝试；全失败退回链式 |
| **$F_e$ 数据稀疏** | `verdict_stability` 多数届无数据 | 自动退化只留 `calibration_error`，标记 `fitness_partial` |
| **evaluator 单方面淘汰** | `rule_violation` 由当届 evaluator 单方判定，误判即永久排除好 supernode（无第二意见/申诉） | 暂不解决，记为已知风险（§11） |
| **$F_e$ 自评可刷分** | `calibration_error` / `verdict_stability` 均由当届 evaluator 产出，存在"保守预测/固定 verdict"刷分空间 | 暂不解决，记为已知风险（§11） |
| **档案是派生索引** | 与 supernode 文件可能不一致 | `rebuild` 从 `ckpt/supernodes/*.json` 重建；启动时校验 |
| **混合基准（未消除）** | `reevaluated=true` 条目用固定 root 分数，`false` 条目用主线 inner 分数，两者**尺度不同**却同进一个 $z_{\text{ref}}$ 池 | 每条自带 `eval_baseline` + `reevaluated` 可审计；如需严格可比，消融时固定 `eval_base` 并保证评估不降级（看 `supernode_eval_degraded`） |
| **固定 root 与主线解耦** | 评估基线与主线 task 代码不同 | 明确 `eval_base` 语义；审计记录两套代码 commit |

---

## 11. 待办 / 未决

1. **固定 root 评估运行（B6b，已实现骨架）**：离线 stub 回归已绿，真实 LLM 运行待 Linux 部署环境验证。
2. `accepted_fixed_rate` 是否把 `acted_differently` 计为接受（当前按 `acted`）。
3. 参考伙伴窗口"赛季"是滚动还是按 outer 编号分段（当前滚动）。
4. `eval_base` ref 的命名与固定策略（当前 = initial task commit，永不移动）。
5. ~~配置键未接入~~ **已解决**：`fitness.{beta_variance,lambda_novelty,w1_calibration,w2_verdict_stability}`、`mome.{x_buckets,y_buckets,z_buckets}`、`eval.eval_timeout_s` 均已接入 consumer（`loop.__init__` 读取 → `_archive_obj` / `_archive_supernode` / `run_eval_rounds` 消费）。分桶给 int 时是**断言**（桶数不一致启动报错），给 list 才是显式边界。

> 已解决：漏洞 10（冷却，`outer_select.cooldown_window`）、verdict_stability（客观事实命中率 = `fix_verdict` vs benchmark 相对父代的一致率）。

### 已知风险：不冻结 evaluator（暂不解决）

不冻结 evaluator（evaluator 随 outer 自进化）的两个真实问题，已评估但本轮不修：

1. **evaluator 单方面淘汰 supernode**（急性、不可逆）：当前硬门 = `triggered ∧ evidence_ref 非空`，缺 4 项保护——evidence 可定位校验、独立只读 evaluator 第二意见、`rule_violation_unconfirmed` 审计事件、申诉/复核通道。一次误判即永久损失一个好 supernode。
2. **$F_e$ 自评 Goodhart**（慢性、影响排名）：$F_e$ 的两项都由当届 evaluator 产出，存在"永远预测均值以降低 $\text{calibration\_error}$"、"固定 fix_verdict 刷 $\text{verdict\_stability}$"的刷分空间，排名会偏向"会刷分"而非"真正更好"的 evaluator。
3. **特征漂移**（低危）：`issues_per_task` / `severity` 随 evaluator 标准漂移，影响 Y 轴分桶与 novelty，但不影响 $F_p$/$F_e$ 的绝对排名。
4. **反馈漂移**（低危）：evaluator 标准变化使 planner 追着改，可能振荡；属学习动力学，不影响选择器。

> 前三项均不影响 `outer_select.enabled=false` 的默认行为。
