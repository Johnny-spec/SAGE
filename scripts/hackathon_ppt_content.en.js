"use strict";

const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const OUTPUT = path.join(ROOT, "docs", "SAGE-hackathon-demo.en-US.pptx");

const STORYBOARD = [
  {
    time: "0:00-0:07",
    seconds: 7,
    title: "SAGE",
    subtitle: "Team Skills, Governed as Agent APIs",
    caption: "Turn personal Skills into governed AI capabilities the whole team can reuse.",
    narration: "SAGE turns personal prompts, scripts, and tools into governed AI capabilities that the whole team can reuse.",
    action: "Hold on the title, then emphasize Skill, governance gate, and team API.",
  },
  {
    time: "0:07-0:18",
    seconds: 11,
    title: "AI Capabilities Are Trapped on Personal Machines",
    caption: "The challenge is not calling a Tool. It is safe delivery and continuous change.",
    narration: "Teams already have AI capabilities. But they are trapped in personal environments, execution boundaries live in documents, and upgrades can disrupt active work.",
    action: "Move left to right through environment, policy, and upgrade risk.",
  },
  {
    time: "0:18-0:34",
    seconds: 16,
    title: "One Trusted Delivery Chain, Two Execution Paths",
    caption: "Validation, release, version pinning, and policy form one trusted loop.",
    narration: "SAGE validates and evaluates each Skill, builds a Candidate, then activates an immutable Generation. Known tasks bypass the LLM. Complex goals load Skills on demand. Every request stays version-pinned behind the same policy boundary.",
    action: "Follow the main chain, then compare the Direct and Agent paths.",
  },
  {
    time: "0:34-0:48",
    seconds: 14,
    title: "1. Onboard and Govern an MCP Tool",
    caption: "Every MCP Tool enters with a version, risk class, and content digest.",
    narration: "An administrator connects an MCP server, discovers calculate total, and registers it as a read-only native tool with a version and content digest. Governance begins at onboarding.",
    action: "Point to calculate_total, read-only, version, and active status.",
  },
  {
    time: "0:48-1:06",
    seconds: 18,
    title: "2. Design, Publish, and Run in One Click",
    caption: "One action resolves dependencies, builds, activates, and starts a durable run.",
    narration: "The designer provides visual configuration. On the right is the validated Quick Flow execution chain. Quick Publish and Run resolves dependencies, builds, validates, activates, and starts a durable run in one step.",
    action: "Show the designer, follow the validated chain, then emphasize Quick Publish and Run.",
  },
  {
    time: "1:06-1:24",
    seconds: 18,
    title: "3. Inspect the Structured Run Output",
    caption: "Output: 32.4. Run, Release, and Generation remain traceable.",
    narration: "Temporal runs the workflow durably. Unit price twelve, quantity three, and a ten percent discount produce a JSON total of thirty-two point four, linked to its Run, Release, and Generation.",
    action: "Show completed, point to the real JSON zoom, then name the result and version links.",
  },
  {
    time: "1:24-1:39",
    seconds: 15,
    title: "Only Gated Releases Go Live",
    caption: "Pass: activate atomically. Fail: keep the active Generation.",
    narration: "Publishing never overwrites a live version. A new release activates atomically only after validation, evaluation, and approval. If any gate fails, the active Generation remains unchanged. Release history records the version switch.",
    action: "Compare version 1.1.0 active with 1.0.0 inactive, then explain both branches.",
  },
  {
    time: "1:39-1:46",
    seconds: 7,
    title: "Turn Personal Skills into Team Capabilities",
    caption: "Build once, reuse across the team. Releases are gated; failures keep the active version.",
    narration: "SAGE is not another tool gateway. It is the governance and delivery layer for Agent Skills.",
    action: "Hold on the three proof points and the SAGE mark.",
  },
];

module.exports = { OUTPUT, STORYBOARD };