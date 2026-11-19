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
         Agent-S (gui-agents) from Git, with a PyPI fallback when Git is absent.
      3. Starts the local computer-use MCP server as a child of OpenCode and
         injects it through OPENCODE_CONFIG_CONTENT for this process only.

    Nothing is written to your global OpenCode config, PATH, or any machine-wide
    location. When OpenCode exits, the ability is gone.

.PARAMETER NoAgentS
    Skip installing Agent-S (gui-agents). Screen, mouse, keyboard, windows, UIA and
    clipboard tools still work.

.PARAMETER Reinstall
    Delete and rebuild .runtime\venv from scratch.

.PARAMETER SelfTest
    Check the runtime and the MCP tools without launching OpenCode.

.PARAMETER RefreshTools
    Re-download the vendored OpenCode and uv into .runtime\tools.

.PARAMETER LatestTools
    With -RefreshTools, resolve the newest upstream release instead of the pins.

.PARAMETER PrepareVendor
    Copy the portable OpenCode and uv binaries into vendor\. This makes the
    toolchain portable (no npm/Node), but the first run still needs network for
    the private Python and Python packages unless a working .runtime is included.

.PARAMETER OpenCodeVersion / UvVersion
    Explicit tool versions to fetch, e.g. -OpenCodeVersion 2.3.2.

.PARAMETER Directory
    Working directory for OpenCode (defaults to the current folder).

.EXAMPLE
    .\Computer-Use.cmd
.EXAMPLE
    .\Computer-Use.cmd -Directory C:\Users\Admin\projects\demo
.EXAMPLE
    .\Computer-Use.cmd --prompt "Open notepad and type hello"
.EXAMPLE
    .\Computer-Use.cmd -SelfTest
#>
[CmdletBinding()]
param(
    [switch]$NoAgentS,
    [switch]$Reinstall,
    [switch]$SelfTest,
    [switch]$RefreshTools,
    [switch]$LatestTools,
    [switch]$PrepareVendor,
    [string]$OpenCodeVersion = "2.3.2",
    [string]$UvVersion = "0.11.28",
    [string]$Directory,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$OpenCodeArgs
)

$ErrorActionPreference = "Stop"

$script:Root = Split-Path -Parent $PSScriptRoot
$script:Runtime = Join-Path $script:Root ".runtime"
$script:Tools = Join-Path $script:Runtime "tools"
$script:Venv = Join-Path $script:Runtime "venv"
$script:VenvPy = Join-Path $script:Venv "Scripts\python.exe"
$script:ServerScript = Join-Path $PSScriptRoot "computer_use_mcp.py"
$script:LogDir = Join-Path $script:Runtime "logs"
$script:AgentSRepo = "git+https://github.com/simular-ai/Agent-S.git"
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

function Get-LatestTag {
    param([string]$Repository, [string]$ApiUrl)
    if ($ApiUrl) {
        $release = Invoke-RestMethod -Uri $ApiUrl
    } else {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/latest"
    }
    return ([string]$release.tag_name).TrimStart("v")
}

# --------------------------------------------------------------- opencode
function Install-OpenCode {
    $version = $OpenCodeVersion
    if ($LatestTools) {
        try { $version = Get-LatestTag -Repository "anomalyco/opencode" } catch { Write-Warn "Could not resolve the latest OpenCode; using $version." }
    }
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
    if ($RefreshTools) { return (Install-OpenCode) }

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
    $version = $UvVersion
    if ($LatestTools) {
        try { $version = Get-LatestTag -Repository "astral-sh/uv" } catch { Write-Warn "Could not resolve the latest uv; using $version." }
    }
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
    if ($RefreshTools) {
        try { return (Install-Uv) } catch { Write-Warn "uv download failed: $($_.Exception.Message)" }
    }

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

if (-not (Test-Path $script:ServerScript)) {
    Fail "Missing $script:ServerScript. Keep the scripts folder next to Computer-Use.cmd."
}
if ($LatestTools) { $RefreshTools = $true }
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
    if ((Test-Path $vendorOpenCodeFile) -and (Test-Path $vendorUvFile) -and (-not $RefreshTools)) {
        Write-Host ""
        Write-Ok "vendor\ already contains the portable toolchain; nothing to do."
        Write-Step "Use -RefreshTools to re-download it."
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
    Write-Step "That rebuild restores the core tools; Agent-S may need to be reinstalled."
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

$script:AgentS = Join-Path $script:Venv "Scripts\agent_s.exe"
if (-not $NoAgentS) {
    if (-not (Test-Path $script:AgentS)) {
        $installed = $false
        $git = Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($git) {
            Write-Step "Installing Agent-S (gui-agents) from $script:AgentSRepo"
            Write-Step "First run only - this pulls paddle/ocr/selenium dependencies and can take a few minutes."
            try {
                Install-Packages -Packages @($script:AgentSRepo) -Label "Installing Agent-S..."
                $installed = Test-Path $script:AgentS
            } catch {
                Write-Warn "Agent-S git install failed: $($_.Exception.Message)"
            }
        } else {
            Write-Step "Git was not found; installing the official gui-agents package from PyPI instead."
        }

        # A fresh PC may not have Git. The official PyPI package is the same
        # Agent-S release (gui-agents 0.3.2) and avoids a Git dependency.
        if (-not $installed) {
            try {
                Install-Packages -Packages @("gui-agents==0.3.2") -Label "Installing Agent-S from PyPI..."
                $installed = Test-Path $script:AgentS
            } catch {
                Write-Warn "Agent-S PyPI install failed: $($_.Exception.Message)"
            }
        }

        if (-not $installed) {
            Write-Warn "Agent-S was not installed. GUI primitives still work."
            Write-Warn "Retry with network access, or run with -NoAgentS to skip this step."
        }
    } else {
        Write-Step "Agent-S already present in the runtime."
    }
}

# --------------------------------------------------------------- self test
if ($SelfTest) {
    Write-Host ""
    Write-Step "Running self-test (no OpenCode, no global changes)..."
    $env:COMPUTER_USE_SESSION_TOKEN = "selftest"
    Invoke-External { & $script:VenvPy (Join-Path $PSScriptRoot "selftest.py") }
    $selfExit = $script:LastExitCode
    Remove-Item Env:\COMPUTER_USE_SESSION_TOKEN -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host ("  Self-test exit code {0}. Nothing was left running." -f $selfExit) -ForegroundColor Cyan
    exit $selfExit
}

# --------------------------------------------------------------- session token
$token = [Guid]::NewGuid().ToString("N")
$serverEntry = [ordered]@{
    type        = "local"
    command     = @($script:VenvPy, $script:ServerScript)
    cwd         = $script:Root
    environment = [ordered]@{ COMPUTER_USE_SESSION_TOKEN = $token }
    timeout     = [ordered]@{ startup = 120000; catalog = 60000 }
}

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
$mode = if (Test-Path $script:AgentS) { "screen/mouse/keyboard + Agent-S" } else { "screen/mouse/keyboard only" }
Write-Ok "Computer use attached for this session ($mode)."
Write-Step "Session token: $($token.Substring(0, 8))... (dies with this process)"
Write-Step "No global config was modified. Ctrl+C / exit OpenCode to detach."

$arguments = @("--standalone")
if ($Directory) { $arguments += $Directory }
if ($OpenCodeArgs) { $arguments += $OpenCodeArgs }

$exitCode = 1
$runDirectory = if ($Directory -and (Test-Path $Directory)) { $Directory } else { $script:Root }
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
