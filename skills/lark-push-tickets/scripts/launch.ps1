param(
    [Parameter(Mandatory)]
    [string[]] $WorkerArgs
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$rootIndex = [Array]::IndexOf($WorkerArgs, '--root')
if ($rootIndex -lt 0 -or $rootIndex + 1 -ge $WorkerArgs.Count) {
    throw 'WorkerArgs must include --root and its absolute project path.'
}
$projectRoot = (Resolve-Path -LiteralPath $WorkerArgs[$rootIndex + 1]).Path
foreach ($requiredFile in @('docs\agents\domain.md', 'docs\agents\issue-tracker.md')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $requiredFile))) {
        throw "Run setup-drdandan-skills first: missing $requiredFile"
    }
}
$helper = Join-Path $PSScriptRoot 'ticket_sync.py'
$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$pythonCommand = Get-Command python -CommandType Application | Select-Object -First 1
$startInfo.FileName = $pythonCommand.Source
$startInfo.UseShellExecute = $true
$startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$startInfo.ArgumentList.Add($helper)
foreach ($workerArgument in $WorkerArgs) {
    $startInfo.ArgumentList.Add($workerArgument)
}
$worker = [System.Diagnostics.Process]::Start($startInfo)
$progressFile = Join-Path $projectRoot '.scratch\.feishu-sync\last-run.json'
$startupFailure = Join-Path $projectRoot ('.scratch\.feishu-sync\start-' + $worker.Id + '.json')
Start-Sleep -Seconds 2
$record = if (Test-Path -LiteralPath $startupFailure) {
    Get-Content -LiteralPath $startupFailure -Raw | ConvertFrom-Json
} elseif (Test-Path -LiteralPath $progressFile) {
    Get-Content -LiteralPath $progressFile -Raw | ConvertFrom-Json
}
[pscustomobject]@{
    pid = $worker.Id
    progressFile = $progressFile
    progressVerified = ($null -ne $record -and $record.pid -eq $worker.Id)
    record = $record
} | ConvertTo-Json -Depth 6
