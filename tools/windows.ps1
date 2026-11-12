param(
    [ValidateSet('Check','BootstrapPython','Install','Setup','Configure','Login','Start','Auto','Pause','Resume','Stop','Diagnose','Test','Backup','EnableStartup','DisableStartup')][string]$Action='Check',
    [ValidateSet('xiaohongshu')][string[]]$Channels=@('xiaohongshu'),
    [string]$PythonPath,
    [string]$StartupDirectory,
    [switch]$UsePrivatePython,
    [switch]$Interactive,
    [switch]$PlanOnly,
    [switch]$NoBrowser
)
$ErrorActionPreference='Stop'
$OutputEncoding=New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding=$OutputEncoding
$env:PYTHONIOENCODING='utf-8'
$bundleRoot=[IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$appRoot=Join-Path $bundleRoot 'coldstart'
$dataRoot=Join-Path $appRoot 'data'
$runRoot=Join-Path $bundleRoot 'run'
$logRoot=Join-Path $bundleRoot 'logs'
$xhsRoot=Join-Path $appRoot 'adapters\xiaohongshu-mcp'
$corePython=Join-Path $appRoot '.venv\Scripts\python.exe'
$lock=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot 'sources.lock.json') | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'python-bootstrap.ps1')
if($PlanOnly){
    [pscustomobject]@{action=$Action;root=$bundleRoot;channels=$Channels;downloads=$false;startsProcesses=$false;publishes=$false;requiresPython='Core >=3.11';requiresGit=$false;sourceLock=$lock} | ConvertTo-Json -Depth 8
    exit 0
}

function Assert-Channels {
    foreach($channel in $Channels){if($channel -notin @('xiaohongshu')){throw 'Unsupported channel.'}}
    if($Channels.Count -eq 0){throw 'Select at least one channel.'}
}
function Invoke-Checked([string]$Executable,[string[]]$Arguments){
    & $Executable @Arguments | Out-Host
    if($LASTEXITCODE -ne 0){throw ('Command failed. Exit code: '+$LASTEXITCODE)}
}
function Find-Python([bool]$Require312){
    $candidates=New-Object System.Collections.Generic.List[string]
    if($PythonPath){$candidates.Add($PythonPath)}
    if(Test-Path -LiteralPath $corePython){$candidates.Add($corePython)}
    $privatePython=Join-Path $bundleRoot 'runtime\python312\python.exe'
    if(Test-Path -LiteralPath $privatePython){$candidates.Add($privatePython)}
    foreach($hive in @('HKCU:\Software\Python','HKLM:\Software\Python','HKLM:\Software\WOW6432Node\Python')){
        foreach($entry in @(Get-Item -Path ($hive+'\*\*\InstallPath') -ErrorAction SilentlyContinue)){
            try{
                $exe=$entry.GetValue('ExecutablePath')
                if($exe){$candidates.Add([string]$exe)}
                $directory=$entry.GetValue('')
                if($directory){$candidates.Add((Join-Path ([string]$directory) 'python.exe'))}
            }catch{continue}
        }
    }
    foreach($versionName in @('311','312','313','314','315','316')){
        foreach($prefix in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'),$env:ProgramFiles,'C:\')){
            if($prefix){$candidates.Add((Join-Path $prefix ('Python'+$versionName+'\python.exe')))}
        }
    }
    $launcher=Get-Command py -ErrorAction SilentlyContinue
    if($launcher){
        try{
            $inventory=& $launcher.Source -0p 2>$null
            foreach($line in $inventory){if($line -match '(?i)([A-Z]:\\.*?python.exe)\s*$'){$candidates.Add($matches[1].Trim())}}
        }catch{}
    }
    $pythonCommand=Get-Command python -ErrorAction SilentlyContinue
    if($pythonCommand -and $pythonCommand.Source -notlike '*WindowsApps*'){$candidates.Add($pythonCommand.Source)}
    foreach($candidate in $candidates){
        if(-not (Test-Path -LiteralPath $candidate)){continue}
        try{
            $probe=& $candidate -c "import sys,struct; print(str(sys.version_info[0])+'.'+str(sys.version_info[1])+'|'+str(struct.calcsize('P')*8))" 2>$null
            $version=([string]$probe).Split('|')[0]
            if($LASTEXITCODE -eq 0 -and ([string]$probe).EndsWith('|64') -and (($Require312 -and $version -eq '3.12') -or (-not $Require312 -and $version -match '^3\.(1[1-9]|[2-9][0-9])$'))){return [IO.Path]::GetFullPath($candidate)}
        }catch{}
    }
    return $null
}
function Ensure-Core {
    if(-not (Test-Path -LiteralPath $corePython)){throw 'Run 01 Install first. The project virtual environment is missing.'}
    return $corePython
}
function Invoke-Ops([string[]]$Arguments){
    $python=Ensure-Core
    Invoke-Checked $python (@((Join-Path $appRoot 'ops.py'),'--data-dir',$dataRoot)+$Arguments)
}
function Get-Settings {
    $python=Ensure-Core
    $raw=& $python (Join-Path $appRoot 'ops.py') --data-dir $dataRoot status
    if($LASTEXITCODE -ne 0){throw 'Unable to read local status.'}
    return ($raw -join "`n") | ConvertFrom-Json
}
function Download-Asset($Asset){
    if($Asset.name -notmatch '^[a-z0-9-]+\.exe$' -or $Asset.sha256 -notmatch '^[a-f0-9]{64}$'){throw 'Invalid locked asset.'}
    $uri=[Uri]$Asset.url
    if($uri.Scheme -ne 'https' -or $uri.Host -ne 'github.com' -or $uri.AbsolutePath -notlike '/xpzouying/xiaohongshu-mcp/releases/download/*'){throw 'Unexpected asset source.'}
    New-Item -ItemType Directory -Path $xhsRoot -Force | Out-Null
    $target=Join-Path $xhsRoot $Asset.name
    if(Test-Path -LiteralPath $target){
        if((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Asset.sha256){Write-Host ('Already verified: '+$Asset.name);return}
        throw ('Existing executable differs from the source lock. It was preserved: '+$target)
    }
    $temporary=$target+'.download'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    Write-Host ('Downloading official asset: '+$Asset.name)
    Save-DependencyDownload $Asset.url $temporary 900
    if((Get-Item -LiteralPath $temporary).Length -ne $Asset.bytes -or (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Asset.sha256){throw 'Download checksum failed. The executable was not installed.'}
    # Both checked absolute paths are within this bundle's adapter directory.
    $resolvedRoot=[IO.Path]::GetFullPath($xhsRoot).TrimEnd('\')+'\'
    foreach($candidate in @($temporary,$target)){if(-not ([IO.Path]::GetFullPath($candidate).StartsWith($resolvedRoot,[StringComparison]::OrdinalIgnoreCase))){throw 'Asset target escaped the adapter directory.'}}
    Move-Item -LiteralPath $temporary -Destination $target
}
function Get-Record([string]$Name){
    $path=Join-Path $runRoot ($Name+'.json')
    if(-not (Test-Path -LiteralPath $path)){return $null}
    try{return Get-Content -Raw -Encoding UTF8 -LiteralPath $path | ConvertFrom-Json}catch{Write-Host ('Unreadable process record ignored; no process was stopped: '+$Name);return $null}
}
function Get-OwnedProcess([string]$Name){
    $record=Get-Record $Name
    if(-not $record){return $null}
    $process=Get-Process -Id ([int]$record.processId) -ErrorAction SilentlyContinue
    if(-not $process){return $null}
    if($process.StartTime.ToUniversalTime().Ticks.ToString() -ne [string]$record.startTicks){Write-Host ('Stale PID record ignored; no process was stopped: '+$Name);return $null}
    if([IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath([string]$record.executable)){Write-Host ('Different process identity ignored; no process was stopped: '+$Name);return $null}
    return $process
}
function Save-Record([string]$Name,$Process,[string]$Executable){
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    $record=[ordered]@{processId=$Process.Id;startTicks=$Process.StartTime.ToUniversalTime().Ticks.ToString();executable=[IO.Path]::GetFullPath($Executable)}
    [IO.File]::WriteAllText((Join-Path $runRoot ($Name+'.json')),($record | ConvertTo-Json),[Text.Encoding]::UTF8)
}
function Start-Owned([string]$Name,[string]$Executable,[string[]]$Arguments,[string]$WorkingDirectory){
    $existing=Get-OwnedProcess $Name
    if($existing){return $existing}
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $quoted=foreach($argument in $Arguments){if($argument.Contains('"')){throw 'Unexpected quote in native argument.'};'"'+$argument+'"'}
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
    $options=@{FilePath=$Executable;WorkingDirectory=$WorkingDirectory;PassThru=$true;WindowStyle='Hidden';RedirectStandardOutput=(Join-Path $logRoot ($Name+'-'+$stamp+'.out.log'));RedirectStandardError=(Join-Path $logRoot ($Name+'-'+$stamp+'.error.log'))}
    if($quoted){$options.ArgumentList=$quoted -join ' '}
    $process=Start-Process @options
    Start-Sleep -Milliseconds 800
    $process.Refresh()
    if($process.HasExited){throw ('Process exited during startup. Read logs: '+$logRoot)}
    Save-Record $Name $process $Executable
    return $process
}
function Start-All {
    $python=Ensure-Core
    $already=Get-OwnedProcess 'backend'
    if($already){
        $existingReady=Join-Path $runRoot 'backend-ready.json'
        if(-not (Test-Path -LiteralPath $existingReady)){throw 'Owned host is recovering; backend is not ready yet. No second instance was started.'}
        $existingState=Get-Content -Raw -Encoding UTF8 -LiteralPath $existingReady | ConvertFrom-Json
        if($existingState.version -ne $lock.bundle_version){throw 'An earlier version is still running. Run 07 Stop, then 03 Start before using this update.'}
        Write-Host 'Workbench is already running.'
        if(-not $NoBrowser){Start-Process 'http://127.0.0.1:18796'}
        return
    }
    if(-not (Test-FreeLocalPort 18796)){throw 'Port 18796 is unavailable. Nothing was killed.'}
    Invoke-Ops @('control','prepare_start')
    $settings=Get-Settings
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $stopFile=Join-Path $runRoot 'backend-stop.flag'
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    if(Test-Path -LiteralPath $stopFile){Remove-Item -LiteralPath $stopFile}
    $hostArgs=@('-u',(Join-Path $appRoot 'managed_host.py'),'--cwd',$appRoot,'--log',(Join-Path $logRoot ('backend-service-'+$stamp+'.log')),'--restart','--stop-file',$stopFile)
    if('xiaohongshu' -in $settings.channels -and $settings.manage_local_xhs){
        foreach($asset in $lock.assets){$file=Join-Path $xhsRoot $asset.name;if(-not (Test-Path -LiteralPath $file) -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256){throw 'Xiaohongshu adapter is missing or changed. Run Install first.'}}
        if(-not (Test-FreeLocalPort 18060)){throw 'Port 18060 is unavailable. Nothing was killed.'}
        Invoke-Ops @('prepare-local-auth')
        $hostArgs+=@('--related-auth-from-data')
        $hostArgs+=@('--related-exe',(Join-Path $xhsRoot 'xiaohongshu-mcp-windows-amd64.exe'),'--related-cwd',$xhsRoot,'--related-log',(Join-Path $logRoot ('xhs-service-'+$stamp+'.log')),'--related-arg=-port','--related-arg=127.0.0.1:18060')
    }
    $readyFile=Join-Path $runRoot 'backend-ready.json'
    if(Test-Path -LiteralPath $readyFile){Remove-Item -LiteralPath $readyFile}
    $hostArgs+=@('--',$python,'-u',(Join-Path $appRoot 'app.py'),'--no-browser','--data-dir',$dataRoot,'--ready-file',$readyFile)
    $hostProcess=Start-Owned 'backend' $python $hostArgs $appRoot
    $deadline=(Get-Date).AddSeconds(12)
    while(-not (Test-Path -LiteralPath $readyFile) -and (Get-Date) -lt $deadline){$hostProcess.Refresh();if($hostProcess.HasExited){throw 'Workbench startup failed. Read logs.'};Start-Sleep -Milliseconds 200}
    if(-not (Test-Path -LiteralPath $readyFile)){throw 'No readiness record yet. Read status and logs; no second instance was started.'}
    $ready=Get-Content -Raw -Encoding UTF8 -LiteralPath $readyFile | ConvertFrom-Json
    if($ready.port -ne 18796 -or [IO.Path]::GetFullPath([string]$ready.data_dir) -ne $dataRoot -or $ready.version -ne $lock.bundle_version){throw 'Readiness record does not match this bundle.'}
    Write-Host 'Started managed processes. This does not verify login or publication.'
    if(-not $NoBrowser){Start-Process 'http://127.0.0.1:18796'}
}
function Test-FreeLocalPort([int]$Port){
    $listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Parse('127.0.0.1'),$Port)
    $listener.Server.ExclusiveAddressUse=$true
    try{$listener.Start();return $true}catch [Net.Sockets.SocketException]{return $false}finally{$listener.Stop()}
}
function Stop-All {
    $backend=Get-OwnedProcess 'backend'
    if($backend){
        New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $runRoot 'backend-stop.flag'),'stop',[Text.Encoding]::UTF8)
        if(Test-Path -LiteralPath (Join-Path $dataRoot 'coldstart.sqlite3')){try{Invoke-Ops @('control','shutdown')}catch{Write-Host 'Stop flag saved; waiting for the owned host to stop.'}}
        $deadline=(Get-Date).AddSeconds(30)
        while((Get-Date) -lt $deadline){$backend.Refresh();if($backend.HasExited){break};Start-Sleep -Milliseconds 500}
        $backend.Refresh()
        if(-not $backend.HasExited){Write-Host 'Shutdown requested. The current task is still finishing; its host will close related services when it completes. Nothing was forced to stop.';return $false}
    }elseif(Test-Path -LiteralPath (Join-Path $dataRoot 'coldstart.sqlite3')){
        Invoke-Ops @('control','prepare_start')
    }
    $xhs=Get-OwnedProcess 'xhs'
    if($xhs){Stop-Process -Id $xhs.Id -ErrorAction Stop}
    Write-Host 'Managed workbench stopped. Other applications were not stopped.'
    return $true
}

function Prepare-Browser {
    $bridge=Join-Path $appRoot 'xhs-session-windows-amd64.exe'
    if(-not (Test-Path -LiteralPath $bridge) -or (Get-FileHash -LiteralPath $bridge -Algorithm SHA256).Hash.ToLowerInvariant() -ne $lock.creator_bridge_sha256){throw 'Browser preparation tool is missing or differs from the source lock.'}
    Write-Host 'Preparing Chromium (first install may download approximately 200 MB). No account login is opened.'
    $raw=& $bridge --mode install-browser --timeout 1800
    if($LASTEXITCODE -ne 0){throw 'Chromium preparation failed. Check your network and run Install again.'}
    $result=($raw -join "`n") | ConvertFrom-Json
    if($result.mode -ne 'install-browser' -or $result.stage -ne 'browser_ready' -or -not (Test-Path -LiteralPath $result.browser_path)){throw 'Chromium preparation returned an invalid result.'}
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $logRoot 'browser-dependency.json'),($result | ConvertTo-Json -Depth 6),[Text.Encoding]::UTF8)
    Write-Host 'Chromium dependency is ready.'
}
function Install-All {
            if((Test-Path -LiteralPath $corePython) -and (Test-Path -LiteralPath (Join-Path $dataRoot 'coldstart.sqlite3'))){if(-not (Stop-All)){throw 'Wait for the current task to finish before installation.'}}
            $installTemp=Join-Path $bundleRoot '.cache\install-temp'
            New-Item -ItemType Directory -Path $installTemp -Force | Out-Null
            $env:TEMP=$installTemp
            $env:TMP=$installTemp
            $python=if($UsePrivatePython){Install-PrivatePython312 $bundleRoot}else{Find-Python $false}
            if(-not $python){$python=Install-PrivatePython312 $bundleRoot}
            if(-not (Test-Path -LiteralPath $corePython)){Invoke-Checked $python @('-m','venv',(Join-Path $appRoot '.venv'))}
            Invoke-Checked $corePython @('-c','import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)')
            Invoke-Checked $corePython @('-m','pip','install','-r',(Join-Path $appRoot 'requirements.txt'))
            if('xiaohongshu' -in $Channels){foreach($asset in $lock.assets){Download-Asset $asset}}
            Prepare-Browser
            Invoke-Checked $corePython @('-m','pip','check')
            $cfg=@('configure','--channels',($Channels -join ','));if($Interactive){$cfg+='--interactive'}
            Invoke-Ops $cfg
            Write-Host 'Python, Pillow, pinned Xiaohongshu services and Chromium are ready. Configure your API and login before use.'
}

try{
    Assert-Channels
    switch($Action){
        'Check'{
            $python=Find-Python $true
            [pscustomobject]@{pythonCore=(Find-Python $false);python312=$python;coreEnvironment=(Test-Path -LiteralPath $corePython);offlineBundle=$false;next=$(if(Test-Path -LiteralPath $corePython){'Environment exists. Use 02 to login, then 03 to start; Check itself does not verify accounts.'}else{'Run 01 Install. Missing Python will be prepared privately from python.org; Check itself does not install.'})} | ConvertTo-Json
        }
        'BootstrapPython'{
            $python=Find-Python $true
            if(-not $python){$python=Install-PrivatePython312 $bundleRoot}
            Write-Host ('Python3.12 available: '+$python)
        }
        'Install'{Install-All}
        'Setup'{Install-All;Start-All}
        'Configure'{if(-not (Stop-All)){throw 'Wait for shutdown before configuration.'};Invoke-Ops @('configure','--interactive','--replace-key')}
        'Login'{
            if(-not (Stop-All)){throw 'Wait for shutdown before login.'}
            $settings=Get-Settings
            Invoke-Checked $corePython @((Join-Path $appRoot 'xhs_session_login.py'),'--data-dir',$dataRoot)
            Write-Host 'Login commands completed. They do not prove publication succeeded.'
        }
        'Start'{Start-All}
        'Auto'{
            Start-All
            $body='{"action":"start"}'
            $result=Invoke-RestMethod -Uri 'http://127.0.0.1:18796/api/public-control' -Method Post -ContentType 'application/json' -Headers @{'X-Workbench'='local'} -Body $body -TimeoutSec 300
            if(-not $result.ok){throw 'Public automation did not confirm startup. Check the workbench; do not retry publishing.'}
            Write-Host 'Public comments, public replies and publishing are enabled. Private messages are excluded.'
        }
        'Pause'{Invoke-Ops @('control','pause')}
        'Resume'{Invoke-Ops @('control','resume')}
        'Stop'{$null=Stop-All}
        'Diagnose'{
            New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
            Invoke-Ops @('status','--output',(Join-Path $logRoot 'diagnostics.json'))
            Write-Host ('Managed backend running: '+[bool](Get-OwnedProcess 'backend'))
            Write-Host 'Related services are owned by the managed host; process state is not a login check.'
            Write-Host 'This checks local configuration. API and account verification are NOT implied.'
        }
        'Test'{if(-not (Stop-All)){throw 'Wait for shutdown before tests.'};$python=Ensure-Core;Push-Location -LiteralPath $appRoot;try{Invoke-Checked $python @('selftest.py')}finally{Pop-Location}}
        'Backup'{if(-not (Stop-All)){throw 'Wait for shutdown before backup.'};Invoke-Ops @('backup','--destination',(Join-Path $bundleRoot 'backups'))}
        'EnableStartup'{
            $startup=if($StartupDirectory){[IO.Path]::GetFullPath($StartupDirectory)}else{[Environment]::GetFolderPath('Startup')}
            if(-not $startup){throw 'User Startup folder is unavailable.'}
            New-Item -ItemType Directory -Path $startup -Force | Out-Null
            $hash=[Security.Cryptography.SHA256]::Create();try{$suffix=([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($bundleRoot)))).Replace('-','').Substring(0,10)}finally{$hash.Dispose()}
            $file=Join-Path $startup ('Coldstart-'+$suffix+'.lnk')
            $script=Join-Path $PSScriptRoot 'windows.ps1'
            $shell=New-Object -ComObject WScript.Shell
            $shortcut=$shell.CreateShortcut($file)
            $shortcut.TargetPath=Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $shortcut.Arguments='-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+$script+'" -Action Start -NoBrowser'
            $shortcut.WorkingDirectory=$bundleRoot
            $shortcut.WindowStyle=7
            $shortcut.Save()
            Write-Host 'Enabled startup after this user signs in. This is not a Windows service. Keep the bundle at its current path.'
        }
        'DisableStartup'{
            $startup=if($StartupDirectory){[IO.Path]::GetFullPath($StartupDirectory)}else{[Environment]::GetFolderPath('Startup')}
            if(-not $startup){throw 'User Startup folder is unavailable.'}
            $hash=[Security.Cryptography.SHA256]::Create();try{$suffix=([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($bundleRoot)))).Replace('-','').Substring(0,10)}finally{$hash.Dispose()}
            $file=Join-Path $startup ('Coldstart-'+$suffix+'.lnk')
            if(Test-Path -LiteralPath $file){Remove-Item -LiteralPath $file}
            Write-Host 'Disabled the startup entry for this bundle path.'
        }
    }
    exit 0
}catch{Write-Host ('FAILED: '+$_.Exception.Message) -ForegroundColor Red;exit 1}
