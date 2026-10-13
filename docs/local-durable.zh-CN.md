# Windows 本地持久化运行

## 无 Docker 一键启动

本机已安装 Windows 原生 Temporal CLI 1.8.3，位置为
项目内 `tools/temporal/temporal.exe`（Server 1.31.2），同目录保留官方 LICENSE。
启动脚本根据自身位置解析路径，不依赖项目所在盘符或当前工作目录。
项目使用现有 `.venv`，无需管理员权限或修改系统 PATH。
另一台机器可从 [Temporal 官方下载页](https://docs.temporal.io/cli/setup-cli)
安装适合目标 Windows 架构的 CLI 到项目同一相对目录，或传入 `-TemporalExe`。

迁移时可连同 `tools/temporal` 一起复制项目；该工具目录被 Git 忽略，git clone 不会带上程序。
当前副本为 Windows amd64，不适用于所有操作系统或 CPU 架构。
Python `.venv` 不可视为可移植环境，在新机器上应重新创建并安装项目依赖。
本次只调整软件位置：业务数据仍在下述 TEMP 目录，日志仍在 LocalAppData，复制项目不等于迁移全部状态。
迁移业务状态前应停止所有服务、备份完整数据目录，并检查其中的本机绝对路径及凭据配置。

双击根目录 [start-local.cmd](../start-local.cmd)，会启动 Temporal、SAGE API、SAGE Worker，
检查 API durable 状态和 Workflow/Activity Worker poller 后打开管理页面。
保留启动窗口；按 Ctrl+C 或双击 [stop-local.cmd](../stop-local.cmd) 统一停止本次启动的服务。
推荐用停止脚本，不要直接强制关闭窗口。停止不会删除业务数据或 Temporal 数据库。

- SAGE：`http://127.0.0.1:8014/admin`
- Temporal UI：`http://127.0.0.1:8233`；gRPC：`127.0.0.1:7233`
- 默认数据：`%TEMP%\sage-designer-preview`，沿用此前 8014 预览；长期保留前请备份，系统可能清理 TEMP。
- Temporal SQLite：数据目录下 `temporal.db`；不要在运行中手工移动数据库。
- 日志：`%LOCALAPPDATA%\Sage\local-8014` 下的 temporal/api/worker `.log` 和 `.error.log`。
- 相同端口或同一数据目录已有本脚本运行时拒绝重复启动；端口被其他程序占用时不自动杀进程。
- 本地开发限定：只监听回环地址，匿名公共 API、关闭管理鉴权、关闭 MCP host allowlist，
  保留实际调用审批及其他安全检查，不可用于生产或对外开放端口。
- 不改写 `.env`。默认明确使用离线模型，不连接 PostgreSQL/S3；MCP 配置仍从既有数据和配置加载。
  Agent 持久化 checkpoint 仍未启用，不能据此承诺完整模型进程重启恢复。

```powershell
.\start-local.cmd -CheckOnly
.\start-local.cmd -ConfiguredModel
.\start-local.cmd -NoBrowser
.\start-local.cmd -Port 8015 -TemporalPort 7234 -TemporalUiPort 8234 -DataRoot "$env:LOCALAPPDATA\Sage\demo-data"
.\stop-local.cmd -Port 8015
```

`-ConfiguredModel` 使用现有进程环境和 `.env` 中的模型配置（包括 Azure API key/Entra），
不自动登录、不修改密钥、不保证模型端点已可用。默认 task queue 为 `sage-local-<API端口>`，
API 与 Worker 自动一致；改变端口也会改变队列，不应在有待处理任务时随意切换。
首次启动服务就绪检查最多等待各 120 秒；失败提示会给出日志目录，清理本次已启动进程。

## Docker 与 PostgreSQL 启动（可选）

要求 Docker Desktop（Linux containers）、Python 3.12 和项目虚拟环境。
API 和 worker 运行在 Windows，同一用户的 Azure CLI 登录可用于模型请求。
Compose 仅启动配套服务，凭据仅限回环开发环境，不用于生产。

```powershell
docker compose -f compose.local.yaml up -d
$env:SAGE_TEMPORAL_TARGET = "127.0.0.1:7233"
$env:SAGE_CHECKPOINT_DATABASE_URL = "postgresql://sage:sage-local-only@127.0.0.1:5432/sage?sslmode=disable"
.\.venv\Scripts\python.exe -m uvicorn sage.app:app --app-dir src --host 127.0.0.1 --port 8000
```

第二个终端启动 worker：

```powershell
$env:SAGE_TEMPORAL_TARGET = "127.0.0.1:7233"
$env:SAGE_API_BASE_URL = "http://127.0.0.1:8000"
.\.venv\Scripts\python.exe -m sage.temporal_worker
```

启用 Admin 鉴权时，worker 的 `SAGE_ADMIN_TOKEN` 必须与 API 一致。
公共 API 使用 Entra 时，worker 使用 `SAGE_WORKER_BEARER_TOKEN`，或
`SAGE_WORKER_TOKEN_SCOPE` 配合 DefaultAzureCredential；token 不写入示例文件。
模型端 Azure CLI 登录、SAGE 入站登录、MCP 连接凭据是三个独立边界。
匿名公共 API 只有只读角色；关闭 Admin 鉴权不会赋予匿名调用者高风险执行权。

## 使用

- `/admin`：MCP Test/Discover 后可直接 Test tool，无需注册或发布。
  写测试需确认，未知风险先由管理员评审，高风险测试需 HumanTask 批准。
- 注册原生 Tool 后，将注册记录及引用它的 Agent/Skill 加入 Candidate。
  开发环境可 Publish；生产仍使用提交、审批、激活。
- 主页面选择 Durable Skill 或 Durable Agent，提交 JSON 输入。
  运行区域可刷新状态、查看结果、取消和处理有权审批的任务。
- Tool API 仍是 `POST /v1/invocations`。高风险调用必须带 `idempotency_key`，
  返回 202、`run_id` 和 `approval_task_id`；未配置 Temporal 时返回 503，不执行工具。
- 审批：`POST /v1/human-tasks/{task_id}/decision`，提交 `approve` 和 `reason`。
  查询：`GET /v1/durable-runs/{run_id}/result`；取消：同路径的 `/cancel`。

独立 Skill 请求示例：

```json
{"skill":"quote","idempotency_key":"quote-001","payload":{"unit_price":12,"quantity":3,"service":"iis"}}
```

Agent 请求使用同一 `/v1/durable-runs`，将 `skill` 换为 `agent_id`。
Agent、Skill、旧 `workflow_type` 三种目标只能选择一个。
Solution 范围的请求仍必须满足 Release 能力锁和 durable profile。

## 恢复边界

模型审批使用 LangGraph checkpoint 中断和恢复，不重新提交原始消息。
普通 durable Agent 和高风险模型 Skill 要求配置 Postgres；只读模型 Skill
可在开发中使用内存 checkpoint，但进程重启恢复不受保证。
高风险依赖检查同时覆盖原生 Tool 和普通 Skill。`memory_policy=none`
关闭业务记忆，但不会关闭持久化审批所需的执行 checkpoint。
本地控制面是单 API 实例的 JSON/SQLite 存储，不提供多副本写入保证。

工具派发由精确参数、调用 ID、身份、Generation 和定义摘要绑定。
外部写入超时、进程中途退出或派发结果未知时，不自动重试；必须核对上游结果。
这不是外部系统的 exactly-once 保证。派发前检查运行状态；取消不能撤销已经
发出的请求，也不能保证消除状态检查与外部调用之间的竞争窗口。
保存的运行输入和 checkpoint 可能含业务数据，需保护本地数据目录和数据库；
不要向模型或工具参数提交连接密码、token 或 API key，使用服务端凭据引用。

目前自动测试覆盖 API、模拟 Temporal 和共享 checkpoint 的审批恢复。
真实 Postgres/Temporal 下的 API/worker 重启场景仍需在上述服务可用后验收。
模型 turn 中途退出或启动结果未知时，需要人工核对，不会自动修复 checkpoint
与调用记录。独立 Skill 不支持嵌套 model/workflow Skill。

普通 Skill 包装器和原生 Tool 共用精确调用审批账本，审批作用域仍分离。
模型图和 YAML 步骤都可在审批后继续。带隐式附件或旧式显式审批参数的
Skill API 请求保留原有执行契约，不自动转为新的待审批流程。
审批视图展示有大小上限的脱敏参数，完整参数由摘要绑定；过滤不是完整的数据防泄漏保证。
旧版本待审批记录缺少新绑定字段时会被拒绝，需重新发起调用和审批。

持久化 Agent 委派保留稳定子运行、调用身份和 turn 序号。子审批显示在根运行
工作台，决定后通知根 Temporal workflow。连续审批恢复不会重新启动子 Agent。
子调用派发前检查所有祖先运行，拒绝循环委派；当前持久化委派仅支持 JSON 输入，
不接受隐式 `artifact_ids`，可传递声明过的 JSON 资源引用。
`POST /v1/agent-invocations` 的持久化父运行要求 checkpoint，待审批返回 202 和
`tasks`；默认 `turn_index=0`，批准后显式推进到下一 turn，相同序号只重放原结果。
模型图与 worker 自动处理该序号。上述恢复测试使用共享内存 checkpoint 和模拟
Temporal，尚不构成真实进程重启或外部系统 exactly-once 验收。

未发布 MCP 测试在审批后发生配置或风险评审变化、审批过期时记录失败，不派发工具。
已派发写测试被取消时记录未知结果并传播取消，不自动重试。

## 浏览器回归检查

可选测试依赖 Playwright 和本机 Microsoft Edge。先启动 API，再运行：

```powershell
.\.venv\Scripts\python.exe -m pip install "playwright>=1.62,<2"
.\.venv\Scripts\python.exe scripts/smoke_ui.py --url http://127.0.0.1:8011 --output "$env:TEMP\sage-ui-check"
```

脚本检查桌面和手机布局、参数类型、结果转义和刷新不重复派发，并保存截图。
MCP 测试响应由浏览器拦截模拟，不代表真实服务器写入或持久化恢复验收。

## Entra 模型验证

本地 Windows 推荐使用 [start-entra.cmd](../start-entra.cmd)。它复用一键启动器，
启用真实模型，并在本次进程中明确设置 `SAGE_AZURE_OPENAI_AUTH=entra-id` 和
`SAGE_AZURE_OPENAI_CREDENTIAL=azure-cli`，覆盖旧 API Key 认证选择。
现有模型部署名、端点和可选租户/scope 仍从环境及 `.env` 加载；不修改或删除旧 Key，
Entra 分支不使用该 Key，也不会在认证失败时回退到 Key。
原 `start-local.cmd` 的默认离线模式不变。当前运行实例不会自动切换，必须重启。

1. 请资源管理员在 Azure OpenAI 资源的 **Access control (IAM)** 中给你的登录账号分配
  **Cognitive Services OpenAI User**（或包含模型推理 DataActions 的适当角色）。
  仅能在门户查看资源不代表拥有推理权限，角色变更需要传播时间。
2. 在运行 SAGE 的同一个 Windows 用户下完成登录，将占位符替换为模型资源所属租户 ID：

  ```powershell
  az login --tenant "<资源租户ID>"
  az account show --query "{tenant:tenantId,subscription:name}" -o table
  $env:SAGE_AZURE_OPENAI_TENANT_ID = "<资源租户ID>"
  ```

  Windows 可能弹出系统账户窗口（WAM）或浏览器，按提示完成 MFA 和订阅选择。
  密码、验证码和 token 不要粘贴到聊天或配置文件。无法弹窗时，可在组织策略允许下使用
  `az login --tenant "<资源租户ID>" --use-device-code`，仅使用自己刚运行命令产生的设备码。
  多租户用户建议将非秘密的 `SAGE_AZURE_OPENAI_TENANT_ID` 固定在 `.env` 中，
  这样双击入口时也使用正确租户。
3. 执行一次真实模型验证（会发送短测试提示词，产生少量模型用量；不调用 MCP 工具）：

  ```powershell
  .\.venv\Scripts\python.exe scripts/smoke_azure_entra.py --tenant "<资源租户ID>"
  ```

  该脚本显式使用 Entra + Azure CLI，忽略旧 Key 认证选择。成功会显示
  `Verified ... using Entra ID (Azure CLI).`；此命令不改写 `.env`。
4. 停止当前实例，等待原启动窗口退出，再启动 Entra 实例：

  ```powershell
  .\stop-local.cmd
  .\start-entra.cmd
  ```

  管理页面仍是 `http://127.0.0.1:8014/admin`。后续登录凭据有效时直接双击 Entra 入口即可；
  Azure SDK 获取并刷新访问 token，登录失效或组织策略要求重新认证时再运行 `az login`。
  `start-entra.cmd -CheckOnly` 只检查本地配置/依赖，不验证 Azure 登录、RBAC 或真实推理。

401 优先核对租户、登录状态、token scope 与端点；403 核对资源数据面角色以及防火墙/私有网络；
404 核对部署名与 `/openai/v1` 端点；连接超时核对 VPN、代理和私有 DNS。
当前项目默认 scope 为 `https://ai.azure.com/.default`，不要将管理 API 的 scope 当成模型 scope。
Azure CLI 登录只解决模型出站认证，不等于 SAGE 网页用户登录或 MCP 认证，也不启用 PostgreSQL checkpoint。

参考：[Azure CLI 登录](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-interactively)、
[Azure OpenAI 资源角色](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/role-based-access-control)。

可选：若要让其他真实模型启动方式也使用 Entra，可用已有验证脚本在成功后写入非秘密认证选项：

```powershell
.\.venv\Scripts\python.exe scripts/smoke_azure_entra.py --configure
```

默认复用当前 `az login`；`--login` 强制浏览器登录，`--tenant` 指定租户。
仅在实际模型请求成功后写入 `.env` 的非秘密认证选项，不删除现有 API key。
运行中的 API 需重启，进程环境变量仍优先于 `.env`。
Azure 403 通常需要核对数据平面角色、租户及资源访问限制，不代表 CLI 登录失败。