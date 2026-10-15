# 等待与版本化恢复

宿主必须保存 state，不能期待 SKILL.md 或 CLI 自己监听用户输入。每次新材料用新版本，全量清空旧 stage、review、final_result；这是一种保守依赖失效策略，不是只更新缺件字段。

resume-input 为 {state,addition}，addition.input_text 是新增文本，不是要覆写的全文；file_list 是新提供附件名称；evidence 是新记录。旧证据不能用同 ID 改内容、来源或 verified 标记，如需修订使用新 ID 并在文本说明替代关系。主 agent 保存旧版本以供审计。

补件轮数只在接受新信息时增加。无新信息的请求失败而非增加轮数。默认最多三轮，max_supplement_rounds 可在初始输入配置，不能临时无限提高以绕过人工复核。