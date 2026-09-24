# 工具与组件体系：全量盘点、结构诊断与重构建议

> 承接前两份：`工具与算子管理_漏洞梳理与修改建议.md`、`工具与算子管理_实现方案.md`。本轮把范围从"注册表相关工具"扩大到 **`gan/tools/**` 与 `gan/components/**` 全部**，并对**目录组织结构**本身给出重构建议。
> 仓库：`\\wsl.localhost\Ubuntu\root\HyperAgents`。三路并行只读调查（tools 全量 / components 全量 / 结构→加载机制）+ 自持取证。均附 `文件:行号`。

---

## 0. 结论摘要

### 0.1 一个根因，多个症状

**根因（S1）**：体系里存在**三套互不强制的身份**——

| 身份 | 定义处 | 用途 |
|---|---|---|
| **目录位置** | `gan/tools/<层>/`、`gan/components/<role>/<kind>/` | 人类分类 + 装配源 |
| **注册名** | `gan/registries/*.json` 的 `name` | 选择（`select_component`）、装配解析（`module_path`） |
| **可调用名** | 文件 stem（`agent/tools/__init__.py:45`） | LLM 实际调用的工具名 |

三者被要求一致，但**没有任何代码强制**。前两轮报告里的 T1、N1、V4、孤儿检测缺失、以及本轮新发现的评估点别名问题——**全部是这一个根因的症状**。

新增的关键事实：`tools_available` 在角色路径上**恒为 `"all"`**（`gan/roles/base_role.py:105` 默认值，`planner.py:116`/`evaluator.py:88` 的 `run()` 调用都不传该参数），因此 `names` 过滤**从不生效**——装配目录里有什么就被全部加载。这改变了若干缺陷的性质（见 §2 精算）。

### 0.2 结构层面的六个问题

| # | 问题 | 严重度 |
|---|---|---|
| S2 | 目录承载语义，但**装配丢弃语义**（扁平化）→ 目录分类是装饰性的 | 中 |
| S3 | **两套正交分类轴并存**：`tools/` 按权限级、`components/` 按角色×能力类型 → 同类能力被按不同轴切分 | 中 |
| S4 | `work/common/` 是**杂项桶**（读源码 + 读轨迹混放），判据是"冻结且共享"而非能力类型 | 低 |
| S5 | `shared/` 名义共享、**实际单角色**（只放 skill，而 skill 只有 task 能选）；无 `roles` 字段 → 无法表达"仅某几个角色共享" | 中 |
| S6 | `deep/` **内部分层矛盾**：申请网关 与 直接执行+自带授权 混放 | 中 |
| S7 | **空占位目录**：`task/skills/`、`task.json`、`planner.json` 均空，后者还对三角色全不可写 | 低 |

---

## 1. 全量盘点

### 1.1 `gan/tools/**`（18 个工具 + `assembly.py`）

角色集合由目录归属推导（`assembly.py:37-47`：planner/evaluator 得 `work/<role>`+`work/common`+`design`+`deep`；task 得空）。

| # | 文件 | 可调用名 | 职责 | required | context | record | 层 | 角色 |
|---|---|---|---|---|---|---|---|---|
| 1 | `design/set_prompt.py` | `set_prompt` | 写 `config["prompt"]` | `text` | Design | ✅ | always-on | p,e |
| 2 | `design/set_config.py` | `set_config` | 写已存在的 key；拒绝新 key | `key,value` | Design | ✅ | always-on | p,e |
| 3 | `design/set_param.py` | `set_param` | 写 `params[name]` | `name,value` | Design | ✅ | always-on | p,e |
| 4 | `design/select_component.py` | `select_component` | 把已注册组件加入 slot | `slot,name` | Design | ✅ | always-on | p,e※ |
| 5 | `design/deselect_component.py` | `deselect_component` | 从 slot 移除 | `slot,name` | Design | ✅ | always-on | p,e※ |
| 6 | `deep/request_source_access.py` | `request_source_access` | DEEP 网关：申请 view/modify 并拷入 workspace | `paths,reason` | Access+Design | ✅ | always-on | p,e |
| 7 | `deep/unregister_component.py` | `unregister_component` | DEEP 删：删条目+删文件 | `kind,name` | Access | ❌ | always-on | p,e |
| 8 | `work/common/read_file.py` | `read_file` | 读 workspace `src` | `path` | Access | ❌ | always-on | p,e |
| 9 | `work/common/list_dir.py` | `list_dir` | 列 `src` 目录 | — | Access | ❌ | always-on | p,e |
| 10 | `work/common/grep.py` | `grep` | 在 `src` 内正则搜索 | `pattern` | Access | ❌ | always-on | p,e |
| 11 | `work/common/edit_source.py` | `edit_source` | 受限编辑器（代理 `agent.tools.edit`） | `command,path` | Access | ❌ | always-on | p,e |
| 12 | `work/common/list_editable.py` | `list_editable` | 展开 allowlist 为具体文件 | — | Access | ❌ | always-on | p,e |
| 13 | `work/common/read_trajectory.py` | `read_trajectory` | 读某 task 代的脱敏轨迹 | — | Access | ❌ | always-on | p,e |
| 14 | `work/common/read_session_trajectory.py` | `read_session_trajectory` | 读自己本 outer 会话 | — | Access | ❌ | always-on | p,e |
| 15 | `work/planner/respond_issue.py` | `respond_issue` | planner 回应一条 issue | `issue_id,accepted` | Plan | ❌ | always-on | **p** |
| 16 | `work/evaluator/report_issue.py` | `report_issue` | 记录问题 | `issue_id,description` | Eval | ❌ | always-on | **e** |
| 17 | `work/evaluator/judge_fix.py` | `judge_fix` | 判定 issue 是否真修复 | `issue_id,fixed` | Eval | ❌ | always-on | **e** |
| 18 | `work/evaluator/record_predicted_score.py` | `record_predicted_score` | 记录 blind 预测分 | `score` | Eval | ❌ | always-on | **e** |

※ planner 拿到但**不可用**：planner schema 无 slot（`schema.py:21-24`），两算子必然返回 `slot not in schema`（`select_component.py:39-40`）。

**命名一致性**：`gan/tools/**` 内 **18/18 的 stem == `tool_info().name`** ✅。唯一不一致在外部源 `agent/tools/edit.py`（stem `edit` vs name `editor`），GAN 侧通过把它重导出为 `editor.py` 规避。

**三个 `__init__.py`、`assembly.py`、`gan/tools/__init__.py` 均不会被装配**（`always_on_dirs` 只返回子目录，`assembly.py:41-47`；顶层从不作为源）。**"装配器被自己装配"的担忧排除。**

**记录风格不统一**：design 5 个算子用 `ctx.record`；`respond_issue`/`report_issue`/`judge_fix` 走各自上下文的专用方法（`add_response`/`add_issue`/`add_fix_verdict`）；其余 work 工具不记录。`unregister_component` **完全无 record**（仅 `broker.grant` 落 `events.jsonl`）。

### 1.2 `gan/components/**`（6 个真实组件）

| 文件 | 可调用名 | 注册名 | 注册处 | kind | 可被选中 |
|---|---|---|---|---|---|
| `shared/skills/bash.py` | `bash` | `bash` | `shared.json:3-9` | skill | registry 层全角色；**实际仅 task** |
| `shared/skills/editor.py` | `editor` | `editor` | `shared.json:10-16` | skill | 同上 |
| `evaluator/eval_points/eval_trajectory_quality.py` | `eval_trajectory_quality` | **`trajectory_quality`** | `evaluator.json:3-5` | eval_point | evaluator |
| `evaluator/eval_points/eval_hard_failure.py` | `eval_hard_failure` | **`hard_failure`** | `evaluator.json:6-8` | eval_point | evaluator |
| `evaluator/eval_points/eval_reward_hacking.py` | `eval_reward_hacking` | **`reward_hacking`** | `evaluator.json:9-11` | eval_point | evaluator |
| `evaluator/eval_points/eval_rule_violation.py` | `eval_rule_violation` | **`rule_violation`** | `evaluator.json:12-14` | eval_point | evaluator |

**四位评估点的可调用名 ≠ 注册名**（前缀 `eval_` 之差）。这**不是**加载缺陷（因 `names="all"`，工具照常加载），但意味着：

- 设计 config 的 `eval_points` 列表写 `trajectory_quality`（`build.py:49` 从 `reg.names("eval_point")` 取注册名），而 LLM 看到并可调用的工具名是 `eval_trajectory_quality`。
- **两套名字在 agent 视野里分裂**：config 里一个名字、工具 schema 里另一个名字。任何提示词若引用 config 中的名字，都是错的。
- 对本轮提出的 `list_components` 有直接影响：它**必须报告可调用名（stem）**，否则 agent 会按 config 里的名字去调，必然 `Tool not found`。

**默认选中：无。** task `skills=[]`、evaluator `eval_points=[]`（`schema.py:34,37`），seed 只写 prompt。**没有任何组件被任何角色默认选中。**

**`task/skills/` 与 `task.json` 为空占位**：task 的 opt-in 面实际全部来自 `shared.json`（`bash`/`editor`），而 `task_agent.py:63` 传 `tools_available=skills`——这是**唯一**真正传了 `names` 的地方（task 侧）。

**`planner.json` 空，且对三个角色全不可写**（`frozen.py:30-50` 的三个根集合均不含它）。

### 1.3 三套身份的实测偏差

| 场景 | 目录 | 注册名 | stem | 是否一致 |
|---|---|---|---|---|
| 18 个 `gan/tools` 工具 | ✅ | —（不注册） | ✅ | 一致 |
| `shared/skills/{bash,editor}` | ✅ | ✅ | ✅ | 一致 |
| 4 个 eval_points | ✅ | ❌（少 `eval_` 前缀） | — | **不一致** |
| `agent/tools/edit.py` | ✅ | — | ❌（`edit` vs `editor`） | **不一致** |

---

## 2. 结构诊断（新发现 S1–S12）

### S1【根因】三套身份无强制一致 — `中`

见 §0.1/§1.3。**归一化后**这一条能同时消除：T1（同名覆盖）、N1、V4（`shared.json` 静默遮蔽）、N2（name 契约）、孤儿检测缺失、评估点别名、`edit`/`editor` 不一致。

### S2 目录承载语义但装配丢弃语义 — `中`

装配用 `basename` 落盘（`assembly.py:111,114`），源目录层级完全消失；加载用单层 `glob("*.py")`（`agent/tools/__init__.py:23`）。**推论**：`components/<role>/<kind>/` 的两级语义在装配后**不可观测**——目录既不是隔离手段，也不是校验依据。当前目录分类**纯属人类可读性**。

### S3 两套正交分类轴并存 — `中`

- `tools/` 按**权限级/功能**分：`work/`（角色能力）、`design/`（浅层算子）、`deep/`（深层门控）。
- `components/` 按**角色 × 能力类型**分：`<role>/<kind>/`。

后果：**同类能力被按不同轴切分**。最典型是 evaluator 的评估点被"劈成两半"——核心 3 个（`report_issue`/`record_predicted_score`/`judge_fix`）在 `tools/work/evaluator/`（always-on），可选 4 个在 `components/evaluator/eval_points/`（opt-in）。同一语义域（"评估点"）跨两个顶层目录、两套激活机制。这是**有意的设计**（`docs/3:51`），但从"目录即语义"的角度看，语义被激活方式切碎了。

### S4 `work/common/` 是杂项桶 — `低`

| 子族 | 工具 | 数据域 | 控制通道 |
|---|---|---|---|
| 读**源码** | `read_file`,`list_dir`,`grep`,`edit_source`,`list_editable` | `<workspace>/src` | `AccessBroker` 授权 |
| 读**轨迹** | `read_trajectory`,`read_session_trajectory` | `output_dir` 下轨迹文件 | loop 注入的 `trajectory_genids` 白名单 |

两者数据来源、可见性控制、上下文通道**都不同**，却同放 `common/`。命名的实际判据是"**冻结且共享**"（`docs/3:55`、`AGENTS.md:123-126`）——即**按可变性而非能力**分类。这个判据本身自洽，但目录名 `common` 无法传达它。

### S5 `shared/` 名义共享、实际单角色 — `中`

- `shared.json` 被所有角色合并加载（`loader.py:128`）→ 是**唯一**的跨角色共享机制。
- 但 `shared/` 当前只放 `skill`，而 skill 只有 task 能选（evaluator/planner schema 无 `skills` slot）→ 对 planner/evaluator **不可选中**，"共享"是名义上的。
- 注册表条目 schema 只有 `name/kind/module/description/params_schema`（`loader.py:3-5`），**无 `roles` 字段** → 无法表达"仅 planner+evaluator 共享、排除 task"；也无法表达"跨角色的 eval_point"（`shared/` 下无 `eval_points/`）。

### S6 `deep/` 内部分层矛盾 — `中`

| 维度 | `request_source_access` | `unregister_component` |
|---|---|---|
| 语义 | **申请**权限（网关） | **直接执行**删除 |
| 是否走申请流程 | 它就是流程 | **绕过**：自行 `broker.grant(reason="unregister")` 后立即改盘（`:66,:68`） |
| 审计 | `ctx.record("request_source_access",...)`（`:41`） | **无 record** |
| 自述 | "the single entry for source-level changes"（`:1-7`） | "DEEP delete" |

即：一个名副其实的"单一入口"旁边，放了一个自带授权、无记录的旁路。`docs/3:52` 把二者并列描述为"深度门控入口"，但它们在**分层**上不同（申请 vs 执行）。

### S7 空占位与不可写注册表 — `低`

`task/skills/` 仅 `__init__.py`；`task.json` 与 `planner.json` 均为 `{"components": []}`；`planner.json` 还被 `code_repo.registry_report` 纳入校验（`code_repo.py:291`）却无人可写 → "被校验但不可写"的空资产。

### S8 planner 拿到不可用算子，且提示词要求使用 — `中`

`select_component`/`deselect_component` 是 always-on 给 planner 的，但 planner schema 无 slot（`schema.py:21-24`）→ 调用必然失败；而 planner 的 self-improve 提示词**要求**使用它们（`planner.py:127,197`）。这是"工具可见性"与"schema 能力"脱节。

### S9 `assembly.py` 位置不符语义 — `低`

它是装配器（无 `tool_info`/`tool_function`），不是工具，却被以包路径导入（`base_role.py:12`、`task_execution.py:27`）。**不会被误装配**（§1.1），但放在"存放可加载工具模块"的目录里语义不符。

### S10 文档漂移（`memory` 残留）— `低`

`docs/3:23,42` 与 `docs/4` 仍写 `components/shared/memory/` 与 schema 的 `memory` 槽；代码已于 v5 删除（`docs/3:65`），`schema.py:13-31` 无 `memory`。（`AGENTS.md:31-39` 已与代码一致，`docs/6:16` 的 D2 修正已生效。）

### S11 死引用 `code_edit` — `低`

`gan/roles/planner.py:125-126` 检测 `r.get("op") == "code_edit"`，但该工具文件不存在（已并入 `request_source_access`，`request_source_access.py:2-4`）。

### S12 无命名空间隔离机制 — `中`

扁平化 + `basename` 决定：**不可能靠目录隔离**。跨目录同名 `.py` 会静默覆盖（顺序 `work/<role>`→`work/common`→`design`→`deep`→opt-in，且 `glob.glob` 未排序）。当前无实际冲突，但这是结构脆弱点。

---

## 3. 重构建议

### 3.1 目标模型：单一身份 + 显式元数据 + 目录仅作分类

```
① 注册表条目 = 唯一真值源
     name      = 可调用名（强制 == 文件 stem）
     kind      = 能力类型（skill | eval_point | …）
     module    = 相对 gan/components 的路径
     roles     = [新] 可见角色白名单 ← 取代"目录即角色"
     activation= [新·可选] always-on | opt-in
     description
     删除 params_schema（无消费者）

② 目录 = 人类分类，不承载授权
     components/<kind>/...        按能力类型分（不再按角色分）
     tools/<layer>/...            layer ∈ {work, design, gate}

③ 装配 = 注册表驱动（而非目录扫描）+ 保留结构（→ 使文件名可带命名空间）
```

**核心判断**：既然目录语义在装配时被丢弃、授权实际由 `frozen.py` 白名单决定、可见性实际由 schema+config 决定——**那么让目录继续表达 role/activation 就是重复且易漂移的状态**。把它收敛到注册表的 `roles` 字段（单一真值），目录只留人类分类。

### 3.2 分层方案（按风险递增，可只做前两层）

#### Tier 0 — 零机制风险，立即可做（纯一致性）

| # | 动作 | 落点 |
|---|---|---|
| T0-1 | **强制 `name == stem`**，并把 4 个 eval_point 的注册名改为与 stem 一致（或反之改文件名），使 config/工具/schema 三处同名 | `evaluator.json:3-14`；`eval_*.py:7` |
| T0-2 | 修 `agent/tools/edit.py` 的 `editor` vs `edit`（或显式记录该例外） | `agent/tools/edit.py:6` |
| T0-3 | 删 `planner.py:125-126` 的 `code_edit` 死分支 | `gan/roles/planner.py:125-126` |
| T0-4 | 文档校正：`docs/3:23,42`、`docs/4` 移除 `memory` 槽与 `components/shared/memory/` | 文档 |
| T0-5 | `unregister_component` 补 `ctx.record`（结构化字段），与 `register_component` 对称 | `unregister_component.py:66` |
| T0-6 | 清理空占位或加注释说明其占位意图（`task/skills/`、`task.json`、`planner.json`） | 三处 |

**Tier 0 不改任何机制**，只消除三套身份的偏差与文档漂移。**建议先做**，因为 §1.3 的偏差是所有下游缺陷的输入。

#### Tier 1 — 低风险，注册表加字段（不改装配）

| # | 动作 | 落点 |
|---|---|---|
| T1-1 | 注册表条目增加 **`roles`** 字段（可选；缺省 = 原语义：`shared.json` 全角色、`<role>.json` 仅该角色） | `loader.py:50-63` `entry_reason`；`loader.py:124-128` 合并逻辑 |
| T1-2 | **B5 修复**：`bash`/`editor` 的 `roles` 设为 `[]` 对 task 关闭（或移入 task 不可见的注册表），消除"task 拿到 bash 可读 `.env`/数据集" | `shared.json:3-16` |
| T1-3 | **S8 修复**：`select_component`/`deselect_component` 加 `roles` 限制（不给 planner），或给 planner schema 加只读占位 | `schema.py:21-24`；或算子内自检 |
| T1-4 | **`assembly.py` 移出 `tools/`** → `gan/framework/assembly.py`（它是框架代码）；同步两处 import | `base_role.py:12`、`task_execution.py:27` |
| T1-5 | 采纳前两轮的 **R-a/V1–V4 + `validate_registry`**（含 `roles` 一致性、`name==stem`、孤儿、duplicate） | `loader.py`；`code_repo.py:283-320` |

Tier 1 让"目录 vs 注册表"的重复状态**只有一份真值**（`roles`），但**装配仍是目录扫描**——即目录仍须保持现有布局。这是**渐进迁移的中间态**。

#### Tier 2 — 中风险，需改装配/加载机制（使目录结构真正可用）

**前提**：让嵌套子目录可用。三处小改：

| # | 动作 | 落点 | 说明 |
|---|---|---|---|
| T2-1 | 装配改 `rglob("*.py")` 并**保留相对路径**（`os.makedirs` + 相对路径落盘） | `assembly.py:106-114` | 使目录层级在装配后仍存在 |
| T2-2 | 加载改 `glob("**/*.py")`（递归） | `agent/tools/__init__.py:23` | 发现子目录内模块 |
| T2-3 | 模块名由相对路径生成（`/`→`_`）而非叶子名 | `agent/tools/__init__.py:32` | 避免同名文件撞模块名 |

**T2 的价值**：一旦这三处改完，**目录结构才真正承载语义**——可以用 `components/skills/`、`components/eval_points/` 做**天然的命名空间隔离**（S12 消除），也可以用子目录做分组，不再有"加一层即失效"的陷阱。

**风险**：`T2-1` 改变装配产物布局，需确认 `_clear_tools_dir`（`assembly.py:70-85`）与 task 沙箱拷贝（`task_execution.py:71-79`，`GAN_TASK_SKILLS_DIR`）能处理子目录；`T2-3` 改模块名会改变 `sys.modules` 键（当前 `module_from_spec` 不写入 `sys.modules`，影响面小）。

#### Tier 3 — 结构性重划（可选，建议随 Tier 2 之后）

**A. `components/` 改按能力类型分（去掉 role 层）**

```
gan/components/
  skills/           ← 原 shared/skills + task/skills
    bash.py  editor.py  …
  eval_points/      ← 原 evaluator/eval_points
    trajectory_quality.py  hard_failure.py  …
```

角色可见性由注册表 `roles` 声明。**收益**：①`components/` 变为 kind 的完整笛卡尔积（S5 消除：可以放"跨角色 eval_point"）；②评估点不必因"只有 evaluator 能选"而放进 `evaluator/` 目录；③直接消灭 S2 的"目录语义丢失"——目录只表达 kind，而 kind 是注册表字段，两者天然一致。

**B. `tools/` 按权限级收敛为三层，并显式命名**

```
gan/tools/
  work/          角色能力（可进化面：work/<role>/）
    common/      共享只读/受限写（plumbing）
    planner/     仅 planner
    evaluator/   仅 evaluator
  design/        浅层算子（写 config）
  gate/          [改名自 deep] 深层门控 — 统一为"申请-执行"两段
    request.py   申请（原 request_source_access）
    unregister.py 执行（补 record，见 T0-5）
  (assembly 移出，见 T1-4)
```

**收益**：S6 的"申请 vs 执行"矛盾被显式化为同一层的两个明确阶段；`gate` 比 `deep` 更能传达"门控"语义（`deny_deep` 开关对应改名为 `deny_gate`，或保留以兼容）。

**C. 拆分 `work/common/`（S4）**

```
gan/tools/work/common/
  src/           read_file list_dir grep edit_source list_editable
  traj/          read_trajectory read_session_trajectory
```

**依赖 T2**（否则子目录失效）。若不采纳 T2，则**不要**做这一步——保持扁平，仅更新文档说明 `common` 的判据是"冻结且共享"。

### 3.3 迁移路径与取舍

| 阶段 | 内容 | 前置 | 风险 |
|---|---|---|---|
| P0 | Tier 0（六项一致性） | 无 | 极低（改常量/文档/改注册名） |
| P1 | Tier 1（`roles` 字段 + B5 + S8 + assembly 移出 + 校验层） | P0 | 低（不改装配机制） |
| P2 | Tier 2（递归装配/加载/模块名） | P1 | 中（改产物布局，需回归） |
| P3 | Tier 3（components 按 kind、tools 三层、common 拆分） | P2 | 中（纯移动 + 同步注册表） |

**取舍建议**：
- **若只做一致性、不做结构大改** → 完成 P0+P1 即可（覆盖 B5/S8 两个中危、消除三套身份偏差、装上校验层）。
- **若要"目录结构可承载语义"** → 必须做 P2，否则 P3 的任何目录调整都是装饰性的（S2）。
- **P3-A（components 按 kind）优先级最高**：它同时解决 S2、S5、S12，且改动集中在一个目录树 + 注册表 `module` 路径。

---

## 4. 与既有工作的衔接

| 既有编号 | 本轮补充 |
|---|---|
| **T1**（basename 覆盖） | S12 确认它是**机制性**的（扁平化 + 未排序 glob）；根治在 T2，缓解在 Tier 1 的 `validate_registry` 同名检测 |
| **N1**（覆盖面修正） | 仍成立；且 `names="all"` 意味着覆盖**不会被 `names` 过滤救回** |
| **N2**（name 契约） | 升级为 S1 的一部分；T0-1/T0-2 直接修 |
| **V4**（`shared.json` 遮蔽） | 由 Tier 1 的 `roles`/duplicate 校验覆盖 |
| **R4/F1/G5**（信息传递） | 不受本轮结构影响；但 `list_components` 需报告 **stem（可调用名）**，否则与 §1.2 的别名问题冲突 |
| **三项实现方案** | `register_component` 增加 `roles` 参数（Tier 1 后）；`list_components` 输出须含 ①可调用名 ②`roles` ③validity ④未注册候选 |
| **B5**（task 拿 bash） | 本轮给出结构性修法：`roles` 字段（T1-2） |
| **S8**（planner 拿到不可用算子） | 本轮新登记 |
| **`docs/7` §3 待办** | 本轮建议把"目录结构"单列为 §3 的独立条目（当前 §3 的分段未含结构维度） |

---

## 5. 验证断言（本轮新增）

| # | 断言 | 对应 |
|---|---|---|
| W-a | 每个注册条目的 `name` == 其 `module` 文件 stem（含 4 个 eval_point 与 `edit`/`editor`） | T0-1/T0-2 |
| W-b | 注册表 config 中 `eval_points` 的名字与 LLM 实际可调用的工具名**逐项相等** | §1.2 |
| W-c | 装配产物的工具名集合 == 该角色 schema 允许选择的组件名集合（无"可见不可用"） | S8 |
| W-d | `shared.json` 中 `roles` 不含 task 的条目，未被装配进 task 的 `skills` 目录 | T1-2/B5 |
| W-e | 嵌套子目录中的组件（若采纳 T2）能被装配并加载 | T2 |
| W-f | 跨目录同名文件（若存在）**不**静默覆盖，而是报冲突 | S12 |
| W-g | `components/` 下任一 `kind` 目录中的组件，其注册 `kind` 与目录名一致 | T3-A |
| W-h | `planner.py` 中不存在对已删算子名（`code_edit`）的引用 | S11 |
| W-i | 文档中不出现 `memory` 槽或 `components/shared/memory/` | S10 |

---

## 6. 未决 / 需确认

| # | 事项 | 说明 |
|---|---|---|
| U1 | `docs/3:51` 把评估点劈成两半是**有意设计** | Tier 3-A 若采纳，需评估是否会与"always-on 评估点不做"的既有决策（`docs/5:23`）冲突 |
| U2 | T2 对 task 沙箱的影响 | `GAN_TASK_SKILLS_DIR` 下若出现子目录，需确认 `task_agent.py` 侧加载能发现 |
| U3 | `gate` 改名波及 `deny_deep` 键名 | 改名会牵动 `loop.yaml` 与文档；可只改目录名、保留配置键名以降低波及 |
| U4 | `roles` 字段与 `frozen.py` 的关系 | `frozen.py` 管"能否改"，`roles` 管"能否选"——两者正交，需在文档中明确，避免混淆 |
| U5 | Tier 2 的 `sys.modules` 影响 | 当前 `module_from_spec` 不写入 `sys.modules`；改模块名后需确认无隐蔽依赖 |
| U6 | `params_schema` 删除 | 与上一份报告 U4 同一项，需确认无未来规划 |
