# 工具与算子管理：deep 写通道修复与会话整体梳理

> **承接**：`docs/工具管理审查.md`、`docs/7_遗留问题与待办.md` §3、`gan_tools_*.md` 系列。
> **本文档定位**：本会话在"工具与算子管理"方向上的**唯一汇总记录**。每轮改动**追加**到 §3，遗留问题**追加**到 §6；不另开新文档、不按改动分册。
> **范围**：`register_component` / `unregister_component` / `request_source_access` 三个 deep 工具，以及它们依赖的 grant 底座（`gan/framework/access.py`、`gan/framework/frozen.py`）与补丁/提交链路（`gan/patch.py`、`gan/framework/code_repo.py`）。
> **方式**：真实链路实证——真实 `AccessBroker` / 真实工具 / `build_patch_from_workspace` / `apply_code_patch`，不依赖手写补丁。
> **证据约定**：`文件:行号` 指**本文档最后一次更新时**的仓库状态；跨轮次引用时以符号名（函数/常量）为准。

---

## 0. 结论摘要

### 0.1 提交索引

| 轮次 | 提交 | 内容 |
|---|---|---|
| 第 1 轮 | `e87fedc` | R1（单独调用丢失）+ P1（换行） |
| 第 2 轮 | `fix(gan/tools): deep-write workspace-overwrite, registry scan, path traversal, new-component register` | R2 + S1 + P4 + V1（安全回归）+ P3/H3a |

> 第 2 轮之后的提交 hash 见 `git log --oneline`（文档内不写自身提交的 hash，避免自引用失效）。

### 0.2 逐条结论

| 编号 | 问题 | 状态 |
|---|---|---|
| **R1** | `register_component` / `unregister_component` 单独调用时补丁不生成（三成因叠加） | 已修（第 1 轮） |
| **P1** | 注册表末尾换行丢失 → 补丁必然 apply 失败 | 已修（第 1 轮） |
| **R2** | 同会话多次 `unregister_component` 只有最后一次生效 | 已修（第 2 轮） |
| **S1** | 扫描 repo 注册表 → `register` 后同会话 `unregister` 报 "component not found" | 已修（第 2 轮） |
| **P4** | 重复 `request_source_access(modify)` 覆盖 `edit_source` 编辑 | 已修（第 2 轮，新增 `refresh`） |
| **V1** | **路径穿越 → 工作区外任意文件删除**（S1 引入的安全回归） | 已修（第 2 轮） |
| **P3/H3a** | 无法注册工作区新建模块 / 校验读 repo 版导致死锁 | 已修（第 2 轮） |
| **S2** | `unregister` 后 `register` 被拒 | **正确行为，不修**（见 §5.2） |
| **A2** | `register` 的本地守卫静默丢弃注册 | 遗留（§6.1） |
| **H1** | 跨注册表重复：工具报成功、提交才被拒 | 遗留（§6.2） |
| **H2** | 重建条目丢 `description`/`params_schema` | 遗留（§6.3） |
| **H7** | 深删除打断 import 方，编译门不覆盖 | 遗留（§6.5） |
| **H6** | 默认注册表按角色而非模块前缀 | 遗留（§6.6） |

---

## 1. 写通道模型（理解全部问题的前提）

```
request_source_access ──grant──▶ <workspace>/src 副本 ──edit_source/工具──▶ 改动
                                                          │
                            build_patch_from_workspace ◀──┘
                                        │  只遍历 broker.granted_paths
                                        ▼
                            apply_code_patch：allowlist + compile + registry 差分门
```

三条不变量，全部问题的根因都在这里：

1. **补丁可见性由 grant 决定**：`build_patch_from_workspace` 只遍历 `granted_paths`（`gan/patch.py:62`）。一个工作区文件若不在任何已 grant 路径之下，**永远进不了补丁**——即使它确实存在、确实被改过。
2. **注册表条目是数据，不是代码**：`module` 字段由注册表内容决定。自 S1 起，扫描源可以是**agent 用 `edit_source` 改写过的工作区注册表**，因此该字段**不可信**。
3. **提交门是差分的**：`code_repo._registry_worsened` 只拦"新增的"坏状态（新增 invalid / duplicate / orphan / unparseable），既有脏状态不阻断。

---

## 2. 第 1 轮：R1 + P1（`e87fedc`）

### 2.1 问题定位：三个成因 + 一个漂移点

#### C1 补丁闸门只看 records，而两个工具不记录

三个 `_build_patch` 均以 records 为判据，`register_component` / `unregister_component` 只调 `broker.grant`、不写 design/plan context：`planner.plan`、`planner.self_improve`、`evaluator.self_improve`。

**实证**：工具返回成功、workspace 已产生 234 字节注册表 diff，但 `dctx.records == []` → 闸门返回空 → 补丁为空 → `rejected=None`、无事件、无 hint → **注册被静默丢弃**。

#### C2 注册表末尾换行丢失（P1）

4 个注册表均以 `\n` 结尾；两个工具用 `json.dump(...)` 重写 workspace 副本，不带末尾换行；`difflib.unified_diff` 无法表达"文件末尾无换行"。

**实证**：`register_component` 产出 234B 补丁 → `apply_patch_detail` 返回 `(False, 'error: corrupt patch at line 14')`；多 chunk 场景返回 `patch fragment without header`。即 **deep 注册/注销的写接口整体失效**。

#### C3 evaluate 会话没有补丁通道

`evaluator.evaluate()` 只设 `EvalContext` + `AccessContext`，**不设 `DesignContext`、也没有任何补丁构建路径**；而 `deep/` 是 always-on（evaluator 会话可见这三个 deep 工具）。设计算子在该会话本就返回 `Error: no design context`，两个 deep 工具却漏了这一层。

#### C4 第四个漂移点：`_modify_depth`

`gan/framework/task_runner.py:_modify_depth` 用同一套 op 列表。若只给工具加 record 而不改这里，register/unregister 会被记为 `modify_depth=1`（operator），而它们实际是 code 级（`store.py`：0=config / 1=operator / 2=code）。

### 2.2 决策记录

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 换行修复层次 | **工具层 + 统一写入 helper**（`write_registry_json`）；不在本次实现 `patch.py` 的 `\ No newline` marker 支持 |
| D2 | evaluate 会话语义 | **禁止调用（运行时守卫）+ 提示词声明**；文案直接说"**当前不可用**"，不写"仅 self-improve 可用" |
| D3 | 防御层 | **实现** `deep_edit_dropped` 事件 |
| D4 | 判据形式 | **混合：从 `gan/tools/design/` 动态派生浅层集合 + 常量兜底**；未知 op 一律默认"深写" |

D2 的备选（"分割装配权限"：evaluate 不装配 register/unregister）被否，理由：需改 assembly / base_role / loop / 目录布局 / B7 receipt，且要精细保留 `request_source_access(view)`；而"禁止调用"与既有设计算子的守卫完全同构，改动最小。

### 2.3 实现

- **`gan/patch.py`**：`_SHALLOW_DESIGN_OPS` 从 `gan/tools/design/*.py` 的 stem 动态派生（`:119-127`），失败回退 `_FALLBACK_SHALLOW`（`:113`）。`has_deep_write(records)`（`:130`）：浅层 design 算子不触发；`request_source_access` 仅 `intent == "modify"` 计深写；**其余任何 op** 计深写（未知 op 默认深写 = fail-safe）。
- **`gan/registries/loader.py`**：`write_registry_json(path, data)`（`:70`）先读原文件判断是否以 `\n` 结尾，`json.dump(..., indent=2)` 后按原约定补 `\n`。
- **两个 deep 工具**：无 `DesignContext` → 终结性错误（"currently unavailable … Do not retry"）；写入走 `write_registry_json`；`dctx.record(...)` 只带结构化字段（无自由文本），进入 receipt / diff_summary 不构成泄漏。
- **四个消费点统一**：`planner.py`（plan + self_improve）、`evaluator.py`（self_improve）、`task_runner.py:_modify_depth`（`:28-32`）全部改用 `has_deep_write`。
- **防御层**：`gan/roles/base_role.py:15` `warn_dropped_workspace_edits(...)`——会话结束若 `patch_str` 为空、**无深写记录**、但工作区仍有差异 → `soft_fail(..., event_type="deep_edit_dropped")`。接入 `planner.py:163/240`、`evaluator.py:179`；**不接入 `evaluate()`**（与 self_improve 共用同一 workspace key，误报）。
- **evaluate 提示词**：blind 与 reveal 指令各加一句 "deep registry changes … are currently unavailable in this session"。

### 2.4 验证（第 1 轮）

| # | 断言 | 结果 |
|---|---|---|
| V1 | `has_deep_write` 8 例（浅层 / view / modify / 新工具 / 未来新工具） | 全部符合预期 |
| V2 | `_modify_depth(register)=2`、`_modify_depth(set_prompt)=1` | 通过 |
| V3 | register-only 真实工具 E2E：232B → `apply_code_patch` 成功 | 通过 |
| V4 | unregister-only 真实工具 E2E：739B（注册表 + 模块同时删除）→ 成功 | 通过 |
| V5 | `planner.plan` 角色级 E2E：records 含 `register_component`、232B、无 rejection | 通过 |
| V6 | `evaluator.self_improve` 角色级 E2E：2064B、无 rejection | 通过 |
| V7 | 空会话（无深写）：patch 为空、无 rejection、无事件 | 通过 |
| V8 | evaluate 形态（新进程，无 `DesignContext`）：两个工具均返回 "currently unavailable" | 通过 |
| V9 | 防御层：有 workspace diff、无深写记录 → 写 `deep_edit_dropped`；对照组不写 | 通过 |

---

## 3. 第 2 轮：R2 + S1 + P4 + V1 + P3/H3a

本轮全部围绕**"工作区覆盖"**：工作区副本到底该被当作"缓存"还是"本会话的真相来源"。

### 3.1 新增底座：`grant(if_absent=...)`（`gan/framework/access.py`）

| 位置 | 改动 |
|---|---|
| `_grant_concrete(..., if_absent, skipped)`（`:125-162`） | 在 `_within_caps` 之后加：`if if_absent and os.path.exists(d): granted.append(rel); skipped.append(rel); return` |
| `grant(..., if_absent=False)`（`:172-196`） | 透传 `if_absent`；累计 `skipped`；审计记录加 `"skipped"`；`last_result` 加 `skipped` |

**语义**：`if_absent=True` 时，工作区已有副本**绝不覆盖**；该路径**仍计入 `granted` / `granted_paths`**（补丁构建器继续跟踪它），并出现在 `skipped` / 审计 / `last_result` 里。默认 `False`，向后兼容。

这是 P4 与 R2 的共同底座——原先只有 `register_component` 用 `if not os.path.isfile(ws_reg)` 这种**本地守卫**模拟同样效果，而本地守卫与 `if_absent` 并**不等价**（见 §6.1 A2）。

### 3.2 R2：同会话多次 `unregister_component` 只有最后一次生效

**根因**：`unregister_component` 无条件 `broker.grant([rel], ...)`，把 repo 注册表**拷回覆盖**工作区副本，于是前一次 `unregister` 的删除被还原，而模块文件早已删除 → 补丁"条目复活 + 文件缺失" → 被注册表门拒绝。

**实证（修复前）**：`unregister(editor)` → `unregister(bash)` 后 workspace `shared.json` 里 `editor` 复活 → `PatchRejected: new invalid component(s): shared.json:skill:editor (module file not found: shared/skills/editor.py)`。

**修法**：两个 grant 都改 `if_absent=True`（`unregister_component.py:99,102`）。模块即使不在工作区也照样 grant，使"删除"对补丁构建器保持可见。

**实证（修复后）**：workspace `shared=[]`，两个模块都消失，1110B 补丁，提交成功，`registry_report` 干净。

### 3.3 S1：扫描 repo 注册表 → 同会话 `register` 后 `unregister` 找不到

**根因**：扫描源是 `code_root` 的注册表，看不到本会话刚写进工作区的条目（`register(probe8)` 后 `unregister(probe8)` 报 `Error: component not found: skill probe8`）。

**修法**（`unregister_component.py:57-78`）：工作区副本优先（`in_ws`），无副本时回退 repo 副本；**工作区注册表不可解析时是硬错误**（`Error: workspace registry is not parseable`），不退化成 "component not found"——那是本会话自己写坏的文件。

**实证（修复后）**：`register(probe8)` → `unregister(probe8)` 正确命中，231B 补丁 = 仅删除 `probe8.py`，提交成功，`registry_report: none`。

### 3.4 V1：S1 引入的路径穿越 → 工作区外任意文件删除（安全回归）

**根因链**：S1 让扫描源变成 **agent 可改写的工作区注册表** ⇒ 条目里的 `module` 成为不可信输入；而 `unregister_component` 直接 `ws_mod = os.path.join(src, module_rel)` + `os.remove(ws_mod)`。前置的 `frozen.is_allowed` 拦不住，因为它按**前缀 glob** 匹配（`fnmatch` 的 `*` 跨 `/`）且**不做 `..` 归一化**：

```
'gan/components/task/skills/../../../..../code/VICTIM.txt'  → is_allowed(...,'modify') = True
```

**实证（修复前）**：payload 写进工作区 `shared.json` → `unregister(kind=skill,name=v)` → 返回成功，且 `/tmp/.../code/VICTIM_DO_NOT_DELETE.txt` **被删除**。

**修法（三层）**：

| 层 | 位置 | 改动 |
|---|---|---|
| 根因（一处封住全部消费者） | `gan/framework/frozen.py:is_allowed` | 拒绝绝对路径与任何 `..` 分量。调用方审计：7 处调用无一处传 `..`/绝对路径 |
| 纵深防御 | `unregister_component.py:88` | 条目 `module` 必须 `.py` 且无 `..`，否则 `Error: unsafe module path in registry entry` |
| 纵深防御 | `unregister_component.py:106-110` | `realpath` 确认落点在 `src/` 内（覆盖符号链接等手法） |
| 纵深防御 | `register_component.py:88-89` | 调用方传入的 `module` 同样拒绝 `..` |

**实证（修复后）**：`is_allowed` 8 例单测全对；穿越 payload 被拒且文件保留。

### 3.5 P3/H3a：新组件注册链路（存在性 + 校验都读工作区副本）

**两个症状，同一根因**——`register_component` 只看 `code_root`：

- **P3**：`edit_source` 新建的模块（只存在于工作区）→ `Error: module not found`。由于提交门同时把"未注册的组件文件"判为 orphan，**新增组件端到端不可能**。
- **H3a**：repo 里已有的模块（无工具 API），agent 在工作区补上 API 后 register → `Error: invalid entry (module does not expose tool_info/tool_function)`；但**不注册直接提交**又被判 orphan → **双向死锁**。

**修法**（`register_component.py`）：

| 步骤 | 改动 |
|---|---|
| 2) 存在性（`:120-133`） | 工作区副本 **或** repo 副本存在即可 |
| 2b) 新增覆盖检查（`:31-45,129`） | 仅"工作区新文件"需要：必须落在某个已 grant 路径之下，否则补丁带不上它 → 提前给出可执行提示 `request_source_access(paths=['gan/components/task/skills/**'], intent='modify')` |
| 4) 校验（`:151-153`） | 有工作区副本就校验**工作区版**（那才是补丁要提交的版本），否则回退 repo 版；提交时的 `entry_reason` 仍是兜底 |

**边界（与 §5.2 一致）**：这不违反"register 不恢复源码"——`register` 从不复制/创建/删除任何文件，它只为**已存在**的源码登记条目。

**实证**：P3 → 754B（新文件 + 注册表条目）→ 提交成功；H3a → 534B → 提交成功（死锁解除）；未覆盖的新文件 → 提前报错而非延迟被拒。

---

## 4. 验证汇总（第 2 轮，全部真实链路）

| # | 场景 | 结果 |
|---|---|---|
| E1 | 同会话两次 `unregister`（同注册表） | `shared=[]`，1110B，提交成功，`registry_report` 干净 |
| E2 | `register` → 同会话 `unregister` | 231B = 仅删模块，提交成功 |
| E3 | 重复 `unregister` 同一组件 | 第二次 `component not found`（幂等语义正确） |
| E4 | 重复 `request_source_access(modify)` | 编辑保留；返回 "kept as-is (not overwritten)" |
| E5 | `request_source_access(refresh=true)` | 丢弃本地编辑（逃生门生效） |
| E6 | **S2**：`unregister` → `register` | 仍被拒（`new invalid component(s): task.json:skill:editor`）——**期望行为** |
| E7 | **P3**：新建模块 → `register` | 754B（新文件 + 条目），提交成功 |
| E8 | **H3a**：工作区补 API → `register` | 534B，提交成功 |
| E9 | 新文件未被 grant 覆盖 | 提前报错并给出补救命令 |
| E10 | **V1** 穿越 payload | `unsafe module path` 拒绝，工作区外文件保留 |
| E11 | `is_allowed` 8 例（含 `..` / 绝对路径 / 正常路径） | 全部符合预期 |
| E12 | 回归：单次 `unregister` 739B / 单次 `register` 232B | 与第 1 轮一致 |
| E13 | glob grant（`gan/components/task/**`）与 `list_editable` | 正常（加固未误伤） |
| E14 | `scripts/local/test_frozen.py` / `test_kinship.py` / `test_domain_entry.py` | 全绿 |
| E15 | `preflight_tools` 三角色 | 0 problems / 0 collisions |
| E16 | `compileall` | 通过 |

---

## 5. 行为变化与边界

### 5.1 第 2 轮引入的行为变化

1. **`request_source_access` 默认不再覆盖工作区副本**（P4）。需要刷新为 repo 版时显式传 `refresh=true`。框架驱动的刷新**不受影响**：每个 outer 开头的 `_resync_workspace` 自己用 `shutil.copy2`，**不经过 `grant`**。
2. **`is_allowed` 拒绝 `..` 与绝对路径**（V1 根因修复）。全仓 7 处调用方均不传此类路径，无行为变化。
3. **`unregister_component` 的扫描源改为工作区优先**（S1），且工作区注册表损坏时报硬错误。
4. **`register_component` 接受"只存在于工作区"的模块**（P3），但要求其目录已被 grant。
5. **两个 deep 工具拒绝不安全的 `module` 路径**。

### 5.2 S2 是**正确的拒绝**，不是缺陷

`register_component` 只负责"为已存在的源码登记条目"，**不负责恢复源码**。因此 `unregister`（删条目 + 删文件）之后同会话再 `register`，得到的补丁是"条目 + 文件"的矛盾组合，被注册表门拒绝是**正确且必要**的：

```
973B → REJECTED: new invalid component(s): task.json:skill:editor (module file not found)
```

**补救路径是显式的**：`unregister` → `request_source_access(refresh=true)`（语义就是"我要还原源码"）→ `register(registry=...)`。已实测可提交（但仍有 §6.3 的元数据残差）。因此"移动注册表"= 三步显式操作，而不是让 `register` 偷偷把文件带回来。

### 5.3 既有边界

- `register_component` / `unregister_component` **要求 design context**（plan / self_improve 有；evaluate 无 → 终结性拒绝）。
- `request_source_access(intent=view)` + `edit_source` 仍不构建补丁（保持"view = 只读"语义），但会触发 `deep_edit_dropped` 告警。
- 未采用"工作区有 diff 就构建"的闸门：plan 与 self_improve 共用同一 `akey` workspace，被拒补丁的残留编辑会在后续会话被反复重放；records 闸门是有意的会话边界。
- `edit_source` 只做工作区**根目录** realpath 约束，**不要求路径已被 grant**。这不是漏洞（未 grant 的文件进不了补丁），但它制造了 §6.1 的 A2 状态。

---

## 6. 未做 / 遗留（按优先级）

### 6.1 A2：`register_component` 的本地守卫静默丢弃注册 —— **必修**

**机制**：补丁只遍历 `granted_paths`（`gan/patch.py:62`），而 `edit_source` 不要求 grant（§5.3）⇒ 工作区里可以存在"**存在但未 grant**"的文件。`register_component.py:166-167` 的本地守卫：

```python
if not os.path.isfile(ws_reg):      # "文件存在" 被当成 "文件可入补丁"
    broker.grant(...)               # 于是跳过 grant → 该路径永不进 granted_paths
```

**实证（可达路径）**：grant `shared.json`（"先看目录"，顺带让 `gan/registries/` 存在）→ `edit_source create task.json` → `register(registry="task.json")`：

```
register -> Registered skill 'probe8' ...   <-- 工具报成功
granted_paths after: [... 完全不变 ...]      <-- task.json 未纳入
patch 0B                                    <-- 注册被静默丢弃
```

**兜底为何失效**：`base_role.py:26` 的 `deep_edit_dropped` 只在 `has_deep_write(records)` **为假**时告警——而 `register_component` 恰恰记录了 op ⇒ 直接 return ⇒ **完全无声**。这是 R1 的同一缺陷类，入口从"工具忘记记录"换成"记录了但路径没进 `granted_paths`"。

**修法（1 行，已验证）**：守卫下沉为无条件 `broker.grant(..., if_absent=True)`。实测 `skipped=['gan/registries/task.json']` → `granted_paths` 增加该路径 → 补丁 232B，注册落地。附带收益：与 `unregister_component` 统一到同一原语。

**归属**：既有缺陷（本地守卫从一开始就在），**非本会话引入**。

### 6.2 H1：跨注册表重复只被提交门拦下

`register_component` 只查**目标注册表**内是否已存在同名同 kind，不查其它注册表。实测：`register(editor, registry="task.json")`（`editor` 已在 `shared.json`）→ 工具返回**成功**，提交时才 `REJECTED: new duplicate component(s) (merged registry): task:skill:editor`。

**修法**：追加前先查其它注册表（工作区优先），命中即早失败并说明"一个组件只能声明在一个注册表；要迁移请先 `unregister` 再 `request_source_access(refresh=true)` 再 `register`"。

### 6.3 H2：重建条目丢元数据

`register_component` 合成条目为 `{name, kind, module}`，丢掉原条目的 `description` / `params_schema`。实测：§5.2 的**正确补救路径**仍有 **350B 残差**（纯元数据丢失）。

**修法**：追加前在 repo 注册表里找同名同 kind 的原条目，命中则复用其字段、只覆盖 `module`。不涉及任何源码恢复。

### 6.4 原子写：`write_registry_json` 非原子

当前是 `open(path,"w")` + `json.dump` + 补换行。仓库已有统一约定（`checkpoint.py:50-53`、`trajectory.py:116/224`、`design/store.py:33-39` 均为 tmp + `os.replace`；`trajectory.py:78` 明确写了理由），`write_registry_json` 是唯一例外。

**失败模式**：写入中途进程被杀 / 磁盘满 → 工作区注册表被**截断**。后果被 S1 放大：工作区注册表现在是 `unregister` 的权威扫描源 → 之后任何 unregister 都返回 `Error: workspace registry is not parseable`，**会话内不可恢复**；同时补丁会带上损坏的注册表 → 整批提交被拒（`registry became unparseable`），**连累同会话其它正当编辑**。

**边界**：同角色会话串行、角色间工作区隔离 ⇒ 没有并发读者，买到的是**崩溃安全**而非并发安全。4 行、零行为变更。

### 6.5 H7：深删除会打断 import 方，而编译门不覆盖

`validate_python(code_root, rel_files)`（`code_repo.py:316-325`）只编译**变更**的 `.py`。实测：组件 A `import` 组件 B，`unregister(B)` → 提交**成功**（`registry_report: none`），但 `import A` → `ModuleNotFoundError`。

**修法（两选一）**：(a) 补丁触碰 `gan/registries/**` 时把编译范围扩到整个 `gan/components/**`（能捕获 import 断裂，代价是编译量上升）；(b) 记为已知限制。当前仓库无组件互相 import，故无实例，但布局明确允许。

### 6.6 H6：默认注册表按角色而非模块前缀

省略 `registry` 时按 `_OWN_REGISTRY`（planner → `task.json`，evaluator → `evaluator.json`）选择，与模块所在前缀无关。因此 `shared/**` 模块不显式传 `registry` 就会落进 `task.json`，**静默变成 task 专属**（planner/evaluator 之外的角色看不到）。修法：按模块前缀自动路由，显式跨前缀时放行但提示可见范围。属行为改进，可后置。

### 6.7 其它

1. **`patch.py` 的 `\ No newline at end of file` 支持**（D1 备选）：未实现。当前由 `write_registry_json` 在工具层保证；若未来有工具整体重写文件丢换行，同类 bug 会复发（防御层不会捕获——补丁能构建但 apply 失败，会走既有 rejection 通道）。
2. **`evaluate()` 的 `edit_source`**：仍无补丁通道且无守卫；其编辑可能被后续 self_improve 补丁带上，也可能丢失。
3. **文档同步**：`AGENTS.md` 与 `docs/7` §3 尚未记录"register/unregister 需要 plan/self-improve 补丁通道"这一新约束，以及 `request_source_access` 默认行为的变更。
4. **`docs/工具管理审查.md` 的过时结论**：该文档中"选择静默降级未修 / E3 checkout 吞异常"两处判断已过时——B7 装配报告与 E3 的 `RepoIntegrityError` fail-fast 在本仓库均已实现（本轮复核已实测确认）。此外该文档另有 4 处结论经复核不成立：B7 未实现（实为已实现）、E3 吞异常（实为 fail-fast）、`grep` 无输出上限（实为 `max_results=100`）、`select_component` docstring 被注入提示词（实为不注入）。

---

## 7. 变更清单

### 7.1 第 1 轮（`e87fedc`，8 个文件）

- `gan/patch.py` — `has_deep_write` + 浅层集合派生
- `gan/registries/loader.py` — `write_registry_json`
- `gan/tools/deep/register_component.py` — 换行 + record + 守卫
- `gan/tools/deep/unregister_component.py` — 换行 + record + 守卫
- `gan/roles/planner.py` — 两个闸门 + 防御层接入
- `gan/roles/evaluator.py` — self_improve 闸门 + 防御层 + evaluate 提示词
- `gan/roles/base_role.py` — `warn_dropped_workspace_edits`
- `gan/framework/task_runner.py` — `_modify_depth`

### 7.2 第 2 轮（本次提交，5 个文件）

| 文件 | 改动 |
|---|---|
| `gan/framework/access.py` | `grant(if_absent=...)` + `_grant_concrete` 跳过分支 + `skipped` 审计/`last_result`（P4/R2 底座） |
| `gan/framework/frozen.py` | `is_allowed` 拒绝绝对路径与 `..`（V1 根因） |
| `gan/tools/deep/unregister_component.py` | 工作区注册表优先扫描（S1）+ 两个 grant `if_absent=True`（R2）+ 不安全 `module` 拒绝与 realpath 落点确认（V1） |
| `gan/tools/deep/request_source_access.py` | 默认 `if_absent=not refresh` + 新增 `refresh` 参数 + 返回文案区分"新授权/按原样保留"（P4） |
| `gan/tools/deep/register_component.py` | `module` 穿越拒绝（V1）+ 存在性与校验读工作区副本（P3/H3a）+ 新文件 grant 覆盖检查 |

### 7.3 文档

- 本文件（会话唯一汇总）

### 7.4 关联

- `docs/工具管理审查.md` — 上一轮复核结论；其中 6 处判断已过时/不成立，见 §6.7.4。
- `docs/7_遗留问题与待办.md` §3 — 待同步。
