# 非重大事项简报

只有 identification.status=DONE 且 is_major=false，并具有可追溯证据时可使用。approval_status=NOT_APPLICABLE、final_risk_assessment=N/A。

正文说明材料范围、事项识别理由与“不属于本次三重一大审核适用范围”的结论，不写“事项已批准”或“完全合法”。key_findings 依据 identification，recommendations 仅用前序已提出且适用的建议，未提出则 []，不自动新增“按一般事项程序办理”等要求。

输出仍为五字段 JSON，compliance_summary 为完整简报。技术错误、信息不足、无法确认阈值不得走此路径。