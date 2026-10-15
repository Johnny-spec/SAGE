---
name: funds
description: 'Review funding, assets, equity, investment amounts, ownership and personnel information for 三重一大 state-owned enterprise compliance; identify factual risk points and missing information without issuing the final approval.'
---

# 资金、资产、股权要素审查

输入原文、identification、证据、version；输出 capital stage。读取 [审查要求](./references/review.md)、[契约](../law/references/contracts.md)，通过 [CLI](../law/scripts/cli.py) `prompt --name STEP3_CAPITAL_COMPLIANCE_PROMPT` 读取原指令。

1. 仅分析已命中类别，未涉及明确标注“未涉及”。
2. 分别形成 financial_involvement、asset_involvement、equity_structure 对象，保留金额、用途、主体、资产方式、出资比例与原文依据。
3. 列 risk_points、insufficient_information，不推定缺失事实或违法。区分已确认事实、模糊信息与缺失材料。
4. 如涉及人事事项，记录明确任免事实、主体与授权信息，personnel_notes 标明现有材料及规则不足；不要补造人事政策。
5. 缺失关键数据返回 INSUFFICIENT、needs；风险建议只基于现有事实／缺口并写 recommendations。不得检查会议程序或独立决定 APPROVED。
6. 执行 `CLI validate --kind stage`，name="capital"；交由主 skill 汇总。