#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Setup', 'Start', 'Stop', 'Status', 'InstallStartup', 'RemoveStartup')]
    [string]$Action = 'Status',
    # Used by Task Scheduler so a failed Python process can be restarted.
    [switch]$WaitForExit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ConfigPath = Join-Path $ProjectRoot 'config.toml'
$ScriptPath = Join-Path $ProjectRoot 'scripts\telegram.ps1'
$PowerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$TaskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Action Start -WaitForExit' -f $ScriptPath
$Hasher = [Security.Cryptography.SHA256]::Create()
try {
    $RootHash = ([BitConverter]::ToString($Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant())))).Replace('-', '').Substring(0, 12)
} finally { $Hasher.Dispose() }
$TaskName = 'PolyBOT Telegram ' + $RootHash
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$CurrentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Get-StateDirectory {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw 'Python .venv belum tersedia. Ikuti docs/TELEGRAM.md.'
    }
    # This reads config.toml only. It never loads or prints .env.
    $ReadState = 'import pathlib,sys; from polybot.config import load_config; print((pathlib.Path(sys.argv[1])/load_config(sys.argv[2]).state_dir).resolve())'
    Push-Location -LiteralPath $ProjectRoot
    try {
        $Result = & $PythonPath -c $ReadState $ProjectRoot $ConfigPath 2>$null
        if ($LASTEXITCODE -ne 0 -or @($Result).Count -ne 1 -or -not $Result) {
            throw 'config.toml belum valid. Periksa konfigurasi lokal.'
        }
        return [IO.Path]::GetFullPath([string]$Result)
    } finally { Pop-Location }
}

function Get-OwnedProcess {
    if (-not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) { return $null }
    try {
        $Record = Get-Content -LiteralPath $MetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $ProcessId = [int]$Record.pid
        if ($ProcessId -le 0) { return $null }
        $Candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId"
        if (-not $Candidate) { return $null }
        $Created = $Candidate.CreationDate.ToUniversalTime().ToString('o')
        if ($Created -ne $Record.createdUtc -or
            $Candidate.ExecutablePath -ine $Record.executable -or
            $Candidate.CommandLine -cne $Record.commandLine -or
            $Candidate.ExecutablePath -ine $PythonPath -or
            $Candidate.CommandLine -notmatch '(^|\s)-m\s+polybot\.telegram(\s|$)') {
            return $null
        }
        return $Candidate
    } catch {
        throw 'Metadata proses tidak dapat diverifikasi. Gunakan modul --stop untuk berhenti dengan aman.'
    }
}

function Assert-OwnedTask($Task) {
    $Actions = @($Task.Actions)
    $PrincipalId = [string]$Task.Principal.UserId
    if ($PrincipalId -notmatch '^S-1-') {
        $PrincipalId = ([Security.Principal.NTAccount]::new($PrincipalId)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($Actions.Count -ne 1 -or $Actions[0].Execute -ine $PowerShellPath -or
        $Actions[0].Arguments -cne $TaskArguments -or
        $Actions[0].WorkingDirectory -ine $ProjectRoot -or
        $PrincipalId -ne $CurrentUserSid) {
        throw 'Nama scheduled task sudah dipakai konfigurasi lain; tidak diubah.'
    }
}

function Move-PreviousLog([string]$LogPath) {
    # Start-Process replaces redirects on each start; retain the previous run.
    if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
        Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.previous') -Force
    }
}

try {
    if ($Action -in @('InstallStartup', 'RemoveStartup')) {
        $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($ExistingTask) { Assert-OwnedTask $ExistingTask }
        if ($Action -eq 'RemoveStartup') {
            if ($ExistingTask) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
            Write-Host 'Startup otomatis dihapus. Proses yang sudah berjalan tetap aktif sampai dihentikan.'
            exit 0
        }
        $null = Get-StateDirectory
        $TaskAction = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $TaskArguments -WorkingDirectory $ProjectRoot
        $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
        $Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
        $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $null = Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Layanan Telegram PolyBOT di $ProjectRoot; trading dimulai dari Telegram." -Force
        Write-Host "Startup terpasang: $TaskName. Aktif saat akun Windows ini login; trading tetap menunggu perintah Telegram."
        exit 0
    }

    $StateDirectory = Get-StateDirectory
    $MetadataPath = Join-Path $StateDirectory 'telegram-background.json'
    $StdoutPath = Join-Path $StateDirectory 'telegram.stdout.log'
    $StderrPath = Join-Path $StateDirectory 'telegram.stderr.log'

    if ($Action -eq 'Stop') {
        Push-Location -LiteralPath $ProjectRoot
        try {
            & $PythonPath -m polybot.telegram --config $ConfigPath --stop
            exit $LASTEXITCODE
        } finally { Pop-Location }
    }

    if ($Action -eq 'Status') {
        $Owned = Get-OwnedProcess
        if ($Owned) {
            $StopPending = Test-Path -LiteralPath (Join-Path $StateDirectory 'telegram-service.stop')
            $Phase = 'menghubungkan ke Telegram'
            if (Test-Path -LiteralPath $StdoutPath -PathType Leaf) {
                foreach ($LogLine in (Get-Content -LiteralPath $StdoutPath -Tail 100 -Encoding UTF8)) {
                    try {
                        $Entry = $LogLine | ConvertFrom-Json -ErrorAction Stop
                        if ($Entry.PSObject.Properties['pair_url']) { $Phase = 'menunggu pairing pemilik' }
                        if ($Entry.PSObject.Properties['paired'] -and $Entry.paired) { $Phase = 'pairing selesai, memulai layanan' }
                        if ($Entry.PSObject.Properties['telegram'] -and $Entry.telegram -eq 'running') { $Phase = 'layanan berjalan' }
                    } catch { }
                }
            }
            Write-Host ("Proses Telegram aktif (PID {0}); tahap: {1}; permintaan berhenti: {2}." -f $Owned.ProcessId, $Phase, $StopPending)
            if ($Phase -eq 'layanan berjalan') { Write-Host 'Status trading: kirim /status ke bot Telegram.' }
            else { Write-Host 'Tunggu kesiapan layanan atau selesaikan pairing memakai kode pada log.' }
        } else {
            Write-Host 'Tidak ada proses aktif yang terverifikasi dari helper ini. Proses yang dibuka manual tidak dicatat.'
        }
        Write-Host "Log layanan: $StdoutPath"
        Write-Host "Log error: $StderrPath"
        exit 0
    }

    $Guard = [Threading.Mutex]::new($false, ('Local\PolyBOTTelegram-' + $RootHash))
    $Acquired = $false
    $StartedProcess = $null
    try {
        try { $Acquired = $Guard.WaitOne(10000) }
        catch [Threading.AbandonedMutexException] { $Acquired = $true }
        if (-not $Acquired) { throw 'Helper Telegram lain sedang memulai proses. Coba Status.' }
        $Owned = Get-OwnedProcess
        if ($Owned) {
            Write-Host "Layanan Telegram sudah aktif (PID $($Owned.ProcessId))."
            exit 0
        }
        $null = New-Item -ItemType Directory -Path $StateDirectory -Force
        Move-PreviousLog $StdoutPath
        Move-PreviousLog $StderrPath
        $Arguments = '-u -m polybot.telegram --config "{0}"' -f $ConfigPath
        if ($Action -eq 'Setup') { $Arguments += ' --setup --pair-timeout 86400' }
        $StartedProcess = Start-Process -FilePath $PythonPath -ArgumentList $Arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath -PassThru
        $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($StartedProcess.Id)"
        if (-not $ProcessInfo) { throw 'Layanan selesai sebelum siap. Periksa log lokal.' }
        $Record = [ordered]@{
            pid = $ProcessInfo.ProcessId
            createdUtc = $ProcessInfo.CreationDate.ToUniversalTime().ToString('o')
            executable = $ProcessInfo.ExecutablePath
            commandLine = $ProcessInfo.CommandLine
        }
        $TemporaryMetadata = $MetadataPath + '.tmp'
        $Record | ConvertTo-Json | Set-Content -LiteralPath $TemporaryMetadata -Encoding UTF8
        Move-Item -LiteralPath $TemporaryMetadata -Destination $MetadataPath -Force
        if ($Action -eq 'Setup') {
            Write-Host "Pairing background dimulai (PID $($StartedProcess.Id)). Buka pair_url dari log lalu tekan Start di Telegram."
        } else {
            Write-Host "Proses Telegram dimulai (PID $($StartedProcess.Id)). Paper/live menunggu perintah Anda di Telegram."
        }
        Write-Host "Periksa kesiapan di Telegram atau log: $StdoutPath"
    } finally {
        if ($Acquired) { $Guard.ReleaseMutex() }
        $Guard.Dispose()
    }
    if ($WaitForExit -and $StartedProcess) {
        $StartedProcess.WaitForExit()
        exit $StartedProcess.ExitCode
    }
} catch {
    # No environment values, command line from untrusted processes, or traceback.
    Write-Error 'Helper Telegram gagal. Periksa .venv, config.toml, metadata proses, izin Task Scheduler, dan docs/TELEGRAM.md.' -ErrorAction Continue
    exit 1
}
