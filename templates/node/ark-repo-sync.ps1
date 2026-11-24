<#
.SYNOPSIS
    NoahsArk 仓库同步与版本门禁（A→C 双通道）。

.DESCRIPTION
    A：git clone/fetch + checkout 锚定 commit（HTTP/1.1、浅克隆、重试 3 次）
    C：A 失败时从 OSS 下载完整 bundle，校验 sha256 后 git clone 本地

    目录：<Workspace>\repos\<repo>

    退出码：0 通过；2 A/C 均失败；4 索引不可读；5 缺少 git
#>
[CmdletBinding()]
param(
    [string]$Workspace = "D:\zhsh\workspace",
    [string[]]$Repo = @("lstest", "ark-kb", "burner", "ark-node"),
    [string]$IndexUrl = "https://listenai-test-internal.oss-cn-beijing.aliyuncs.com/noahsark/index.json",
    [string]$RepoBaseUrl = "https://listenai-test-internal.oss-cn-beijing.aliyuncs.com/noahsark/repos",
    [switch]$CheckOnly,
    [switch]$AllowClone,
    [switch]$InstallTask
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$logDir = Join-Path $Workspace "logs"
$null = New-Item -ItemType Directory -Path $logDir -Force
$logPath = Join-Path $logDir "repo-sync.log"

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { Write-Log "WARN git-not-installed, will use D channel (tarball)" }
$gitExe = if ($git) { $git.Source } else { $null }

try {
    $index = (Invoke-WebRequest -Uri $IndexUrl -UseBasicParsing -TimeoutSec 30).Content | ConvertFrom-Json
} catch {
    Write-Log ("FATAL index-unreadable: " + $_.Exception.Message)
    exit 4
}

$entries = @{}
foreach ($item in $index.items) { if ($item.id) { $entries[[string]$item.id] = $item } }

$reposRoot = Join-Path $Workspace "repos"
$cacheRoot = Join-Path $reposRoot "_cache"
$null = New-Item -ItemType Directory -Path $reposRoot -Force
$null = New-Item -ItemType Directory -Path $cacheRoot -Force

function Invoke-Git([string[]]$GitArgs) {
    & $gitExe @GitArgs 2>&1 | Out-Null
    return $LASTEXITCODE
}

function Try-ChannelA([string]$path, [string]$url, [string]$pinned) {
    $common = @("-c", "http.version=HTTP/1.1", "-c", "http.postBuffer=524288000")
    for ($i = 1; $i -le 3; $i++) {
        if (-not (Test-Path -LiteralPath (Join-Path $path ".git"))) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
            $rc = Invoke-Git ($common + @("clone", "--depth", "1", "--quiet", $url, $path))
        } else {
            $rc = Invoke-Git (@("-C", $path, "fetch", "--depth", "1", "--quiet", "origin", $pinned))
            if ($rc -eq 0) {
                $rc = Invoke-Git (@("-C", $path, "-c", "advice.detachedHead=false", "checkout", "--quiet", $pinned))
            }
        }
        if ($rc -eq 0) {
            $head = (& $gitExe -C $path rev-parse HEAD).Trim()
            if ($head -eq $pinned) { return $true }
        }
        Write-Log "  A try$i failed rc=$rc"
        Start-Sleep -Seconds (3 * $i)
    }
    return $false
}

function Try-ChannelD([string]$name, [string]$path, [string]$pinned) {
    # 无 git 节点：下载 tarball 解压，用 .ark_version 记录版本
    $metaUrl = "$RepoBaseUrl/$name/latest.json"
    try {
        $meta = (Invoke-WebRequest -Uri $metaUrl -UseBasicParsing -TimeoutSec 30).Content | ConvertFrom-Json
    } catch {
        Write-Log "  D meta-unavailable: $metaUrl"
        return $false
    }
    if ([string]$meta.commit -ne $pinned) {
        Write-Log ("  D meta-commit-mismatch meta=" + [string]$meta.commit + " pinned=" + $pinned)
        return $false
    }
    if (-not $meta.tarball) { Write-Log "  D no-tarball-in-meta"; return $false }
    $tarCache = Join-Path $cacheRoot ([string]$meta.tarball)
    if (-not (Test-Path -LiteralPath $tarCache)) {
        try {
            Invoke-WebRequest -Uri "$RepoBaseUrl/$name/$($meta.tarball)" -OutFile $tarCache -UseBasicParsing -TimeoutSec 180 | Out-Null
        } catch {
            Write-Log ("  D download-failed: " + $_.Exception.Message)
            return $false
        }
    }
    $actual = (Get-FileHash -LiteralPath $tarCache -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne [string]$meta.tarball_sha256) {
        Write-Log "  D sha-mismatch"
        Remove-Item -LiteralPath $tarCache -Force -ErrorAction SilentlyContinue
        return $false
    }
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
    $null = New-Item -ItemType Directory -Path $path -Force
    $tarExe = (Get-Command tar -ErrorAction SilentlyContinue)
    if (-not $tarExe) {
        try { Add-Type -AssemblyName System.IO.Compression.FileSystem } catch { }
        Write-Log "  D tar-unavailable"; return $false
    }
    & $tarExe.Source -xzf $tarCache -C $path 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Log "  D extract-failed rc=$LASTEXITCODE"; return $false }
    Set-Content -LiteralPath (Join-Path $path ".ark_version") -Value $pinned -Encoding UTF8
    return $true
}

function Try-ChannelC([string]$name, [string]$path, [string]$pinned) {
    $metaUrl = "$RepoBaseUrl/$name/latest.json"
    try {
        $meta = (Invoke-WebRequest -Uri $metaUrl -UseBasicParsing -TimeoutSec 30).Content | ConvertFrom-Json
    } catch {
        Write-Log "  C meta-unavailable: $metaUrl"
        return $false
    }
    if ([string]$meta.commit -ne $pinned) {
        Write-Log ("  C meta-commit-mismatch meta=" + [string]$meta.commit + " pinned=" + $pinned)
        return $false
    }
    $bundleCache = Join-Path $cacheRoot ([string]$meta.bundle)
    if (-not (Test-Path -LiteralPath $bundleCache)) {
        try {
            Invoke-WebRequest -Uri "$RepoBaseUrl/$name/$($meta.bundle)" -OutFile $bundleCache -UseBasicParsing -TimeoutSec 180 | Out-Null
        } catch {
            Write-Log ("  C download-failed: " + $_.Exception.Message)
            return $false
        }
    }
    $actual = (Get-FileHash -LiteralPath $bundleCache -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne [string]$meta.sha256) {
        Write-Log "  C sha-mismatch"
        Remove-Item -LiteralPath $bundleCache -Force -ErrorAction SilentlyContinue
        return $false
    }
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
    $rc = Invoke-Git (@("clone", "--quiet", $bundleCache, $path))
    if ($rc -ne 0) {
        Write-Log "  C clone-failed rc=$rc; refetch bundle once"
        Remove-Item -LiteralPath $bundleCache -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        try {
            Invoke-WebRequest -Uri "$RepoBaseUrl/$name/$($meta.bundle)" -OutFile $bundleCache -UseBasicParsing -TimeoutSec 180 | Out-Null
            $retrySha = (Get-FileHash -LiteralPath $bundleCache -Algorithm SHA256).Hash.ToLower()
            if ($retrySha -ne [string]$meta.sha256) { Write-Log "  C retry sha-mismatch"; return $false }
            $rc = Invoke-Git (@("clone", "--quiet", $bundleCache, $path))
        } catch {
            Write-Log ("  C retry download-failed: " + $_.Exception.Message)
            return $false
        }
        if ($rc -ne 0) { Write-Log "  C retry clone-failed rc=$rc"; return $false }
    }
    $rc = Invoke-Git (@("-C", $path, "-c", "advice.detachedHead=false", "checkout", "--quiet", $pinned))
    if ($rc -ne 0) { Write-Log "  C checkout-failed rc=$rc"; return $false }
    $head = (& $gitExe -C $path rev-parse HEAD).Trim()
    return ($head -eq $pinned)
}

$failed = 0
foreach ($name in $Repo) {
    $key = "git.$name"
    if (-not $entries.ContainsKey($key)) { Write-Log "SKIP $name (no $key)"; continue }
    $entry = $entries[$key]
    $pinned = [string]$entry.source_commit
    $slug = [string]$entry.source_repo
    if (-not $pinned -or -not $slug) { Write-Log "SKIP $name (missing source_commit/source_repo)"; continue }

    $path = Join-Path $reposRoot $name
    $hasRepo = Test-Path -LiteralPath (Join-Path $path ".git")
    $versionFile = Join-Path $path ".ark_version"
    if (-not $hasRepo -and (Test-Path -LiteralPath $versionFile)) {
        $stamp = (Get-Content -LiteralPath $versionFile -Raw).Trim()
        if ($stamp -eq $pinned) { Write-Log "OK $name $($pinned.Substring(0,12)) (tarball, already)"; continue }
    }
    if ($hasRepo) {
        $head = (& $gitExe -C $path rev-parse HEAD).Trim()
        if ($head -eq $pinned) { Write-Log "OK $name $($pinned.Substring(0,12)) (already)"; continue }
        if ($CheckOnly) { Write-Log "FAIL $name stale head=$($head.Substring(0,12)) pinned=$($pinned.Substring(0,12))"; $failed++; continue }
    } elseif ($CheckOnly) {
        Write-Log "FAIL $name missing $path"; $failed++; continue
    } elseif (-not $AllowClone) {
        Write-Log "FAIL $name not-cloned (pass -AllowClone)"; $failed++; continue
    }

    $ok = $false
    $channel = "D"
    if ($gitExe) {
        $ok = Try-ChannelA $path "https://github.com/$slug.git" $pinned
        $channel = "A"
        if (-not $ok) {
            Write-Log "  -> fallback to C for $name"
            $ok = Try-ChannelC $name $path $pinned
            $channel = "C"
        }
    }
    if (-not $ok) {
        Write-Log "  -> fallback to D for $name (tarball, no git)"
        $ok = Try-ChannelD $name $path $pinned
        $channel = "D"
    }
    if (-not $ok) { Write-Log "FAIL $name both-channels-failed"; $failed++; continue }
    Set-Content -LiteralPath (Join-Path $path ".ark_version") -Value $pinned -Encoding UTF8
    Write-Log "OK $name $($pinned.Substring(0,12)) via $channel -> $path"
}

if ($InstallTask) {
    $taskName = "NoahsArk-Repo-Sync"
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -AllowClone') `
        -WorkingDirectory $Workspace
    $principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\Administrator" -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew -Hidden -StartWhenAvailable
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Trigger $trigger -Force | Out-Null
    Write-Log "INSTALL-TASK $taskName registered (every 30 min)"
}

if ($failed -gt 0) {
    Write-Log "GATE BLOCKED_STALE_REPO failed=$failed"
    exit 2
}
Write-Log "GATE PASS"
exit 0
