# Regression test for the launcher's auto-update logic.
# Creates a throwaway bare origin + clone, extracts Update-LauncherRepo from
# Start-ComputerUse.ps1, and proves:
#   * a clean checkout strictly behind origin is fast-forwarded
#   * .runtime user data survives the update
#   * a dirty checkout is never overwritten
#   * -NoUpdate skips the check
$ErrorActionPreference = "Stop"

$launcher = Join-Path $PSScriptRoot "Start-ComputerUse.ps1"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $launcher, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw "Cannot parse $launcher : $($parseErrors[0].Message)"
}
$functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq "Update-LauncherRepo"
}, $true)
if (-not $functionAst) {
    throw "Update-LauncherRepo not found in $launcher"
}
Invoke-Expression $functionAst.Extent.Text

function Write-Step { param([string]$Message) Write-Host "  $Message" }
function Write-Warn { param([string]$Message) Write-Host "  ! $Message" }
function Write-Ok { param([string]$Message) Write-Host "  [ok] $Message" }

function Invoke-Git {
    param([string]$Repo, [string[]]$GitArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git -C $Repo @GitArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) {
        throw "git $($GitArgs -join ' ') failed in ${Repo}: $output"
    }
    return $output
}

function Commit-And-Push {
    param([string]$Repo, [string]$Message)
    Invoke-Git $Repo @("add", "-A") | Out-Null
    Invoke-Git $Repo @("commit", "-m", $Message) | Out-Null
    Invoke-Git $Repo @("push", "origin", "main") | Out-Null
}

if (-not (Get-Command git -CommandType Application -ErrorAction SilentlyContinue)) {
    Write-Host "SKIP: git is not installed."
    exit 0
}

$base = Join-Path $env:TEMP ("opencode-update-test-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$origin = Join-Path $base "origin.git"
$seed = Join-Path $base "seed"
$work = Join-Path $base "work"
$failures = 0
function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = "")
    $suffix = if ($Detail) { " - $Detail" } else { "" }
    if ($Ok) {
        Write-Host "  [ok] $Name$suffix"
    } else {
        Write-Host "  [!!] $Name$suffix"
        $script:failures++
    }
}

try {
    New-Item -ItemType Directory -Path $base | Out-Null
    Invoke-Git $base @("init", "--bare", "--initial-branch=main", $origin) | Out-Null
    Invoke-Git $base @("clone", $origin, $seed) | Out-Null
    Invoke-Git $seed @("config", "user.email", "selftest@example.invalid") | Out-Null
    Invoke-Git $seed @("config", "user.name", "Update Selftest") | Out-Null

    Set-Content -Path (Join-Path $seed "tracked.txt") -Value "v1" -Encoding ascii
    Set-Content -Path (Join-Path $seed ".gitignore") -Value ".runtime/" -Encoding ascii
    Commit-And-Push $seed "v1"

    Invoke-Git $base @("clone", $origin, $work) | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $work ".runtime") -Force | Out-Null
    Set-Content -Path (Join-Path $work ".runtime\userdata.txt") -Value "keep me" -Encoding ascii

    Set-Content -Path (Join-Path $seed "tracked.txt") -Value "v2" -Encoding ascii
    Commit-And-Push $seed "v2"

    $script:Root = $work
    $noUpdateMode = $false
    Update-LauncherRepo

    $workHead = (Invoke-Git $work @("rev-parse", "HEAD")).Trim()
    $seedHead = (Invoke-Git $seed @("rev-parse", "HEAD")).Trim()
    Check "clean checkout fast-forwarded" ($workHead -eq $seedHead) $workHead.Substring(0, 8)
    Check "tracked file updated" (
        (Get-Content (Join-Path $work "tracked.txt") -Raw).Trim() -eq "v2"
    )
    Check "runtime user data preserved" (
        (Get-Content (Join-Path $work ".runtime\userdata.txt") -Raw).Trim() -eq "keep me"
    )

    Set-Content -Path (Join-Path $work "tracked.txt") -Value "local edit" -Encoding ascii
    Set-Content -Path (Join-Path $seed "tracked.txt") -Value "v3" -Encoding ascii
    Commit-And-Push $seed "v3"
    $dirtyHead = (Invoke-Git $work @("rev-parse", "HEAD")).Trim()
    Update-LauncherRepo
    $afterDirtyHead = (Invoke-Git $work @("rev-parse", "HEAD")).Trim()
    Check "dirty checkout not moved" ($dirtyHead -eq $afterDirtyHead)
    Check "dirty file preserved" (
        (Get-Content (Join-Path $work "tracked.txt") -Raw).Trim() -eq "local edit"
    )

    Invoke-Git $work @("checkout", "--", "tracked.txt") | Out-Null
    Set-Content -Path (Join-Path $seed "tracked.txt") -Value "v4" -Encoding ascii
    Commit-And-Push $seed "v4"
    $beforeSkip = (Invoke-Git $work @("rev-parse", "HEAD")).Trim()
    $noUpdateMode = $true
    Update-LauncherRepo
    $afterSkip = (Invoke-Git $work @("rev-parse", "HEAD")).Trim()
    Check "-NoUpdate leaves HEAD alone" ($beforeSkip -eq $afterSkip)
}
catch {
    Write-Host "  [!!] unexpected: $($_.Exception.Message)"
    $failures++
}
finally {
    Remove-Item -Recurse -Force $base -ErrorAction SilentlyContinue
}

if ($failures -gt 0) {
    Write-Host "UPDATE SELFTEST: FAILED ($failures)"
    exit 1
}
Write-Host "UPDATE SELFTEST: OK"
