@echo off
setlocal
set "AZURE_CORE_ENABLE_BROKER_ON_WINDOWS=false"
set "SAGE_AZURE_OPENAI_AUTH=entra-id"
set "SAGE_AZURE_OPENAI_CREDENTIAL=azure-cli"
set "SAGE_AZURE_OPENAI_TENANT_ID=16b3c013-d300-468d-ac64-7eda0820b6d3"
set "AZURE_OPENAI_ENDPOINT=https://wshao-childqa-resource.services.ai.azure.com/openai/v1"
echo Signing in with Entra ID. Complete any account selection or MFA in your browser.
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\smoke_azure_entra.py" --tenant 16b3c013-d300-468d-ac64-7eda0820b6d3 --subscription c3bb825f-7a17-4c5e-ad18-9e7f850e488b --configure
if errorlevel 1 exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local.ps1" -ConfiguredModel %*
exit /b %errorlevel%