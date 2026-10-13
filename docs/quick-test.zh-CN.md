# 本地快速测试

## 当前入口与数据

- 管理台：<http://127.0.0.1:8014/admin>
- 运行工作台：<http://127.0.0.1:8014>
- Temporal：<http://127.0.0.1:8233>
- 默认启动：在项目目录执行 `start-local.cmd`。
- 旧 8012、8015 实例已停止，避免打开不同数据目录导致误判。

2026-09-10 已清空旧实例的自定义 Agent、上传/生成的 Skill、native Tool 注册、
Solution、Workflow draft、Release、Candidate、审批、旧 Generation、运行、记忆、
附件与 Temporal 历史。验收生成的业务数据也再次清空。
原有 MCP Server 配置、凭据引用、发现结果和风险配置保留。
系统启动所需的内置 Skill、默认 Agent 与 `gen_startup` 不属于用户历史。

清理前的原始目录归档为相邻的 `.backup-时间-标识` 目录，不再被应用读取。
这些备份可能包含敏感配置，应保存在本机，不应上传或提交 Git。
源码、示例包与文档未删除。

## 最短测试路径

1. 在 **Solutions → New Solution** 创建定义。填写 ID、Title、Owner，勾选 **Durable**。
   成功后会自动选中新 Solution；失败不会清空输入。
2. 点击 **New workflow**，填写工作流名称和 ID，添加节点。
   最简单的测试是 Skill 节点选择 `echo`，Input JSON 填 `{"text":"hello"}`。
3. 点击设计器的 **Review release** 返回工作台。这个按钮只保存草稿并切换页面，
   不要求完成 Review，也不会单独发布。
4. 点击 **Quick publish and run**。工作台自动解析依赖、构建、校验、开发自动审批、
   激活和启动工作流，不需要勾选 Review 或进入 Catalog。
5. 点击 **Refresh result** 查看完成状态、每个节点输出和运行事件。运行中可取消。
   **Run published** 运行当前已发布版本；编辑后的草稿需重新快速发布。

只发布不运行时使用 **Quick publish**。重复使用已有的三段数字版本时自动递增补丁版本，
例如 `1.0.0` → `1.0.1`。新发布仍会生成 Release 与 Generation，便于锁定运行版本，
只是无需手工管理它们。

## 其他能力

- Agent：**Save draft → Quick publish**，不必先手工 Validate 或创建 Candidate。
- Skill ZIP：上传后使用列表中的 **Quick publish**。发布与是否允许执行是两回事。
- MCP：保存连接、Test、Discover 后，**Register native tool** 和 **Import as Skill**
  在开发环境自动发布；失败时显示原因，可从资源列表重新快速发布。
- 工作流只能选择已生效的能力。先发布依赖，再添加到工作流。

交互运行的会话会固定到首次使用的 Generation。新发布 Agent 后，旧会话可能仍不含该
Agent；遇到 `Agent not found` 请点击运行页 **New thread**，再执行。
切换 Agent 现在会自动新建会话；同一个 Agent 发布新版本后，仍需手动新建会话才能使用新版本。
新会话从空对话记忆开始，不会删除原有运行历史。

## MCP 已发布但模型说未连接

发布成功不等于当前聊天身份有调用权限。默认本地聊天使用 `anonymous / user`，
`write-low`、`write-high`、`destructive` 能力会在发送给模型前被权限策略隐藏。
Azure Entra 登录用于模型服务，并不赋予聊天请求 SAGE 管理员角色。
检查实际运行的身份、Generation、工具风险分类及 `tool.started` 事件；
没有工具调用事件时，不能把模型回答中的“未连接”当作 MCP 网络错误。

如果 Agent 的全部绑定能力都不可访问，运行时现在直接返回
`no accessible bound capabilities`，不再让模型猜测原因。Python 服务重启后生效。

确认工具仅检索、没有写入或其他副作用后，可在管理台选择对应 MCP 工具，
将风险设为 `read-only`，填写未使用的新版本，点击 **Register native tool**。
开发环境自动发布成功后，在运行页点击 **New thread** 使用新的 Generation。
不要覆盖已有不可变版本；不要仅为了测试而降低真实写操作的风险。
若工具确有副作用，应保持原分类，使用具备权限的身份，并保留所需执行审批。

## 保留的限制

- 快速路径仅限 `development`；生产审批和签名流程没有关闭。
- 输入 schema、依赖、内容扫描和不可变版本检查仍在后台执行。
- 有副作用操作的执行审批，以及显式 Human Task，不会被自动批准。
- 上传的 Python 执行包仍受隔离执行策略限制，不能通过快速发布解除禁用。
- 默认启动器使用离线演示模型，不能代表真实 LLM 推理；本地没有 PostgreSQL checkpoint，
  不支持完整的持久化 Agent 节点恢复。此次验证 Agent 创建/发布，未验证真实模型 Agent 节点。
- AI draft proposal 仍是单独的建议确认流程，不属于上述直接建模快速路径。

## 验收证据

使用 Playwright 从 UI 创建资源，没有通过 API 预先创建 Solution。
桌面 1440px 和手机 390px 均完成：Agent 发布、MCP 注册/导入自动发布、
Solution 自动选中、Quick publish and run、Echo + MCP Skill + native MCP Tool 执行。
两个计算节点都返回 `32.4`；运行状态为 completed；重复发布递增版本；刷新后 active 保持。

- [桌面结果](images/quick-flow/completed-1440.png)
- [手机结果](images/quick-flow/completed-390.png)
- [结构化报告](images/quick-flow/report.json)
- [浏览器 trace](images/quick-flow/trace.zip)

验收脚本：`python -m scripts.smoke_quick_flow --headless --output docs/images/quick-flow`。
该脚本会创建真实测试数据，只应在专用开发实例执行。

## 再次清空

先执行 `stop-local.cmd`，待服务完全停止后：

```powershell
.\.venv\Scripts\python.exe scripts/reset_local_state.py --data-root "$env:TEMP\sage-designer-preview" --ports 8014 7233 8233 --confirm-reset
```

脚本拒绝在指定端口仍监听时操作；完整备份后重建空业务状态，默认保留全部 MCP 配置。
`--exclude-mcp` 只用于明确指定要删除的临时 MCP Server ID，不要用于原始连接。