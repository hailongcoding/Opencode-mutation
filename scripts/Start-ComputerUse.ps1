#Requires -Version 5.1
<#
.SYNOPSIS
    Start OpenCode with a computer-use (screen/mouse/keyboard) ability attached
    to this one session only.

.DESCRIPTION
    Self-contained launcher. No Node, npm, or system Python is required:

      1. Finds OpenCode (vendor\ -> .runtime\tools\ -> PATH) or downloads the
         official Windows binary once.
      2. Finds uv the same way, then uses it to build .runtime\venv and install
         the native MCP dependencies.
      3. Starts the local computer-use MCP server as a child of OpenCode and
         injects it through OPENCODE_CONFIG_CONTENT for this process only.

    OpenCode is the only model in the loop: the tool surface is deterministic
    native automation (UI Automation, screen, mouse, keyboard), with no second
    GUI agent and no extra API keys.

    Nothing is written to your global OpenCode config, PATH, or any machine-wide
    location. When OpenCode exits, the ability is gone.

    On launch, a git checkout is checked against origin/main and fast-forwarded
    only when it is clean and strictly behind. .runtime, vendor files and user
    data are preserved; local commits and uncommitted edits are never
    overwritten. Use -NoUpdate to skip the check.

    -ReadOnly (or --read-only) keeps the inspection tools but blocks every input,
    window and launch action inside the MCP server. Ctrl+Alt+F12 pauses
    automation at any time and releases held input. Every tool call is audited in
    .runtime\logs\actions.log with typed text redacted.

.PARAMETER Reinstall
    Delete and rebuild .runtime\venv from scratch.

.PARAMETER SelfTest
    Check the runtime and the MCP tools without launching OpenCode.

.PARAMETER PrepareVendor
    Copy the portable OpenCode and uv binaries into vendor\. This makes the
    toolchain portable (no npm/Node), but the first run still needs network for
    the private Python and Python packages unless a working .runtime is included.

.PARAMETER ReadOnly
    Start with inspection tools only (snapshot, tree, screenshots, reads, waits).
    Every mouse, keyboard, window and launch action is blocked by the MCP server.

.PARAMETER NoUpdate
    Skip the startup check for newer commits on GitHub. Auto-update only
    fast-forwards a clean checkout; local commits or uncommitted files are left
    alone.

.EXAMPLE
    .\Computer-Use.cmd
.EXAMPLE
    .\Computer-Use.cmd -Directory C:\Users\Admin\projects\demo
.EXAMPLE
    .\Computer-Use.cmd --prompt "Open notepad and type hello"
.EXAMPLE
    .\Computer-Use.cmd -SelfTest
.EXAMPLE
    .\Computer-Use.cmd -ReadOnly
.EXAMPLE
    .\Computer-Use.cmd pair
.EXAMPLE
    .\Computer-Use.cmd pair --remote
.EXAMPLE
    .\Computer-Use.cmd pair --remote --read-only

.NOTES
    OpenCode opens in the folder you launched from by default. -Directory <path>
    overrides it. -Directory is parsed with the passthrough arguments so that
    `pair` and other subcommands keep working.
#>
[CmdletBinding()]
param(
    [switch]$Reinstall,
    [switch]$SelfTest,
    [switch]$PrepareVendor,
    [switch]$ReadOnly,
    [switch]$NoUpdate,
    # Everything else (including `pair` and `-Directory <path>`) is parsed here.
    # Keeping a typed positional -Directory parameter would swallow `pair`.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$OpenCodeArgs
)

$ErrorActionPreference = "Stop"

# Where the user actually invoked the launcher. All launcher internals resolve
# against $script:Root, but OpenCode's project folder should default to this.
$startDirectory = (Get-Location).Path

# --read-only is a launcher flag, not an OpenCode flag. -Directory is parsed
# manually because PowerShell would otherwise bind the `pair` subcommand to it.
$readOnlyMode = $ReadOnly.IsPresent
if ($env:COMPUTER_USE_READ_ONLY -match '^(1|true|yes|on)$') { $readOnlyMode = $true }
$noUpdateMode = $NoUpdate.IsPresent
$directoryArgument = $null
$directoryError = ""
$filteredArgs = @()
$rawArgs = @($OpenCodeArgs)
for ($index = 0; $index -lt $rawArgs.Count; $index++) {
    $argument = [string]$rawArgs[$index]
    if ($argument -ieq "--read-only") {
        $readOnlyMode = $true
    } elseif ($argument -ieq "--no-update") {
        $noUpdateMode = $true
    } elseif ($argument -ieq "-Directory" -or $argument -ieq "--directory") {
        if ($index + 1 -ge $rawArgs.Count) {
            $directoryError = "-Directory requires a folder path."
            break
        }
        $directoryArgument = [string]$rawArgs[$index + 1]
        $index++
    } elseif ($argument -match '^-{1,2}directory=(.+)$') {
        $directoryArgument = $matches[1]
    } else {
        $filteredArgs += $argument
    }
}
$OpenCodeArgs = $filteredArgs
$Directory = $directoryArgument

$script:Root = Split-Path -Parent $PSScriptRoot
$script:Runtime = Join-Path $script:Root ".runtime"
$script:Tools = Join-Path $script:Runtime "tools"
$script:Venv = Join-Path $script:Runtime "venv"
$script:VenvPy = Join-Path $script:Venv "Scripts\python.exe"
$script:ServerScript = Join-Path $PSScriptRoot "computer_use_mcp.py"
$script:LogDir = Join-Path $script:Runtime "logs"
# Pinned toolchain. There is intentionally no update command; change these only
# when you are prepared to re-test the whole stack.
$script:OpenCodeVersion = "2.3.2"
$script:UvVersion = "0.11.28"
$script:LastExitCode = 0
$script:OpenCode = $null
$script:Uv = $null
$script:Py = $null

function Write-Step {
    param([string]$Message)
    Write-Host ("  {0}" -f $Message) -ForegroundColor DarkGray
}

function Write-Ok {
    param([string]$Message)
    Write-Host ("  [ok] {0}" -f $Message) -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host ("  [!!] {0}" -f $Message) -ForegroundColor Yellow
}

function Fail {
    param([string]$Message)
    Write-Host ("`n[computer-use] {0}" -f $Message) -ForegroundColor Red
    exit 1
}

# Keep this checkout current without ever overwriting local work: only a clean
# checkout that is strictly behind origin/main is fast-forwarded. The running
# PowerShell process keeps its parsed copy, so launcher changes apply next run;
# the MCP server (started later) uses the updated files immediately.
function Update-LauncherRepo {
    if ($noUpdateMode) { return }
    if (-not (Test-Path (Join-Path $script:Root ".git"))) { return }
    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $git) {
        Write-Step "Update check skipped: git is not installed."
        return
    }
    $env:GIT_TERMINAL_PROMPT = "0"
    Write-Step "Checking GitHub for launcher updates..."
    # Do not pipe git through Select-Object -First 1: stopping the pipeline
    # early sets $LASTEXITCODE to -1 even when git succeeded.
    $before = @(& $git.Source -C $script:Root rev-parse HEAD 2>$null)[0]
    if ($LASTEXITCODE -ne 0 -or -not $before) {
        Write-Step "Update check skipped: this folder is not a git checkout."
        return
    }
    & $git.Source -C $script:Root fetch --quiet origin main 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Update check skipped: git fetch failed (offline or no access)."
        return
    }
    $remote = @(& $git.Source -C $script:Root rev-parse origin/main 2>$null)[0]
    if (-not $remote) {
        Write-Warn "Update check skipped: origin/main was not found."
        return
    }
    if ($remote -eq $before) {
        Write-Step "Launcher is up to date."
        return
    }
    & $git.Source -C $script:Root merge-base --is-ancestor $before $remote 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Local commits differ from GitHub; auto-update skipped."
        return
    }
    $dirty = (& $git.Source -C $script:Root status --porcelain 2>$null)
    if ($dirty) {
        Write-Warn "Uncommitted local changes present; auto-update skipped."
        return
    }
    Write-Step "Newer commits found; fast-forwarding this checkout..."
    & $git.Source -C $script:Root pull --ff-only --quiet origin main 2>$null
    if ($LASTEXITCODE -eq 0) {
        $after = @(& $git.Source -C $script:Root rev-parse --short HEAD 2>$null)[0]
        Write-Ok "Updated launcher to $after (server changes apply now; launcher changes next run)."
    } else {
        Write-Warn "Auto-update failed; continuing with the local version."
    }
}

if ($directoryError) { Fail $directoryError }

# PowerShell 5.1 turns native stderr into a terminating error when
# ErrorActionPreference is Stop (uv, pip, curl and opencode all write there).
# Runs a native command with a tolerant error action and records its code,
# without piping stdout (the OpenCode TUI needs the real console).
function Invoke-External {
    param([scriptblock]$Body)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Body
    } finally {
        $ErrorActionPreference = $previous
    }
    $script:LastExitCode = $LASTEXITCODE
    if ($null -eq $script:LastExitCode) { $script:LastExitCode = 0 }
}

# Captures stdout from an OpenCode subcommand. Never used for the TUI: pairing
# and service discovery need the text, while the TUI needs the real console.
function Invoke-OpenCodeCapture {
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $script:OpenCode @Arguments 2>$null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($null -eq $code) { $code = 0 }
    $script:LastExitCode = $code
    return [pscustomobject]@{
        Output   = (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
        ExitCode = $code
    }
}

# Small wrapper so every runtime service call uses the same timeout and JSON
# handling. The background service listens on loopback and uses Basic auth.
function Invoke-ServiceHttp {
    param(
        [string]$Method,
        [string]$Uri,
        [hashtable]$Headers,
        [string]$Body
    )
    $request = @{
        Uri             = $Uri
        Method          = $Method
        Headers         = $Headers
        UseBasicParsing = $true
        TimeoutSec      = 30
    }
    if ($Body) {
        $request.ContentType = "application/json"
        $request.Body = $Body
    }
    Invoke-WebRequest @request
}

# --------------------------------------------------------------- tool helpers
function Get-TextContent {
    param([string]$Url)
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing
    if ($response.Content -is [byte[]]) {
        return [Text.Encoding]::UTF8.GetString($response.Content)
    }
    return [string]$response.Content
}

function Save-Download {
    param([string]$Url, [string]$Destination)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    $temp = "$Destination.part"
    Remove-Item $temp -Force -ErrorAction SilentlyContinue

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($curl) {
        Invoke-External { & $curl.Source -L --fail --retry 3 --silent --show-error -o $temp $Url }
        if ($script:LastExitCode -ne 0) { throw "Download failed (curl $($script:LastExitCode)): $Url" }
    } else {
        $previous = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"
        try {
            Invoke-WebRequest -Uri $Url -OutFile $temp -UseBasicParsing
        } catch {
            throw "Download failed: $Url ($($_.Exception.Message))"
        } finally {
            $ProgressPreference = $previous
        }
    }
    if (-not (Test-Path $temp) -or (Get-Item $temp).Length -eq 0) { throw "Download failed: $Url" }
    Move-Item -Force $temp $Destination
}

function Assert-Sha256 {
    param([string]$Path, [string]$Expected, [string]$Label)
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
    if ($actual -ne $Expected.Trim().ToLower()) {
        Remove-Item $Path -Force -ErrorAction SilentlyContinue
        throw "$Label checksum mismatch. expected=$Expected actual=$actual"
    }
}

function Expand-Zip {
    param([string]$Zip, [string]$Destination)
    if (Test-Path $Destination) { Remove-Item $Destination -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path $Zip).Path, (Resolve-Path $Destination).Path)
}

function Get-SumForAsset {
    param([string]$Url, [string]$Asset)
    try {
        $text = Get-TextContent $Url
        $line = ($text -split "`n" | Where-Object { $_ -match [regex]::Escape($Asset) } | Select-Object -First 1)
        if (-not $line) { return $null }
        return ($line.Trim() -split '\s+')[0]
    } catch {
        return $null
    }
}

# --------------------------------------------------------------- opencode
function Install-OpenCode {
    $version = $script:OpenCodeVersion
    $asset = "opencode-windows-x64.zip"
    $base = "https://github.com/anomalyco/opencode/releases/download/v$version"

    Write-Step "Downloading OpenCode v$version (~125 MB, one time)..."
    $zip = Join-Path $script:Tools "downloads\$asset"
    Save-Download "$base/$asset" $zip

    $expected = Get-SumForAsset "$base/SHA256SUMS" $asset
    if ($expected) {
        Assert-Sha256 $zip $expected "OpenCode v$version"
    } else {
        Write-Warn "No checksum published for OpenCode v$version; skipping verification."
    }

    $dir = Join-Path $script:Tools "opencode"
    Expand-Zip $zip $dir
    $exe = Get-ChildItem $dir -Recurse -Filter "opencode.exe" | Select-Object -First 1
    if (-not $exe) { throw "opencode.exe was not found inside the archive." }
    $target = Join-Path $dir "opencode.exe"
    if ($exe.FullName -ne $target) { Move-Item $exe.FullName $target -Force }
    Unblock-File $target -ErrorAction SilentlyContinue
    Remove-Item $zip -Force -ErrorAction SilentlyContinue

    Invoke-External { & $target --version }
    if ($script:LastExitCode -ne 0) { throw "The downloaded OpenCode binary does not run." }
    return $target
}

function Resolve-OpenCode {
    $vendor = Join-Path $script:Root "vendor\opencode\opencode.exe"
    if (Test-Path $vendor) { return $vendor }

    $cached = Join-Path $script:Tools "opencode\opencode.exe"
    if (Test-Path $cached) { return $cached }

    $onPath = Get-Command opencode -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }

    try {
        return (Install-OpenCode)
    } catch {
        Fail "OpenCode is not available: $($_.Exception.Message)`n  Install OpenCode, or place opencode.exe in vendor\opencode\, or retry with network access."
    }
}

# --------------------------------------------------------------- uv
function Install-Uv {
    $version = $script:UvVersion
    $asset = "uv-x86_64-pc-windows-msvc.zip"
    $base = "https://github.com/astral-sh/uv/releases/download/$version"

    Write-Step "Downloading uv v$version (~25 MB, one time)..."
    $zip = Join-Path $script:Tools "downloads\$asset"
    Save-Download "$base/$asset" $zip

    $sumText = $null
    try { $sumText = (Get-TextContent "$base/$asset.sha256").Trim() } catch { }
    if ($sumText) {
        Assert-Sha256 $zip ($sumText -split '\s+')[0] "uv v$version"
    }

    $dir = Join-Path $script:Tools "uv"
    Expand-Zip $zip $dir
    $exe = Get-ChildItem $dir -Recurse -Filter "uv.exe" | Select-Object -First 1
    if (-not $exe) { throw "uv.exe was not found inside the archive." }
    $target = Join-Path $script:Tools "uv.exe"
    Move-Item $exe.FullName $target -Force
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Unblock-File $target -ErrorAction SilentlyContinue
    return $target
}

function Resolve-Uv {
    $vendor = Join-Path $script:Root "vendor\uv\uv.exe"
    if (Test-Path $vendor) { return $vendor }

    $cached = Join-Path $script:Tools "uv.exe"
    if (Test-Path $cached) { return $cached }

    $onPath = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }

    try {
        return (Install-Uv)
    } catch {
        Write-Warn "Could not download uv: $($_.Exception.Message)"
        return $null
    }
}

# --------------------------------------------------------------- prerequisites
Write-Host ""
Write-Host "  OpenCode + computer use (this session only)" -ForegroundColor Cyan
Write-Host "  ------------------------------------------" -ForegroundColor DarkCyan

Update-LauncherRepo

if (-not (Test-Path $script:ServerScript)) {
    Fail "Missing $script:ServerScript. Keep the scripts folder next to Computer-Use.cmd."
}
if (-not (Test-Path $script:LogDir)) {
    New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
}
if (-not (Test-Path $script:Tools)) {
    New-Item -ItemType Directory -Force -Path $script:Tools | Out-Null
}

$script:OpenCode = Resolve-OpenCode
$script:Uv = Resolve-Uv
if (-not $script:Uv) {
    $script:Py = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $script:Py) {
        Fail "Need uv or python to build the isolated runtime.`n  Put uv.exe in vendor\uv\ or run once with network access."
    }
    $script:Py = $script:Py.Source
}

# Keep the portable toolchain inside this folder.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $script:Tools "python"
$env:UV_CACHE_DIR = Join-Path $script:Tools "uv-cache"

$opencodeLabel = $script:OpenCode
if ($script:OpenCode -like "*\vendor\opencode\opencode.exe" -or $script:OpenCode -like "*\.runtime\tools\opencode\opencode.exe") {
    $opencodeLabel = "$script:OpenCode (portable)"
}
Write-Step "OpenCode: $opencodeLabel"
if ($script:Uv) { Write-Step "uv:       $script:Uv" }

# --------------------------------------------------------- portable toolchain
if ($PrepareVendor) {
    $vendorOpenCodeFile = Join-Path $script:Root "vendor\opencode\opencode.exe"
    $vendorUvFile = Join-Path $script:Root "vendor\uv\uv.exe"
    if ((Test-Path $vendorOpenCodeFile) -and (Test-Path $vendorUvFile)) {
        Write-Host ""
        Write-Ok "vendor\ already contains the pinned portable toolchain; nothing to do."
        Write-Step "Delete vendor\opencode\opencode.exe and vendor\uv\uv.exe to rebuild it."
        exit 0
    }

    Write-Host ""
    Write-Step "Preparing the portable toolchain in vendor\ (no npm/Node needed)..."
    try {
        $cachedOpenCode = Join-Path $script:Tools "opencode\opencode.exe"
        if (-not (Test-Path $cachedOpenCode)) { $null = Install-OpenCode }
        $vendorOpenCode = Join-Path $script:Root "vendor\opencode"
        New-Item -ItemType Directory -Force -Path $vendorOpenCode | Out-Null
        Copy-Item $cachedOpenCode (Join-Path $vendorOpenCode "opencode.exe") -Force

        $cachedUv = Join-Path $script:Tools "uv.exe"
        if (-not (Test-Path $cachedUv)) { $null = Install-Uv }
        $vendorUv = Join-Path $script:Root "vendor\uv"
        New-Item -ItemType Directory -Force -Path $vendorUv | Out-Null
        Copy-Item $cachedUv (Join-Path $vendorUv "uv.exe") -Force
    } catch {
        Fail "Could not prepare the portable toolchain: $($_.Exception.Message)"
    }
    $bundleSize = [math]::Round((Get-ChildItem (Join-Path $script:Root "vendor") -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host ""
    Write-Ok "vendor\ toolchain is ready ($bundleSize MB)."
    Write-Step "On another PC the first run still downloads Python and packages (OpenCode needs internet anyway)."
    Write-Step "If you also copy .runtime, its bundled Python/uv cache lets the venv rebuild without re-downloading."
    Write-Step "That rebuild restores the native tools."
    exit 0
}

# ------------------------------------------------------------------ venv setup
if ($Reinstall -and (Test-Path $script:Venv)) {
    Write-Step "Removing old runtime (requested -Reinstall)..."
    Remove-Item -Recurse -Force $script:Venv
}

function New-Runtime {
    Write-Step "Creating isolated Python runtime at .runtime\venv"
    if ($script:Uv) {
        # Prefer a uv-managed Python installed under .runtime\tools\python, so
        # the runtime does not depend on a machine-wide Python installation.
        # Fall back to a system Python when managed Python is unavailable.
        Invoke-External { & $script:Uv venv --python 3.12 --relocatable --managed-python $script:Venv }
        if ($script:LastExitCode -ne 0) {
            Invoke-External { & $script:Uv venv --python 3.12 --relocatable $script:Venv }
        }
        if ($script:LastExitCode -ne 0) {
            Invoke-External { & $script:Uv venv --python 3.12 $script:Venv }
        }
        if ($script:LastExitCode -ne 0) {
            Invoke-External { & $script:Uv venv $script:Venv }
        }
    } else {
        Invoke-External { & $script:Py -m venv $script:Venv }
    }
    if (-not (Test-Path $script:VenvPy)) {
        Fail "Could not create the virtual environment. Check the network connection."
    }
}

function Install-Packages {
    param([string[]]$Packages, [string]$Label)
    Write-Step $Label
    if ($script:Uv) {
        Invoke-External { & $script:Uv pip install --python $script:VenvPy @Packages }
    } else {
        Invoke-External { & $script:VenvPy -m pip install --disable-pip-version-check @Packages }
    }
    if ($script:LastExitCode -ne 0) { throw "Install failed: $($Packages -join ' ')" }
}

function Test-Runtime {
    Invoke-External { & $script:VenvPy -c "import mcp.server.fastmcp, pyautogui, PIL, pywinauto" 2>$null }
    return ($script:LastExitCode -eq 0)
}

$runtimeUsable = $false
if (Test-Path $script:VenvPy) {
    Invoke-External { & $script:VenvPy -c "import sys" 2>$null }
    $runtimeUsable = ($script:LastExitCode -eq 0)
}

if (-not $runtimeUsable) {
    if (Test-Path $script:Venv) {
        Write-Warn "The existing runtime does not run here (copied from another PC?). Rebuilding it."
        Remove-Item -Recurse -Force $script:Venv
    }
    New-Runtime
    Install-Packages -Packages @("mcp>=1.10,<2", "pyautogui", "pillow", "pywinauto") -Label "Installing MCP server dependencies..."
    if (-not (Test-Runtime)) {
        Fail "The isolated runtime cannot import mcp/pyautogui/Pillow/pywinauto."
    }
} elseif (-not (Test-Runtime)) {
    Write-Warn "Runtime is incomplete; repairing base dependencies."
    Install-Packages -Packages @("mcp>=1.10,<2", "pyautogui", "pillow", "pywinauto") -Label "Repairing MCP server dependencies..."
    if (-not (Test-Runtime)) {
        Fail "The isolated runtime cannot import mcp/pyautogui/Pillow/pywinauto."
    }
}

# --------------------------------------------------------------- self test
if ($SelfTest) {
    Write-Host ""
    Write-Step "Running self-test (no OpenCode, no global changes)..."
    $env:COMPUTER_USE_SESSION_TOKEN = "selftest"
    Remove-Item Env:\COMPUTER_USE_READ_ONLY -ErrorAction SilentlyContinue
    Invoke-External { & $script:VenvPy (Join-Path $PSScriptRoot "selftest.py") }
    $selfExit = $script:LastExitCode
    Remove-Item Env:\COMPUTER_USE_SESSION_TOKEN -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host ("  Self-test exit code {0}. Nothing was left running." -f $selfExit) -ForegroundColor Cyan
    exit $selfExit
}

# --------------------------------------------------------------- session token
$token = [Guid]::NewGuid().ToString("N")
$serverEnvironment = [ordered]@{
    COMPUTER_USE_SESSION_TOKEN = $token
    COMPUTER_USE_PARENT_PID    = "$PID"
}
if ($readOnlyMode) { $serverEnvironment["COMPUTER_USE_READ_ONLY"] = "1" }
$serverEntry = [ordered]@{
    type        = "local"
    command     = @($script:VenvPy, $script:ServerScript)
    cwd         = $script:Root
    environment = $serverEnvironment
    timeout     = [ordered]@{ startup = 120000; catalog = 60000 }
}

$runDirectory = $startDirectory
if (-not (Test-Path -LiteralPath $runDirectory -PathType Container)) {
    $runDirectory = $script:Root
}
if ($Directory) {
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        Fail "Directory not found: $Directory"
    }
    $runDirectory = (Resolve-Path -LiteralPath $Directory).Path
}

# `pair` must be the first argument. Only checking the first token keeps a
# flag value (for example `--prompt pair`) from being treated as the subcommand.
$pairMode = ($OpenCodeArgs.Count -gt 0 -and $OpenCodeArgs[0] -ieq "pair")

# ------------------------------------------------------------------ pair mode
# `opencode pair` needs the shared background service and rejects --standalone.
# Attach the MCP server to that service at runtime, only for the launch folder.
if ($pairMode) {
    $pairHelp = $false
    foreach ($argument in $OpenCodeArgs) {
        if ($argument -in @("--help", "-h", "--version", "-v")) { $pairHelp = $true }
    }

    Write-Host ""
    Write-Step "Pair mode uses OpenCode's background service (not --standalone)."
    Write-Step "Computer use will be added for this folder only: $runDirectory"
    if ($readOnlyMode) {
        Write-Warn "Read-only mode: the paired session can inspect the desktop but cannot act."
    }

    $serviceWasRunning = $false
    $before = Invoke-OpenCodeCapture @("service", "status")
    if ($before.ExitCode -eq 0 -and $before.Output -match '^https?://') {
        $serviceWasRunning = $true
    }

    $pairExit = 1
    $runtimeAttached = $false
    $startedService = $false
    $serviceUrl = $null
    $mcpUri = $null
    $headers = $null

    # `opencode pair` starts or reuses the shared background service. If this
    # launcher was itself started from another Computer-Use window, its inline
    # config and token would be inherited by that service and register a second
    # `computer` server for every project. Pair mode manages the runtime entry
    # itself, so drop those launcher-only variables first.
    Remove-Item Env:\OPENCODE_CONFIG_CONTENT -ErrorAction SilentlyContinue
    Remove-Item Env:\COMPUTER_USE_SESSION_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\COMPUTER_USE_PARENT_PID -ErrorAction SilentlyContinue
    Remove-Item Env:\COMPUTER_USE_READ_ONLY -ErrorAction SilentlyContinue

    Push-Location $runDirectory
    try {
        Invoke-External { & $script:OpenCode @OpenCodeArgs }
        $pairExit = $script:LastExitCode

        if ($pairExit -ne 0) {
            Write-Warn "OpenCode pairing exited with code $pairExit."
        } elseif ($pairHelp) {
            # `pair --help` never starts the service or issues a code.
        } else {
            $status = Invoke-OpenCodeCapture @("service", "status")
            if ($status.ExitCode -ne 0 -or $status.Output -notmatch '^https?://') {
                Write-Warn "Pairing finished, but the background service was not found."
                $pairExit = 1
            } else {
                $serviceUrl = $status.Output.TrimEnd('/')
                $startedService = (-not $serviceWasRunning)

                $password = (Invoke-OpenCodeCapture @("service", "get", "password")).Output
                if (-not $password) {
                    Write-Warn "Could not read the background service password."
                    $pairExit = 1
                } else {
                    $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("opencode:$password"))
                    $headers = @{ Authorization = "Basic $basic" }
                    $query = "location%5Bdirectory%5D=" + [Uri]::EscapeDataString($runDirectory)
                    $mcpUri = "$serviceUrl/api/experimental/mcp/computer?$query"

                    # Do not hijack an existing `computer` server (for example a
                    # different Computer-Use window that is still attached).
                    $existing = $null
                    try {
                        $list = Invoke-ServiceHttp -Method "Get" -Uri "$serviceUrl/api/mcp?$query" -Headers $headers
                        $parsed = $list.Content | ConvertFrom-Json
                        $existing = @($parsed.data) | Where-Object { $_.name -eq "computer" } | Select-Object -First 1
                    } catch {
                        Write-Step "Could not inspect existing MCP servers: $($_.Exception.Message)"
                    }

                    if ($existing) {
                        Write-Warn "A 'computer' MCP server is already registered for this folder (status: $($existing.status.status))."
                        Write-Step "Close the other Computer-Use window, or run 'opencode service restart' to clear a stale entry."
                        $pairExit = 1
                    } else {
                        $payload = [ordered]@{
                            config = [ordered]@{
                                type        = "local"
                                command     = @($script:VenvPy, $script:ServerScript)
                                cwd         = $script:Root
                                environment = $serverEnvironment
                                timeout     = [ordered]@{ startup = 120000; catalog = 60000 }
                            }
                        } | ConvertTo-Json -Depth 20 -Compress

                        for ($attempt = 1; $attempt -le 8; $attempt++) {
                            try {
                                $response = Invoke-ServiceHttp -Method "Put" -Uri $mcpUri -Headers $headers -Body $payload
                                if ($response.StatusCode -eq 204 -or $response.StatusCode -eq 200) {
                                    $runtimeAttached = $true
                                    break
                                }
                            } catch {
                                if ($attempt -eq 8) {
                                    Write-Warn "Could not attach computer use: $($_.Exception.Message)"
                                } else {
                                    Start-Sleep -Milliseconds 500
                                }
                            }
                        }

                        if (-not $runtimeAttached) {
                            $pairExit = 1
                        } else {
                            # Give the local MCP server time to finish starting so
                            # the first tool call from the paired app does not race it.
                            $ready = $false
                            for ($attempt = 0; $attempt -lt 40; $attempt++) {
                                try {
                                    $current = Invoke-ServiceHttp -Method "Get" -Uri "$serviceUrl/api/mcp?$query" -Headers $headers
                                    $parsed = $current.Content | ConvertFrom-Json
                                    $entry = @($parsed.data) | Where-Object { $_.name -eq "computer" } | Select-Object -First 1
                                    if ($entry -and $entry.status.status -eq "connected") {
                                        $ready = $true
                                        break
                                    }
                                    if ($entry -and $entry.status.status -eq "failed") { break }
                                } catch { }
                                Start-Sleep -Milliseconds 500
                            }

                            Write-Host ""
                            if ($ready) {
                                Write-Ok "Computer use is attached to the background service."
                            } else {
                                Write-Warn "Computer use was added but has not reported 'connected' yet."
                                Write-Step "The paired session can retry once the MCP server finishes starting."
                            }
                            if ($readOnlyMode) {
                                Write-Step "MCP server policy: read-only (inspection tools only)."
                            }
                            Write-Step "Background service: $serviceUrl"
                            Write-Step "In the paired app, open this folder as the project: $runDirectory"
                            Write-Step "The pairing links above are one-time and expire after five minutes."
                            Write-Step "Keep this window open while the paired app is in use."

                            $holdSeconds = 0
                            [void][int]::TryParse($env:COMPUTER_USE_PAIR_HOLD_SECONDS, [ref]$holdSeconds)
                            if ($holdSeconds -gt 0) {
                                Write-Step "Auto-detaching after $holdSeconds seconds (COMPUTER_USE_PAIR_HOLD_SECONDS)."
                                Start-Sleep -Seconds $holdSeconds
                            } elseif ([Console]::IsInputRedirected) {
                                Write-Step "Input is not interactive: keep this process running; press Ctrl+C to detach."
                                while ($true) { Start-Sleep -Seconds 60 }
                            } else {
                                Write-Step "Press Enter here to detach and clean up."
                                [void][Console]::ReadLine()
                            }
                        }
                    }
                }
            }
        }
    } finally {
        Pop-Location

        if ($runtimeAttached -and $mcpUri -and $headers) {
            $removed = $false
            for ($attempt = 1; $attempt -le 5; $attempt++) {
                try {
                    $null = Invoke-ServiceHttp -Method "Delete" -Uri $mcpUri -Headers $headers
                    $removed = $true
                    break
                } catch {
                    Start-Sleep -Milliseconds 400
                }
            }
            if ($removed) {
                Write-Step "Removed the runtime computer-use server from the background service."
            } else {
                Write-Warn "Could not remove the runtime computer-use server; it is inert once this window closes."
            }
        }

        if ($startedService) {
            $after = Invoke-OpenCodeCapture @("service", "status")
            if ($after.ExitCode -eq 0 -and $after.Output -match '^https?://') {
                $null = Invoke-OpenCodeCapture @("service", "stop")
                Write-Step "Stopped the background service this launcher started."
            }
        }

        Write-Host ""
        if ($pairExit -eq 0) {
            Write-Host "  Pairing detached. The computer-use tools are no longer attached." -ForegroundColor Cyan
        } else {
            Write-Host "  Pairing ended with code $pairExit. Nothing was left attached." -ForegroundColor Yellow
        }
    }

    exit $pairExit
}

# ------------------------------------------------------------ normal launch
# Merge over any inline config the user already had; nothing is written to disk.
$configJson = $null
if ($env:OPENCODE_CONFIG_CONTENT) {
    try {
        $base = $env:OPENCODE_CONFIG_CONTENT | ConvertFrom-Json
        if (-not $base.mcp) {
            $base | Add-Member -NotePropertyName mcp -NotePropertyValue ([pscustomobject]@{}) -Force
        }
        if (-not $base.mcp.servers) {
            $base.mcp | Add-Member -NotePropertyName servers -NotePropertyValue ([pscustomobject]@{}) -Force
        }
        $base.mcp.servers | Add-Member -NotePropertyName computer -NotePropertyValue ([pscustomobject]$serverEntry) -Force
        $configJson = $base | ConvertTo-Json -Depth 20 -Compress
    } catch {
        Write-Warn "Could not merge OPENCODE_CONFIG_CONTENT; replacing it for this session."
    }
}
if (-not $configJson) {
    $configJson = [ordered]@{
        '$schema' = "https://opencode.ai/config.json"
        mcp       = [ordered]@{ servers = [ordered]@{ computer = $serverEntry } }
    } | ConvertTo-Json -Depth 20 -Compress
}

$env:COMPUTER_USE_SESSION_TOKEN = $token
$env:OPENCODE_CONFIG_CONTENT = $configJson

# ------------------------------------------------------------------- launching
if ($readOnlyMode) {
    Write-Ok "Native computer use attached for this session in READ-ONLY mode."
    Write-Step "Inspection only: snapshot/tree/screenshots/reads/waits; actions are blocked."
} else {
    Write-Ok "Native computer use attached for this session (UI Automation + screen/mouse/keyboard)."
}
Write-Step "Session token: $($token.Substring(0, 8))... (dies with this process)"
Write-Step "Working folder: $runDirectory"
Write-Step "No global config was modified. Ctrl+C / exit OpenCode to detach."

$arguments = @("--standalone")
if ($Directory) { $arguments += $Directory }
if ($OpenCodeArgs) { $arguments += $OpenCodeArgs }

$exitCode = 1
Push-Location $runDirectory
try {
    Invoke-External { & $script:OpenCode @arguments }
    $exitCode = $script:LastExitCode
} finally {
    Pop-Location
    Remove-Item Env:\COMPUTER_USE_SESSION_TOKEN -ErrorAction SilentlyContinue
    if ($env:OPENCODE_CONFIG_CONTENT -eq $configJson) {
        Remove-Item Env:\OPENCODE_CONFIG_CONTENT -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "  Computer use detached. OpenCode exited with code $exitCode." -ForegroundColor Cyan
}
exit $exitCode
