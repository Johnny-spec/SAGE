# SkillsCollection 诊断管理器

根目录下的 `orchestrator` 是 IIS 与 Microsoft Edge 问题的统一入口。它以
AI manager 的角色确定产品范围，调用产品 skills 分析日志，执行多轮交叉
验证，并生成统一、脱敏、可追溯的诊断报告。

## 标准流程

1. 输入问题描述、JSON 上下文或日志目录。
2. manager 根据显式产品、症状、problem type 和日志证据路由到 IIS、Edge
   或两个产品。
3. IIS/Edge product orchestrator 调用各自的日志及场景 skills。
4. 执行产品本地验证、manager 交叉验证、证据引用及根因质量检查。
5. 同时输出中文版和英文版报告，包括：问题描述、问题排查过程、问题原因
   与证据、解决方案与证据、下一步操作和缺失日志、交叉验证的相同点与
   不同点、各模块置信度，以及每个 skill 做了什么的调用链。

```powershell
python orchestrator/scripts/orchestrator.py C:\incident `
  --report C:\incident\manager-report.html
```

命令会生成：

- `C:\incident\manager-report.html`：英文版。
- `C:\incident\manager-report.zh-CN.html`：中文版。

中文版会本地化报告结构和管理器说明。为避免翻译改变诊断含义，日志中的
技术结论、命令、产品标识和没有权威译法的内容会保留原文，并在报告中
明确说明。

对于 Procmon、NetLog 等高噪声日志，系统会结合问题类型、现象描述、目标
进程或请求、时间窗口和健康基线，将观察分为“支持性证据、上下文相关、
背景观察”。背景观察仍保留用于审计，但不会进入候选原因、解决方案或
独立交叉验证。报告中的证据 ID 会同时显示具体结论、来源、时间、记录定位、
关键事实、现象相关性和噪声等级。

无法可靠判断产品时，manager 会要求指定 `--product IIS|Edge|both`，不会
静默猜测。IIS 与 Edge 目录中的旧 orchestrator 保留为兼容 delegate。