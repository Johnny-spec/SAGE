# 流程、Gate 与循环

Skill 是指令，agent 是调度器，CLI 是 tools。读取 SKILL.md 不会自动运行 Python，也不会自动创建子 agent。宿主负责用户会话与状态持久化，不能仅依靠模型记忆保存审核结果。

## 主路径

`init -> smjy -> gate -> facts / funds / proc -> docs -> gate -> 主 skill review -> gate -> final -> validate`

非重大事项走 final skip；未确定范围走补件；任何技术错误停止当前工作。各阶段以独立文件返回，主 agent 顺序合入状态。分支完成后再处理缺口，避免多个分支分别向用户发问。

| Gate | 核对内容 | 后续 |
|---|---|---|
| G1 | 分类有效、证据可追溯、事项编号有规则 | 非重大事项简报／启动分支／补件 |
| G2 | 法律事实、引用来源、有效性及不确定性 | 继续／补件／人工复核／服务阻断 |
| G3 | 资金资产股权数据、风险及缺失信息 | 继续／补件；风险不自动等于拒绝 |
| G4 | 依据、会议层级、顺序及完成证据 | 信息缺失补件；确证不合规进入否定意见或人工复核 |
| G5 | 必要材料与实际可查看材料的差集 | 继续／集中补件 |

CLI gate 实现统一检查，不执行推理；各 stage.status、needs、evidence_refs 承载 agent 的阶段判断。schemas.py 校验类型、版本、引用及部分矛盾；不会证明法律正确性。引用不存在、未知编号或错误格式会返回 ERROR，agent 修正自己的输出，而不是要求用户补件。

## Gate action 调度表

| action | agent 行为 |
|---|---|
| RUN_SKILLS | 读取 skills 列表中的子 skill，完成对应阶段 |
| FINAL_SKIP | 生成 NOT_APPLICABLE 简报，不启动重大事项分支 |
| SUPPLEMENT | 读取 supp、汇总 needs、保存快照、等待用户 |
| REVIEW | 主 skill 形成有证据的审核结论；不足以形成结论则人工复核 |
| FINAL | 读取 final，仅汇总已记录 review |
| MANUAL_REVIEW | 输出已知问题与复核要求，不生成批准结论 |
| BLOCKED / ERROR | 报告配置、服务或输出格式问题，保留状态并暂停 |

## 补件循环

1. Gate 返回 SUPPLEMENT，agent 汇总成一份补件请求，不立即循环跑模型。
2. 持久化原 state、补件清单与会话标识。等待用户提供材料；没有新材料不重跑。
3. 准备 `{state, addition}`；addition 可包含补充原文 input_text、附件 file_list、带新 ID 的 evidence。
4. 执行 `resume --input resume-input.json --output resume-output.json`，读取返回的 state，保存为新版本。旧证据不可改写；更正内容以新 ID、明确说明修订关系提供。
5. 当前实现采取保守全量失效：清空五个阶段、review 和 final_result，重新从 smjy 开始。这样范围变化不会保留陈旧结论。原快照不被修改。
6. 完成新一轮分析、Gate 和审核。默认三轮补件，超限转人工复核。对确证不合规且仍有缺口的事项直接人工复核，不反复补件企图“通过”。

系统故障不是材料缺失。修复技术问题后可重新运行故障阶段，但必须保留当前 version；若业务材料改变则按新版本全量重审。并行调度和运行快照存储由宿主实现，本包没有后台循环或数据库会话管理服务。