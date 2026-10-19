param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build') (Join-Path $PSScriptRoot '报销助手.spec')
if ($LASTEXITCODE -ne 0) { throw '打包失败，请查看上方日志。' }
Get-FileHash -LiteralPath (Join-Path $root 'dist\报销助手.exe') -Algorithm SHA256
