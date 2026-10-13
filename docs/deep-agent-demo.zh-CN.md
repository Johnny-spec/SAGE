# Deep Agent Demo 实施说明

## 目标

在保留 Direct Skill 零模型路径的同时，增加一条可演示、可测试的 Deep Agent 路径：

```text
Message
  -> Deep Agents coordinator
  -> structured plan
  -> incident-analyst subagent
  -> read /iis-logs/SKILL.md
  -> read /iis-logs/references/triage.md when required
  -> iis-logs Skill Tool
  -> read /report-summary/SKILL.md
  -> report-summary Skill Tool
  -> final response + thread checkpoint
```

## 任务分解

| 任务 | 实现 | 验收信号 |
| --- | --- | --- |
| Agent Runtime | `deepagents 0.7` + LangGraph | `/v1/agent` 返回运行时状态 |
| 多步骤规划 | `TodoListMiddleware` | 两次 `plan.updated`，最终全部 completed |
| 渐进加载 | `SkillsMiddleware` + 只读 `FilesystemBackend` | metadata -> SKILL.md -> optional reference |
| Tool 循环 | Registry Skill 动态转 `StructuredTool` | 两个 `tool.started/completed` |
| 多 Skill 组合 | `iis-logs` 后接 `report-summary` | `skills_used` 顺序稳定 |
| 子 Agent | `incident-analyst` 只使用只读 Skills | `subagent.started/completed` |
| 线程记忆 | `InMemorySaver` + `thread_id` | 第二轮可回忆第一轮事实 |
| 模型推理 | 默认离线 Demo 模型，可替换 LangChain Provider | `model_calls > 0` |
| 事件投影 | FastAPI POST SSE | 不输出 prompt、reasoning、密钥或宿主机路径 |
| 前端 | Deep Agent/Direct 模式与 New thread | 桌面和 390px 视口无溢出 |

## 执行边界

- Agent 不能直接执行任意代码，只能调用 Registry 中已审核的 Skill Tool。
- Agent 初始只获得 Skill 名称、描述和指令虚拟路径，不预加载完整内容。
- SAGE 拒绝尚未成功读取对应 `SKILL.md` 的 Skill Tool 执行。
- Tool 调用仍经过服务器参数注入、JSON Schema、超时和输出大小检查。
- `log_path` 由服务器从不透明附件 ID 注入，模型和客户端不能提供宿主机路径。
- 只读虚拟文件系统根限定在 `skills/`，不提供写入或宿主机路径逃逸能力。
- 每次 `read_file` 结果作为 ToolMessage 进入下一轮 LLM；SSE 仅公开虚拟资源路径和字节数。
- `incident-analyst` 收到隔离上下文，只向协调 Agent 返回最终结构化结果。
- 当前 Checkpointer 是进程内存实现，适合 Demo；服务重启后 thread 历史清空。

## 模型模式

### 离线 Demo

无环境变量时使用 `sage-demo-model`。它通过 LangChain `BaseChatModel` 接口发出真实工具调用，保证 CI 稳定覆盖完整 Deep Agents graph，但不冒充通用 LLM。

### Provider 模型

项目根目录 `.env` 是本地配置入口，进程环境变量具有更高优先级。设置 `SAGE_AGENT_MODEL=provider:model` 后，运行时通过 `init_chat_model` 创建真实模型，并传入温度、Token、超时和重试设置。

OpenAI 示例：

```powershell
$env:SAGE_AGENT_MODEL = "openai:gpt-4.1-mini"
$env:OPENAI_API_KEY = "<secret>"
```

Azure OpenAI 示例：

```dotenv
SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol
SAGE_AZURE_OPENAI_AUTH=api-key
AZURE_OPENAI_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1
AZURE_OPENAI_API_KEY=<secret>
```

API Key 与 Entra ID 分支都使用 `ChatOpenAI`、OpenAI v1 endpoint 和 Responses API；只有凭据来源不同。v1 endpoint 不需要 API version。

Azure OpenAI v1 + Entra ID 示例：

```dotenv
SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol
SAGE_AZURE_OPENAI_AUTH=entra-id
AZURE_OPENAI_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1
SAGE_AZURE_OPENAI_TOKEN_SCOPE=https://ai.azure.com/.default
```

此模式使用 `DefaultAzureCredential` 与 `get_bearer_token_provider`，将 callable token provider 作为 `ChatOpenAI.api_key`，并启用 Responses API。认证模式开关是权威选择；即使环境中残留 API Key，`entra-id` 分支也不会使用它。

本地开发通常先运行 `az login`；Azure 托管环境使用 Managed Identity。调用身份需要模型资源上的推理 RBAC，推荐最小角色为 `Cognitive Services OpenAI User`。角色刚分配后可能需要等待数分钟生效。

配置字段：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SAGE_AGENT_MODEL` | 空 | 空值使用 `sage-demo-model`；否则为 `provider:model` |
| `SAGE_AGENT_TEMPERATURE` | `0` | 采样温度，范围 0 到 2 |
| `SAGE_AGENT_MAX_TOKENS` | `4096` | 单次模型最大输出 Token |
| `SAGE_AGENT_TIMEOUT_SECONDS` | `120` | 单次 Provider 请求超时 |
| `SAGE_AGENT_MAX_RETRIES` | `2` | Provider 请求重试次数 |
| `SAGE_AGENT_RECURSION_LIMIT` | `40` | LangGraph 单次运行最大图步数 |
| `SAGE_AZURE_OPENAI_AUTH` | `api-key` | Azure 认证方式：`api-key` 或 `entra-id` |
| `SAGE_AZURE_OPENAI_TOKEN_SCOPE` | `https://ai.azure.com/.default` | OpenAI v1 Entra token scope |

端到端 Provider 验收：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_azure.py
.\.venv\Scripts\python.exe scripts\smoke_agent.py --require-provider
```

### 多轮调用协议

Deep Agents/LangGraph 将模型输出中的 `tool_calls` 作为状态机边：

1. 模型接收 system prompt、thread 历史、Skill 元数据和可用 Tool Schema。
2. 模型返回一个或多个 Tool Call，例如 `read_file`、`task` 或 `skill_iis_logs`。
3. Runtime 执行 Tool，把结果追加为 `ToolMessage`。
4. Runtime 使用更新后的 messages 再次调用模型。
5. 模型可继续加载资源、调用另一个 Skill，或返回无 Tool Call 的最终文本。

IIS 报告的离线验收至少触发 10 次模型调用。子 Agent 拥有独立消息上下文，只把最终分析结果交还协调 Agent；协调 Agent 再加载并调用报告 Skill。Skill Python 进程保持确定性，LLM 负责选择、解析 Tool 结果和决定下一步。

### 渐进披露协议

1. **发现**：`SkillsMiddleware` 只把 Skill 名称、描述和虚拟路径加入模型上下文。
2. **主指令**：模型调用 `read_file('/<skill>/SKILL.md')`，结果成为下一轮的 `ToolMessage`。
3. **资源**：只有主指令要求时才继续读取 `references/`、模板或其他文件。
4. **执行**：SAGE 检查本轮状态中是否存在成功的主指令读取；否则返回 `skill_instructions_not_loaded`，不会启动 Skill。
5. **综合**：Skill 结构化结果成为新的 `ToolMessage`，模型据此继续下一 Skill 或生成最终回答。

## 自动测试

- 模型状态机与 Provider loader
- 独立 Provider 风格模型的 `read_file -> Skill -> final` 三轮真实图测试
- 未加载 `SKILL.md` 时拒绝执行的负向门禁测试
- Deep Agent graph 规划、委派、双 Skill 工具循环
- REST/SSE Agent 契约
- Direct 模式兼容与 `model_calls: 0`
- 同 thread 记忆与不同 thread 隔离基础
- 附件内容识别、ZIP 安全与路径保护

完整命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
node --check src/sage/static/app.js
```