# 实现计划：outer supernode + inner-outer 衔接 + 拆仓库

> **状态**：实施中（分支 `outer-supernode`）
> **前置决策快照**：见文末「已确认决策」。
> **拆分细节**：`docs/12_拆分仓库方案.md`。

---

## 1. 范围与里程碑

- **里程碑 A（本轮）**：拆仓库 + supernode 记录 + 设计戳 + inner 子树内选。
- **里程碑 B（后续）**：联合 MOME + 锦标赛 + novelty + 参考伙伴赛季化。

---

## 2. 数据模型

### 2.1 仓库布局

沿用 `docs/12`：`base/`（只读快照）+ `task/`（git）+ `roles/`（git），位于 `ckpt/code/` 下。

### 2.2 节点设计戳

`task` 节点 `meta` 增加：

```
outer_id              # 产出该节点的 outer
planner_design_hash   # 产出时 planner 设计 hash
evaluator_design_hash # 产出时 evaluator 设计 hash
task_commit           # 任务代码 commit（拆仓库后）
planner_commit        # planner 代码 commit（拆仓库后，角色侧）
evaluator_commit      # evaluator 代码 commit（拆仓库后，角色侧）
```

设计 hash 用设计 config 内容的 `sha256`（`planner_design_hash` / `evaluator_design_hash`）。

### 2.3 supernode 记录

位置：`ckpt/supernodes/outer_<N>.json`（每 outer 一份，不可变）。

```json
{
  "outer": 3,
  "planner_design_hash": "...",
  "evaluator_design_hash": "...",
  "code_commits": {"task": "...", "planner": "...", "evaluator": "..."},
  "root_genid": "...",
  "subtree": ["g7", "g8", "g9"],
  "benchmark": {
    "per_question": {"g7": [0.9, 0.8, ...], "g8": [...], "g9": [...]},
    "mean": 0.81, "std": 0.07, "coverage": 1.0, "n_valid": 30
  },
  "planner_stats": {"op_dist": {...}, "response_kind": {...}},
  "evaluator_stats": {"issues_per_task": 1.2, "severity": {...},
                      "eval_points_run": ["trajectory_quality", "hard_failure"],
                      "bias": {"predicted": 0.9, "actual": 0.81}}
}
```

> benchmark 的 `per_question` 以「唯一题」为键，逐题存分数序列，**保留用于审计与未来分析**；当前里程碑 B 的 $F_p$ 方差取**该届 inner 分数的总体标准差**（`statistics.pstdev(scores)`），**不使用 bootstrap**（`eval_samples` 太小、且逐题方差度量的是题库难度而非策略稳健性）。

### 2.4 inner 父代候选池与 `cross_subtree` 开关

inner 父代选择的候选池由两个配置键控制（`loop.yaml` 的 `tree.*`）：

- `tree.cross_subtree: bool`（默认 `false`）
  - `false`：**子树优先**——候选池 = 当前 outer 子树（`meta.outer_id == 当前 outer` 的 task 节点）；**子树为空时回落全局一次**（2026-10-23 修订：每个 outer 刚开始时子树必然为空，只在本届子树内选会在 outer 3+ 之后饿死；回落时用同一个 UCB/best/latest metric 从全局池选上一届的真实节点，并记 `inner_pool_fallback` 事件）。初始/迁移节点（无 `outer_id`）在两种池里都仍可参与。
  - `true`：以 `cross_subtree_epsilon` 的概率把候选池扩大到全局（所有 outer 的有效节点，仍受 valid_parent / branch / depth 约束）；**`epsilon = 1.0` 时每轮都全局，回落分支永不触发**。
- `tree.cross_subtree_epsilon: float`（默认 `0.1`）：触发全局采样的概率；其余轮次仍在当前子树内选。

UCB / best / latest 等排序方法不变，只改变候选池构成。

**跨届选父的三个连带语义（2026-10-23 修订）：**

1. **议题往返按届清洗**：`respond_issue` / `judge_fix` 的 `issue_id` 是每届 evaluator 会话内的命名空间（每届刷新实例，id 重新从 `i-1` 开始）。跨届父代携带的旧 issues 在选父处被清空（保留 `diff_summary` / 分数 / 校准），`_parent_digest` 置空，使 planner 不再被强制回应旧议题、evaluator 不再被要求对旧议题 `judge_fix`，也避免旧 `i-1` 与本局新 `i-1` 的 id 撞车。
2. **task 谱系与 P/E 谱系解耦**：inner 回落决定的是 **task 节点的 config+code 血缘**；外层选择（docs/14）恢复的是 **P/E 设计**。两者独立、可能来自不同届，报告/论文中必须声明二者不是同一条线，避免读者误读。
3. **depth_penalty 暂不特判（TODO，未解决）**：全局 UCB 的 `depth_penalty`（0.05/层）会系统偏向早期浅节点（深度差 4 ≈ 0.2 分，对 0~1 的分数不可忽略）。是否在回落时去掉该惩罚、或改用 `latest`，留待观察 `inner_pool_fallback` 选出的父代谱系后再决定。


---

## 3. 文件改动清单

### 3.1 拆仓库（docs/12 §5–§13）

| 文件 | 改动 |
|---|---|
| `gan/framework/paths.py` | 增加 `repo_root(name)` / `base_root` / `task_root` / `roles_root` 路径 |
| `gan/build.py` | `ensure_code_root` → `ensure_roots`（建 base/task/roles + 初始提交） |
| `gan/framework/frozen.py` | 增加 `OWNER_MAP`（相对路径 → 仓库名），不改授权语义 |
| 新增 `gan/framework/repo_roots.py` | `RepoRoots` / `owner_repo` / `resolve_read` / `gan_roots(role, roots)` |
| `gan/tools/assembly.py` | `gan_roots` 改为按 role 路由 |
| `gan/registries/loader.py` | 不变（调用方传正确目录） |
| `gan/framework/access.py` | `AccessBroker` 持 `RepoRoots`；grant 记录 `repo` |
| `gan/framework/code_repo.py` | 所有操作按仓库；锁按仓库 |
| `gan/framework/loop.py` | `_align_code_base` 只 checkout task；`_apply_self_patch` 提交 roles；节点 meta 记三 commit |
| `gan/framework/checkpoint.py` | `code.json` 清单化；restore 逐仓库 |
| `gan/framework/task_execution.py` | 装配/沙箱按仓库 |
| `gan/framework/preflight.py` | `repair_dangling_designs` 按仓库 |

### 3.2 设计戳 + 两级树 + 子树内选

| 文件 | 改动 |
|---|---|
| `gan/framework/tree/store.py` | `TreeStore` 支持按 `outer_id` 过滤候选（子树内选） |
| `gan/framework/loop.py` | 选父代时传入当前 outer 的 `outer_id` 过滤；产出节点时写设计戳 |

### 3.3 supernode 记录

| 文件 | 改动 |
|---|---|
| 新增 `gan/framework/supernode.py` | `record_supernode(...)`：聚合 per-question 分数、op/issue/eval 统计，写 `ckpt/supernodes/outer_<N>.json` |
| `gan/framework/loop.py` | outer 末尾调用 `record_supernode` |

---

## 4. 实施顺序

| 阶段 | 内容 | 验证 |
|---|---|---|
| **P0** | 分支 + 本计划 | — |
| **P1** | `RepoRoots` + `owner_repo` + `gan_roots(role)`（先只接一个 root，行为不变） | `py_compile` + 现有回归 |
| **P2** | `ensure_roots` 建 base/task/roles + `code.json` 清单化 | 冒烟：三仓库生成、初始提交 |
| **P3** | `_align_code_base` 只 checkout task；self-improve 提交 roles | 断言：checkout 后 roles 不变 |
| **P4** | broker/workspace/patch/commit 按 seat→repo 路由 | 断言：task 补丁只改 task；role 补丁只改 roles |
| **P5** | checkpoint/resume 三 commit + 迁移 | 断言：resume 一致 |
| **P6** | 设计戳 + 子树内选（含 `cross_subtree` 开关 + `loop.yaml` 键） | 断言：默认只在本 outer 子树选；`cross_subtree=True` 时低概率跨子树 |
| **P7** | `record_supernode` + `ckpt/supernodes/outer_<N>.json` | 断言：字段齐全 |
| **P8** | 沙箱裁剪 + 回归 | 现有套件全绿 |

---

## 5. 测试

- 新增 `scripts/local/regress_repo_split.py`：拆仓库不串味 / 对齐不碰角色 / self-improve 不丢 / resume 一致 / supernode 字段。
- 回归：`scripts/local/run_tool_regression.py`、frozen 布局检查、deep/batch 套件。

---

## 6. 已确认决策（快照）

| 项 | 决策 |
|---|---|
| 档案 | 联合 MOME（格子存 P/E 对 Pareto 前沿） |
| 评估口径 | 固定 root + 独立 inner 取均值 |
| 参考伙伴 | 赛季化，M=10 个 supernode |
| 违规第二意见 | 冻结参考 Evaluator |
| novelty 归一化 | 运行 z-score |
| 冷启动 | λ=0 + 降桶数 |
| 重复评估 | 2–3 次独立 inner |
| 本里程碑 | 拆仓库 + supernode + 设计戳 + 子树内选 |
| inner 选择 | 默认限制在当前 outer 子树内（`cross_subtree=False`） |
| inner 跨子树 | `cross_subtree=True` 时以 `cross_subtree_epsilon`（默认 0.1）概率从全局候选池采样 |
| supernode 字段 | 完整（含逐题分数 + issue/eval 统计） |
| 分支 | `outer-supernode` |
