# 分块与事实图

企业图允许 Company/Government/ResearchInstitute/Platform/Person 和 INVESTS_IN/CONTROL/SUBSIDIARY_OF/CO_ESTABLISH/HOLD_SHARE/GOVERNED_BY。依据 graph_schema.json，只提取明示或可唯一确定事实。关系带 from/to，必须引用同图实体。

NER 可抽取 ontology 支持的法律主体、概念、动作、规则和法律名称。未知属性省略，不补默认 false/0；缺失与否定不同。金额用元、比例用 0..1 时必须有可靠换算，并保留原值／单位证据。涉及日期无法确定时保留原文，不编日期。

normalize 输入 extraction.entities/relations；每块 ID 在本块唯一。工具自动添加 chunk_id 前缀并同步关系端点；不要先手动添加或重复调用已经归一化的输出。错误关系端点导致 ERROR，修正抽取而不是跳过。源 ontology 的泛类型匹配优先，不保证识别实际控制人、国有属性等细分身份；agent 应核验事实，不凭映射推断。

enterprise 触发与 post_ner 触发独立记录。CORE_TRIGGER_RULES 的实体和关系条件必须联合匹配；ontology 触发按动作、两端类型、属性及层级策略匹配。NATIONAL 默认启用；PARTY/COMPANY_POLICY 等需显式适用授权。所有规则可能是旧版本或示例，置信度只表示算法线索。

每个 chunk 结果包括 chunk_id、原文位置、extraction、工具结果、legal_facts、laws_involved、uncertainty_notes、evidence。事实可采用 {content,evidence_refs} 对象，法条可采用 {name,article,content,evidence_refs,applicability}。aggregate 保留原始 chunk 记录，不生成新理由或结论。重要冲突不得在去重时丢弃。