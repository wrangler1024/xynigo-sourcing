param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$StageDir,
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [int]$ParentPid = 0,
    [string]$WorkDir = "",
    [string]$StateDir = "",
    [switch]$SkipWait,
    [switch]$NoRestart,
    [int]$TestFailAfterInstall = 0
)

$ErrorActionPreference = "Stop"
$ManagedPaths = @(
    "app", "deps", "python-embed", "run.py", "Xynigo.exe",
    "xynigo-logo.png", "xynigo-x.ico", "启动.bat", "启动-本地执行器.bat",
    "配对本地执行器.bat",
    "update-helper.ps1", "VERSION.json", "使用说明.txt"
)
$LogRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "XynigoSourcing\logs"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot ("update-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

function Write-UpdateLog([string]$Message) {
    $Line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $Line
    Add-Content -Path $LogPath -Value $Line -Encoding UTF8
}

function Invoke-WithRetry([scriptblock]$Action, [string]$Description) {
    $LastError = $null
    for ($Attempt = 1; $Attempt -le 10; $Attempt++) {
        try {
            & $Action
            return
        }
        catch {
            $LastError = $_
            Write-UpdateLog ("{0} 失败，第 {1}/10 次重试：{2}" -f $Description, $Attempt, $_.Exception.Message)
            Start-Sleep -Milliseconds 600
        }
    }
    throw $LastError
}

function Start-Xynigo([string]$Root) {
    if ($NoRestart) { return }
    if ([string]::IsNullOrWhiteSpace($StateDir)) {
        $StateDir = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "XynigoSourcing"
    }
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    Set-Content -LiteralPath (Join-Path $StateDir "skip-update-once") -Value "1" -Encoding ASCII
    $env:XYNIGO_SKIP_UPDATE_ONCE = "1"
    $StatusCenter = Join-Path $Root "Xynigo.exe"
    if (Test-Path -LiteralPath $StatusCenter) {
        Start-Process -FilePath $StatusCenter -ArgumentList @("--show") -WorkingDirectory $Root
        return
    }
    $Launcher = Join-Path $Root "启动.bat"
    if (-not (Test-Path -LiteralPath $Launcher)) {
        Write-UpdateLog "找不到启动.bat，无法自动重启。"
        return
    }
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", ('"{0}"' -f $Launcher)) -WorkingDirectory $Root
}

$MovedPaths = New-Object System.Collections.Generic.List[string]
$InstalledPaths = New-Object System.Collections.Generic.List[string]

try {
    $InstallDir = [IO.Path]::GetFullPath($InstallDir)
    $StageDir = [IO.Path]::GetFullPath($StageDir)
    $BackupDir = [IO.Path]::GetFullPath($BackupDir)
    Write-UpdateLog "准备更新 Xynigo Sourcing。"
    Write-UpdateLog ("安装目录：{0}" -f $InstallDir)

    if (-not $SkipWait -and $ParentPid -gt 0) {
        Write-UpdateLog ("等待旧程序退出，PID={0}" -f $ParentPid)
        $Deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $Deadline) {
            if (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
        }
        if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
            throw "旧程序在 90 秒内未退出"
        }
    }

    foreach ($Name in $ManagedPaths) {
        if (-not (Test-Path -LiteralPath (Join-Path $StageDir $Name))) {
            throw ("更新包缺少受管文件：{0}" -f $Name)
        }
    }

    if (Test-Path -LiteralPath $BackupDir) {
        $BackupDir = "{0}-{1}" -f $BackupDir, (Get-Date -Format "yyyyMMdd-HHmmss")
    }
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    Write-UpdateLog ("当前程序备份目录：{0}" -f $BackupDir)

    foreach ($Name in $ManagedPaths) {
        $Current = Join-Path $InstallDir $Name
        if (Test-Path -LiteralPath $Current) {
            $BackupTarget = Join-Path $BackupDir $Name
            $BackupParent = Split-Path -Parent $BackupTarget
            New-Item -ItemType Directory -Force -Path $BackupParent | Out-Null
            Invoke-WithRetry { Move-Item -LiteralPath $Current -Destination $BackupTarget -Force } ("备份 {0}" -f $Name)
            $MovedPaths.Add($Name)
        }
    }

    $InstalledCount = 0
    foreach ($Name in $ManagedPaths) {
        $Source = Join-Path $StageDir $Name
        $Target = Join-Path $InstallDir $Name
        $InstalledPaths.Add($Name)
        Invoke-WithRetry { Copy-Item -LiteralPath $Source -Destination $Target -Recurse -Force } ("安装 {0}" -f $Name)
        $InstalledCount++
        if ($TestFailAfterInstall -gt 0 -and $InstalledCount -ge $TestFailAfterInstall) {
            throw "测试注入：模拟替换失败"
        }
    }

    Write-UpdateLog "更新安装成功，用户配置和本地数据未被修改。"
    Start-Xynigo $InstallDir
    exit 0
}
catch {
    Write-UpdateLog ("更新失败，开始回滚：{0}" -f $_.Exception.Message)
    try {
        foreach ($Name in $InstalledPaths) {
            $Target = Join-Path $InstallDir $Name
            if (Test-Path -LiteralPath $Target) {
                Invoke-WithRetry { Remove-Item -LiteralPath $Target -Recurse -Force } ("清理新版本 {0}" -f $Name)
            }
        }
        foreach ($Name in $MovedPaths) {
            $BackupSource = Join-Path $BackupDir $Name
            if (Test-Path -LiteralPath $BackupSource) {
                $RestoreTarget = Join-Path $InstallDir $Name
                Invoke-WithRetry { Move-Item -LiteralPath $BackupSource -Destination $RestoreTarget -Force } ("恢复 {0}" -f $Name)
            }
        }
        Write-UpdateLog "回滚完成，正在重新启动原版本。"
        Start-Xynigo $InstallDir
    }
    catch {
        Write-UpdateLog ("回滚失败，需要人工从备份目录恢复：{0}" -f $_.Exception.Message)
    }
    exit 1
}
