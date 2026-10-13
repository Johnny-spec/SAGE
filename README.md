# SAGE

SAGE 是一个自托管的 Governed Agent Capability Platform。领域团队通过 Solution Bundle 组合 Skills、Agents、Subagents、Workflow、MCP、评测与 UI Schema；业务用户只能发现和运行当前身份获准的 Solution。合同审查、热点新闻、企业问答和 IIS/Edge 诊断都是独立 Reference Solution，而不是 Kernel 内置领域模型。

## 当前能力

Windows 本地无 Docker 启动：双击 [start-local.cmd](start-local.cmd)，自动启动原生 Temporal、API 和 Worker；
双击 [stop-local.cmd](stop-local.cmd) 停止。默认离线模型，真实模型使用 `start-local.cmd -ConfiguredModel`。
Azure CLI 登录后可双击 [start-entra.cmd](start-entra.cmd)，明确使用 Entra ID 调用真实模型，无需 API Key。
详细配置及本地开发安全边界见 [启动说明](docs/local-durable.zh-CN.md)。

创建后不知道如何发布？从 [创建、发布与运行操作指南](docs/publishing-guide.zh-CN.md) 开始，包含 MCP、Native Tool、Skill、Agent、Solution 全流程，以及 Calc/Echo/Flow 三个可重复运行的 demo。

- 原生 MCP Tool 可注册、发布并被 Agent、Skill 编排和统一调用 API 使用，无需包装成 Skill
- MCP Servers 支持未发布工具测试、显式风险评审、写确认和高风险审批
- 独立 Skill 支持 YAML workflow 和 SKILL.md 模型编排；开发环境支持 Candidate 一键发布
- [Windows 持久化运行与 Entra CLI 登录](docs/local-durable.zh-CN.md)
- `/admin` → Solutions：Solution 工作台、AI 草稿建议、自动依赖锁、Bundle/Release/Candidate 构建和既有审批发布闭环；Workflows 提供 DAG 编辑。AI 需配置真实模型，运行仍需相应执行基础设施。
- [流程设计器技术方案、需求与实施进度](docs/workflow-designer.zh-CN.md)

- Direct 模式：自动规则路由或显式指定 Skill，`model_calls: 0`
- Deep Agent 模式：结构化计划、模型工具循环和多 Skill 组合
- `SolutionDefinition`、不可变 `SolutionRelease` 组件锁与 Generation 原子激活/回滚
- 按 Solution、角色、Release 和 thread 隔离的能力发现与执行
- Solution Bundle `solution.yaml` 离线验证、控制面导入和 `sage` CLI
- 持久、内容寻址且按 Solution 隔离的 Artifact Store；旧附件 API 保持兼容
- SQLite 开发实现的持久 Run/Event、Plan、Memory、Invocation 幂等与 HumanTask 存储
- `/v1/invocations` 类型化 Direct API，重复幂等键安全回放结果
- `/v1/capabilities` 与 Solution-scoped discovery 返回 Schema、版本锁、风险和可调用 operation
- `/v1/operations` 为只读 Skill/Agent 提供幂等异步句柄、独立事件订阅与显式取消
- 可断线恢复的 Run SSE、稳定 ResultView 与不可变用户反馈 API，见 [流式结果与反馈 API](docs/streaming-feedback-api.zh-CN.md)
- 用户授权反馈可冻结为版本化回归样本集，并导入逐样本 baseline/candidate 对比证据
- `/admin` → Quality 提供反馈处置、版本化样本集和 baseline/candidate 趋势工作区
- 可选 Citation、Provenance、Assessment、Workflow、Trigger 和安全 UI Schema 契约
- Temporal `DurableExecutionPort` 官方 Adapter；Direct 与交互运行不依赖 Temporal
- Microsoft Entra Web API Bearer Token 签名、issuer、audience、tenant 和 app-role 校验
- Release-bound Evaluation Report；默认 advisory，严格环境可要求通过后才能激活
- 声明式 `AgentDefinition`、不可变 `AgentCatalog` 与按 `agent_id + version` 缓存的 Runtime Factory
- `incident-analyst` 隔离子 Agent，其能力绑定和独占委派由定义声明
- 基于 workspace、principal、Solution、Agent、Generation、Release 和 thread 隔离的 LangGraph Checkpointer
- 统一 `CapabilityView` 和 `PolicyEnforcer`，发现与执行阶段分别授权
- 默认离线 Demo 模型；可通过环境变量切换真实 LangChain 模型
- 可注入 Adapter Registry；可信开发代码可用本地子进程，生产 Python Skill 通过独立 Runner，另支持 HTTP endpoint alias 和 MCP Gateway
- 独立 MCP Gateway FastAPI 边界，执行 server alias、tool 和 schema digest 校验
- `/admin` 控制台：Solution、Release、Evaluation、Agent、Skill、MCP、Candidate、Approval 与 Generation 管理
- 原子 JSON 控制面状态和不可变本地 Generation Artifact，支持重启恢复
- 官方 MCP SDK Streamable HTTP 与受批准 stdio preset
- Candidate diff digest、显式审批、激活前重验和审批后回滚
- `RunStore` 端口与持久有序事件记录，支持按 sequence 查询和重放
- `SKILL.md` + `skill.yaml` 可执行 Skill 包
- 最大 50 MiB 的流式 Artifact 上传与不透明 ID，支持内容摘要、重启恢复和旧附件兼容
- POST SSE 事件流，不暴露推理内容或内部路径
- `echo` smoke-test Skill
- 从 SkillsCollection 复制并封装的只读 `iis-logs` Skill
- 可调用的 `enterprise-qa`、`news-intelligence`、`contract-review` Reference Skills
- 全离线自动测试；Agent 路径报告模型、工具和子 Agent 调用次数

## 启动

环境要求：Windows、Python 3.12、uv。

```powershell
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple
.\.venv\Scripts\python.exe -m uvicorn sage.app:app --app-dir src --host 127.0.0.1 --port 8000
```

运行工作台：<http://127.0.0.1:8000>。页面支持：

1. 选择 `Deep Agent`，上传 IIS W3C 文本日志或 ZIP 包。
2. 发送 `Use a subagent to analyze this IIS log and create a concise report`。
3. 在 Run activity 中观察计划、模型、子 Agent、两个 Skill Tool 和完成事件。
4. 发送 `Remember that the project codename is Orion`，再询问代号，验证 thread 记忆。
5. 切换 `Direct Skill`，验证零模型单 Skill 路径。

外部客户端集成页面：<http://127.0.0.1:8000/solution-api>。可选择已激活
Solution，直接测试 `POST /v1/messages` HTTP 响应与
`POST /v1/messages/stream` POST-SSE 事件流，并复制 cURL/JavaScript 示例。
其他网站跨域调用时，通过 `SAGE_CORS_ALLOWED_ORIGINS` 配置精确 Origin 后重启服务。

### 控制面

当前为便于开发和自动测试，Admin 鉴权默认关闭，打开 `/admin` 后直接连接：

```dotenv
SAGE_ENVIRONMENT=development
SAGE_ADMIN_AUTH_ENABLED=false
SAGE_ADMIN_TOKEN=
SAGE_CONTROL_DATA_ROOT=.sage-state
SAGE_MCP_ALLOWED_HOSTS=learn.example.com,internal.example.com:8443
SAGE_MCP_HEALTH_TTL_SECONDS=900
SAGE_MCP_STDIO_PRESETS={}
SAGE_ALLOW_PLAINTEXT_DEV_SECRETS=false
```

生产公共 API 推荐使用 Entra ID：

```dotenv
SAGE_AUTHENTICATION_MODE=entra-id
SAGE_ENTRA_TENANT_ID=<tenant-guid>
SAGE_ENTRA_AUDIENCE=<api-app-client-id>
```

Web API 会验证 RS256 签名、issuer、audience、`tid` 和有效期，再将 `oid/sub` 与 `roles` 映射为 SAGE Principal。不要把 ID Token 或其他资源的 Access Token 发送给该 API。

后台 Durable Profile 使用可选 Temporal Adapter：

```dotenv
SAGE_TEMPORAL_TARGET=temporal:7233
SAGE_TEMPORAL_NAMESPACE=default
SAGE_TEMPORAL_TASK_QUEUE=sage-workflows
SAGE_TEMPORAL_TLS=false
```

未配置 Temporal 时 `/v1/durable-runs` 明确返回 `503`，Direct 和 Interactive Profile 仍可正常使用。

`python -m sage.temporal_worker` 注册通用 `sage-solution-workflow`。Worker 只执行 Release 锁定的 Workflow Template，并通过 SAGE API 调用 Skill/Agent；HumanTask 决定通过 Temporal signal 恢复原 Workflow。生产 Worker 可设置 `SAGE_WORKER_TOKEN_SCOPE=api://<api-client-id>/.default`，由 `DefaultAzureCredential` 获取 API Token。

需要跨实例恢复 Agent thread 时配置官方 LangGraph PostgreSQL Checkpointer：

```dotenv
SAGE_CHECKPOINT_DATABASE_URL=postgresql://sage:<password>@postgres:5432/sage
```

启动时会执行 Checkpointer schema setup。未配置时使用进程内 Checkpointer，适合本地开发但不提供进程重启恢复。

Runtime Artifact 可切换到 S3 或 MinIO：

```dotenv
SAGE_ARTIFACT_BACKEND=s3
SAGE_S3_BUCKET=sage-artifacts
SAGE_S3_PREFIX=sage
SAGE_S3_ENDPOINT_URL=https://minio.example.com
SAGE_S3_REGION=us-east-1
```

SDK 使用标准 AWS Credential Chain。S3 metadata 最后提交；节点只保留受控物化 cache，事实源仍是对象存储。

配置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后，HTTP route 和状态会导出为 OpenTelemetry spans；请求正文、Token、合同内容和 Tool 参数不会写入 span attributes。

重启服务后直接打开 <http://127.0.0.1:8000/admin>。前端从 development-only bootstrap 获取同一个 Token，只存入当前 tab 的 `sessionStorage`，并隐藏 Token 输入条。

开发环境可显式开启 Token 门禁；未提供 Token 时会生成本地 Token。生产环境强制 `SAGE_ADMIN_AUTH_ENABLED=true`，关闭时拒绝启动；anonymous 管理模式还必须显式提供高熵 Token：

```dotenv
SAGE_ADMIN_AUTH_ENABLED=true
SAGE_ADMIN_TOKEN=<high-entropy-random-token>
```

Entra 模式下开启同一开关后，管理 API 不使用共享 Token，而要求 Access Token 包含 `platform_admin` 或 `capability_admin` app role：

```dotenv
SAGE_ADMIN_AUTH_ENABLED=true
SAGE_AUTHENTICATION_MODE=entra-id
```

`SAGE_ADMIN_AUTH_ENABLED=false` 期间，任何能够访问服务端口的用户都可以调用管理 API，只适用于受控开发/测试环境。

Evaluation 默认作为发布建议证据：

```dotenv
SAGE_EVALUATION_GATE_MODE=advisory
```

此模式下缺少通过报告会产生 `solution_evaluation_not_passed` warning，但不阻断 Candidate。需要把锁定 Evaluation Suite 作为硬门禁时设置 `required`；历史 Report 和 Release 摘要不会被改写。

Python Skill 的生产执行使用独立 Runner：

```dotenv
SAGE_RUNNER_URL=http://sage-runner:8080
SAGE_RUNNER_TOKEN=<high-entropy-shared-service-token>
SAGE_RUNNER_PROFILE=python-restricted-v1
SAGE_ALLOW_LOCAL_SUBPROCESS=false
```

API 只传输当前 Skill 包和已授权 Artifact 内容，不向 Runner 挂载控制面状态，也不转发用户或管理员 Token。Runner 会重验协议、请求、包、输入和输出摘要或 Schema，并在私有临时目录执行。Compose 与 Kubernetes 示例已包含独立 Runner；Kubernetes Runner 无 ServiceAccount token、无共享 PVC、只读根文件系统且默认无 egress。

`GET /health/ready` 返回 `runner: ready | local-subprocess | disabled`。配置远程 Runner 后，API 会验证其协议和 profile；Runner 不可达或 profile 不匹配时返回 `503`，避免流量进入缺少生产执行依赖的实例。

当前 Runner 是独立受限服务，提供进程和容器级故障域隔离，但不是每调用 Pod、gVisor 或 microVM 级敌对多租户沙箱。运行不受信任的第三方代码前，仍应演进为每调用可丢弃执行环境。

推荐发布流程：

```text
创建或导入资源
	-> Validate
	-> 创建统一 Catalog Candidate
	-> Validate Candidate
	-> Submit Approval
	-> Approve with reason
	-> Activate
```

### Solution Bundle 与 CLI

每个领域 Solution 使用独立目录，`solution.yaml` 锁定 Skill、Agent、Workflow、Trigger、UI Schema 和 Evaluation Suite 的版本、SHA-256 与可选文件路径。上传包含文件路径的 Bundle 时必须使用 ZIP；服务器会安全解包、解析每种组件，并验证所有跨组件引用闭包。

```powershell
# 校验目录中的 manifest、摘要、DAG、Trigger、UI action 和评测定义
.\.venv\Scripts\python.exe -m sage.cli validate-solution solutions/news-intelligence

# 生成安全 ZIP
.\.venv\Scripts\python.exe -m sage.cli package-solution `
	solutions/news-intelligence --output news-intelligence.zip

# 上传 Release；当前默认不要求 Token
.\.venv\Scripts\python.exe -m sage.cli publish-solution news-intelligence.zip

# 创建、验证并提交 Candidate，审批仍由控制面完成
.\.venv\Scripts\python.exe -m sage.cli promote `
	--release news-intelligence@1.0.0 --title 'Promote news intelligence'
```

仓库内四个 Reference Solution 位于 `solutions/`；系统测试会导入、评测、原子激活并分别执行它们，证明领域扩展无需修改 Kernel。

Agent 的 `Display name` 仅用于展示，发布与调用使用不可变 `Agent ID`。Catalog 的 Default Agent 下拉框必须选择实际 Agent ID；留空会保留当前默认 Agent。

回滚必须单独申请并审批。新 thread 使用新的 active Generation；已有 thread 继续固定原 Generation。

详细实现与安全约束见 [平台内核与扩展边界](docs/platform-kernel.zh-CN.md) 和 [控制面实现设计](docs/control-plane.zh-CN.md)。

### 本地 MCP Demo

第一个终端启动项目内置 FastMCP Demo：

```powershell
.\.venv\Scripts\python.exe scripts\demo_mcp_server.py --host 127.0.0.1 --port 8765
```

第二个终端为本地回环 Demo 启动 SAGE：

```powershell
$env:SAGE_MCP_ALLOWED_HOSTS = "127.0.0.1:8765"
$env:SAGE_ALLOW_INSECURE_LOCAL_MCP = "true"
.\.venv\Scripts\python.exe -m uvicorn sage.app:app --app-dir src --host 127.0.0.1 --port 8000
```

在 `/admin` 的 MCP Servers 页面执行：

1. 创建 alias `sage-demo`，transport 选择 `Streamable HTTP`，URL 填写 `http://127.0.0.1:8765/mcp`。
2. `Test` 执行真实 MCP initialize 握手。
3. `Discover tools` 读取 `calculate_total` 和 `lookup_runbook` 及其 schema digest。
4. 选择 `calculate_total`，补充 Skill 名称、说明、instructions 和 risk，然后导入。
5. 在 Catalog 页面创建 Candidate，依次 Validate、Submit、Approve、Activate。

发布后可直接调用 MCP-backed Skill：

```json
{
	"message": "calculate demo total",
	"mode": "direct",
	"skill": "demo-calculate-total",
	"arguments": {
		"unit_price": 25,
		"quantity": 4,
		"discount_percent": 10
	}
}
```

请求发往 `POST /v1/messages`，结果中的 `total` 为 `90.0`。Demo MCP 终端会记录 `CallToolRequest`。

只有已发布且 `skill.yaml` 使用 `execution.type: mcp` 的 Skill 会进入 MCP Adapter。仅在 `SKILL.md` 或普通 Python 代码中写“使用 MCP”不会自动获得 MCP 调用权限。Agent 模式还要求已发布 `AgentDefinition.capability_ids` 显式包含 `skill:demo-calculate-total`；否则该 Tool 不会暴露给模型。MCP test/discovery 超过 freshness window 后，调用会被拒绝，管理员必须重新 Test 和 Discover。

### 使用真实模型

项目根目录的 `.env` 是本地配置入口，已被 `.gitignore` 排除；`.env.example` 是可提交模板。进程环境变量优先于 `.env`，修改后必须重启服务。

未配置 Provider 时使用 `sage-demo-model`，确保 Demo 和 CI 无需密钥。OpenAI 配置示例：

```dotenv
SAGE_AGENT_MODEL=openai:gpt-4.1-mini
SAGE_AGENT_TEMPERATURE=0
SAGE_AGENT_MAX_TOKENS=4096
SAGE_AGENT_TIMEOUT_SECONDS=120
SAGE_AGENT_MAX_RETRIES=2
SAGE_AGENT_RECURSION_LIMIT=40
OPENAI_API_KEY=<your-key>
```

`SAGE_AGENT_MODEL` 使用 LangChain 的 `provider:model` 格式。服务不会把提示词、私有 reasoning、密钥或宿主机路径投影到 SSE。

Azure OpenAI 使用部署名而不是基础模型名：

```dotenv
SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol
SAGE_AZURE_OPENAI_AUTH=api-key
AZURE_OPENAI_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1
AZURE_OPENAI_API_KEY=<your-key>
```

API Key 模式与 Entra ID 模式使用相同的 OpenAI v1 endpoint 和 Responses API。v1 endpoint 不需要 `OPENAI_API_VERSION`。

使用 Microsoft Entra ID 和 Azure OpenAI v1 endpoint（推荐的无密钥方式）：

```dotenv
SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol
SAGE_AZURE_OPENAI_AUTH=entra-id
AZURE_OPENAI_ENDPOINT=https://wshao-childqa-resource.services.ai.azure.com/openai/v1
SAGE_AZURE_OPENAI_TOKEN_SCOPE=https://ai.azure.com/.default
AZURE_OPENAI_API_KEY=
```

该分支使用 `DefaultAzureCredential` 和 `get_bearer_token_provider`，把自动刷新的 callable token provider 传给 `ChatOpenAI.api_key`，并启用 Responses API。认证模式开关是权威选择；`entra-id` 模式不会读取 `AZURE_OPENAI_API_KEY`。

本地开发前运行 `az login`，并确保登录身份在模型资源上至少拥有 `Cognitive Services OpenAI User` 角色。Azure 托管环境会由 `DefaultAzureCredential` 自动选择 Managed Identity；用户分配身份可设置 `AZURE_CLIENT_ID`。

启动后使用以下命令确认非敏感配置；返回的 `model` 不应再是 `sage-demo-model`：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/agent | ConvertTo-Json -Depth 4
```

响应只包含模型名、温度、Token、超时、重试和 recursion limit，不返回 Key、Endpoint 或其他凭据。

完整测试真实 Provider 与 Skills 执行：

```powershell
# 1. 编辑项目根目录 .env
# 2. 重启 Uvicorn
.\.venv\Scripts\python.exe scripts\smoke_azure.py
.\.venv\Scripts\python.exe scripts\smoke_agent.py --require-provider
```

第一个脚本按 `SAGE_AZURE_OPENAI_AUTH` 使用 API Key 或 Entra ID，通过原生 OpenAI Responses API 验证连接；第二个脚本验证 Provider 模型、日志上传、`SKILL.md` 与 reference 渐进加载、`incident-analyst` 子 Agent、`iis-logs -> report-summary` 双 Skill 流程，以及唯一 `run.completed`。脚本不会打印 token、API Key 或完整日志分析结果。

### LLM 多轮与渐进加载

Skill 脚本本身不直接调用 LLM。Deep Agent 在每次 Tool 结果后重新调用模型，形成受控循环：

```text
LLM 选择动作
	-> Tool 执行
	-> ToolMessage 追加到 thread
	-> LLM 读取新结果并选择下一动作
	-> 直到 LLM 返回无 tool_calls 的最终回答
```

IIS 报告示例会依次经历：计划、委派子 Agent、读取 `/iis-logs/SKILL.md`、按指令读取 `references/triage.md`、执行 `iis-logs`、读取 `/report-summary/SKILL.md`、执行 `report-summary`、更新计划、生成最终回答。每一步都是独立模型回合；同一 `thread_id` 的完整消息与 ToolMessage 由 Checkpointer 带入下一次请求。

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/skills` | 列出已加载 Skill 及风险级别 |
| `GET` | `/v1/routes` | 列出顶层与 `父级 - 子集 - 子子集` 分层 Route |
| `GET` | `/v1/agent` | 查看 Deep Agents、模型、子 Agent 和记忆状态 |
| `GET` | `/v1/agents` | 列出当前发布的声明式 Agent |
| `GET` | `/v1/solutions` | 按 Principal 角色列出当前 Generation 可见的 Solution |
| `POST/GET` | `/v1/artifacts...` | 上传和查询持久、内容寻址 Artifact |
| `POST` | `/v1/attachments` | Artifact 上传兼容入口 |
| `POST` | `/v1/invocations` | 类型化、幂等的 Direct Skill 调用 |
| `POST` | `/v1/messages` | 同步路由与执行 |
| `POST` | `/v1/messages/stream` | 通过 POST SSE 路由与执行 |
| `GET` | `/v1/runs/{id}` | 查询持久 Run 状态 |
| `GET` | `/v1/runs/{id}/events` | 按 sequence 重放 Run Event |
| `GET` | `/v1/runs/{id}/plan` | 查询持久 Plan 投影 |
| `GET/PUT/DELETE` | `/v1/memories...` | 管理显式 Solution-scoped 用户记忆 |
| `GET/POST` | `/v1/human-tasks...` | 查询和决定分配给当前角色的人工任务 |
| `POST/GET` | `/v1/durable-runs...` | 启动、查询和取消 Temporal Workflow |
| `GET` | `/admin` | 控制面页面 |
| `GET/POST` | `/v1/admin/agents` | Agent 草稿管理 |
| `GET/POST` | `/v1/admin/skills...` | Skill 上传、验证和下载 |
| `GET/POST` | `/v1/admin/mcp...` | MCP 配置、测试、发现和 Tool 导入 |
| `GET/POST` | `/v1/admin/candidates...` | Candidate 验证、审批和激活 |
| `GET/POST` | `/v1/admin/generations...` | Generation diff 和审批回滚 |
| `GET/POST` | `/v1/admin/solutions...` | Solution、Release Lock 和 Bundle 导入 |
| `GET/POST` | `/v1/admin/solution-releases/.../evaluations` | Release 评测报告 |

Direct SSE 包含 `route.selected`、`skill.started` 和 `skill.completed`。Agent SSE 额外包含 `agent.started`、`plan.updated`、`model.started/completed`、`subagent.started/completed`、`skill.loading/loaded`、`skill.resource.loaded` 和 `tool.started/completed`。每次运行最后恰好一个 `run.completed` 或 `run.failed`。

Agent 请求示例：

```json
{
	"mode": "agent",
	"agent_id": "diagnostic-agent",
	"message": "Use a subagent to analyze this IIS log and create a concise report",
	"thread_id": "demo-thread",
	"attachment_ids": ["opaque-attachment-id"]
}
```

## Skill 包

Registry 只读取 `skills/` 的直接子目录。每个包至少包含：

```text
skills/<name>/
├── SKILL.md
└── skill.yaml
```

`SKILL.md` 描述过程知识；`skill.yaml` 绑定执行 Adapter、JSON Schema、路由关键词和策略。Agent 只看到当前 `CapabilityView` 中的工具及去除服务器路径字段后的输入 Schema。匹配后，模型先通过只读虚拟文件系统加载完整 `SKILL.md`；指令确有需要时再读取 `references/` 等资源；最后才能调用对应执行 Tool。每次读取和 Tool 结果都会进入下一轮 LLM 上下文。

基础 Generation 启用受信任的 `builtin` 与 `python-subprocess`。上传 Python 包可以验证、下载和发布；其执行仍受 manifest policy、Capability View 和部署模式约束。Development 可显式使用受信任本地 subprocess，Production 仅在独立 Runner 已配置时暴露 Python 能力。HTTP Skill 只能引用管理员登记的 `endpointAlias`；MCP Skill 固定 `serverAlias + serverConfigDigest + toolName + schemaDigest`，请求不能提供任意 URL 或命令。

### SkillsCollection 导入

本地诊断集合已复制到 `skills/SkillsCollection/`，包含 `Common`、`Edge`、`IIS` 和根级 `orchestrator` 源码。复制时排除嵌套 `.git`、pytest/Python 缓存和包含浏览器 profile 数据的 `Temp/` 运行产物。

该目录按 vendored 上游源码管理，不由 Ruff 自动改写。SAGE Registry 仍只自动执行 `skills/` 直接子目录中同时具有 `SKILL.md` 与 `skill.yaml` 的受控包装；导入集合中的脚本不会因为被复制就自动获得 Web 执行权限。新增 Web 能力时应为目标 Skill 建立显式 manifest、Schema、风险策略和 Adapter 绑定。

Route 下拉框通过 `/v1/routes` 展示 `SkillsCollection - IIS - ...`、`SkillsCollection - Edge - ...` 和 `SkillsCollection - Common - ...`。安全的离线子 Skill 可选择，并通过顶层 `skills-collection` manager wrapper 执行；需要管理员、live-host、MCP 或仅供内部委派的项会显示为 `restricted` 或 `internal` 并禁用。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
node --check src/sage/static/app.js
node --check src/sage/static/admin.js
```

测试使用合成 IIS W3C 日志，不访问外部网络或真实服务器。

控制面元数据、Generation、Artifact、Run/Event、Plan、Memory、Invocation、Audit 和 HumanTask 均可跨本地服务重启恢复。Runtime Artifact 已支持本地与 S3/MinIO Provider；Agent checkpoint 已支持官方 LangGraph PostgreSQL Provider。其余查询/控制存储的 SQLite 实现仍是单节点开发实现。

## 安全边界

- Skill 是管理员预先审核并安装的代码，不接受请求内动态代码。
- Registry 拒绝符号链接 Skill 包，子进程不使用 shell。
- 上传大小、类型、执行超时、输入输出 Schema 和最大输出均受限。
- ZIP 包拒绝加密、符号链接与路径穿越，最多 100 个成员、解压后最多 100 MiB。
- IIS 测试 Skill 仅分析已上传日志，不启用 live-host 或 `--auto-trigger`。
- Direct、Agent 与 Adapter 执行共享同一个 Capability View 和 Policy Enforcer。
- HTTP Skill 默认只允许管理员登记的 HTTPS endpoint；MCP 调用经过独立 Gateway alias 和 schema snapshot 校验。
- Skill ZIP 进入隔离目录，按实际解压字节限制，拒绝路径穿越、设备名、依赖安装入口和可执行二进制。
- Admin 鉴权当前默认关闭；显式设置 `SAGE_ADMIN_AUTH_ENABLED=true` 后恢复 Bearer Token 或 Entra 管理角色门禁。
- MCP 默认使用 Secret 环境变量引用；明文只允许显式 DEV 模式，API 永不回显。

尚未生产化的部分包括控制面及 Run/Plan/Memory/Invocation/HumanTask 的 PostgreSQL Provider、恶意软件扫描、完整供应链签名验证、每调用强沙箱和多实例写一致性。服务级隔离 Runner、Entra Token/PKCE、Solution RBAC、S3/MinIO、持久 Checkpointer、通用 Temporal Worker、审计与 OTLP tracing 已实现。`SAGE_REPLICA_COUNT>1` 会在生产配置中拒绝启动，直到所有状态 Provider 支持多写者。

本地集成拓扑见 `compose.yaml`；Kubernetes 单副本试点见 `deploy/kubernetes/`。Docker 不可用的环境仍可用 `sage serve` 启动本地服务。