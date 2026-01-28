<#
.SYNOPSIS
    Launch all three workflow agents in parallel using PowerShell jobs or Windows Terminal.

.DESCRIPTION
    This script starts Planner, Worker, and Reviewer agents for a given repository.
    Supports multiple modes: jobs (background), windows (separate windows), or terminal (Windows Terminal tabs).

.PARAMETER Repo
    Repository in owner/repo format (required)

.PARAMETER Mode
    Launch mode: jobs, windows, or terminal (default: jobs)

.PARAMETER Config
    Path to config file (optional)

.EXAMPLE
    .\launch.ps1 -Repo owner/repo
    .\launch.ps1 -Repo owner/repo -Mode windows
    .\launch.ps1 -Repo owner/repo -Mode terminal
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$Repo,

    [ValidateSet("jobs", "windows", "terminal")]
    [string]$Mode = "jobs",

    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineDir = Split-Path -Parent $ScriptDir

# Build config argument
$ConfigArg = if ($Config) { "--config `"$Config`"" } else { "" }

# Agent commands
$PlannerCmd = "uv run `"$EngineDir\planner-agent\main.py`" $Repo $ConfigArg"
$WorkerCmd = "uv run `"$EngineDir\worker-agent\main.py`" $Repo $ConfigArg"
$ReviewerCmd = "uv run `"$EngineDir\reviewer-agent\main.py`" $Repo $ConfigArg"

function Start-WithJobs {
    Write-Host "Starting agents as background jobs..." -ForegroundColor Cyan
    Write-Host "Repository: $Repo" -ForegroundColor Yellow
    Write-Host ""

    # Note: Planner is interactive, so we run it in foreground
    Write-Host "Starting Worker Agent (background)..." -ForegroundColor Green
    $workerJob = Start-Job -ScriptBlock {
        param($cmd, $dir)
        Set-Location $dir
        Invoke-Expression $cmd
    } -ArgumentList $WorkerCmd, $EngineDir

    Write-Host "Starting Reviewer Agent (background)..." -ForegroundColor Green
    $reviewerJob = Start-Job -ScriptBlock {
        param($cmd, $dir)
        Set-Location $dir
        Invoke-Expression $cmd
    } -ArgumentList $ReviewerCmd, $EngineDir

    Write-Host ""
    Write-Host "Background jobs started:" -ForegroundColor Cyan
    Write-Host "  Worker Job ID: $($workerJob.Id)"
    Write-Host "  Reviewer Job ID: $($reviewerJob.Id)"
    Write-Host ""
    Write-Host "Use 'Get-Job' to check status, 'Receive-Job <id>' to see output" -ForegroundColor Gray
    Write-Host "Use 'Stop-Job <id>' to stop a job" -ForegroundColor Gray
    Write-Host ""

    Write-Host "Starting Planner Agent (interactive)..." -ForegroundColor Green
    Set-Location $EngineDir
    Invoke-Expression $PlannerCmd

    # Cleanup jobs when planner exits
    Write-Host ""
    Write-Host "Stopping background jobs..." -ForegroundColor Yellow
    Stop-Job $workerJob, $reviewerJob -ErrorAction SilentlyContinue
    Remove-Job $workerJob, $reviewerJob -ErrorAction SilentlyContinue
}

function Start-WithWindows {
    Write-Host "Starting agents in separate windows..." -ForegroundColor Cyan
    Write-Host "Repository: $Repo" -ForegroundColor Yellow
    Write-Host ""

    # Start each agent in a new PowerShell window
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$EngineDir'; $WorkerCmd" -WindowStyle Normal
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$EngineDir'; $ReviewerCmd" -WindowStyle Normal
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$EngineDir'; $PlannerCmd" -WindowStyle Normal

    Write-Host "Three windows launched for Worker, Reviewer, and Planner agents." -ForegroundColor Green
}

function Start-WithTerminal {
    # Check if Windows Terminal is available
    $wtPath = Get-Command wt -ErrorAction SilentlyContinue

    if (-not $wtPath) {
        Write-Host "Windows Terminal (wt) not found. Falling back to separate windows." -ForegroundColor Yellow
        Start-WithWindows
        return
    }

    Write-Host "Starting agents in Windows Terminal tabs..." -ForegroundColor Cyan
    Write-Host "Repository: $Repo" -ForegroundColor Yellow
    Write-Host ""

    # Launch Windows Terminal with three tabs
    $wtCmd = "wt " +
        "--title `"Worker Agent`" -d `"$EngineDir`" powershell -NoExit -Command `"$WorkerCmd`" ``; " +
        "new-tab --title `"Reviewer Agent`" -d `"$EngineDir`" powershell -NoExit -Command `"$ReviewerCmd`" ``; " +
        "new-tab --title `"Planner Agent`" -d `"$EngineDir`" powershell -NoExit -Command `"$PlannerCmd`""

    Invoke-Expression $wtCmd

    Write-Host "Windows Terminal launched with three tabs." -ForegroundColor Green
}

# Main
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Workflow Engine Launcher (Windows)   " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

switch ($Mode) {
    "jobs" { Start-WithJobs }
    "windows" { Start-WithWindows }
    "terminal" { Start-WithTerminal }
}
