---
name: supp
description: 'Prepare consolidated 补件清单 and clarification requests for incomplete compliance reviews; pause for user material and resume versioned skills workflows without reusing stale approval or repeating unchanged requests.'
---

# 补件与恢复

只在 Gate action=SUPPLEMENT 时使用。输入 State 与 Gate.needs；输出中文补件请求及宿主保存的等待状态，不输出 final_result。读取 [恢复规则](./references/resume.md)、[契约](../law/references/contracts.md)。CLI 为 [共享入口](../law/scripts/cli.py)。

1. `CLI prompt --name SUPPLEMENT_REQUEST_PROMPT` 获取原指令，合并重复问题，逐项说明所需材料／事实及对应审核影响。
2. 不增加新审核范围，不把缺证据措辞变成已违法。把技术服务问题交主 skill，不要求用户补无关业务材料。
3. 保存 state 快照、run_id、version 与补件清单，向用户提问后停止等待。不得立即在同一输入上重复调用 skills。
4. 用户补充后，读取 addition 与旧 state，执行 `CLI resume --input resume-input.json --output resume-output.json`。
5. 保存输出中的新 state，按 next 从 smjy 重新审查。没有新信息或证据 ID 冲突时不要绕过工具，说明仍待补充／修订需新 ID。
6. 达到补件上限或确证不合规并伴随未解决缺口时主 skill 转人工复核。