# HyperAgents(DGM-H) 部署记录

> 目的：在受限网络（深信服 aTrust 企业准入管控）的 Windows + WSL2 机器上，以复现/部署为目标搭建 facebookresearch/HyperAgents（DGM-H）。
> 日期：2026-09-05
> 结论：**host 编排层与 `hyperagents:base` GPU 容器均已成功部署并验证**；实际 LLM 自改进循环因未配置 API Key 未运行。

---

## 一、环境最终形态

| 项 | 值 |
|---|---|
| 宿主 OS | Windows（build 26200 / 25H2） |
| WSL | WSL2 Ubuntu 24.04（内核 6.6.87.2），systemd=enabled |
| 容器运行时 | **Docker Desktop 4.89.0**（Engine 29.7.2，Windows 侧，WSL 内 `docker` CLI 直通其 daemon） |
| GPU | RTX 4070 8GB，Driver 592.82，CUDA 13.1（WSL/容器内 GPU 直通正常） |
| 网络管控 | 深信服 aTrust（fake-IP DNS + TUN 策略路由），拦截大量国际/大流量 TCP |
| 代码位置(WSL) | `/root/HyperAgents`（WSL 原生盘） |
| host venv | `/root/HyperAgents/venv_nat`，Python 3.12.3 |
| 容器镜像 | `hyperagents:base`（标签 `docker.io/library/hyperagents:base`） |

---

## 二、关键网络结论（为何不能用官方直连）

1. 宿主 aTrust 客户端（`aTrustService` Running）从驱动层管控流量：
   - WSL VM **无 TCP 出站**（NAT 转发被阻，NAT/mirrored 皆不通）；仅有 `localhost` 与 ICMP 可达。
   - 宿主进程可访问部分站点：`pypi.org`/清华（200），但 **`hub.docker.com`/`registry-1.docker.io` → 000**（被拦）。
2. 因此镜像拉取/依赖安装需走**国内镜像源**，且直连 Docker Hub 拉大层易中途断连（`unexpected EOF`）。

### 可用通道（本机实测）
- 清华 PyPI：`https://pypi.tuna.tsinghua.edu.cn/simple`（可达，快）
- 清华 Ubuntu：`http://mirrors.tuna.tsinghua.edu.cn/ubuntu/`（可达，快）
- GitHub 加速：`gh-proxy.com`、`ghfast.top`、`ghproxy.net`（仅代理 **release/archive 下载**，**不支持 `git clone` 智能协议**）
- Docker Hub：登录后（`docker login`）直连慢但可下载大层；registry 镜像源 `docker.m.daocloud.io` 可达(401)

---

## 三、部署步骤与产物

### 1. 基础镜像
```bash
docker login                        # 用户手动完成
docker pull nvidia/cuda:13.0.0-devel-ubuntu22.04   # 3.94GB；断连后用其余层缓存续传
```

### 2. host 编排层（WSL）
- apt 切清华镜像（`/etc/apt/sources.list.d/ubuntu.sources`），安装系统依赖。
- git 将 github URL 重写到 gh-proxy（`git config --global url."https://gh-proxy.com/https://github.com/".insteadOf ...`），用于仓库克隆。
- 克隆仓库至 `/root/HyperAgents`。
- venv + 安装依赖：`requirements.txt`、`requirements_dev.txt`（**跳过 git+ VCS 行**，见适配表）。
- 验证：`generate_loop/meta_agent/task_agent/utils.gl_utils/agent.base_agent/domains.harness` 全部 import 成功。

### 3. 容器镜像 `hyperagents:base`（对 Dockerfile 做环境补丁后构建成功）
补丁内容详见表四。验证结果：
- `nvidia-smi` in container：Driver 592.82 / CUDA 13.1 ✓
- `torch 2.14.0+cu130`，`torch.cuda.is_available()=True` ✓
- `/hyperagents` 仓库就位，核心模块 import 全 OK ✓
- `fix-cuda.sh` ENTRYPOINT 自动修复 libcuda.so 软链 ✓

---

## 四、为受限网络所做的环境适配（相对上游的偏离）

| 层 | 上游 | 本机调整 | 原因 |
|---|---|---|---|
| apt(host) | archive.ubuntu.com | 清华镜像 | 官方源 000 |
| git | github.com | 重写到 gh-proxy | github 443 被拦 |
| pip(host/容器) | pypi.org | 清华 `PIP_INDEX_URL` | pypi.org 000 |
| pip(容器) | `git+https://github.com` 依赖 | 构建时过滤 `git+` 行 | 容器内 github 不通 |
| apt(容器) | archive/security.ubuntu.com | 清华镜像（security 并入主 `.../ubuntu/` 基址） | 传输中断、Security 独立前缀 404 |
| 容器 RUN | `pip install -e proofgrader_repo` | 跳过 | `proofgrader_repo` 目录由 `setup_initial.sh`(imo) 生成，仓库不含则必失败 |
| 容器 RUN | `python -m domains.balrog.scripts.post_install` | 跳过 | 需外网，非目标领域 |
| Dockerfile 原件 | — | 保留为 `Dockerfile.orig`、`.dockerignore` | 留底 + 精简上下文(6.6GB→10KB) |

> 影响说明：跳过 git+ 依赖意味着 balrog/genesis 领域在容器/venv 中不可用；proofgrader 相关 imo_proof 需要时需先跑 `domains/imo/setup.sh` 生成并在容器重建。pypi/apt 改镜像源不影响用户代码逻辑。

---

## 五、当前状态与未达成项

### 已达成（部署/构建目标）
- [x] host 编排层 + venv + 依赖 + 模块导入验证
- [x] `hyperagents:base` GPU 容器构建 + 全功能验证
- [x] 网络受限环境的镜像源/代理适配，原始 Dockerfile 留底

### 未执行（因无 API Key / 网络 / 用户决策）
- [ ] `generate_loop.py` 实际自改进循环运行
- [ ] `setup_initial.sh` 领域初始化（paper_review 基线等）
- [ ] polyglot 领域：需 `clone SWE-bench`（github 被拦）+ `domains/polyglot/docker_build.py` 构建多语言评测镜像
- [ ] balrog / genesis / imo 领域（依赖被跳过的 git+/proofgrader）

---

## 六、复现/运行指引

```bash
# WSL 内
cd /root/HyperAgents
source venv_nat/bin/activate

# 为容器运行（Docker Desktop 提供 daemon）
docker run --rm --gpus all hyperagents:base nvidia-smi

# 若要跑实际自改进循环（需 API Key + LiteLLM 模型名映射，见仓库调研）
# python generate_loop.py --domains <domain> --max_generation 1 --eval_samples 10
```

## 七、给后续/IT 的建议
- 若希望完整复现，需在 aTrust 策略放行：`hub.docker.com`、`registry-1.docker.io`、`github.com`、`raw.githubusercontent.com`、`nvcr.io`、`download.pytorch.org` 的 WSL/容器流量（参考《WSL网络放行配置手册.md》）。
- 保留 `Dockerfile.orig` 与 `.dockerignore`，后续若网络放开可对照还原上游构建。

---

## 八、polyglot 领域配置（本期）

polyglot 需要两份 github 数据（本机 github 443 被拦，均改走 gh-proxy **tarball** 下载，`git clone` 智能协议不可用）：

| 组件 | 来源 | 处理 | 状态 |
|---|---|---|---|
| `swebench` 包 | `princeton-nlp/SWE-bench` @ `dc4c087…` | tarball 解压至 `domains/polyglot/SWE-bench`，`pip install -e .` | ✓ 安装并 `import swebench.harness.utils` OK |
| 数据集 | `Aider-AI/polyglot-benchmark` | tarball 解压至 `domains/polyglot/polyglot-benchmark` | ✓ |
| 元数据 | 由 `prepare_polyglot_dataset` 生成 | 6 语言 225 任务 → `polyglot_benchmark_metadata.json` | ✓ |
| TestSpec | `get_test_specs_from_dataset` | 225 实例构建成功（无需 API/镜像） | ✓ |

> 关键坑：github `git clone` 不适用于 gh-proxy（其仅代理 release/archive 下载）；故用 **`/archive/<sha或tag>.tar.gz`** 下载后解压。数据集提取顶层为 `polyglot-benchmark-main`，需把语言目录归入 `domains/polyglot/polyglot-benchmark/`。`prepare_polyglot_dataset` 会为 225 个练习目录逐一 `git init`+commit，需先设置 git 身份（`git config --global user.email/name`）。

### 尚未执行（需 Docker Hub 语言基础镜像，网络受限且较重）
- spring：`domains/polyglot/harness.py` 实际评测需构建 base/env/instance 镜像（`docker_build.py` 拉取 `python/rust/go/node/gcc/gradle` 等 Docker Hub 镜像）。因 Docker Hub 限速/中断风险，本期未构建图像，仅完成数据集与 TestSpec 层配置。

## 九、LLM 接入（Zhipu GLM，OpenAI 兼容，本期）
- `agent/llm.py` 用 **litellm**；`agent/base_agent.py` 默认 `model=OPENAI_MODEL`。
- litellm 的 `openai/` provider 读取 **`OPENAI_API_BASE`**（自定义兼容端点）与 **`OPENAI_API_KEY`**。
- 配置：`/root/HyperAgents/.env`
  ```ini
  OPENAI_API_BASE=https://open.bigmodel.cn/api/paas/v4
  OPENAI_API_KEY=<由用户填写>
  ```
- 最小化调用：`model="openai/glm-4.7-flash"`（`get_response_from_llm`）。litellm 会 POST 到 `{OPENAI_API_BASE}/chat/completions`。
- 注意：该 API 响应慢/有限流，测试仅做**单次调用**（`max_tokens=64`）。

## 本期问题与解决（问题 → 解决方案）
| 问题 | 解决方案 |
|---|---|
| host venv `pip install -r requirements.txt` 卡死（git+ 依赖经 gh-proxy 克隆挂起 20+min） | gh-proxy **不支持 git 智能协议**；过滤 `git+` 行，只装纯 pypi 核心依赖（banroj/genesis 非目标） |
| `docker build` 第一步 apt 就失败（archive.ubuntu.com 传输被掐、包索引不完整） | 容器内 apt 源改清华镜像；**security 也并入主 `.../ubuntu/` 基址**（独立 `ubuntu-security` 前缀 404） |
| 构建上下文 6.6GB（误含 venv_nat/.git）| 新增 `.dockerignore`，上下文降至 ~10KB |
| 容器内 pip 走 pypi.org 不通 | Dockerfile 设 `PIP_INDEX_URL`/`PIP_TRUSTED_HOST` 指向清华 |
| 容器 `pip install -e proofgrader_repo` 必然失败（目录由 `setup` 生成、仓库不含）| 补丁版跳过；原 Dockerfile 留底 `Dockerfile.orig` |
| homelab 网络不稳定导致 `base 镜像` docker pull 中途 `unexpected EOF` | 登录 Docker Hub 后重试（已缓存层续传）成功 |
| 容器 GPU 无 libcuda.so | 复用仓库自带 `fix-cuda.sh` ENTRYPOINT，自动建软链（已验证） |

## 十、全面测试（本期）—— 管线各层验证

### 预置
- 镜像重命名：`docker tag hyperagents:base hyperagents:latest`（管线 `generate_loop.py`/`run_eval.py` 用 `image_name=REPO_NAME="hyperagents"`，即 `hyperagents:latest`）。
- 容器 exec 冒烟：`docker run hyperagents:latest python -c "import generate_loop, meta_agent, task_agent, domains.harness, utils.gl_utils"` → **OK**。

### 测试结果矩阵
| 层 | 入口 | 结果 |
|---|---|---|
| LLM 单点 | `get_response_from_llm("Reply with exactly: PONG", model="openai/glm-4.7-flash")` | ✓ — 返回 `PONG` |
| LLM 直接(诊断) | `litellm.completion`（较新 litellm 返回 `ModelResponse`，非 dict） | ✓ — 见 token 结论 |
| Agent 工具循环 | `chat_with_agent(..., model="openai/glm-4.7-flash", tools_available="all")` | ◐ — 机制在容器内运行、bash 工具被调用；但 LLM 调用**被 Zhipu 429 限流**中断 |
| 容器+镜像 | `docker run hyperagents:latest` + 导入/exec | ✓ |
| polyglot 数据集 | `prepare_polyglot_dataset` + `get_test_specs_from_dataset` | ✓ — 225 实例 TestSpec 构建 |
| polyglot 实评测 | `domains/harness`（容器内） | ✗ — 需构建 base/env/instance 镜像（Docker Hub 语言镜尚未拉取） |

### 关键问题与结论（本轮新）
1. **glm-4.7-flash 是推理(reasoning)模型**：
   - 回复走 `message.reasoning_content`（思维链）+ `message.content`（最终答案）。
   - 若 `max_tokens` 太小（如 64），推理就把预算耗尽 → `finish_reason=length` 且 `content=""`。
   - 管线 `MAX_TOKENS=16384` 充足；把测试 `max_tokens` 提到 1024 后正常返回 `PONG`（推理124+文本）。
2. **litellm 1.74.9 返回 pydantic `ModelResponse`**（非 dict）：`agent/llm.py` 用订阅 `response['choices'][0]['message']['content']`，在 1.74.9 下**仍可用**（实测返回 PONG）——模型层兼容性 OK。
3. **Zhipu 429 限流（code 1305，“该模型当前访问量过大”）**：`get_response_from_llm` 的 `@backoff.on_exception` 只捕获 `requests.RequestException/JSONDecodeError/KeyError`，**不捕获 `RateLimitError`** → 限流时会立即抛出而非重试。对限流 API 若需稳健，建议把 `litellm.exceptions.RateLimitError` 纳入 backoff。属外部条件而非管线缺陷。
4. **agent 工具循环**：`llm_withtools.chat_with_agent` 采用**文本协议**工具调用（模型输出 `<json>{tool_name,tool_input}</json>`），不依赖 OpenAI 原生 function-calling，glm 只需遵循该格式。机制已确认可在容器内触发 bash 工具。

### 目前“整条 generate_loop”尚不能完整跑通的硬边界（如实记录）
- **polyglot 实评测需评测镜像**：`domains/polyglot/harness.py`/`run_evaluation.py` 需 base/env/instance 镜像，来源于 Docker Hub（python/rust/go/node/gcc/gradle）。受 aTrust/限速与 Docker Hub 匿名限流影响未构建。构建这些镜像属后续较重的网络相关工作。
- **API 限流**：glm-4.7-flash 当前 429 频繁，多轮 agent 循环（meta-agent 开环）在当前限流下不稳定。

> 说明：以上验证覆盖了 LLM 层、agent 工具循环、容器、数据集等管线核心环节；“整条自改进循环（generate_loop 全流程）” 的完整跑通受限于【评测镜像 + API 限流】两点，非代码/环境配置问题。

---

## 十一、DGM-H 原代码流程测试（新 API：az.gptplus5.com / gpt-4o-mini）

> 日期：2026-09-13　范围：**仅原 DGM-H 代码，不含新 GAN 设计**。目的：验证换用新 API 后原流程能否跑通。

### 11.1 API 配置
- `.env`：`OPENAI_API_BASE=https://az.gptplus5.com/v1`，`OPENAI_API_KEY=<用户配置>`（该代理为 new-api，需有效 token；无 token 返回 `401 Invalid token`）。
- `domains/paper_review/utils.py`：`MODEL` 由 `"gpt-4o"` 改为 `"gpt-4o-mini"`（唯一模型配置改动，为匹配该代理模型）。

### 11.2 测试 0：API 连通
- `litellm.completion(model="gpt-4o-mini", ...)` → 返回 `OK`，实际模型 `gpt-4o-mini-2024-07-18`。✅

### 11.3 测试 1：原代码 task-agent 评测流程（`domains.harness` → `domains.report`）
- 命令：`bash scripts/test_dgmh_paper_review.sh 2`（paper_review，2 样本）。
- 产物：`outputs/dgmh_smoke_train_0/predictions.csv`、`report.json`（`overall_accuracy`）。
- 结果：**跑通** ✅；但 accuracy=0。
- 现象：初始通用 `task_agent.py` + gpt-4o-mini 返回 `{"response": <整篇论文 JSON>}`（把输入当成答案），未输出 `accept/reject`。
- 判定：**管道（harness→TaskAgent→chat_with_agent→litellm→predictions→report）完全正常**；低分源于"种子 agent 弱"——这正是 DGM-H 靠 meta-agent 迭代改进的对象，非流程缺陷。

### 11.4 测试 2：原代码 meta-agent 自改进步骤（`run_meta_agent.py`）
- 在**一次性仓库副本**上运行（避免污染主仓库），以副本基线 commit 作为 `--base_commit`。
- 命令要点：`python run_meta_agent.py --model gpt-4o-mini --repo_path <副本>/ --evals_folder <smoke输出> --git_dir <副本> --base_commit <HEAD> --outdir <agent_output>`。
- 结果：**跑通** ✅
  - `MetaAgent.forward` 工具循环正常；gpt-4o-mini 以 `editor`/`bash` 完成 **40 次工具调用**（触及 `max_tool_calls` 上限）。
  - `run_meta_agent.py` 产出 `model_patch.diff`（2058 B）。
- 观察：diff 仅含 `__pycache__/*.pyc`（仓库 `.gitignore` 只忽略 `outputs/`、`outputs_os/`，未忽略 `__pycache__`；且 gpt-4o-mini 40 步内未做实质源码修改）。机制跑通，输出质量受模型能力限制。
- 踩坑（测试脚本）：`run_meta_agent.py` 不会自动创建 `--outdir`；不预先 `mkdir` 会在最后写 `model_patch.diff` 时抛 `FileNotFoundError`（`generate_loop` 正常会预先创建该目录）。

### 11.5 结论
- 原 DGM-H 两大核心机制（**task-agent 评测** 与 **meta-agent 改写→patch**）在新 API（gptplus5 / gpt-4o-mini）上**均可运行**。
- 未执行：完整 `generate_loop.py` 编排（需先生成 `outputs/initial_paper_review_*` 基线，再跑容器化 meta+eval）。用户决定**到此为止**。

### 11.6 测试期间改动
| 项 | 内容 |
|---|---|
| `.env` | 切换为 `https://az.gptplus5.com/v1` + 用户 Key |
| `domains/paper_review/utils.py` | `MODEL="gpt-4o-mini"` |
| 新增 | `scripts/test_dgmh_paper_review.sh`（原始流程一键测试） |
| 产物 | `outputs/dgmh_smoke_train_0/`（predictions.csv + report.json + agent_evals/chat_history_*.md） |
| 清理 | 一次性仓库副本 `/root/dgmh_meta_test` 已删；临时脚本已清 |

### 11.7 完整 generate_loop 的已知前置（若后续要跑）
1. 生成基线：`python -m domains.harness --domain paper_review --run_id initial_paper_review_filtered_100_train_0 --subset _filtered_100_train --num_samples N`（另 val 同理），供 `setup_initial_gen(copy_eval=True)` 拷贝。
2. 容器内 API Key：`generate_loop` 把仓库（含 `.env`）挂载为 `/hyperagents`，容器内 `load_dotenv()` 可读到 Key（依据 `utils/docker_utils.build_container` 的 volume 挂载）；`build_container` 对已存在镜像 `hyperagents:latest` 跳过重建。
3. 容器网络/GPU、`domains/harness` 在容器内执行、`apply_diffs_container` 过滤 `domains/` 等均已在第十章验证基础可用。