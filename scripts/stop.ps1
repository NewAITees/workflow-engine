<#
.SYNOPSIS
    Stop all running workflow agents.

.PARAMETER Repo
    Repository in owner/repo format (optional, for cleaning up specific sessions)

.EXAMPLE
    .\stop.ps1
    .\stop.ps1 -Repo owner/repo
#>

param(
    [string]$Repo = ""
)

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Workflow Engine - Stop Agents          " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Stop PowerShell jobs
$jobs = Get-Job | Where-Object { $_.Command -like "*agent/main.py*" }
if ($jobs) {
    Write-Host "Stopping PowerShell jobs..." -ForegroundColor Yellow
    $jobs | ForEach-Object {
        Write-Host "  Stopping job: $($_.Id) - $($_.Name)"
        Stop-Job $_ -ErrorAction SilentlyContinue
        Remove-Job $_ -ErrorAction SilentlyContinue
    }
}

# Kill Python processes running agents
Write-Host ""
Write-Host "Checking for agent processes..." -ForegroundColor Yellow

$agents = @("planner-agent", "worker-agent", "reviewer-agent")
foreach ($agent in $agents) {
    $procs = Get-Process python*, uv* -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*$agent*" }

    foreach ($proc in $procs) {
        Write-Host "  Stopping $agent (PID: $($proc.Id))"
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
