$caseflowPidFile = Join-Path $PSScriptRoot 'runs/server.pid'
if (-not (Test-Path -LiteralPath $caseflowPidFile)) {
    Write-Output 'No saved CaseFlow background process.'
    exit 0
}
$caseflowPid = [int](Get-Content -LiteralPath $caseflowPidFile -Raw)
$caseflowProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $caseflowPid"
if (-not $caseflowProcess) {
    Write-Output 'CaseFlow process is already stopped.'
    exit 0
}
if ($caseflowProcess.CommandLine -notmatch '-m\s+caseflow\s+serve\s+--port\s+8780(?:\s|$)') {
    throw 'Saved PID no longer identifies the expected CaseFlow server; no process was stopped.'
}
Stop-Process -Id $caseflowPid
Write-Output 'CaseFlow background server stopped.'
