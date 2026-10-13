"use strict";

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { ASSETS, OUTPUT, STORYBOARD } = require("./hackathon_ppt_content");

function loadPptxGenJS() {
  try {
    return require("pptxgenjs");
  } catch (error) {
    const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
    const npmRoot = execFileSync(npmCommand, ["root", "--global"], {
      encoding: "utf8",
    }).trim();
    return require(path.join(npmRoot, "pptxgenjs"));
  }
}

const PptxGenJS = loadPptxGenJS();

function validateInputs() {
  const duration = STORYBOARD.reduce((total, slide) => total + slide.seconds, 0);
  if (STORYBOARD.length !== 8) throw new Error("The deck must contain exactly 8 slides.");
  if (duration > 120) throw new Error(`Deck duration ${duration}s exceeds 120s.`);
  for (const [index, slide] of STORYBOARD.entries()) {
    if (!slide.caption || !slide.narration || !slide.action) {
      throw new Error(`Slide ${index + 1} is missing caption or speaker notes.`);
    }
  }
  for (const asset of Object.values(ASSETS)) {
    if (!fs.existsSync(asset)) throw new Error(`Missing asset: ${asset}`);
  }
  return duration;
}

const C = {
  bg: "F6F3ED",
  paper: "FFFDF9",
  ink: "202126",
  night: "181B22",
  muted: "666A73",
  line: "D8D2C8",
  soft: "EAE5DC",
  crimson: "A9233A",
  crimsonSoft: "F4DDE2",
  teal: "137A70",
  tealSoft: "DDEFEA",
  amber: "C77C10",
  amberSoft: "F7E8C9",
  white: "FFFFFF",
};

const FONT_CN = "Microsoft YaHei";
const FONT_LATIN = "Aptos Display";
const W = 13.333;
const H = 7.5;

function shadow() {
  return { type: "outer", color: "000000", opacity: 0.14, blur: 7, angle: 45, offset: 2 };
}

function text(slide, value, options = {}) {
  slide.addText(value, {
    fontFace: FONT_CN,
    color: C.ink,
    margin: 0,
    breakLine: false,
    fit: "shrink",
    ...options,
  });
}

function rect(slide, x, y, w, h, fill, line = fill, radius = false) {
  slide.addShape(radius ? "roundRect" : "rect", {
    x,
    y,
    w,
    h,
    rectRadius: radius ? 0.08 : undefined,
    fill: { color: fill },
    line: { color: line, width: line === fill ? 0 : 1 },
  });
}

function line(slide, x, y, w, h, color = C.line, width = 1.5, arrow = false) {
  slide.addShape("line", {
    x,
    y,
    w,
    h,
    line: { color, width, endArrowType: arrow ? "triangle" : "none" },
  });
}

function addNotes(slide, info) {
  slide.addNotes(
    `${info.time}｜${info.seconds} 秒\n旁白：${info.narration}\n画面提示：${info.action}`,
  );
}

function addHeader(slide, info, number, dark = false) {
  const foreground = dark ? C.white : C.ink;
  text(slide, "SAGE / HACKATHON DEMO", {
    x: 0.58,
    y: 0.3,
    w: 3.3,
    h: 0.22,
    fontFace: FONT_LATIN,
    fontSize: 9,
    bold: true,
    charSpacing: 1.5,
    color: dark ? "C9CBD0" : C.crimson,
  });
  text(slide, String(number).padStart(2, "0"), {
    x: 12.22,
    y: 0.28,
    w: 0.48,
    h: 0.24,
    fontFace: FONT_LATIN,
    fontSize: 10,
    bold: true,
    align: "right",
    color: dark ? "A8ABB2" : C.muted,
  });
  if (number > 1 && number < 8) {
    text(slide, info.title, {
      x: 0.58,
      y: 0.66,
      w: 11.9,
      h: 0.55,
      fontSize: 27,
      bold: true,
      color: foreground,
    });
  }
}

function addCaption(slide, info, dark = false) {
  const y = 6.55;
  slide.addShape("rect", {
    x: 0,
    y,
    w: W,
    h: H - y,
    fill: { color: dark ? "0D0F14" : C.night, transparency: dark ? 8 : 0 },
    line: { color: dark ? "0D0F14" : C.night, transparency: 100 },
  });
  rect(slide, 0.55, 6.8, 1.18, 0.38, C.crimson, C.crimson, true);
  text(slide, info.time, {
    x: 0.55,
    y: 6.85,
    w: 1.18,
    h: 0.2,
    fontFace: FONT_LATIN,
    fontSize: 11,
    bold: true,
    align: "center",
    color: C.white,
  });
  text(slide, info.caption, {
    x: 1.98,
    y: 6.73,
    w: 10.65,
    h: 0.5,
    fontSize: 16,
    bold: true,
    valign: "mid",
    color: C.white,
  });
}

function baseSlide(pptx, info, number, dark = false) {
  const slide = pptx.addSlide();
  slide.background = { color: dark ? C.night : C.bg };
  addHeader(slide, info, number, dark);
  addCaption(slide, info, dark);
  addNotes(slide, info);
  return slide;
}

function addTag(slide, label, x, y, w, fill, color = C.ink) {
  rect(slide, x, y, w, 0.38, fill, fill, true);
  text(slide, label, {
    x,
    y: y + 0.075,
    w,
    h: 0.18,
    fontFace: FONT_LATIN,
    fontSize: 9.5,
    bold: true,
    align: "center",
    color,
  });
}

function addNumber(slide, value, x, y, fill = C.crimson) {
  slide.addShape("ellipse", {
    x,
    y,
    w: 0.42,
    h: 0.42,
    fill: { color: fill },
    line: { color: C.white, width: 1.3 },
  });
  text(slide, String(value), {
    x,
    y: y + 0.075,
    w: 0.42,
    h: 0.19,
    fontFace: FONT_LATIN,
    fontSize: 11,
    bold: true,
    align: "center",
    color: C.white,
  });
}

function addCroppedImage(slide, asset, source, crop, dest, altText) {
  const scaleX = dest.w / crop.w;
  const scaleY = dest.h / crop.h;
  slide.addImage({
    path: asset,
    x: dest.x,
    y: dest.y,
    w: source.w * scaleX,
    h: source.h * scaleY,
    sizing: {
      type: "crop",
      x: crop.x * scaleX,
      y: crop.y * scaleY,
      w: dest.w,
      h: dest.h,
    },
    altText,
  });
}

function addBrowserFrame(slide, asset, source, crop, dest, altText) {
  slide.addShape("rect", {
    x: dest.x - 0.05,
    y: dest.y - 0.28,
    w: dest.w + 0.1,
    h: dest.h + 0.33,
    fill: { color: C.white },
    line: { color: "CBC5BB", width: 1 },
    shadow: shadow(),
  });
  rect(slide, dest.x - 0.05, dest.y - 0.28, dest.w + 0.1, 0.28, "E8E5DF", "CBC5BB");
  for (let i = 0; i < 3; i += 1) {
    slide.addShape("ellipse", {
      x: dest.x + 0.12 + i * 0.19,
      y: dest.y - 0.18,
      w: 0.08,
      h: 0.08,
      fill: { color: [C.crimson, C.amber, C.teal][i] },
      line: { color: [C.crimson, C.amber, C.teal][i], transparency: 100 },
    });
  }
  addCroppedImage(slide, asset, source, crop, dest, altText);
}

function addSlide1(pptx) {
  const info = STORYBOARD[0];
  const slide = baseSlide(pptx, info, 1, true);
  addTag(slide, "SKILL AGENT GOVERNANCE ENGINE", 0.72, 1.02, 3.55, "3A2029", C.white);
  text(slide, "SAGE", {
    x: 0.7,
    y: 1.55,
    w: 6.3,
    h: 1.15,
    fontFace: FONT_LATIN,
    fontSize: 66,
    bold: true,
    color: C.white,
  });
  text(slide, info.subtitle, {
    x: 0.76,
    y: 2.85,
    w: 6.7,
    h: 0.72,
    fontSize: 25,
    bold: true,
    color: "F0D8DE",
  });
  text(slide, "VALIDATE  /  PUBLISH  /  PIN  /  ROLLBACK", {
    x: 0.78,
    y: 4.02,
    w: 5.6,
    h: 0.28,
    fontFace: FONT_LATIN,
    fontSize: 11,
    bold: true,
    charSpacing: 2,
    color: "AEB2BA",
  });

  const stages = [
    { x: 7.6, label: "SKILL", sub: "Prompt / Script / Tool", fill: "30252A" },
    { x: 9.45, label: "GATE", sub: "Policy + Evidence", fill: "48212C" },
    { x: 11.3, label: "API", sub: "Team Runtime", fill: "153D39" },
  ];
  stages.forEach((stage, index) => {
    slide.addShape("ellipse", {
      x: stage.x,
      y: 2.0,
      w: 1.34,
      h: 1.34,
      fill: { color: stage.fill },
      line: { color: index === 1 ? C.crimson : "656A73", width: index === 1 ? 2.5 : 1.2 },
    });
    text(slide, stage.label, {
      x: stage.x,
      y: 2.46,
      w: 1.34,
      h: 0.25,
      fontFace: FONT_LATIN,
      fontSize: 16,
      bold: true,
      align: "center",
      color: C.white,
    });
    text(slide, stage.sub, {
      x: stage.x - 0.15,
      y: 3.55,
      w: 1.64,
      h: 0.35,
      fontSize: 10,
      align: "center",
      color: "BFC2C8",
    });
    if (index < stages.length - 1) line(slide, stage.x + 1.4, 2.67, 0.42, 0, "7D828B", 1.8, true);
  });
  text(slide, "个人能力", { x: 7.75, y: 4.42, w: 1.1, h: 0.25, fontSize: 13, bold: true, align: "center", color: "C6C8CD" });
  text(slide, "可信交付", { x: 9.58, y: 4.42, w: 1.1, h: 0.25, fontSize: 13, bold: true, align: "center", color: "F0D8DE" });
  text(slide, "组织复用", { x: 11.42, y: 4.42, w: 1.1, h: 0.25, fontSize: 13, bold: true, align: "center", color: "C6C8CD" });
}

function addSlide2(pptx) {
  const info = STORYBOARD[1];
  const slide = baseSlide(pptx, info, 2);
  const items = [
    { x: 0.58, n: "01", tag: "LOCAL", title: "环境依赖", body: "别人要先复制文件、安装运行时，\n才能复用同一能力。", color: C.crimson, soft: C.crimsonSoft },
    { x: 4.52, n: "02", tag: "POLICY", title: "边界藏在文档", body: "Schema、权限、超时和副作用\n没有统一的执行门禁。", color: C.amber, soft: C.amberSoft },
    { x: 8.46, n: "03", tag: "CHANGE", title: "升级影响运行", body: "覆盖式发布缺少版本固定，\n错误变更难以及时回退。", color: C.teal, soft: C.tealSoft },
  ];
  items.forEach((item) => {
    slide.addShape("rect", {
      x: item.x,
      y: 1.55,
      w: 3.55,
      h: 3.92,
      fill: { color: C.paper },
      line: { color: "D6D0C7", width: 1 },
      shadow: shadow(),
    });
    rect(slide, item.x, 1.55, 3.55, 0.12, item.color, item.color);
    text(slide, item.n, { x: item.x + 0.25, y: 1.93, w: 0.74, h: 0.52, fontFace: FONT_LATIN, fontSize: 29, bold: true, color: item.color });
    addTag(slide, item.tag, item.x + 2.25, 1.95, 0.95, item.soft, item.color);
    text(slide, item.title, { x: item.x + 0.25, y: 2.72, w: 2.95, h: 0.42, fontSize: 21, bold: true });
    text(slide, item.body, { x: item.x + 0.25, y: 3.42, w: 2.95, h: 0.9, fontSize: 15, color: "4D5159", breakLine: true, lineSpacingMultiple: 1.1 });
    line(slide, item.x + 0.25, 4.68, 2.95, 0, item.color, 2.2);
  });
  text(slide, "问题不在“能不能调用”，而在“能不能放心交付”", {
    x: 1.15,
    y: 5.72,
    w: 11.0,
    h: 0.42,
    fontSize: 20,
    bold: true,
    align: "center",
    color: C.crimson,
  });
}

function addSlide3(pptx) {
  const info = STORYBOARD[2];
  const slide = baseSlide(pptx, info, 3);
  const nodes = [
    ["SKILL", "Source"],
    ["VERIFY", "Schema · Eval"],
    ["CANDIDATE", "Release gate"],
    ["GENERATION", "Immutable"],
    ["RESOLVE", "Identity · Policy"],
    ["RUN", "Evidence"],
  ];
  nodes.forEach(([title, sub], index) => {
    const x = 0.52 + index * 2.12;
    const active = index === 3;
    rect(slide, x, 1.55, 1.72, 1.08, active ? C.crimson : C.paper, active ? C.crimson : "CDC7BD", true);
    text(slide, title, { x: x + 0.08, y: 1.86, w: 1.56, h: 0.23, fontFace: FONT_LATIN, fontSize: 13.5, bold: true, align: "center", color: active ? C.white : C.ink });
    text(slide, sub, { x: x + 0.08, y: 2.18, w: 1.56, h: 0.18, fontFace: FONT_LATIN, fontSize: 9.5, align: "center", color: active ? "F5DDE2" : C.muted });
    if (index < nodes.length - 1) line(slide, x + 1.76, 2.08, 0.31, 0, index === 2 ? C.crimson : "9F9A91", 1.5, true);
  });

  rect(slide, 0.75, 3.17, 5.72, 1.76, C.tealSoft, "9CCEC5", true);
  addTag(slide, "DIRECT", 1.04, 3.47, 1.05, C.teal, C.white);
  text(slide, "确定性任务", { x: 1.04, y: 4.02, w: 2.4, h: 0.3, fontSize: 19, bold: true, color: C.teal });
  text(slide, "类型化输入输出，跳过模型推理", { x: 1.04, y: 4.43, w: 3.75, h: 0.27, fontSize: 12.5, color: "356B65" });
  text(slide, "0", { x: 4.88, y: 3.43, w: 0.75, h: 0.62, fontFace: FONT_LATIN, fontSize: 39, bold: true, align: "center", color: C.teal });
  text(slide, "MODEL CALLS", { x: 4.55, y: 4.16, w: 1.42, h: 0.2, fontFace: FONT_LATIN, fontSize: 9, bold: true, align: "center", color: C.teal });

  rect(slide, 6.85, 3.17, 5.72, 1.76, C.crimsonSoft, "DCADB7", true);
  addTag(slide, "AGENT", 7.14, 3.47, 1.05, C.crimson, C.white);
  text(slide, "复杂目标", { x: 7.14, y: 4.02, w: 2.4, h: 0.3, fontSize: 19, bold: true, color: C.crimson });
  text(slide, "按需加载 Skill，并组合受控 Tool", { x: 7.14, y: 4.43, w: 3.85, h: 0.27, fontSize: 12.5, color: "7F4350" });
  text(slide, "ON DEMAND", { x: 10.73, y: 3.82, w: 1.58, h: 0.25, fontFace: FONT_LATIN, fontSize: 12.5, bold: true, align: "center", color: C.crimson });

  rect(slide, 1.56, 5.36, 10.2, 0.57, C.night, C.night, true);
  text(slide, "IDENTITY   ·   SCHEMA   ·   RISK   ·   BUDGET   ·   APPROVAL   ·   AUDIT", {
    x: 1.76,
    y: 5.54,
    w: 9.8,
    h: 0.2,
    fontFace: FONT_LATIN,
    fontSize: 10.5,
    bold: true,
    charSpacing: 1,
    align: "center",
    color: C.white,
  });
}

function addSlide4(pptx) {
  const info = STORYBOARD[3];
  const slide = baseSlide(pptx, info, 4);
  addBrowserFrame(
    slide,
    ASSETS.mcp,
    { w: 1440, h: 1451 },
    { x: 205, y: 535, w: 1190, h: 675 },
    { x: 0.62, y: 1.57, w: 8.1, h: 4.6 },
    "SAGE MCP 管理页面，显示 calculate_total 的治理状态",
  );
  slide.addShape("rect", { x: 3.14, y: 2.22, w: 5.43, h: 0.57, fill: { color: C.white, transparency: 100 }, line: { color: C.crimson, width: 2 } });
  slide.addShape("rect", { x: 3.14, y: 4.47, w: 5.43, h: 0.32, fill: { color: C.white, transparency: 100 }, line: { color: C.teal, width: 2 } });
  text(slide, "能力进入运行时之前，\n先成为可治理资产", { x: 9.12, y: 1.48, w: 3.55, h: 0.88, fontSize: 22, bold: true, breakLine: true, color: C.crimson });
  const steps = [
    ["连接", "MCP Server"],
    ["发现", "calculate_total"],
    ["注册", "version · risk · digest"],
  ];
  steps.forEach(([title, sub], index) => {
    const y = 2.72 + index * 0.92;
    addNumber(slide, index + 1, 9.13, y, index === 2 ? C.teal : C.crimson);
    text(slide, title, { x: 9.72, y: y + 0.01, w: 0.92, h: 0.24, fontSize: 16, bold: true });
    text(slide, sub, { x: 10.67, y: y + 0.03, w: 1.95, h: 0.22, fontFace: FONT_LATIN, fontSize: 11.5, color: C.muted });
    if (index < steps.length - 1) line(slide, 9.34, y + 0.48, 0, 0.36, "B9B2A8", 1.3);
  });
  addTag(slide, "READ-ONLY", 9.13, 5.58, 1.35, C.tealSoft, C.teal);
  addTag(slide, "ACTIVE", 10.62, 5.58, 1.05, C.teal, C.white);
}

function addSlide5(pptx) {
  const info = STORYBOARD[4];
  const slide = baseSlide(pptx, info, 5);
  addBrowserFrame(
    slide,
    ASSETS.workflow,
    { w: 1425, h: 1156 },
    { x: 160, y: 160, w: 1230, h: 813 },
    { x: 0.58, y: 1.55, w: 7.18, h: 4.75 },
    "SAGE Workflow Designer 中的已保存工作流",
  );
  addTag(slide, "WORKFLOW DESIGNER", 0.82, 1.69, 1.72, C.night, C.white);
  text(slide, "已验收 QUICK FLOW", { x: 8.22, y: 1.48, w: 2.25, h: 0.22, fontSize: 10, bold: true, charSpacing: 0.5, color: C.crimson });
  const flow = [
    ["输入预处理", "Echo Skill", C.soft, C.ink],
    ["业务计算", "calculate_total", C.crimsonSoft, C.crimson],
    ["结果封装", "Calc Skill", C.tealSoft, C.teal],
  ];
  flow.forEach(([name, sub, fill, color], index) => {
    const y = 1.85 + index * 0.92;
    rect(slide, 8.22, y, 4.35, 0.67, fill, fill, true);
    text(slide, name, { x: 8.48, y: y + 0.12, w: 1.55, h: 0.2, fontFace: FONT_LATIN, fontSize: 11, bold: true, color });
    text(slide, sub, { x: 10.2, y: y + 0.12, w: 2.05, h: 0.22, fontSize: 12.5, align: "right", color });
    if (index < flow.length - 1) line(slide, 10.39, y + 0.69, 0, 0.2, "AAA49B", 1.4, true);
  });
  rect(slide, 8.22, 4.88, 4.35, 0.77, C.crimson, C.crimson, true);
  text(slide, "QUICK PUBLISH AND RUN", { x: 8.4, y: 5.12, w: 3.99, h: 0.23, fontFace: FONT_LATIN, fontSize: 13, bold: true, align: "center", color: C.white });
  text(slide, "resolve  ·  build  ·  validate  ·  activate  ·  run", { x: 8.18, y: 5.88, w: 4.45, h: 0.25, fontFace: FONT_LATIN, fontSize: 9.5, align: "center", color: C.muted });
}

function addSlide6(pptx) {
  const info = STORYBOARD[5];
  const slide = baseSlide(pptx, info, 6);
  addBrowserFrame(
    slide,
    ASSETS.completed,
    { w: 1440, h: 1448 },
    { x: 245, y: 465, w: 1115, h: 715 },
    { x: 0.58, y: 1.55, w: 7.3, h: 4.68 },
    "SAGE Solution 运行已完成并显示结构化结果",
  );
  slide.addShape("rect", { x: 5.08, y: 3.12, w: 2.62, h: 2.92, fill: { color: C.white }, line: { color: C.crimson, width: 2 }, shadow: shadow() });
  addCroppedImage(
    slide,
    ASSETS.completedMobile,
    { w: 390, h: 1753 },
    { x: 30, y: 1070, w: 330, h: 390 },
    { x: 5.14, y: 3.36, w: 2.5, h: 2.62 },
    "结构化 JSON 输出，包含 discount、quantity、subtotal、total 32.4 和 unit_price",
  );
  addTag(slide, "STRUCTURED JSON", 5.24, 3.18, 1.62, C.crimson, C.white);
  addTag(slide, "TEMPORAL RUN", 8.35, 1.55, 1.55, C.tealSoft, C.teal);
  text(slide, "单价 12  ·  数量 3  ·  折扣 10%", { x: 8.35, y: 2.05, w: 4.35, h: 0.45, fontSize: 18, bold: true, align: "center", color: C.ink });
  line(slide, 8.72, 2.73, 3.62, 0, C.line, 1.4);
  text(slide, "32.4", { x: 8.35, y: 2.92, w: 4.35, h: 1.0, fontFace: FONT_LATIN, fontSize: 52, bold: true, align: "center", color: C.crimson });
  text(slide, "RUN OUTPUT", { x: 8.35, y: 3.92, w: 4.35, h: 0.23, fontFace: FONT_LATIN, fontSize: 10, bold: true, charSpacing: 1.6, align: "center", color: C.muted });
  const trace = [
    ["RUN", "completed"],
    ["RELEASE", "immutable"],
    ["GENERATION", "pinned"],
  ];
  trace.forEach(([label, value], index) => {
    const x = 8.35 + index * 1.47;
    rect(slide, x, 4.68, 1.3, 0.92, index === 0 ? C.tealSoft : C.paper, index === 0 ? "A5D2CA" : "D5CFC5", true);
    text(slide, label, { x, y: 4.88, w: 1.3, h: 0.18, fontFace: FONT_LATIN, fontSize: 8.5, bold: true, align: "center", color: index === 0 ? C.teal : C.muted });
    text(slide, value, { x, y: 5.23, w: 1.3, h: 0.18, fontFace: FONT_LATIN, fontSize: 10.5, bold: true, align: "center", color: C.ink });
  });
}

function addSlide7(pptx) {
  const info = STORYBOARD[6];
  const slide = baseSlide(pptx, info, 7);
  addBrowserFrame(
    slide,
    ASSETS.release,
    { w: 1440, h: 1000 },
    { x: 230, y: 170, w: 1130, h: 764 },
    { x: 0.58, y: 1.55, w: 7.15, h: 4.83 },
    "SAGE Release 页面，显示 1.1.0 active 和 1.0.0 inactive",
  );
  text(slide, "RELEASE GATE", { x: 8.15, y: 1.55, w: 1.75, h: 0.24, fontFace: FONT_LATIN, fontSize: 10, bold: true, charSpacing: 1.3, color: C.crimson });
  rect(slide, 8.15, 1.98, 4.45, 0.82, C.night, C.night, true);
  text(slide, "Validate  ·  Evaluate  ·  Approve", { x: 8.38, y: 2.25, w: 3.98, h: 0.22, fontFace: FONT_LATIN, fontSize: 11.5, bold: true, align: "center", color: C.white });
  line(slide, 10.37, 2.82, 0, 0.37, "9E988F", 1.5);
  line(slide, 8.94, 3.17, 1.43, 0.36, C.teal, 1.8);
  line(slide, 10.37, 3.17, 1.43, 0.36, C.amber, 1.8, true);
  rect(slide, 8.04, 3.57, 2.45, 1.05, C.tealSoft, "9CCEC5", true);
  addTag(slide, "PASS", 8.28, 3.78, 0.82, C.teal, C.white);
  text(slide, "1.1.0  ACTIVE", { x: 8.25, y: 4.22, w: 2.0, h: 0.2, fontFace: FONT_LATIN, fontSize: 12, bold: true, align: "center", color: C.teal });
  rect(slide, 10.75, 3.57, 2.05, 1.05, C.amberSoft, "E1BD7A", true);
  addTag(slide, "FAIL", 11.3, 3.78, 0.82, C.amber, C.white);
  text(slide, "KEEP CURRENT", { x: 10.96, y: 4.22, w: 1.63, h: 0.2, fontFace: FONT_LATIN, fontSize: 10.5, bold: true, align: "center", color: C.amber });
  rect(slide, 8.15, 5.05, 4.45, 0.78, C.paper, "CFC8BE", true);
  text(slide, "FAILED CANDIDATE", { x: 8.4, y: 5.24, w: 1.75, h: 0.2, fontFace: FONT_LATIN, fontSize: 9.5, bold: true, color: C.muted });
  text(slide, "ACTIVE GENERATION UNCHANGED", { x: 9.86, y: 5.24, w: 2.5, h: 0.2, fontFace: FONT_LATIN, fontSize: 9.5, bold: true, align: "right", color: C.crimson });
}

function addSlide8(pptx) {
  const info = STORYBOARD[7];
  const slide = baseSlide(pptx, info, 8, true);
  text(slide, "SAGE", { x: 0.72, y: 0.92, w: 3.5, h: 0.78, fontFace: FONT_LATIN, fontSize: 46, bold: true, color: C.white });
  text(slide, info.title, { x: 0.74, y: 1.85, w: 7.9, h: 0.62, fontSize: 30, bold: true, color: "F0D8DE" });
  text(slide, "不是另一个 Tool Gateway，而是 Agent Skills 的治理与交付层。", { x: 0.76, y: 2.75, w: 8.2, h: 0.42, fontSize: 18, color: "C7CAD0" });
  const stats = [
    { x: 0.75, value: "0", label: "MODEL CALLS", sub: "仅 Direct 路径", color: C.teal },
    { x: 4.7, value: "32.4", label: "RUN OUTPUT", sub: "结构化运行结果", color: C.crimson },
    { x: 8.65, value: "v1→v2", label: "GATED RELEASES", sub: "通过门禁才生效", color: C.amber },
  ];
  stats.forEach((stat) => {
    rect(slide, stat.x, 3.62, 3.42, 1.75, "232730", "3B3F49", true);
    text(slide, stat.value, { x: stat.x + 0.2, y: 3.92, w: 1.25, h: 0.65, fontFace: FONT_LATIN, fontSize: 38, bold: true, align: "center", color: stat.color });
    text(slide, stat.label, { x: stat.x + 1.45, y: 4.02, w: 1.68, h: 0.25, fontFace: FONT_LATIN, fontSize: 10, bold: true, color: C.white });
    text(slide, stat.sub, { x: stat.x + 1.45, y: 4.5, w: 1.68, h: 0.25, fontSize: 12, color: "BFC3CA" });
  });
  text(slide, "01:46", { x: 11.5, y: 5.87, w: 1.0, h: 0.3, fontFace: FONT_LATIN, fontSize: 13, bold: true, align: "right", color: "999DA5" });
}

async function buildDeck() {
  const duration = validateInputs();
  const pptx = new PptxGenJS();
  pptx.layout = "LAYOUT_WIDE";
  pptx.author = "SAGE Team";
  pptx.company = "SAGE";
  pptx.subject = "Hackathon judge demo with Chinese captions and timed speaker notes";
  pptx.title = "SAGE：让团队 Skills 成为可治理的 Agent API";
  pptx.lang = "zh-CN";
  pptx.theme = { headFontFace: FONT_CN, bodyFontFace: FONT_CN, lang: "zh-CN" };

  [addSlide1, addSlide2, addSlide3, addSlide4, addSlide5, addSlide6, addSlide7, addSlide8]
    .forEach((addSlide) => addSlide(pptx));

  await pptx.writeFile({ fileName: OUTPUT });
  console.log(`Created ${OUTPUT}`);
  console.log(`Slides: ${STORYBOARD.length}; planned duration: ${duration}s`);
}

if (require.main === module) {
  buildDeck().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

module.exports = { ASSETS, OUTPUT, PptxGenJS, STORYBOARD, buildDeck, validateInputs };