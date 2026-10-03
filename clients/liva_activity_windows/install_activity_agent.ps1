param(
    [string]$ActivityKey = $env:LIVA_ACTIVITY_API_KEY,
    [string]$PairingToken = $env:LIVA_ACTIVITY_LOCAL_TOKEN,
    [string]$InstallRoot = "C:\LIVA-Activity",
    [string]$ServerUrl = "https://liva.example.com",
    [string]$DeviceName = "Windows-PC"
)

$ErrorActionPreference = "Stop"
$agentRoot = Join-Path $InstallRoot "clients\liva_activity_windows"
$pythonExe = Join-Path $InstallRoot ".venv-activity\Scripts\pythonw.exe"
$taskName = "LIVA Activity Agent"

# A newly opened or elevated PowerShell can have an older environment block.
# Fall back to the values persisted in the Windows user profile so reinstalling
# the task does not require copying the secrets back into shell variables.
if ([string]::IsNullOrWhiteSpace($ActivityKey)) {
    $ActivityKey = [Environment]::GetEnvironmentVariable("LIVA_ACTIVITY_API_KEY", "User")
}
if ([string]::IsNullOrWhiteSpace($PairingToken)) {
    $PairingToken = [Environment]::GetEnvironmentVariable("LIVA_ACTIVITY_LOCAL_TOKEN", "User")
}

if (-not (Test-Path $pythonExe)) {
    throw "Python-Umgebung fehlt: $pythonExe"
}
if ([string]::IsNullOrWhiteSpace($ActivityKey)) {
    throw "Activity-Key fehlt. Setze LIVA_ACTIVITY_API_KEY oder übergib -ActivityKey."
}
if ([string]::IsNullOrWhiteSpace($PairingToken)) {
    throw "Pairing-Token fehlt. Setze LIVA_ACTIVITY_LOCAL_TOKEN oder übergib -PairingToken."
}
if (-not (Test-Path (Join-Path $agentRoot "__main__.py"))) {
    throw "Agent-Dateien fehlen: $agentRoot"
}

[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_SERVER_URL", $ServerUrl, "User")
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_API_KEY", $ActivityKey, "User")
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_LOCAL_TOKEN", $PairingToken, "User")
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_DEVICE_NAME", $DeviceName, "User")
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_IDLE_THRESHOLD_SECONDS", "180", "User")
$env:LIVA_ACTIVITY_SERVER_URL = $ServerUrl
$env:LIVA_ACTIVITY_API_KEY = $ActivityKey
$env:LIVA_ACTIVITY_LOCAL_TOKEN = $PairingToken
$env:LIVA_ACTIVITY_DEVICE_NAME = $DeviceName
$env:LIVA_ACTIVITY_IDLE_THRESHOLD_SECONDS = "180"

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "-m clients.liva_activity_windows" `
    -WorkingDirectory $InstallRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $taskName `
    -Description "LIVA Activity Windows-Agent" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Limited `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "LIVA Activity ist eingerichtet und läuft im Hintergrund."
Write-Host "Task: $taskName"
Write-Host "Prüfen mit: Get-ScheduledTask -TaskName '$taskName'"
