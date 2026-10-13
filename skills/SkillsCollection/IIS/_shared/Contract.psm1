<#
.SYNOPSIS
  Shared PowerShell contract for IIS skill collection (mirrors _shared/contract.py).

.DESCRIPTION
  Exposes:
    - Get-SkillRegistry          : load _shared/registry.json
    - Resolve-SkillEntry         : absolute path of a skill's entry script
    - Read-SkillContext          : parse arg as JSON string, @file, or stdin/{}
    - New-SkillResult            : build a contract envelope
    - Write-SkillResult          : emit envelope as JSON to stdout
    - ConvertTo-SkillTime        : ISO-8601 -> [datetime]
    - Get-SkillTimeRange         : (start, end) from context.time_range
    - Test-InTimeWindow          : window membership with tolerance
    - New-SkillFinding           : build a finding hashtable

  Python 3.10+/PowerShell 5.1+ compatible. No external modules.
#>

$script:RootPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Get-SkillRegistry {
    [CmdletBinding()] param()
    $registryFile = Join-Path $script:RootPath '_shared/registry.json'
    Get-Content -LiteralPath $registryFile -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-ObjectPropertyInternal {
    param([object] $Object, [Parameter(Mandatory)] [string] $Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

function Resolve-RegistryEntryInternal {
    param(
        [Parameter(Mandatory)] [object] $Registry,
        [Parameter(Mandatory)] [string] $RegistryFile,
        [Parameter(Mandatory)] [string] $SkillId,
        [Parameter(Mandatory)] [string] $Runtime
    )
    $skills = Get-ObjectPropertyInternal -Object $Registry -Name 'skills'
    $skill = Get-ObjectPropertyInternal -Object $skills -Name $SkillId
    if (-not $skill) { return $null }
    $entry = Get-ObjectPropertyInternal -Object $skill -Name 'entry'
    $relative = Get-ObjectPropertyInternal -Object $entry -Name $Runtime
    if (-not $relative) { throw "Skill $SkillId has no $Runtime entry" }

    $registryDirectory = Split-Path -Parent $RegistryFile
    $skillRoot = if ((Split-Path -Leaf $registryDirectory) -eq '_shared') {
        Split-Path -Parent $registryDirectory
    } else {
        $registryDirectory
    }
    return [System.IO.Path]::GetFullPath((Join-Path $skillRoot $relative))
}

function Resolve-SkillEntry {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $SkillId,
        [ValidateSet('python','pwsh')] [string] $Runtime = 'pwsh'
    )
    $registryFile = Join-Path $script:RootPath '_shared/registry.json'
    $registry = Get-SkillRegistry
    $local = Resolve-RegistryEntryInternal -Registry $registry -RegistryFile $registryFile `
                                    -SkillId $SkillId -Runtime $Runtime
    if ($local) { return $local }

    $lookupId = $SkillId
    $overrides = Get-ObjectPropertyInternal -Object $registry -Name 'skill_overrides'
    $override = Get-ObjectPropertyInternal -Object $overrides -Name $SkillId
    if ($override) { $lookupId = [string] $override }

    foreach ($relativeRegistry in @($registry.external_registries)) {
        if (-not $relativeRegistry) { continue }
        $externalFile = [System.IO.Path]::GetFullPath(
            (Join-Path $script:RootPath ([string] $relativeRegistry))
        )
        if (-not (Test-Path -LiteralPath $externalFile -PathType Leaf)) { continue }
        $external = Get-Content -LiteralPath $externalFile -Raw -Encoding UTF8 |
                    ConvertFrom-Json
        $resolved = Resolve-RegistryEntryInternal -Registry $external `
            -RegistryFile $externalFile -SkillId $lookupId -Runtime $Runtime
        if ($resolved) { return $resolved }
    }
    throw "Unknown skill: $SkillId"
}

function Read-SkillContext {
    [CmdletBinding()]
    param([string] $Argument)
    if ([string]::IsNullOrWhiteSpace($Argument)) {
        # Try stdin if it's piped
        if (-not [Console]::IsInputRedirected) { return @{} }
        $raw = [Console]::In.ReadToEnd()
        if ([string]::IsNullOrWhiteSpace($raw)) { return @{} }
        return ($raw | ConvertFrom-Json -AsHashtable -ErrorAction Stop)
    }
    if ($Argument.StartsWith('@')) {
        $p = $Argument.Substring(1)
        return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 |
                ConvertFrom-Json -AsHashtable -ErrorAction Stop)
    }
    try {
        return ($Argument | ConvertFrom-Json -AsHashtable -ErrorAction Stop)
    } catch {
        if (Test-Path -LiteralPath $Argument) {
            return (Get-Content -LiteralPath $Argument -Raw -Encoding UTF8 |
                    ConvertFrom-Json -AsHashtable -ErrorAction Stop)
        }
        throw
    }
}

function New-SkillFinding {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Summary,
        [ValidateSet('critical','warning','info')] [string] $Severity = 'info',
        [hashtable] $Evidence = @{}
    )
    [ordered]@{
        summary  = $Summary
        severity = $Severity
        evidence = $Evidence
    }
}

function New-SkillSolution {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Title,
        [string[]] $Steps = @(),
        [string] $ProblemRef = $null,
        [ValidateSet('critical','warning','info')] [string] $Severity = 'info',
        [string[]] $References = @()
    )
    [ordered]@{
        title       = $Title
        steps       = @($Steps)
        problem_ref = $ProblemRef
        severity    = $Severity
        references  = @($References)
    }
}

function New-SkillNextStep {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Action,
        [string] $Why = $null,
        [string] $Skill = $null
    )
    [ordered]@{
        action = $Action
        why    = $Why
        skill  = $Skill
    }
}

function New-SkillLogRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $LogKind,
        [Parameter(Mandatory)] [string] $Why,
        [string] $HowToCollect = $null,
        [string] $Skill = $null
    )
    [ordered]@{
        log_kind        = $LogKind
        why             = $Why
        how_to_collect  = $HowToCollect
        skill           = $Skill
    }
}

function Get-FlattenedRecommendationsInternal {
    param($Solutions, $NextSteps, $LogReqs)
    $out = @()
    foreach ($s in @($Solutions)) {
        if (-not $s) { continue }
        $tag  = if ($s.problem_ref) { "[fix:$($s.problem_ref)]" } else { '[fix]' }
        $head = "$tag $($s.title)"
        if ($s.steps -and $s.steps.Count -gt 0) { $head += " — $($s.steps[0])" }
        $out += $head
    }
    foreach ($n in @($NextSteps)) {
        if (-not $n) { continue }
        $tag = if ($n.skill) { "[next:$($n.skill)]" } else { '[next]' }
        $out += "$tag $($n.action)"
    }
    foreach ($l in @($LogReqs)) {
        if (-not $l) { continue }
        $out += "[logs:$($l.log_kind)] $($l.why)"
    }
    return $out
}

function New-SkillResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Skill,
        [bool]   $Ok = $true,
        [object[]] $Findings = @(),
        [string] $RootCause = $null,
        [ValidateSet('high','medium','low')] [string] $Confidence = 'low',
        [string[]] $Recommendations = @(),
        [object[]] $Solutions = @(),
        [object[]] $NextSteps = @(),
        [object[]] $AdditionalLogsNeeded = @(),
        [object] $CrossValidation = $null,
        [ValidateSet(
            'ROOT_CAUSE_FOUND', 'INCONCLUSIVE_EVIDENCE',
            'MISSING_REQUIRED_EVIDENCE', 'TIME_NOT_COMPARABLE',
            'PERMISSION_BLOCKED', 'TOOL_UNAVAILABLE', 'UNKNOWN_LOG_TYPE',
            'LIVE_COLLECTION_REQUIRED', 'COLLECTION_PLAN_READY'
        )]
        [string] $EvidenceState = 'INCONCLUSIVE_EVIDENCE',
        [object[]] $EvidenceGaps = @(),
        [object] $CollectionPlan = $null,
        [hashtable] $Raw = @{},
        [string] $ErrorMessage = $null
    )
    if ((-not $Recommendations -or $Recommendations.Count -eq 0) -and `
        (($Solutions -and $Solutions.Count -gt 0) -or `
         ($NextSteps -and $NextSteps.Count -gt 0) -or `
         ($AdditionalLogsNeeded -and $AdditionalLogsNeeded.Count -gt 0))) {
        $Recommendations = Get-FlattenedRecommendationsInternal -Solutions $Solutions `
                                                          -NextSteps $NextSteps `
                                                          -LogReqs $AdditionalLogsNeeded
    }
    if ($EvidenceState -eq 'INCONCLUSIVE_EVIDENCE') {
        if ($CollectionPlan -and $CollectionPlan.requests) {
            $EvidenceState = 'COLLECTION_PLAN_READY'
        } elseif ($AdditionalLogsNeeded -and $AdditionalLogsNeeded.Count -gt 0) {
            $EvidenceState = 'MISSING_REQUIRED_EVIDENCE'
        }
    }
    $NormalizedRootCause = if ([string]::IsNullOrWhiteSpace($RootCause)) { $null } else { $RootCause }
    if ($NormalizedRootCause -and $EvidenceState -eq 'ROOT_CAUSE_FOUND') {
        $quality = $Raw['quality']
        $independentSources = if ($quality -is [System.Collections.IDictionary]) {
            [int] ($quality['independent_sources'] ?? 0)
        } else { 0 }
        $proofValid = (
            $Confidence -in @('medium','high') -and
            $quality -is [System.Collections.IDictionary] -and
            ($quality['direct_join'] -or $independentSources -ge 2) -and
            $quality['sample_sufficient'] -eq $true -and
            $quality['capture_complete'] -eq $true -and
            $quality['time_comparable'] -eq $true -and
            $quality['blocking_gaps'] -eq $false
        )
        if ($proofValid) {
            $Raw['root_quality_gate'] = @{ promoted = $true; issues = @() }
        } else {
            $Raw['candidate_cause'] = $NormalizedRootCause
            $Raw['root_quality_gate'] = @{
                promoted = $false
                issues = @(@{ path = 'raw.quality'; message = 'does not prove ROOT_CAUSE_FOUND' })
            }
            $NormalizedRootCause = $null
            $Confidence = 'low'
            $EvidenceState = if ($CollectionPlan -and $CollectionPlan.requests) {
                'COLLECTION_PLAN_READY'
            } else {
                'INCONCLUSIVE_EVIDENCE'
            }
        }
    } elseif ($NormalizedRootCause) {
        if (-not $Raw.ContainsKey('candidate_cause')) {
            $Raw['candidate_cause'] = $NormalizedRootCause
        }
        $NormalizedRootCause = $null
        $Confidence = 'low'
    }
    [ordered]@{
        skill                  = $Skill
        ok                     = $Ok
        findings               = @($Findings)
        root_cause             = $NormalizedRootCause
        confidence             = $Confidence
        recommendations        = @($Recommendations)
        solutions              = @($Solutions)
        next_steps             = @($NextSteps)
        additional_logs_needed = @($AdditionalLogsNeeded)
        cross_validation       = $CrossValidation
        evidence_state         = $EvidenceState
        evidence_gaps          = @($EvidenceGaps)
        collection_plan        = $CollectionPlan
        raw                    = $Raw
        error            = $ErrorMessage
        generated_at     = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Get-SkillRedactionHashInternal {
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Value,
        [ValidateRange(8, 64)] [int] $Length = 16
    )
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $hex = -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
        return $hex.Substring(0, $Length)
    } finally {
        $sha.Dispose()
    }
}

function Protect-SkillStringInternal {
    param(
        [AllowEmptyString()] [string] $Value,
        [AllowEmptyString()] [string] $Key
    )
    $name = $Key.Trim().ToLowerInvariant().Replace(' ', '_')
    if ($name -match '(password|passwd|pwd|authorization|cookie|set-cookie|client_secret|access_token|refresh_token|api_key|private_key|sas_token|credential)') {
        return '<redacted-secret>'
    }
    if ($name -in @('username','user_name','cs-username','account','account_name','email','client_ip','c-ip','src_ip','source_ip','user_sid')) {
        return "<identity:$(Get-SkillRedactionHashInternal $Value)>"
    }
    if ($name -in @('message','properties','snippet','stack_trace','commandline','command_line','excerpt')) {
        return "<$($name):$(Get-SkillRedactionHashInternal $Value)>"
    }
    if ($name -in @('cs-uri-query','uri_query','query_string') -or
        ($name -eq 'query' -and ($Value.Contains('=') -or $Value.Contains('&')))) {
        if (-not $Value) { return $Value }
        return "<query:$(Get-SkillRedactionHashInternal $Value)>"
    }

    $isPathKey = $name -in @(
        'path','paths','source_path','source_file','source_files','fullpath',
        'evtx_path','evtx_paths','user_data_dir','reports_dir','preferences_file',
        'exe','executable','sample_files','repo_roots','config_roots','symbols_roots'
    )
    $looksLikeWindowsPath = $Value -match '^(?i:[a-z]:[\\/]|\\\\)'
    if (($isPathKey -and $looksLikeWindowsPath) -or $looksLikeWindowsPath) {
        $trimmed = $Value.TrimEnd([char[]]@('\','/'))
        $basename = [System.IO.Path]::GetFileName($trimmed)
        if (-not $basename) { $basename = 'unknown' }
        return "<path:$(Get-SkillRedactionHashInternal $Value)>/$basename"
    }

    if ($Value.StartsWith('http://', [System.StringComparison]::OrdinalIgnoreCase) -or
        $Value.StartsWith('https://', [System.StringComparison]::OrdinalIgnoreCase)) {
        try {
            $builder = [System.UriBuilder]::new($Value)
            $builder.UserName = ''
            $builder.Password = ''
            $builder.Query = ''
            $builder.Fragment = ''
            return $builder.Uri.GetLeftPart([System.UriPartial]::Path)
        } catch {
            return '<redacted-url>'
        }
    }
    $pathEvaluator = {
        param([System.Text.RegularExpressions.Match] $Match)
        $raw = $Match.Value
        $path = $raw.TrimEnd([char[]]@('.',',',';',':',')',']','}'))
        $suffix = $raw.Substring($path.Length)
        $parts = @($path -split '[\\/]')
        $basename = if ($parts.Count) { $parts[-1] } else { 'unknown' }
        return "<path:$(Get-SkillRedactionHashInternal -Value $path)>/$basename$suffix"
    }
    $Value = [regex]::Replace(
        $Value,
        '(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s<>"]+',
        [System.Text.RegularExpressions.MatchEvaluator] $pathEvaluator
    )
    $Value = [regex]::Replace(
        $Value,
        '(?<![:/\w])/(?:[^/\s<>"]+/)*[^/\s<>"]+',
        [System.Text.RegularExpressions.MatchEvaluator] $pathEvaluator
    )
    $Value = [regex]::Replace($Value, '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer <redacted>')
    return [regex]::Replace(
        $Value,
        '(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+',
        '$1=<redacted>'
    )
}

function Protect-SkillValueInternal {
    param(
        [AllowNull()] [object] $Value,
        [AllowEmptyString()] [string] $Key = ''
    )
    if ($null -eq $Value) { return $null }
    if ($Value -is [string]) {
        return Protect-SkillStringInternal -Value $Value -Key $Key
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $output = [ordered]@{}
        foreach ($childKey in $Value.Keys) {
            $output[[string] $childKey] = Protect-SkillValueInternal `
                -Value $Value[$childKey] -Key ([string] $childKey)
        }
        return $output
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $output = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            $output[$property.Name] = Protect-SkillValueInternal `
                -Value $property.Value -Key $property.Name
        }
        return $output
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        return ,@($Value | ForEach-Object {
            Protect-SkillValueInternal -Value $_ -Key $Key
        })
    }
    return $Value
}

function Add-SkillFindingProvenanceInternal {
    param([Parameter(Mandatory)] [System.Collections.IDictionary] $Result)
    $skill = [string] $Result['skill']
    foreach ($finding in @($Result['findings'])) {
        if ($finding -isnot [System.Collections.IDictionary]) { continue }
        $evidence = $finding['evidence']
        if ($evidence -isnot [System.Collections.IDictionary]) { $evidence = @{} }
        if (-not $finding['evidence_id']) {
            $identity = [ordered]@{
                skill = $skill
                summary = $finding['summary']
                severity = $finding['severity']
                evidence = $evidence
            } | ConvertTo-Json -Depth 12 -Compress
            $finding['evidence_id'] = "EV-$(Get-SkillRedactionHashInternal -Value $identity -Length 20)"
        }

        $source = $null
        foreach ($key in @('source_file','source_path','file','path','source','log')) {
            if ($evidence[$key]) {
                $source = [ordered]@{ field = $key; value = $evidence[$key] }
                break
            }
        }
        $record = [ordered]@{}
        foreach ($key in @('record_locator','line_number','lineno','record_id','event_id','source_id','pid','request_id')) {
            if ($null -ne $evidence[$key] -and [string] $evidence[$key] -ne '') {
                $record[$key] = $evidence[$key]
            }
        }
        $observedTime = $null
        foreach ($key in @('normalized_time_utc','timestamp','time','original_time')) {
            if ($evidence[$key]) {
                $observedTime = [ordered]@{ field = $key; value = $evidence[$key] }
                break
            }
        }
        $quality = if ($source -and $record.Count) {
            'located'
        } elseif ($source -or $record.Count) {
            'partial'
        } else {
            'unlocated'
        }
        $finding['provenance'] = [ordered]@{
            quality = $quality
            source = $source
            record = $record
            time = $observedTime
        }
    }
}

function New-SkillOperationalFailure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Skill,
        [Parameter(Mandatory)] [string] $ErrorMessage,
        [Parameter(Mandatory)] [string] $Code,
        [ValidateSet(
            'ROOT_CAUSE_FOUND', 'INCONCLUSIVE_EVIDENCE',
            'MISSING_REQUIRED_EVIDENCE', 'TIME_NOT_COMPARABLE',
            'PERMISSION_BLOCKED', 'TOOL_UNAVAILABLE', 'UNKNOWN_LOG_TYPE',
            'LIVE_COLLECTION_REQUIRED', 'COLLECTION_PLAN_READY'
        )]
        [string] $EvidenceState = 'TOOL_UNAVAILABLE',
        [hashtable] $Execution = @{},
        [object[]] $ContractIssues = @(),
        [string] $InvalidOutput = $null
    )
    $raw = [ordered]@{
        failure = [ordered]@{ code = $Code }
        contract_validation = [ordered]@{
            valid = ($ContractIssues.Count -eq 0)
            interpreted_version = '2.4'
            warnings = @()
            issues = @($ContractIssues)
        }
        execution = $Execution
    }
    if ($null -ne $InvalidOutput) {
        $raw['invalid_output'] = [ordered]@{
            character_count = $InvalidOutput.Length
            sha256 = Get-SkillRedactionHashInternal -Value $InvalidOutput -Length 64
        }
    }
    return [ordered]@{
        schema_version = '2.4'
        skill = $Skill
        ok = $false
        findings = @()
        root_cause = $null
        confidence = 'low'
        recommendations = @()
        solutions = @()
        next_steps = @()
        additional_logs_needed = @()
        cross_validation = $null
        evidence_state = $EvidenceState
        evidence_gaps = @()
        collection_plan = $null
        raw = $raw
        error = $ErrorMessage
        generated_at = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function ConvertTo-SkillChildResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Skill,
        [AllowEmptyString()] [string] $Stdout,
        [AllowEmptyString()] [string] $Stderr,
        [Parameter(Mandatory)] [int] $ExitCode,
        [Parameter(Mandatory)] [string] $Runtime
    )
    try {
        $result = $Stdout | ConvertFrom-Json -AsHashtable -ErrorAction Stop
    } catch {
        return New-SkillOperationalFailure -Skill $Skill `
            -ErrorMessage 'Child result failed contract validation' `
            -Code 'contract_validation_failed' `
            -EvidenceState 'INCONCLUSIVE_EVIDENCE' `
            -Execution @{ runtime = $Runtime; stage = 'parse'; exit_code = $ExitCode } `
            -ContractIssues @(@{ path = '$'; message = 'invalid JSON or non-object output' }) `
            -InvalidOutput $Stdout
    }

    $issues = @()
    if ($result -isnot [System.Collections.IDictionary]) {
        $issues += @{ path = '$'; message = 'must be an object' }
    } else {
        if ([string] $result['skill'] -ne $Skill) {
            $issues += @{ path = 'skill'; message = "must equal '$Skill'" }
        }
        if ($result['ok'] -isnot [bool]) {
            $issues += @{ path = 'ok'; message = 'must be a boolean' }
        }
        if ($result['schema_version'] -and $result['schema_version'] -notin @('2.3','2.4')) {
            $issues += @{ path = 'schema_version'; message = 'contains an unsupported version' }
        }
        if ($result['schema_version'] -eq '2.4') {
            foreach ($field in @(
                'schema_version','skill','ok','findings','root_cause','confidence',
                'recommendations','solutions','next_steps','additional_logs_needed',
                'cross_validation','evidence_state','evidence_gaps','collection_plan',
                'raw','error'
            )) {
                if (-not $result.Contains($field)) {
                    $issues += @{ path = $field; message = 'is required by schema version 2.4' }
                }
            }
        }
        if ($result['confidence'] -and $result['confidence'] -notin @('high','medium','low')) {
            $issues += @{ path = 'confidence'; message = 'contains an unknown value' }
        }
        $states = @(
            'ROOT_CAUSE_FOUND','INCONCLUSIVE_EVIDENCE','MISSING_REQUIRED_EVIDENCE',
            'TIME_NOT_COMPARABLE','PERMISSION_BLOCKED','TOOL_UNAVAILABLE',
            'UNKNOWN_LOG_TYPE','LIVE_COLLECTION_REQUIRED','COLLECTION_PLAN_READY'
        )
        if ($result['evidence_state'] -and $result['evidence_state'] -notin $states) {
            $issues += @{ path = 'evidence_state'; message = 'contains an unknown state' }
        }
        if ($result.Contains('raw') -and $null -ne $result['raw'] -and
            $result['raw'] -isnot [System.Collections.IDictionary]) {
            $issues += @{ path = 'raw'; message = 'must be an object' }
        }
        foreach ($field in @(
            'findings','recommendations','solutions','next_steps',
            'additional_logs_needed','evidence_gaps'
        )) {
            if ($result.Contains($field) -and $result[$field] -isnot [System.Array]) {
                $issues += @{ path = $field; message = 'must be an array' }
            }
        }
        if ($result['findings'] -is [System.Array]) {
            for ($index = 0; $index -lt $result['findings'].Count; $index++) {
                $finding = $result['findings'][$index]
                if ($finding -isnot [System.Collections.IDictionary]) {
                    $issues += @{ path = "findings[$index]"; message = 'must be an object' }
                    continue
                }
                if ($finding['summary'] -isnot [string]) {
                    $issues += @{ path = "findings[$index].summary"; message = 'must be a string' }
                }
                if ($finding['severity'] -notin @('critical','warning','info')) {
                    $issues += @{ path = "findings[$index].severity"; message = 'contains an unknown value' }
                }
                if ($finding.Contains('evidence') -and
                    $finding['evidence'] -isnot [System.Collections.IDictionary]) {
                    $issues += @{ path = "findings[$index].evidence"; message = 'must be an object' }
                }
                if ($finding.Contains('evidence_id') -and $finding['evidence_id'] -isnot [string]) {
                    $issues += @{ path = "findings[$index].evidence_id"; message = 'must be a string' }
                }
                if ($finding.Contains('provenance') -and
                    $finding['provenance'] -isnot [System.Collections.IDictionary]) {
                    $issues += @{ path = "findings[$index].provenance"; message = 'must be an object' }
                }
            }
        }
        if ($result['recommendations'] -is [System.Array]) {
            for ($index = 0; $index -lt $result['recommendations'].Count; $index++) {
                if ($result['recommendations'][$index] -isnot [string]) {
                    $issues += @{ path = "recommendations[$index]"; message = 'must be a string' }
                }
            }
        }
        $objectArrays = @{
            solutions = @('title')
            next_steps = @('action')
            additional_logs_needed = @('log_kind','why')
            evidence_gaps = @('code','summary','severity','blocking')
        }
        foreach ($field in $objectArrays.Keys) {
            if ($result[$field] -isnot [System.Array]) { continue }
            for ($index = 0; $index -lt $result[$field].Count; $index++) {
                $item = $result[$field][$index]
                if ($item -isnot [System.Collections.IDictionary]) {
                    $issues += @{ path = "$field[$index]"; message = 'must be an object' }
                    continue
                }
                foreach ($required in $objectArrays[$field]) {
                    if (-not $item.Contains($required)) {
                        $issues += @{ path = "$field[$index].$required"; message = 'is required' }
                    }
                }
                if ($field -eq 'evidence_gaps') {
                    if ($item['severity'] -notin @('critical','warning','info')) {
                        $issues += @{ path = "$field[$index].severity"; message = 'contains an unknown value' }
                    }
                    if ($item['blocking'] -isnot [bool]) {
                        $issues += @{ path = "$field[$index].blocking"; message = 'must be a boolean' }
                    }
                }
            }
        }
        foreach ($field in @('cross_validation','collection_plan')) {
            if ($result.Contains($field) -and $null -ne $result[$field] -and
                $result[$field] -isnot [System.Collections.IDictionary]) {
                $issues += @{ path = $field; message = 'must be an object or null' }
            }
        }
        foreach ($field in @('root_cause','error')) {
            if ($result.Contains($field) -and $null -ne $result[$field] -and
                $result[$field] -isnot [string]) {
                $issues += @{ path = $field; message = 'must be a string or null' }
            }
        }
        if ($result.Contains('generated_at') -and $null -ne $result['generated_at'] -and
            $result['generated_at'] -isnot [string] -and
            $result['generated_at'] -isnot [datetime] -and
            $result['generated_at'] -isnot [datetimeoffset]) {
            $issues += @{ path = 'generated_at'; message = 'must be a date-time string' }
        }
        if ($result['evidence_state'] -eq 'ROOT_CAUSE_FOUND') {
            $quality = if ($result['raw'] -is [System.Collections.IDictionary]) {
                $result['raw']['quality']
            } else { $null }
            $independentSources = if ($quality -is [System.Collections.IDictionary]) {
                [int] ($quality['independent_sources'] ?? 0)
            } else { 0 }
            if (-not $result['root_cause']) {
                $issues += @{ path = 'root_cause'; message = 'is required for ROOT_CAUSE_FOUND' }
            }
            if ($quality -isnot [System.Collections.IDictionary] -or
                (-not $quality['direct_join'] -and $independentSources -lt 2) -or
                -not $quality['sample_sufficient'] -or
                -not $quality['capture_complete'] -or
                -not $quality['time_comparable'] -or
                $quality['blocking_gaps'] -ne $false) {
                $issues += @{ path = 'raw.quality'; message = 'does not prove ROOT_CAUSE_FOUND' }
            }
        }
    }
    if ($issues.Count) {
        return New-SkillOperationalFailure -Skill $Skill `
            -ErrorMessage 'Child result failed contract validation' `
            -Code 'contract_validation_failed' `
            -EvidenceState 'INCONCLUSIVE_EVIDENCE' `
            -Execution @{ runtime = $Runtime; stage = 'validate'; exit_code = $ExitCode } `
            -ContractIssues $issues `
            -InvalidOutput $Stdout
    }
    if ($ExitCode -ne 0 -and $result['ok']) {
        return New-SkillOperationalFailure -Skill $Skill `
            -ErrorMessage "Child exited with code $ExitCode despite ok=true" `
            -Code 'child_exit_mismatch' `
            -Execution @{ runtime = $Runtime; stage = 'execute'; exit_code = $ExitCode }
    }

    if (-not $result['schema_version']) { $result['schema_version'] = '2.3' }
    if (-not $result['evidence_state']) { $result['evidence_state'] = 'INCONCLUSIVE_EVIDENCE' }
    foreach ($field in @(
        'findings','recommendations','solutions','next_steps',
        'additional_logs_needed','evidence_gaps'
    )) {
        if ($null -eq $result[$field]) { $result[$field] = @() }
    }
    foreach ($field in @('root_cause','cross_validation','collection_plan','error')) {
        if (-not $result.Contains($field)) { $result[$field] = $null }
    }
    if (-not $result['confidence']) { $result['confidence'] = 'low' }
    if ($result['generated_at'] -is [datetime]) {
        $result['generated_at'] = $result['generated_at'].ToUniversalTime().ToString('o')
    } elseif ($result['generated_at'] -is [datetimeoffset]) {
        $result['generated_at'] = $result['generated_at'].ToUniversalTime().ToString('o')
    }
    if ($result['raw'] -isnot [System.Collections.IDictionary]) { $result['raw'] = @{} }
    $result['raw']['contract_validation'] = [ordered]@{
        valid = $true
        interpreted_version = $result['schema_version']
        warnings = @()
    }
    $execution = [ordered]@{
        runtime = $Runtime
        stage = 'execute'
        exit_code = $ExitCode
    }
    if ($Stderr.Trim()) {
        $execution['stderr_character_count'] = $Stderr.Length
        $execution['stderr_sha256'] = Get-SkillRedactionHashInternal -Value $Stderr -Length 64
    }
    $result['raw']['execution'] = $execution
    return $result
}

function Write-SkillResult {
    [CmdletBinding()] param([Parameter(Mandatory)][object] $Result)
    $safeResult = Protect-SkillValueInternal -Value $Result
    if ($safeResult['raw'] -is [System.Collections.IDictionary]) {
        $safeResult['raw']['privacy'] = [ordered]@{
            default_redaction = $true
            shape_preserved = $true
        }
    }
    Add-SkillFindingProvenanceInternal -Result $safeResult
    # -Depth 12 is plenty for our envelopes; -Compress keeps it stdout-friendly when piped
    $safeResult | ConvertTo-Json -Depth 12 | Write-Output
}

function ConvertTo-SkillTime {
    [CmdletBinding()] param([Parameter(Mandatory)] [string] $Value)
    [datetime]::Parse($Value.Trim().Replace('Z','+00:00'))
}

function Get-SkillTimeRange {
    [CmdletBinding()] param([Parameter(Mandatory)] [hashtable] $Context)
    $tr = $Context['time_range']
    $start = $null; $end = $null
    if ($tr) {
        if ($tr['start']) { $start = ConvertTo-SkillTime $tr['start'] }
        if ($tr['end'])   { $end   = ConvertTo-SkillTime $tr['end'] }
    }
    return ,@($start, $end)
}

function Test-InTimeWindow {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [datetime] $Timestamp,
        [Nullable[datetime]] $Start = $null,
        [Nullable[datetime]] $End   = $null,
        [double] $ToleranceMinutes  = 0
    )
    $tol = [timespan]::FromMinutes($ToleranceMinutes)
    if ($Start -and $Timestamp -lt $Start - $tol) { return $false }
    if ($End   -and $Timestamp -gt $End   + $tol) { return $false }
    return $true
}

Export-ModuleMember -Function `
    Get-SkillRegistry, Resolve-SkillEntry, Read-SkillContext,
    New-SkillFinding, New-SkillSolution, New-SkillNextStep, New-SkillLogRequest,
    New-SkillResult, New-SkillOperationalFailure, ConvertTo-SkillChildResult,
    Write-SkillResult,
    ConvertTo-SkillTime, Get-SkillTimeRange, Test-InTimeWindow
