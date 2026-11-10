<#
.SYNOPSIS
  ark 环境变量同步（Windows）
.EXAMPLE
  .\ark-env.ps1 -Pass <pw> -Apply
  .\ark-env.ps1 -Diff
#>
param(
  [string]$BaseUrl = "",
  [string]$User = "ark",
  [string]$Pass = "",
  [switch]$Apply,
  [switch]$Diff,
  [ValidateSet("User","Machine")]
  [string]$Scope = "Machine"
)
$ErrorActionPreference = "Stop"

# Machine 作用域需要管理员；非管理员直接报错，不静默降级
if ($Scope -eq "Machine") {
  $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $isAdmin) { throw "Machine 作用域需要以管理员身份运行（当前不是）。可用 -Scope User 退回到用户级。" }
}
$authDir = Join-Path $env:USERPROFILE ".config\ark"
$authFile = Join-Path $authDir "auth.json"
if (-not $Pass) {
  if ($env:ARK_HTTP_PASS) { $Pass = $env:ARK_HTTP_PASS }
  elseif (Test-Path $authFile) { $Pass = (Get-Content $authFile -Raw | ConvertFrom-Json).pass }
  else { throw "缺少口令：用 -Pass 或设置 ARK_HTTP_PASS" }
} else {
  New-Item -ItemType Directory -Force -Path $authDir | Out-Null
  @{ user = $User; pass = $Pass } | ConvertTo-Json | Set-Content $authFile -Encoding UTF8
  & icacls $authFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}
$pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${User}:${Pass}"))
$hdr = @{ Authorization = "Basic $pair" }

function Get-Ark([string]$path, [string]$base) {
  (Invoke-WebRequest -UseBasicParsing -Uri "$base$path" -Headers $hdr -TimeoutSec 15).Content
}

if (-not $BaseUrl) {
  # 自动探测：先试站点 A 的转发入口，再试站点 B 直连
  foreach ($candidate in @("http://ark-env-a.internal:8090", "http://ark-env-b.internal:8080")) {
    try {
      $h = (Invoke-WebRequest -UseBasicParsing -Uri "$candidate/health" -TimeoutSec 5).StatusCode
      if ($h -eq 200) { $BaseUrl = $candidate; break }
    } catch { }
  }
  if (-not $BaseUrl) { throw "两个入口都不通（8090 / 8080），检查网络" }
}
Write-Host "使用入口: $BaseUrl"

$ver = (Get-Ark "/ark/version.json" $BaseUrl | ConvertFrom-Json).version
$cfg = Get-Ark "/ark/env.json" $BaseUrl | ConvertFrom-Json
$local = [Environment]::GetEnvironmentVariable("ARK_ENV_VERSION", $Scope)

function New-Sha([string]$s) {
  $sha = [Security.Cryptography.SHA256]::Create()
  ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($s))) -replace '-', '').ToLower()
}

$changed = @{}
foreach ($p in $cfg.PSObject.Properties) {
  if ($p.Name -like "__*") { continue }
  $want = [string]$p.Value
  if (-not $want) { continue }
  $have = [Environment]::GetEnvironmentVariable($p.Name, $Scope)
  if ($have -and (New-Sha $have) -eq (New-Sha $want)) { continue }
  $changed[$p.Name] = $want
}

Write-Host ("remote version={0} local={1} changed={2}" -f $ver, $local, $changed.Count)
if ($Diff -or (-not $Apply)) {
  $changed.Keys | ForEach-Object { Write-Host "  would set: $_" }
  if (-not $Apply) { Write-Host "（预演；加 -Apply 才写入）"; return }
}
if ($changed.Count -gt 0) {
  foreach ($k in $changed.Keys) { [Environment]::SetEnvironmentVariable($k, $changed[$k], $Scope) }
}
[Environment]::SetEnvironmentVariable("ARK_ENV_VERSION", $ver, $Scope)

$report = foreach ($p in $cfg.PSObject.Properties) {
  if ($p.Name -like "__*") { continue }
  "{0}={1}" -f $p.Name, [bool]([Environment]::GetEnvironmentVariable($p.Name, $Scope))
}
Write-Host ($report -join " | ")
Write-Host ("完成（作用域 {0}）。新开的进程才会看到新值；SYSTEM 服务需重启服务。" -f $Scope)
