param([Parameter(Mandatory = $true)][string]$InstallDir)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$launcherPath = Join-Path $root 'Xynigo.exe'
$runtimePrefix = (Join-Path $root 'versions') + '\'
$comparison = [StringComparison]::OrdinalIgnoreCase

function Test-ManagedExecutable([string]$Path) {
    if (-not $Path) { return $false }
    $pathValue = [IO.Path]::GetFullPath($Path)
    return $pathValue.Equals($launcherPath, $comparison) -or (
        $pathValue.StartsWith($runtimePrefix, $comparison) -and
        $pathValue.Substring($runtimePrefix.Length) -match
            '^[^\\]+\\python-embed\\pythonw?\.exe$'
    )
}

function Assert-NoActiveTasks($ProcessEntry) {
    if ($ProcessEntry.Name -notmatch '^pythonw?\.exe$' -or
        $ProcessEntry.CommandLine -notmatch 'run\.py') { return }
    $listeners = @(Get-NetTCPConnection -State Listen -OwningProcess $ProcessEntry.ProcessId -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') })
    foreach ($listener in $listeners) {
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$($listener.LocalPort)/executor-status.json" -TimeoutSec 2
            if ($status.schemaVersion -ne 1 -or $status.cloudOrigin -ne 'https://app.xynigo.com') { continue }
            if ([int]$status.tasks.activeCount -gt 0) {
                throw 'Production executor has active tasks; stop was cancelled.'
            }
            return
        }
        catch {
            if ($_.Exception.Message -like '*active tasks*') { throw }
        }
    }
    throw 'Production executor status is unavailable; stop was cancelled.'
}

# Stop the launcher and any orphaned interpreter belonging to this install.
# A detached Python child is not necessarily covered by taskkill /T.
$deadline = (Get-Date).AddSeconds(10)
do {
    $owned = @(Get-CimInstance Win32_Process -Filter "Name='Xynigo.exe' OR Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { Test-ManagedExecutable $_.ExecutablePath })
    if ($owned.Count -eq 0) { exit 0 }
    # Check every managed runtime before stopping any launcher.  Active tasks
    # must leave the whole installation and its tray process running.
    foreach ($entry in $owned) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.ProcessId)"
        if ($current -and (Test-ManagedExecutable $current.ExecutablePath)) {
            Assert-NoActiveTasks $current
        }
    }
    foreach ($entry in $owned) {
        # CIM can resolve 64-bit executable paths even from the installer's
        # 32-bit PowerShell. Recheck ownership in case the PID was reused.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.ProcessId)"
        if ($current -and (Test-ManagedExecutable $current.ExecutablePath)) {
            $result = Invoke-CimMethod -InputObject $current -MethodName Terminate
            if ($result.ReturnValue -ne 0) {
                $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.ProcessId)"
                if ($remaining -and (Test-ManagedExecutable $remaining.ExecutablePath)) {
                    throw 'A managed Xynigo process could not be stopped.'
                }
            }
        }
    }
    Start-Sleep -Milliseconds 200
} while ((Get-Date) -lt $deadline)

throw 'The installed Xynigo processes did not stop; uninstall was cancelled.'
