<#
.SYNOPSIS
  Native PowerShell fan-out orchestrator for an IIS analyzer payload.

.DESCRIPTION
  Consumes the standard IIS analyzer raw payload from -Context, @file, a JSON
  file, or stdin. Follow-up skills are launched concurrently and may run via
  PowerShell or Python according to -Runtime.

  Folder-first discovery and HTML reporting remain owned by
  skill_orchestrator.py.
#>
#requires -Version 7.0
[CmdletBinding()]
param(
    [string] $Context,
    [ValidateSet('Auto', 'Python', 'Pwsh')]
    [string] $Runtime = 'Auto',
    [ValidateRange(1, 3600)]
    [int] $PerSkillTimeout = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Import-Module (Join-Path $root '_shared\Contract.psm1') -Force

$SKILL = 'orchestrator'
$registry = Get-SkillRegistry

function Get-PropertyValue {
    param(
        [object] $Object,
        [Parameter(Mandatory)] [string] $Name
    )
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

function Resolve-ChildRuntime {
    param([Parameter(Mandatory)] [string] $SkillId)

    switch ($Runtime) {
        'Pwsh' {
            $path = Resolve-SkillEntry -SkillId $SkillId -Runtime pwsh
            return @{ runtime = 'pwsh'; path = $path }
        }
        'Python' {
            $path = Resolve-SkillEntry -SkillId $SkillId -Runtime python
            return @{ runtime = 'python'; path = $path }
        }
        default {
            try {
                $path = Resolve-SkillEntry -SkillId $SkillId -Runtime pwsh
                if (Test-Path -LiteralPath $path -PathType Leaf) {
                    return @{ runtime = 'pwsh'; path = $path }
                }
            } catch { }
            try {
                $path = Resolve-SkillEntry -SkillId $SkillId -Runtime python
                if (Test-Path -LiteralPath $path -PathType Leaf) {
                    return @{ runtime = 'python'; path = $path }
                }
            } catch { }
            throw "Skill $SkillId has no available runnable entry"
        }
    }
}

function Get-ProblemTypeForSkill {
    param(
        [Parameter(Mandatory)] [string] $SkillId,
        [object[]] $Problems
    )

    $skill = Get-PropertyValue -Object $registry.skills -Name $SkillId
    $triggers = if ($skill) { @($skill.triggers_for) } else { @() }
    foreach ($problem in $Problems) {
        if ($triggers -contains $problem['type']) { return $problem['type'] }
    }
    if ($Problems.Count -gt 0) { return $Problems[0]['type'] }
    return $null
}

function New-ChildContext {
    param(
        [Parameter(Mandatory)] [string] $SkillId,
        [Parameter(Mandatory)] [hashtable] $Payload,
        [object[]] $Problems
    )

    $extra = @{}
    if ($Payload['extra'] -is [hashtable]) {
        foreach ($key in $Payload['extra'].Keys) { $extra[$key] = $Payload['extra'][$key] }
    }

    $suspiciousIps = @(
        $Problems |
            Where-Object { $_['evidence'] -and $_['evidence']['client_ip'] } |
            ForEach-Object { $_['evidence']['client_ip'] } |
            Sort-Object -Unique
    )
    if ($suspiciousIps.Count -gt 0) { $extra['suspicious_ips'] = $suspiciousIps }

    return @{
        time_range  = if ($Payload['metrics']) { $Payload['metrics']['time_range'] } else { @{} }
        problem_type = Get-ProblemTypeForSkill -SkillId $SkillId -Problems $Problems
        metrics      = if ($Payload['metrics']) { $Payload['metrics'] } else { @{} }
        extra        = $extra
    }
}

function Start-ChildProcess {
    param(
        [Parameter(Mandatory)] [string] $SkillId,
        [Parameter(Mandatory)] [hashtable] $ResolvedEntry,
        [Parameter(Mandatory)] [string] $ContextJson
    )

    $contextArgument = $ContextJson
    $temporaryContext = $null
    if ($ContextJson.Length -gt 7000) {
        $temporaryContext = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($temporaryContext, $ContextJson, [System.Text.Encoding]::UTF8)
        $contextArgument = "@$temporaryContext"
    }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    if ($ResolvedEntry.runtime -eq 'pwsh') {
        $startInfo.FileName = (Get-Process -Id $PID).Path
        foreach ($argument in @(
            '-NoProfile', '-NonInteractive', '-File', $ResolvedEntry.path,
            '-Context', $contextArgument
        )) {
            [void] $startInfo.ArgumentList.Add($argument)
        }
    } else {
        $python = Get-Command python -CommandType Application -ErrorAction Stop |
            Select-Object -First 1
        $startInfo.FileName = $python.Source
        [void] $startInfo.ArgumentList.Add($ResolvedEntry.path)
        [void] $startInfo.ArgumentList.Add($contextArgument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Failed to start skill $SkillId" }

    return [pscustomobject]@{
        SkillId   = $SkillId
        Runtime   = $ResolvedEntry.runtime
        Process   = $process
        Stdout    = $process.StandardOutput.ReadToEndAsync()
        Stderr    = $process.StandardError.ReadToEndAsync()
        StartedAt = [datetime]::UtcNow
        TemporaryContext = $temporaryContext
    }
}

function Receive-ChildProcess {
    param([Parameter(Mandatory)] [object] $Job)

    $result = $null
    $elapsed = [datetime]::UtcNow - $Job.StartedAt
    $remainingMs = [Math]::Max(0, ($PerSkillTimeout * 1000) - [int] $elapsed.TotalMilliseconds)
    if (-not $Job.Process.WaitForExit($remainingMs)) {
        $Job.Process.Kill($true)
        $Job.Process.WaitForExit()
        $result = New-SkillOperationalFailure -Skill $Job.SkillId `
            -ErrorMessage "Timed out after $PerSkillTimeout seconds" `
            -Code 'timeout' `
            -Execution @{
                runtime = $Job.Runtime
                stage = 'execute'
                timeout_seconds = $PerSkillTimeout
            }
    } else {
        $stdout = $Job.Stdout.GetAwaiter().GetResult()
        $stderr = $Job.Stderr.GetAwaiter().GetResult()
        $result = ConvertTo-SkillChildResult -Skill $Job.SkillId `
            -Stdout $stdout -Stderr $stderr -ExitCode $Job.Process.ExitCode `
            -Runtime $Job.Runtime
    }
    if ($Job.TemporaryContext -and (Test-Path -LiteralPath $Job.TemporaryContext)) {
        Remove-Item -LiteralPath $Job.TemporaryContext -Force -ErrorAction SilentlyContinue
    }
    return $result
}

$payload = Read-SkillContext -Argument $Context
if ($payload['raw'] -is [hashtable] -and $payload['raw']['skills_to_trigger']) {
    $payload = $payload['raw']
}

$requestedFollowUps = @($payload['skills_to_trigger'] | Where-Object { $_ })
$validatorRequested = $requestedFollowUps -contains 'validator'
$followUps = @($requestedFollowUps | Where-Object { $_ -ne 'validator' })
$payloadExtra = if ($payload['extra'] -is [hashtable]) { $payload['extra'] } else { @{} }
$hasSourceRoots = @('repo_roots', 'config_roots', 'symbols_roots') |
    Where-Object { $payloadExtra[$_] } |
    Select-Object -First 1
if ($followUps.Count -eq 0 -and -not $hasSourceRoots -and -not $validatorRequested) {
    Write-SkillResult (New-SkillResult -Skill $SKILL -Ok $true `
        -Findings @((New-SkillFinding -Summary 'Nothing to orchestrate' -Severity 'info')) `
        -Recommendations @('No follow-up skills were requested.') `
        -Raw @{ executed = @(); results = @{}; runtimes = @{}; root_cause_chain = @() })
    exit 0
}

$problems = @($payload['problems'] | Where-Object { $_ })
$jobs = @()
$results = @{}
$runtimes = @{}

foreach ($skillId in $followUps) {
    try {
        $resolved = Resolve-ChildRuntime -SkillId $skillId
        $runtimes[$skillId] = $resolved.runtime
        $childContext = New-ChildContext -SkillId $skillId -Payload $payload -Problems $problems
        $contextJson = $childContext | ConvertTo-Json -Depth 12 -Compress
        $jobs += Start-ChildProcess -SkillId $skillId -ResolvedEntry $resolved -ContextJson $contextJson
    } catch {
        $results[$skillId] = New-SkillOperationalFailure -Skill $skillId `
            -ErrorMessage $_.Exception.Message -Code 'skill_resolution_failed' `
            -Execution @{ stage = 'resolve' }
    }
}

foreach ($job in $jobs) {
    $results[$job.SkillId] = Receive-ChildProcess -Job $job
}

$hasActionableRoot = @(
    $results.Values | Where-Object {
        $_['ok'] -and $_['root_cause'] -and $_['evidence_state'] -eq 'ROOT_CAUSE_FOUND'
    }
).Count -gt 0

if ($hasSourceRoots -and -not $hasActionableRoot) {
    try {
        $sourceId = 'source_logging_discovery'
        $resolved = @{
            runtime = 'python'
            path = Resolve-SkillEntry -SkillId $sourceId -Runtime python
        }
        $runtimes[$sourceId] = $resolved.runtime
        $sourceExtra = @{}
        foreach ($key in $payloadExtra.Keys) { $sourceExtra[$key] = $payloadExtra[$key] }
        $sourceExtra['product'] = 'IIS'
        $sourceContext = @{
            time_range = if ($payload['metrics']) { $payload['metrics']['time_range'] } else { @{} }
            problem_type = if ($problems.Count) { $problems[0]['type'] } else { $null }
            problems = $problems
            extra = $sourceExtra
        }
        $sourceJson = $sourceContext | ConvertTo-Json -Depth 20 -Compress
        $sourceJob = Start-ChildProcess -SkillId $sourceId -ResolvedEntry $resolved -ContextJson $sourceJson
        $results[$sourceId] = Receive-ChildProcess -Job $sourceJob
        $followUps += $sourceId
    } catch {
        $results['source_logging_discovery'] = New-SkillOperationalFailure `
            -Skill 'source_logging_discovery' -ErrorMessage $_.Exception.Message `
            -Code 'skill_resolution_failed' -Execution @{ stage = 'resolve' }
        $followUps += 'source_logging_discovery'
    }
}

$validationResult = $null
$problemTypes = @($problems | ForEach-Object { $_['type'] } | Where-Object { $_ })
$eligibleTypes = @($registry.cv_config.eligible_problem_types)
$validationEligible = @($problemTypes | Where-Object { $_ -in $eligibleTypes }).Count -gt 0
if ($registry.cv_config.enabled -and ($validatorRequested -or $validationEligible) -and $results.Count) {
    try {
        $resolved = @{
            runtime = 'python'
            path = Resolve-SkillEntry -SkillId 'validator' -Runtime python
        }
        $validationExtra = @{
            product = 'IIS'
            children = $results
            model_analyses = if ($payloadExtra['model_analyses']) { $payloadExtra['model_analyses'] } else { @{} }
            incident_mode = if ($payloadExtra['incident_mode']) { $payloadExtra['incident_mode'] } else { 'offline_bundle' }
            minimum_independent_skills = if ($registry.cv_config.minimum_independent_skills) {
                $registry.cv_config.minimum_independent_skills
            } else { 2 }
        }
        $validationContext = @{
            time_range = if ($payload['metrics']) { $payload['metrics']['time_range'] } else { @{} }
            problem_type = if ($problemTypes.Count) { $problemTypes[0] } else { $null }
            problems = $problems
            extra = $validationExtra
        }
        $validationJson = $validationContext | ConvertTo-Json -Depth 30 -Compress
        $validationJob = Start-ChildProcess -SkillId 'validator' -ResolvedEntry $resolved -ContextJson $validationJson
        $validationResult = Receive-ChildProcess -Job $validationJob
    } catch {
        $validationResult = New-SkillOperationalFailure -Skill 'validator' `
            -ErrorMessage $_.Exception.Message -Code 'skill_resolution_failed' `
            -Execution @{ stage = 'resolve' }
    }
}

$findings = @()
$rootCauseChain = @()
$solutions = @()
$nextSteps = @()
$additionalLogs = @()
$evidenceGaps = @()
$collectionPlan = $null
$successes = @()
$failures = @()

foreach ($skillId in $followUps) {
    $result = $results[$skillId]
    if (-not $result -or -not $result['ok']) {
        $failures += $skillId
        continue
    }

    $successes += $skillId
    foreach ($finding in @($result['findings'])) {
        if (-not $finding) { continue }
        $findings += New-SkillFinding `
            -Summary "[$skillId] $($finding['summary'])" `
            -Severity ($finding['severity'] ?? 'info') `
            -Evidence ($finding['evidence'] ?? @{})
    }
    if ($result['root_cause'] -and $result['evidence_state'] -eq 'ROOT_CAUSE_FOUND') {
        $rootCauseChain += @{ skill = $skillId; finding = $result['root_cause'] }
    }
    $solutions += @($result['solutions'] | Where-Object { $_ })
    $nextSteps += @($result['next_steps'] | Where-Object { $_ })
    $additionalLogs += @($result['additional_logs_needed'] | Where-Object { $_ })
    $evidenceGaps += @($result['evidence_gaps'] | Where-Object { $_ })
    if (-not $collectionPlan -and $result['collection_plan']) {
        $collectionPlan = $result['collection_plan']
    }
}

if ($validationResult) {
    foreach ($finding in @($validationResult['findings'])) {
        if (-not $finding) { continue }
        $findings += New-SkillFinding `
            -Summary "[validator] $($finding['summary'])" `
            -Severity ($finding['severity'] ?? 'info') `
            -Evidence ($finding['evidence'] ?? @{})
    }
    if ($validationResult['root_cause'] -and $validationResult['evidence_state'] -eq 'ROOT_CAUSE_FOUND') {
        $rootCauseChain = @(@{
            skill = 'validator'; finding = $validationResult['root_cause']
        }) + $rootCauseChain
    }
    $solutions += @($validationResult['solutions'] | Where-Object { $_ })
    $nextSteps += @($validationResult['next_steps'] | Where-Object { $_ })
    $additionalLogs += @($validationResult['additional_logs_needed'] | Where-Object { $_ })
    $evidenceGaps += @($validationResult['evidence_gaps'] | Where-Object { $_ })
    if (-not $collectionPlan -and $validationResult['collection_plan']) {
        $collectionPlan = $validationResult['collection_plan']
    }
    if (-not $validationResult['ok']) {
        $evidenceGaps += @{
            code = 'validator_failure'
            summary = "Final validator failed: $($validationResult['error'] ?? 'unknown error')"
            severity = 'warning'
            blocking = $true
            log_kind = $null
            skill = 'validator'
            details = @{}
        }
    }
}

foreach ($failedSkill in $failures) {
    $evidenceGaps += @{
        code = 'skill_failure'
        summary = "$failedSkill failed: $($results[$failedSkill]['error'])"
        severity = 'warning'
        blocking = $true
        log_kind = $null
        skill = $failedSkill
        details = @{}
    }
}

$provenResults = @(
    @($results.Values) + @($validationResult) |
        Where-Object {
            $_ -and $_['ok'] -and $_['root_cause'] -and
            $_['evidence_state'] -eq 'ROOT_CAUSE_FOUND'
        }
)
$provenQualities = @($provenResults | ForEach-Object { $_['raw']['quality'] })
$directJoin = @($provenQualities | Where-Object { $_['direct_join'] }).Count -gt 0
$declaredIndependent = @(
    $provenQualities | ForEach-Object { [int] ($_['independent_sources'] ?? 0) }
)
$independentSources = [Math]::Max(
    $provenResults.Count,
    $(if ($declaredIndependent.Count) { ($declaredIndependent | Measure-Object -Maximum).Maximum } else { 0 })
)
$sampleSufficient = $provenResults.Count -gt 0 -and @(
    $provenQualities | Where-Object { $_['sample_sufficient'] -ne $true }
).Count -eq 0
$captureComplete = $provenResults.Count -gt 0 -and @(
    $provenQualities | Where-Object { $_['capture_complete'] -ne $true }
).Count -eq 0
$timeComparable = $provenResults.Count -gt 0 -and @(
    $provenQualities | Where-Object { $_['time_comparable'] -ne $true }
).Count -eq 0
$blockingGaps = @($evidenceGaps | Where-Object { $_['blocking'] }).Count -gt 0 -or @(
    $provenQualities | Where-Object { $_['blocking_gaps'] -ne $false }
).Count -gt 0

$confidence = if ($provenResults.Count -and @(
    $provenResults | Where-Object { $_['confidence'] -ne 'high' }
).Count -eq 0) { 'high' } elseif ($provenResults.Count) { 'medium' } else { 'low' }
$validationStatus = if ($validationResult -and $validationResult['cross_validation']) {
    $validationResult['cross_validation']['status']
} else { $null }
if ($validationStatus -eq 'conflict') { $confidence = 'low' }
elseif ($validationResult -and $validationResult['root_cause'] -and `
        $validationResult['evidence_state'] -eq 'ROOT_CAUSE_FOUND') {
    $confidence = $validationResult['confidence']
}
$recommendations = @("Run rate: $($successes.Count)/$($followUps.Count) skills succeeded.")
if ($failures.Count -gt 0) {
    $recommendations += "Re-run failed skills: $($failures -join ', ')"
}

$rootCause = @($rootCauseChain | ForEach-Object { $_.finding }) -join ' -> '
if (-not $rootCause) { $confidence = 'low' }
$evidenceState = if (-not $rootCause -and $collectionPlan -and @($collectionPlan['requests']).Count) {
    'COLLECTION_PLAN_READY'
} elseif ($validationStatus -eq 'conflict' -or $failures.Count -or
          ($validationResult -and -not $validationResult['ok'])) {
    'INCONCLUSIVE_EVIDENCE'
} elseif ($additionalLogs.Count) {
    'MISSING_REQUIRED_EVIDENCE'
} elseif ($rootCause -and $confidence -in @('medium','high') -and
          ($directJoin -or $independentSources -ge 2) -and
          $sampleSufficient -and $captureComplete -and $timeComparable -and
          -not $blockingGaps) {
    'ROOT_CAUSE_FOUND'
} else {
    'INCONCLUSIVE_EVIDENCE'
}
if ($evidenceState -ne 'ROOT_CAUSE_FOUND') {
    $rootCause = $null
    $confidence = 'low'
}
$rootQuality = @{
    proven_results = $provenResults.Count
    direct_join = $directJoin
    independent_sources = $independentSources
    sample_sufficient = $sampleSufficient
    capture_complete = $captureComplete
    time_comparable = $timeComparable
    blocking_gaps = $blockingGaps
}
$orchestratorResult = New-SkillResult -Skill $SKILL `
    -Ok ($failures.Count -eq 0 -and (-not $validationResult -or $validationResult['ok'])) `
    -Findings $findings `
    -RootCause $rootCause `
    -Confidence $confidence `
    -Recommendations $recommendations `
    -Solutions $solutions `
    -NextSteps $nextSteps `
    -AdditionalLogsNeeded $additionalLogs `
    -CrossValidation $(if ($validationResult) { $validationResult['cross_validation'] } else { $null }) `
    -EvidenceState $evidenceState `
    -EvidenceGaps $evidenceGaps `
    -CollectionPlan $collectionPlan `
    -Raw @{
        executed = $followUps
        results = $results
        runtimes = $runtimes
        validation = $validationResult
        root_cause_chain = $rootCauseChain
        quality = $rootQuality
    }

Write-SkillResult $orchestratorResult
exit $(if ($orchestratorResult.ok) { 0 } else { 1 })