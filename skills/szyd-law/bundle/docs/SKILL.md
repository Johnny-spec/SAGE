---
name: docs
description: 'Check compliance material completeness, missing attachments, meeting resolutions, funding and asset documents; use after legal, capital and decision-procedure analysis to consolidate evidence gaps for supplement requests.'
---

# 材料完整性

输入 state.file_list、实际已查看证据、legal/capital/procedures 三个分支结果与 version；输出 materials stage。读取 [核对要求](./references/checklist.md)、[契约](../law/references/contracts.md)。CLI 为 [共享入口](../law/scripts/cli.py)。

1. `CLI prompt --name STEP5_PROCESS_COMPLETENESS_PROMPT` 获取原指令，根据已有规则、分支 needs、资金缺失信息和程序缺口形成 required_documents。
2. 对照实际提供且可核验的材料形成 provided_documents；不能只把 reflecting_documents 或文本中的“附件见”视为已提供。
3. 形成 missing_documents，去重但保留每项所支撑的分析与依据。记录 impact_assessment。
4. 关键材料不足时 INSUFFICIENT，needs 写具体材料／信息；足够时 DONE。不得把文件目录完整当成内容、签署、时效和权限全部有效。
5. `CLI validate --kind stage`，name="materials"；交主 skill 执行 Gate。需用户补件交 supp，不自己循环发问。