# 工具与算子管理：deep 写通道修复记录（R1 + P1）

> **承接**：`docs/工具管理审查.md`（P1/P4）、`docs/7_遗留问题与待办.md` §3、以及本轮复核新发现的 R1。
> **范围**：修复 `register_component` / `unregister_component` **单独调用时补丁不生成**（R1），以及**注册表丢失末尾换行导致补丁必然 apply 失败**（P1）。
> **方式**：真实链路实证——真实 `AccessBroker` / 真实工具 / `build_patch_from_workspace` / `apply_code_patch`，不依赖手写补丁。
> **基线**：`b03bf83`；改动提交前工作树见文末变更清单。
> **证据约定**：`文件:行号` 指**本次修复后**的仓库状态。

---

## 0. 结论摘要

1. "单独调用丢失"不是单一 bug，而是**三个独立成因叠加**；只修任一处都不通。
2. 修复后，`register_component` / `unregister_component` 在 plan / self_improve 会话中**单次调用即可生成补丁并提交**（真实链路验证）。
3. 判据收敛为**一个** `has_deep_write`（`gan/patch.py:130`），覆盖 4 个消费点；新增 deep 工具**无需登记**即自动生效（fail-safe）。
4. evaluate 会话中的两个工具改为**终结性拒绝 + 提示词声明"当前不可用"**，消除 C3 的静默丢弃。
5. 遗留：同会话多次 `unregister_component` 的覆盖问题（R2/P4）**不在本次范围**，见 §6。

---

## 1. 问题定位（三个成因 + 一个漂移点）

### C1 补丁闸门只看 records，而两个工具不记录

三个 `_build_patch` 均以 records 为判据，`register_component` / `unregister_component` 只调 `broker.grant`、不写 design/plan context：

- `planner.plan`（修复前 `gan/roles/planner.py:122-128`）：`code_edit` 或 `request_source_access(intent=modify)`；
- `planner.self_improve`（修复前 `:209-216`）与 `evaluator.self_improve`（修复前 `gan/roles/evaluator.py:138-148`）：仅 `request_source_access(intent=modify)`。

**实证**：`register_component` 返回成功、workspace 已产生 234 字节注册表 diff，但 `dctx.records == []` → 闸门返回空 → 补丁为空 → `rejected=None`、无事件、无 hint → **注册被静默丢弃**。

### C2 注册表末尾换行丢失（P1）

4 个注册表均以 `\n` 结尾；两个工具用 `json.dump(...)` 重写 workspace 副本，不带末尾换行（修复前 `register_component.py:130-131`、`unregister_component.py:80-81`）；`difflib.unified_diff` 无法表达"文件末尾无换行"。

**实证**：`register_component` 产出 234B 补丁 → `apply_patch_detail` 返回 `(False, 'error: corrupt patch at line 14|patch: **** malformed patch at line 13')`；`unregister_component` 多 chunk 场景返回 `patch fragment without header at line 19`。即 **deep 注册/注销的写接口整体失效**。

### C3 evaluate 会话没有补丁通道

`evaluator.evaluate()`（`gan/roles/evaluator.py:76`）只设 `EvalContext` + `AccessContext`，**不设 `DesignContext`、也没有任何补丁构建路径**；而 `deep/` 是 always-on（evaluator 会话可见 `register_component` / `unregister_component` / `request_source_access`，实测 23 个工具）。设计算子在该会话本就返回 `Error: no design context`，两个 deep 工具却漏了这一层 → 深改被整体丢弃。

### C4 第四个漂移点：`_modify_depth`

`gan/framework/task_runner.py:_modify_depth` 用同一套 op 列表（修复前 `:28-36`）。若只给工具加 record 而不改这里，register/unregister 会被记为 `modify_depth=1`（operator），而它们实际是 code 级（`store.py:47`：0=config / 1=operator / 2=code）。

---

## 2. 决策记录

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 换行修复层次 | **工具层 + 统一写入 helper**（`write_registry_json`）；不在本次实现 `patch.py` 的 `\ No newline` marker 支持 |
| D2 | evaluate 会话语义 | **禁止调用（运行时守卫）+ 提示词声明**；文案直接说"**当前不可用**"，不写"仅 self-improve 可用" |
| D3 | 防御层 | **实现** `deep_edit_dropped` 事件（工作区有改动但未生成补丁时） |
| D4 | 判据形式 | **混合：从 `gan/tools/design/` 动态派生浅层集合 + 常量兜底**；未知 op 一律默认"深写" |

D2 的备选（"分割装配权限"：evaluate 不装配 register/unregister）被否，理由：需改 assembly / base_role / loop / 目录布局 / B7 receipt，且要精细保留 `request_source_access(view)`；而"禁止调用"与既有设计算子的守卫完全同构，改动最小、风险最低。

---

## 3. 实现

### 3.1 `gan/patch.py`：唯一判据

- `_DESIGN_DIR = Path(__file__).resolve().parent / "tools" / "design"`；`_SHALLOW_DESIGN_OPS` 从该目录 `*.py` 的 stem 动态派生（`gan/patch.py:112-127`），失败回退到显式 5 名单 `_FALLBACK_SHALLOW`。
- `has_deep_write(records)`（`:130`）：
  - 浅层 design 算子 → 不触发补丁；
  - `request_source_access` 仅 `intent == "modify"` 计深写（`view` 是只读拷贝）；
  - **其余任何 op**（`register_component` / `unregister_component` / `code_edit` / 未来 deep 工具）→ 计深写。

设计取舍：**未知 op 默认深写**。新 deep 工具忘了登记也仍会生成补丁（正是 R1 的 bug 类别）；新 design 算子放进 `gan/tools/design/` 即自动浅层。

### 3.2 `gan/registries/loader.py`：换行安全的统一写入

`write_registry_json(path, data)`（`:70`）：先读原文件判断是否以 `\n` 结尾，`json.dump(..., indent=2)` 后按原约定补 `\n`。两个工具都改走它，换行不变量收敛到一处。

### 3.3 两个 deep 工具

| 位置 | 改动 |
|---|---|
| `register_component.py:68-71` | 无 `DesignContext` → 返回终结性错误（"currently unavailable … Do not retry"） |
| `register_component.py:139-140` | `write_registry_json` + `dctx.record("register_component", kind, name, module, registry)` |
| `unregister_component.py:51-54` | 同上守卫 |
| `unregister_component.py:89-93` | `write_registry_json` + `dctx.record("unregister_component", kind, name, registry, module)` |

record 只带**结构化字段**（无自由文本），进入 receipt / diff_summary 时不构成泄漏（`receipt._ops_summary` 白名单只复制 `slot`/`name`/`key`/`intent`/`paths`；`summary.build_diff_summary` 对未知 op 只留 `{"op": op}`）。

### 3.4 四个消费点统一

| 文件 | 位置 | 改动 |
|---|---|---|
| `gan/roles/planner.py` | `:125`（plan）、`:210`（self_improve） | `_build_patch` 改用 `has_deep_write` |
| `gan/roles/evaluator.py` | `:146`（self_improve） | 同上 |
| `gan/framework/task_runner.py` | `:28`（`_modify_depth`） | 改用 `has_deep_write`，register/unregister 正确计为 2 |

### 3.5 防御层（D3）

`gan/roles/base_role.py:15` `warn_dropped_workspace_edits(...)`：会话结束若 `patch_str` 为空、无深写记录，但 `build_patch_from_workspace` 仍有差异 → `soft_fail(..., event_type="deep_edit_dropped")`。仅告警，不失败会话；diff 计算异常被吞（advisory probe）。

接入点：`planner.py:163`（plan）、`planner.py:240`（self_improve）、`evaluator.py:179`（self_improve）。**不接入 `evaluate()`**：evaluate 与 self_improve 共用同一 workspace key，evaluate 的编辑可能被后续 self_improve 的补丁合法带上，此时告警会是误报。

### 3.6 evaluate 提示词（D2）

`evaluator.py` 的 blind（`:30-31`）与 reveal（`:68-69`）指令各加一句：

> Note: deep registry changes (`register_component` / `unregister_component`) are currently unavailable in this session.

---

## 4. 验证

| # | 断言 | 结果 |
|---|---|---|
| V1 | `has_deep_write` 8 例（浅层 / view / modify / 两个新工具 / 未来新工具） | 全部符合预期 |
| V2 | `_modify_depth(register)=2`、`_modify_depth(set_prompt)=1` | 通过 |
| V3 | **register-only 真实工具 E2E**：232B 补丁 → `apply_code_patch` 成功 | 通过（commit `7910ca88`） |
| V4 | **unregister-only 真实工具 E2E**：739B（注册表 + 模块文件同时删除）→ 成功 | 通过（commit `5fa08142`） |
| V5 | **`planner.plan` 角色级 E2E**（mock LLM 回合）：records 含 `register_component`、patch 232B、无 rejection | 通过 |
| V6 | **`evaluator.self_improve` 角色级 E2E**：patch 2064B、无 rejection | 通过 |
| V7 | 空会话（无深写）：patch 为空、无 rejection、无事件 | 通过 |
| V8 | evaluate 形态（新进程，无 `DesignContext`）：两个工具均返回 "currently unavailable" | 通过 |
| V9 | 防御层：有 workspace diff、无深写记录 → 写 `deep_edit_dropped` 事件；对照组不写 | 通过 |
| V10 | `scripts/local/test_frozen.py` / `test_kinship.py` / `test_domain_entry.py` | 全绿 |
| V11 | `preflight_tools` 三角色 0 问题 0 碰撞 | 通过 |
| V12 | 全仓 `compileall` + 改动模块 import smoke | 通过 |

---

## 5. 行为变化与边界

- `register_component` / `unregister_component` **要求 design context**（plan / self_improve 有；evaluate 无 → 终结性拒绝）。已确认 `scripts/local/*` 无直接调用，不破坏本地测试。
- `code_edit` 被纳入 self_improve 闸门；全仓无 `.record("code_edit")`（死分支），无行为变化。
- `request_source_access(intent=view)` + `edit_source` 仍不构建补丁（保持"view = 只读"语义），但会触发 `deep_edit_dropped` 告警 → 不再静默。
- 未采用"工作区有 diff 就构建"的闸门：plan 与 self_improve 共用同一 `akey` workspace，被拒补丁的残留编辑会在后续会话被反复重放；records 闸门是有意的会话边界。

---

## 6. 未做 / 遗留

1. **R2 / P4（同会话多次 `unregister_component` 只有最后一次生效）**：`unregister_component.py:73,75` 的 `broker.grant` 无条件把 repo 注册表拷回 workspace，还原前一次删除。实测 `editor`→`bash` 后 `editor` 复活。最小修法是与 `register_component` 的 `if not os.path.isfile(ws_reg)` 守卫对称，但会改变"重复 `request_source_access` 刷新工作区副本"的既有语义，**属需单独拍板的语义决策**，本次未改。
2. **`patch.py` 的 `\ No newline at end of file` 支持**（D1 备选）：未实现。当前由 `write_registry_json` 在工具层保证；若未来有工具整体重写文件丢换行，同类 bug 会复发（防御层不会捕获——补丁能构建但 apply 失败，会走既有 rejection 通道）。
3. **文档同步**：`AGENTS.md` 与 `docs/7` §3 尚未记录"register/unregister 需要 plan/self-improve 补丁通道"这一新约束；`docs/工具管理审查.md` 中关于 B7/E3 的过时结论（本轮复核已确认）也未回改。
4. **`evaluate()` 的 `edit_source`**：仍无补丁通道且无守卫；其编辑可能被后续 self_improve 补丁带上，也可能丢失。未在本次处理。

---

## 7. 变更清单

**代码（8 个文件）**
- `gan/patch.py` — `has_deep_write` + 浅层集合派生
- `gan/registries/loader.py` — `write_registry_json`
- `gan/tools/deep/register_component.py` — 换行 + record + 守卫
- `gan/tools/deep/unregister_component.py` — 换行 + record + 守卫
- `gan/roles/planner.py` — 两个闸门 + 防御层接入
- `gan/roles/evaluator.py` — self_improve 闸门 + 防御层 + evaluate 提示词
- `gan/roles/base_role.py` — `warn_dropped_workspace_edits`
- `gan/framework/task_runner.py` — `_modify_depth`

**文档**
- 本文件

**关联**
- 上一轮复核结论见 `docs/工具管理审查.md`。该文档中"选择静默降级未修 / E3 checkout 吞异常"两处判断已过时：B7 装配报告与 E3 的 `RepoIntegrityError` fail-fast 在本仓库均已实现，本轮复核已实测确认。
