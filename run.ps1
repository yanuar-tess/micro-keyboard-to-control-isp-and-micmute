<#
.SYNOPSIS
  Install what's missing, compile isp_switcher.exe if the sources changed, then run it.

.DESCRIPTION
  1. Stops if this is not Windows.
  2. Installs uv and Python 3.12 (winget) only if they are missing, then the
     Python packages (uv sync; a no-op when already installed).
  3. Rebuilds dist\isp_switcher\isp_switcher.exe with PyInstaller when it is
     missing or older than any source file.
  4. Runs the exe. Its built-in preflight stops with exit code 2 on a fatal
     problem: bad [router] config, router not a MikroTik, no mic control, or
     router config below the minimum requirement (see preflight.py). Anything
     else is only a warning.

.PARAMETER Rebuild
  Force a rebuild of the exe.

.PARAMETER Check
  Only run the preflight checks, don't start the switcher.

.EXAMPLE
  .\run.ps1
  .\run.ps1 -Check
  .\run.ps1 -Rebuild
#>
param(
    [switch]$Rebuild,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Fail([string]$Message, [int]$Code = 1) {
    Write-Host "[run] FATAL   $Message" -ForegroundColor Red
    exit $Code
}

function Step([string]$Message) {
    Write-Host "[run] $Message" -ForegroundColor Cyan
}

# --- 1. Windows only -------------------------------------------------------
# $IsWindows only exists in PowerShell 6+; Windows PowerShell 5.1 is always Windows.
$onWindows = if ($PSVersionTable.PSVersion.Major -ge 6) { $IsWindows } else { $env:OS -eq 'Windows_NT' }
if (-not $onWindows) {
    Fail "Windows is required (USB HID LED control and Windows Core Audio mic mute)."
}

$configPath = Join-Path $PSScriptRoot 'config.toml'
if (-not (Test-Path -LiteralPath $configPath)) {
    Fail "config.toml not found. Copy config.example.toml to config.toml and fill in [router]."
}

# --- 2. Tools and packages -------------------------------------------------
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$env:USERPROFILE\.local\bin"
}

function Install-WithWinget([string]$Id, [string]$What) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "$What is missing and winget is not available to install it. Install $What manually."
    }
    Step "installing $What ($Id) with winget..."
    winget install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Fail "winget could not install $What (exit $LASTEXITCODE)." }
    Refresh-Path
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Install-WithWinget 'astral-sh.uv' 'uv'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail "uv was installed but is not on PATH yet. Open a new terminal and re-run."
    }
}

# pyproject.toml pins python-preference = only-system, so a Python from the
# official installer is required (uv will not download its own).
$systemPython = uv python find '>=3.12' --python-preference only-system 2>$null
if ($LASTEXITCODE -ne 0 -or -not $systemPython) {
    Install-WithWinget 'Python.Python.3.12' 'Python 3.12'
}

Step "checking Python packages..."
uv sync --group build --quiet
if ($LASTEXITCODE -ne 0) { Fail "installing Python packages failed (uv sync exit $LASTEXITCODE)." }

# --- 3. Compile ------------------------------------------------------------
$distDir = Join-Path $PSScriptRoot 'dist\isp_switcher'
$exe = Join-Path $distDir 'isp_switcher.exe'
$sources = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter *.py -File) +
           @(Get-Item -LiteralPath (Join-Path $PSScriptRoot 'pyproject.toml'),
                                   (Join-Path $PSScriptRoot 'uv.lock'),
                                   (Join-Path $PSScriptRoot 'isp_switcher.spec'))

# A copy already running from dist (e.g. the logon task) locks its files and
# holds the single-instance mutex.
$running = @(Get-CimInstance Win32_Process -Filter "Name like 'isp_switcher%.exe'" |
             Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($distDir, 'OrdinalIgnoreCase') })

$needBuild = $Rebuild -or -not (Test-Path -LiteralPath $exe)
if (-not $needBuild) {
    $built = (Get-Item -LiteralPath $exe).LastWriteTimeUtc
    $newer = $sources | Where-Object { $_.LastWriteTimeUtc -gt $built }
    if ($newer) {
        Step "sources changed since last build: $(($newer | ForEach-Object Name) -join ', ')"
        $needBuild = $true
    }
}

if ($needBuild) {
    if ($running) {
        $names = ($running | ForEach-Object { "$($_.Name) (PID $($_.ProcessId))" }) -join ', '
        Fail "can't rebuild while $names is running from dist. Stop it first: .\install-task.ps1 -Stop, or close the running copy."
    }
    Step "compiling dist\isp_switcher\ (isp_switcher.exe + isp_switcher_bg.exe)..."
    uv run --group build pyinstaller isp_switcher.spec `
        --noconfirm --clean --distpath dist --workpath build --log-level WARN
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exe)) {
        Fail "compiling the exe failed (pyinstaller exit $LASTEXITCODE)."
    }
    Step "build ok"
} else {
    Step "exe is up to date"
}

# --- 4. Run ----------------------------------------------------------------
$env:ISP_SWITCHER_CONFIG = $configPath
$exeArgs = @()
if ($Check) { $exeArgs += '--check' }

Step "starting $(Split-Path -Leaf $exe) $($exeArgs -join ' ')"
& $exe @exeArgs
$code = $LASTEXITCODE
if ($code -eq 2) { Fail "preflight found fatal problems (see above)." 2 }
if ($code -eq 3) { Fail "another copy is already running (logon task?). Stop it with .\install-task.ps1 -Stop to run here." 3 }
exit $code
