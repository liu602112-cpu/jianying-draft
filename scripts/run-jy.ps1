param(
    [Parameter(Position=0, Mandatory=$true)] [string] $Script,
    [Parameter(ValueFromRemainingArguments=$true)] [string[]] $Arguments
)
$ErrorActionPreference = 'Stop'
$pythonCandidates = @(
    $env:JY_PYTHON,
    (Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe')
)
$pythonPath = $pythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
if (-not $pythonPath) {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
}
if ($Script -notmatch '^jy(?:_[a-z]+)?(?:\.py)?$') { throw '只允许调用本工具包里的 jy 脚本' }
if (-not $Script.EndsWith('.py')) { $Script += '.py' }
$scriptPath = Join-Path $PSScriptRoot $Script
if ($Script -in @('jy_pack.py', 'jy_audit.py')) {
    $scriptPath = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "live-clip/scripts/$Script"
}
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "脚本不存在：$Script" }
$env:PYTHONUTF8 = '1'
& $pythonPath $scriptPath @Arguments
exit $LASTEXITCODE
