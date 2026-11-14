$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $projectRoot 'launcher\Launcher.cs'
$staged = Join-Path $projectRoot 'build\LocalAIWorkspace.exe'
$icon = Join-Path $projectRoot 'resources\app.ico'
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Windows.Forms.dll "/win32icon:$icon" "/out:$staged" $source
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed' }
if ((Get-Item -LiteralPath $staged).Length -lt 4096) { throw 'Invalid launcher output' }
$backup = Join-Path $projectRoot 'legacy-backup'
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$old = Join-Path $projectRoot 'LocalAIWorkspace.exe'
if ((Test-Path -LiteralPath $old) -and !(Test-Path -LiteralPath (Join-Path $backup 'LocalAIWorkspace.exe'))) {
    Copy-Item -LiteralPath $old -Destination (Join-Path $backup 'LocalAIWorkspace.exe')
}
Copy-Item -LiteralPath $staged -Destination $old -Force
Write-Output "Built: $old"
