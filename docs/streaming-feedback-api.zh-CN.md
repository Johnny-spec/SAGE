# 流式结果与反馈 API

本文描述 SAGE 当前客户端契约：能力发现、Interactive Operation、持久 Run 事件流、稳定结果视图和不可变用户反馈。

## 安全边界

客户端可以看到结构化执行进度、Skill/MCP 调用、公开分析摘要、证据和最终结果。平台不输出或持久化模型的隐藏思维链、系统提示、内部消息状态或未受控的工具原始内容。

## 实时流与断线恢复

客户端首先发现当前身份和 Solution 可调用的能力：

```http
GET /v1/capabilities?solution_id=incident-diagnostics
GET /v1/solutions/incident-diagnostics/capabilities
```

两个地址返回相同的版本化 `CapabilityDescriptor`。每项包含：

- Skill、MCP Tool 或 Agent 的 ID、版本、内容摘要与当前 Generation/Release。
- 输入/输出 JSON Schema、风险、超时、输出上限与是否需要审批。
- 客户端可调用的 operation：`invoke`、`message-stream`、`start-operation` 或 `durable-run`。
- endpoint、请求中承载业务输入的字段、流式/异步/取消能力和幂等要求。
- 通用 Run events、result 和 feedback 链接。

Discovery 使用与执行相同的 `CapabilityView` 和 Policy。普通用户看不到写能力；依赖全部不可访问能力的 Agent 也不会出现。旧 `/v1/skills`、`/v1/agents` 保持兼容，其中 Agent 的 capability 列表也只返回当前调用者可访问的绑定。

## Interactive Operation Handle

浏览器、IDE 或需要先返回请求句柄的服务端客户端可使用：

```http
POST /v1/operations
Idempotency-Key: client-request-42
Content-Type: application/json

{
  "solution_id": "incident-diagnostics",
  "capability_id": "agent:diagnostic-agent",
  "input": {"message": "Analyze the uploaded incident evidence"},
  "artifact_ids": ["art_123"]
}
```

接口返回 `202` 和 `OperationHandle`，其中包含 status、events、result、cancel 与 feedback 链接。重复提交相同幂等键和内容返回相同 Run；内容不同返回 `409`。

Interactive Operation 只接受 read-only Skill 和只绑定可见只读能力的 Agent。它是当前 API 进程中的异步任务：支持显式取消，但 `restart_recoverable=false`。服务正常关闭时任务被取消并记录终态；进程硬中断后若状态仍为 running，handle 会显示 `runtime_attached=false`，取消请求返回 `409`。写能力继续使用 governed invocation，高风险写操作使用审批与 Temporal；要求跨重启恢复时必须选择 descriptor 中 `restart_recoverable=true` 的 `durable-run`。

交互式调用继续使用：

```http
POST /v1/messages/stream
Content-Type: application/json
Accept: text/event-stream
```

该流的 SSE `id` 现在等于持久 `RunEvent.sequence`。收到首个 `run.started` 后保存其中的 `run_id`；连接中断时改用持久流恢复：

```http
GET /v1/runs/{run_id}/events/stream?after_sequence=17
Accept: text/event-stream
Authorization: Bearer <access-token>
```

也可以发送 `Last-Event-ID: 17`。服务端先重放 sequence 大于 17 的历史事件，然后在 Run 仍为 running 时继续等待新事件；Run 进入终态后关闭流。心跳使用 SSE comment，不产生业务 sequence。

事件 data 使用版本化 envelope，同时保留原有 `name` 字段：

```json
{
  "schema_version": "1.0",
  "event_id": "run_123:18",
  "sequence": 18,
  "run_id": "run_123",
  "type": "tool.completed",
  "name": "tool.completed",
  "category": "execution",
  "occurred_at": "2026-09-11T12:34:56+00:00",
  "visibility": "public",
  "data": {"tool": "skill:iis-logs"},
  "links": {}
}
```

浏览器在 Entra 模式下应使用 `fetch()` + `ReadableStream`，因为原生 `EventSource` 不能设置 `Authorization` header。桌面工具和服务端 SDK 使用相同的 `Last-Event-ID`/`after_sequence` 游标，不需要另一套协议。

JSON 轮询恢复仍可使用：

```http
GET /v1/runs/{run_id}/events?after=17
```

连接断开只停止事件传输，不等同于取消 Run。

在 `run.completed` 前，平台会持久化 `result.available`。该事件只包含 `result_digest`、Block ID/type/revision/digest、Artifact links 和最终结果/反馈地址，不重复发送完整业务结果。普通消息、native MCP Tool 和 orchestrated Skill 使用同一事件；事件中的 digest 与随后 `GET /v1/runs/{run_id}/result` 完全一致。显式标准 Evidence 无效时记录 `result.projection.failed`，机器执行结果仍按原执行语义结束。

## 完整结果

Run 完成后读取不可变结果投影：

```http
GET /v1/runs/{run_id}/result
```

`ResultView` 同时提供：

- `data`：满足 Skill/Solution 输出 Schema 的机器结果。
- `blocks`：安全、声明式的前端展示块，每块有稳定 ID、revision 和内容摘要。
- `artifact_ids`：大文件或二进制结果引用。
- `evidence`：后续接入 Citation、Assessment 和 Provenance 的标准位置。
- `usage`：模型、工具和耗时等使用量。
- `result_digest`：对完整用户可见投影计算的 canonical SHA-256。
- `generation_id`、`solution_release_id`、`capability_id`、`agent_id`：结果对应的精确运行版本。
- `input_digest`：创建 Run 时保存的结构化用户调用投影摘要，不包含系统 Prompt 或隐藏模型状态。

平台自动产生以下安全 Block：

- `markdown`：最终用户回答。
- `key-value`：机器结果中的标量摘要。
- `table`：结构化 `issues`、`findings` 或 `stories`。
- `citations`：引用列表。
- `assessments`：状态、摘要、置信依据、缺口和 provenance。
- `artifact`：可下载结果文件。

机器 `data` 始终保留完整原始 JSON，不因展示投影丢字段。前端只使用 DOM `textContent` 和受控 table/list，不执行结果中的 HTML 或 JavaScript。

只有输出中显式提供并通过平台 `EvidenceEnvelope` 模型验证的 `evidence` 才进入标准 Citation/Assessment。Reference Solution 历史 `citations` 形状继续作为展示引用，但不会被平台伪装成具有 artifact/source digest、locator 和 provenance 的标准证据。省略的 provenance 时间使用不可变 Run 创建时间，因此重复读取具有稳定 digest。

ResultView 中的 `artifacts` 提供带 Solution scope 的 metadata/content URL。文件内容通过以下端点读取：

```http
GET /v1/artifacts/{artifact_id}/content?solution_id={solution_id}
Authorization: Bearer <access-token>
```

响应包含原文件名、媒体类型、`ETag` 和 `X-Artifact-Content-Digest`。目录型 Artifact 没有单一内容文件，返回 `409`。浏览器前端使用 `authenticatedFetch` 获取 Blob 后下载，避免把 Entra Token 放进 URL；跨 workspace 或错误 Solution scope 返回 `404`。

未完成的 Run 或没有物化结果的旧式 Run 返回 `409`，不会伪造空结果。

## 用户反馈

用户只能对自己已完成的 Run 提交反馈，并且必须提供幂等键：

```http
POST /v1/runs/{run_id}/feedback
Idempotency-Key: feedback-from-client-42
Content-Type: application/json

{
  "rating": 3,
  "categories": ["incomplete"],
  "comment": "结论缺少影响范围。",
  "annotations": [
    {
      "block_id": "result",
      "block_revision": 1,
      "target_digest": "sha256:...",
      "comment": "请解释这个字段。"
    }
  ],
  "expected_action": "evaluation-candidate",
  "quality_use_allowed": true,
  "evaluation_use_allowed": true
}
```

相同 Run、用户和幂等键提交相同内容时返回原记录；内容不同则返回 `409`。Block revision 或 digest 不一致时返回 `422`，防止反馈被错误绑定到后来变化的内容。

读取当前 Run 的反馈：

```http
GET /v1/runs/{run_id}/feedback
```

读取本人单条反馈：

```http
GET /v1/feedback/{feedback_id}
```

Feedback 是独立、不可变的质量记录：

- 不会直接修改 Agent Prompt、Skill、MCP 配置或已发布 Generation。
- `propose-memory` 必须同时携带 `memory_proposal`，仍不会自动写入 Memory。原用户再次调用 `POST /v1/feedback/{feedback_id}/memory/confirm` 后才写入；如果同名 Memory 已存在且内容不同则返回 `409`，不会覆盖。
- `evaluation_use_allowed` 授权质量管理员把反馈冻结为回归样本候选，但不会自动修改 Agent/Skill、发布版本或生成训练数据。
- 后续质量流程应克隆新 Draft，在固定样本集上比较 baseline 与 candidate，再通过现有 Candidate/Generation 发布链上线。

Memory proposal 示例：

```json
{
  "comment": "以后请使用更简洁的答案。",
  "expected_action": "propose-memory",
  "memory_proposal": {
    "key": "answer-style",
    "value": "Use concise answers",
    "sensitivity": "normal"
  }
}
```

## 质量运营与回归样本

管理 API 继续使用现有 Admin 鉴权。只有 `quality_use_allowed=true` 的反馈会进入管理列表：

```http
GET  /v1/admin/feedback
GET  /v1/admin/feedback/quality-summary
POST /v1/admin/feedback/{feedback_id}/dispositions
GET  /v1/admin/feedback/{feedback_id}/dispositions
```

Disposition 是追加记录，原始 Feedback 不会被 PATCH。支持 acknowledged、rejected、needs-human-review、accepted-as-evaluation-candidate、proposed-revision 和 resolved。

质量摘要只返回聚合数据，不返回评论或输入正文。它提供总体及按 Solution Release、Capability 分组的反馈数、评分数、均分、低分数、问题分类计数和已冻结评测候选数。

只有同时满足 `quality_use_allowed=true` 和 `evaluation_use_allowed=true` 的反馈才能被冻结：

```http
POST /v1/admin/feedback/{feedback_id}/evaluation-candidates
GET  /v1/admin/evaluation-sample-candidates
```

候选样本锁定原 Feedback、Run 输入投影、输入摘要、Result 摘要、Generation、Solution Release、Agent/Capability、rubric 和期望结果。相同请求安全重放，不同 rubric 或期望结果返回 `409`。它仍只是待审核回归样本，不等于现有 Release-bound `EvaluationReport`，也不会触发在线学习。

管理员可以把同一个 Solution、同一个目标 Capability 的候选组成不可变版本：

```http
POST /v1/admin/evaluation-sample-sets
Idempotency-Key: contract-review-regression-v1

{
  "version": "1.0.0",
  "title": "Contract review regression",
  "candidate_ids": ["esc_...", "esc_..."]
}
```

```http
GET /v1/admin/evaluation-sample-sets
GET /v1/admin/evaluation-sample-sets/{sample_set_id}
```

样本集冻结每个候选的完整结构化输入、输入摘要、原结果摘要、期望结果、rubric、tags、来源 Generation/Release 和候选/反馈摘要。因此单独读取一个 Sample Set 就足以交给隔离的外部评测执行器，不依赖可变反馈页面。一个集合不能混合 workspace、Solution 或目标 Capability；同一 scope/version 内容漂移返回 `409`。

外部或人工评估器完成 baseline/candidate 执行后，按样本导入证据：

```http
POST /v1/admin/evaluation-sample-sets/{sample_set_id}/comparisons
Idempotency-Key: comparison-run-42

{
  "baseline_target_digest": "sha256:...",
  "candidate_target_digest": "sha256:...",
  "evaluator_id": "human-review",
  "evaluator_version": "1.0.0",
  "outcomes": [
    {
      "candidate_id": "esc_...",
      "baseline_score": 0.4,
      "candidate_score": 0.9,
      "baseline_result_digest": "sha256:...",
      "candidate_result_digest": "sha256:..."
    }
  ]
}
```

```http
GET /v1/admin/evaluation-sample-sets/{sample_set_id}/comparisons
```

客户端不能提交 aggregate。服务端要求每个 Sample 恰好出现一次，并派生 sample count、baseline/candidate average、score delta、improved/regressed/unchanged count 和最终趋势。记录固定为 `execution_mode=imported`，明确表示 SAGE 保存并验证的是外部执行证据，而不是声称自己执行了模型。

Sample Set 与 Comparison 当前是 advisory quality evidence：不会自动修改 Prompt、Draft、Skill 或 Agent，不会自动创建 Release-bound `EvaluationReport`，也不会绕过 Candidate validation/approval/activation。团队应依据对比结果创建新 Draft，并通过原发布链上线。

控制台 `/admin` 的 **Quality** 工作区提供同一闭环：

1. 查看总体、Release 与 Capability 质量趋势。
2. 选择授权 Feedback，追加 disposition 或冻结为 evaluation sample。
3. 选择同一 Solution/Capability 的样本，创建不可变版本化 Sample Set。
4. 导入逐样本 baseline/candidate outcomes，查看服务端派生的趋势与 delta。

Quality UI 不提供在线 Prompt 编辑或“自动学习”按钮。改进仍从新的 Agent/Skill/Solution Draft 开始，并经 Catalog 发布链上线。

## 当前边界

- `/v1/messages/stream` 支持请求内实时执行；`/v1/operations` 支持先返回 `202`，再独立订阅、查询或取消只读 Skill/Agent Run。
- Durable Run 可直接使用已返回的 `run_id` 订阅持久事件。
- Result block、标准 Evidence、Artifact 内容下载与通用前端渲染已实现；Solution 自定义 presentation schema 到 Block 的声明式映射仍属于后续阶段。
- 质量摘要、版本化样本集和 imported baseline/candidate comparison 已实现；可视化看板与隔离自动评测执行器仍属于后续阶段。
