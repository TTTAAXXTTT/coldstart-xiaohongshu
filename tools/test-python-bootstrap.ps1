param([string]$Root=(Split-Path -Parent $PSScriptRoot),[string]$PythonForProbe)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
. (Join-Path $Root 'tools\python-bootstrap.ps1')
$fixture=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'tools\fixtures\python-index-example.json') | ConvertFrom-Json
$testRoot=Join-Path $Root ('qa\python-bootstrap-tests\run-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Root 'logs') -Force | Out-Null
$cases=New-Object Collections.Generic.List[object]
$script:networkCalls=0
function Invoke-WebRequest { $script:networkCalls++;throw 'Network must not be used by these fixture tests.' }
function Good([string]$Name,[bool]$Pass){if(-not $Pass){throw ('FAILED: '+$Name)};$cases.Add([pscustomobject]@{name=$Name;status='passed'})}
function Must-Reject([string]$Name,[scriptblock]$Body){$caught=$false;try{& $Body | Out-Null}catch{$caught=$true};Good $Name $caught}
function Clone($Value){return $Value | ConvertTo-Json -Depth 10 | ConvertFrom-Json}
$selected=Get-Python312Package $fixture.versions
Good 'select_highest_stable_full_x64_version' ($selected.'sort-version' -eq '3.12.10')
$bad=Clone $selected;$bad.tag='3.12-32';Must-Reject 'reject_32bit_runtime' {Get-Python312Package @($bad)}
$bad=Clone $selected;$bad.company='PythonEmbed';Must-Reject 'reject_embedded_runtime' {Get-Python312Package @($bad)}
$bad=Clone $selected;$bad.'sort-version'='3.12.11rc1';Must-Reject 'reject_preview_runtime' {Get-Python312Package @($bad)}
$bad=Clone $selected;$bad.url='https://evil.invalid/python-3.12.10-amd64.zip';Must-Reject 'reject_foreign_download_source' {Get-Python312Package @($bad)}
$bad=Clone $selected;$bad.hash.sha256='';Must-Reject 'require_official_sha256' {Get-Python312Package @($bad)}
$file=Join-Path $testRoot 'hash.txt';[IO.File]::WriteAllText($file,'fixture',[Text.Encoding]::ASCII)
$hash=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
Good 'accept_matching_download_hash' (Test-PythonArchiveHash $file $hash)
Good 'reject_changed_download_hash' (-not (Test-PythonArchiveHash $file ('0'*64)))
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
function New-Zip([string]$Path,[string]$Entry){$z=[IO.Compression.ZipFile]::Open($Path,[IO.Compression.ZipArchiveMode]::Create);try{$e=$z.CreateEntry($Entry);$s=New-Object IO.StreamWriter($e.Open());try{$s.Write('fixture')}finally{$s.Dispose()}}finally{$z.Dispose()}}
$zip=Join-Path $testRoot 'good.zip';New-Zip $zip 'Lib/readme.txt';$dest=Join-Path $testRoot 'good';Expand-PythonArchive $zip $dest
Good 'extract_valid_archive' ((Get-Content -Raw -LiteralPath (Join-Path $dest 'Lib\readme.txt')) -eq 'fixture')
$zip=Join-Path $testRoot 'bad.zip';New-Zip $zip '../escaped.txt';Must-Reject 'reject_archive_path_escape' {Expand-PythonArchive $zip (Join-Path $testRoot 'bad')}
Good 'archive_escape_created_no_outside_file' (-not (Test-Path -LiteralPath (Join-Path $testRoot 'escaped.txt')))
$runtime=Join-Path $testRoot 'runtime\python312';New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$marker=Join-Path $runtime 'preserved.txt';[IO.File]::WriteAllText($marker,'keep')
Must-Reject 'preserve_incomplete_existing_runtime' {Install-PrivatePython312 $testRoot}
Good 'existing_runtime_data_unchanged' ((Get-Content -Raw -LiteralPath $marker) -eq 'keep')
Good 'fixture_tests_used_no_network' ($networkCalls -eq 0)
if($PythonForProbe){
    $check=& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'tools\windows.ps1') -Action Check -PythonPath $PythonForProbe
    if($LASTEXITCODE -ne 0){throw 'Existing Python detection failed.'}
    $detected=($check -join "`n") | ConvertFrom-Json
    Good 'powershell51_detects_real_existing_python' ([IO.Path]::GetFullPath([string]$detected.pythonCore) -eq [IO.Path]::GetFullPath($PythonForProbe))
}
[pscustomobject]@{scope='PowerShell logic and real local ZIP/hash operations, not a real Python download';passed=$cases.Count;cases=$cases;real_runtime_download='NOT_VERIFIED';real_runtime_install='NOT_VERIFIED'} | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'logs\PYTHON_BOOTSTRAP_LOCAL_RESULTS.json')
Write-Output ('Passed '+$cases.Count+' bootstrap checks; actual download/install NOT VERIFIED.')
