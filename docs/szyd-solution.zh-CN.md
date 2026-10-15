# SZYD 三重一大审核 Solution

## 范围与来源

源码快照来自 `C:\AIProject\skills_collection\szyd\skills`，纳入
[szyd-law](../skills/szyd-law/SKILL.md)。八个原始 skill、共享脚本、规则、
prompts、合成示例和离线测试保留原相对结构，原资产 manifest 未修改。
运行和部署不依赖开发机的原目录。不包含凭据、数据库、向量库或业务材料。

本版使用聊天模型做审核推理，Python 做离线规则、Gate、校验和恢复。
“离线”指不连接 Neo4j、Chroma、embedding 服务；聊天模型仍按 SAGE 配置调用。
没有 OCR、PDF/DOCX 解析、数据库回写或现行法律有效性核验。
内置清单和规则只是分析线索，不是自动批准依据。缺乏有效法律依据时须补件或人工复核。

## 独立本地启动

先按项目现有方式在 `.env` 配置真实聊天模型。使用 PowerShell 7：

```powershell
.\scripts\start-szyd-local.ps1
```

默认使用 `http://127.0.0.1:8015/`，Runner 在 `8016`，不停止原 `8014` 服务。
启动器生成临时服务令牌，启动独立 Runner，通过正常 Candidate 审批流程发布并激活
Solution。日志和数据默认放在 `$env:LOCALAPPDATA\Sage\szyd-local`。
使用 `-Port`、`-RunnerPort`、`-DataRoot` 可指定其他空闲端口和独立数据目录。
保留终端，Ctrl+C 停止本次启动的服务。不要让多个实例共享同一个本地数据目录。

启动器仅适用于本地开发：匿名用户、管理认证关闭、仅回环监听。
本地 Runner 是进程边界，不是敌对多租户沙箱；生产按现有容器 Runner 部署。
`-DemoModel` 仅检查平台，不能用于真实合规审核。

## 发布到已有环境

已有服务须重启加载本次代码，并配置隔离 Runner。保留原数据目录即可，无需清空状态。
上传 Python Skill 不会因允许本地 subprocess 而自动获得执行权限。
设置 `SAGE_AGENT_RECURSION_LIMIT=200` 为多阶段流程提供图执行预算。
管理令牌通过环境变量 `SAGE_ADMIN_TOKEN` 提供，禁止放入命令行参数。

```powershell
# 上传 Skill、导入 Solution 并创建已校验 Candidate，等待控制台审批激活
.\.venv\Scripts\python.exe scripts/publish_szyd_solution.py --url http://127.0.0.1:8014

# 仅本地开发，显式执行审批和激活
.\.venv\Scripts\python.exe scripts/publish_szyd_solution.py --url http://127.0.0.1:8014 --activate
```

脚本检查环境、Runner、Skill/Agent/Solution 的不可变版本和 SHA-256 锁。
同内容已激活时直接返回；版本内容漂移时停止，需创建新版本并更新锁，不能覆盖历史发布。

## 使用与恢复

1. 刷新页面，选择 **SZYD Compliance Review**。其 `interactive` profile 自动切换到 Agent 模式。
2. 提交事项、金额、企业范围、已履行程序和适用制度。不是切换下拉框时立即执行。
3. 入口 Agent 读取 `law`，依 Gate 分派 `smjy/facts/funds/proc/docs/supp/final`。
4. 材料不足时，一次性提出补件请求。在同一 thread 继续回复即可恢复；原阶段全量失效，重新审核。
5. 新 thread 创建独立 case。已完成 case 不可变。重大事项只能在 Gate 授权并校验最终结果后出正式报告。

附件只支持累计 1 MiB 内的 UTF-8 TXT/JSON/CSV/LOG 内容。其他格式或过大附件会明确标为
未解析，应提交文本版或转人工审核。不能把“已上传”当成“已核实”。

## 状态与隔离

权威审核 state 和转换快照位于 `<data_root>/szyd-cases.sqlite3`，按 workspace、principal、
solution/release、generation、agent、thread 隔离。每次修改带 revision，过期提交被拒绝；
同一 run 的相同转换可幂等重放。默认最多三轮补件，超过后转人工复核。
附件保存 artifact ID，恢复时重新校验 scope；临时 Runner 路径不会写入 state。
业务大对象通过包外临时 JSON 文件传递，调用后删除，不转发服务凭据。

应用重启后，即使没有对话 checkpoint，入口 Agent 仍会从数据库读取状态。
对话历史的重启恢复另外依赖 `SAGE_CHECKPOINT_DATABASE_URL`；本地启动器不提供 PostgreSQL。
发布新 generation 后的新 thread 使用新版本，旧 thread 按平台既有固定 generation 规则处理。

数据库包含敏感业务原文、证据和报告，不加密、不自动清理。应按组织要求配置磁盘权限、
加密卷、备份与保留策略。当前仅支持单副本本地 SQLite；多副本生产需共享状态提供者。
匿名本地模式的用户共享同一个 principal，不提供真实用户间隔离。

## 聚焦验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_szyd_bundle.py tests/test_szyd_state.py tests/test_szyd_adapter.py tests/test_szyd_solution.py -q
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m unittest discover -s skills/szyd-law/bundle/tests -p test_bundle.py -v
.\.venv\Scripts\python.exe -m sage.cli validate-solution solutions/szyd-compliance
```

端到端测试使用脚本化聊天模型和真实 Runner 执行路径，验证发布、首次补件、应用重启、
同线程恢复与最终结果校验。它不证明真实聊天模型语义、法律适用性或报告的专业正确性。