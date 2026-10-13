[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8014,
    [ValidateRange(1024, 65535)][int]$TemporalPort = 7233,
    [ValidateRange(1024, 65535)][int]$TemporalUiPort = 8233,
    [string]$DataRoot = (Join-Path $env:TEMP 'sage-designer-preview'),
    [string]$TemporalExe,
    [switch]$ConfiguredModel,
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $TemporalExe) { $TemporalExe = Join-Path $projectRoot 'tools\temporal\temporal.exe' }
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runRoot = Join-Path $env:LOCALAPPDATA "Sage\local-$Port"
$stopFile = Join-Path $runRoot 'stop.request'
$lockPath = Join-Path $runRoot 'launcher.lock'
$processes = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$lock = $null
$dataLock = $null

function Test-Listening([int]$ListenPort) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync('127.0.0.1', $ListenPort)
        return ($pending.Wait(500) -and $client.Connected)
    } catch { return $false }
    finally { $client.Dispose() }
}

function Start-ServiceProcess([string]$Name, [string]$Executable, [string]$Arguments) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runRoot "$Name.log") `
        -RedirectStandardError (Join-Path $runRoot "$Name.error.log")
    $processes.Add($process)
    Write-Host "$Name started (PID $($process.Id))"
    return $process
}

function Assert-Processes {
    foreach ($process in $processes) {
        if ($process.HasExited) {
            throw "A service exited (PID $($process.Id), code $($process.ExitCode)). Logs: $runRoot"
        }
    }
    if (Test-Path $stopFile) { throw 'Stop requested.' }
}

function Wait-Ready([string]$Name, [scriptblock]$Check) {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    do {
        Assert-Processes
        if (& $Check) { Write-Host "$Name ready"; return }
        Wait-Event -Timeout 1 | Out-Null
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Name did not become ready within 120 seconds. Logs: $runRoot"
}

if ($Stop) {
    if (-not (Test-Path $lockPath)) { Write-Host 'No local launcher found.'; exit 0 }
    try {
        $probe = [System.IO.File]::Open($lockPath, 'Open', 'ReadWrite', 'None')
        $probe.Dispose()
        Write-Host 'Local launcher is not running.'
    } catch [System.IO.IOException] {
        [System.IO.File]::WriteAllText($stopFile, 'stop')
        Write-Host 'Stop requested. The launcher will stop its own three services.'
    }
    exit 0
}

foreach ($file in @($python, $TemporalExe)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required executable missing: $file" }
}
if (@($Port, $TemporalPort, $TemporalUiPort | Select-Object -Unique).Count -ne 3) {
    throw 'API, Temporal and Temporal UI ports must be distinct.'
}
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
if ($DataRoot.Contains('"') -or $runRoot.Contains('"')) { throw 'Paths must not contain double quotes.' }
& $TemporalExe --version
if ($LASTEXITCODE -ne 0) { throw 'Temporal executable failed.' }
& $python -c "import sage.app, sage.temporal_worker; print('SAGE dependencies available')"
if ($LASTEXITCODE -ne 0) { throw 'Install project dependencies in .venv before starting.' }
if ($CheckOnly) {
    Write-Host "Preflight passed. Data: $DataRoot; Logs: $runRoot"
    exit 0
}

$overrides = @{
    SAGE_ENVIRONMENT = 'development'; SAGE_REPLICA_COUNT = '1'
    SAGE_CONTROL_DATA_ROOT = $DataRoot; SAGE_SKILLS_ROOT = (Join-Path $projectRoot 'skills')
    SAGE_ADMIN_AUTH_ENABLED = 'false'; SAGE_ADMIN_TOKEN = ''
    SAGE_AUTHENTICATION_MODE = 'anonymous'; SAGE_ARTIFACT_BACKEND = 'local'
    SAGE_MCP_ENFORCE_ALLOWED_HOSTS = 'false'
    SAGE_TEMPORAL_TARGET = "127.0.0.1:$TemporalPort"; SAGE_TEMPORAL_NAMESPACE = 'default'
    SAGE_TEMPORAL_TASK_QUEUE = "sage-local-$Port"; SAGE_TEMPORAL_TLS = 'false'
    SAGE_TEMPORAL_API_KEY = ''; SAGE_CHECKPOINT_DATABASE_URL = ''
    SAGE_API_BASE_URL = "http://127.0.0.1:$Port"
    SAGE_WORKER_BEARER_TOKEN = ''; SAGE_WORKER_TOKEN_SCOPE = ''
}
if (-not $ConfiguredModel) {
    $overrides.SAGE_AGENT_MODEL = ''
    $overrides.SAGE_AZURE_OPENAI_AUTH = ''
}
$original = @{}
try {
    New-Item -ItemType Directory -Force $runRoot, $DataRoot | Out-Null
    $lock = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
    $dataLock = [System.IO.File]::Open((Join-Path $DataRoot 'local-launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    foreach ($listenPort in @($Port, $TemporalPort, $TemporalUiPort)) {
        if (Test-Listening $listenPort) {
            throw "Port $listenPort is occupied. Stop that service explicitly or choose another port."
        }
    }
    Remove-Item $stopFile -ErrorAction SilentlyContinue
    foreach ($name in $overrides.Keys) {
        $original[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $overrides[$name], 'Process')
    }
    $database = Join-Path $DataRoot 'temporal.db'
    $null = Start-ServiceProcess 'temporal' $TemporalExe `
        "server start-dev --ip 127.0.0.1 --port $TemporalPort --ui-ip 127.0.0.1 --ui-port $TemporalUiPort --ui-disable-news-fetch --db-filename `"$database`""
    Wait-Ready 'Temporal' {
        if (-not (Test-Listening $TemporalPort)) { return $false }
        & $TemporalExe operator cluster health --address "127.0.0.1:$TemporalPort" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    $emptySettings = ($overrides.Keys | Where-Object { $overrides[$_] -eq '' } | ForEach-Object {
        "'$_':''"
    }) -join ','
    $apiCommand = "import os; os.environ.update({$emptySettings}); import uvicorn; uvicorn.run('sage.app:app', host='127.0.0.1', port=$Port)"
    $null = Start-ServiceProcess 'api' $python "-c `"$apiCommand`""
    Wait-Ready 'SAGE API with Temporal' {
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$Port/health/ready" -TimeoutSec 3
            return ($health.status -eq 'ready' -and $health.durable -eq $true)
        } catch { return $false }
    }
    $null = Start-ServiceProcess 'worker' $python '-m sage.temporal_worker'
    Wait-Ready 'SAGE Worker' {
        foreach ($kind in @('workflow', 'activity')) {
            $report = & $TemporalExe task-queue describe --address "127.0.0.1:$TemporalPort" `
                --namespace default --task-queue "sage-local-$Port" --legacy-mode `
                --task-queue-type-legacy $kind --output json 2>$null
            if ($LASTEXITCODE -ne 0) { return $false }
            $queue = ($report -join "`n") | ConvertFrom-Json
            if (-not $queue.pollers -or @($queue.pollers).Count -eq 0) { return $false }
        }
        return $true
    }
    Write-Host "SAGE: http://127.0.0.1:$Port/admin"
    Write-Host "Temporal UI: http://127.0.0.1:$TemporalUiPort"
    Write-Host "Data: $DataRoot"
    Write-Host "Logs: $runRoot"
    Write-Host 'Local development only: anonymous API, admin auth disabled. No PostgreSQL checkpoints.'
    Write-Host 'Keep this window open. Press Ctrl+C or run stop-local.cmd to stop all three services.'
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/admin" }
    while (-not (Test-Path $stopFile)) {
        Assert-Processes
        Wait-Event -Timeout 1 | Out-Null
    }
} finally {
    for ($index = $processes.Count - 1; $index -ge 0; $index--) {
        $process = $processes[$index]
        if (-not $process.HasExited) {
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
        }
    }
    foreach ($name in $original.Keys) {
        [Environment]::SetEnvironmentVariable($name, $original[$name], 'Process')
    }
    if ($dataLock) { $dataLock.Dispose() }
    if ($lock) {
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        $lock.Dispose()
    }
}