# 流程设计器技术方案与需求说明

文档状态：草稿设计器、Solution 工作台和发布闭环已实现；运行观察与高级编排仍待完善。基线日期：2026-09-10。

本文用于后续需求评审、任务拆分、代码生成、实现和验收，不表示所有目标功能已经交付。实际可用范围以“当前实施进度”为准；后文“拟新增”章节保留目标设计。

### 当前实施进度（2026-09-10）

入口为 `/admin` → Solutions 的 Solution 工作台，或 Workflows 草稿设计器。本节为实际实施状态；后文仍保留完整目标方案，未在本节列明的能力不能视为已经实现。

- 管理员可以创建归属于已有 Solution 的 Workflow 草稿，保存、重载、删除，工作区间草稿不可互访。
- 草稿使用独立设计元数据、revision 和 `If-Match`；缺少版本返回 428，冲突返回 409，保存不修改 Generation 或 Candidate。
- 桌面提供可拖动节点、依赖箭头和自动布局；移动端使用节点列表及属性面板。
- 支持 Agent、普通 Skill、原生 MCP Tool 节点，配置依赖、并发、条件、join/failure policy、超时、重试、输入、绑定与输出。
- 输入表单可从活动目标 Schema 添加字段；绑定提供上游选择与 JSON Pointer 建议，复杂参数仍使用 JSON 编辑。
- MCP Tool 节点展示已发布工具的描述、Server、Tool ID、版本、风险、执行限制和参数说明，可展开输入/输出 Schema 与摘要。同名工具按 Server 区分；未发布的发现结果不会进入目标列表，列表为空时明确提示。
- 支持会话内撤销/重做、未保存提示、YAML 导出、设计包导出与往返导入。导入不调用 Tool、不发布、不执行。
- 服务端校验当前 DAG 契约和活动目标，拦截禁用或嵌套编排 Skill、嵌套 Workflow、HumanTask、concurrencyKey 和 escalate；后两者尚无完整执行语义。
- 导入限制 512 KiB、32 层深度、禁止 YAML alias 和重复键；草稿拒绝敏感命名字段、非法布局及保留对象键。此检查不能替代全面的数据分类或秘密识别。
- 控制存储升级为 v13，保留 v12 发布历史和摘要；仍是单 API 进程开发存储。

已提供的 API：`GET/POST /v1/admin/workflow-drafts`，`GET/PUT/DELETE /v1/admin/workflow-drafts/{id}`，`POST .../{id}/validate`，`GET .../{id}/export`，`POST /v1/admin/workflow-drafts/import`，`GET /v1/admin/workflow-targets`。读取和保存返回 ETag；校验和导出也要求 If-Match。列表以 offset/limit 分页。导入当前只预览，不自动保存。

设计器 Validate 仍只返回 `scope: structure-and-active-targets` 和 `publication_ready: false`；发布必须经过下面的构建与 Candidate 验证。尚未完成完整 JSON Schema 兼容性证明、生产级控制数据库、模拟运行、运行启动与节点观察 UI、HumanTask 编排、有限循环、自主调度配置和 Trigger 管理。通用 Workflow 返回节点 results，不应用 outputs 投影，构建预览对此给出警告。发布成功不代表 Temporal/Postgres 基础设施已验收。

#### Solution 工作台与发布闭环

1. 在 Solutions 创建或选择 Solution，再新建或选择它的 Workflow 草稿；已有手动导入、组件锁和评估表单保留在折叠区域。Solution 不要求预先建立 Domain 或 Agent。
2. 在设计器编辑后点击 Review release，自动保存并回到该 Solution 工作台。填写 Release version，点击 Resolve dependencies。
3. 服务端从同一活动 Generation 递归解析 Agent 能力、子 Agent 能力、Agent 委派和 Skill 声明依赖，去重并生成确切版本/摘要。引用缺失、禁用、循环、Skill 内容漂移或不支持节点会阻止构建。当前只解析已激活能力，不自动发布尚未激活的 Agent/Skill/Tool 草稿。
4. 审阅依赖来源、风险、字段警告与 manifest，勾选确认后下载未签名 ZIP 或创建 Release/Candidate。ZIP 包含 solution.yaml、workflow.yaml、独立 design.yaml、组件级 CycloneDX SBOM 和构建 provenance。Skill/Agent/Tool 为版本锁引用，不把其源码或凭据复制进 Bundle；跨环境导入要求目标环境具备匹配能力。
5. 构建预览摘要绑定工作区、草稿 revision、Generation、版本、定义、布局和依赖。后续下载/构建要求同一摘要和 If-Match；审阅后变化返回 409。相同预览重复构建返回原 Release/Candidate。激活后 Generation 已变化，旧预览失效。
6. 构建使用原有 Bundle 扫描、文件摘要和签名校验。Release 与已验证 Candidate 在本地控制事务中一次提交；失败回滚记录并清理本次包。无新发布体系，不自动批准或激活。无效 Candidate 保留验证问题以供处理，不能发布。
7. 开发环境可显式 Publish；其他环境通过 Catalog 的 Submit、Approve、Activate。要求签名时，先下载 Bundle，在组织可信签名流程中签名，再上传同一 manifest 的 signed ZIP 构建。工作台不保管私钥、不自行签名，不关闭生产扫描或签名要求。

字段检查覆盖已声明输入字段、必填字段、字面量约束、已知上游输出路径和基础类型兼容性；继承输入、workflow-input、未知 Schema 字段仍由运行时验证并显示警告。并非完整静态类型系统。

新增管理员 API（相同工作区草稿访问控制）：

| API | 请求与效果 |
| --- | --- |
| `POST /v1/admin/workflow-drafts/{id}/resolve` | If-Match + `{version}`，只读依赖/风险/manifest 预览及 build_digest |
| `POST .../{id}/bundle` | If-Match + `{version, build_digest}`，下载确定性未签名 ZIP，无发布副作用 |
| `POST .../{id}/build` | 同上，经原有扫描/签名规则创建 Release/Candidate |
| `POST .../{id}/build-signed` | If-Match + multipart file/version/build_digest，校验签名及预览一致性后构建 |
| `POST /v1/admin/solution-assist` | `{solution_id, goal, targets?}`，只返回未保存建议 |

#### AI 草稿建议

在工作台 AI draft proposal 中输入 Solution ID、目标和可选能力范围。模型可建议标题、描述、Workflow 节点、依赖与字段绑定，并说明选择理由。用户审阅后 Apply as editable draft，只创建可继续编辑的草稿；不会构建、批准、发布或调用业务工具。模型不能指定角色、权限、签名或审批设置。

后端复用现有模型配置与 Azure API key/Entra 路径，但只调用普通 ChatModel，不进入 Agent 工具执行循环。能力范围最多 100 项，目录 JSON 最多 120 KB，单次模型调用限时 60 秒，建议最多 30 个 DAG 节点，响应最多 128 KB。返回前验证结构、已激活目标、字段及依赖；拒绝工具调用、虚构能力和非法草稿。离线 DemoAgentModel 返回 503，不以模板冒充 AI。已用受控模型测试正确/错误建议和零持久化副作用；真实提供方推理仍需按部署环境验证。

实现入口：[依赖构建器](../src/sage/solution_builder.py)、[构建 API](../src/sage/solution_build_api.py)、[字段校验](../src/sage/workflow_contracts.py)、[AI 建议服务](../src/sage/solution_assist.py)、[工作台](../src/sage/static/solution-workbench.js)。

实现入口：[草稿模型](../src/sage/workflow_draft_models.py)、[草稿服务](../src/sage/workflow_drafts.py)、[管理 API](../src/sage/workflow_api.py)、[前端模块](../src/sage/static/workflow-designer.js)。本版本采用独立原生 JavaScript 模块及简单 DAG 展示，不包含 React Flow；连线通过依赖表单设置，不支持拖拽端口连线。

回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_workflow_drafts.py tests/test_workflow_api.py tests/test_control_store.py tests/test_tool_registration.py -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
node --check src/sage/static/workflow-designer.js
node --check src/sage/static/solution-workbench.js
.\.venv\Scripts\python.exe -m pytest tests/test_solution_builder.py tests/test_solution_build_api.py tests/test_solution_assist.py -q
.\.venv\Scripts\python.exe scripts/smoke_solution_workbench.py --url http://127.0.0.1:8014 --output "$env:TEMP\sage-workbench-check"
.\.venv\Scripts\python.exe scripts/smoke_workflow_designer.py --url http://127.0.0.1:8013 --output "$env:TEMP\sage-designer-check"
```

浏览器脚本需要可选 Playwright 和已安装的 Edge，测试目标必须为独立且关闭管理鉴权的开发预览。设计器脚本正常结束时删除测试草稿；工作台脚本会创建并激活示例 Release，保留审计记录，分别覆盖开发快捷发布和提交/审批/激活。AI UI 成功建议使用受控响应，离线不可用状态使用真实 API。不应指向生产或用户真实数据。端口需按实际启动值替换。

## 1. 目标与核心决策

为 SAGE 增加可通过 UI 定制的流程设计器，使用户无需手写 Subagents JSON 或 Workflow YAML，即可组合 Agent、Skill、原生 MCP Tool 和人工任务，配置串行、并行、条件判断、输入输出映射及发布运行。

核心决策：

1. 固定流程使用版本化 Workflow 定义，由 Temporal 执行；不在浏览器或 API 进程中新建调度引擎。
2. 第一阶段使用当前无环 DAG 契约，画布位置等展示信息单独保存，不混入执行定义。
3. 多 Agent 固定编排以独立 Agent 为节点；Agent 内部 Subagents 不直接成为全局流程节点。
4. 模型自主调度与确定性流程分开配置、分开呈现，避免把“允许调用”误画成“必定执行”。
5. 回环采用有上限、有退出条件的循环容器，需要后端扩展；不直接放开 DAG 或 Agent delegation 的循环限制。
6. 保存草稿不改变线上运行。发布进入既有 Solution Release、Catalog Candidate、审批和 Generation 激活流程。
7. 审批针对最终实际调用参数；发布审批、业务人工任务和高风险调用审批是不同对象，不互相替代。
8. 未确认结果的外部写操作不得自动重发，不承诺跨外部系统 exactly-once。

### 1.1 用户与典型场景

| 用户 | 主要操作 | 成功标准 |
| --- | --- | --- |
| 流程作者 | 选择能力、连线、绑定字段、调试、提交发布 | 不写 YAML 也能完成固定流程 |
| 能力管理员 | 管理可选 Agent/Skill/Tool，审查依赖和风险 | 未发布、失效或无权限能力不能执行 |
| 审批人员 | 查看实际参数、证据、版本和审批范围 | 可准确判断批准的是哪一次操作 |
| 业务使用者 | 填写输入、启动流程、处理人工任务、查看结果 | 不需要理解底层图和 Temporal |
| 运维人员 | 查看节点、轮次、错误、取消和恢复记录 | 可解释故障位置，不靠盲目重试恢复 |

首要场景：起草与评审、多个分析 Agent 并行汇总、检索与处理工具组合、人工批准后发布，以及限定轮次的修改与复核。

### 1.2 非目标

- 不建设 BPMN 全量实现，也不支持任意 Python/JavaScript 表达式或浏览器执行脚本节点。
- 不把所有 Tool 包装成 Skill，不要求用户为独立 Skill 新建 Agent。
- 不在第一阶段支持任意图回边、无限循环、动态嵌套 Workflow、任意 foreach 扩展。
- 不默认支持多人实时协同编辑、运行中修改图、任意节点跳过或失败节点强制继续。
- 不通过 UI 绕过 Policy、角色、发布锁、数据隔离或模型预算。
- 不为该功能重写整个现有前端或替换 Temporal/DeepAgents/LangGraph。

## 2. 现有实现基线与缺口

主要依据：[Workflow 契约](../src/sage/workflows.py)、[Temporal Workflow](../src/sage/temporal_workflow.py)、[Worker](../src/sage/temporal_worker.py)、[Trigger](../src/sage/triggers.py)、[多 Agent 编排说明](multi-agent-orchestration.zh-CN.md)。

| 能力 | 当前状态 | 设计器处理方式 |
| --- | --- | --- |
| WorkflowTemplate alpha1/beta1 | 已有解析与 DAG 校验 | MVP 导出 beta1，禁止静默改写旧版本 |
| Skill、原生 Tool、独立 Agent 节点 | 已有模型和执行分支 | MVP 开放，按实际调用 Schema 配置 |
| HumanTask | 已有创建、等待信号机制 | 补齐表单、分支映射和超时验收后开放 |
| 串行、并行 | dependsOn、maxConcurrency 已执行 | UI 连线生成依赖，不以画布位置决定顺序 |
| 并发执行策略 | 当前按就绪节点排序并分批 gather | 不宣称是完成一个即补充一个的工作队列 |
| 条件与汇总 | when、all/all-settled 已执行 | 单条件优先；复合条件不能直接写入现有模型 |
| 数据绑定 | literal、workflow-input、step-output、artifact | 字段选择器生成 JSON Pointer |
| 重试、超时 | Activity 相关参数已有 | 补充风险约束；不是整个业务流程的总时限 |
| failurePolicy: escalate | 契约允许，当前没有自动创建升级人工任务的专门分支 | 隐藏该选项，补实现并测试后再开放 |
| concurrencyKey | 契约字段存在，当前调度未实现对应互斥 | MVP 禁止配置，不展示为生效能力 |
| workflow 嵌套节点 | 契约允许，Worker 当前不支持该 kind | 导入时报告不支持；不能发布运行 |
| Workflow outputs | 独立 workflow Skill 会映射 outputs；通用 Workflow 当前返回 results | 补齐通用输出适配，不承诺两种入口已一致 |
| 独立 workflow Skill | 当前仅允许 Skill/Tool 节点 | 多 Agent 设计器使用 Solution Workflow，不套入此入口 |
| Agent delegation | 声明目标、调用上限、拒绝循环；已有持久调用路径 | 后续提供专门权限图，保持无环 |
| Agent UI | 已有 Capabilities、Subagents JSON；没有完整 delegation 表单 | 单独补齐；编辑必须保留已有 delegations |
| 持久审批 | 已有 Tool/普通 Skill/Agent 子调用路径和调用账本 | 复用并补充节点映射、恢复与终态对账 |
| Trigger | 已有 schedule/event 契约和 Temporal Provider | 不是完整调度管理面；接入另列阶段 |
| 流程设计器 | 当前无可视化编辑与独立 Workflow 草稿管理闭环 | 本方案新增 |

当前校验已有约束：节点数 1–500，maxConcurrency 1–100，节点 ID 唯一、依赖存在、图无环；节点输入绑定和条件只允许引用祖先输出。平台可进一步收紧这些上限。

现有 `inheritInput` 默认为 true。新建 UI 节点显式使用 false，按需映射字段，减少意外传递敏感输入；导入已有流程必须保留其原值。

真实 Postgres/Temporal 进程重启恢复及实际 Entra 模型调用尚未完成环境验收。内存 Checkpointer 测试不能替代这些验收，参见 [本地持久化说明](local-durable.zh-CN.md)。

## 3. 功能需求与优先级

优先级定义：P0 为可发布的固定流程闭环，P1 为受控扩展，P2 为后续增强。每项 ID 用于关联实现任务和验收测试。

| ID | 优先级 | 功能 | 最小要求 |
| --- | --- | --- | --- |
| FD-01 | P0 | 流程列表 | 所属 Solution、名称、版本、草稿状态、发布状态、搜索和复制 |
| FD-02 | P0 | 草稿编辑 | 新建、保存、冲突检测、撤销/重做、离开未保存提示 |
| FD-03 | P0 | 节点与连线 | 添加、删除、复制、移动、依赖连线、防止自连及非法循环 |
| FD-04 | P0 | 节点表单 | 目标能力、参数、超时、重试、输出约束、风险与版本 |
| FD-05 | P0 | 数据映射 | 上游字段选择、常量、流程输入、类型检查、缺失字段提示 |
| FD-06 | P0 | 控制流 | 串行、有限并发、单条件分支、汇总及跳过语义 |
| FD-07 | P0 | 人工任务与调用审批 | 分别展示；拒绝、过期、取消不能被当作成功继续 |
| FD-08 | P0 | 分层校验 | 错误定位到节点/字段，支持保存不完整草稿，发布必须通过 |
| FD-09 | P0 | 版本与发布 | 依赖锁、差异预览、Candidate、生产审批、激活和回滚 |
| FD-10 | P0 | 试运行与观察 | 静态检查、模拟运行、受控真实运行；节点事件与结果 |
| FD-11 | P0 | 导入导出 | YAML/JSON 执行定义、设计包；不支持内容不得静默丢弃 |
| FD-12 | P0 | 安全与审计 | 服务端授权、秘密引用、脱敏、幂等、版本绑定 |
| FD-13 | P1 | 有限循环 | 循环体、状态映射、退出条件、轮次与预算上限 |
| FD-14 | P1 | 自主调度配置 | 独立 delegation 配置、调用权限图、预算与真实调用树 |
| FD-15 | P1 | 定时与事件触发 | 版本绑定、时间语义、身份、去重、启停与重叠策略 |
| FD-16 | P2 | 模板与辅助生成 | 模板库、自然语言生成草稿；生成后仍经过相同校验发布 |

## 4. UI 与交互设计

### 4.1 页面结构

在现有 Admin 增加 Workflows 入口，不改变 Agents、Skills 和 MCP Servers 的所有权边界。普通使用者通过所属 Solution 的运行入口使用已发布流程。

- 列表页：草稿、发布版本和最近运行，筛选条件保留在 URL；无权限对象不可发现。
- 设计页：顶部名称/保存状态/校验/发布操作，左侧节点与能力列表，中间画布，右侧选中节点属性。
- 底部可折叠面板：校验问题、编译后的 YAML、依赖差异；不把 YAML 当作必填项。
- 运行页：固定版本的只读图、输入摘要、时间线、节点结果、审批与子 Run；不编辑正在运行的图。
- 小屏：节点顺序列表与独立属性页，保留所有关键操作，不要求精确拖拽完成配置。

遵循现有站点样式及 Clawpilot 主题变量，工具按钮使用现有图标库与 tooltip。状态同时用文字和图标表达，不只依赖颜色。表单提供键盘访问、焦点顺序和错误关联。

### 4.2 节点与连线含义

业务执行节点只有 Agent、Skill、Tool、HumanTask。Start、End、条件、并行、汇总在 MVP 中是编辑器辅助结构，不是新增 Worker kind。

- Start 表示流程输入；End 表示输出选择，不产生外部调用。
- 串行 A → B 编译为 B.dependsOn 包含 A；把 B 的字段绑定到 A 时，同时要求建立该依赖。
- 并行分支共享前置依赖，配合 maxConcurrency；汇总节点明确依赖所有所需分支。
- 条件辅助结构编译为分支入口的 when；第一阶段只允许现有单条件能无损表达的结构。
- “其他情况”只有在能明确表达缺失值规则时才能生成，不能把 not-equals 当作通用 else。
- 空分支、复杂多入口控制流或无法无损编译的图给出错误，不猜测作者意图。
- 数据连线与控制依赖在 UI 上区分；执行事实源仍是 dependsOn、bindings、when。

`maxConcurrency: 1` 只限制并发，不代表按画布位置或 YAML 顺序执行。需要固定顺序时必须建立依赖。

### 4.3 属性面板

通用字段：稳定节点 ID、显示名称、目标版本、输入、输出约束、超时、重试、失败策略、依赖与条件。名称变化不改变节点 ID；复制节点必须产生新 ID。

Agent 节点选择已发布独立 Agent。当前 Worker 将输入中的 `message` 取作 goal，其余字段作为输入；UI 可显示“本次任务”，但必须正确映射为 message，而不是默默修改 Agent 全局 instructions。

Skill/Tool 参数表单来自实际调用 Schema。原生 Tool 使用完整 `tool:mcp:` 能力 ID，不混同 `skill:`；Agent 内模型包装函数的 `{arguments: ...}` 不得误用于直接 Tool 调用参数。

Schema 不完整时允许受控 JSON 编辑，并显示“无法静态验证字段”，不伪造可选字段。Schema `$ref` 仅从受控资源解析，不允许任意网络读取。

HumanTask 使用业务审批表单与结果映射。实际结果字段由后端任务契约提供；设计器不能假设其与高风险调用审批结果完全相同。

### 4.4 数据映射和汇总

输入合并顺序沿用当前运行时：流程输入（仅 inheritInput=true 时）、节点 input、bindings 覆盖。UI 展示最终来源及覆盖关系，绑定键当前是顶层参数名，不支持把它假装成任意嵌套写入路径。

上游选择器展示结果 envelope 和可用字段。Agent 文本通常位于 `/output/message`；不得假设所有节点都返回相同形状，也不得将文本模型回答自动视作已验证 JSON。

`joinPolicy: all` 遇到非 completed 依赖会跳过；`all-settled` 允许汇总失败和跳过结果，但不会凭空补齐其 output。第一阶段此类汇总优先绑定完整分支 envelope，让汇总者显式处理 `_sage.status`，避免绑定不存在的成功字段。

大结果使用受权限保护的 Artifact ID，表单不自动展开整个对象。读取 Artifact 仍需执行身份校验；ID 本身不是访问授权。

## 5. 架构与职责

逻辑路径：设计器 → Workflow 草稿 API → 校验/编译服务 → 发布包与 Release → Catalog 激活 → Durable Run → Temporal Workflow → Worker → 既有调用 API → Agent/Skill/Tool。

| 层 | 职责 | 禁止事项 |
| --- | --- | --- |
| UI | 编辑、映射、预校验、展示服务端状态 | 自行调度外部操作或判断最终授权 |
| 草稿服务 | 所有权、revision、设计元数据、草稿存取 | 保存时改变 Active Generation |
| 编译/校验服务 | 规范化、DAG/类型/能力/风险校验、确定性产物 | 通过运行 Tool 来完成“静态校验” |
| 发布服务 | 依赖闭包、版本摘要、Solution 包与 Candidate | 绕过生产审批或静默换依赖版本 |
| Temporal | 固定版本控制流、持久等待、恢复 | 在 Workflow 代码中直接做网络/模型 I/O |
| Worker/调用 API | 授权、实际调用、Schema、账本、审批 | 重发结果未知的写操作 |
| 运行投影 | 将事件组织为节点、轮次、子 Run 和审批状态 | 根据前端缓存推断权威执行状态 |

前端保持现有静态页面入口。建议将画布封装为局部模块，优先评估成熟图编辑库；如选 React Flow，仅在设计器区域挂载构建产物，不迁移整个站点。最终选型先验证节点自定义、导入布局、键盘操作、性能和现有样式兼容性，再固定依赖版本。

## 6. 数据模型与版本策略

### 6.1 三类数据分离

| 对象 | 拟新增核心字段 | 用途 |
| --- | --- | --- |
| WorkflowDraft | draft_id、workspace_id、solution_id、workflow_id、revision、definition、design、dependency_intent、input_schema、output_schema | 可修改草稿与入口表单；后两项不是当前 WorkflowTemplate 字段 |
| WorkflowDesign | schema_version、节点名称/位置/分组、视口、辅助结构映射 | 纯编辑信息，不能决定隐藏执行逻辑 |
| WorkflowValidationReport | draft_revision、definition_digest、issues、resolved_dependencies、compiler_version、runtime_requirements | 绑定特定 revision 的校验结果 |
| Workflow 发布包 | workflow.yaml、design.json、入口契约、依赖锁、校验清单 | 产生不可变发布资产 |
| NodeRun 投影 | run_id、step_id、iteration_path、attempt、turn_index、status、child_run_id、event_sequence | 执行观察，不写回草稿 |

复用已有 SolutionRelease/Generation 作为发布事实源，不再创建一套平行激活状态。新资产如何进入 manifest、组件摘要及迁移由发布实现显式定义，不把未识别字段塞入旧契约。

设计元数据不影响执行定义的语义摘要；可以有独立 design_digest。若完整包摘要包含设计资产，移动节点可能改变包摘要，但不能误报为执行行为变化。

### 6.2 保存、校验和编译

1. 保存可以接受不完整的业务草稿，但必须满足大小、结构、权限和秘密检查；不完整草稿不可编译发布。
2. 使用 revision/ETag 与 If-Match 做乐观并发。旧 revision 更新返回冲突，提示比较差异；不得覆盖他人内容。
3. 对相同规范化定义、依赖锁和编译器版本，编译结果应确定一致。
4. 校验报告绑定 draft revision 和依赖解析结果；草稿或依赖变化后旧报告失效。
5. 发布必须重新做服务端校验，并核对 UI 审阅过的摘要；异步响应只更新对应 revision 的 UI。

### 6.3 兼容当前执行契约的示例

下面是 MVP 可以生成的 WorkflowTemplate，不包含画布元数据。Agent ID 仅作说明，实际发布前必须替换为目标 Release 中已锁定的 Agent。通过 Schema 校验不等于已获授权或可在本环境执行。

```yaml
apiVersion: sage.dev/v1beta1
kind: WorkflowTemplate
workflowId: draft-review
version: 1.0.0
maxConcurrency: 2
steps:
  - id: draft
    kind: agent
    target: writer
    inheritInput: false
    input:
      message: Draft a response for the supplied request.
    bindings:
      request:
        source: workflow-input
        path: /request
  - id: accuracy
    kind: agent
    target: fact-reviewer
    dependsOn: [draft]
    inheritInput: false
    input:
      message: Review the draft for factual accuracy.
    bindings:
      draft:
        source: step-output
        stepId: draft
        path: /output/message
  - id: clarity
    kind: agent
    target: style-reviewer
    dependsOn: [draft]
    inheritInput: false
    input:
      message: Review the draft for clarity.
    bindings:
      draft:
        source: step-output
        stepId: draft
        path: /output/message
  - id: finalize
    kind: agent
    target: editor
    dependsOn: [accuracy, clarity]
    inheritInput: false
    bindings:
      accuracy:
        source: step-output
        stepId: accuracy
        path: /output/message
      clarity:
        source: step-output
        stepId: clarity
        path: /output/message
      draft:
        source: step-output
        stepId: draft
        path: /output/message
    input:
      message: Produce the final response using the draft and both reviews.
outputs:
  answer:
    source: step-output
    stepId: finalize
    path: /output/message
```

当前通用 Workflow 仍返回 results；设计器第一阶段必须补齐统一输出处理，或在运行 UI 明确从 results 展示，不得把本例 outputs 宣称为已由所有入口处理。

### 6.4 导入与迁移

- 导入执行 YAML 时先解析和校验，再生成默认布局。仅根据现有依赖还原语义，不保证还原原始分组美术布局。
- 导出设计包时同时保留执行定义和布局；纯 YAML 导出不带私有字段。
- 不支持的 kind、版本或字段以只读方式展示并报错，禁止删除后假装导入成功。
- MVP 不自动更新旧 API 版本；后续循环能力采用明确的新契约版本，升级前显示差异并保留原发布产物。
- 导入包限制大小、节点数、嵌套深度和解包路径；文件名不能逃逸工作目录。

## 7. 控制流与循环语义

### 7.1 固定 DAG

就绪条件由依赖完成结果决定，再应用 joinPolicy 和 when。流程 UI 必须区分运行失败、条件跳过和依赖跳过；跳过不是错误，也不是成功执行。

Activity 重试用于临时故障，不是业务“再修改一次”。默认 maximum_attempts=1；读操作可以配置重试，写操作是否重试由调用账本及外部幂等保障决定，不能仅凭作者勾选放开。

MVP 不提供自动补偿节点。确需撤销外部变更时，作为独立受治理能力显式配置，经过相同权限和审批。

### 7.2 有限循环容器（P1，拟新增）

推荐语义为 do-until：至少执行一轮，轮末计算退出条件。界面明确显示该语义；不同时引入含糊的 while/until 多种模式。

循环配置需要：稳定 loop_id、无环 body、initial_state、逐轮 state_mapping、结构化 until、max_iterations、循环总时限、全局预算、上限处理策略。

执行规则：

1. 外层图仍是 DAG，循环体也是 DAG；第一版禁止循环嵌套及任意跨容器连线。
2. 每轮只可读取流程输入、显式循环状态和本轮已完成祖先结果；不能读取其他轮的任意中间输出。
3. 执行 body，校验本轮结果，计算下一状态与 until；满足条件则输出最终状态。
4. 未满足且达到轮数、时间或预算上限时，明确失败或创建业务人工任务；不得自动视为成功。
5. 退出条件使用受限表达式契约，不执行脚本。模型输出 PASS/REVISE 等判断必须先满足输出 Schema。
6. 同一轮审批恢复沿用调用身份；新一轮生成不同调用身份和审批 subject，不能沿用旧参数的授权。
7. 循环状态、轮次、条件判断结果持久记录；Worker 重启后从确定状态恢复。
8. 父 Run 取消阻止所有后续轮次和子调用；已经在外部发生的写操作不宣称被撤销。

推荐示例：循环体“起草/修改 → 评审”，until 为评审结构化结果等于 PASS，max_iterations=3，超限进入人工任务。重复使用同一 Agent 是允许的，但不同轮次必须有不同执行身份。

建议身份元组为 `(root_run_id, workflow_digest, step_id, iteration_path, logical_call_id)`，再生成稳定摘要；attempt 和 approval turn 是同一次逻辑调用的恢复记录，不应导致重新产生外部写操作。该身份扩展必须保持旧流程恢复兼容。

Temporal 内直接解释受限循环体即可，不为每轮创建一套自制引擎。循环执行版本必须固定；升级通过 Worker 版本管理或 Temporal 兼容机制处理，不能让已运行实例采用新语义。历史大小必须有界，先限制轮次和节点数，再评估 Continue-As-New。

### 7.3 自主调度（P1）

独立页面配置父 Agent 可委派的目标、alias、description、max_calls、模型/工具/子 Agent 预算。权限图表示“允许调用”，运行页展示“实际调用”，两者不能混同。

Embedded Subagents 仍在 Agent 内配置角色与 capability_ids。独立 delegations 使用 Agent ID，发布时校验目标和无环关系；不允许 A → B → A。若需求是反复评审，优先使用有限循环容器，而不是递归授权图。

固定 Workflow 的一个 Agent 节点可以内部自主委派；全局图展示该节点，详情展开子 Run。多级调用共享父级安全边界并累计预算，不能通过新增子 Agent 重置总限额。

## 8. 拟新增 API 与错误契约

以下路径均为设计建议，实施时统一到现有路由风格；不是已有接口清单。

| 方法与路径 | 用途 | 关键约束 |
| --- | --- | --- |
| GET/POST /v1/admin/workflow-drafts | 分页列表/创建 | Solution 和 workspace 授权 |
| GET/PUT /v1/admin/workflow-drafts/{id} | 读取/保存 | If-Match；返回 revision |
| DELETE /v1/admin/workflow-drafts/{id} | 删除草稿 | 不删除发布资产及运行历史 |
| POST /v1/admin/workflow-drafts/{id}/validate | 校验并编译预览 | 请求指定 revision，不执行外部调用 |
| POST /v1/admin/workflow-drafts/{id}/build-release | 生成发布包并进入现有发布链 | 幂等键、锁定 revision、返回 Release/Candidate 引用 |
| POST /v1/admin/workflow-drafts/import | 导入 YAML/设计包 | 大小、路径、版本检查 |
| GET /v1/admin/workflow-drafts/{id}/export | 导出执行 YAML 或设计包 | 脱敏，不导出凭据 |
| POST /v1/admin/workflow-drafts/{id}/simulate | 用显式 mock 数据模拟 | 禁止连接真实 Tool，结果不可冒充实际运行 |
| GET /v1/workflow-runs/{run_id}/nodes | 节点执行投影 | 与 Run 相同授权、游标和脱敏 |

真实运行复用现有 `POST /v1/durable-runs` 及结果/取消 API，按已发布 Solution 中锁定的 Workflow 启动；具体请求沿用 [api_models.py](../src/sage/api_models.py)，不新增接受任意客户端执行图的公共接口。

能力选择器复用现有 Catalog/Capability 查询，并补齐有效版本、输入 Schema、风险、不可选原因等缺失字段。前端不从名称拼接能力 ID。

校验 issue 建议字段：code、severity、node_id、field_path、message、suggestion。常见 code 包括 graph_cycle、unknown_dependency、unavailable_output、schema_mismatch、unsupported_runtime_feature、capability_not_locked、policy_denied、checkpoint_required、sensitive_literal。

错误建议：403 权限不足，409 revision/发布状态冲突，413 内容超限，422 定义或映射非法，503 持久后端/必要 Checkpointer 不可用。Schema 未知与明确类型错误要分开：前者可给警告并保留运行时检查，后者阻止发布。

## 9. 发布、权限和审批

### 9.1 发布生命周期

草稿 → 校验通过 → 生成锁定发布包 → Candidate 校验 → 提交审批 → 批准 → 激活 Generation。

“草稿校验通过”只是该 revision 的事实，不是永久状态。开发环境可沿用受服务端环境限制的一键发布；生产不得出现可绕过审批的相同快捷路径。

依赖解析包括节点直接目标、Agent 的能力与 delegation、Skill 声明的依赖。生成完整锁闭包尚需实现，不假设 Catalog 当前会自动完成所有传递依赖补齐。缺失、版本冲突、Schema/配置漂移必须阻断发布。

差异页区分执行逻辑、依赖/风险、输入输出契约和纯布局变化。旧 Run 固定旧 Generation/Release；回滚只影响后续运行，不修改执行中的实例。

### 9.2 三种审批

| 类型 | 授权对象 | 恢复行为 |
| --- | --- | --- |
| 发布审批 | 候选配置及摘要 | 允许激活，不授权具体业务写调用 |
| 业务人工任务 | 审核内容或流程决策 | 输出业务结果，由明确分支决定下一步 |
| 高风险调用审批 | 一次实际调用、最终参数、能力和版本 | 仅恢复该调用；不扩大可调用能力集合 |

调用审批继承现有身份绑定：run/workspace/owner、Generation/Release、能力及内容摘要、input_digest、call_id；循环扩展时再加入轮次身份。审批 UI 显示脱敏且有大小上限的最终参数视图。

拒绝、过期、能力漂移、父 Run 取消后都不能继续 dispatch。重复提交相同决定可以幂等处理，修改已经完成的决定必须拒绝。

### 9.3 安全与数据治理

- 前端可选能力不是授权结果；保存、发布、启动、实际调用各边界独立校验。
- 草稿、模型输入、Checkpoint 和 Artifact 均可能含敏感业务数据；定义保留期、加密及访问策略，不仅对日志脱敏。
- 密钥通过受控连接引用，不作为 input 常量、示例数据、URL 或导出字段保存。
- 节点模板、Tool 输出和模型生成配置均是不可信数据，显示时转义，绝不拼接为可执行 HTML。
- 所有运行继承固定 principal/workspace/release；客户端不可伪造 Generation、父身份或批准字段。
- 跨租户/跨 Solution 对象发现和引用必须禁止；Artifact、子 Run 和审批采用同样边界。

## 10. 运行状态、恢复与可观测性

拟新增节点投影状态：pending、ready、running、awaiting-approval、completed、skipped、failed、cancelled、outcome-uncertain。它们是设计器统一视图，不是声明现有全部存储已经使用这些枚举。

运行页展示节点开始/结束、耗时、attempt、approval turn、循环轮次、调用量、失败分类、子 Run 和审批链接。不展示模型隐藏推理、凭据或完整敏感请求正文。

复用 Run events，并为节点生命周期补充 step_id、iteration_path、invocation_id 等关联信息。事件持久后再供前端消费，按 sequence 增量读取；UI 刷新不触发重新执行。

恢复规则：

- 完成调用从账本回放；批准后使用原逻辑调用身份恢复。
- 网络超时不等于外部操作失败；未确认写结果进入 outcome-uncertain 并等待人工对账。
- 不在 MVP 提供“从任意节点重跑”。安全重跑应新建 Run、显式选择输入，并再次校验权限与风险。
- 增加 Temporal 终态与 Run/子 Run 终态对账，覆盖父流程失败而子记录仍 running 的情况。
- 审批等待时限与 Activity 执行超时分开；采用 Temporal timer/事件处理过期，不能无限等信号。
- 取消需要确认父子调用的后续 dispatch 被阻断；无法保证中断已经提交到外部系统的操作。

当前原子 JSON 控制存储只适合单 API 进程。新增草稿可沿用该开发模式，但生产多实例需要事务性存储和数据库并发控制；不可把文件锁宣传为多副本保障。

## 11. 定时与事件调度（P1）

这里的“调度”与图内依赖调度分开：Trigger 决定什么时候启动一次流程，Workflow 决定启动后节点如何执行。

已有 TriggerDefinition 支持 schedule/event、cron/event_type、payload_template、allow/forbid/replace，并有 Temporal Provider。仍需补齐服务端生命周期管理、权限、发布联动和 UI，不能仅暴露 cron 输入框就视为交付。

计划功能：

- 手动触发作为 MVP；定时触发提供时区、下次运行时间预览、启停、错过时间处理和重叠策略。
- 当前契约没有显式时区/补跑窗口字段，需新增并定义 DST 规则；旧定义按明确兼容规则解释。
- 发布绑定默认固定 Release。升级触发器指向新版本必须审阅和审计，不在每次触发时静默选择最新版本。
- 定时运行使用明确的执行身份与权限生命周期；禁止长期复用浏览器 Token 或放大作者权限。
- 事件入口需要鉴权、workspace/Solution 隔离、event_id 去重和 payload Schema；不要只依赖内存注册表。
- Provider 注册必须幂等，支持更新、启停、删除与重启后重建；当前事件注册表为进程内数据。
- replace 的取消旧运行不代表撤销旧外部写入；存在未决写结果时不自动并发启动替代运行。

## 12. 试运行与测试策略

### 12.1 三种验证方式

1. 静态校验：只解析和检查定义、依赖、类型、权限及后端要求，不调用模型或外部 Tool。
2. 模拟运行：作者为节点提供 mock 输出/错误/审批决定，执行相同控制流语义的受控模拟；不产生真实业务副作用。结果明确标注为模拟，不能当成生产评测证据。
3. 受控真实运行：先生成并发布开发/测试版本，再调用现有 Durable API；执行真实身份、风险规则、预算和审批。MCP Servers 的单 Tool 测试可辅助排查，但不替代整图验证。

模拟器只作校验用途，不成为生产调度引擎；与 Temporal 用同一组语义测试验证一致性。无持久后端时仍可设计、校验和导出，真实持久运行明确显示不可用，不自动降级到内存后台任务。

### 12.2 验收矩阵

| 验收 ID | 覆盖需求 | 场景与通过标准 |
| --- | --- | --- |
| AT-01 | FD-01/02 | 新建、保存、重开图与字段一致；旧 revision 保存被拒绝；保存不发布 |
| AT-02 | FD-03/06 | A → B → C 严格依赖执行；并行峰值不超过 maxConcurrency；汇总等待所需分支 |
| AT-03 | FD-05/08 | 循环依赖、跨非祖先绑定、未知目标、明确类型不匹配均定位到字段并阻止发布 |
| AT-04 | FD-06 | 条件缺失值、跳过传播、all-settled 失败分支按约定处理，无不存在字段误读 |
| AT-05 | FD-07/12 | 高风险调用只在最终参数批准后执行一次；重复决定不重复调用；拒绝/过期/漂移均阻止执行 |
| AT-06 | FD-09 | 开发快捷发布有效；生产强制审批；依赖漂移阻止激活；旧 Run 不切换版本 |
| AT-07 | FD-10 | 运行刷新不发起调用；子审批归属正确；Agent 多次审批可连续恢复 |
| AT-08 | FD-11 | YAML → 图 → YAML 语义一致；设计包布局保留；不支持字段明确报错 |
| AT-09 | FD-12 | 跨身份/跨 workspace/跨 Solution 请求被拒绝；输入秘密不出现在日志/导出/页面 |
| AT-10 | FD-10/12 | API/Worker 重启后通过真实 Temporal/Postgres 恢复；未知写结果不重发；父子终态一致 |
| AT-11 | FD-13 | 循环首轮退出、最后一轮退出、超限、审批恢复、取消、重启均正确；轮次身份隔离 |
| AT-12 | FD-14 | delegation 无环且预算累计；权限图与实际调用树区分；编辑保留原 delegation 字段 |
| AT-13 | FD-15 | 时区/DST、重复事件、并发重叠、触发器升级和重启重建通过；运行身份不提升权限 |
| AT-14 | FD-02/04 | 桌面与移动视口无文字遮挡，键盘能完成节点配置；错误和未保存状态可感知 |

测试层次：模型与编译单测、API 权限/版本集成、Temporal 控制流与重放、真实基础设施恢复、Playwright UI 回归。用可计数的假 Tool 验证调用次数，不能仅凭 UI 显示 completed 判断未重复写入。

初步性能目标（验收环境记录硬件和浏览器后固定）：100 节点图打开不超过 2 秒，常规编辑响应不超过 100 毫秒，离线结构校验不超过 1 秒；500 节点可加载和校验，无崩溃或无响应。外部依赖检查单列耗时，不混入离线指标。

## 13. 实施拆分与交付门槛

### 阶段 A：契约与草稿闭环

- 确定 WorkflowDraft/Design/ValidationReport，增加开发存储及迁移，明确生产存储策略。
- 草稿 CRUD、revision、导入导出、服务端编译与校验；建立执行语义摘要与依赖锁规则。
- 增加现有 Workflow 往返测试，拒绝当前 Worker 不支持的选项。
- 交付：即使没有画布，也能通过 API 保存、验证、导出当前合法 DAG。
- 门槛：AT-01、AT-03、AT-08，以及草稿服务授权检查通过。

### 阶段 B：固定流程设计器与发布运行（P0）

- 实现列表、画布/移动列表、属性表单、绑定器、条件/并行/汇总辅助结构。
- 补齐 HumanTask 拒绝/超时语义、通用输出处理、节点事件和终态对账。
- 生成完整依赖锁，接通现有 Candidate 发布和 Durable Run；实现模拟和受控试运行。
- 交付：业务作者从 UI 创建并发布多 Agent DAG，查看节点执行、审批与结果。
- 门槛：AT-01 至 AT-10、AT-14 通过；真实重启验收未完成时只能标记开发预览，不宣称生产可用。

### 阶段 C：有限循环（P1）

- 新增版本化循环契约、编译检查、Temporal 执行、逐轮状态与稳定调用身份。
- UI 提供循环容器、状态映射、退出条件、轮次上限和人工接管策略。
- 交付：最多三轮的起草/评审模板，以及批准、拒绝、超限、重启测试。
- 门槛：AT-11 及现有审批/幂等/重放回归通过。

### 阶段 D：自主调度与触发器（P1）

- 完成 delegation 表单和权限图、预算累计与实际调用树。
- 完成定时/事件触发管理、身份和版本锁、重启恢复与审计。
- 交付：受控自主调度与定时/事件启动，可分别启用，不阻塞固定 DAG 使用。
- 门槛：AT-12、AT-13 通过。

### 阶段 E：模板与辅助生成（P2）

- 提供“串行处理”“并行评审”“有限修改复核”等模板。
- 自然语言生成的结果只写入草稿；未知能力、非法边、无限循环和未锁依赖必须被正常校验拦截。
- 生成前确认可用能力和 Schema；不能编造 Agent ID、Tool 参数或审批权限。
- 不允许生成操作自动触发生产发布或高风险执行。

## 14. 代码落点与后续生成约束

| 当前文件 | 预期工作 |
| --- | --- |
| [workflows.py](../src/sage/workflows.py) | 保持旧 DAG 契约；增加有版本边界的新能力，不静默放宽 |
| [admin_api.py](../src/sage/admin_api.py) | 草稿/校验/构建发布接口，权限和 revision |
| [control_models.py](../src/sage/control_models.py)、[control_store.py](../src/sage/control_store.py) | 草稿存储、迁移；保留历史摘要和既有对象 |
| [solution_packages.py](../src/sage/solution_packages.py)、[catalog.py](../src/sage/catalog.py) | Workflow 资产、锁闭包、差异与发布校验 |
| [temporal_workflow.py](../src/sage/temporal_workflow.py)、[temporal_worker.py](../src/sage/temporal_worker.py) | 输出、人工等待/终态、循环及版本兼容 |
| [execution_approvals.py](../src/sage/execution_approvals.py)、[skill_orchestration.py](../src/sage/skill_orchestration.py) | 节点/轮次身份、精确审批和父子恢复约束 |
| [triggers.py](../src/sage/triggers.py) | 触发器生命周期、时区/身份/版本接入 |
| [admin.js](../src/sage/static/admin.js)、[admin.html](../src/sage/static/admin.html)、[admin.css](../src/sage/static/admin.css) | 页面入口与独立设计器模块集成，不把全部逻辑继续堆入单文件 |
| [test_workflows.py](../tests/test_workflows.py)、[test_durable.py](../tests/test_durable.py)、[test_triggers.py](../tests/test_triggers.py) | 扩展现有语义回归；草稿/编译/设计器另建聚焦测试 |

后续代码生成任务必须指定：阶段与需求 ID、现有代码入口、本次允许修改的契约、明确非目标、验收 ID、测试命令及未满足的环境条件。一次只完成可验证的纵向闭环，不能以“界面已显示”替代后端执行能力。

推荐首个任务：实现阶段 A 的最小草稿保存、revision 冲突检查及当前 WorkflowTemplate 校验/导出，增加聚焦 API 和往返测试；不同时引入循环、触发器或整个前端重构。

## 15. 评审时需要确认的事项

以下采用建议默认值推进方案，实施对应阶段前确认；不影响先完成阶段 A。

| 决策 | 建议默认值 | 影响 |
| --- | --- | --- |
| 流程归属 | 必须属于 Solution，多 Agent 用通用 Workflow | 发布锁与访问边界 |
| 首批用户 | 能力管理员/流程作者先使用 Admin；业务用户只运行发布版本 | 权限与导航 |
| 回环 | do-until，默认 3 轮，第一版不嵌套，超限转人工或失败 | 契约和恢复复杂度 |
| 真实试运行 | 必须先发布测试版本，不直接执行可变草稿 | 避免绕过锁和审计 |
| 运行中变更 | 禁止修改图，仅审批、取消；新配置启动新 Run | 版本确定性 |
| 定时升级策略 | 固定 Release，显式审批后切换 | 防止无人值守行为漂移 |
| 生产并发编辑/部署 | 事务数据库；开发仍可单进程存储 | 基础设施范围 |
| 数据保留 | 由业务数据分类确定，部署前必须配置 | 草稿、Checkpoint、Artifact 成本与合规 |

## 16. 相关资料

- [多 Agent 编排](multi-agent-orchestration.zh-CN.md)
- [多 Agent Workflow 示例](examples/multi-agent-workflow.yaml)
- [控制面设计](control-plane.zh-CN.md)
- [平台内核设计](platform-kernel.zh-CN.md)
- [原生工具说明](native-tools.md)
- [Windows 持久化与 Entra 验收说明](local-durable.zh-CN.md)

当前首版只增加草稿设计与管理，不改变既有运行、审批和发布行为。后续实现以阶段门槛和可执行测试为准。