[CmdletBinding()]
param(
    [string]$InputPptx,
    [string]$OutputPptx
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $InputPptx) {
    $InputPptx = Join-Path $projectRoot "docs\SAGE-hackathon-demo.zh-CN.pptx"
}
if (-not $OutputPptx) {
    $OutputPptx = Join-Path $projectRoot "docs\SAGE-hackathon-demo.en-US.pptx"
}
$InputPptx = [System.IO.Path]::GetFullPath($InputPptx)
$OutputPptx = [System.IO.Path]::GetFullPath($OutputPptx)

function Get-EnglishStoryboard {
    $jsonPath = Join-Path $env:TEMP ("sage-ppt-en-storyboard-{0}.json" -f $PID)
    Push-Location $projectRoot
    try {
        & node -e "require('fs').writeFileSync(process.argv[1], JSON.stringify(require('./scripts/hackathon_ppt_content.en').STORYBOARD), 'utf8')" $jsonPath
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to load the English storyboard."
        }
        return @(Get-Content $jsonPath -Raw -Encoding utf8 | ConvertFrom-Json)
    }
    finally {
        Pop-Location
        Remove-Item $jsonPath -Force -ErrorAction SilentlyContinue
    }
}

$replacements = [ordered]@{
    "把个人 Skill，升级为团队可复用、可治理的 AI 能力。" = "Turn personal Skills into governed AI capabilities the whole team can reuse."
    "让团队 Skills 成为可治理的 Agent API" = "Team Skills, Governed as Agent APIs"
    "个人能力" = "PERSONAL ASSET"
    "可信交付" = "GOVERNED DELIVERY"
    "组织复用" = "TEAM REUSE"
    "AI 能力很多，却停在个人电脑里" = "AI Capabilities Are Trapped on Personal Machines"
    "难点不是调用 Tool，而是安全交付与持续升级。" = "The challenge is not calling a Tool. It is safe delivery and continuous change."
    "环境依赖" = "DEPENDENCY FRICTION"
    "别人要先复制文件、安装运行时，`r才能复用同一能力。" = "Others must copy files and install runtimes`rto reuse the same capability."
    "边界藏在文档" = "POLICIES STAY IN DOCS"
    "Schema、权限、超时和副作用`r没有统一的执行门禁。" = "Schemas, permissions, timeouts, and side effects`rlack one enforcement gate."
    "升级影响运行" = "UPGRADES DISRUPT WORK"
    "覆盖式发布缺少版本固定，`r错误变更难以及时回退。" = "Overwrite releases lack version pinning,`rand bad changes are hard to contain."
    '问题不在“能不能调用”，而在“能不能放心交付”' = "The question is not 'Can we call it?' but 'Can we ship it safely?'"
    "一条可信交付链，两种执行路径" = "One Trusted Delivery Chain, Two Execution Paths"
    "验证、发布、版本固定与执行策略，被连接成可信闭环。" = "Validation, release, version pinning, and policy form one trusted loop."
    "确定性任务" = "DETERMINISTIC TASKS"
    "类型化输入输出，跳过模型推理" = "Typed I/O that bypasses model reasoning"
    "复杂目标" = "COMPLEX GOALS"
    "按需加载 Skill，并组合受控 Tool" = "Load Skills on demand; compose governed Tools"
    "1. 接入并治理 MCP 能力" = "1. Onboard and Govern an MCP Tool"
    "MCP Tool 接入时即绑定版本、风险等级与内容摘要。" = "Every MCP Tool enters with a version, risk class, and content digest."
    "能力进入运行时之前，`r先成为可治理资产" = "Governance starts before`rthe capability can run"
    "连接" = "CONNECT"
    "发现" = "DISCOVER"
    "注册" = "REGISTER"
    "2. 设计工作流，一键发布运行" = "2. Design, Publish, and Run in One Click"
    "一键完成依赖解析、构建、激活与持久运行。" = "One action resolves dependencies, builds, activates, and starts a durable run."
    "已验收 QUICK FLOW" = "VALIDATED QUICK FLOW"
    "输入预处理" = "NORMALIZE"
    "业务计算" = "CALCULATE"
    "结果封装" = "PACKAGE"
    "3. 查看结构化运行结果" = "3. Inspect the Structured Run Output"
    "输出 32.4；Run、Release 与 Generation 均可追踪。" = "Output: 32.4. Run, Release, and Generation remain traceable."
    "单价 12  ·  数量 3  ·  折扣 10%" = "Unit price 12  ·  Quantity 3  ·  Discount 10%"
    "新版本通过门禁才会生效" = "Only Gated Releases Go Live"
    "通过则原子激活；失败则保留当前 Generation。" = "Pass: activate atomically. Fail: keep the active Generation."
    "一次开发，全团队复用；发布有门禁，失败保留当前版本。" = "Build once, reuse across the team. Releases are gated; failures keep the active version."
    "让个人 Skill 成为组织能力" = "Turn Personal Skills into Team Capabilities"
    "不是另一个 Tool Gateway，而是 Agent Skills 的治理与交付层。" = "Not another Tool Gateway: the governance and delivery layer for Agent Skills."
    "仅 Direct 路径" = "DIRECT PATH ONLY"
    "结构化运行结果" = "STRUCTURED RUN OUTPUT"
    "通过门禁才生效" = "ACTIVATE AFTER GATES"
}

$altTextReplacements = [ordered]@{
    "SAGE MCP 管理页面，显示 calculate_total 的治理状态" = "SAGE MCP management page showing the governed calculate_total Tool"
    "SAGE Workflow Designer 中的已保存工作流" = "Saved workflow in the SAGE Workflow Designer"
    "SAGE Solution 运行已完成并显示结构化结果" = "Completed SAGE Solution run with structured output"
    "结构化 JSON 输出，包含 discount、quantity、subtotal、total 32.4 和 unit_price" = "Structured JSON output containing discount, quantity, subtotal, total 32.4, and unit_price"
    "SAGE Release 页面，显示 1.1.0 active 和 1.0.0 inactive" = "SAGE Release page showing version 1.1.0 active and 1.0.0 inactive"
}

if (-not (Test-Path $InputPptx)) {
    throw "Presentation not found: $InputPptx"
}
$storyboard = Get-EnglishStoryboard
if ($storyboard.Count -ne 8) {
    throw "Expected 8 English storyboard entries, found $($storyboard.Count)."
}

$outputDirectory = Split-Path -Parent $OutputPptx
if (-not (Test-Path $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}
Copy-Item $InputPptx $OutputPptx -Force

$powerPoint = New-Object -ComObject PowerPoint.Application
$translated = 0
try {
    $presentation = $powerPoint.Presentations.Open($OutputPptx, $false, $false, $false)
    try {
        foreach ($slide in $presentation.Slides) {
            foreach ($shape in $slide.Shapes) {
                if ($shape.HasTextFrame -eq -1 -and $shape.TextFrame.HasText -eq -1) {
                    $current = $shape.TextFrame.TextRange.Text
                    if ($replacements.Contains($current)) {
                        $shape.TextFrame.TextRange.Text = $replacements[$current]
                        $shape.TextFrame.TextRange.Font.Name = "Aptos"
                        $translated++
                    }
                    $translatedValue = $shape.TextFrame.TextRange.Text
                    if ($translatedValue -in @("CONNECT", "DISCOVER", "REGISTER")) {
                        $shape.TextFrame.TextRange.Font.Size = 11
                        $shape.TextFrame.WordWrap = 0
                    }
                    if ($translatedValue -eq "v1→v2") {
                        $shape.TextFrame.TextRange.Font.Size = 26
                        $shape.TextFrame.WordWrap = 0
                    }
                }
                $altText = $shape.AlternativeText
                if ($altTextReplacements.Contains($altText)) {
                    $shape.AlternativeText = $altTextReplacements[$altText]
                }
            }

            $entry = $storyboard[$slide.SlideIndex - 1]
            foreach ($shape in $slide.NotesPage.Shapes) {
                if ($shape.Type -eq 14 -and $shape.PlaceholderFormat.Type -eq 2) {
                    $shape.TextFrame.TextRange.Text = @(
                        "$($entry.time) | $($entry.seconds) seconds"
                        "Narration: $($entry.narration)"
                        "Visual cue: $($entry.action)"
                    ) -join "`r"
                    $shape.TextFrame.TextRange.Font.Name = "Aptos"
                }
            }
        }

        $remaining = [System.Collections.Generic.List[string]]::new()
        foreach ($slide in $presentation.Slides) {
            foreach ($shape in $slide.Shapes) {
                if ($shape.HasTextFrame -eq -1 -and $shape.TextFrame.HasText -eq -1) {
                    $value = $shape.TextFrame.TextRange.Text
                    if ($value -match "[\u3400-\u9FFF]") {
                        $remaining.Add("Slide $($slide.SlideIndex): $value")
                    }
                }
            }
        }
        if ($remaining.Count) {
            throw "Untranslated slide text remains:`n$($remaining -join "`n")"
        }
        if ($translated -lt 40) {
            throw "Only $translated text shapes were translated; expected at least 40."
        }
        $presentation.Save()
    }
    finally {
        $presentation.Close()
        [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation) |
            Out-Null
    }
}
finally {
    $powerPoint.Quit()
    [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($powerPoint) |
        Out-Null
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::Open(
    $OutputPptx,
    [System.IO.Compression.ZipArchiveMode]::Update
)
try {
    $entry = $archive.GetEntry("docProps/core.xml")
    if (-not $entry) {
        throw "The presentation is missing docProps/core.xml."
    }
    $reader = [System.IO.StreamReader]::new($entry.Open(), [System.Text.Encoding]::UTF8)
    try {
        $coreXml = $reader.ReadToEnd()
    }
    finally {
        $reader.Dispose()
    }
    $coreXml = [regex]::Replace(
        $coreXml,
        "<dc:title>.*?</dc:title>",
        "<dc:title>SAGE: Turning Team Skills into Governed Agent APIs</dc:title>"
    )
    $stream = $entry.Open()
    try {
        $stream.SetLength(0)
        $writer = [System.IO.StreamWriter]::new(
            $stream,
            [System.Text.UTF8Encoding]::new($false)
        )
        try {
            $writer.Write($coreXml)
        }
        finally {
            $writer.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}
finally {
    $archive.Dispose()
}

Write-Output "Created $OutputPptx"
Write-Output "Translated text shapes: $translated"
Write-Output "English speaker notes: $($storyboard.Count)"