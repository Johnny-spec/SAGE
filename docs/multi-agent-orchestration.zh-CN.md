# 多 Agent 编排

SAGE 将控制流与模型推理解耦：Temporal Workflow 决定节点何时运行、如何重试和恢复；Agent 只负责节点内部的分析、委派与结构化输出。

## 编排操作

- 串行：后续节点通过 `dependsOn` 等待前序节点。
- 并行：所有依赖已经完成的节点按 `maxConcurrency` 并发运行。
- 条件分支：`when` 从 Workflow 输入或上游输出读取值，支持 `equals`、`not-equals`、`in`、`not-in` 和 `exists`。
- 汇总：汇总节点依赖多个并行节点。`joinPolicy: all` 要求依赖成功；`all-settled` 也接收跳过或失败结果。
- 失败传播：默认 `fail-fast`；`continue-with-errors` 将错误转成带 `_sage.status: failed` 的结构化结果。
- 验证：`outputSchema` 在 Worker 边界校验 Skill 或 Agent 的 `output`，不合格结果不会进入下游。

完整示例见 [examples/multi-agent-workflow.yaml](examples/multi-agent-workflow.yaml)。

## 数据传递

`bindings` 只支持明确的数据源，不执行任意字符串模板：

- `literal`：静态值。
- `workflow-input`：Workflow 请求输入，`path` 使用 JSON Pointer。
- `step-output`：已完成祖先节点的结果。
- `artifact`：受治理的 Artifact ID。

Agent invocation 的标准输出位于 `/output`。当前文本回答位于 `/output/message`，Tool 结果可能位于 `/output/result`。大对象应写入 Artifact，只沿图传递 ID。

## Agent 间调用

Agent 定义通过 `delegations` 引用同一发布快照中的其他 Agent：

```yaml
delegations:
  - alias: reviewer
    target_agent_id: incident-verifier
    description: Independently verify findings against evidence.
    max_calls: 2
```

Runtime 将其暴露为 `delegate_reviewer` Tool。调用经过统一 Agent invoker，并执行以下约束：

1. 目标 Agent 必须已经发布且出现在父 Run 固定的 Generation/Solution Release 中。
2. Child Run 继承父 Run 的 workspace、principal、lifecycle、Generation 和 Release。
3. 每次调用记录 `agent.handoff.started/completed/failed`，并带 Child Run ID。
4. 幂等键、`max_calls` 与 Agent 的 `max_subagent_calls` 限制重复和失控委派。
5. Catalog 拒绝缺失目标、自委派与 delegation 循环。

Workflow Agent 节点和交互式 delegation 使用同一 `/v1/agent-invocations` 语义。外部调用方不能自行指定 Generation 或冒充父 Run 的 principal。

## 验证拓扑

推荐使用独立角色而不是让生产 Agent 自我确认：

```text
Router -> [Analyst A || Analyst B || Analyst C]
       -> Aggregator -> Verifier
       -> PASS: Publish
       -> REVISE/ESCALATE: HumanTask
```

确定性检查应优先使用 JSON Schema、Policy 和 Skill；只有证据语义一致性才交给 Verifier Agent。Verifier 输出应限制为 `PASS`、`REVISE` 或 `ESCALATE`，使 Workflow 能可靠分支。