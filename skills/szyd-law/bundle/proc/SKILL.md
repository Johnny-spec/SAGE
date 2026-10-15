---
name: proc
description: 'Review state-owned enterprise decision procedures, 党委会前置研究, 董事会决议, meeting order, authorization and 三重一大 checklist rules; distinguish missing evidence from proven procedural noncompliance.'
---

# 决策程序审查

输入 identification、原文、会议及授权证据、version；输出 procedures stage。读取 [程序要求](./references/review.md)、[契约](../law/references/contracts.md)。CLI 为 [共享入口](../law/scripts/cli.py)。

1. `CLI rules` 按 matched_items 获取真实会议决策规则，核对企业适用范围。`CLI prompt --name STEP4_PROJECT_DECISION_PROMPT` 获取原指导。
2. 按规则形成 required_procedures，包含会议类型、角色与先后顺序；列出 detected_procedures 和证据引用，不仅记录会议名称。
3. 核对前置研究、最终决策层级、日期／顺序、授权范围与有效材料。缺证明列 missing_procedures，不推定已完成。
4. reflecting_documents 记录文中提及的相关材料，不能当作实际提供。
5. 未知是否履行为 INSUFFICIENT_INFO，stage.status=INSUFFICIENT、needs 列要求；只有明确事实与适用依据证明违反程序才 NON_COMPLIANT。已有完整证明则 COMPLIANT，stage.status=DONE。
6. 提出来自已识别风险的 recommendations；执行 `CLI validate --kind stage`，name="procedures"；交给 docs 和主 skill。