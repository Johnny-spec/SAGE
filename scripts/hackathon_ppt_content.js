"use strict";

const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const OUTPUT = path.join(ROOT, "docs", "SAGE-hackathon-demo.zh-CN.pptx");

const ASSETS = {
  mcp: path.join(ROOT, "docs", "images", "quick-flow", "mcp-published.png"),
  workflow: path.join(
    ROOT,
    "docs",
    "images",
    "solution-walkthrough",
    "04-workflow.png",
  ),
  completed: path.join(ROOT, "docs", "images", "quick-flow", "completed-1440.png"),
  completedMobile: path.join(ROOT, "docs", "images", "quick-flow", "completed-390.png"),
  release: path.join(ROOT, "docs", "images", "publication", "release-1440.png"),
};

const STORYBOARD = [
  {
    time: "0:00-0:07",
    seconds: 7,
    title: "SAGE",
    subtitle: "让团队 Skills 成为可治理的 Agent API",
    caption: "把个人 Skill，升级为团队可复用、可治理的 AI 能力。",
    narration: "SAGE 把个人 Prompt、脚本和工具，变成团队可复用、可治理的 AI 能力。",
    action: "标题页停留，依次强调 Skill、治理门禁和团队 API。",
  },
  {
    time: "0:07-0:18",
    seconds: 11,
    title: "AI 能力很多，却停在个人电脑里",
    caption: "难点不是调用 Tool，而是安全交付与持续升级。",
    narration: "团队并不缺 AI 能力。问题是它们分散在个人环境，调用边界写在说明里，升级还可能影响正在运行的任务。",
    action: "按从左到右的顺序讲环境、政策和升级风险。",
  },
  {
    time: "0:18-0:34",
    seconds: 16,
    title: "一条可信交付链，两种执行路径",
    caption: "验证、发布、版本固定与执行策略，被连接成可信闭环。",
    narration: "SAGE 先验证和评测 Skill，再形成 Candidate，激活为不可变 Generation。已知任务零 LLM 直达；复杂目标由 Agent 按需加载 Skill。请求固定版本并经过统一策略边界。",
    action: "沿主流程从左向右讲，再对比 Direct 与 Agent 两条执行路径。",
  },
  {
    time: "0:34-0:48",
    seconds: 14,
    title: "1. 接入并治理 MCP 能力",
    caption: "MCP Tool 接入时即绑定版本、风险等级与内容摘要。",
    narration: "管理员连接 MCP Server，发现 calculate_total，并把它注册为只读、带版本和摘要的原生 Tool。治理从能力接入时就开始。",
    action: "依次指向 calculate_total、read-only、版本和 active 状态。",
  },
  {
    time: "0:48-1:06",
    seconds: 18,
    title: "2. 设计工作流，一键发布运行",
    caption: "一键完成依赖解析、构建、激活与持久运行。",
    narration: "设计器负责可视化配置；右侧是已验收 Quick Flow 的执行链。点击 Quick publish and run，平台自动解析依赖、构建、校验、激活并启动运行。",
    action: "先看设计器，再沿右侧已验收运行链讲解，最后强调 Quick publish and run。",
  },
  {
    time: "1:06-1:24",
    seconds: 18,
    title: "3. 查看结构化运行结果",
    caption: "输出 32.4；Run、Release 与 Generation 均可追踪。",
    narration: "Temporal 持久执行工作流。单价十二、数量三、折扣百分之十，JSON 输出 total 三十二点四。界面同时保留 Run、Release 和 Generation。",
    action: "先展示 completed，再指向真实 JSON 放大框，最后读出结果和版本信息。",
  },
  {
    time: "1:24-1:39",
    seconds: 15,
    title: "新版本通过门禁才会生效",
    caption: "通过则原子激活；失败则保留当前 Generation。",
    narration: "发布不是覆盖文件。新版本通过验证、评测和审批后才原子激活；任一步失败，Active Generation 保持不变。左侧 Release history 记录了版本切换。",
    action: "对比 1.1.0 active 与 1.0.0 inactive，再讲通过和失败两条分支。",
  },
  {
    time: "1:39-1:46",
    seconds: 7,
    title: "让个人 Skill 成为组织能力",
    caption: "一次开发，全团队复用；发布有门禁，失败保留当前版本。",
    narration: "SAGE 不是另一个 Tool Gateway，而是 Agent Skills 的治理与交付层。",
    action: "停留在三项证据和 SAGE 标识，完成收束。",
  },
];

module.exports = { ASSETS, OUTPUT, STORYBOARD };