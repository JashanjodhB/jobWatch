<#
.SYNOPSIS
    Start, stop, and check jobwatch on Windows.

.DESCRIPTION
    Runs the service in the background from this machine. No admin rights, no
    service registration, no auto-start: you launch it, it keeps polling until
    you stop it or the machine reboots.

    Logs go to logs\jobwatch-<date>.log. A PID file in logs\jobwatch.pid is what
    -Stop and -Status read, and what stops a second copy from starting: two
    pollers on one database means two alerts for every posting.

.PARAMETER Hidden
    Detach with no console window. Without it the process runs in this window
    and closing the window stops it.

.PARAMETER Stop
    Stop the running instance.

.PARAMETER Status
    Report whether it is running, and where the log is.

.PARAMETER Follow
    Tail the log after starting.

.PARAMETER DryRun
    Print alerts instead of sending them. Use this to tune filters.

.EXAMPLE
    .\deploy\jobwatch.ps1 -Hidden
    Start in the background and return to the prompt.

.EXAMPLE
    .\deploy\jobwatch.ps1 -Status
#>
[CmdletBinding(DefaultParameterSetName = 'Start')]
param(
    [Parameter(ParameterSetName = 'Start')][switch]$Hidden,
    [Parameter(ParameterSetName = 'Start')][switch]$Follow,
    [Parameter(ParameterSetName = 'Start')][switch]$DryRun,
    [Parameter(ParameterSetName = 'Start')][switch]$NoWeb,
    [Parameter(ParameterSetName = 'Stop')][switch]$Stop,
    [Parameter(ParameterSetName = 'Status')][switch]$Status
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root 'logs'
$PidFile = Join-Path $LogDir 'jobwatch.pid'
# Per-start, not per-day: Start-Process truncates what it redirects into, so a
# restart would otherwise erase the morning's log right when you want to read it.
$LogFile = Join-Path $LogDir ("jobwatch-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd-HHmmss'))

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Get-PidRecord {
    if (-not (Test-Path $PidFile)) { return $null }
    $recorded = @(Get-Content $PidFile -ErrorAction SilentlyContinue)[0]
    if (-not $recorded) { return $null }

    $parts = $recorded -split '\|'
    $processId = 0
    if (-not [int]::TryParse($parts[0], [ref]$processId)) { return $null }

    # A PID file outlives a crash, so the number alone proves nothing. Match the
    # recorded start time too: Windows reuses PIDs, and killing an unrelated
    # process because it inherited this one's number would be a bad day.
    try { $proc = Get-Process -Id $processId -ErrorAction Stop } catch { return $null }

    if ($parts.Count -ge 2) {
        $stamp = [DateTime]::MinValue
        if ([DateTime]::TryParse($parts[1], [ref]$stamp)) {
            if ([math]::Abs(($proc.StartTime - $stamp).TotalSeconds) -gt 5) { return $null }
        }
    }
    $log = $null
    if ($parts.Count -ge 3) { $log = $parts[2] }
    return @{ Process = $proc; Log = $log }
}

function Get-RunningProcess {
    $record = Get-PidRecord
    if ($record) { return $record.Process }
    return $null
}

function Resolve-Launcher {
    # uv owns the environment; fall back to the venv's console script if the
    # repo was set up with plain pip.
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return @{ File = $uv.Source; Args = @('run', 'jobwatch') } }

    $exe = Join-Path $Root '.venv\Scripts\jobwatch.exe'
    if (Test-Path $exe) { return @{ File = $exe; Args = @() } }

    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $python) { return @{ File = $python; Args = @('-m', 'jobwatch.cli') } }

    throw "Found neither uv nor .venv in $Root. Run 'uv sync' first."
}

# ── stop ──────────────────────────────────────────────────────────────────

if ($Stop) {
    $proc = Get-RunningProcess
    if (-not $proc) {
        Write-Host 'jobwatch is not running.' -ForegroundColor Yellow
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
        exit 0
    }
    Write-Host "Stopping jobwatch (PID $($proc.Id))..." -NoNewline
    # A hidden console process has no window to close, so asking costs five
    # seconds and achieves nothing. Killing it is safe either way: every alert
    # is a committed outbox row before it is a message, and SQLite is in WAL.
    if ($proc.MainWindowHandle -ne 0) {
        $proc.CloseMainWindow() | Out-Null
        if (-not $proc.WaitForExit(5000)) { Stop-Process -Id $proc.Id -Force }
    }
    else {
        Stop-Process -Id $proc.Id -Force
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host ' stopped.' -ForegroundColor Green
    exit 0
}

# ── status ────────────────────────────────────────────────────────────────

if ($Status) {
    $record = Get-PidRecord
    if ($record) {
        $proc = $record.Process
        $uptime = (Get-Date) - $proc.StartTime
        Write-Host "jobwatch is running" -ForegroundColor Green
        Write-Host "  PID      $($proc.Id)"
        Write-Host ("  uptime   {0:d\d\ hh\:mm\:ss}" -f $uptime)
        if ($record.Log) { Write-Host "  log      $($record.Log)" }
        Write-Host "  web UI   http://127.0.0.1:8080"
    }
    else {
        Write-Host 'jobwatch is not running.' -ForegroundColor Yellow
        Write-Host "  start it with: .\deploy\jobwatch.ps1 -Hidden"
    }
    exit 0
}

# ── start ─────────────────────────────────────────────────────────────────

$existing = Get-RunningProcess
if ($existing) {
    Write-Host "jobwatch is already running (PID $($existing.Id))." -ForegroundColor Yellow
    Write-Host "Two pollers on one database would alert twice for every posting."
    Write-Host "Stop it first:  .\deploy\jobwatch.ps1 -Stop"
    exit 1
}

$launcher = Resolve-Launcher
$jobArgs = $launcher.Args + @('run')
if ($DryRun) { $jobArgs += '--dry-run' }
if ($NoWeb) { $jobArgs += '--no-web' }

if (-not (Test-Path (Join-Path $Root '.env'))) {
    Write-Host 'No .env found — alerts will queue in the outbox but cannot be delivered.' -ForegroundColor Yellow
    Write-Host '  copy .env.example to .env and set DISCORD_WEBHOOK_URL, or SMTP_HOST and EMAIL_TO.'
}

Write-Host "Starting jobwatch from $Root"
Write-Host "  log  $LogFile"

$startArgs = @{
    FilePath               = $launcher.File
    ArgumentList           = $jobArgs
    WorkingDirectory       = $Root
    RedirectStandardOutput = $LogFile
    RedirectStandardError  = (Join-Path $LogDir 'jobwatch-stderr.log')
    PassThru               = $true
}
if ($Hidden) {
    $startArgs['WindowStyle'] = 'Hidden'
}
else {
    $startArgs['NoNewWindow'] = $true
}

$proc = Start-Process @startArgs
"$($proc.Id)|$($proc.StartTime.ToString('o'))|$LogFile" | Set-Content -Path $PidFile -Encoding utf8

Start-Sleep -Seconds 2
if ($proc.HasExited) {
    Write-Host "jobwatch exited immediately (code $($proc.ExitCode)). Last lines:" -ForegroundColor Red
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 }
    $stderr = Join-Path $LogDir 'jobwatch-stderr.log'
    if (Test-Path $stderr) { Get-Content $stderr -Tail 20 }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "jobwatch is running (PID $($proc.Id))." -ForegroundColor Green
Write-Host "  web UI   http://127.0.0.1:8080"
Write-Host "  stop     .\deploy\jobwatch.ps1 -Stop"

if ($Follow) { Get-Content $LogFile -Wait -Tail 20 }
