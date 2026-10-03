param([int]$Port = 8780, [string]$EnvFile = '')
Push-Location $PSScriptRoot
try {
    if ($EnvFile) { python -m caseflow serve --port $Port --env-file $EnvFile }
    else { python -m caseflow serve --port $Port }
}
finally { Pop-Location }
