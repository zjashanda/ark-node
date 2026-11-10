<#
.SYNOPSIS
  ark-node 启动器（Windows）：不安装也能直接跑。

.EXAMPLE
  .\bin\ark-node.ps1 probe --out capability.json

.NOTES
  需要 Python 3.8+。可用 $env:ARK_NODE_PYTHON 指定解释器路径。
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:ARK_NODE_PYTHON) { $env:ARK_NODE_PYTHON } else { 'python' }

if (-not (Get-Command $python -ErrorAction SilentlyContinue)) {
    Write-Error "ark-node: 找不到 $python，请安装 Python 3.8+ 或设置 `$env:ARK_NODE_PYTHON"
    exit 2
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$root;$env:PYTHONPATH" } else { $root }
& $python -m ark_node @Arguments
exit $LASTEXITCODE
