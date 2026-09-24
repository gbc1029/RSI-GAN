# 工具与算子管理：实现方案（合法性检验 + register_component + list_components）

> 承接 `工具与算子管理_漏洞梳理与修改建议.md`。本轮新增三份只读调查（config 继承链、代码/注册表 git 继承链、实现模板），其中 config 继承链的结论**更正了上一份报告 N5 的措辞**，见 §1。
> 仓库：`\\wsl.localhost\Ubuntu\root\HyperAgents`。所有结论附 `文件:行号`。

---

## 1. 措辞更正与重新核验："config、注册表、代码都可以正确保存和继承"

### 1.1 原措辞不准确，更正后成立

原措辞"config、注册表、代码**均被 git 覆盖**"**不准确**：design config 不在任何 git 树内（它在 `<output_dir>/ckpt/design/**`，而默认 `output_dir=outputs/gan_run` 被 `.gitignore:26` 忽略）。

更正后的"**都可以正确保存和继承**"——**成立**。但三者机制不同：

| 资产 | 保存 | 继承 | 机制 | git 血缘 |
|---|---|---|---|---|
| 注册表 `gan/registries/*.json` | ✅ | ✅ | 在 `_COPY_TOP="gan"` 内 → `ckpt/code` → `apply_code_patch` 提交 | ✅ 有 |
| 代码（`gan/components/**`、`gan/tools/work/**`、`gan/roles/*.py`…） | ✅ | ✅ | 同上，`task_<genid>` / `outer_<O>_base` ref | ✅ 有 |
| design config（三 role） | ✅ | ✅ | `ckpt/design/**` 磁盘文件 + checkpoint `designs` 快照 | ❌ 无 |

关键证据：
- 注册表在代码树内：`_COPY_TOP = ["gan", ...]`（`gan/framework/code_repo.py:25`），`gan/registries/` 不被 `_EXCLUDE_DIRS`/`_EXCLUDE_PATTERNS` 排除（`code_repo.py:27-29`）。
- 注册表可提交且受校验：`apply_code_patch` = allowlist → apply → compile → `registry_report` 对比 → commit（`code_repo.py:347-373`）。
- 跨 outer 复用同一 `ckpt/code`：`ensure_code_root` 以 `ckpt/code/.git` 是否存在为幂等判据（`gan/build.py:57`）。
- config 双通道继承：磁盘文件复用（`_seed_self_designs` 见文件存在即 `continue`，`build.py:36-39`）+ checkpoint 覆盖写（`restore_designs`，`gan/framework/checkpoint.py:124-129`，调用点 `gan/framework/loop.py:530`）。
- task config 的继承载体是 **node meta**，不是 design 文件：`_parent_config` 读 `parent.meta["config_dict"]`（`loop.py:151-153`），随 trees 快照持久化（`checkpoint.py:198-204`）。

### 1.2 更正我上一份报告 N5

上一份报告把 N5 表述为"config 未进 git，**继承面不成立**"。**该表述过强，予以更正**：

- **成立的部分**：config 确实不在 git 内，因此缺少 `task_<genid>` 式的血缘审计，可移植性弱于代码与注册表；`snapshot_designs` 的 role 被硬编码为 `["planner","evaluator"]`（`loop.py:315`），**task 的 per-genid design 文件确实不被快照/恢复**。
- **需撤回的部分**：这**不导致继承失败**。planner/evaluator 有磁盘+checkpoint 双保险；task 经 node meta `config_dict` 继承，与 per-genid 文件无关。
- **准确的严重度**：N5 应降级为"**审计与可移植性缺口**"，而非"继承面不成立"。

### 1.3 重新核验：不成立/需注意的场景（逐条附触发条件）

| # | 触发条件 | 后果 | 代码位置 |
|---|---|---|---|
| E1 | `ckpt/` 被删或换机器 | planner/evaluator config 被重置到 gen-0 seed，累积自改进全丢；无 git 可恢复 | `.gitignore:26`；`build.py:36-50`；`paths.py:66-67` |
| E2 | `code_repo=False`（`--in-process`） | 注册表/代码**无任何跨 outer 持久化**；注册表回退真实仓库；workspace 注册表改动被丢弃 | `build.py:98`；`loop.py:583,386-387`；`base_role.py:60` |
| E3 | `ckpt/code` 被删 | `ensure_code_root` 静默重建 initial 树，演化 commit 与 `task_*`/`outer_*_base` 分支全丢；resume 的 `checkout` 因目标 commit 不存在而**吞异常静默失败**，无 fail-fast | `build.py:57-61`；`driver.py:116`；`code_repo.py:128-134` |
| E4 | 只改 seed 文件 | 对已存在 config 无效果（文件存在即权威） | `build.py:36-39` |
| E5 | **需确认**：下一 outer 中某代的 parent 是 task 节点 | `_resolve_base` 取 `parent.meta["code_commit"]`（早于上一 outer 末尾的 self-edit），而 `outer_<O>_base` 才含 self-edit → **self-edit 在该代可能不可见** | `loop.py:568-570` |

> E5 是子代理的推理结论，机制上有代码支撑，但"是否属设计意图"需人工确认；建议在实现前用一个最小实验（2 outer × 2 inner，检查各代工作树是否含上一 outer 的 self-edit）验证。

**结论**：措辞更正为"都可以正确保存和继承"后，**主结论成立**；上表 E1–E5 是边界条件，其中 E3、E5 值得单独跟进（E3 是静默失败，E5 待确认）。

---

## 2. 总体设计：三层

三项改动不是并列的三个补丁，而是一组配合：

```
① 校验层（合法性检验完善）  → 定义"什么算合法"，被三处复用
     gan/registries/loader.py   （判据本体）
     gan/framework/code_repo.py （提交门控）
     gan/framework/preflight.py （启动期）

② 写接口（register_component）→ agent 的注册动作，原子 + 校验 + 审计
     gan/tools/deep/register_component.py   （对称于 unregister_component）

③ 读接口（list_components）  → agent 的枚举能力（当前完全缺失）
     gan/tools/work/common/list_components.py
```

**为什么校验层要独立**：同一套判据要同时服务于"选择时拒绝""提交时打回""启动期报错"三个时机。若各写一份，必然漂移。

**为什么两个工具要成对**：只有 `register_component` 而无 `list_components`，agent 仍然只能"猜名字"——注册表作为接口依旧失效。这是当前最缺的一块。

---

## 3. ① 合法性检验完善

### 3.1 现状判据（4 条）

`gan/registries/loader.py:50-63` 的 `entry_reason` 校验：①是 dict ②`name`/`kind`/`module` 非空 ③`kind ∈ KINDS` ④`module` 文件存在。返回 None 即合法。

### 3.2 新增判据（4 条）

| # | 判据 | 理由（附证据） |
|---|---|---|
| V1 | `name` 必须等于 `module` 的 basename（去 `.py`） | `load_tools` 以 `tool_file.stem` 为工具名（`agent/tools/__init__.py:45`），再用 config 里的组件名过滤（`:46`）→ 不等则**装配成功但静默不加载**（上一份报告 N2） |
| V2 | `kind` 必须与所在目录约定一致（`skill`↔`skills/`，`eval_point`↔`eval_points/`） | `select_component` 的 `_SLOT_KIND`（`select_component.py:10`）与 `assembly` 的 kind→slot 映射依赖该约定 |
| V3 | `module` 必须暴露 `tool_info` 与 `tool_function` | `load_tools` 的硬性要求（`agent/tools/__init__.py:42-44`）；缺失则记日志跳过。用 **AST 静态检查**（不 exec，无副作用） |
| V4 | **(kind, name) 在合并后的注册表内唯一** | **新发现**：`load_registry_for_role` 拼接 `shared + specific`（`loader.py:128`），而 `get` 返回**第一个**匹配（`loader.py:80-83`）→ **`shared.json` 的条目会静默遮蔽 `role.json` 的同名条目**；且 `names()`（`loader.py:76-77`）会返回重复名，经 `build.py:49` 写进 evaluator 的 `eval_points` |

### 3.3 新增：注册表级校验函数（含孤儿检测）

```python
# gan/registries/loader.py（新增）
def validate_registry(role, registry_dir=None, components_dir=None) -> List[Dict[str, str]]:
    """Return all problems for a role's merged registry.

    problem = {"type": "invalid"|"duplicate"|"orphan", "kind", "name", "reason"}
    """
    reg = load_registry_for_role(role, registry_dir, components_dir)
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    out: List[Dict[str, str]] = []

    # (a) per-entry invalid
    out += [{"type": "invalid", **r} for r in reg.invalid()]

    # (b) duplicate (kind, name) -- shared silently shadows role-specific
    seen = set()
    for e in reg.entries:
        if not isinstance(e, dict):
            continue
        key = (e.get("kind"), e.get("name"))
        if key in seen:
            out.append({"type": "duplicate", "kind": str(key[0]), "name": str(key[1]),
                        "reason": "duplicate (kind,name) in merged registry; earlier entry wins"})
        seen.add(key)

    # (c) orphans: component files with no registry entry  [上一份报告 N6]
    registered = {str(e.get("module")).replace("\\", "/")
                  for e in reg.entries if isinstance(e, dict) and e.get("module")}
    for f in sorted(cdir.rglob("*.py")):
        if f.name == "__init__.py":
            continue
        rel = f.relative_to(cdir).as_posix()
        if rel not in registered:
            out.append({"type": "orphan", "kind": "", "name": f.stem,
                        "reason": f"component file not registered: {rel}"})
    return out
```

`_exposes_tool_api`（V3 的实现，AST 静态检查）：

```python
def _exposes_tool_api(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    defs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return "tool_info" in defs and "tool_function" in defs
```

### 3.4 三处复用点

**(a) 选择时**（已有，无需改）：`select_component` 已调 `reg.has` + `reg.is_valid`（`select_component.py:41-45`）。V1–V4 生效后，`is_valid` 自动覆盖。

**(b) 提交时**（需改：放宽触发条件 + 扩充判据）

现状：`_patch_touches_registry(files)` 仅在 patch 触及 `gan/registries/` 前缀时才跑校验（`code_repo.py:319-320`）。**只改组件代码、不动注册表的 patch 不做任何注册表校验**。

```python
# gan/framework/code_repo.py
def _patch_touches_registry(files) -> bool:
    return any(f.startswith("gan/registries/") for f in files)

def _patch_touches_components(files) -> bool:          # 新增
    return any(f.startswith(("gan/components/", "gan/tools/work/")) for f in files)
```

在 `apply_code_patch` 中把触发条件放宽，并改为**差分**判定（沿用 `_registry_worsened` 的思路，避免因既存问题硬拒而"烧轮次与门搏斗"）：

```python
if _patch_touches_registry(files) or _patch_touches_components(files):
    reason = _registry_worsened(before, registry_report(code_root), strict=strict_unparseable)
    if reason:
        _hard_rollback(code_root, prev)
        raise PatchRejected(reason)
```

`registry_report` 改为按 role 调 `validate_registry`，把 `invalid` / `duplicate` / `orphan` 三类计数一并纳入；`_registry_worsened` 比较"新增"的这三类问题数。**关键**：只拒"新增"，既存问题不阻塞（否则首次开启该门控会把所有历史遗留一次性打回）。

**(c) 启动期**（新增）：`gan/framework/preflight.py` 目前**只探模型可用性**（`preflight.py:15-38`），完全不碰工具集/注册表。扩展为：

```python
# gan/framework/preflight.py（扩展）
def preflight_tools(roles=("planner", "evaluator", "task")) -> Dict[str, Any]:
    """Validate each role's registry + assembled toolset. Fail fast on problems."""
    out = {}
    for role in roles:
        problems = validate_registry(role)
        # 装配到临时目录并做 basename 唯一性检查（对应上一份报告 A-a）
        collisions = _assemble_dryrun_collisions(role)
        out[role] = {"problems": problems, "collisions": collisions}
    return out
```

在 `build_gan_loop` 的 `preflight` 分支（`build.py:137-142`）一并调用，任一 role 有问题即 `raise`。**注意**：启动期用"零问题"判据（严格），提交门控用"无新增"判据（宽松）——两者语义不同，不可混用。

### 3.5 边界与取舍

- **V3 用 AST 而非 import**：import 会执行模块顶层代码（可能有副作用、可能依赖运行时环境如 `get_access_context`）。AST 检查足以覆盖"忘了定义 tool_info/tool_function"这一主要失误；真正的 import 失败由 `load_tools` 现有的 try/except 兜底（`agent/tools/__init__.py:52-54`）。
- **孤儿检测的粒度**：只扫 `*.py`（跳过 `__init__.py`）。`gan/components/task/skills/` 当前为空，不会误报。
- **不引入自动注册**：注册仍是显式动作（见 §2 结论），`validate_registry` 只做"检测与打回"，不替 agent 写注册表。

---

## 4. ② register_component 工具

### 4.1 归类：DEEP，不是 design 算子

依据：`gan/registries/loader.py:12` 与 `select_component.py:4-5` 均明确"注册一个新组件是**源码级（deep）改动**"；且注册与注销同类——`unregister_component` 位于 `gan/tools/deep/`（`unregister_component.py:1-8`）。

**因此必须放 `gan/tools/deep/`，与 `unregister_component` 对称**，走 workspace + patch 校验 + `frozen.is_allowed` 门控。做成 `gan/tools/design/` 的浅层算子会造成"浅层算子做深改动"的分类矛盾。

### 4.2 行为（完全镜像 unregister_component）

unregister 的流程（`unregister_component.py:40-88`）：
1. 取 `get_access_context()` → role / broker / node
2. 定位注册表文件，`frozen.is_allowed(role, rel, "modify")` 校验
3. 校验组件源码也可写
4. `broker.grant(role, node, [rel], intent="modify", reason="unregister")` 把文件**拉进 workspace**
5. 改 **workspace 副本**（删条目 + 删文件）
6. 返回"将随本会话 patch 提交"

register 对称：定位注册表 → 权限校验 → **校验模块文件存在且可写** → 拉进 workspace → **追加条目** → 返回。

### 4.3 工具形态

```python
# gan/tools/deep/register_component.py（新增）
"""DEEP gate: register an existing component module in a registry.

Symmetric to ``unregister_component``. Edits the role's **workspace** copy of the
registry (entry added) rather than committing immediately -- the change is folded
into the session's patch and goes through the normal commit validation
(allowlist + compile + registry) together with the rest of the edits.
"""
import json
import os
from pathlib import Path

from gan.framework import frozen
from gan.framework.context import get_access_context
from gan.registries.loader import entry_reason

_REGISTRY_FILES = ["shared.json", "task.json", "planner.json", "evaluator.json"]
# planner designs the TASK agent -> registers task skills; evaluator -> eval_points
_OWN_REGISTRY = {"planner": "task.json", "evaluator": "evaluator.json"}


def tool_info():
    return {
        "name": "register_component",
        "description": (
            "DEEP add: register an EXISTING component module in a registry so it can be "
            "selected. The module file must already exist and be editable by your role. "
            "Applied to your workspace; committed with this session's patch after "
            "validation. kind: skill | eval_point. registry defaults to your role's own "
            "(planner -> task.json, evaluator -> evaluator.json)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["skill", "eval_point"]},
                "name": {"type": "string",
                         "description": "Must equal the module file stem (without .py)."},
                "module": {"type": "string",
                           "description": "Path relative to gan/components, e.g. "
                                          "task/skills/foo.py"},
                "registry": {"type": "string",
                             "enum": ["shared.json", "task.json", "evaluator.json"]},
            },
            "required": ["kind", "name", "module"],
        },
    }


def tool_function(kind, name, module, registry=None, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    role = actx.role
    broker = actx.broker
    node = actx.node_id
    code_root = broker.repo_root

    reg_name = registry or _OWN_REGISTRY.get(role)
    if reg_name not in _REGISTRY_FILES:
        return f"Error: no registry for role {role} (pass registry= explicitly)"
    reg_rel = f"gan/registries/{reg_name}"

    mod = str(module or "").replace("\\", "/").strip("/")
    if not mod.endswith(".py"):
        return "Error: module must be a .py path relative to gan/components"
    module_rel = f"gan/components/{mod}"

    # 1) permission gate (same rule as unregister_component)
    if not frozen.is_allowed(role, reg_rel, "modify"):
        return f"Error: registry not editable for {role}: {reg_rel}"
    if not frozen.is_allowed(role, module_rel, "modify"):
        return f"Error: component source not editable for {role}: {module_rel}"

    # 2) the module must already exist in the code tree (deep add of a REGISTRATION,
    #    not of the file itself -- file creation stays with edit_source)
    if not os.path.isfile(os.path.join(code_root, module_rel)):
        return (f"Error: module not found: {module_rel}. Create the file first "
                f"(edit_source), then register it.")

    # 3) name contract, identical to the loader validator (V1)
    if Path(mod).stem != str(name):
        return (f"Error: name '{name}' must equal the module basename "
                f"'{Path(mod).stem}'")

    # 4) validate the entry as it would appear (V1/V2/V3 via entry_reason)
    candidate = {"name": name, "kind": kind, "module": mod}
    reason = entry_reason(candidate, Path(os.path.join(code_root, "gan", "components")))
    if reason:
        return f"Error: invalid entry ({reason})"

    # 5) bring the registry into the workspace and append atomically
    broker.grant(role, node, [reg_rel], intent="modify", reason="register")
    src = broker.src_dir(role, node)
    ws_reg = os.path.join(src, reg_rel)
    try:
        with open(ws_reg, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"Error: cannot edit registry in workspace: {e}"
    if not isinstance(data, dict):
        return f"Error: registry is not an object: {reg_rel}"

    comps = data.setdefault("components", [])
    for c in comps:
        if isinstance(c, dict) and c.get("kind") == kind and c.get("name") == name:
            if str(c.get("module")).replace("\\", "/") == mod:
                return f"Already registered: {kind} '{name}' in {reg_rel}"
            return (f"Error: {kind} '{name}' already registered with a different "
                    f"module ({c.get('module')})")
    comps.append({"name": name, "kind": kind, "module": mod})
    with open(ws_reg, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return (f"Registered {kind} '{name}' -> {reg_rel} (module {module_rel}). It will be "
            f"committed with this session's patch after validation; then use "
            f"select_component to enable it.")


op_info = tool_info
op_function = tool_function
```

### 4.4 设计要点

- **原子性**：读-改-写在同一函数内完成，且写的是 workspace 副本；不产生半写的注册表。
- **与 patch gate 的关系**：本工具**不自己 commit**。它改 workspace → 会话 patch → `apply_code_patch` 走 allowlist + compile + registry 校验（§3.4b）。因此 V1–V4 与 commit 门控自动叠加，无需重复实现。
- **权限天然受限**：`_OWN_REGISTRY` 让 planner→`task.json`、evaluator→`evaluator.json`；再经 `frozen.is_allowed` 二次判定。`planner.json` 不在任何 role 的 write roots（`frozen.py:55-58`）→ 即使用 `registry="planner.json"` 也会被 `is_allowed` 拒绝。
- **不做自动注册**：工具只登记**已存在**的模块；文件创建仍归 `edit_source`。这保持了"agent 显式决定注册"的语义（§2 结论），也让 `list_components` 的"未注册候选"有意义。
- **审计**：与 `unregister_component` 一致，用 `broker.grant(..., reason="register")` 留痕。**可选增强**：若采纳上一份报告 T2 的"结构化 record"约定，应补 `ctx.record("register_component", kind=..., name=..., module=...)`（纯结构化字段，无自由文本）——但需同时给 `unregister_component` 补对称记录，避免两者审计不对称。

### 4.5 需同步的小改动

- `gan/tools/deep/__init__.py`：无需改（`always_on_dirs` 按目录 `glob("*.py")` 自动纳入，`assembly.py:106-111`）。
- `AGENTS.md` §Conventions：把"deep add/modify = new component or logic, which **must** touch source via the gated `request_source_access`"（`AGENTS.md:79-81`）更新为"deep add = 创建文件（`request_source_access` + `edit_source`）**+ 注册**（`register_component`）"，否则文档与实现不一致。
- `frozen.py`：**无需改**——`gan/tools/deep/**` 是 always-on plumbing，角色不可写；注册表写入权限已由现有 write roots 覆盖。

---

## 5. ③ list_components 工具

### 5.1 为什么必需

当前工具集里**没有任何枚举组件的手段**（`gan/tools/**` 全量清单已核对：design 5 个 + deep 2 个 + work 若干，无 list）。agent 只能"猜名字"调 `select_component`，失败得到 `"not registered"`。注册表作为**接口**因此完全失效。

### 5.2 归类：`gan/tools/work/common/`（always-on，planner+evaluator 共享）

与 `list_editable` 同层同形（`gan/tools/work/common/list_editable.py:1-5`：角色不知道 allowlist，故提供枚举工具）。task 无 always-on（`assembly.py:38-39`），故 task 拿不到——符合预期（task 不做选择）。

### 5.3 工具形态

```python
# gan/tools/work/common/list_components.py（新增）
"""Work tool: list components available to this role (registry view + validity).

Roles cannot enumerate the registry a priori; ``list_components`` exposes the
merged registry (shared + role-specific) with per-entry validity, what is
currently selected, and component files that exist but are NOT yet registered
(candidates for ``register_component``).
"""
import json
import os

from gan.framework.context import get_access_context, get_design_context
from gan.registries.loader import load_registry_for_role, validate_registry

_SLOTS = ("skills", "eval_points")


def tool_info():
    return {
        "name": "list_components",
        "description": (
            "List components available to YOU: registered skills/eval_points with "
            "validity, which are currently selected, registry problems, and component "
            "files that are NOT yet registered (candidates for register_component)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["skill", "eval_point"]},
            },
        },
    }


def tool_function(kind=None, **kwargs):
    actx = get_access_context()
    if actx is None:
        return "Error: no access context"
    role = getattr(actx, "role", None)
    if not role:
        return "Error: no role in access context"
    broker = getattr(actx, "broker", None)
    code_root = getattr(broker, "repo_root", None)
    rdir = os.path.join(code_root, "gan", "registries") if code_root else None
    cdir = os.path.join(code_root, "gan", "components") if code_root else None

    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
    problems = validate_registry(role, registry_dir=rdir, components_dir=cdir)

    selected = {}
    dctx = get_design_context()
    if dctx is not None:
        for slot in _SLOTS:
            selected[slot] = list((dctx.config or {}).get(slot) or [])

    registered = []
    for e in reg.entries:
        if not isinstance(e, dict):
            continue
        k, n = e.get("kind"), e.get("name")
        if kind and k != kind:
            continue
        registered.append({
            "name": n, "kind": k, "module": e.get("module"),
            "valid": reg.is_valid(k, n), "reason": reg.reason(k, n),
        })

    return json.dumps({
        "role": role,
        "selected": selected,
        "registered": registered,
        "problems": [p for p in problems if p["type"] != "orphan"],
        "unregistered": [p for p in problems if p["type"] == "orphan"],
    }, ensure_ascii=False, indent=2)


op_info = tool_info
op_function = tool_function
```

### 5.4 设计要点

- **与 `list_editable` 同形**：`get_access_context()` → 取 role/broker → `json.dumps(..., ensure_ascii=False, indent=2)`（`list_editable.py:27-44`）。
- **一次调用回答四个问题**：有哪些组件、哪些可用（valid/reason）、哪些已选、哪些文件还没注册。最后一项直接喂给 `register_component`，形成闭环。
- **不返回 description**：注册表的 `description`/`params_schema` **无消费者**（全仓仅 `loader.py:5` docstring 提及）。agent 需要的说明来自 `tool_info()`（由 `load_tools` 注入到 user message，`agent/llm_withtools.py:119-129`），不必从注册表读。这也顺带印证了"注册表实质是 `name→module` 索引"。
- **`selected` 的语义需确认**：`get_design_context()` 在 planner 会话中承载的是**planner 所设计的 task config**（planner 产出 `plan_result["config"]`，`gan/roles/planner.py:102-104,167`）。因此 planner 看到的 `selected.skills` 是 task 的 skills 选择，这符合"planner 设计 task"的语义；但**实现时需确认** planner 会话的 design context 装配点，避免读成 planner 自身的 config。

---

## 6. 三项改动的联动关系

```
                  ┌─────────────────────────────────────────┐
                  │  validate_registry / entry_reason (V1-V4)│
                  └───────┬──────────────┬──────────────────┘
                          │              │
        选择时 is_valid ───┘              └─── 启动期 preflight（零问题，fail fast）
        （已有，自动受益）
                          │
                    提交门控（无新增，宽松）← 放宽触发条件到 components/work
                          ▲
                          │ 会话 patch
   agent ──register_component──► workspace 注册表副本 ──┘
     │
     └──list_components──► 看到 registered / unregistered / problems
```

闭环行为：
1. agent 用 `edit_source` 创建组件文件 → 此刻**未注册**（孤儿）。
2. agent 调 `list_components` → 在 `unregistered` 里看到它。
3. agent 调 `register_component` → 校验通过后写入 workspace 注册表。
4. 会话 patch 提交 → `apply_code_patch` 的 registry 校验发现"新增孤儿数未增加"→ 通过。
5. agent 调 `select_component` → 成功。

**若 agent 跳过第 3 步**：提交门控发现"新增孤儿"→ 打回，并给出可操作提示（"run register_component or remove the file"）。这正是"合法性检验后打回"的落点。

---

## 7. 实施顺序

| 阶段 | 内容 | 依赖 | 可独立验证 |
|---|---|---|---|
| P1 | §3.2 的 V1–V4 + `validate_registry` + `_exposes_tool_api` | 无 | 构造非法条目，断言 `entry_reason` 返回对应 reason |
| P2 | §5 `list_components`（只读，无副作用） | P1（用 `validate_registry`） | 断言输出含 registered/selected/unregistered/problems |
| P3 | §4 `register_component`（写 workspace） | P1 | 断言非法条目被拒、合法条目写入 workspace 注册表 |
| P4 | §3.4b 放宽提交门控 + `registry_report` 纳入三类计数 | P1 | 断言"新增孤儿"被打回、"既存孤儿"不阻塞 |
| P5 | §3.4c preflight 扩展 | P1 | 断言启动期对非法注册表 fail fast |
| P6 | `AGENTS.md` 约定更新 | P3 | 文档与实现一致 |

P1 是其余全部的前置。P2 可先于 P3 落地（只读、零风险），让 agent 至少获得枚举能力。

---

## 8. 验证断言（建议）

| # | 断言 | 对应 |
|---|---|---|
| V-a | `name != module basename` 的条目在注册期即被拒（而非装配后静默不加载） | V1 |
| V-b | `kind=skill` 但 module 在 `eval_points/` 下 → 被拒 | V2 |
| V-c | module 缺 `tool_function` → 被拒 | V3 |
| V-d | `shared.json` 与 `evaluator.json` 同名条目 → 报 duplicate（而非静默遮蔽） | V4 |
| V-e | `gan/components/**` 新增未注册文件 → 提交被打回，且提示含 `register_component` | §3.4b |
| V-f | 已存在的孤儿**不**导致打回（差分判定） | §3.4b |
| V-g | `register_component` 对 `planner.json` 请求被拒（无写权限） | §4.4 |
| V-h | `register_component` 对不存在的 module 被拒，提示先 `edit_source` | §4.3 |
| V-i | `register_component` 重复注册（同名同 module）幂等返回 `Already registered` | §4.3 |
| V-j | `list_components` 输出含未注册文件 | §5 |
| V-k | `task` 角色调 `list_components` 不可用（无 always-on） | §5.2 |

---

## 9. 未决 / 需确认

| # | 事项 | 说明 |
|---|---|---|
| U1 | E5（self-edit 在"task 父节点"的代里是否不可见） | 子代理推理，机制有代码支撑；建议最小实验验证后再决定是否修复 |
| U2 | `list_components` 中 `selected` 的语义 | planner 会话的 design context 指向 task config，需按实现确认 |
| U3 | 是否给 `register_component` 加 `ctx.record` 审计 | 需与 `unregister_component` 对称处理，否则审计不对称 |
| U4 | 注册表 `description`/`params_schema` 的处置 | 当前无消费者；建议要么删除，要么明确未来用途（避免"看起来是富目录、其实是索引"的误导） |
| U5 | B5（task 经 `shared.json` 拿到 bash） | 与本方案正交，但同属"注册表授权面"问题；`register_component` 落地后更需一个显式"不提供给某角色"的表达方式 |
| U6 | E3（`ckpt/code` 丢失时静默重建 + `checkout` 吞异常） | 独立缺陷，建议单独修：至少把静默失败改为显式事件/报错 |
