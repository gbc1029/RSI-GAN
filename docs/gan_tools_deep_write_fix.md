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
| 第 3 批（A 批） | `docs(gan/tools): correct stale review conclusions, sync records, document deep-write constraints` | A1–A13：被审查文档的过时结论更正（只增不删）+ 约束写入 `AGENTS.md`/seeds + 汇总文档自洽。A14–A16 未做，见 §6.1 |
| 第 4 批（读侧） | `fix(gan/tools): list_components workspace overlay, design-role catalog, unsafe registry module paths` | **B18 + H10 + H9**：读侧与两个 deep 工具口径统一（工作区优先、设计目标角色、增删/覆盖可见），注册表 `module` 路径净化。见 §8 |
| 第 5 批（plan→task 应用） | `fix(gan/framework): heal task design before persist; overlay-validated selection; set_config slot validation` | **B24 + B25 + P-3**：task 设计持久化前自愈（H11/B24 关闭）、`select_component`/`set_config` 槽位同权校验（B25/H12 关闭）、同会话选择（推翻第 4 批决策，见 §9.1）。见 §9 |
| 第 6 批（注册表统一） | `fix(gan/registries): per-role single-writer catalogs, role-directory binding, md knowledge base` | **A14 + B2 + H6/C6 关闭**：三注册表独立（无 shared）、name 主键、角色目录绑定、单一 `tools` 槽位（legacy 归一化）、kind 机械退役、md 知识库（pull 型 + p/e 同批启用）。见 §10 |

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
| **H8** | `register` 的本地守卫静默丢弃注册（**原编号 A2**） | 遗留（§6.2 B1） |
| **H1** | 跨注册表重复：工具报成功、提交才被拒 | 遗留（§6.2 B2） |
| **H2** | 重建条目丢 `description`/`params_schema` | 遗留（§6.2 B3） |
| **H7** | 深删除打断 import 方，编译门不覆盖 | 遗留（§6.3 C1） |
| **H6** | 默认注册表按角色而非模块前缀 | 遗留（§6.3 C6） |
| **H9** | 注册表 `module` 路径未净化：绝对路径/`..` 可把 `gan/components` 外的文件装配进工具集并 import | **已修（第 4 批）**，`docs/**` 记录见 §8 |
| **H10** | `list_components` 用访问角色而非设计目标角色 ⇒ planner 的 plan 会话列错目录（deep-add 闭环阻塞项） | 已修（第 4 批） |
| **H11** | 任务补丁被拒时设计仍被持久化 ⇒ 设计永久引用不存在的组件 | **已修（第 5 批，A2 自愈）**，见 §9 |
| **H12** | `set_config("skills", [...])` 只做 schema 键白名单 ⇒ 未注册组件不经拒绝直接进设计（=B25） | **已修（第 5 批）**，见 §9 |

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

这是 P4 与 R2 的共同底座——原先只有 `register_component` 用 `if not os.path.isfile(ws_reg)` 这种**本地守卫**模拟同样效果，而本地守卫与 `if_absent` 并**不等价**（见 §6.2 B1，原编号 H8）。

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
- `edit_source` 只做工作区**根目录** realpath 约束，**不要求路径已被 grant**。这不是漏洞（未 grant 的文件进不了补丁），但它制造了 §6.2 B1 的 H8 状态。

---

## 6. 未做 / 遗留（完整待办）

**分类口径**：**A 文档/注释**（零行为风险）· **B 小范围代码**（单文件、少行、低风险）· **C 大范围修改**（跨文件 / 改语义 / 需设计决策）。编号与 `docs/7` §3.4 的"仍未修"清单一致；标 **需决策** 的项须先定口径再动手。**本节是本会话结束时的完整待办快照**——A1–A13 与 **B18** 已在本会话完成（见 §7.3 / §8），A14 起、B1–B17、B19 起为剩余项。

### 6.1 A 类：文档或注释（剩余 2 项）

| 编号 | 项 | 位置 | 说明 |
|---|---|---|---|
| ~~**A14**~~ | ~~P5 措辞收敛~~ —— **✅ 第 6 批结构性消解**：shared.json 已删除，问题对象不复存在（见 §10） | — | — |
| **A15** | 防御层盲区记为已知限制 | 本文档 §5.3、`docs/7` §3.4 | `warn_dropped_workspace_edits`（`base_role.py:29`）用 `build_patch_from_workspace` 做检测 ⇒ 与补丁构建器**同一盲区**（只看 `granted_paths`），H8 类问题**构造性不可发现**；彻底修法见 C3 |
| **A16** | `loop.yaml` 自洽说明 | `gan/framework/loop.yaml` 文件头 | 头声明 "every key below MUST have a code consumer" 与 `cost.token_budget_per_gen` / `cost.record_usage`（保留待 G3）冲突；补"保留键"说明，或接线（见 B21） |

### 6.2 B 类：小范围代码（原 23 项 + 第 4 批 B24 + 第 5 批 B26；**B18、B24、B25(=H12) 已完成**，余 23 项）

| 编号 | 项 | 位置 | 规模 | 需决策 |
|---|---|---|---|---|
| **B1** | **H8**（原编号 A2）守卫下沉（静默丢弃，**最高优先**） | `register_component.py:166-167` | **1 行** | — |
| **B2** | ~~**H1** 跨注册表重复早检查~~ —— **✅ 第 6 批结构性消解**：单文件单写者 ⇒ 跨文件重复不可构造（见 §10） | ~~原位~~ | — | — |
| **B3** | **H2** 复用 repo 原条目字段（`description`/`params_schema`） | `register_component.py` 追加前 | ~8 行 | — |
| **B4** | **原子写** `write_registry_json` | `registries/loader.py:70` | ~5 行 | — |
| **B5** | ~~**H6** 默认注册表按模块前缀路由~~ —— **✅ 第 6 批结构性消解**：角色目录绑定（见 §10） | ~~原位~~ | — | — |
| **B6** | ~~报告 R4：`unregister` 对"多注册表声明同一组件"只删第一个~~ —— **✅ 第 6 批结构性消解**：每组件只可能声明在一个文件（见 §10） | ~~原位~~ | — | — |
| **B7** | 报告 R5：always-on 工具（`gan/tools/**`）不参与 V3/AST 校验 | `preflight.py` | ~15 行 | — |
| **B8** | 报告 R6：`load_tools` 每次把 `tools_dir` 插 `sys.path`（跨 outer 线性增长） | `agent/tools/__init__.py:27-28` | ~4 行 | — |
| **B9** | 报告 R7：`build_diff_summary.files` 混用完整路径与 basename | `summary.py:37-42` | ~3 行 | — |
| **B10** | 报告 R8：`AccessBroker.last_result` 未在 `__init__` 初始化（**本会话起有真实读者**） | `access.py` | ~2 行 | — |
| **B11** | N3-A/C：`meta_view` 未排除 `records` | `loop.py:834` | ~2 行 | — |
| **B12** | N3-B/N4：`receipt.grants` 剥 `reason`，与 `_ops_summary` 白名单策略统一 | `receipt.py:94` | ~5 行 | — |
| **B13** | N3-D：`evaluator_reward.py` 的 `said: {fb}` 不入 digest | `reward/evaluator_reward.py:199` | ~3 行 | — |
| **B14** | 报告 R4：`summary.py` 删死分支 + "未知 op 透传 + 安全字段白名单"（含 `planner.py` 的 `code_edit` 死分支） | `summary.py:24-40` | ~25 行 | — |
| **B15** | 选择静默降级**残余**：`preflight_tools` 校验"设计选中项是否仍存在/valid" | `preflight.py` | ~20 行 | — |
| **B16** | T1/R3：注册期碰撞预检（复用 `validate_registry`/`assemble_collisions`） | `register_component.py` | ~15 行 | — |
| **B17** | R3：把 `collisions` 纳入提交门控（`registry_report` + `_registry_worsened` 差分） | `code_repo.py` | ~15 行 | — |
| **B18** | P3 残留：`list_components` 只扫 `broker.repo_root`，工作区新文件不可见 —— **✅ 已修（第 4 批，Tier 2）**，见 §8.5 | `list_components.py:63,69-70` | ~15 行 | — |
| **B19** | `list_dir` 加输出上限（与 `grep` 的 `max_results=100` 对齐） | `list_dir.py` | ~5 行 | — |
| **B20** | `deny_deep` 接线（**须含 `register_component`**，见 A13） | `access.py` / `build.py` / 3 个 deep 工具 | 跨 4 文件 | — |
| **B21** | 预算/死键：`task_agent.py` 未传 `max_tool_calls`（默认 40）；`cost.*` 接线或移除 | `task_agent.py` + `loop.yaml` | ~5 行 | **是** |
| **B22** | `patch.py` 支持 `\ No newline at end of file`（D1 备选） | `patch.py` | ~15 行 | 可选 |
| **B23** | 回归断言固化（报告 §六 的 6 条真实链路 e2e） | 测试代码 | 6 例 | — |
| **B24** | **H11**（第 4 批新发现）任务补丁被拒时设计仍被持久化 ⇒ 设计永久引用不存在的组件（`task_runner` 中 `apply_task_patch(...)` 之后**无条件** `persist_design(...)`；下一代装配时被 B7 记 skip，静默失能且留痕） —— **✅ 已修（第 5 批，A2 自愈）**，见 §9.3 | `gan/framework/task_runner.py:103-128` | ~15 行 | **是** |
| **B25** | **H12**（第 5 批新发现）`set_config("skills", [...])` 只做 schema 键白名单，未注册组件不经拒绝直接进设计（实证：装配报 `not registered`） —— **✅ 已修（第 5 批，Option 1）**，见 §9.4 | `gan/tools/design/set_config.py` | ~15 行 | **是** |
| **B26** | **角色设计的 heal**（第 5 批新发现）：`evaluator/planner.self_improve` 在补丁重试循环后**无条件** `save_self_config(cfg)`（`gan/roles/evaluator.py:194`），与 H11 同模式；角色设计现在 committed-only 故**不会新造**悬空，但存量链上的悬空名未自愈。修法 = 角色设计也开 overlay + 自愈（需决策） | `gan/roles/base_role.py:215` + 两个 `self_improve` | ~15 行 | **是** |

**B 类详细写法**（B1–B4 为本文档已完整分析的四项；B5–B26 见上表与 `docs/工具管理审查.md` 对应编号）

#### B1 — H8（原编号 A2）：`register_component` 的本地守卫静默丢弃注册 —— **必修**

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

#### B2 — H1：跨注册表重复只被提交门拦下

`register_component` 只查**目标注册表**内是否已存在同名同 kind，不查其它注册表。实测：`register(editor, registry="task.json")`（`editor` 已在 `shared.json`）→ 工具返回**成功**，提交时才 `REJECTED: new duplicate component(s) (merged registry): task:skill:editor`。

**修法**：追加前先查其它注册表（工作区优先），命中即早失败并说明"一个组件只能声明在一个注册表；要迁移请先 `unregister` 再 `request_source_access(refresh=true)` 再 `register`"。

#### B3 — H2：重建条目丢元数据

`register_component` 合成条目为 `{name, kind, module}`，丢掉原条目的 `description` / `params_schema`。实测：§5.2 的**正确补救路径**仍有 **350B 残差**（纯元数据丢失）。

**修法**：追加前在 repo 注册表里找同名同 kind 的原条目，命中则复用其字段、只覆盖 `module`。不涉及任何源码恢复。

#### B4 — 原子写：`write_registry_json` 非原子

当前是 `open(path,"w")` + `json.dump` + 补换行。仓库已有统一约定（`checkpoint.py:50-53`、`trajectory.py:116/224`、`design/store.py:33-39` 均为 tmp + `os.replace`；`trajectory.py:78` 明确写了理由），`write_registry_json` 是唯一例外。

**失败模式**：写入中途进程被杀 / 磁盘满 → 工作区注册表被**截断**。后果被 S1 放大：工作区注册表现在是 `unregister` 的权威扫描源 → 之后任何 unregister 都返回 `Error: workspace registry is not parseable`，**会话内不可恢复**；同时补丁会带上损坏的注册表 → 整批提交被拒（`registry became unparseable`），**连累同会话其它正当编辑**。

**边界**：同角色会话串行、角色间工作区隔离 ⇒ 没有并发读者，买到的是**崩溃安全**而非并发安全。4 行、零行为变更。

### 6.3 C 类：大范围修改（7 项）

| 编号 | 项 | 范围 | 需决策的点 |
|---|---|---|---|
| **C1** | **H7** 编译门覆盖 import 断裂 | `code_repo.validate_python` + `apply_code_patch` | 见下 |
| **C2** | `evaluate()` 的深改定位 | `roles/evaluator.py` + `edit_source.py` | 加守卫（小，~5 行）vs 补**补丁通道**（大，且与"评估者不改产物"的设计冲突） |
| **C3** | 防御层检测方法重构（A15 的彻底版） | `roles/base_role.py` + 新检测逻辑 | 见下 |
| **C4** | 补丁闸门判据重设计 | `patch.py` + `planner.py`/`evaluator.py` 的 `_build_patch` 闸门 | 报告 P0 建议改为"workspace 相对 `code_root` 确有差异"；本会话实际选了 **records 判据**（`has_deep_write`）+ 防御层——**H8 与 C3 都源于这个差异** |
| **C5** | P2 的根本面：补丁只遍历 `granted_paths` | `patch.py` + 授权模型 | "未 grant 的新文件"是否升为一等公民（改枚举方式 or 授权模型改目录级）；`list_editable` 只返回具体文件是加重因素 |
| **C6** | 注册表作用域模型（H6 彻底版） | `registries/loader.py:entry_reason` + `code_repo._registry_worsened` + `schema.py` | 见下 |
| **C7** | T1 的**命名空间前缀**方案 | `assembly.py` | 若不走 B16/B17 的检测路线则须做它 |

#### C1 — H7：深删除会打断 import 方，而编译门不覆盖

`validate_python(code_root, rel_files)`（`code_repo.py:316-325`）只编译**变更**的 `.py`。实测：组件 A `import` 组件 B，`unregister(B)` → 提交**成功**（`registry_report: none`），但 `import A` → `ModuleNotFoundError`。

**修法（两选一）**：(a) 补丁触碰 `gan/registries/**` 时把编译范围扩到整个 `gan/components/**`（能捕获 import 断裂，代价是编译量上升）；(b) 记为已知限制。当前仓库无组件互相 import，故无实例，但布局明确允许。

#### C3 — 防御层检测方法重构（A15 的彻底版）

现检测（`base_role.py:25-31`）在 `has_deep_write(records)` 为假时调用 `build_patch_from_workspace` 比对工作区与 `code_root`。**问题**：`build_patch_from_workspace` 只遍历 `granted_paths`，因此"工作区存在但未 grant 的文件"对它**不可见**——检测器与它要监视的补丁构建器共享同一盲区，H8 类问题在原理上无法被发现（§6.2 B1 的 `deep_edit_dropped` 完全无声即由此而来）。

**修法方向**：改为"工作区树 vs granted 覆盖"的**独立**差分（不经过 `patch.py`），并对"工作区存在但既未 grant、也未被任何 record 提及"的文件报事件/回执。**需决策**：`edit_source` 合法地产生这类文件（§5.3 的边界），所以必须先定义"哪些未 grant 的工作区文件算异常"，否则会把正常编辑误报为丢弃。

#### C6 — H6：默认注册表按角色而非模块前缀

省略 `registry` 时按 `_OWN_REGISTRY`（planner → `task.json`，evaluator → `evaluator.json`）选择，与模块所在前缀无关。因此 `shared/**` 模块不显式传 `registry` 就会落进 `task.json`，**静默变成 task 专属**（planner/evaluator 之外的角色看不到）。修法：按模块前缀自动路由，显式跨前缀时放行但提示可见范围。属行为改进，可后置。

### 6.4 与 §6.2 / §6.3 的交叉引用（原"其它"项已并入分类表）

- `patch.py` 的 `\ No newline at end of file` 支持 → **B22**（D1 备选；当前由 `write_registry_json` 在工具层保证。若未来有工具整体重写文件丢换行，同类 bug 会复发：补丁**能**构建但 apply 失败，走既有 rejection 通道）。
- `evaluate()` 的 `edit_source` → **C2**（仍无补丁通道且无守卫；其编辑可能被后续 self_improve 补丁带上，也可能丢失）。
- `docs/工具管理审查.md` 的 4 处过时结论 → **A1 已完成**（见 §7.3）。
- `AGENTS.md` / `docs/7` §3 的文档同步 → **A2 + A10 已完成**（见 §7.3）。

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

### 7.3 第 3 批（A1–A13：记录自洽 + 约束文档化）

> 编号说明：本节的 **A1–A16 是本轮"文档/注释改动"的任务编号**，与 `docs/7` §2 的 A1/A2（代际继承簇）、以及本文件的 **H 系列发现编号**（H1/H2/H6/H7/**H8**）互不相干。**H8 即早期草稿里误称为 "A2" 的"守卫下沉"缺陷**（改名原因：`docs/7` 自身已用 A1/A2 表示代际继承簇，同名会误导）。

**A1–A7**（更正被审查文档；`docs/**` 一律**只增不删**——原文保留 + 就地 `【复核更正·本会话】` 标注）

| 项 | 文件 | 内容 |
|---|---|---|
| A1 | `docs/工具管理审查.md` | 顶部「本会话复核状态」块 + 4 处过时结论就地更正（B7 已实现 / E3 已 fail-fast / docstring 不注入 / `grep` 有上限） |
| A2 | `docs/7_遗留问题与待办.md` | 新增 **§3.4 复核与本会话修复状态**（已闭环 8 项 + 新登记 7 项 + 仍未修清单） |
| A3 | 同上 §3.1 T1 | 触发例更正：task skills 装配到独立目录，不覆盖 `work/common`；真实碰撞面 = `shared`/`<role>` 注册组件 vs planner/evaluator 的 always-on |
| A4 | `docs/gan_tools_components_structure.md` | S8 诊断更正：plan 阶段 `select_component` **可用**（design context 是 task 的），不可用的是 self_improve |
| A5 | `docs/gan_tools_ops_impl.md` | §5.4 更正：`list_components` **会**返回 `description` |
| A6 | `docs/gan_tools_ops_review.md` | §6 更正：`grep` **有** `max_results=100`；只有 `list_dir` 无 cap |
| A7 | `gan/tools/design/select_component.py` | docstring 过期注册表路径 **彻底替换**（`docs/` 之外，不适用"只增不删"） |

**A8–A10**（约束写入契约与提示词；`docs/` 之外一律**彻底改**）

| 项 | 文件 | 内容 |
|---|---|---|
| A8 | `AGENTS.md`、`seeds/planner.md`、`seeds/evaluator.md` | 新约定「算子必须结构化自记录」（`ctx.record(op, **结构化字段)`，禁自由文本 rationale；`records` 同时是补丁闸门与评估者可见摘要的唯一来源） |
| A9 | `seeds/planner.md` | "深层删"句点名 `unregister_component(name, kind, registry=...)` |
| A10 | `AGENTS.md` | 补丁通道约束（`evaluate` 无补丁构建器 ⇒ 深改被丢弃）、`register_component` 语义（**只注册已存在的源码**，不创建/恢复/删除）、`refresh` 语义（重复授权不重拷）、`granted_paths` 可见性（捕获新文件须授权父目录） |

**A11–A13**（汇总文档自洽 + 状态指针）

| 项 | 文件 | 内容 |
|---|---|---|
| A11 | 本文件 | 计数矛盾改正（**4 处**，非 6 处）。本文件是会话汇总而非被审查对象，故此处为**净替换** |
| A12 | 4 份 `gan_tools_*.md` | 顶部状态指针（历史记录 / 行号已漂移 / 权威汇总指向本文件）+ `assembly.py`（`:111,114`→`:149`/`:163`）、`evaluator_reward.py`（→ `gan/framework/reward/`）、`loop.py`（`:762-763`→`:834`）漂移对照 |
| A13 | `docs/6_v5改动.md` §八、`docs/7` §7 | `deny_deep` 冻结面应为**三个** deep 工具（漏了 `register_component`）；`last_result` 已有真实读者，`__init__` 初始化已是**前置条件** |

**未做**：A14（P5 措辞收敛）、A15（防御层盲区记为已知限制）、A16（`loop.yaml` 自洽说明）——见 §6.1。

### 7.4 关联

- `docs/工具管理审查.md` — 上一轮复核结论；其中 **4 处**判断已过时/不成立（A1 已就地标注），见 §7.3。
- `docs/7_遗留问题与待办.md` §3 — **已同步**（A2/A3：新增 §3.4 修复状态表 + T1 触发例更正；A13：§7 deny_deep 冻结面更正）。

---

## 8. 第 4 批：读侧口径统一（B18 + H10 + H9）

> 起因：讨论 B18（`list_components` 只扫 `broker.repo_root`）。用户给出四个约束——"只列工作区，还是工作区与 code-root 分开列"、"过程中是否申请权限并将 tools 复制到工作区"、"是否增删了工具"、"以注册表为准还是以存在的代码为准"。讨论中**实测出两个比 B18 更重的问题**（H10、H9）。用户决策：**Tier 2 输出形态 + `select_component` 保持"只认已提交" + 同批修 B18/H10/H9，H11 记入待办**。

### 8.1 决策记录（用户确认）

| 决策点 | 结论 | 理由 |
|---|---|---|
| 输出形态 | **Tier 2**：单一合并视图（并集）+ 每项 `origin`/`pending`/`selectable`/`note` + 顶层 `patch_channel` | agent 的三个动作都是"对一个名字做一件事"，两份平行清单把 diff 推给 LLM；只列工作区则错误（工作区是**部分副本**、outer 起点为空、evaluate 会话改动不落地） |
| `select_component` 的权威 | **保持"只认已提交"**（读进程内 `loader.REGISTRY_DIR` = worker 模式下的 code_root）；`selectable` 只是对它的忠实镜像 | 与"自改在下一个 outer 生效"一致；若改成工作区感知，**必须先修 H11**，否则补丁被拒时设计会永久引用不存在的组件 |
| `selectable` 的定义 | **必须严格等于 `select_component` 的实际行为**（提交规则则存在性 + valid） | 避免再造一处"lister 与 selector 不一致"——那正是 H10 的类型 |

**问题 4 的答案（以注册表为准还是以代码为准）**：分层，且两个方向都要——注册表 = 命名空间（谁能被 select）；代码存在性 + 身份契约 = 有效性（`entry_reason`）；代码扫描 = 未注册候选（`orphan_modules`）。B18 要改的是"扫哪些根 + 标出差异"，不是换权威。

### 8.2 三个根 — 事实对齐

| 名称 | 实际指向 | 生命周期 |
|---|---|---|
| `broker.repo_root`（`gan/build.py` 传入 `code_root or repo_root`） | **code_root**：per-run git 树，**已提交基线**（名字有误导性，它不是原仓库） | 整个 run |
| 工作区 `broker.src_dir(role,node)` = `workspaces/<role>/outer_<O>/src` | 被 grant 过的路径的**部分副本**；同 outer 的 plan/evaluate/self_improve **共享** | **每个 outer 开始清空** |
| 原仓库 `repo_root` | 只给父 harness 读 benchmark 标签 | 不变 |

由此两点：`list_components` 原来并**没有读错树**（它读的是已提交基线，只缺工作区一层）；来源标注应是 **workspace (this outer)**，不能写成 "this session"（工作区跨会话共享，plan 会看到 evaluate 阶段 grant 进来的东西）。

### 8.3 H10（最严重）：catalog 用了访问角色而非设计目标角色

`list_components` 旧代码用 `actx.role` 取注册表，而 `select_component` 用 `dctx.role`。planner 的 plan 会话是 `DesignContext(role="task")`（`gan/roles/planner.py`）⇒ 列的是 `shared.json + planner.json`，可能选择的却是 `shared.json + task.json`。

**实证**（模拟 worker 模式，`task.json` 含组件 `foo`）：

```
list_components(actx.role) -> registry role: planner
  registered: [] | unregistered: [] | problems: []
  'foo'（就在 task.json 里）对 planner 可见? False
select_component(dctx.role=task) -> skills = ['foo']      # 选择器接受了
```

`foo` 在**任何**清单里都不出现（`registered` 用错注册表；`unregistered` 也不报——孤儿检测是全注册表集合级）。这正是 `list_components` 存在的理由被废掉：planner 只能"猜名字"。当前被掩盖是因为 `task.json` 为空 ⇒ planner 视图 == task 视图；**deep-add 闭环一旦跑通（往 task.json 注册一条）立刻朝最坏方向发作**。

**修法**：catalog（registry/problems/valid/selectable）一律用 `dctx.role`（必须与 `select_component` 一致）；会话层（工作区 overlay、`granted_paths` 覆盖、patch 通道）用 `actx.role`/`actx.node_id`。输出新增 `design_role` / `access_role` 两个字段（`role` 保留为 `design_role` 的兼容别名）。

### 8.4 H9：注册表 `module` 路径未净化（安全）

`entry_reason` 原来只做 `components_dir / mod` 然后 `is_file()`，`module_path` 直接返回拼接结果——**不拒绝绝对路径与 `..`**（只有 `unregister_component` 的删除路径拒了，V1 修的）。实测：绝对路径条目被判 `entry_reason -> None`（合法），`module_path` 返回树外路径，`assemble_tools_dir_reported` 把该文件**装配进工具集**（`selected_assembled: ['evil.py']`，无 skip），随后 `load_tools` 会 import 并暴露其工具。身份契约的"父目录必须叫 `skills/`"只挡普通文件，挡不住 `/tmp/.../skills/x.py`。

**修法**：`gan/registries/loader.py` 新增 `safe_module_rel`（拒绝绝对路径、盘符、任何 `..` 段，规范化为相对路径），在 `entry_reason` / `resolve_module` / `module_path` / `declared_modules` 统一使用——注册表条目是 DATA（不变量 2），**读路径同样不可信**。装配端现在 skip 且带该 reason。落在 `loader.py`（不在任何角色写权限内，冻结面）是刻意的。

### 8.5 B18：工作区 overlay（与 deep 工具同一条解析规则）

- `loader.py` 公共入口新增 `overlay_root=...`（会话工作区根）：注册表文件与组件模块一律**工作区优先、已提交兜底**（与 `unregister_component` 扫描同规则）；`components_dir` 内部化为**搜索路径**（`_as_dirs`；单 `Path` 仍兼容 ⇒ 提交门/装配/`register_component` 的既有调用不变）。
- `resolve_registry_file` 公开：返回 `(path, origin)`，`origin ∈ {"workspace","code"}`；工作区注册表**不可解析时不静默回退**（`validate_registry` 的 `unparseable` 带 `origin`；`list_components` 另在 `notes` 里点明"它声明的条目不会列出"）。
- `orphan_modules` 扫两边的并集（声明集也取并集；`safe_module_rel` 丢弃不安全声明）。
- `unregistered[]` 每项加 `origin` + `patch_covered`（新谓词 **`AccessBroker.covers`**；`register_component._granted_coverage` 改为其别名 = 谓词的唯一实现；已提交树里的文件恒为 covered，因为无需补丁承载）。
- `registered[]` = 有效视图 + 提交基线**并集**，每项报 `pending: added|removed|modified|none`（集合差 + 条目字段对比 + `filecmp` 逐字节比模块内容）；`valid` 描述 **agent 将要动手的那一份**（工作区副本优先；`removed` 描述仍存在的提交副本）；`selectable` 严格镜像 `select_component`；`selectable_reason`/`note` 说明张力。
- 顶层 `patch_channel`（无 DesignContext = evaluate ⇒ 提示"deep 改动会被丢弃"）、`roots`、`registry_sources`、`notes`（注册表损坏 / 增删计数 / 未覆盖孤儿 / **selected 引用但注册表缺失**——H11 的可见化，不动 H11 本体）。

### 8.6 证据与验证（第 4 批）

| 验证 | 结果 |
|---|---|
| planner plan 会话（design=task, access=planner）→ `design_role: task`，`task.json` 条目**可见且 selectable=True** | ✅（修复前 `registered: []`，而同会话 `select_component` 却接受） |
| planner self_improve（design=planner）/ evaluator 会话 → 仍列各自注册表 | ✅ |
| 工作区 overlay：`beta` added（selectable=False + note）、`gamma` removed（selectable=True + 警告）、`alpha` modified（工作区副本有效） | ✅ |
| 覆盖：已授权目录下的新文件 `patch_covered=True`；授权外 `foo.py` False + 提示先授权父目录 | ✅ |
| 无 DesignContext（evaluate）→ `patch_channel: false` + 警告 | ✅ |
| H9：绝对路径 / `..` 的 `module` → `entry_reason` 报 unsafe；`module_path` None；**装配 skip 并报 reason** | ✅ |
| 深层工具回归 `/tmp/gan_verify/regress.py` 全部 11 例（含 `p3_uncovered`/`reg_traversal`，直接踩 `broker.covers` 新路） | ✅ 全 OK |
| H8/A2 探针（`ws_reg_ungranted` → 232B 补丁、注册入 patch） | ✅ |
| `preflight_tools`（真实仓库 3 角色）problems/collisions 全空 | ✅ 无回归 |
| 无 overlay 的既有调用（旧签名）行为不变 | ✅ |

### 8.7 变更清单（第 4 批）

| 文件 | 内容 |
|---|---|
| `gan/registries/loader.py` | `safe_module_rel` + `resolve_module` + `_as_dirs`（搜索路径）+ `overlay_root`（`load_registry_for_role`/`validate_registry`/`orphan_modules`/`declared_modules`）+ `resolve_registry_file`（provenance）；模块 docstring 增"注册表条目是 DATA"+"Overlay"两节 |
| `gan/tools/work/common/list_components.py` | **重写**：design_role/access_role 两角色；双视图并集 + `origin/pending/selectable/selectable_reason/note`；`patch_covered`；`patch_channel`；`registry_sources`；`notes`（含 selected 悬空检测） |
| `gan/framework/access.py` | 新增 `AccessBroker.covers(...)`（"该路径能否进本会话补丁"的**单一定义**） |
| `gan/tools/deep/register_component.py` | `_granted_coverage` 改为 `broker.covers` 的别名（消重，行为不变） |
| `gan/registries/__init__.py` | 导出新增 helper |
| `docs/**`（只增不删）、`AGENTS.md` | 同步记录；H11 登记为 §6.2 **B24（需决策）** |

### 8.8 明确不做（第 4 批）

- **`select_component` 工作区感知**：已决策保持"只认已提交"（见 §8.1）；将来若改，**前置条件是先修 H11**。
- **H11 本体**（`task_runner` 中 `apply_task_patch(...)` 之后**无条件** `persist_design(...)`）：设计持久化与补丁结果解耦 ⇒ 悬空引用。记入 §6.2 **B24（需决策）**；本批只在 `list_components` 的 `notes` 里加"selected 但不在注册表"的可见化。

> **§8.8 的两项在第 5 批都有了结论**：H11 已修（A2 自愈），"select 工作区感知"随 P-3 启动（前置条件恰好被本批满足）——见 §9.1，**第 4 批决策被正式推翻并记录原因**。

---

## 9. 第 5 批：plan → task agent 正确应用（B24 + B25 + P-3）

> 起因：用户问"planner plan 之后，task agent 能否正确应用 plan"，并要求解释"select 只认已提交"的代码路径。核实中实证出 **H12**（`set_config` 槽位绕过），与 B24 同族（悬空注入的第二条路径）。用户决策：**A2（每次持久化前自愈）+ P-3（同会话选择）+ set_config Option 1（slot 复用 select 校验）**。

### 9.1 决策记录（用户确认，含对 §8.1 的推翻）

| 决策点 | 结论 | 说明 |
|---|---|---|
| 自愈口径 | **A2**：每次持久化前都剥离 | 同时掐断"补丁被拒"与"父代继承旧悬空"两条链；"持久化设计 == 子代可交付"成为不变式。A1（仅拒时）不收敛存量链 |
| 同会话选择 | **P-3 启动** | "闭环快一个 inner"；前提 = A2 自愈存在（补丁被拒时设计被治愈，不留悬空）。**这推翻了 §8.1 的"select 只认已提交"决策**——当时的理由是"若改必须先修 H11"，本批修了 H11，前置条件满足 |
| set_config | **Option 1**：slot 键复用 select 校验 | 保留批量重写能力；Option 2（禁掉）会让 agent 逐名调用 |
| P-3 适用面 | **仅 task 设计** | 角色自改（`evaluator.self_improve:194` / `planner.self_improve`）在重试循环后**无条件** `save_self_config(cfg)`、无 heal——若给角色设计开 overlay，补丁被拒会留下悬空。角色设计的 heal 是后续项（§9.7） |

### 9.2 目标拆解与现状（"正确应用"的含义）

| 编号 | 含义 | 现状 |
|---|---|---|
| G1 | 设计引用的组件全被装配进子代 | ✅（B7 toolset_report，可观察） |
| G2 | plan 的 deep 源码改动到达子代运行副本 | ✅（`apply_task_patch` 先于 `prepare_run_dir`/`assemble_task_env`，`task_runner.py:103-137`） |
| G3 | 补丁被拒 ⇒ 设计不声称子代不具备的能力 | ❌（H11：无条件 persist + `config_dict` 同对象继承）→ **本批修** |
| G4 | 失配对 planner 可见 | ✅（拒绝原因已回传 `loop.py:467`；本批增 `design_stripped`） |
| G5 | 设计面校验无旁路 | ❌（H12）→ **本批修** |

**核实过且无需改**：`prepare_run_dir` 对副本再跑一次 `apply_patch`，已应用补丁再 apply 失败时 `gan/patch.py:apply_patch` 返回 False 不抛错，副本内容由 code_root 拷贝保证 → 无害（记录备查，不做）。

### 9.3 H11/B24 机理与修法（A2 自愈）

**机理**：`task_runner.run` 的 `config = plan.get("config")`（`task_runner.py:81`）**无深拷贝**——与 `plan_result["config"]`、`loop.py:788` 的 `child.meta["config_dict"]` 是**同一对象**；`persist_design`（`:128`）无条件执行。补丁被拒时 `apply_task_patch` 已 `_hard_rollback` 到父代 code，设计里的新组件名却照样进持久化文件与继承链 ⇒ 逐代传播（B7 每代重复报同一 skip，历史污染）。

**修法**（`task_execution.heal_design_slots` + `task_runner` 接线）：

- 插入点：`apply_task_patch` 之后、`persist_design` 之前；用**与装配完全同根同解析器**（`gan_roots(code_root)` + `load_registry_for_role` + `module_path`）逐名解析，失败即剥离；
- **就地改 `config`**（一个动作同时治愈持久化文件、`config_dict`、继承链——这是利用同一对象这一事实，不是副作用侥幸）；
- **只删不加**（不恢复源码、不重放补丁，与 S2 契约一致）；
- 审计：事件 `design_dangling_stripped {genid, role, slot, names:[{name, reason}]}` + `child.meta["design_stripped"]` + receipt `design_stripped` + 渲染行 `design note: dangling slot names STRIPPED before persist`。

### 9.4 H12/B25 机理与修法

**机理**：`set_config.py` 旧代码只做 `key in allowed_keys(ctx.role)`（schema **键**集合），`"skills"` 是合法键 ⇒ `ctx.config["skills"] = value` 直写，无 `reg.has`/`reg.is_valid`，也不查类型（`"abc"` 会被装配端拆成单字符名）。实证：`set_config("skills", ["created_this_session"]) → "task.skills updated"`，装配报 `not registered`。

**修法**（Option 1）：slot 键（`skills`/`eval_points`）必须 `list[str]`；逐名 `has` + `is_valid`（**同一权威** = select_component 用的视图，见 §9.5）；**原子拒绝**（任一名字非法整批失败，设计不动）；错误信息指路（list_components / register_component / select_component）；`record("set_config", key, selected=[...])`。`params.model` 过滤与未知键拒绝不变。

### 9.5 权威矩阵（P-3 之后）

| 会话 | select_component / set_config(slot) | 理由 |
|---|---|---|
| task 设计（planner 的 plan） | **有效视图**（工作区优先） | 持久化前有 A2 自愈兜底 ⇒ 同会话选择安全 |
| 角色自设计（self_improve） | **只认已提交** | 无自愈（见 §9.1 决策行）⇒ 不能开 overlay |
| deep 工具扫描 | 有效视图（第 2 批起） | 不变 |
| list_components `selectable` | **严格镜像 select**（I-3） | 跟随上两行分叉 |

三条时间线语义（写进 seed/AGENTS 的话）：选择在 plan 时刻对有效注册表校验；子代是否真拿到由"补丁是否落地"决定；落地失败由自愈剥离并记录——**设计永远不悬空**。

### 9.6 验证（第 5 批，`/tmp/gan_b5/verify.py`，51/51）

| 组 | 结果 |
|---|---|
| V3 set_config（12）：未注册名 / 非法类型×2 / 原子拒绝 / 合法接受+record / params/prompt 不受影响 / 未知键 / eval_points 正反 | ✅ |
| V4 镜像（13）：added→selectable=True 且 select 接受；removed→False 且 select 拒绝；evaluator 设计 committed-only；brand_new 对 evaluator 不可见不可选 | ✅ |
| V1/V2 深层 e2e（17，真实 `materialize` git 树 + `DomainTaskRunner` + 打桩 harness）：**成功路径**——补丁提交、无剥离、持久化设计含新技能、装配解析到；**拒绝路径**——补丁被拒、`design_stripped=[ghost]`、设计/持久化/`config_dict` 三处干净、事件存在、receipt 渲染 STRIPPED 行、就地对象同一性 | ✅ |
| batch-4 smoke（H10 catalog / H9 traversal） | ✅ |
| V5 回归（preflight 三角色 clean；summary 三个新分支） | ✅ |

### 9.7 变更清单（第 5 批）与遗留

| 文件 | 内容 |
|---|---|
| `gan/framework/task_execution.py` | 新增 `heal_design_slots(config, role, code_root)`（只删不加；就地；返回剥离清单） |
| `gan/framework/task_runner.py` | 接线（补丁应用后、persist 前）+ 事件 + `meta["design_stripped"]` |
| `gan/framework/loop.py` | `_make_receipt` 传 `design_stripped` |
| `gan/framework/receipt.py` | `build_receipt` 增字段 + 渲染行 |
| `gan/framework/context.py` | 新增 `session_overlay_root()`（workspace 含 `gan/` 时返回根，供设计算子取有效视图） |
| `gan/tools/design/select_component.py` | task 设计传 overlay（P-3）+ 返回语说明生效条件 |
| `gan/tools/design/set_config.py` | slot 校验（H12）+ 类型白名单 + 原子拒绝 + record 加 `selected` |
| `gan/summary.py` | `set_config`/`select_component`/`deselect_component` 分支（B14 最小子集） |
| `gan/tools/work/common/list_components.py` | `selectable` 镜像分叉（task=有效视图 / 角色=已提交）+ notes 文案同步 |
| `gan/tools/deep/register_component.py` | 成功消息同步（"可立即 select；补丁落地才真正生效"） |
| `AGENTS.md`、`docs/**`（追加式） | 新约定 + 状态同步 |

**新增遗留**：
- **角色设计的 heal**（`save_self_config` 无条件，同 H11 模式）：角色自设计现在 committed-only 所以**不会新造**悬空，但存量链上的悬空名未自愈——登记为 **B26**（~15 行，需决策：角色设计是否也开 overlay + heal）。
- B14 其余死分支清理仍在（本批只做了 3 个现代 op 分支）。

---

## 10. 第 6 批：注册表统一 + knowledge 体系（"三注册表统一 + SKILL=md 知识"）

> 起因：B2（跨注册表重复）讨论中用户先问了注册表分工（planner/evaluator 是否应共享 shared.json、skills 是否应仅限 task、task 的工具渠道），随后提出更彻底的设计——**不再有 shared，三注册表各自独立、源码按角色分开、全部视为 tools**；再修正词汇：**skills 应是 md 文本知识（经提示词/按需取阅），而非可调用工具**。用户决策：TOOL 统一 + knowledge（pull 型）+ p/e 知识库同批，**一批做完**。

### 10.1 决策记录

| 决策 | 结论 |
|---|---|
| 注册表模型 | **三文件独立、单写者**：`task.json`/`planner.json`/`evaluator.json`，无 shared.json；条目以 **name 为主键**；**角色目录绑定**（条目只能引用设计角色自己的 `gan/components/<role>/**`） |
| kind 字段 | **机械退役**（内容保留为描述性标签）：skill/eval_point 不再参与任何路由；旧条目/旧配置不需要改字段 |
| 设计槽位 | **每角色单一 `tools`**；旧键 `skills`/`eval_points` 作为别名被算子接受，并在载入时归一化（`schema.normalize_config`）——旧 checkpoint 不因改名静默丢选择 |
| SKILL 语义 | **md 知识（DATA），pull 型**：不进注册表、无 name=stem 契约；planner 经 deep 补丁创作；提交后整库物化进 task 沙盒；task 经 **opt-in `knowledge` 工具**按需取阅；**无知识配置清单**（base 即 base） |
| p/e 知识库 | **同批启用**：evaluator 知识库零代码（allowlist 已覆盖）；planner 知识库 = allowlist 加 `gan/components/planner/**` 一行；p/e 经**现有访问通道**读（grant+list_dir+read_file），不造第二条未审计读通道 |
| planner/evaluator 侧 tools 槽位 | schema 三角色同构（planner 先留空）；deep 工具的目标注册表 = 本角色设计的那个文件 |

**机制事实（为什么可行）**：eval_point 本来就是 `tool_info`/`tool_function` 形态的可调用工具（`hard_failure.py`），与 skill 走同一装配/装载链；差别只是词汇与目录。penalties 是活通道、`eval_point_results` 全仓无读者（死数据，处置留 B28），且**无任何"选中即必须调用"的强制**——统一不弱化现存治理。

### 10.2 由此消解的待办

- **A14（P5 措辞）**：shared 不存在了，"名不副实"问题**整体消失**；
- **B2（H1 跨注册表重复）**：单文件单写者 ⇒ 跨文件重复**构造不出来**；提交门与 `validate_registry` 的 duplicate 收敛为**文件内重名**（同一检查，双端一致）；
- **H6/C6（注册表作用域）**：角色目录绑定使"死条目"（evaluator 注册 skill 类）**不可构造**；
- **Q2 的死条目/目录噪音类**：同上，结构性消失。

**保留**：B1/B16/B17（碰撞面仍在 always-on）、身份契约（统一后更重要）、第 4/5 批 overlay+自愈机制（原样，槽位换 `tools`）。

### 10.3 变更清单

| 文件 | 内容 |
|---|---|
| `gan/registries/loader.py` | 单文件加载（去 shared 合并）、name 主键、`entry_reason(.., owning_role)` 角色目录绑定、duplicate=文件内重名、`_ROLE_OF_FILE`、`KINDS` 降级为 legacy 标签 |
| `gan/framework/frozen.py` | 去 shared 两行；planner 加 `gan/components/planner/**` |
| 内容迁移 | `shared/skills/{bash,editor}.py` → `task/skills/`；删 `gan/components/shared/**`；`shared.json` 删除；`task.json` 重写（bash/editor/knowledge，kind 留标签） |
| `gan/components/task/tools/knowledge.py` | **新**：pull 型取阅工具（列目录/读单篇、`basename` 封目录、单篇 20k 封顶） |
| `gan/framework/task_execution.py` | `GAN_TASK_TOOLS_DIR`（改名）+ `GAN_TASK_KNOWLEDGE_DIR` + 提交库整库物化（单篇/总量封顶、skip 入 toolset_report.knowledge）；`heal_design_slots` 槽位 `tools` |
| `gan/design/schema.py` + `store.py` | `tools` 槽位统一 + `normalize_config`（legacy 折叠，载入/恢复路径全覆盖） |
| `gan/tools/assembly.py` | 单槽位解析（兼容 legacy 键） |
| `gan/tools/design/{select,deselect,set_config}.py` | slot 别名归一（`skills`/`eval_points`→`tools`）；set_config 的 slot 校验随 H12 保持 |
| `gan/tools/deep/{register,unregister}_component.py` | 去 kind 参数、目标=设计角色注册表、角色目录绑定检查（register 中**最优先**报错）、unregister 按名扫描单文件 |
| `gan/tools/work/common/list_components.py` | 单注册表简化（去跨文件 origin/pending 逻辑），保留 overlay/pending/H10/selectable 镜像/patch_covered |
| `gan/framework/{preflight,code_repo}.py` | preflight 用 `resolve_module`；gate 的 duplicate=文件内重名（双端一致） |
| `gan/framework/{receipt,task_runner,loop,build}.py` + `task_agent.py` | `tools_added/removed`、slot=tools、`_parent_config` 归一化、`tools` 键 + env 双读（兼容旧 env 名） |

### 10.4 验证（/tmp/b6_verify.py，35/35）

- V1 loader：task 目录=bash/editor/knowledge、evaluator 目录原样保留、legacy kind 标签通过校验、跨树条目被拒、文件内重名被检；
- V2 assembly：`tools` 槽位装配 ✓、legacy `skills` 键仍装配 ✓；
- V3 设计算子：select/deselect/set_config 的别名与原子拒绝、record 带 `selected`；
- V4 归一化：`normalize_config` 折叠 + `DesignStore.load` 折叠（旧 checkpoint 安全）；
- V5 knowledge：物化（超限 skip 入 report）+ env 改名 + 工具列/读/目录封顶；
- V6 deep：无 kind 注册、同会话选择（P-3 不变）、跨树拒绝、record 无 kind、按名注销；patch 携带注册；
- V8 heal：`tools` 槽位剥离 ghost（"补丁被拒→设计被治愈"在统一词汇下重建）；
- V7 preflight/gate：真实仓库 0 问题 0 碰撞；文件内重复被门拒。
- 另：深层 heal 回归（register→select→poison patch→strip）在统一词汇下重建通过。

### 10.5 明确不做（第 6 批）

- **R5/B28**：`eval_point_results`（全仓无读者）的接线或删除——统一时未动，待决策；
- **B27**：子进程 `load_tools` skip 可见性（此前已拍板登记）；
- **B29**：`set_prompt` 记录升级（hash+size）——放弃知识配置清单后，提示词是唯一"不透明"知识通道，此项价值上升；
- **push 型知识清单**：被 pull 型替代，不实现；
- **planner.json 启用**：schema 同构就绪，等真有 planner 组件需求时解锁（C 档）。
