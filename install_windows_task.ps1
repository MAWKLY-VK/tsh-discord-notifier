# Registers a Windows scheduled task that runs notifier.py every 15 minutes in
# the background (pythonw.exe, no console window). Output goes to notifier.log.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows_task.ps1
#
# Remove it again with:
#   schtasks /Delete /TN "TSH Discord Notifier" /F

param(
    [int]$Minutes = 15,
    [string]$TaskName = "TSH Discord Notifier"
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

$python = (& py -3 -c "import sys; print(sys.executable)" | Select-Object -First 1)
if (-not $python) { throw "Python 3 was not found. Install it from https://www.python.org/downloads/ first." }
$pythonw = Join-Path (Split-Path $python.Trim()) "pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe was not found next to $python" }

if (-not (Test-Path (Join-Path $here "discord_webhook.txt"))) {
    throw "Put your Discord webhook URL in discord_webhook.txt (next to notifier.py) first."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$here\notifier.py`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Posts TheSuperHackers/GeneralsGameCode pull requests and releases to Discord" -Force | Out-Null

Write-Host "Task '$TaskName' runs every $Minutes minutes while you are signed in."
Write-Host "Log file: $here\notifier.log"
