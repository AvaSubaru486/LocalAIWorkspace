$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backupRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'legacy-backup'))
if (!$backupRoot.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid backup target' }
$modelBackup = Join-Path $backupRoot 'models'
New-Item -ItemType Directory -Path $modelBackup -Force | Out-Null
$files = @('data\models\Holo-3.1-9B.Q4_K_M.gguf', 'data\models\Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M.gguf', 'data\models\Spark-X2.5-4B-Q4_K_M.gguf', 'LocalAIWorkspace-win-x64.zip')
foreach ($relative in $files) {
    $source = [IO.Path]::GetFullPath((Join-Path $projectRoot $relative))
    if (!$source.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid source path' }
    $destination = if ($relative.EndsWith('.gguf')) { Join-Path $modelBackup ([IO.Path]::GetFileName($source)) } else { Join-Path $backupRoot ([IO.Path]::GetFileName($source)) }
    $destination = [IO.Path]::GetFullPath($destination)
    if (!$destination.StartsWith($backupRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid destination path' }
    if ((Test-Path -LiteralPath $source) -and !(Test-Path -LiteralPath $destination)) {
        Move-Item -LiteralPath $source -Destination $destination
        Write-Output "Archived: $relative"
    }
}
$oldManifest = Join-Path $projectRoot 'data\MODEL-MANIFEST.json'
$backupManifest = Join-Path $backupRoot 'MODEL-MANIFEST.json'
if ((Test-Path -LiteralPath $oldManifest) -and !(Test-Path -LiteralPath $backupManifest)) {
    Copy-Item -LiteralPath $oldManifest -Destination $backupManifest
}
