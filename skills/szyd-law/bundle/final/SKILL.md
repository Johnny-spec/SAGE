---
name: final
description: 'Generate formal Chinese 合法合规性审核意见书 or a NOT_APPLICABLE summary from completed, evidence-grounded upstream compliance results. Use only when the main workflow authorizes FINAL or FINAL_SKIP; never independently reassess compliance.'
---

# 最终审核报告

输入完整 State 和 Gate 结果；输出原系统五字段 final_result。读取 [报告要求](./references/report.md)、[非适用简报](./references/skip.md)、[契约](../law/references/contracts.md)。CLI 为 [共享入口](../law/scripts/cli.py)。

1. 执行 Gate 确认 action 为 FINAL 或 FINAL_SKIP；补件、故障、人工复核状态不进入成功报告路径。
2. FINAL：通过 `CLI prompt --name FINAL_RESULT_PROMPT` 读取原报告指导；用各阶段结果替换 smjy_info/legal_facts/capital_compliance/project_decision/process_completeness。格式采用新契约，不调用 LangChain，也不字面保留模板占位符。
3. 仅汇总已有事实、法律制度、风险、建议与主 skill review。approval_status、final_risk_assessment、key_findings、recommendations 必须与 review 完全一致。不能因为 prompt 假设 Gate 通过，就写无保留意见。
4. FINAL_SKIP：读取 FINAL_RESULT_SKIP_PROMPT，仅说明不适用三重一大审核，不背书事项总体合法。
5. compliance_summary 写完整中文报告／简报，JSON 中换行正确转义。执行 `CLI validate --kind final --input final-check.json`，输入含 state 和 final_result。
6. 校验仅验证结构、标题和上游一致性。人工核对正文有无新事实、法律、建议、扩大结论、占位符与错误引用；通过后交付，并由主 agent 保存 final_result 与快照。