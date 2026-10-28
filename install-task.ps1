<#
.SYNOPSIS
  Run isp_switcher in the background at logon, like a service.

.DESCRIPTION
  A real Windows service can't be used: services run in session 0, which has no
  keyboard hook access and no user audio session. Instead this registers a Task
  Scheduler task that:
    - starts dist\isp_switcher\isp_switcher_bg.exe (no window) at your logon, 30 s delayed
    - runs with highest privileges, so the keys also work while an elevated
      (admin) window has focus
    - retries the startup checks for 5 minutes (--wait 300) while network/router come up
    - has no time limit, keeps running on battery, restarts if it crashes
  Output goes to the rotating log configured in config.toml [log].

.PARAMETER Uninstall
  Stop and remove the task.

.PARAMETER Stop
  Stop the running background copy (e.g. before rebuilding with run.ps1).

.PARAMETER Start
  Start the task now.

.PARAMETER Status
  Show the task state, the running process and the end of the log.

.EXAMPLE
  .\install-task.ps1            # install (or update) and start it
  .\install-task.ps1 -Status
  .\install-task.ps1 -Stop
  .\install-task.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [switch]$Stop,
    [switch]$Start,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$TaskName = 'ISP Switcher (micro keyboard)'
$distDir = Join-Path $PSScriptRoot 'dist\isp_switcher'
$exe = Join-Path $distDir 'isp_switcher_bg.exe'
$configPath = Join-Path $PSScriptRoot 'config.toml'

function Fail([string]$Message) {
    Write-Host "[task] FATAL   $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Message) {
    Write-Host "[task] $Message" -ForegroundColor Cyan
}

function Get-Task {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Get-Running {
    @(Get-CimInstance Win32_Process -Filter "Name like 'isp_switcher%.exe'" |
      Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($distDir, 'OrdinalIgnoreCase') })
}

function Stop-Switcher {
    if (Get-Task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
    foreach ($p in Get-Running) {
        Step "stopping $($p.Name) (PID $($p.ProcessId))"
        Stop-Process -Id $p.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue
    }
}

function Get-LogPath {
    # [log] dir from config.toml (relative to it), default "log".
    $dir = 'log'
    if (Test-Path -LiteralPath $configPath) {
        $inLog = $false
        foreach ($line in Get-Content -LiteralPath $configPath) {
            if ($line -match '^\s*\[(.+)\]\s*$') { $inLog = ($Matches[1] -eq 'log'); continue }
            if ($inLog -and $line -match '^\s*dir\s*=\s*"([^"]*)"') { $dir = $Matches[1]; break }
        }
    }
    $dir = [Environment]::ExpandEnvironmentVariables($dir.Replace('~', $HOME))
    if (-not [IO.Path]::IsPathRooted($dir)) { $dir = Join-Path $PSScriptRoot $dir }
    Join-Path $dir 'isp_switcher.log'
}

function Show-Status {
    $task = Get-Task
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Step "task '$TaskName': $($task.State), last run $($info.LastRunTime), last result 0x$('{0:X}' -f $info.LastTaskResult)"
    } else {
        Step "task '$TaskName' is not installed"
    }
    $procs = Get-Running
    if ($procs) {
        $procs | ForEach-Object { Step "running: $($_.Name) (PID $($_.ProcessId))" }
    } else {
        Step "not running"
    }
    $log = Get-LogPath
    if (Test-Path -LiteralPath $log) {
        Step "last lines of ${log}:"
        Get-Content -LiteralPath $log -Tail 15 | ForEach-Object { "    $_" }
    }
}

if ($Status) { Show-Status; exit 0 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Fail "run this from an elevated (Run as administrator) PowerShell: the task runs with highest privileges."
}

if ($Stop) { Stop-Switcher; Step "stopped"; exit 0 }

if ($Uninstall) {
    Stop-Switcher
    if (Get-Task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Step "task removed"
    } else {
        Step "task was not installed"
    }
    exit 0
}

if ($Start) {
    if (-not (Get-Task)) { Fail "task is not installed: run .\install-task.ps1 first." }
    Start-ScheduledTask -TaskName $TaskName
    Step "started"
    exit 0
}

# --- install / update ------------------------------------------------------
if (-not (Test-Path -LiteralPath $exe)) {
    Fail "$exe not found. Build it first with .\run.ps1 -Check."
}
if (-not (Test-Path -LiteralPath $configPath)) {
    Fail "config.toml not found next to this script."
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute $exe `
    -Argument "--config `"$configPath`" --wait 300" -WorkingDirectory $distDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)

Stop-Switcher
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description 'Mini keyboard: F18 swaps the MikroTik ISP, F19 toggles the mic, LED shows state. Installed by install-task.ps1.' |
    Out-Null
Step "task '$TaskName' installed for $user (at logon, highest privileges)"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
Show-Status
