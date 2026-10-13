[CmdletBinding()]
param(
    [string]$InputPptx,
    [string]$OutputVideo,
    [string]$Voice = "Microsoft Huihui Desktop",
    [string]$StoryboardModule = "./scripts/hackathon_ppt_content",
    [int]$FrameRate = 30,
    [int]$Width = 1920,
    [int]$Height = 1080,
    [switch]$KeepWorkFiles
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $InputPptx) {
    $InputPptx = Join-Path $projectRoot "docs\SAGE-hackathon-demo.zh-CN.pptx"
}
if (-not $OutputVideo) {
    $OutputVideo = Join-Path $projectRoot "docs\SAGE-hackathon-demo.zh-CN.mp4"
}

$InputPptx = [System.IO.Path]::GetFullPath($InputPptx)
$OutputVideo = [System.IO.Path]::GetFullPath($OutputVideo)
$subtitlePath = [System.IO.Path]::ChangeExtension($OutputVideo, ".srt")
$workDir = Join-Path $env:TEMP "sage-hackathon-video-build"
$slidesDir = Join-Path $workDir "slides"
$audioDir = Join-Path $workDir "audio"
$clipsDir = Join-Path $workDir "clips"

function Get-StoryBoard {
    $jsonPath = Join-Path $env:TEMP ("sage-video-storyboard-{0}.json" -f $PID)
    Push-Location $projectRoot
    try {
        & node -e "const p=require('path').resolve(process.argv[1]); require('fs').writeFileSync(process.argv[2], JSON.stringify(require(p).STORYBOARD), 'utf8')" $StoryboardModule $jsonPath
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to load the PPT storyboard."
        }
        $storyboard = @(Get-Content $jsonPath -Raw -Encoding utf8 | ConvertFrom-Json)
        if (-not $storyboard.Count -or -not $storyboard[0].caption -or
            ($storyboard | ConvertTo-Json -Depth 8 -Compress).Contains([char]0xFFFD)) {
            throw "Storyboard text was not decoded as UTF-8."
        }
        return $storyboard
    }
    finally {
        Pop-Location
        Remove-Item $jsonPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-FfmpegPath {
    $command = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "FFmpeg was not found and the project Python environment is unavailable."
    }
    $path = & $python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $path -or -not (Test-Path $path)) {
        throw "FFmpeg was not found. Install imageio-ffmpeg in .venv or add ffmpeg to PATH."
    }
    return $path.Trim()
}

function Get-WavDuration([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        $reader.ReadBytes(22) | Out-Null
        $reader.ReadInt16() | Out-Null
        $reader.ReadInt32() | Out-Null
        $byteRate = $reader.ReadInt32()
        $reader.ReadBytes(6) | Out-Null
        while ($reader.BaseStream.Position -lt $reader.BaseStream.Length) {
            $chunkId = [System.Text.Encoding]::ASCII.GetString($reader.ReadBytes(4))
            $chunkSize = $reader.ReadInt32()
            if ($chunkId -eq "data") {
                return $chunkSize / [double]$byteRate
            }
            $reader.BaseStream.Seek(
                $chunkSize + ($chunkSize % 2),
                [System.IO.SeekOrigin]::Current
            ) | Out-Null
        }
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
    throw "No audio data chunk was found in $Path."
}

function Convert-ToSrtTime([string]$Value) {
    $parts = $Value.Split(":")
    if ($parts.Count -ne 2) {
        throw "Invalid storyboard timecode: $Value"
    }
    return "00:{0:D2}:{1:D2},000" -f [int]$parts[0], [int]$parts[1]
}

function Export-Slides([string]$PresentationPath, [string]$Destination) {
    $powerPoint = New-Object -ComObject PowerPoint.Application
    try {
        $presentation = $powerPoint.Presentations.Open(
            $PresentationPath,
            $true,
            $false,
            $false
        )
        try {
            $presentation.Export($Destination, "PNG", $Width, $Height)
            return $presentation.Slides.Count
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
}

function New-TimedNarration(
    [System.Speech.Synthesis.SpeechSynthesizer]$Synthesizer,
    [string]$Narration,
    [double]$AllocatedSeconds,
    [string]$Destination
) {
    $maximumDuration = $AllocatedSeconds - 0.35
    for ($rate = -2; $rate -le 10; $rate++) {
        $Synthesizer.Rate = $rate
        $Synthesizer.SetOutputToWaveFile($Destination)
        $Synthesizer.Speak($Narration)
        $Synthesizer.SetOutputToNull()
        $duration = Get-WavDuration $Destination
        if ($duration -le $maximumDuration) {
            return [pscustomobject]@{
                Rate = $rate
                Duration = $duration
                Margin = $AllocatedSeconds - $duration
            }
        }
    }
    throw "Narration cannot fit into ${AllocatedSeconds}s even at the maximum voice rate."
}

function Invoke-Ffmpeg([string[]]$Arguments) {
    & $script:ffmpeg @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path $InputPptx)) {
    throw "Presentation not found: $InputPptx"
}

$storyboard = Get-StoryBoard
$totalSeconds = ($storyboard | Measure-Object -Property seconds -Sum).Sum
if ($storyboard.Count -ne 8) {
    throw "Expected 8 storyboard entries, found $($storyboard.Count)."
}
if ($totalSeconds -gt 120) {
    throw "Storyboard duration ${totalSeconds}s exceeds the two-minute limit."
}

$script:ffmpeg = Get-FfmpegPath
if (Test-Path $workDir) {
    Remove-Item $workDir -Recurse -Force
}
New-Item -ItemType Directory -Path $slidesDir, $audioDir, $clipsDir | Out-Null

$slideCount = Export-Slides $InputPptx $slidesDir
if ($slideCount -ne $storyboard.Count) {
    throw "PPT slide count $slideCount does not match storyboard count $($storyboard.Count)."
}

Add-Type -AssemblyName System.Speech
$synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
$availableVoices = @($synthesizer.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
if ($Voice -notin $availableVoices) {
    $synthesizer.Dispose()
    throw "Voice '$Voice' is unavailable. Installed voices: $($availableVoices -join ', ')"
}
$synthesizer.SelectVoice($Voice)

$concatLines = [System.Collections.Generic.List[string]]::new()
$subtitleLines = [System.Collections.Generic.List[string]]::new()
$timingReport = [System.Collections.Generic.List[object]]::new()

try {
    for ($index = 0; $index -lt $storyboard.Count; $index++) {
        $number = $index + 1
        $entry = $storyboard[$index]
        $image = Join-Path $slidesDir ("Slide{0}.PNG" -f $number)
        $audio = Join-Path $audioDir ("slide-{0:D2}.wav" -f $number)
        $clip = Join-Path $clipsDir ("slide-{0:D2}.mp4" -f $number)
        if (-not (Test-Path $image)) {
            throw "Rendered slide image is missing: $image"
        }

        $timing = New-TimedNarration `
            $synthesizer `
            $entry.narration `
            ([double]$entry.seconds) `
            $audio
        $timingReport.Add([pscustomobject]@{
            Slide = $number
            Allocated = [double]$entry.seconds
            Audio = [math]::Round($timing.Duration, 2)
            Rate = $timing.Rate
            Margin = [math]::Round($timing.Margin, 2)
        })

        $durationText = ([double]$entry.seconds).ToString(
            "0.###",
            [System.Globalization.CultureInfo]::InvariantCulture
        )
        Invoke-Ffmpeg @(
            "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", "$FrameRate", "-i", $image,
            "-i", $audio,
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", "scale=${Width}:${Height}:flags=lanczos,format=yuv420p",
            "-af", "loudnorm=I=-18:TP=-1.5:LRA=7,apad",
            "-t", $durationText,
            "-r", "$FrameRate",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", $clip
        )

        $concatPath = $clip.Replace("\", "/").Replace("'", "'\''")
        $concatLines.Add("file '$concatPath'")
        $times = $entry.time.Split("-")
        $subtitleLines.Add("$number")
        $subtitleLines.Add("$(Convert-ToSrtTime $times[0]) --> $(Convert-ToSrtTime $times[1])")
        $subtitleLines.Add([string]$entry.caption)
        $subtitleLines.Add("")
    }
}
finally {
    $synthesizer.Dispose()
}

$concatFile = Join-Path $workDir "concat.txt"
[System.IO.File]::WriteAllLines($concatFile, $concatLines, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllLines(
    $subtitlePath,
    $subtitleLines,
    [System.Text.UTF8Encoding]::new($true)
)

$outputDirectory = Split-Path -Parent $OutputVideo
if (-not (Test-Path $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}
Invoke-Ffmpeg @(
    "-y", "-hide_banner", "-loglevel", "error",
    "-f", "concat", "-safe", "0", "-i", $concatFile,
    "-c", "copy", "-movflags", "+faststart", $OutputVideo
)

Write-Output "Created $OutputVideo"
Write-Output "Created $subtitlePath"
Write-Output "Voice: $Voice"
Write-Output "Planned duration: ${totalSeconds}s"
$timingReport | Format-Table -AutoSize | Out-String | Write-Output

if (-not $KeepWorkFiles) {
    Remove-Item $workDir -Recurse -Force
}