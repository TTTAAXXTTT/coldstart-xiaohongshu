# PowerShell 5.1 compatible. Functions only: importing this file never downloads or installs.
function Save-DependencyDownload([string]$Url,[string]$Destination,[int]$TimeoutSeconds=900){
    # Use Windows curl to honor HTTPS_PROXY. Keep TLS verification enabled.
    # The caller validates the source and SHA256 before executing downloads.
    $curl=Get-Command curl.exe -ErrorAction SilentlyContinue
    if($curl){
        for($attempt=1;$attempt -le 3;$attempt++){
            & $curl.Source --fail --location --continue-at - --connect-timeout 20 --max-time $TimeoutSeconds --silent --show-error --output $Destination $Url
            if($LASTEXITCODE -eq 0){return}
            if($attempt -lt 3){Write-Host 'Download interrupted; retrying from the saved partial file.'}
        }
        throw ('Dependency download failed. Partial file was preserved. curl exit code: '+$LASTEXITCODE)
    }else{
        $options=@{UseBasicParsing=$true;Uri=$Url;OutFile=$Destination;TimeoutSec=$TimeoutSeconds}
        if($env:HTTPS_PROXY){$options.Proxy=$env:HTTPS_PROXY}
        Invoke-WebRequest @options
    }
}

function Get-Python312Package([object[]]$Versions){
    $valid=@()
    foreach($item in $Versions){
        if($item.company -ne 'PythonCore' -or $item.tag -ne '3.12-64' -or $item.'sort-version' -notmatch '^3\.12\.\d+$'){continue}
        if($item.executable -notin @('python.exe','.\python.exe','./python.exe')){continue}
        $uri=$null
        if(-not [Uri]::TryCreate([string]$item.url,[UriKind]::Absolute,[ref]$uri)){continue}
        if($uri.Scheme -ne 'https' -or $uri.Host -ne 'www.python.org' -or $uri.Port -ne 443 -or $uri.UserInfo -or $uri.Query -or $uri.Fragment){continue}
        if($uri.AbsolutePath -notmatch '^/ftp/python/3\.12\.\d+/python-3\.12\.\d+-amd64\.zip$'){continue}
        if([string]$item.hash.sha256 -notmatch '^[a-fA-F0-9]{64}$'){continue}
        $valid+=$item
    }
    $selected=$valid | Sort-Object -Property @{Expression={[Version]$_.'sort-version'};Descending=$true} | Select-Object -First 1
    if(-not $selected){throw 'No full stable Python3.12 x64 package with an official SHA256 was found.'}
    return $selected
}

function Test-PythonArchiveHash([string]$Path,[string]$Expected){
    if($Expected -notmatch '^[a-fA-F0-9]{64}$'){throw 'Missing Python archive SHA256.'}
    if(-not (Test-Path -LiteralPath $Path)){return $false}
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -eq $Expected
}

function Expand-PythonArchive([string]$Archive,[string]$Destination){
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $prefix=[IO.Path]::GetFullPath($Destination).TrimEnd('\')+'\'
    $zip=[IO.Compression.ZipFile]::OpenRead($Archive)
    try{
        [long]$total=0
        foreach($entry in $zip.Entries){
            if($entry.FullName.Contains(':') -or [IO.Path]::IsPathRooted($entry.FullName)){throw 'Python archive contains an absolute or stream path.'}
            $candidate=[IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName.Replace('/','\')))
            if(-not $candidate.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Python archive contains a path outside its target.'}
            $total+=$entry.Length
            if($total -gt 800MB){throw 'Python archive exceeds the unpacked size limit.'}
        }
    }finally{$zip.Dispose()}
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive,$Destination)
}

function Install-PrivatePython312([string]$Root){
    if(-not [Environment]::Is64BitOperatingSystem){throw 'This bundle targets 64-bit Windows.'}
    $runtimeRoot=Join-Path ([IO.Path]::GetFullPath($Root)) 'runtime'
    $target=Join-Path $runtimeRoot 'python312'
    $executable=Join-Path $target 'python.exe'
    if(Test-Path -LiteralPath $target){
        if(Test-Path -LiteralPath $executable){
            $probe=& $executable -c "import sys,struct,venv,ensurepip; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')==8 else 1)" 2>$null
            if($LASTEXITCODE -eq 0){return $executable}
        }
        throw ('Existing private runtime is incomplete. It was preserved: '+$target)
    }
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $cache=Join-Path ([IO.Path]::GetFullPath($Root)) '.cache\python-bootstrap'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $sourceLockPath=Join-Path ([IO.Path]::GetFullPath($Root)) 'tools\sources.lock.json'
    $lockedPackage=$null
    if(Test-Path -LiteralPath $sourceLockPath){
        $sourceLock=Get-Content -Raw -Encoding UTF8 -LiteralPath $sourceLockPath | ConvertFrom-Json
        if($sourceLock.python_bootstrap.package){$lockedPackage=Get-Python312Package @($sourceLock.python_bootstrap.package)}
    }
    $url=[Uri]'https://www.python.org/ftp/python/index-windows.json'
    $versions=@()
    for($page=0;$page -lt 3;$page++){
        if($lockedPackage){break}
        Write-Host ('Reading official Python index: '+$url.AbsoluteUri)
        $response=Invoke-WebRequest -UseBasicParsing -Uri $url.AbsoluteUri -TimeoutSec 90
        $feed=$response.Content | ConvertFrom-Json
        if(-not $feed.versions){throw 'Official Python index returned no versions.'}
        $versions+=@($feed.versions)
        if(-not $feed.next){break}
        $next=[Uri]::new($url,[string]$feed.next)
        if($next.Scheme -ne 'https' -or $next.Host -ne 'www.python.org' -or $next.Port -ne 443 -or $next.UserInfo -or $next.Query -or $next.Fragment -or $next.AbsolutePath -notmatch '^/ftp/python/index-windows[^/]*\.json$'){throw 'Python index pagination left the official source.'}
        $url=$next
    }
    $package=if($lockedPackage){$lockedPackage}else{Get-Python312Package $versions}
    $archive=Join-Path $cache ([IO.Path]::GetFileName(([Uri]$package.url).AbsolutePath))
    if(-not (Test-PythonArchiveHash $archive $package.hash.sha256)){
        Write-Host ('Downloading official Python '+$package.'sort-version'+' x64; this may take a few minutes.')
        Save-DependencyDownload $package.url $archive 900
    }
    if(-not (Test-PythonArchiveHash $archive $package.hash.sha256)){throw 'Python download checksum failed. The downloaded program was not executed.'}
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $temporary=Join-Path $runtimeRoot ('.python312-'+[Guid]::NewGuid().ToString('N'))
    Expand-PythonArchive $archive $temporary
    $candidate=Join-Path $temporary 'python.exe'
    if(-not (Test-Path -LiteralPath $candidate)){throw 'Python archive did not contain the full runtime executable.'}
    $actualVersion=& $candidate -c "import sys,struct,venv,ensurepip; assert sys.version_info[:2] == (3,12) and struct.calcsize('P')==8; print(sys.version.split()[0])"
    if($LASTEXITCODE -ne 0 -or [string]$actualVersion -ne [string]$package.'sort-version'){throw 'Downloaded runtime could not verify its version on this Windows installation. Temporary files were retained for diagnosis.'}
    # Verify both absolute directory paths before the directory move.
    $scope=[IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\')+'\'
    foreach($path in @($temporary,$target)){if(-not [IO.Path]::GetFullPath($path).StartsWith($scope,[StringComparison]::OrdinalIgnoreCase)){throw 'Runtime move escaped its intended directory.'}}
    Move-Item -LiteralPath $temporary -Destination $target
    [IO.File]::WriteAllText((Join-Path $target 'coldstart-runtime-source.json'),($package | ConvertTo-Json -Depth 8),[Text.Encoding]::UTF8)
    Write-Host ('Private Python '+$package.'sort-version'+' prepared; system PATH was not changed.')
    return $executable
}
