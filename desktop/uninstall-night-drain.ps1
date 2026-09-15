# uninstall-night-drain.ps1 - remove the 03:00 wake and restore the wake-timer setting.
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\ComfyUI\uninstall-night-drain.ps1

param([string]$Dir = 'C:\ComfyUI')
$ErrorActionPreference = 'Stop'
$task = 'photodump night drain'

if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false
    Write-Output "removed task: $task"
} else {
    Write-Output "no task named $task"
}

$prevFile = Join-Path $Dir 'night-drain.prev-rtcwake'
if (Test-Path $prevFile) {
    $prev = [int](Get-Content $prevFile)
    if ($prev -ge 0) {
        powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE $prev
        powercfg /setactive SCHEME_CURRENT
        Write-Output "restored Allow wake timers (AC) to $prev"
    }
    Remove-Item $prevFile
}
