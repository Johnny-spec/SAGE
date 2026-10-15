# JSON 契约与脚本接口

入口：`python <skills>/law/scripts/cli.py <operation> --input <JSON> --output <JSON>`。
输入输出 UTF-8；输出目录必须已存在。--output 可省略，此时读取 stdout JSON。退出码 0 为执行成功，2 为格式错误或技术阻断。Gate 返回 SUPPLEMENT / MANUAL_REVIEW 是正常决策，不代表执行失败。宿主必须读取 action/status，不能仅凭退出码批准。

脚本不自动加载 .env；宿主把环境变量注入进程。`schema` 返回 Pydantic JSON Schema，不包含模型调用。

## State

必需：run_id:string，input_text:string。默认：version=1、file_list=[]、evidence=[]、stages={}、supplement_round=0、max_supplement_rounds=3、review=null、final_result=null。禁止未知顶层字段。

每条 evidence：id、source、content、locator 均为非空 string；kind 为 document/law/rule/case/graph；verified 默认 false。正文或检索服务不能自行声称 verified=true。agent 确认原文匹配、来源、适用范围和时点后才标记。检索证据不能仅凭相似度、规则命中或数据库有记录而验证。

每个 stage：version:int、status、data:object、reasoning:string、evidence_refs:list[string]、needs:list[string]、recommendations:list[string]。默认 needs/recommendations=[]。status 为 DONE/INSUFFICIENT/NON_COMPLIANT/BLOCKED/ERROR；DONE 指分析完成，不代表合规。INSUFFICIENT 必须列 needs。所有引用必须存在于 state.evidence，版本须一致。

| stages 键 | data 必需字段 |
|---|---|
| identification | is_major:boolean/null；matched_categories、matched_items 为 string 数组；重大事项 DONE 必须有清单 ID |
| legal | legal_facts、laws_involved、uncertainty_notes 为数组；建议每个事实／依据使用含 content 和 evidence_refs 的对象 |
| capital | financial_involvement、asset_involvement、equity_structure 为对象；risk_points、insufficient_information 为数组 |
| procedures | required_procedures、detected_procedures、missing_procedures、reflecting_documents 为数组；compliance_status 为 COMPLIANT/NON_COMPLIANT/INSUFFICIENT_INFO |
| materials | required_documents、provided_documents、missing_documents 为数组 |

data 允许额外业务字段，例如 chunk_results、legal_reasoning、impact_assessment、personnel_notes、retrieval_meta；不要在 data 隐藏与 envelope 状态相矛盾的结论。法律严重不确定性不能仅放在附加字段而遗漏 uncertainty_notes。

review 由主 agent 形成：version、approval_status、final_risk_assessment、reasoning、evidence_refs、key_findings、recommendations。批准仅在证据充分、已确认适用、没有确定违规和未解决关键疑点时可使用。CONDITIONAL_APPROVAL 必须有来自前序的明确条件；它不是绕过关键缺件的通道。不能合理定性时交付 MANUAL_REVIEW，不填猜测结论。

final_result 固定五字段：approval_status 为 APPROVED/REJECTED/CONDITIONAL_APPROVAL/NOT_APPLICABLE；final_risk_assessment 为 LOW/MEDIUM/HIGH/N/A；key_findings 和 recommendations 为 string 数组；compliance_summary 为完整中文报告。重大事项前四个结构化值必须与 review 一致；报告文本仍须语义复核。

## Tools

| operation | 输入 | 输出 |
|---|---|---|
| init | 初始 State | 完整默认 State |
| schema | 无 | state/stage/final_result JSON Schema |
| prompt --name NAME | 无 | 指定原始 prompt，仅作指导 |
| rules | `{item_ids:["33"]}` | 对应决策规则；未知 ID 失败 |
| trigger | `{entities:[],relations:[]}` 企业事实图 | enterprise 规则族命中与概念 |
| normalize | `{chunk_id,extraction:{entities,relations},matched_categories?,rule_level_policy?}` | 带 chunk 前缀的实体／关系、类型、动作、后置触发、映射与 filters |
| retrieve --backend neo4j | entity_names、keywords、concept_ids、categories 字符串数组 | 关联实体、法律条款、案例、规则、邻居及未验证 evidence |
| retrieve --backend chroma | queries 数组、retrieval_filters 对象 | 最多 20 个完整候选、来源、软验证及 evidence |
| rerank | `{candidates:[候选对象],selected_ids:[候选id]}` | 保持全文的排序结果；虚构或重复 ID 失败 |
| reason | `{canonical_relations:[],rules:[]}` | 可能冲突／缺失要求，不写数据库、不形成法律结论 |
| aggregate | `{chunks:[{chunk_id,legal_facts,laws_involved,uncertainty_notes,evidence,...}]}` | 保留各块、去重事实与证据；冲突证据 ID 失败 |
| validate --kind stage | `{name:"legal",stage:{...}}` | valid 或 ERROR |
| validate --kind state | State | valid 或 ERROR |
| validate --kind final | `{state,final_result}` | 结构／前序一致性校验；semantic_review_required=true |
| validate --kind bundle | 无 | 目录、frontmatter、链接、资产哈希检查 |
| gate | State | action、skills／needs／reason |
| resume | `{state,addition:{input_text?,file_list?,evidence?}}` | 新 state、invalidated、next；无新证据不重跑 |
| preflight --offline | 无 | 包校验，不连接服务 |
| preflight --services | 无 | 包校验、只读服务结构与 embedding 维度检查 |
| backfill [--apply] | `{run_id,concept_ids:[],linked_entity_ids:[]}` | 默认 dry_run；显式授权后写 DocumentFact 与关联关系 |

normalize 的 entity 必须有唯一 id、name、type；relation 有 from/to/type 和 attributes 或 properties。未知属性省略。金额阈值用元、比例用 0..1，保留原始单位／文本作为证据；不能把 50% 当成数值 50。无法可靠转换时不填写数值，列 needs。不能将已带前缀的结果重复 normalize。

rule_level_policy 默认只启用 NATIONAL。其他层级须明确 `{"allow_non_national":true,"allowed_levels":["PARTY","COMPANY_POLICY"]}`，且用户提供适用依据。公司决策清单单独使用 rules，与此 policy 不是同一个规则族。

服务出错为 BLOCKED/ERROR，不是空查询成功。成功但无结果为 status=OK、empty=true；agent 仍需评估是否有足够依据。Chroma embedding 必须与原向量库同模型、同维度；维度相同不保证模型匹配。软验证仅表示相关性。

## 旧字段映射

| 旧字段 | 新字段 |
|---|---|
| isMajorDecision / matchedCategories / matchedItems | identification.data.is_major / matched_categories / matched_items |
| step1_reasoning | identification.reasoning；不再被法律分析覆盖 |
| legalFacts / lawsInvolved / uncertaintyNotes | legal.data.legal_facts / laws_involved / uncertainty_notes |
| financialInvolvement / assetInvolvement / equityStructure | capital.data 对应 snake_case 字段 |
| complianceRiskPoints / insufficientInformation | capital.data.risk_points / insufficient_information |
| requiredprocedures / processIdentified / processMissing | procedures.data.required_procedures / detected_procedures / missing_procedures |
| complianceStatus / step_4reasoning | procedures.data.compliance_status / procedures.reasoning |
| existingDocumentsMatched / missingDocumentsRequired | materials.data.provided_documents / missing_documents |

这是迁移对照，不是自动兼容适配器。agent 按新契约产出 JSON，不直接把旧 LLM JSON 原样合入 state。