---
name: smjy
description: 'Identify 三重一大 and major decisions in state-owned enterprise materials, match decision checklist IDs, classify major decisions, personnel, projects and large funds; use before compliance analysis or when supplement changes scope.'
---

# 三重一大事项识别

读取 [识别说明](./references/identify.md) 和 [统一契约](../law/references/contracts.md)。输入为 state.input_text、附件证据、当前 version；输出写入 identification stage。

1. 读取 [清单](../law/assets/major.json)，必要时执行 `CLI prompt --name STEP1_SMJY_IDENTIFY_PROMPT` 获取原指令。
2. 根据原文匹配类别与具体事项，记录条目编号、事实来源和识别理由。MD-01 等是类别 ID，不是规则条目 ID。
3. 执行 `CLI rules --input rules-input.json` 确认 matched_items 可对应决策规则。
4. 按新契约返回 is_major、matched_categories、matched_items；填 version、status、reasoning、evidence_refs、needs。重大事项不是违法认定。
5. 执行 `CLI validate --kind stage --input stage-check.json`，其中 name="identification"，stage 为结果。主 skill 合并状态并调用 Gate。

信息不足为 INSUFFICIENT，is_major=null，并提出范围问题；解析或模型执行错误为 ERROR。仅有真实依据认定非重大事项时返回 DONE、is_major=false。不得把金额不明当作未达阈值。CLI 指 [共享入口](../law/scripts/cli.py)，完整复制所有兄弟目录。