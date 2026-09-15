# install-night-drain.ps1 - wake the render node at 03:00 to drain photodump's queue.
#
# Run as the desktop user, elevated (an admin SSH session from kanto is fine):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\ComfyUI\install-night-drain.ps1
# kanto's tools/install-night-drain.sh copies the files and runs this for you.
#
# Idempotent. Undo with uninstall-night-drain.ps1, which also restores the
# "Allow wake timers" value this changes.

param(
    [string]$At = '03:00',
    [string]$Dir = 'C:\ComfyUI'
)

$ErrorActionPreference = 'Stop'
$task = 'photodump night drain'
$script = Join-Path $Dir 'night-drain.ps1'
if (-not (Test-Path $script)) { throw "missing $script - copy night-drain.ps1 there first" }

# "Allow wake timers" (SUB_SLEEP / RTCWAKE). Windows ships it enabled on AC
# on most desktops, but OEM plans and "Balanced" tweaks often disable it, and
# then a WakeToRun task silently never wakes anything. Remember the old value.
$query = powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE | Out-String
$prev = if ($query -match 'Current AC Power Setting Index:\s+0x([0-9a-f]+)') { [Convert]::ToInt32($Matches[1], 16) } else { -1 }
$prevFile = Join-Path $Dir 'night-drain.prev-rtcwake'
if (-not (Test-Path $prevFile)) { Set-Content -Path $prevFile -Value $prev }
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setactive SCHEME_CURRENT

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# WakeToRun is the whole point. StartWhenAvailable stays off: a 03:00 run
# missed because the PC was switched off must not fire at lunchtime instead.
$settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
$settings.StartWhenAvailable = $false
# Interactive = "run only when the user is logged on", in their session. The
# drain script reads keyboard/mouse activity, which a session-0 task cannot see.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Wakes the PC to render photodump jobs queued on kanto, then sleeps it again. See photodump/desktop/.' -Force | Out-Null

$t = Get-ScheduledTask -TaskName $task
$info = $t | Get-ScheduledTaskInfo
Write-Output "task:          $task ($($t.State)), next run $($info.NextRunTime)"
Write-Output "wake to run:   $($t.Settings.WakeToRun)"
Write-Output "wake timers:   AC index 1 (was $prev)"
Write-Output ''
Write-Output 'sleep states this machine supports (a wake timer needs S3 or hibernate; Modern Standby may ignore it):'
powercfg /a | Out-String | Write-Output

# One short real run, never suspending: proves the script compiles its native
# calls, reaches kanto over Tailscale+TLS, and can read the queue. Its lines
# land in night-drain.log like a real night's would.
Write-Output 'self-test (about a minute, -NoSleep):'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -NoSleep -ReachMinutes 1 -IdleMinutes 0 -MaxMinutes 2
Get-Content (Join-Path $Dir 'night-drain.log') -Tail 4 | Write-Output
