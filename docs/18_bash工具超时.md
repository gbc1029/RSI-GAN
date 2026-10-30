# 18 — bash 工具 120s 超时归因（run small_1030_095410）

状态：取证归因完成（5 条悖论簇案例按指示停止探查、留档未定案）。
本文只记录事实与推理，不包含修复实现。

## 1. 背景与计数口径

首轮 field audit 报告 polyglot 小规模实验 run
`/opt/rsi-gan/runs/polyglot/small_1030_095410` 存在 13 次 bash 工具 120s 超时。
复核后的计数口径：

- **主链 11 条**（task.jsonl 直接命中）：gen0 = 7，gen1 = 4，gen2 = 0；
- eval 轮 2 条（`work/eval_outer1_s1/.../chat_history_{robot-name,resistor-color-trio}.jsonl`）；
- 13 = 11 主链 + 2 eval，两条口径各自成立。

成本影响：11 × 120s ≈ **22 分钟纯等待**，另有每次超时后 2–4 次重试调用；
gen0 五题 220–363s、gen1 dominoes 528.5s（预算 1200s）。gen2（rust/cpp）零超时。

## 2. 工具协议模型（归因基础）

工具本体 `agent/tools/bash.py`（task 侧经 `gan/components/task/skills/bash.py`
re-export，注册于 `gan/registries/task.json`）。run 的冻结副本
`ckpt/code/agent/tools/bash.py` 与仓库 HEAD **逐字相同**，协议模型无失真。

每次 `tool_function_call`：

1. 新建 `BashSession` → `asyncio.create_subprocess_shell("/bin/bash -i")`
   （实际进程树 `/bin/sh -c /bin/bash -i`；`preexec_fn=os.setsid` 落在 sh 包装进程上，
   真 bash 是孙进程）；stdin/stdout/stderr 全部为管道；
2. 向 stdin 写入 `command + "; echo '<<exit>>'\n"`（哨兵胶水）；
3. 每 0.2s 轮询 `stdout._buffer`（asyncio 私有缓冲）查找哨兵 `<<exit>>`；
4. 120s 无哨兵 → `_timed_out=True` → ValueError →
   `Error: Timed out: bash has not returned in 120.0 seconds and must be restarted.`
   （**stderr 在此路径被丢弃**）。

调用侧环境：`domains/harness.py` 用 `ThreadPoolExecutor(max_workers=5)` 在
**单进程 5 线程**内并发跑 qid 会话（gen0 实测 4–5 个 bash 调用同时在飞）；
所有工具调用经 `agent/llm_withtools.py:process_tool_call` 的
**模块级共享 `ThreadPoolExecutor(max_workers=4)` + `ctx.run` +
`redirect_stdout/redirect_stderr` 捕获代理**派发。

## 3. 案例总表

| # | row (gen) | qid | 命令形状 | 归因 |
|---|---|---|---|---|
| 1 | 61 (0) | java__book-store | `python3 - <<'EOF'…EOF`（裸 EOF 结尾） | ✅ 工具派发缺陷 |
| 2 | 107 (0) | javascript__complex-numbers | `cd … \|\| cd $(find / …)` | ✅ agent 出错（无界 find /） |
| 3 | 113 (0) | javascript__complex-numbers | heredoc+`timeout 30 node` | ❓ 悖论簇 |
| 4 | 137 (0) | javascript__rest-api | 多行 `node -e "…"` | ❓ 悖论簇 |
| 5 | 140 (0) | javascript__rest-api | heredoc+`timeout 30 node` | ❓ 悖论簇 |
| 6 | 166 (0) | python__list-ops | 多行 `python -c "…"`（python 缺失） | ❓ 悖论簇 |
| 7 | 169 (0) | python__list-ops | 同上 + `timeout 30` 双保护 | ❓ 悖论簇 |
| 8 | 173 (1) | javascript__rest-api | `node --check … && node -e "…"` | ❓ 悖论簇 |
| 9 | 204 (1) | python__dominoes | `… && python - <<'EOF'…EOF`（裸 EOF） | ✅ 工具派发缺陷 |
| 10 | 213 (1) | python__dominoes | `timeout 30 python3 - <<'EOF'…EOF`（裸 EOF） | ✅ 工具派发缺陷 |
| 11 | 225 (1) | python__dominoes | `timeout 20 python3 -c … <<'PYEOF'…PYEOF`（裸 PYEOF） | ✅ 工具派发缺陷 |

row 9/10/11 是同一 qid 的连续重试（同形状盲试 3 次）。

## 4. 已定案一：bare-EOF heredoc = 工具派发缺陷（4 条）

### 4.1 机制（逻辑必然，非概率事件）

命令以裸 heredoc 终止符结尾（无尾换行）时，工具把胶水拼在同一行：

```
… <<'EOF'
<body>
EOF; echo '<<exit>>'        ← 这一行不再是终止符
```

bash 要求终止符独占一行 → heredoc 永远收不完 → bash 阻塞在 stdin 读
（stdin 由工具持有、永不 EOF）→ 哨兵永不出现 → 120s 工具超时。
**这是协议 + 输入形状的必然结果**，与负载、并发、环境无关。

### 4.2 证据链（判定为工具侧的依据）

1. **命令本身合法**：同一字节串交给参考语义 `bash -c` 执行，rc=0、毫秒级、
   输出正确（row 61 实测：`30.0 / 0.0 / 24.0`）。工具文档亦未要求命令
   以换行结尾——坏的是传输方式，不是输入。
2. **执行层无错**：bash 自身按规范等待终止符；进程树、fd、syscall 全部正常。
3. **原代码 + 原字节确定性复现**：用 run 的 `bash.py` 与轨迹原始命令，
   宿主 121.1s 挂（工具自身 deadline）；沙箱内 12s（补丁 deadline）每轮必挂；
   最小形状 S7 每轮必挂，而 S1（heredoc + 尾随命令）稳定返回——
   **失败边界与机制预测精确吻合**。
4. **agent 的防御以机制预测的方式失效**：row 213/225 加了 `timeout 30/20`
   仍 120.4s 挂——bash 根本没进入执行阶段，`timeout` 无从生效。
   若归因"命令跑得久（agent 错）"，timeout 会把挂死封顶在 ~30s。排除。
5. **类别归属**：命令语义无害（agent 无错）+ 执行层无错 +
   错在派发层"同行拼接哨兵"的传输契约 ⇒ **bash 工具解析 agent 输出的缺陷**。

agent 侧加重因素：dominoes 同形状连试 3 次未换方法（row 204→213→225）。

## 5. 已定案二：CASE2 = agent 出错（无界 find /）

row 107：`cd javascript/exercises/practice/complex-numbers 2>/dev/null ||
cd $(find / -type d -name complex-numbers -path '*practice*' … | head -1)`。

- polyglot 工作区是**平面布局**（`/workspace/scratch/…`），exercism 式路径不存在
  → 首个 `cd` 必败 → `find /` 全盘扫描，目标不存在 → 无提前 SIGPIPE；
- 沙箱全盘 walk 实测 ~32s（row 149→150，带剪枝 + 有匹配的对照）；
  无剪枝 + 无匹配 + 4–5 会话并发 I/O（javac 编译 26–28s 在飞）下超过 120s；
- **沙箱内确定性复现**：12s deadline 下三轮全部挂死；
- 工具本身对该命令无任何解析问题——哨兵协议工作正常，命令真的在跑。

类别：agent 调用出错（无保护的全盘扫描兜底），并发 I/O 是放大器。

## 6. 悖论簇（5 条：row 113/137/140/166/169）——未定案，按指示停止探查

特征：语法完好、多处带 `timeout 30` 硬上限、或 `python` 缺失本应 `&&` 秒级短路；
**直接对照**：row 175（单行 printf + `&& timeout 60 python`）秒回、
row 219（多行 `python3 -c`，二进制存在）0.2s 返回。

### 6.1 已排除假说（全部有数据）

| 假说 | 排除证据 |
|---|---|
| CRLF 污染（`EOF\r` ≠ `EOF`） | gen0 命令 0 个 `\r`；gen1 的 3 处 `\r` 全在 write_file 内容 |
| `!` 历史扩展 / `${` / 反引号 | 11 条命令全部为 0 |
| 解自身死循环 | 同一测试逻辑经 printf 写文件 + `timeout 30 node`（row 179→186）秒回且输出正确 JSON |
| `python` 缺失 → 挂起 | row 176 实证 `timeout: failed to run command 'python'` 秒级失败；S5/S6 形状 2.0s 返回 |
| find / 慢 | 仅 CASE2 含 find；其余 4 条无 |
| 解释器差异 | venv 与系统同为 3.12.3；无 uvloop；仅 1 个 distutils .pth |
| 超时泄漏干扰 | 3 次强制超时预热后，干净形状仍 0.2s 返回 |
| fork 死锁（`preexec_fn`+多线程，CPython 明文雷区） | 宿主 + 沙箱共 48 次高负载并发 fork（含 malloc 风暴兄弟线程）零意外挂死；/proc 抽样未见 exec 前卡死子进程 |

### 6.2 复现矩阵（全部干净；仅两个确定性案例挂）

| 实验 | 规模 | 悖论簇结果 |
|---|---|---|
| 宿主串行 + 最小形状 S1–S6 | — | 0.2–2.8s 返回 |
| 忠实沙箱串行（setpriv 外层 + bwrap `--unshare-all` + 合成挂载 + HOME=/tmp） | 8 案例 | 0.2–0.4s 返回 |
| 沙箱 5 线程并发（复刻 harness 线程模型） | 3 轮 × 8 | 全部返回 |
| 泄漏预热（3 次强制超时后再跑） | — | 全部返回 |
| malloc 风暴兄弟线程（LLM JSON 解析替身） | 又 24 fork | 全部返回 |

每轮唯一挂死的只有两个确定性案例（CASE2 find /、S7 bare-EOF）——沙箱重建忠实。

### 6.3 剩余唯一未测层（留档，不再探查）

真实调用链比复现多一层：`process_tool_call` 把工具提交到**共享 4-worker
executor**，在池线程内经 `ctx.run` + `contextlib.redirect_stdout/redirect_stderr`
捕获代理运行。两个未测变量：(a) redirect 换的是**进程全局** `sys.stdout/stderr`，
4 个池线程并发换全局对象存在竞态；(b) 工具跑在复用池线程而非 harness 线程。

若未来重启探查，建议：按 `process_tool_call` 真实路径做 dispatch 保真复现；
仍干净则在真 harness 加一次性探针（记录 fork/写入瞬间的线程与锁状态）实跑取证。

## 7. 环境事实：`python` 为什么缺失

**结论：不是沙箱裁剪掉了 python，而是 venv 的 bin 目录不在子进程 PATH 上。**

成因链：

1. 驱动以 `venv_nat/bin/python scripts/run_gan.py` **直接执行 venv 解释器，
   未激活 venv** → 驱动进程 PATH 只含系统目录；
2. `_task_safe_env`（`task_execution.py:75` `_TASK_ENV_NAMES`）把 `PATH`
   **原样透传**给 task 子进程 → 子进程 PATH = 系统目录
   （`/usr/local/bin:/usr/bin:/bin:…`）；
3. venv 本体以 `python_prefix` 只读绑进沙箱（解释器可用），但
   `venv_nat/bin` **不在 PATH**；且 venv_nat 是 `--copies` venv——
   `venv_nat/bin/python` 是真实存在的 8MB 文件（部署约束要求 --copies，
   见 AGENTS.md "symlink-based venvs fail the trusted-interpreter realpath check"），
   即"文件存在、只是不在查找路径上"；
4. Ubuntu 系统本身只有 `python3`，无裸 `python` → `python` 必然 command not found。

实锤记录（gen0/gen1 轨迹）：`timeout: failed to run command 'python'`（row 176）、
`bash: python: command not found`（row 208）；`python3 -m py_compile` 正常
（row 211 `COMPILED`）；node v18.19.1 可用；`timeout`/coreutils 可用。

**矛盾点**：主链任务简报（task brief 模板）明文指示
"提交前用 bash 自检：python -m py_compile …必须通过"——框架自己的简报
要求了环境无法满足的命令名。agent 实际行为是自适应改用 `python3`
（代价：row 175/207 等若干次快速失败，烧了少量调用预算，未造成超时）。

## 8. 工具内部缺陷清单（与归因无关、独立成立）

1. `stop()` 从不被调用——每次调用泄漏一个孤儿 `bash -i`（setsid 化，等 stdin EOF）；
2. `terminate()` 只发 SIGTERM 且打在 `sh -c` 包装进程上（真 bash 是孙进程，
   信号不达）；超时路径实际也不调用它；
3. 描述宣称 "State is persistent across command calls"，实现是每次全新会话
   （契约矛盾，方向与 DSH 相反）；
4. stderr 只在 stdout 循环结束后才取，期间无界积压在内存；
5. 哨兵碰撞：输出含字面量 `<<exit>>` 时**提前返回并截断输出**
   （S8 实证：返回 `before`，`after` 与第二行丢失）——产出错误结果而非挂死；
6. 超时路径丢弃已收集的 stderr（本次排查的直接受害者：
   挂死会话的现场证据不可恢复）。

复用/健壮性顺带观察：轮询读 `stdout._buffer` 是 asyncio 私有 API；
每调用一次 `asyncio.run()`，超时泄漏的 transport 在 `__del__` 里触发
`RuntimeError: Event loop is closed`（管道 fd 泄漏在子进程内）。

## 9. 复现过程记录（供复核）

环境基线：本机 = run 同机（WSL Ubuntu，bash 5.2.21，/usr bin 只读绑定同源）；
`python` 在宿主同样缺失，`python3`/`node`/`timeout` 存在。

| 实验 | 脚本/方式 | 结果 |
|---|---|---|
| R1 宿主原始命令复现 | `venv_nat` python + `agent.tools.bash`（`_timeout` 类属性补丁无效→仍 120s） | CTRL OK 2.8s；CASE1 HUNG 121.1s |
| R2 最小形状判别（S1–S8） | 实例级补丁（`__init__` 包一层设 `_timeout=12`） | S1–S6 OK 2.0–2.8s；S7 HUNG；S8 截断实证 |
| R3 沙箱串行 | setpriv(995/985) → bwrap（`--die-with-parent --new-session --unshare-all --unshare-net --cap-drop ALL`，/usr 系只读绑定 + /etc + `--proc/--dev/--tmpfs`，run_dir 绑 /workspace，HOME=/tmp） | CTRL/R113/R137/R140/R166/R169 OK 0.2–0.4s；R107 HUNG；S7 HUNG |
| R4 沙箱 5 线程 | 同沙箱 + `driver.py threads 3` | 24 fork 全净，仅两个确定性挂 |
| R5 泄漏预热 | 3 次强制超时 → 再跑干净形状 | 全部 0.2s 返回 |
| R6 malloc 风暴 | 4 兄弟线程 `json.loads` 200KB 死循环 + 案例线程 fork | 24 fork 全净 |
| R7 `bash -c` 对照 | `subprocess.run(["bash","-c",row61 命令])` | rc=0，毫秒级，输出正确 |

一次性脚本位于 `/tmp/repro_bash_tool.py`、`/tmp/repro2_minimal_shapes.py`、
`/tmp/sbx_repro/repo/driver*.py`（临时目录，核心构造已内嵌上文）。

## 10. 修复方向备忘（未实现）

1. **派发契约**：`command` 经 `bash -c`（或写临时脚本后执行）注入，
   替代"交互 shell + 同行拼接哨兵"——根除裸 EOF/引号续行类挂死；
2. **哨兵唯一化**（随机 per-call token）+ 超时路径保留 stderr；
3. **会话生命周期**：调用结束 `stop()`（或 finally 关闭管道）；
   `terminate()` 改为对进程组发 SIGTERM→SIGKILL 升级；
4. **文档对齐**：删除或兑现 "State is persistent" 承诺；
5. **简报/环境对齐**：task brief 的 `python -m py_compile` 改为 `python3`，
   或在子进程 PATH 上补 venv bin（二选一，倾向前者——环境事实是稳定的）。

## 11. 修复实施记录（本提交）

### 11.1 bash 工具一次性化（`agent/tools/bash.py` 重写）

- 每次调用一个 `subprocess.Popen(["/bin/bash","-c",command])`：进程退出即
  完成信号，哨兵机制整体退役（裸 EOF 类结构性消失、碰撞类消失）；
- `stdin=DEVNULL`（`read` 得 EOF 不挂等）；`start_new_session=True` +
  超时 `killpg` TERM→5s 宽限→KILL（旧 `setsid` 的意图第一次被真正使用，
  `javac`/`find` 不再孤儿化）；
- `communicate()` 增量读取替代 0.2s 全量轮询（二次方成本消失）；同步阻塞
  实现（派发层本在 executor 线程内调用），删除 `asyncio`/`BashSession`/
  `filter_error`（grep 确认无外部 importer，re-export 仅引
  `tool_info/tool_function`，零改动）；
- 新输出约定：非零退出码尾部附 `[exit N]`；超时错误文本携带
  stdout/stderr 尾部（各 ≤2000 字符）——缺陷⑥修复，残余挂死可现场诊断；
- 描述重写：删除 "State is persistent"（假）与 "apt and pip mirror"
  （沙箱无网，假）；写明一次性语义、`[exit N]`、120s 整组终止；
- §6 悖论簇随传输更换自然退役，不再作为活跃问题跟踪。

### 11.2 简报 `python` → `python3`：落点修正

原计划假定简报文本在框架模板——**实施时定位推翻**：`python -m py_compile`
不在仓库任何框架/域模板中，而是 **planner 在 run 内撰写的 task design
config**（`ckpt/design/task/<gen>/config.json`，每代 1 处；run 工件不改）。
仓库侧落点改为**知识库**：新增
`gan/components/task/knowledge/environment.md`（该目录此前为空）——
python3-only、bash 一次性语义、沙箱无网三条环境事实；knowledge 每次
materialize 进 task 沙箱，planner 撰写 task prompt 时亦可经访问通道读取。
Dockerfile/setup 脚本里的 `python -m pip` 属镜像构建环境，不改。

### 11.3 验证结果

- 新增 `scripts/local/test_bash_tool_one_shot.py`（本地，不入库）：**16/16**，
  含裸 EOF heredoc、heredoc+尾随命令、多行 `-c`（row 219 形状）、超时预算
  补丁下 3.2s 返回且 `pgrep` 证实进程组无幸存者、stdout 尾部保留、
  哨兵字面量穿透、`!` 安全、`stdin=DEVNULL`、10 万行输出、py_compile 冒烟；
- `run_tool_regression.py` 七套件全 PASS；`test_frozen.py` 全 PASS；
- 实跑观察项（留待下次 GAN run）：task.jsonl 无旧超时文案；`[exit N]`
  出现；heredoc 自检命令正常完成。

### 11.4 提交

单次提交：`agent/tools/bash.py` + `gan/components/task/knowledge/environment.md`
+ 本节。回滚 = revert 单提交，无数据/格式迁移。
