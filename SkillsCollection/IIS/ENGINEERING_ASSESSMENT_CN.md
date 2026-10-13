# IIS / Edge Skills 工程评估与方法论

本文从 IIS、HTTP.SYS、Windows Event Log、Microsoft Edge/Chromium NetLog
高级工程与高级技术支持视角，定义这套 skills 的质量门槛、证据方法和演进路线。
评分是工程成熟度评估，不代表产品 SLA。

## 1. 结论

当前集合已经具备可复用诊断平台的骨架：统一 envelope、registry 驱动路由、
共享日志 reader、folder-first orchestrator、missing-log gate、结构化 playbook 和
跨 runtime 测试。经过本轮调整，主要的“假成功”和字段语义漂移已被修正。

当前综合成熟度约为 **4.1 / 5**：适合受控生产诊断助手和事件包离线分析；默认
输出已经具备契约隔离、保守根因门、稳定证据 ID、脱敏和关键 acquisition failure
语义。在给出 `ROOT_CAUSE_FOUND` 或自动化生产变更前，仍需补齐 request/PID/app-pool
精确关联、历史基线、全 reader 流式处理和剩余 producer 的统一质量元数据。

## 2. 成熟度评分

| 维度 | 当前评分 | 评价 |
|---|---:|---|
| 契约与可组合性 | 4.8/5 | v2.3 兼容/v2.4 严格边界、Python/PowerShell 脱敏与 provenance、全 child failure envelope 已强制 |
| Registry 与路由 | 4.2/5 | runtime policy、入口文件与 playbook 引用可机器校验 |
| IIS W3C 诊断 | 4.0/5 | 动态字段、status/substatus/win32、来源行号与 UTC 基础已保留 |
| HTTPERR 诊断 | 4.0/5 | 按 `#Fields` 映射 reason/queue/site，避免列位误判 |
| EVTX / crash 关联 | 3.6/5 | acquisition failure/权限/成功空结果已分离，离线 EVTX 可用；精确 join 仍未完成 |
| Edge NetLog | 4.0/5 | 有字节/事件预算、timeTickOffset、BEGIN/END 完整性和 source dependency 闭合度；语义链仍需扩展 |
| 大文件与性能 | 3.1/5 | NetLog 已有硬预算；IIS/FTP/HTTPERR 聚合仍未全面流式化 |
| 安全与隐私 | 4.4/5 | Python/PowerShell JSON、validator pack 与 IIS HTML 统一默认脱敏；仍需审计所有 stderr/旧报告路径 |
| 测试与回归 | 4.8/5 | 根级 CI 强制三产品测试、schema/registry closure、PowerShell AST、隐私与对抗反例 |

## 3. 本轮已落地的高影响调整

1. **Runtime 不再固定为 Python**
   - registry 使用 `runtime_order` / `runtime_status`。
   - Python orchestrator 会选择完整 PowerShell 实现，不再把 Python framework
     stub 的 `ok=true` 当作完成诊断。
   - `raw.runtimes` 和子结果 `raw.execution` 记录实际 runtime 与退出码。

2. **失败语义可组合**
   - 子进程即使非零退出，只要 stdout 有标准 envelope，父编排器优先保留它。
   - 总超时返回标准失败 envelope。
   - secondary fan-out 失败会影响 folder 顶层 `ok` 和 confidence。

3. **IIS / HTTPERR 字段语义对齐**
   - IIS PowerShell 保留 `sc-status`、`sc-substatus`、`sc-win32-status`、site、host、
     username、source file、line number。
   - 输出 `500.19/5` 一类 status signature 和可追溯样本。
   - HTTPERR PowerShell 按 `#Fields` 读取 `s-reason`、`s-queuename`、`s-siteid`，
     不再把 app-pool/queue 名误判为 reason。

  4. **离线事件包与 live host 明确分离**
    - folder mode 默认只运行事件包中已有证据支持的 skill。
    - live Security/Event/Perf/firewall 查询必须显式使用 `--allow-live-host`。
    - 被跳过的 follow-up 写入 `raw.skipped_followups`，缺失证据由 missing-log gate 报告。

5. **Registry 引用闭包**
   - 新增 `registry.schema.json` 和 stdlib-only `registry_validation.py`。
   - 校验入口文件、runtime、problem type、follow-up 和 playbook skill 引用。
   - Common `validator` 与 `source_logging_discovery` 通过 external registry/override
     供 IIS、Edge 共用；override 目标和入口文件均参与闭包检查。

6. **Edge NetLog 质量门**
   - 事件 tick 支持小数。
   - 按 Chromium 定义使用
     `unix_ms = event_time_ms + timeTickOffset_ms`，并明确该换算为近似值。
   - 只有存在 offset 才应用 wall-clock `time_range`；否则保留事件并将相关性标为
     `unavailable`。
   - URL 默认移除 userinfo、query、fragment。
   - AUTH BEGIN/END 使用 FIFO，输出 unmatched frame 统计。
   - 单独出现 `ERR_MISSING_AUTH_CREDENTIALS` 不再自动等价于 handshake loop。

7. **调查控制面（v2.3）**
  - `evidence_state` 将“运行成功”和“证据是否足够”分开。
  - `collection_plan` 记录只读命令、权限、敏感度、输出、验证和停止条件。
  - validator 在 evidence skills 完成后运行，要求共同 evidence id 和独立证据源。
  - source discovery 只扫描显式 roots，不执行/导入目标代码，不自动联网。

## 4. 方法论：从日志到可执行结论

每个 skill 应遵循同一条九阶段流水线。

### 4.1 Case Definition

先定义问题，而不是先搜错误字符串：

- 用户影响、开始/结束时间、站点/主机、URL/操作、预期与实际结果。
- 环境边界：客户端 Edge、代理/WAF、HTTP.SYS、IIS site/app-pool、应用、下游。
- 明确分析是 **live snapshot** 还是 **historical incident**。

### 4.2 Acquisition Manifest

每次事件包应生成采集清单：来源主机、原始路径、采集命令、采集时间、时区、
文件大小、哈希、权限失败和缺失日志。没有采集覆盖，不能把“未看到”写成“没有”。

### 4.3 Integrity Gate

分析前验证：

- 文件可解析、header/schema 存在、记录数合理。
- 捕获是否截断、仍在写入、跨轮转文件是否齐全。
- unknown event/source 数、BEGIN/END 未配对数、过滤/截断数量。
- 权限不足、平台不支持、查询失败必须区别于 `no_events`。

### 4.4 Normalization

内部统一字段，但保留原始值：

```text
source_kind, source_file, record_locator,
original_time, normalized_time_utc, time_quality,
site, app_pool, process_id, host, method, uri,
status, substatus, win32_status, reason, net_error
```

时间规则：

- IIS W3C 与 HTTPERR 按 UTC 解释。
- EVTX 保留 `TimeCreated` 的 offset，并归一 UTC。
- NetLog 只有在 `timeTickOffset` 存在时才具有近似 wall-clock 时间。
- 任何推测出来的时区必须写为 `inferred`，不能写为 `exact`。

### 4.5 Reduction

先做 provider-side / streaming 过滤，再聚合：

- 时间窗、site/app-pool、host、URI、status signature。
- 始终输出 `records_seen / matched / excluded / truncated`。
- Top-K 只影响展示，不应改变总量统计。

### 4.6 Correlation

相关不等于因果。统一使用三级质量：

| 质量 | 条件 | 允许的表述 |
|---|---|---|
| `exact` | 同 request/connection/PID/source dependency，且字段一致 | “该事件直接对应此请求” |
| `time_only` | 同一可靠 UTC 窗口，组件/站点部分匹配 | “时间上相关，需进一步验证” |
| `unavailable` | 时钟不可比或缺少 join key | “不能进行跨日志关联” |

推荐 join key：

- IIS：UTC + site + host + method + URI + client IP。
- HTTPERR：UTC + site id + server endpoint + queue/app-pool + reason。
- EVTX：UTC + provider/event id + PID + app-pool/site（若事件含有）。
- NetLog：校准 UTC + host/path/method + source dependency graph。

### 4.7 Hypothesis Testing

每个 finding 至少包含：

- 结论和证据样本。
- 替代假设。
- 能证伪结论的下一条廉价检查。
- 反证或缺失数据。

示例：`Timer_AppPool` 只能证明 HTTP.SYS 队列超时；只有匹配到同 queue/site、同
窗口的 app-pool recycle/crash/资源证据后，才能把应用池状态写为根因。

### 4.8 Confidence

- `high`：直接 join，且至少一个独立来源支持，无关键质量门失败。
- `medium`：可靠时间窗 + 同组件证据，但没有 request/PID 级 join。
- `low`：单一来源、样本不足、时间不可比、权限失败、捕获不完整或仅靠缺失证据。

置信度不是 finding 严重度。严重故障也可能只有低置信度根因。

### 4.9 Remediation and Verification

默认只读。任何阻断、回收、配置修改必须包含：

- 影响范围与前置确认。
- 变更命令，但不自动执行。
- TTL/回滚命令。
- 验证指标和观察窗口。

IP 高频、4625 或单一 NAT 出口本身不足以证明攻击；防火墙规则必须先检查代理、
负载均衡、监控、办公出口和白名单。

## 5. Skill 设计规范

每个新 skill 应包含：

1. `SKILL.MD`：触发条件、runtime 状态、权限、输入、输出、限制。
2. registry：入口、`runtime_order`、每个 runtime 的完成状态、problem type。
3. shared reader：负责发现、解析、过滤、时间、截断；skill 只负责领域解释。
4. 标准 envelope：`ok=false` 表示诊断未完成；环境不可用不能伪装成“无问题”。
5. `raw.quality`：采集完整性、时区、记录计数、截断、schema 兼容性。
6. 证据定位：文件 + 行号、EVTX record id、NetLog source/event id。
7. 安全限制：只读默认、敏感数据去除、变更需审批。
8. fixture：正常、边界、乱序、缺字段、权限失败、超大输入和反例。

## 6. 阈值设计

固定阈值只能作为候选异常，不应直接成为根因。建议同时满足：

- 最小样本量，例如请求数 `n >= 20`。
- 绝对数量和比例双门槛。
- 按 site、app-pool、URI、客户端类型分层。
- 与同小时/同星期历史基线比较。
- 输出分母、观察时长和置信区间。

单 IP 占比需考虑 NAT、反向代理、健康检查；5xx 比例需排除部署探针和单请求小样本。

## 7. 大文件与生产安全

下一阶段应把 query 路径改为真正流式：

- iterator 上先过滤，在线维护 Counter、percentile sketch 和 top-K。
- 只保留受控证据样本，不保留全部 entries。
- 设置文件大小/事件数预算。
- 输出读取字节、处理记录、跳过记录、截断状态。
- 允许取消与 deadline，并确保超时仍输出 envelope。

## 8. Edge source graph 路线

当前 NetLog 已能做基础 URL_REQUEST、auth、DNS/proxy/TLS 错误分类，但高级定位应
从“按 source 类型计数”演进为依赖图：

```text
URL_REQUEST
  -> AUTH_CONTROLLER
  -> HTTP_STREAM_REQUEST / HTTP_STREAM_JOB
  -> HOST_RESOLVER_MANAGER_JOB
  -> PROXY_RESOLUTION_SERVICE
  -> CONNECT_JOB / SOCKET
  -> SSL_CONNECT / TLS_STREAM_ATTEMPT
```

图中每个节点需保留 source id、父子 dependency、BEGIN/END、目标 host/port、
net_error、OS error 和耗时。只有图链闭合时，才能准确区分 DNS、PAC、代理 CONNECT、
TCP、TLS、HTTP 或 IIS 应用层失败。

## 9. 尚未完成的优先路线

### P0

1. Event Log 按真实 IIS error anchor、PID、site/app-pool 关联，而不是仅按窗口起点。
2. 在已实现的 NetLog source dependency 基础上扩展 HTTP_STREAM_JOB、CONNECT_JOB、
  SSL_CONNECT、DNS 与 proxy 的语义闭环和版本 capability matrix。

### P1

1. historical perfmon/ETW 输入；`resource_monitor` 当前只代表“现在约十秒”。
2. IIS/FTP/HTTPERR 大文件流式聚合；NetLog 当前有硬字节/事件预算但仍是有界全量 JSON 解析。
3. 将 IIS 已实现的最小样本门推广到 FTP/HTTPERR，并增加历史基线与分层阈值。
4. PowerShell parser 继续下沉共享层，减少 Python/PowerShell 重复实现。
5. 输出 acquisition manifest 与文件哈希。

### P2

1. 为 provider adapters 增加组织级认证、成本预算和审计，不把凭据写入 incident input。
2. 对 Edge 版本/schema 做 capability matrix，而不是假设事件字段永远兼容。
3. 引入受控 `include_sensitive` 模式；默认继续只向模型发送脱敏 evidence pack。

## 10. 质量门与命令

```powershell
# 跨产品 schema / registry / playbook 门
python ..\Common\quality_gate.py

# Registry / playbook 引用闭包
python _shared/registry_validation.py

# 全量回归
python -m pytest _shared/tests -q

# PowerShell 语法树检查（无需额外模块）
Get-ChildItem -Recurse -Filter *.ps1 | ForEach-Object {
  $tokens = $null; $errors = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile(
    $_.FullName, [ref]$tokens, [ref]$errors)
  $errors
}
```

发布门槛：registry validator 为 `ok=true`、测试全绿、无未解释 schema/parse error、
无明文 secret fixture、文档中的每个 executable skill 都能解析到真实入口。

## 11. 参考

- Chromium NetLog event/source 文档：
  https://chromium.googlesource.com/chromium/src/+/HEAD/net/log/net_log_event_type_list.h
- Chromium `timeTickOffset` 实现与近似换算说明：
  https://chromium.googlesource.com/chromium/src/+/HEAD/net/log/net_log_util.cc
- Chromium NetLog 隐私与 capture mode 原则：
  https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/net-log.md
- IIS HTTP status/substatus 诊断：
  https://learn.microsoft.com/troubleshoot/developer/webapps/iis/www-administration-management/http-status-code