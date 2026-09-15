# night-drain.ps1 - render whatever photodump has queued, then go back to sleep.
#
# Runs from the "photodump night drain" scheduled task (install-night-drain.ps1),
# which wakes the PC at 03:00. Windows PowerShell 5.1 syntax: no ternaries, no ??.
#
# Kanto cannot wake this machine - it is on a public IP, not this LAN, and
# Wake-on-LAN packets do not cross Tailscale - so the machine wakes itself on a
# timer and asks kanto whether there is anything to do.
#
# Three guards keep it from putting the PC to sleep under someone:
#   1. It only ever suspends a machine that the timer woke. If the PC was
#      already awake before 03:00, it leaves it awake afterwards.
#   2. Any keyboard or mouse input during the run cancels the suspend.
#   3. The task runs "only when the user is logged on", in the user's own
#      session, so guard 2 sees real input (a session-0 task sees none). That
#      also matches ComfyUI, which starts at logon: no session, no renderer.

param(
    [string]$StatusUrl = 'https://kanto.tail4f3755.ts.net:8452/api/status',
    [int]$MaxMinutes = 150,      # hard stop, whatever the queue says
    [int]$IdleMinutes = 6,       # queue empty this long = done
    [int]$ReachMinutes = 8,      # how long to wait for Tailscale/ComfyUI after wake
    [switch]$NoSleep             # for a manual test run
)

$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'night-drain.log'
$started = Get-Date

function Log([string]$msg) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $msg
    Add-Content -Path $log -Value $line
}

Add-Type -Namespace PhotodumpDrain -Name Native -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
[StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
[DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
public static uint LastInputTick() {
    LASTINPUTINFO i = new LASTINPUTINFO();
    i.cbSize = (uint)System.Runtime.InteropServices.Marshal.SizeOf(i);
    GetLastInputInfo(ref i);
    return i.dwTime;
}
public static uint IdleMs() { return unchecked((uint)Environment.TickCount - LastInputTick()); }
'@

# Decimal on purpose: Windows PowerShell 5.1 reads 0x80000000 as a negative
# Int32, and casting that to UInt32 throws.
$ES_CONTINUOUS = [uint32]2147483648
$ES_SYSTEM_REQUIRED = [uint32]1

# Windows PowerShell 5.1 on .NET Framework can still offer TLS 1.0 first;
# tailscale serve's HTTPS only speaks 1.2+.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-Status {
    try {
        return Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 15 -UseBasicParsing
    } catch {
        return $null
    }
}

# Trim the log so a year of nights stays small.
if ((Test-Path $log) -and (Get-Item $log).Length -gt 512KB) {
    Get-Content $log -Tail 2000 | Set-Content "$log.tmp"
    Move-Item -Force "$log.tmp" $log
}

# Guard 1: did the timer wake us? A resume or boot in the last 10 minutes says
# yes. Anything older means the PC was already on and someone may be using it.
$wokeForUs = $false
try {
    $resume = Get-WinEvent -MaxEvents 1 -FilterHashtable @{
        LogName = 'System'; ProviderName = 'Microsoft-Windows-Power-Troubleshooter'; Id = 1 } -ErrorAction Stop
    if ($resume.TimeCreated -gt $started.AddMinutes(-10)) { $wokeForUs = $true }
} catch { }
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
if ($boot -gt $started.AddMinutes(-10)) { $wokeForUs = $true }
Log "start; woke for us: $wokeForUs; idle input ms: $([PhotodumpDrain.Native]::IdleMs())"

# Stay awake. A timer wake with nobody at the keyboard drops back to sleep
# after Windows' ~2 minute "unattended sleep timeout" otherwise.
[void][PhotodumpDrain.Native]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)

$inputAtStart = [PhotodumpDrain.Native]::LastInputTick()
$reason = ''
try {
    # Wait for Tailscale and for kanto to see ComfyUI.
    $status = $null
    $deadline = $started.AddMinutes($ReachMinutes)
    while ((Get-Date) -lt $deadline) {
        $status = Get-Status
        if ($status -and $status.online) { break }
        Start-Sleep -Seconds 20
    }
    if (-not $status) {
        $reason = 'kanto unreachable'
    } elseif (-not $status.online) {
        $reason = "kanto reachable but reports ComfyUI offline ($($status.connection_error))"
    } else {
        $idleSince = $null
        while ($true) {
            if ((Get-Date) -gt $started.AddMinutes($MaxMinutes)) { $reason = "hit the $MaxMinutes minute cap"; break }
            $status = Get-Status
            $busy = $true
            if ($status) {
                # queued counts only jobs that are due; a paused queue will not move.
                $busy = ($status.current -ne $null) -or (($status.queued -gt 0) -and -not $status.queue_paused)
            }
            if ($busy) {
                $idleSince = $null
            } elseif (-not $idleSince) {
                $idleSince = Get-Date
                Log "queue empty (scheduled: $($status.scheduled), paused jobs: $($status.paused_jobs))"
            } elseif ((Get-Date) -gt $idleSince.AddMinutes($IdleMinutes)) {
                $reason = 'queue drained'; break
            }
            Start-Sleep -Seconds 30
        }
    }
} finally {
    [void][PhotodumpDrain.Native]::SetThreadExecutionState($ES_CONTINUOUS)
}

$touched = [PhotodumpDrain.Native]::LastInputTick() -ne $inputAtStart
$minutes = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
if ($NoSleep) {
    Log "done after $minutes min: $reason; -NoSleep given, staying awake"
} elseif (-not $wokeForUs) {
    Log "done after $minutes min: $reason; PC was already awake, leaving it on"
} elseif ($touched) {
    Log "done after $minutes min: $reason; someone used the PC, leaving it on"
} else {
    Log "done after $minutes min: $reason; suspending"
    Add-Type -AssemblyName System.Windows.Forms
    # Suspend, not hibernate; do not force past apps that veto; keep wake events.
    [void][System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend, $false, $false)
}
