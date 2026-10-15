# 检索与安全

Neo4j 输入 entity_names、keywords、concept_ids、categories 数组。概念匹配支持数据库 concept.id 和 name；本地 ontology ID 不一定与数据库 ID 一致，使用有证据的名称作为 keywords 补充。精确实体名／别名匹配不依赖原工程不一致的全文索引；没有匹配时明确记录，不做模糊身份确认。返回 elementId，不用跨数据库／长期保存的内部数值 ID。

Neo4j 法律路径为 LegalConcept -> BELONGS_TO -> LawDomain -> HAS_LAW -> Law -> HAS_ARTICLE -> Article；同时按法律名称、案例关键词、类别查询，并获取链接实体附近规则。缺少标签和数据不自动初始化；由管理员处理。

Chroma 输入 queries 和 normalize 产生的 retrieval_filters。先 strict，再 fallback，最后无过滤语义检索；只有前一层没有结果才降级。过滤字段依赖现有 collection metadata，不能因为匹配不到就伪造 metadata。候选保留全文、来源、距离和软匹配信息；agent 重排并通过 rerank 验证真实候选 ID。

需要已有本地 Chroma SQLite 持久化目录和 collection；不会自动 create_collection 或导入材料。PersistentClient 可能使用已有本地库的锁和内部维护机制，因此本地文件权限由管理员配置，不能保证文件系统字节完全不变。业务脚本不执行 add/update/delete/upsert。embedding 请求会向 Azure 发送查询文本，须符合材料数据处理授权。

成功空结果为 empty=true，不等于没有法律风险。服务故障不能被包装成空结果。agent 把故障写为 legal.status=BLOCKED 并暂停；有效性、适用性无法确认则 uncertainty_notes 加入原因，转补件或人工复核。

reason tool 只按明确 required_relations/prohibited_relations 和 ontology 冲突对产生线索；自由文本 requirements 交给 agent，不能机械当关系 ID。缺少关系可能是未提及，不等于未履行。不得把 ALLOWS 解释为必须发生。

backfill 不属于普通流程。若用户明确要求持久化，先运行无 --apply 的计划，解释写入的 DocumentFact、MENTIONS、LINKED_TO，得到批准后设 LAW_ALLOW_BACKFILL=1 并运行 --apply。只关联已有节点、不生成推断法律事实；使用独立 run_id、预先核对数据库与节点。写入真实计数，不声称创建了已存在关系。