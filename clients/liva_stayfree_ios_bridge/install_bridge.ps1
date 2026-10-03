[CmdletBinding()]
param(
    [string]$ServerUrl = "https://liva.example.com",
    [string]$StayFreeDbPath = "",
    [string]$DeviceName = "iPhone 13"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $RepoRoot ".venv-activity\Scripts\python.exe"
$Pythonw = Join-Path $RepoRoot ".venv-activity\Scripts\pythonw.exe"
$Runner = Join-Path $PSScriptRoot "runner.pyw"
$ConfigDir = Join-Path $env:LOCALAPPDATA "LIVA\StayFreeIOSBridge"
$ConfigPath = Join-Path $ConfigDir "config.json"

if (-not (Test-Path $Python) -or -not (Test-Path $Pythonw)) {
    throw "Die Activity-Python-Umgebung fehlt unter $RepoRoot\.venv-activity"
}

Write-Host "Pruefe Zeitzonen- und TLS-Zertifikatsdaten ..."
$dependencyInstall = Start-Process `
    -FilePath $Python `
    -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "--quiet", "tzdata>=2025.2", "certifi>=2025.8.3") `
    -NoNewWindow `
    -Wait `
    -PassThru
if ($dependencyInstall.ExitCode -ne 0) {
    throw "Die Python-Zeitzonen- oder TLS-Zertifikatsdaten konnten nicht installiert werden."
}

$activityKey = [Environment]::GetEnvironmentVariable("LIVA_ACTIVITY_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($activityKey)) { $activityKey = $env:LIVA_ACTIVITY_API_KEY }
if ([string]::IsNullOrWhiteSpace($activityKey)) {
    $secureKey = Read-Host "Vorhandenen LIVA Activity API Key eingeben" -AsSecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $activityKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
}
if ([string]::IsNullOrWhiteSpace($activityKey)) {
    throw "Der Activity API Key darf nicht leer sein."
}

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$deviceId = [guid]::NewGuid().ToString()
if (Test-Path $ConfigPath) {
    try {
        $oldConfig = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
        if ($oldConfig.device_id) { $deviceId = [string]$oldConfig.device_id }
    } catch {}
}
$config = [ordered]@{
    server_url = $ServerUrl.TrimEnd("/")
    api_key = $activityKey
    device_id = $deviceId
    device_name = $DeviceName
    history_days = 90
    request_timeout_seconds = 20
}
if (-not [string]::IsNullOrWhiteSpace($StayFreeDbPath)) {
    $config.stayfree_db_path = (Resolve-Path $StayFreeDbPath).Path
}
$configJson = $config | ConvertTo-Json
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ConfigPath, $configJson, $utf8WithoutBom)
$activityKey = $null

icacls.exe $ConfigDir /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null

Unregister-ScheduledTask -TaskName "LIVA StayFree iOS Import" -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Konfiguration vorbereitet. Der Schluessel liegt geschuetzt in $ConfigPath"
Write-Host "Pruefe StayFree read-only und verfuegbare abgeschlossene Tage ..."
& $Python -m clients.liva_stayfree_ios_bridge --config $ConfigPath --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "StayFree-Pruefung fehlgeschlagen. Es wurde nichts zu LIVA importiert."
}
Write-Host "Pruefung bestanden. Importiere die verfuegbare abgeschlossene Historie ..."
& $Python -m clients.liva_stayfree_ios_bridge --config $ConfigPath --backfill
if ($LASTEXITCODE -ne 0) {
    throw "Backfill nicht vollstaendig. Der lokale Retry-State bleibt erhalten."
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Runner`" --config `"$ConfigPath`"" -WorkingDirectory $RepoRoot
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "04:15"),
    (New-ScheduledTaskTrigger -Daily -At "04:30"),
    (New-ScheduledTaskTrigger -Daily -At "05:00"),
    (New-ScheduledTaskTrigger -Daily -At "06:00"),
    (New-ScheduledTaskTrigger -Daily -At "12:00"),
    (New-ScheduledTaskTrigger -Daily -At "18:00"),
    (New-ScheduledTaskTrigger -Daily -At "23:00"),
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3 -Minutes 10)
Register-ScheduledTask -TaskName "LIVA StayFree iOS Import" -Action $action -Trigger $triggers -Settings $settings -Description "Importiert abgeschlossene StayFree-iOS-Tageswerte nach LIVA." -Force | Out-Null
Write-Host "Erstimport bestaetigt. Der Hintergrundjob ist jetzt aktiv und holt verpasste Laeufe nach."
