<#
.SYNOPSIS
  iis_analyzer.ps1 - IIS W3C log analyzer (entry-point skill, v2 PS edition).
.DESCRIPTION
  Mirrors IIS_logs/scripts/iis_analyzer.py: dynamic #Fields header parsing,
  metric calc, problem classification, optional auto-trigger of orchestrator.
  Emits the standard skill envelope from _shared/Contract.psm1.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $LogPath,
    [switch] $AutoTrigger
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Import-Module (Join-Path $root '_shared\Contract.psm1') -Force

$SKILL = 'iis_logs'

# Thresholds (kept identical to the Python sibling)
$TH = @{
    P99      = 5000
    Rate5xx  = 1.0
    Abs5xx   = 100
    AuthErr  = 10
    NotFound = 10.0
    Susp     = 30.0
}

function Get-LogFiles {
    param([string] $Path)
    if (Test-Path -LiteralPath $Path -PathType Container) {
        return @(
            Get-ChildItem -LiteralPath $Path -Filter *.log -File -Recurse |
                Where-Object { Test-IisLogFile -File $_ } |
                Sort-Object FullName
        )
    }
    return @(Get-Item -LiteralPath $Path)
}

function Test-IisLogFile {
    param([Parameter(Mandatory)] [System.IO.FileInfo] $File)
    $inspected = 0
    foreach ($line in [System.IO.File]::ReadLines($File.FullName)) {
        $inspected++
        if ($line -match '^#Software:\s+Microsoft Internet Information Services') {
            return $true
        }
        if ($line.StartsWith('#Fields:') -and
            $line -match '\bsc-status\b' -and
            $line -match '\btime-taken\b' -and
            $line -notmatch '\bs-reason\b') {
            return $true
        }
        if ($inspected -ge 64) { break }
    }
    return $false
}

function ConvertTo-LogInt {
    param([object] $Value)
    $parsed = 0
    if ($null -ne $Value -and [int]::TryParse([string] $Value, [ref] $parsed)) {
        return $parsed
    }
    return 0
}

function Read-W3CLog {
    param([Parameter(Mandatory)] [System.IO.FileInfo] $File)
    $fields = $null
    $lineNumber = 0
    foreach ($line in [System.IO.File]::ReadLines($File.FullName)) {
        $lineNumber++
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.StartsWith('#')) {
            if ($line.StartsWith('#Fields:')) {
                $fields = ($line.Substring(8)).Trim() -split '\s+'
            }
            continue
        }
        if (-not $fields) { continue }
        $parts = $line -split '\s+'
        if ($parts.Count -lt $fields.Count) {
            $parts = @($parts) + (@('-') * ($fields.Count - $parts.Count))
        }
        $row = [ordered]@{}
        for ($i = 0; $i -lt $fields.Count; $i++) { $row[$fields[$i]] = $parts[$i] }
        # Output normalised entry
        [pscustomobject]@{
            timestamp   = "$($row['date']) $($row['time'])".Trim()
            method      = $row['cs-method']
            uri         = $row['cs-uri-stem']
            query       = $row['cs-uri-query']
            host        = $row['cs-host']
            username    = $row['cs-username']
            client_ip   = $row['c-ip']
            server_ip   = $row['s-ip']
            server_port = $row['s-port']
            site_name   = $row['s-sitename']
            site_id     = $row['s-siteid']
            status      = ConvertTo-LogInt $row['sc-status']
            substatus   = ConvertTo-LogInt $row['sc-substatus']
            win_status  = ConvertTo-LogInt $row['sc-win32-status']
            time_taken  = ConvertTo-LogInt $row['time-taken']
            source_file = $File.FullName
            line_number = $lineNumber
        }
    }
}

function Get-Percentile {
    param([int[]] $Sorted, [double] $Pct)
    if (-not $Sorted -or $Sorted.Count -eq 0) { return 0 }
    $idx = [Math]::Min($Sorted.Count - 1, [Math]::Max(0, [int]($Sorted.Count * $Pct) - 1))
    return $Sorted[$idx]
}

function Get-IisMetrics {
    param([object[]] $Entries)
    if (-not $Entries -or $Entries.Count -eq 0) { return @{ total_requests = 0 } }
    $times = @($Entries | ForEach-Object { $_.time_taken } | Sort-Object)
    $statuses = @($Entries | ForEach-Object { $_.status })
    $err4 = @($statuses | Where-Object { $_ -ge 400 -and $_ -lt 500 }).Count
    $err5 = @($statuses | Where-Object { $_ -ge 500 -and $_ -lt 600 }).Count
    $total = $Entries.Count
    $tsAll = @($Entries | ForEach-Object { $_.timestamp } | Where-Object { $_ })
    $statusDistribution = @{}
    foreach ($group in @($Entries | Group-Object status)) {
        $statusDistribution[[string] $group.Name] = $group.Count
    }
    $statusSignatures = @(
        $Entries |
            Where-Object { $_.status -ge 400 } |
            Group-Object { "$($_.status).$($_.substatus)/$($_.win_status)" } |
            Sort-Object Count -Descending |
            Select-Object -First 10 |
            ForEach-Object {
                @{
                    signature = $_.Name
                    count = $_.Count
                    samples = @($_.Group | Select-Object -First 3 | ForEach-Object {
                        @{
                            timestamp = $_.timestamp
                            method = $_.method
                            uri = $_.uri
                            client_ip = $_.client_ip
                            site_name = $_.site_name
                            source_file = $_.source_file
                            line_number = $_.line_number
                        }
                    })
                }
            }
    )
    return @{
        total_requests        = $total
        avg_response_time_ms  = ($times | Measure-Object -Average).Average
        min_response_time_ms  = $times[0]
        max_response_time_ms  = $times[-1]
        p95_response_time_ms  = Get-Percentile $times 0.95
        p99_response_time_ms  = Get-Percentile $times 0.99
        error_4xx_count       = $err4
        error_5xx_count       = $err5
        error_rate_percent    = ($err4 + $err5) / $total * 100
        status_code_distribution = $statusDistribution
        top_error_signatures  = $statusSignatures
        time_range = @{
            start = ($tsAll | Sort-Object | Select-Object -First 1)
            end   = ($tsAll | Sort-Object | Select-Object -Last 1)
        }
    }
}

function Get-IisProblems {
    param([object[]] $Entries, [hashtable] $Metrics)
    $reg = (Get-SkillRegistry).problem_types
    $problems = New-Object System.Collections.Generic.List[hashtable]
    if ($Metrics.total_requests -eq 0) { return $problems }
    $total = $Metrics.total_requests

    if ($Metrics.error_5xx_count -gt 0) {
        $rate = $Metrics.error_5xx_count / $total * 100
        if ($rate -gt $TH.Rate5xx -or $Metrics.error_5xx_count -gt $TH.Abs5xx) {
            $problems.Add(@{
                type        = '5xx_error'; severity = 'critical'
                description = "$($Metrics.error_5xx_count) 5xx errors ($([Math]::Round($rate,2))%)"
                follow_ups  = @($reg.'5xx_error'.follow_ups)
                evidence    = @{ top_error_signatures = $Metrics.top_error_signatures }
            })
        }
    }
    if ($Metrics.p99_response_time_ms -gt $TH.P99) {
        $sev = if ($Metrics.p99_response_time_ms -gt 2 * $TH.P99) {'critical'} else {'warning'}
        $problems.Add(@{
            type='high_latency'; severity=$sev
            description="p99 response time $($Metrics.p99_response_time_ms) ms"
            follow_ups=@($reg.'high_latency'.follow_ups)
        })
    }
    $authErrs = @($Entries | Where-Object { $_.status -in 401,403 }).Count
    if ($authErrs -gt $TH.AuthErr) {
        $problems.Add(@{
            type='auth_error'; severity='warning'
            description="$authErrs auth/permission failures"
            follow_ups=@($reg.'auth_error'.follow_ups)
        })
    }
    $nf = @($Entries | Where-Object { $_.status -eq 404 }).Count
    if ($nf -gt $total * ($TH.NotFound / 100)) {
        $problems.Add(@{
            type='not_found'; severity='info'
            description="$nf 404 responses"; follow_ups=@()
        })
    }
    $ipGroups = $Entries | Group-Object client_ip | Sort-Object Count -Descending
    if ($ipGroups -and ($ipGroups[0].Count / $total * 100) -gt $TH.Susp) {
        $problems.Add(@{
            type='suspicious_traffic'; severity='warning'
            description="single IP $($ipGroups[0].Name) = $([Math]::Round($ipGroups[0].Count/$total*100,1))% of traffic"
            follow_ups=@($reg.'suspicious_traffic'.follow_ups)
            evidence=@{ client_ip=$ipGroups[0].Name; count=$ipGroups[0].Count }
        })
    }
    return $problems
}

# --- main -------------------------------------------------------------------

if (-not (Test-Path -LiteralPath $LogPath)) {
    Write-SkillResult (New-SkillResult -Skill $SKILL -Ok $false -ErrorMessage "Path not found: $LogPath")
    exit 2
}

$entries = New-Object System.Collections.Generic.List[object]
$files = @(Get-LogFiles $LogPath)
foreach ($f in $files) {
    foreach ($e in (Read-W3CLog -File $f)) { $entries.Add($e) }
}

$metrics  = Get-IisMetrics -Entries $entries
$problems = @(Get-IisProblems -Entries $entries -Metrics $metrics)
$followUps = @($problems | ForEach-Object { $_.follow_ups } | Sort-Object -Unique)

$findings = @($problems | ForEach-Object {
    $evidence = @{ type = $_.type; follow_ups = $_.follow_ups }
    if ($_.ContainsKey('evidence') -and $_['evidence']) {
        foreach ($key in $_['evidence'].Keys) { $evidence[$key] = $_['evidence'][$key] }
    }
    New-SkillFinding -Summary $_.description -Severity $_.severity `
        -Evidence $evidence
})

$result = New-SkillResult -Skill $SKILL -Ok $true -Findings $findings `
    -Confidence $(if (@($problems).Count -gt 0) {'medium'} else {'low'}) `
    -Recommendations @(
        if (@($followUps).Count -gt 0) { "Run follow-up skills: $($followUps -join ', ')" }
        else                  { 'No problems detected; no further action needed.' }
    ) `
    -Raw @{
        log_files_parsed   = @($files).Count
        metrics            = $metrics
        problems           = @($problems)
        skills_to_trigger  = @($followUps)
        source_files       = @($files | ForEach-Object { $_.FullName })
        timestamp_basis    = 'W3C UTC'
    }

if ($AutoTrigger -and @($followUps).Count -gt 0) {
    $orch = Join-Path $root 'orchestrator\scripts\skill_orchestrator.ps1'
    $ctxJson = ($result.raw | ConvertTo-Json -Depth 10 -Compress)
    try {
        $pwsh = (Get-Process -Id $PID).Path
        $orchOut = & $pwsh -NoProfile -NonInteractive -File $orch `
            -Context $ctxJson -Runtime Auto 2>&1
        $result.raw['orchestrated'] = ($orchOut | Out-String | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        $result.raw['orchestrator_error'] = $_.Exception.Message
    }
}

Write-SkillResult $result
exit 0
