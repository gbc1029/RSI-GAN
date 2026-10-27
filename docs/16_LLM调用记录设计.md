# 16 — LLM 调用记录（非流式 sidecar）设计与实现记录

> 批次：2026-10（非流式补全）。对应探查：网关实测 7 组探针（正常/max_token 截断/
> 流式 chunk 结构/超时/错误返回），结论沉淀于本文 §2 与 §6。
> 后续方案（流式：TTFT / 分阶段 / 中断部分现场）的接入点已在设计中预留，见 §9。

## 1. 背景与目标

LLM 调用的回答超时、网关断连会在旧机制下**不留任何调用级痕迹**：

- `agent/llm.py` 的 backoff 重试循环内，每次中间失败（600s 客户端超时、502、
  断连）完全静默；重试耗尽后异常上抛，只有**会话级**的失败分类
  （`planner_failed` / `self_improve_error` / `eval_failures.jsonl`）拿到一段
  截断的异常文本；
- 会话轨迹里 `input` 行已写、`output` 行永远不来，留下"悬空 input"，会话看似
  无声结束；
- 单次调用耗时只在代理审计（`latency_ms`）里，且与轨迹行无关联键；
- reasoning/response 分阶段耗时、TTFT：全链路非流式，物理上不可得。

目标：**每一次 LLM 调用（成功与失败）都有一条可追溯、内容无关、agent 不可见
的记录**。

## 2. 网关实测结论（设计依据，glm-5.3-flash，7 组探针）

| 探测 | 结论 |
|---|---|
| 正常返回 | body 带 `id`(uuid)、`created`、usage 全量（含 `prompt_tokens_details.cached_tokens`、`completion_tokens_details.reasoning_tokens`）；`reasoning_content` 非流式即返回 |
| 响应头 | `X-Request-Id`（= body `id`）、`X-Oneapi-Request-Id`、`X-Trace-Id` —— 网关侧关联锚点现成 |
| max_token 截断 | `finish_reason=length, content=""` 但 **reasoning 部分文本与精确计数完整返回** → 截断**不是**记录丢失场景 |
| 非流式超时 | `ReadTimeout`，零字节可读 |
| 流式 TTFT 窗口内超时 | 同样零字节（实测 TTFT≈2.6s，期间网关不发任何字节） |
| 流式生成中断 | 已收 chunk 可救回（实测 3 chunks / 14 字符 reasoning） |
| 错误返回 | 503 + `{"error":{code:"model_not_found",...}}`；错误响应也带 request-id |

推论：**唯一真正丢记录的场景是超时/断连**，且 TTFT 窗口内的失败无论流式与否
都只能靠客户端留痕补偿——这是本批次（非流式）的价值所在。

## 3. 设计决策

1. **sidecar，不混主日志**：`llm_call` 行写入 `<trajectory>.llm_calls.jsonl`
   （`utils/trajectory_log.py:llm_calls_sidecar`，路径单一来源）。主轨迹文件
   字节级零改动（连 `output` 行的 join 字段都不加）。
2. **agent 不可见（capability by existence）**：agent 的全部读面
   （`read_trajectory` / `read_session_trajectory` / `ctx_` 面 /
   `materialize_traj` 快照）只解析轨迹文件名；sidecar 不在任何解析路径上。
   同目录 ⇒ 同写根，零新增授权。
3. **内容无关**：记录只有 id/计时/计数/错误类，无 prompt/response 文本；
   写入前尽力过 `redact_record`（task 沙箱无 gan/ 时自动回退——记录本就无内容，
   回退安全，有测试覆盖）。
4. **缺席 ≠ 0**：取不到的计数缺席，绝不伪造 0（沿用 cost_ledger 口径）。
5. **路由用 ContextVar，不用全局 hook 列表**：并行 role 会话共享进程，
   广播列表会把记录串写进别人的 sidecar（有并发测试验证）。
6. **finally 语义发射**：发射点在 `agent/llm.py` 内部（记录生命周期与调用
   同生命周期），失败时上层来不及 emit 也必留痕——这是消除"悬空 input"的关键。
7. **双层关联 id**：客户端自生成 `call_id`（12 位 hex，不依赖网关行为）+
   记录网关 `gateway_request_id`（= body id = `X-Request-Id`）。

## 4. `llm_call` 行 schema

成功（真实冒烟数据）：

```json
{"kind": "llm_call", "call_id": "3a2cce25c608", "model": "openai/glm-5.3-flash",
 "reasoning_effort": null, "timeout_s": 600.0,
 "t_start": 1793157...,
 "duration_s": 8.084, "outcome": "ok", "attempts": 1,
 "finish_reason": "stop", "gateway_request_id": "00c33b34-3231-40ad-b581-03b45c0d593a",
 "usage": {"prompt_tokens": 17, "completion_tokens": 32, "total_tokens": 49,
            "cached_tokens": 0, "reasoning_tokens": 27}}
```

失败（连接断，1 次重试后放弃）：

```json
{"kind": "llm_call", "call_id": "...", "outcome": "error",
 "attempts": 2, "error_class": "TransientUpstreamError",
 "original_error_class": "ConnectionError", "error": "...(≤300字符)",
 "retries": [{"try": 1, "error_class": "ConnectionError", "error": "boom",
               "waited_s": 1.0},
              {"try": 2, "error_class": "TransientUpstreamError", "error": "...",
               "waited_s": null, "gave_up": true}]}
```

要点：
- `error_class` 是 backoff 最终看到的异常；`original_error_class` 是穿透
  `TransientUpstreamError` 包装后的**原始传输异常类**（forensics 关键）；
- backoff 的 `on_backoff` 回调拿不到异常对象，所以失败由
  `_attempt_completion` 自身暂存进调用上下文、回调补 try 序号与等待时长；
- usage 缺席于 error 行（网关对失败调用不给计数，绝不伪造）。

## 5. 数据流

```
get_response_from_llm (agent/llm.py，唯一咽喉；preflight 也经此)
  ├─ 生成 call_id、打 t_start（权威的请求发起时刻）
  ├─ litellm.completion(..., extra_headers={"X-Gan-Call-Id": call_id})
  │     └─ relay（只改 Authorization，自定义头无损穿透，两份实现零改动）
  │          └─ ParentLLMProxy.handle_completion
  │               └─ events.jsonl `type:"llm_call"` 审计行 +=
  │                  {call_id, gw_request_id, gw_error_code, gw_error_type, latency_ms}
  ├─ 成功 → llm_call 行(outcome=ok) ─┐
  └─ 失败 → llm_call 行(outcome=error) ┘→ ContextVar sink
                                            └─ <trajectory>.llm_calls.jsonl (SIDEcar)

cost_ledger.session_cost_from_file
  ├─ 实付 token：仍只读主轨迹 `output` 行（口径不变）
  └─ 新指标 llm_calls_failed / llm_retry_total / llm_time_s ← sidecar

task 侧：chat_history 同目录 sidecar 在 work/<genid>/（会被修剪）
  └─ trajectory.collect() 归档时折叠为 <task.jsonl>.llm_calls.jsonl
     （失败不拖垮主归档，但 soft_fail 留痕：task_llm_calls_sidecar_archive_failed）
```

三方串联：轨迹 sidecar 行 `call_id` ↔ 代理审计行 `call_id` ↔
`gateway_request_id`（= 网关 `X-Request-Id`）。

## 6. 文件改动清单

| 文件 | 改动 |
|---|---|
| `utils/trajectory_log.py` | `llm_calls_sidecar()` 命名 helper（单一来源） |
| `agent/llm.py` | 记录生命周期（`_CallContext` + ContextVar sink + backoff 回调 + finally 发射）、`_usage_full`（cached_tokens）、`_finish_reason`/`_gateway_request_id`、`_completion_kwargs` 增加 `call_id`→`extra_headers`；info dict 增加 `call_id`/`duration_s`/`gateway_request_id`（可选消费） |
| `agent/llm_withtools.py` | `chat_with_agent` 会话级注册/注销 sidecar sink（与 cm_token 同点清理） |
| `gan/framework/llm_proxy.py` | `handle_completion` 读 `X-Gan-Call-Id`（不转发上游）+ 审计行四个新字段 |
| `gan/framework/cost_ledger.py` | `_llm_sidecar_metrics` + `session_cost_from_file` 接线 + `compact` 键 |
| `gan/framework/trajectory.py` | `collect()` 末尾归档 task sidecar（`_collect_llm_calls_sidecar`，原子写、qid 戳、按 ts 排序、软失败留痕） |
| `gan/framework/loop.py` | `cost_session` 事件透出三个新字段 |

**有意不做**（遵守"agent 不可见"约束）：agent 可见的成本陈述
（`cost_ledger.render_*`）不加失败计数；`trajectory._render` 不渲染 sidecar
（sidecar 根本不在其输入里）；`redact_record` 逻辑未动。

## 7. 可见性与写入权限核对

- 读面：sidecar 路径不被任何 agent 工具解析（capability by existence）；
- 写面：seat 写根与主轨迹同目录，零新增 ACL；
- `redact_file` / 轮转（`_parts` 只认数字后缀）不会触碰 sidecar；
- task 沙箱无 gan/ → sink 内 redaction 回退为直写（记录内容无关，安全，
  有回归覆盖）；
- 归并后的事件流（events.jsonl、归档 sidecar）与 cm sidecar 同级，均为
  框架审计面。

## 8. 验证记录

- 新增离线回归 `scripts/local/regress_llm_call_records.py`（本地，gitignored）：
  **50/50 通过**。覆盖：ok/retry/error 行字段、原始异常类穿透包装、giveup 标记、
  并发 sink 无串扰且 exactly-once、无 sink（preflight）不受影响、坏 sink 不炸
  调用、sidecar 与主轨迹隔离、cost_ledger 指标与口径、无 sidecar 时缺席、
  task 归档（排序/qid/防撕裂行）、代理审计 `call_id`/`gw_request_id` 贯通
  （本地 fake upstream + Unix socket 全链）；
- 七套工具管理回归（`run_tool_regression.py`）全过 + `regress_ledger_gate.py`
  21/21（`loop.py` 在守护清单内，按 AGENTS.md 要求运行）；
- 真实网关冒烟：ok 行与 info 的 `gateway_request_id` 一致、`cached_tokens`
  落行、自定义头不破坏真实调用；gan-less 沙箱模拟下 sidecar 正常落盘、
  主轨迹干净；断连失败后 error 行 + 原始异常类落盘，异常照常上抛。

## 9. 已知边界与后续接入点

- **TTFT 窗口内的死亡仍无内容可救**（网关不发字节，流式也一样）——本批次的
  客户端留痕（t_start/timeout/attempts/retries）是唯一补偿；
- **流式方案（未实施）**预留接入点：`llm_call` 行可加 `ttft_s` / `phase`
  （reasoning/response 分相墙钟）/ `partial_on_error`（中断救回的 reasoning
  文本，≤2000 字符，过 redaction）；代理 `handle_completion` 需加 SSE 透传
  （relay 零改动）；回归需先建慢 fake upstream（AGENTS.md transport 教训）；
- 网关脆弱点备忘：`model_not_found` 以 503 包装，`_is_transient_upstream`
  靠文本规则拦截（顺序目前正确）；流式 usage 实测默认附末 chunk，但仍应
  显式 `stream_options.include_usage`。
