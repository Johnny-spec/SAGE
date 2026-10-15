# 原 LangGraph 节点迁移对照

| 原节点／能力 | 新归属 | 变化 |
|---|---|---|
| smjy_identify_agent | smjy | 异常不再视为非重大事项；90 项有效规则编号 |
| text_splitter_agent | facts 分块 | 保留事实关联与来源位置，由宿主推理 |
| enterprise_graph_extraction_agent | facts 企业图 | 不生成法律判断 |
| trigger_scanner_agent / TriggerEngine | trigger tool | 不再自动写本地共享 JSON；实体与关系条件联合匹配 |
| chunk_pipeline_agent | facts 内部步骤 | 不再内部调用聊天模型；主 agent 按顺序使用 tools |
| entity_extraction_agent | facts 抽取 | 输出结构化实体、关系、原文证据 |
| entity_typing_agent | normalize tool | 保留原 ontology 类型优先、别名匹配规则；泛化映射不是事实认定 |
| action_normalization_agent | normalize tool | 同时产出 canonical_relations |
| rule_triggering_agent | normalize tool | 保留后置规则族及层级策略；缺失属性不当作 0 |
| entity_linking_agent | retrieve neo4j | 使用精确名称／别名和 elementId；默认不依赖有冲突的全文索引 |
| concept_mapping_agent | normalize tool | 类型／别名匹配；不能推定国有属性 |
| graph_reasoning_backfill_agent | retrieve + reason + backfill | 查询、线索判断、回写分离；不创建推断的 Concept 或 CANON_REL 法律事实 |
| retrieval_filter_builder_agent | normalize tool | 保留 law_name/regulation_name、article/clause、rule_level、concepts/tags、source_type 过滤 |
| chunk neo4j_query_agent | retrieve neo4j | 固定参数查询、双图条款链与关联规则 |
| chroma_query_agent | retrieve chroma + rerank | agent 扩展查询和重排；只打开已有 collection；保留全文 |
| legal_fact_agent | facts 法律分析 | 法律理由与事项识别理由分离 |
| capital_compliance_agent | funds | 只分析要素、风险、缺口，主 skill 负责综合结论 |
| project_decision_agent | proc | 修正输出字段；不以未提及直接推定程序违法 |
| process_completeness_agent | docs | 区分文件提及、提供与查验，缺件实际阻断审核 |
| supplement_request_agent | supp + resume | 原未接入；新增等待用户／版本化重审闭环 |
| gate_step1..5 | gate tool + law | 原未接入且有无条件返回；新增统一 action 合约 |
| final_result_agent | final | 保留五字段与中文报告；不假定所有 Gate 已通过 |
| 顶层 neo4j_query_agent | 不单独迁移 | 不是现行主图可达路径，以实际 chunk 检索能力为准 |
| relation_normalization_agent | 未单独迁移 | 原 pipeline 未调用；使用实际 action_normalization 的产物 |
| scripts/neo4j_dual_graph_init.py | 仅安装注意事项 | 原仅 Cypher 常量，索引与 Entity 标签存在不一致；不自动执行 |
| main.py 补件交互循环 | 宿主会话调度 | 原注释代码，不当成已具备功能 |

原来源的规则与法条可能存在版本过时或示例性质，包括核心规则 R004 文案与严格 > 阈值不一致。脚本保留数值条件 >50000000，不把触发置信度当法律效力。知识库初始化、法规入库、OCR 和宿主适配器不在此包范围内。