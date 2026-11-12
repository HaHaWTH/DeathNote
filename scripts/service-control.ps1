param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('status', 'start', 'stop', 'restart')]
    [string] $Action,
    [Parameter(Mandatory = $true)]
    [string] $Runtime,
    [Parameter(Mandatory = $true)]
    [string] $RuntimeImage,
    [switch] $Elevated
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$state = Join-Path $root '.native-server'
$runtime = [IO.Path]::GetFullPath($Runtime)
$runtimeImage = [IO.Path]::GetFullPath($RuntimeImage)
$entry = [IO.Path]::GetFullPath((Join-Path $root 'native_server.py'))
$launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'spawn_service.py'))
$pidFile = Join-Path $state 'service.pid'
$identityFile = Join-Path $state 'service.process.json'
$resultFile = Join-Path $state 'control-result.json'

function Save-StateFile([string] $Path, [string] $Text) {
    $target = [IO.Path]::GetFullPath($Path)
    if (-not $target.StartsWith($state + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'state_path_invalid'
    }
    $temporary = $target + '.new'
    [IO.File]::WriteAllText($temporary, $Text, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $target -Force
}

function Read-TrackedProcess {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        return @{ running = $false; pid = $null; issue = $null; process = $null }
    }
    $trackedId = 0
    $text = [IO.File]::ReadAllText($pidFile).Trim()
    if (-not [int]::TryParse($text, [ref] $trackedId) -or $trackedId -le 0) {
        return @{ running = $false; pid = $null; issue = 'pid_mismatch'; process = $null }
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $trackedId"
    if ($null -eq $process) {
        return @{ running = $false; pid = $null; issue = $null; process = $null }
    }
    $identity = $null
    if (Test-Path -LiteralPath $identityFile -PathType Leaf) {
        try { $identity = [IO.File]::ReadAllText($identityFile) | ConvertFrom-Json } catch { $identity = $null }
    }
    $ticks = $process.CreationDate.ToUniversalTime().Ticks.ToString()
    if ($null -ne $identity -and $identity.pid -eq $trackedId -and $identity.creationTicks -ne $ticks) {
        return @{ running = $false; pid = $null; issue = 'creation_mismatch'; process = $null }
    }
    $scriptToken = '(?:"' + [regex]::Escape($entry) + '"|' + [regex]::Escape($entry) + ')'
    $commandMatches = $process.CommandLine -match ('(?i)(?:^|\s)' + $scriptToken + '\s+run(?:\s|$)')
    $executableMatches = $process.ExecutablePath -and (
        [IO.Path]::GetFullPath($process.ExecutablePath).Equals($runtime, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFullPath($process.ExecutablePath).Equals($runtimeImage, [StringComparison]::OrdinalIgnoreCase))
    if (-not ($commandMatches -and $executableMatches)) {
        $recordedMatch = $Action -eq 'status' -and $null -ne $identity -and
                         $identity.pid -eq $trackedId -and $identity.creationTicks -eq $ticks -and
                         $identity.runtime -eq $runtime -and $identity.entry -eq $entry
        if (-not $recordedMatch) {
            return @{ running = $false; pid = $null; issue = 'pid_mismatch'; process = $null }
        }
    }
    return @{ running = $true; pid = $trackedId; issue = $null; process = $process }
}

function Public-State($Tracked) {
    return @{ ok = $true; running = [bool] $Tracked.running; pid = $Tracked.pid; processIssue = $Tracked.issue }
}

function Stop-Native {
    $tracked = Read-TrackedProcess
    if ($tracked.issue) { throw $tracked.issue }
    if ($tracked.running) {
        $process = Get-Process -Id $tracked.pid -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            try {
                [void] $process.Handle
                $actual = $process.StartTime.ToUniversalTime().ToString('yyyyMMddHHmmssffffff')
                $expected = $tracked.process.CreationDate.ToUniversalTime().ToString('yyyyMMddHHmmssffffff')
                if ($actual -ne $expected) { throw 'creation_mismatch' }
                $process.Kill()
                $process.WaitForExit()
            } finally { $process.Dispose() }
        }
    }
    if (Test-Path -LiteralPath $pidFile -PathType Leaf) { Remove-Item -LiteralPath $pidFile -Force }
    if (Test-Path -LiteralPath $identityFile -PathType Leaf) { Remove-Item -LiteralPath $identityFile -Force }
}

function Start-Native {
    $tracked = Read-TrackedProcess
    if ($tracked.issue) { throw $tracked.issue }
    if ($tracked.running) { return }
    if (-not (Test-Path -LiteralPath $runtime -PathType Leaf) -or -not (Test-Path -LiteralPath $entry -PathType Leaf) -or
        -not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw 'runtime_missing'
    }
    $upstream = '1.1.1.1'
    $configurationPath = Join-Path $state 'config.json'
    if (Test-Path -LiteralPath $configurationPath -PathType Leaf) {
        $configuration = [IO.File]::ReadAllText($configurationPath) | ConvertFrom-Json
        if ($configuration.dnsUpstream) {
            $resolverAddress = $null
            if (-not [Net.IPAddress]::TryParse([string] $configuration.dnsUpstream, [ref] $resolverAddress)) {
                throw 'dns_upstream_invalid'
            }
            $upstream = $resolverAddress.ToString()
        }
    }
    $launchOutput = & $runtime -E -s -B -X utf8 $launcher --dns-upstream $upstream
    if ($LASTEXITCODE -ne 0) { throw 'startup_exited' }
    try { $launched = ($launchOutput -join "`n") | ConvertFrom-Json } catch { throw 'startup_exited' }
    $nativePid = 0
    if (-not [int]::TryParse([string] $launched.pid, [ref] $nativePid) -or $nativePid -le 0) { throw 'startup_exited' }
    $process = Get-Process -Id $nativePid -ErrorAction SilentlyContinue
    if ($null -eq $process) { throw 'startup_exited' }
    try {
        $native = Get-CimInstance Win32_Process -Filter "ProcessId = $nativePid"
        if ($null -eq $native) { throw 'startup_exited' }
        $identity = @{ pid = $nativePid; creationTicks = $native.CreationDate.ToUniversalTime().Ticks.ToString(); runtime = $runtime; entry = $entry }
        Save-StateFile $identityFile ($identity | ConvertTo-Json -Compress)
        Save-StateFile $pidFile $nativePid.ToString()
        if ($process.WaitForExit(500)) { throw 'startup_exited' }
    } finally { $process.Dispose() }
}

$result = $null
$mutex = $null
$locked = $false
try {
    if ($Action -eq 'status') {
        $result = Public-State (Read-TrackedProcess)
    } else {
        if (-not (Test-Path -LiteralPath $state -PathType Container)) { [void] [IO.Directory]::CreateDirectory($state) }
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            if ($Elevated) { throw 'uac_cancelled' }
            if (Test-Path -LiteralPath $resultFile -PathType Leaf) { Remove-Item -LiteralPath $resultFile -Force }
            $powerShell = Join-Path $PSHOME 'powershell.exe'
            $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Action ' + $Action +
                         ' -Runtime "' + $runtime + '" -RuntimeImage "' + $runtimeImage + '" -Elevated'
            try {
                $helper = Start-Process -FilePath $powerShell -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru
                $helper.WaitForExit()
            } catch { throw 'uac_cancelled' }
            if ($helper.ExitCode -ne 0 -and -not (Test-Path -LiteralPath $resultFile -PathType Leaf)) { throw 'control_failed' }
            $result = [IO.File]::ReadAllText($resultFile) | ConvertFrom-Json
        } else {
            $hasher = [Security.Cryptography.SHA256]::Create()
            try { $digest = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($root))).Replace('-', '').Substring(0, 20) }
            finally { $hasher.Dispose() }
            $mutex = New-Object Threading.Mutex($false, ('Local\DeathNote_' + $digest))
            try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
            if (-not $locked) { throw 'control_busy' }
            if ($Action -eq 'stop' -or $Action -eq 'restart') { Stop-Native }
            if ($Action -eq 'start' -or $Action -eq 'restart') { Start-Native }
            $result = Public-State (Read-TrackedProcess)
        }
    }
} catch {
    $known = @('pid_mismatch', 'creation_mismatch', 'control_busy', 'uac_cancelled', 'startup_exited', 'runtime_missing', 'dns_upstream_invalid')
    $code = $_.Exception.Message
    if ($code -notin $known) { $code = 'control_failed' }
    $result = @{ ok = $false; running = $false; pid = $null; code = $code }
} finally {
    if ($locked) { [void] $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}

if ($Elevated) { Save-StateFile $resultFile ($result | ConvertTo-Json -Compress) }
$result | ConvertTo-Json -Compress | Write-Output
if (-not $result.ok) { exit 1 }
