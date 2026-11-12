param([string]$BuildDirectory=(Join-Path $PSScriptRoot 'build-source'))
$ErrorActionPreference='Stop'
$revision='3b352aa075d63cd5ebae546c9fe7d7d9b81fa7c9'
$sourceRoot=[IO.Path]::GetFullPath($BuildDirectory)
if(Test-Path -LiteralPath $sourceRoot){throw 'Use a new empty build directory; existing files are preserved.'}
& git clone --depth 1 --branch v2.7.3 https://github.com/xpzouying/xiaohongshu-mcp.git $sourceRoot
if($LASTEXITCODE -ne 0){throw 'Clone failed'}
$actual=& git -C $sourceRoot rev-parse HEAD
if($LASTEXITCODE -ne 0 -or [string]$actual -ne $revision){throw 'Upstream revision did not match'}
foreach($relative in @('cmd/coldstart-session/main.go','cmd/coldstart-session/readback.go','cmd/coldstart-session/readback_test.go','cmd/coldstart-session/inventory_test.go','browser/browser.go','xiaohongshu/creator_session.go','xiaohongshu/publish.go','xiaohongshu/coldstart_identity_wait_test.go')){
    $destination=Join-Path $sourceRoot $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $relative) -Destination $destination
}
$applicationRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../coldstart'))
$output=Join-Path $applicationRoot 'xhs-session-rebuilt-windows-amd64.exe'
& go -C $sourceRoot build -trimpath -o $output ./cmd/coldstart-session
if($LASTEXITCODE -ne 0){throw 'Build failed'}
Get-FileHash -LiteralPath $output -Algorithm SHA256
Write-Host 'A separate rebuilt executable was created. The runtime hash pin was not changed.'
