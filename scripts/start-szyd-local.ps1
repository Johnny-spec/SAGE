[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8015,
    [ValidateRange(1024, 65535)][int]$RunnerPort = 8016,
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'Sage\szyd-local'),
    [switch]$DemoModel
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$processes = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$original = @{}
$dataLock = $null

function Test-Listening([int]$ListenPort) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        return ($client.ConnectAsync('127.0.0.1', $ListenPort).Wait(500) -and $client.Connected)
    } catch { return $false }
    finally { $client.Dispose() }
}

function Start-Child([string]$Name, [string]$Arguments) {
    $process = Start-Process -FilePath $python -ArgumentList $Arguments `
        -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $DataRoot "$Name.log") `
        -RedirectStandardError (Join-Path $DataRoot "$Name.error.log")
    $processes.Add($process)
}

function Wait-Ready([int]$ListenPort) {
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        foreach ($process in $processes) {
            if ($process.HasExited) { throw "Service exited; inspect logs in $DataRoot" }
        }
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$ListenPort/health/ready" -TimeoutSec 2
            if ($health.status -eq 'ready') { return }
        } catch { }
        Wait-Event -Timeout 1 | Out-Null
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Service on port $ListenPort did not become ready. Logs: $DataRoot"
}

if (-not (Test-Path $python)) { throw 'Install project dependencies in .venv first.' }
if ($Port -eq $RunnerPort) { throw 'API and Runner ports must differ.' }
foreach ($listenPort in @($Port, $RunnerPort)) {
    if (Test-Listening $listenPort) { throw "Port $listenPort is occupied; choose another port." }
}
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
if ($DataRoot.Contains('"')) { throw 'DataRoot must not contain quotes.' }
$overrides = @{
    SAGE_ENVIRONMENT = 'development'; SAGE_REPLICA_COUNT = '1'
    SAGE_CONTROL_DATA_ROOT = $DataRoot; SAGE_SKILLS_ROOT = (Join-Path $projectRoot 'skills')
    SAGE_ADMIN_AUTH_ENABLED = 'false'; SAGE_ADMIN_TOKEN = ''
    SAGE_AUTHENTICATION_MODE = 'anonymous'; SAGE_ARTIFACT_BACKEND = 'local'
    SAGE_TEMPORAL_TARGET = ''; SAGE_TEMPORAL_API_KEY = ''
    SAGE_ALLOW_LOCAL_SUBPROCESS = 'false'; SAGE_AGENT_RECURSION_LIMIT = '200'
    SAGE_RUNNER_URL = "http://127.0.0.1:$RunnerPort"
    SAGE_RUNNER_WORK_ROOT = (Join-Path $DataRoot 'runner-tmp')
    SAGE_RUNNER_PROFILE = 'python-restricted-v1'
    SAGE_RUNNER_TOKEN = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
}
if ($DemoModel) { $overrides.SAGE_AGENT_MODEL = ''; $overrides.SAGE_AZURE_OPENAI_AUTH = '' }
try {
    New-Item -ItemType Directory -Force $DataRoot | Out-Null
    $dataLock = [System.IO.File]::Open((Join-Path $DataRoot 'szyd-launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    foreach ($name in $overrides.Keys) {
        $original[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $overrides[$name], 'Process')
    }
    Push-Location $projectRoot
    try {
        if (-not $DemoModel) {
            & $python -c "from sage.settings import load_agent_settings; s=load_agent_settings(); assert s.model, 'Configure SAGE_AGENT_MODEL; Demo is not a legal review model'"
            if ($LASTEXITCODE -ne 0) { throw 'Configure a real chat model or explicitly use -DemoModel.' }
        }
        Start-Child 'runner' "-m uvicorn sage.runner_app:create_runner_app --factory --host 127.0.0.1 --port $RunnerPort"
        Wait-Ready $RunnerPort
        $empty = ($overrides.Keys | Where-Object { $overrides[$_] -eq '' } | ForEach-Object { "'$_':''" }) -join ','
        $command = "import os; os.environ.update({$empty}); import uvicorn; uvicorn.run('sage.app:app', host='127.0.0.1', port=$Port)"
        Start-Child 'api' "-c `"$command`""
        Wait-Ready $Port
        & $python scripts/publish_szyd_solution.py --url "http://127.0.0.1:$Port" --activate
        if ($LASTEXITCODE -ne 0) { throw 'SZYD publication failed.' }
        Write-Host "SZYD ready: http://127.0.0.1:$Port/"
        Write-Host "State and logs: $DataRoot"
        Write-Host 'Keep this terminal open. Ctrl+C stops only the services started here.'
        if ($DemoModel) { Write-Warning 'Demo model is for platform checks only, not compliance reviews.' }
        while ($true) {
            foreach ($process in $processes) {
                if ($process.HasExited) { throw 'A SZYD service exited.' }
            }
            Wait-Event -Timeout 1 | Out-Null
        }
    } finally { Pop-Location }
} finally {
    foreach ($process in $processes) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
    foreach ($name in $original.Keys) {
        [Environment]::SetEnvironmentVariable($name, $original[$name], 'Process')
    }
    if ($dataLock) { $dataLock.Dispose() }
}