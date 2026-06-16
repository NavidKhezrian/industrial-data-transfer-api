#requires -version 5.1
<#
.SYNOPSIS
    Automated bootstrap installer for the Industrial Data Transfer Factory Agent.

.DESCRIPTION
    This script is intended to be converted to an EXE and run as Administrator
    on the Windows 11 factory computer.

    It performs these tasks:
      - checks administrative privileges,
      - creates installation and log directories,
      - installs WireGuard for Windows when missing,
      - installs uv when missing,
      - installs a managed Python 3.11 runtime with uv,
      - obtains the current project source from GitHub,
      - asks the operator to select the SQLite database,
      - generates API keys and a WireGuard key pair,
      - configures the Factory Agent for production over WireGuard,
      - installs the WireGuard tunnel as a Windows service,
      - creates restricted Windows Firewall rules,
      - prepares a manual Factory Agent runner,
      - starts the Factory Agent for the current session,
      - writes deployment information that must be sent securely to the administrator.

.NOTES
    The script is idempotent. Running it again reuses existing API keys and
    the existing Factory WireGuard private key when possible.

    A fully automatic first connection is not possible unless the public
    WireGuard server already knows the Factory public key. This script writes
    that public key to `PLEASE_SEND_THIS_FILE_TO_US.txt`. The administrator must
    add it to the public WireGuard server and configure the same API keys on
    the Receiver computer.
#>

[CmdletBinding()]
param(
    [string]$WireGuardServerEndpoint = "",
    [string]$WireGuardServerPublicKey = "",
    [string]$FactoryVpnAddress = "10.10.0.2/24",
    [string]$ReceiverVpnIp = "10.10.0.3",
    [int]$FactoryAgentPort = 9000,
    [int]$ReceiverPort = 8000,
    [string]$RepositoryUrl = "https://github.com/NavidKhezrian/industrial-data-transfer-api.git",
    [string]$RepositoryBranch = "main",
    [string]$InstallRoot = "",
    [string]$DatabasePath = "",
    [string]$ProgressLogPath = "",
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ScriptVersion = "1.2.0"
$TunnelName = "factory-agent"
$TaskName = "IndustrialDataTransfer-FactoryAgent"
$InboundRuleName = "Factory Agent API 9000 from WireGuard"
$OutboundRuleName = "WireGuard to Public Server UDP 51820"

function Get-BootstrapLauncherDirectory {
    # When compiled with PS2EXE, the current process is the generated EXE.
    # When running as a normal .ps1 file, the current process is powershell.exe,
    # so use $PSScriptRoot instead.
    $processPath = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $processFileName = [System.IO.Path]::GetFileName($processPath).ToLowerInvariant()
    $powershellHosts = @(
        "powershell.exe",
        "pwsh.exe",
        "powershell_ise.exe"
    )

    if ($powershellHosts -notcontains $processFileName) {
        return [System.IO.Path]::GetDirectoryName($processPath)
    }

    if ($PSScriptRoot) {
        return (Resolve-Path -LiteralPath $PSScriptRoot).Path
    }

    return (Get-Location).Path
}

$BootstrapLauncherDirectory = Get-BootstrapLauncherDirectory

if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    # Create one installation folder beside the EXE or PowerShell script.
    $InstallRoot = Join-Path $BootstrapLauncherDirectory "IndustrialDataTransfer"
}
else {
    $InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
}

$ToolsDir = Join-Path $InstallRoot "tools"
$PythonInstallDir = Join-Path $InstallRoot "python"
$PythonBinDir = Join-Path $InstallRoot "python-bin"
$AppDir = Join-Path $InstallRoot "app"
$RepoDir = Join-Path $AppDir "industrial-data-transfer-api"
$FactoryDir = Join-Path $RepoDir "factory_agent"
$ConfigPath = Join-Path $FactoryDir "config.yaml"
$SecretsDir = Join-Path $InstallRoot "secrets"
$SecretsPath = Join-Path $SecretsDir "agent-secrets.json"
$WireGuardDir = Join-Path $InstallRoot "wireguard"
$WireGuardConfigPath = Join-Path $WireGuardDir "$TunnelName.conf"
$RuntimeDir = Join-Path $InstallRoot "runtime"
$RunnerPath = Join-Path $RuntimeDir "Start-FactoryAgent.ps1"
$LogsDir = Join-Path $InstallRoot "logs"
$SharePath = Join-Path $InstallRoot "PLEASE_SEND_THIS_FILE_TO_US.txt"
$ReportPath = Join-Path $InstallRoot "DEPLOYMENT_REPORT.txt"
$StatePath = Join-Path $InstallRoot "bootstrap-state.json"
$LogPath = Join-Path $LogsDir ("bootstrap-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
if ([string]::IsNullOrWhiteSpace($ProgressLogPath)) {
    $ProgressLogPath = Join-Path $InstallRoot "FactoryDeploymentInstaller-progress.log"
}

function Write-ProgressLog {
    param([AllowNull()][string]$Message)
    try {
        if ([string]::IsNullOrWhiteSpace($ProgressLogPath)) { return }
        $parent = Split-Path -Parent $ProgressLogPath
        if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Add-Content -LiteralPath $ProgressLogPath -Value ([string]$Message) -Encoding UTF8
    }
    catch {}
}

function Flush-ConsoleOutput {
    try { [Console]::Out.Flush() } catch {}
    try { [Console]::Error.Flush() } catch {}
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-Host $Message -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-ProgressLog ""
    Write-ProgressLog ("=" * 78)
    Write-ProgressLog $Message
    Write-ProgressLog ("=" * 78)
    Flush-ConsoleOutput
}

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Gray
    Write-ProgressLog "[INFO] $Message"
    Flush-ConsoleOutput
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[OK]   $Message" -ForegroundColor Green
    Write-ProgressLog "[OK]   $Message"
    Flush-ConsoleOutput
}

function Write-WarnLine {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
    Write-ProgressLog "[WARN] $Message"
    Flush-ConsoleOutput
}

function Stop-WithError {
    param([string]$Message)
    Write-Host ""
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Write-Host "Log file: $LogPath" -ForegroundColor Yellow
    Write-ProgressLog ""
    Write-ProgressLog "[ERROR] $Message"
    Write-ProgressLog "Log file: $LogPath"
    Flush-ConsoleOutput
    try { Stop-Transcript | Out-Null } catch {}
    if (-not $NonInteractive) {
        Read-Host "Press Enter to close"
    }
    exit 1
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Read-RequiredValue {
    param(
        [string]$Prompt,
        [string]$CurrentValue = ""
    )
    if ($CurrentValue -and $CurrentValue.Trim()) {
        return $CurrentValue.Trim()
    }
    if ($NonInteractive) {
        throw "Missing required value: $Prompt"
    }
    do {
        $value = Read-Host $Prompt
    } while (-not $value -or -not $value.Trim())
    return $value.Trim()
}

function Select-SqliteDatabase {
    if ($DatabasePath -and $DatabasePath.Trim()) {
        $candidate = $DatabasePath.Trim('"').Trim()
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "SQLite database does not exist: $candidate"
        }
        return (Resolve-Path -LiteralPath $candidate).Path
    }

    if ($NonInteractive) {
        throw "The SQLite database path must be provided with -DatabasePath when -NonInteractive is used."
    }

    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Select the factory SQLite database"
        $dialog.Filter = "SQLite databases (*.db;*.sqlite;*.sqlite3)|*.db;*.sqlite;*.sqlite3|All files (*.*)|*.*"
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            return $dialog.FileName
        }
    }
    catch {
        Write-WarnLine "The graphical file selector could not be opened: $($_.Exception.Message)"
    }

    do {
        $path = Read-Host "Enter the complete path of the SQLite database"
        $path = $path.Trim('"').Trim()
    } while (-not (Test-Path -LiteralPath $path -PathType Leaf))
    return (Resolve-Path -LiteralPath $path).Path
}

function Invoke-ProcessChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory = "",
        [int[]]$AllowedExitCodes = @(0)
    )

    $display = "$FilePath $($ArgumentList -join ' ')"
    Write-Info "Running: $display"

    $start = @{
        FilePath = $FilePath
        ArgumentList = $ArgumentList
        Wait = $true
        PassThru = $true
        NoNewWindow = $true
    }
    if ($WorkingDirectory) {
        $start.WorkingDirectory = $WorkingDirectory
    }

    $process = Start-Process @start
    if ($AllowedExitCodes -notcontains $process.ExitCode) {
        throw "Command failed with exit code $($process.ExitCode): $display"
    }

    # Do not write the exit code to the PowerShell success pipeline.
    # Functions such as Ensure-UvAndPython return a structured object. An
    # unintended integer in the pipeline would turn that result into an array.
}

function Add-MachinePathEntry {
    param([Parameter(Mandatory = $true)][string]$PathEntry)

    if (-not (Test-Path -LiteralPath $PathEntry)) {
        New-Item -ItemType Directory -Path $PathEntry -Force | Out-Null
    }

    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $entries = @($machinePath -split ";" | Where-Object { $_ -and $_.Trim() })
    $alreadyPresent = $false
    foreach ($entry in $entries) {
        if ($entry.TrimEnd("\") -ieq $PathEntry.TrimEnd("\")) {
            $alreadyPresent = $true
            break
        }
    }

    if (-not $alreadyPresent) {
        $newPath = (($entries + $PathEntry) -join ";")
        [Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")
        Write-Ok "Added to the machine PATH: $PathEntry"
    }

    if (($env:Path -split ";") -notcontains $PathEntry) {
        $env:Path = "$env:Path;$PathEntry"
    }
}

function New-ApiToken {
    param([int]$ByteLength = 32)
    $bytes = New-Object byte[] $ByteLength
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Protect-PathForAdmins {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$IsDirectory
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    & icacls.exe $Path /inheritance:r | Out-Null
    if ($IsDirectory) {
        & icacls.exe $Path /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    }
    else {
        & icacls.exe $Path /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
    }
}


function Grant-ReadAccessToAllUsers {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }

    # The share file must be easy to open and send to the administrator.
    # It contains no private WireGuard key, but it does contain API keys.
    # For production, delete this file after the administrator has configured the server and Receiver.
    & icacls.exe $Path /grant:r "*S-1-1-0:R" "*S-1-5-32-545:R" | Out-Null
}

function Test-Base64Key {
    param([string]$Value)
    if (-not $Value) { return $false }
    try {
        $decoded = [Convert]::FromBase64String($Value)
        return $decoded.Length -eq 32
    }
    catch {
        return $false
    }
}

function Get-EndpointHost {
    param([string]$Endpoint)

    if ($Endpoint -match '^\[(.+)\]:(\d+)$') {
        return $Matches[1]
    }
    if ($Endpoint -match '^(.+):(\d+)$') {
        return $Matches[1]
    }
    throw "The WireGuard endpoint must use the format SERVER_IP_OR_DNS:PORT"
}

function Get-EndpointPort {
    param([string]$Endpoint)

    if ($Endpoint -match ':(\d+)$') {
        return [int]$Matches[1]
    }
    throw "The WireGuard endpoint must include a port."
}

function Ensure-WireGuard {
    Write-Step "Installing or verifying WireGuard for Windows"

    $wireGuardExe = Join-Path $env:ProgramFiles "WireGuard\wireguard.exe"
    $wgExe = Join-Path $env:ProgramFiles "WireGuard\wg.exe"

    if ((Test-Path $wireGuardExe) -and (Test-Path $wgExe)) {
        Write-Ok "WireGuard is already installed."
        return [pscustomobject]@{
            WireGuardExe = $wireGuardExe
            WgExe = $wgExe
        }
    }

    $installed = $false
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        try {
            Invoke-ProcessChecked -FilePath $winget.Source -ArgumentList @(
                "install",
                "--id", "WireGuard.WireGuard",
                "-e",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements"
            ) -AllowedExitCodes @(0, -1978335189)
            $installed = $true
        }
        catch {
            Write-WarnLine "WinGet installation failed. The script will try the official MSI source."
        }
    }

    if (-not $installed) {
        $downloadPage = "https://download.wireguard.com/windows-client/"
        Write-Info "Reading the official WireGuard download page."
        $response = Invoke-WebRequest -Uri $downloadPage -UseBasicParsing
        $msiNames = @(
            [regex]::Matches($response.Content, 'wireguard-amd64-([0-9.]+)\.msi') |
            ForEach-Object { $_.Value } |
            Select-Object -Unique
        )

        if (-not $msiNames -or $msiNames.Count -eq 0) {
            throw "Could not discover the current official WireGuard AMD64 MSI."
        }

        $selected = $msiNames |
            Sort-Object {
                try { [version](($_ -replace '^wireguard-amd64-', '') -replace '\.msi$', '') }
                catch { [version]'0.0.0' }
            } -Descending |
            Select-Object -First 1

        $msiUrl = "$downloadPage$selected"
        $msiPath = Join-Path $env:TEMP $selected
        Write-Info "Downloading official WireGuard MSI: $msiUrl"
        Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing

        $signature = Get-AuthenticodeSignature -FilePath $msiPath
        if ($signature.Status -ne "Valid") {
            throw "WireGuard MSI signature validation failed. Status: $($signature.Status)"
        }

        Invoke-ProcessChecked -FilePath "msiexec.exe" -ArgumentList @(
            "/i", "`"$msiPath`"",
            "/qn",
            "DO_NOT_LAUNCH=1",
            "/norestart"
        ) -AllowedExitCodes @(0, 3010)
    }

    Start-Sleep -Seconds 3
    if (-not ((Test-Path $wireGuardExe) -and (Test-Path $wgExe))) {
        throw "WireGuard installation finished, but its command-line tools were not found."
    }

    Write-Ok "WireGuard installation completed."
    return [pscustomobject]@{
        WireGuardExe = $wireGuardExe
        WgExe = $wgExe
    }
}

function Get-UsableUvExecutable {
    $candidatePaths = New-Object System.Collections.Generic.List[string]

    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        [void]$candidatePaths.Add($command.Source)
    }

    $knownPaths = @(
        (Join-Path $ToolsDir "uv.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe")
    )

    foreach ($path in $knownPaths) {
        if ($path) {
            [void]$candidatePaths.Add($path)
        }
    }

    foreach ($candidate in ($candidatePaths | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }

        try {
            $versionOutput = (& $candidate --version 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $versionOutput) {
                return [pscustomobject]@{
                    Path = (Resolve-Path -LiteralPath $candidate).Path
                    Version = $versionOutput
                }
            }
        }
        catch {
            continue
        }
    }

    return $null
}

function Get-PythonInfoFromExecutable {
    param([Parameter(Mandatory = $true)][string]$Executable)

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $null
    }

    $probeCode = "import struct,sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'); print(struct.calcsize('P') * 8)"

    try {
        $probeOutput = @(& $Executable -c $probeCode 2>$null)
        if ($LASTEXITCODE -ne 0 -or $probeOutput.Count -lt 3) {
            return $null
        }

        $resolvedExecutable = [string]$probeOutput[0]
        $version = [version]([string]$probeOutput[1])
        $bits = [int]([string]$probeOutput[2])

        if ($version.Major -ne 3 -or $version.Minor -lt 11 -or $bits -ne 64) {
            return $null
        }

        return [pscustomobject]@{
            Path = $resolvedExecutable
            Version = $version
            Bits = $bits
        }
    }
    catch {
        return $null
    }
}

function Get-CompatiblePython {
    $candidatePaths = New-Object System.Collections.Generic.List[string]

    foreach ($commandName in @("python.exe", "python3.exe", "python3.11.exe", "python3.12.exe", "python3.13.exe", "python3.14.exe")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command -and $command.Source) {
            [void]$candidatePaths.Add($command.Source)
        }
    }

    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($selector in @("-3.11", "-3.12", "-3.13", "-3.14", "-3")) {
            try {
                $launcherOutput = @(
                    & $pyLauncher.Source $selector -c "import sys; print(sys.executable)" 2>$null
                )
                if ($LASTEXITCODE -eq 0 -and $launcherOutput.Count -gt 0) {
                    [void]$candidatePaths.Add(([string]$launcherOutput[0]).Trim())
                }
            }
            catch {
                continue
            }
        }
    }

    $searchPatterns = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe"),
        (Join-Path $env:ProgramFiles "Python*\python.exe")
    )

    if (${env:ProgramFiles(x86)}) {
        $searchPatterns += (Join-Path ${env:ProgramFiles(x86)} "Python*\python.exe")
    }

    foreach ($pattern in $searchPatterns) {
        Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue |
            ForEach-Object { [void]$candidatePaths.Add($_.FullName) }
    }

    foreach ($candidate in ($candidatePaths | Select-Object -Unique)) {
        $info = Get-PythonInfoFromExecutable -Executable $candidate
        if ($info) {
            return $info
        }
    }

    return $null
}

function Ensure-UvAndPython {
    Write-Step "Installing or verifying uv and compatible Python"

    New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null

    $uvInfo = Get-UsableUvExecutable
    $uvInstalledByBootstrap = $false

    if ($uvInfo) {
        $uvExe = $uvInfo.Path
        Write-Ok "Existing uv installation found: $($uvInfo.Version)"
        Write-Ok "uv path: $uvExe"
    }
    else {
        Write-Info "uv was not found. Installing it now."

        $installerPath = Join-Path $env:TEMP "uv-install.ps1"
        Invoke-WebRequest `
            -Uri "https://astral.sh/uv/install.ps1" `
            -OutFile $installerPath `
            -UseBasicParsing

        $previousInstallDir = $env:UV_INSTALL_DIR
        try {
            $env:UV_INSTALL_DIR = $ToolsDir

            $installerOutput = @(
                & powershell.exe `
                    -NoProfile `
                    -ExecutionPolicy Bypass `
                    -File $installerPath 2>&1
            )

            foreach ($line in $installerOutput) {
                if ($line) {
                    Write-Info ([string]$line)
                }
            }

            if ($LASTEXITCODE -ne 0) {
                throw "uv installer exited with code $LASTEXITCODE"
            }
        }
        finally {
            $env:UV_INSTALL_DIR = $previousInstallDir
        }

        $uvExe = Join-Path $ToolsDir "uv.exe"
        if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) {
            throw "uv.exe was not found after installation."
        }

        $uvInstalledByBootstrap = $true
        Add-MachinePathEntry -PathEntry (Split-Path -Parent $uvExe)

        $uvVersion = (& $uvExe --version 2>&1 | Out-String).Trim()
        Write-Ok "uv installation completed: $uvVersion"
        Write-Ok "uv path: $uvExe"
    }

    # An existing uv located outside PATH does not need PATH for this
    # application because all later calls use its absolute path. Add it only
    # when the shell cannot resolve uv normally.
    if (-not (Get-Command uv.exe -ErrorAction SilentlyContinue)) {
        Add-MachinePathEntry -PathEntry (Split-Path -Parent $uvExe)
    }

    $pythonInfo = Get-CompatiblePython
    $pythonInstalledByBootstrap = $false

    if ($pythonInfo) {
        $pythonPath = $pythonInfo.Path
        $pythonVersion = "Python $($pythonInfo.Version)"
        Write-Ok "Existing compatible Python installation found: $pythonVersion"
        Write-Ok "Python path: $pythonPath"

        if (-not (Get-Command python.exe -ErrorAction SilentlyContinue)) {
            Add-MachinePathEntry -PathEntry (Split-Path -Parent $pythonPath)
        }
    }
    else {
        Write-Info "No compatible 64-bit Python 3.11 or newer installation was found."
        Write-Info "Installing a managed Python 3.11 runtime with uv."

        New-Item -ItemType Directory -Path $PythonInstallDir -Force | Out-Null
        New-Item -ItemType Directory -Path $PythonBinDir -Force | Out-Null

        $previousPythonInstallDir = $env:UV_PYTHON_INSTALL_DIR
        $previousPythonBinDir = $env:UV_PYTHON_BIN_DIR

        try {
            $env:UV_PYTHON_INSTALL_DIR = $PythonInstallDir
            $env:UV_PYTHON_BIN_DIR = $PythonBinDir

            Invoke-ProcessChecked -FilePath $uvExe -ArgumentList @(
                "python", "install", "3.11"
            )

            $pythonPath = (& $uvExe python find 3.11 2>&1 | Out-String).Trim()
        }
        finally {
            $env:UV_PYTHON_INSTALL_DIR = $previousPythonInstallDir
            $env:UV_PYTHON_BIN_DIR = $previousPythonBinDir
        }

        if (-not $pythonPath -or -not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw "uv could not locate the Python 3.11 runtime after installation."
        }

        $pythonInfo = Get-PythonInfoFromExecutable -Executable $pythonPath
        if (-not $pythonInfo) {
            throw "The Python runtime installed by uv is not a compatible 64-bit Python 3.11 or newer runtime."
        }

        $pythonInstalledByBootstrap = $true
        $pythonVersion = "Python $($pythonInfo.Version)"

        # uv places versioned Python launchers in this directory. Add it to
        # machine PATH so future administrator shells can also find them.
        Add-MachinePathEntry -PathEntry $PythonBinDir

        Write-Ok "Python installation completed: $pythonVersion"
        Write-Ok "Python path: $pythonPath"
    }

    return [pscustomobject]@{
        UvExe = [string]$uvExe
        PythonExe = [string]$pythonPath
        PythonVersion = [string]$pythonVersion
        UvInstalledByBootstrap = [bool]$uvInstalledByBootstrap
        PythonInstalledByBootstrap = [bool]$pythonInstalledByBootstrap
    }
}

function Ensure-ProjectSource {
    param(
        [Parameter(Mandatory = $true)][string]$UvExe,
        [Parameter(Mandatory = $true)][string]$PythonExe
    )

    Write-Step "Downloading or updating the project source"

    New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
    $git = Get-Command git.exe -ErrorAction SilentlyContinue

    if ((Test-Path (Join-Path $RepoDir ".git")) -and $git) {
        Write-Info "Updating the existing Git repository."
        Invoke-ProcessChecked -FilePath $git.Source -ArgumentList @(
            "-C", $RepoDir, "fetch", "--depth", "1", "origin", $RepositoryBranch
        )
        Invoke-ProcessChecked -FilePath $git.Source -ArgumentList @(
            "-C", $RepoDir, "reset", "--hard", "origin/$RepositoryBranch"
        )
    }
    elseif ($git) {
        if (Test-Path $RepoDir) {
            Remove-Item -LiteralPath $RepoDir -Recurse -Force
        }
        Invoke-ProcessChecked -FilePath $git.Source -ArgumentList @(
            "clone", "--depth", "1", "--branch", $RepositoryBranch, $RepositoryUrl, $RepoDir
        )
    }
    else {
        Write-WarnLine "Git is not installed. Downloading the repository ZIP instead."
        $zipUrl = "https://github.com/NavidKhezrian/industrial-data-transfer-api/archive/refs/heads/$RepositoryBranch.zip"
        $zipPath = Join-Path $env:TEMP "industrial-data-transfer-api-$RepositoryBranch.zip"
        $extractPath = Join-Path $env:TEMP ("industrial-data-transfer-api-" + [guid]::NewGuid().ToString("N"))

        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
        New-Item -ItemType Directory -Path $extractPath -Force | Out-Null
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force

        $extractedRepo = Get-ChildItem -LiteralPath $extractPath -Directory |
            Where-Object { $_.Name -like "industrial-data-transfer-api-*" } |
            Select-Object -First 1

        if (-not $extractedRepo) {
            throw "The downloaded repository archive did not contain the expected folder."
        }

        if (Test-Path $RepoDir) {
            $backup = "$RepoDir.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
            Move-Item -LiteralPath $RepoDir -Destination $backup
            Write-Info "Previous project folder moved to: $backup"
        }

        Move-Item -LiteralPath $extractedRepo.FullName -Destination $RepoDir
        Remove-Item -LiteralPath $extractPath -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path (Join-Path $FactoryDir "pyproject.toml"))) {
        throw "Factory Agent project files were not found after downloading the repository."
    }

    Write-Info "Installing Factory Agent dependencies."
    Push-Location $FactoryDir
    try {
        Invoke-ProcessChecked -FilePath $UvExe -ArgumentList @(
            "sync", "--python", "`"$PythonExe`""
        ) -WorkingDirectory $FactoryDir
    }
    finally {
        Pop-Location
    }

    Write-Ok "Project source and dependencies are ready."
}

function Get-OrCreateSecrets {
    Write-Step "Creating or loading API keys"

    New-Item -ItemType Directory -Path $SecretsDir -Force | Out-Null
    Protect-PathForAdmins -Path $SecretsDir -IsDirectory

    if (Test-Path $SecretsPath) {
        $existing = Get-Content -LiteralPath $SecretsPath -Raw | ConvertFrom-Json
        if ($existing.FACTORY_AGENT_API_KEY -and $existing.RECEIVER_API_KEY) {
            Write-Ok "Existing API keys were reused."
            return $existing
        }
    }

    $secrets = [ordered]@{
        FACTORY_AGENT_API_KEY = New-ApiToken
        RECEIVER_API_KEY = New-ApiToken
        CreatedAtUtc = [DateTime]::UtcNow.ToString("o")
    }

    $secrets | ConvertTo-Json | Set-Content -LiteralPath $SecretsPath -Encoding UTF8
    Protect-PathForAdmins -Path $SecretsPath
    Write-Ok "New API keys were generated."
    return [pscustomobject]$secrets
}

function Update-FactoryConfig {
    param([Parameter(Mandatory = $true)][string]$DatabasePath)

    Write-Step "Configuring the Factory Agent"

    if (-not (Test-Path $ConfigPath)) {
        throw "Factory Agent config.yaml was not found: $ConfigPath"
    }

    $yaml = Get-Content -LiteralPath $ConfigPath -Raw
    $safeDbPath = $DatabasePath.Replace("\", "/").Replace("'", "''")
    $receiverUrl = "http://$ReceiverVpnIp`:$ReceiverPort"

    $yaml = [regex]::Replace(
        $yaml,
        '(?m)^environment:\s*.*$',
        'environment: production',
        1
    )
    $yaml = [regex]::Replace(
        $yaml,
        '(?m)^sqlite_path:\s*.*$',
        "sqlite_path: '$safeDbPath'",
        1
    )
    $yaml = [regex]::Replace(
        $yaml,
        '(?m)^api_base_url:\s*.*$',
        "api_base_url: $receiverUrl",
        1
    )

    Set-Content -LiteralPath $ConfigPath -Value $yaml -Encoding UTF8

    Write-Ok "environment set to production."
    Write-Ok "SQLite path set to: $DatabasePath"
    Write-Ok "Receiver URL set to: $receiverUrl"
}

function Test-SqliteDatabase {
    param(
        [Parameter(Mandatory = $true)][string]$UvExe,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$DatabasePath
    )

    Write-Step "Validating read-only access to the SQLite database"

    if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
        throw "SQLite database does not exist: $DatabasePath"
    }

    $validationScript = @'
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
uri = path.as_uri() + "?mode=ro"
with sqlite3.connect(uri, uri=True, timeout=10) as connection:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 20"
    ).fetchall()

print(f"Database opened read-only: {path}")
print(f"Tables found: {len(tables)}")
for row in tables:
    print(f"  - {row[0]}")
'@

    $validationPath = Join-Path $RuntimeDir "validate_sqlite.py"
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    Set-Content -LiteralPath $validationPath -Value $validationScript -Encoding UTF8

    Invoke-ProcessChecked -FilePath $UvExe -ArgumentList @(
        "run",
        "--python", "`"$PythonExe`"",
        "python",
        "`"$validationPath`"",
        "`"$DatabasePath`""
    ) -WorkingDirectory $FactoryDir

    Write-Ok "The database can be opened in read-only mode."
}

function Configure-WireGuardTunnel {
    param(
        [Parameter(Mandatory = $true)][string]$WireGuardExe,
        [Parameter(Mandatory = $true)][string]$WgExe
    )

    Write-Step "Creating and installing the Factory WireGuard tunnel"

    New-Item -ItemType Directory -Path $WireGuardDir -Force | Out-Null
    Protect-PathForAdmins -Path $WireGuardDir -IsDirectory

    $privateKey = ""
    if (Test-Path $WireGuardConfigPath) {
        $existingConfig = Get-Content -LiteralPath $WireGuardConfigPath -Raw
        if ($existingConfig -match '(?m)^PrivateKey\s*=\s*(.+)$') {
            $privateKey = $Matches[1].Trim()
        }
    }

    if (-not (Test-Base64Key $privateKey)) {
        $privateKey = (& $WgExe genkey | Out-String).Trim()
    }
    if (-not (Test-Base64Key $privateKey)) {
        throw "WireGuard private-key generation failed."
    }

    $publicKey = ($privateKey | & $WgExe pubkey | Out-String).Trim()
    if (-not (Test-Base64Key $publicKey)) {
        throw "WireGuard public-key generation failed."
    }

    $config = @"
[Interface]
PrivateKey = $privateKey
Address = $FactoryVpnAddress

[Peer]
PublicKey = $WireGuardServerPublicKey
Endpoint = $WireGuardServerEndpoint
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
"@

    Set-Content -LiteralPath $WireGuardConfigPath -Value $config -Encoding ASCII
    Protect-PathForAdmins -Path $WireGuardConfigPath

    $serviceName = 'WireGuardTunnel$' + $TunnelName
    $existingService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($existingService) {
        Write-Info "Replacing the existing WireGuard tunnel service."
        try {
            Invoke-ProcessChecked -FilePath $WireGuardExe -ArgumentList @(
                "/uninstalltunnelservice", $TunnelName
            )
        }
        catch {
            Write-WarnLine "The previous tunnel service could not be cleanly removed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 2
    }

    Invoke-ProcessChecked -FilePath $WireGuardExe -ArgumentList @(
        "/installtunnelservice", $WireGuardConfigPath
    )

    Start-Sleep -Seconds 3
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if (-not $service) {
        throw "WireGuard tunnel service was not created."
    }

    try {
        Set-Service -Name $serviceName -StartupType Manual
        Write-Ok "WireGuard tunnel service startup type set to Manual."
    }
    catch {
        Write-WarnLine "Could not set WireGuard tunnel service startup type to Manual: $($_.Exception.Message)"
    }

    if ($service.Status -ne "Running") {
        Start-Service -Name $serviceName
        Start-Sleep -Seconds 2
    }

    Write-Ok "WireGuard tunnel service installed: $serviceName"
    Write-Ok "Factory WireGuard public key: $publicKey"

    return [pscustomobject]@{
        PrivateKey = $privateKey
        PublicKey = $publicKey
        ServiceName = $serviceName
    }
}

function Configure-WindowsFirewall {
    Write-Step "Configuring Windows Firewall"

    $factoryVpnIp = ($FactoryVpnAddress -split "/")[0]
    $endpointPort = Get-EndpointPort -Endpoint $WireGuardServerEndpoint
    $endpointHost = Get-EndpointHost -Endpoint $WireGuardServerEndpoint

    Get-NetFirewallRule -DisplayName $InboundRuleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue

    New-NetFirewallRule `
        -DisplayName $InboundRuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalAddress $factoryVpnIp `
        -LocalPort $FactoryAgentPort `
        -RemoteAddress $ReceiverVpnIp `
        -Profile Any | Out-Null

    Write-Ok "Allowed inbound TCP $FactoryAgentPort only from Receiver $ReceiverVpnIp."

    $profiles = Get-NetFirewallProfile
    $outboundBlocked = @($profiles | Where-Object {
        $_.Enabled -and $_.DefaultOutboundAction -eq "Block"
    }).Count -gt 0

    Get-NetFirewallRule -DisplayName $OutboundRuleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue

    if ($outboundBlocked) {
        $remoteAddresses = @()
        $parsedIp = $null
        if ([Net.IPAddress]::TryParse($endpointHost, [ref]$parsedIp)) {
            $remoteAddresses = @($endpointHost)
        }
        else {
            try {
                $remoteAddresses = @(
                    Resolve-DnsName -Name $endpointHost -Type A -ErrorAction Stop |
                    Select-Object -ExpandProperty IPAddress -Unique
                )
            }
            catch {
                throw "Outbound traffic is blocked, and the WireGuard server hostname could not be resolved."
            }
        }

        New-NetFirewallRule `
            -DisplayName $OutboundRuleName `
            -Direction Outbound `
            -Action Allow `
            -Protocol UDP `
            -RemoteAddress $remoteAddresses `
            -RemotePort $endpointPort `
            -Profile Any | Out-Null

        Write-Ok "Allowed outbound UDP $endpointPort to the WireGuard server."
    }
    else {
        Write-Ok "Windows outbound policy already allows WireGuard traffic. No outbound rule was needed."
    }
}

function Prepare-FactoryAgentManualRunner {
    param(
        [Parameter(Mandatory = $true)][string]$UvExe,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)]$Secrets
    )

    Write-Step "Preparing the Factory Agent manual runner"

    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    $factoryVpnIp = ($FactoryVpnAddress -split "/")[0]
    $runtimeLogPath = Join-Path $LogsDir "FactoryAgent-runtime.log"

    $runner = @"
`$ErrorActionPreference = "Stop"
`$secrets = Get-Content -LiteralPath "$SecretsPath" -Raw | ConvertFrom-Json
`$env:APP_ENV = "production"
`$env:FACTORY_AGENT_API_KEY = `$secrets.FACTORY_AGENT_API_KEY
`$env:RECEIVER_API_KEY = `$secrets.RECEIVER_API_KEY
Set-Location -LiteralPath "$FactoryDir"
"Factory Agent manual runner started at `$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))" | Out-File -FilePath "$runtimeLogPath" -Append -Encoding UTF8
& "$UvExe" run --python "$PythonExe" python -m agent.main --mode server --config "$ConfigPath" --host "$factoryVpnIp" --port $FactoryAgentPort *>> "$runtimeLogPath"
exit `$LASTEXITCODE
"@

    Set-Content -LiteralPath $RunnerPath -Value $runner -Encoding UTF8
    Protect-PathForAdmins -Path $RunnerPath

    # This version intentionally does not create a Scheduled Task or any automatic startup trigger.
    # If an older version created the task, remove it so the Factory Agent starts only when
    # FactoryServiceManager.exe is run manually.
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Info "Removing existing automatic Scheduled Task from an older installer version: $TaskName"
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Write-Ok "Manual Factory Agent runner prepared: $RunnerPath"
}

function Start-FactoryAgentManualProcess {
    Write-Step "Starting the Factory Agent manually"

    $factoryVpnIp = ($FactoryVpnAddress -split "/")[0]
    $healthUrl = "http://$factoryVpnIp`:$FactoryAgentPort/health"

    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        if ($response.status -eq "ok") {
            Write-Ok "Factory Agent is already running: $healthUrl"
            return
        }
    }
    catch {
        # Not running yet. Start it below.
    }

    if (-not (Test-Path -LiteralPath $RunnerPath -PathType Leaf)) {
        throw "Factory Agent runner was not found: $RunnerPath"
    }

    $stdoutPath = Join-Path $LogsDir "FactoryAgent-process.stdout.log"
    $stderrPath = Join-Path $LogsDir "FactoryAgent-process.stderr.log"

    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$RunnerPath`"") `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath | Out-Null

    Write-Ok "Factory Agent process started manually."
    Write-Info "Runtime log: $(Join-Path $LogsDir 'FactoryAgent-runtime.log')"
}

function Test-FactoryAgentHealth {
    param(
        [Parameter(Mandatory = $true)]$Secrets
    )

    Write-Step "Testing the Factory Agent"

    $factoryVpnIp = ($FactoryVpnAddress -split "/")[0]
    $healthUrl = "http://$factoryVpnIp`:$FactoryAgentPort/health"

    $success = $false
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        Start-Sleep -Seconds 2
        try {
            $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
            if ($response.status -eq "ok") {
                $success = $true
                break
            }
        }
        catch {
            Write-Info "Waiting for Factory Agent health endpoint, attempt $attempt of 12."
        }
    }

    if ($success) {
        Write-Ok "Factory Agent health check succeeded: $healthUrl"

        $schemaUrl = "http://$factoryVpnIp`:$FactoryAgentPort/agent/schema"
        try {
            $headers = @{
                Authorization = "Bearer $($Secrets.FACTORY_AGENT_API_KEY)"
            }
            $schemaResponse = Invoke-RestMethod -Uri $schemaUrl -Headers $headers -TimeoutSec 30
            Write-Ok "Factory Agent authenticated database/schema check succeeded."
        }
        catch {
            throw "The Factory Agent started, but it could not read the configured SQLite database as the service account. Details: $($_.Exception.Message)"
        }
    }
    else {
        Write-WarnLine "Factory Agent did not answer its local health check yet."
        Write-WarnLine "Review the bootstrap log and the Factory Agent runtime log."
    }

    $receiverUrl = "http://$ReceiverVpnIp`:$ReceiverPort/health"
    try {
        $receiverResponse = Invoke-RestMethod -Uri $receiverUrl -TimeoutSec 8
        Write-Ok "Receiver is reachable through WireGuard: $receiverUrl"
    }
    catch {
        Write-WarnLine "Receiver is not reachable yet at $receiverUrl."
        Write-WarnLine "This is expected until the server registers the Factory public key and the Receiver uses the same API keys."
    }

    return $success
}

function Write-DeploymentFiles {
    param(
        [Parameter(Mandatory = $true)]$Secrets,
        [Parameter(Mandatory = $true)][string]$FactoryPublicKey,
        [Parameter(Mandatory = $true)][string]$DatabasePath,
        [Parameter(Mandatory = $true)][string]$PythonVersion,
        [Parameter(Mandatory = $true)][bool]$HealthSucceeded
    )

    Write-Step "Writing deployment information"

    $factoryVpnIp = ($FactoryVpnAddress -split "/")[0]
    $receiverUrl = "http://$ReceiverVpnIp`:$ReceiverPort"
    $factoryUrl = "http://$factoryVpnIp`:$FactoryAgentPort"

    $share = @"
INDUSTRIAL DATA TRANSFER - FACTORY BOOTSTRAP INFORMATION
Generated: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")

IMPORTANT
Send this file only through an approved secure channel.
It contains API keys that allow access to the Factory Agent and Receiver API.

WIREGUARD SERVER ACTION
Add this peer to /etc/wireguard/wg0.conf on the public Ubuntu server:

[Peer]
# Factory Agent
PublicKey = $FactoryPublicKey
AllowedIPs = $factoryVpnIp/32

Then run:

sudo systemctl restart wg-quick@wg0
sudo wg

FACTORY WIREGUARD INFORMATION
Factory public key: $FactoryPublicKey
Factory VPN address: $FactoryVpnAddress
Server endpoint: $WireGuardServerEndpoint
Server public key: $WireGuardServerPublicKey

RECEIVER CONFIGURATION
Factory Agent URL: $factoryUrl
Receiver URL used by Factory: $receiverUrl

Use these same API keys on the Receiver computer:

FACTORY_AGENT_API_KEY=$($Secrets.FACTORY_AGENT_API_KEY)
RECEIVER_API_KEY=$($Secrets.RECEIVER_API_KEY)

RECEIVER WINDOWS FIREWALL
Allow inbound TCP $ReceiverPort from Factory VPN IP $factoryVpnIp.

FACTORY WINDOWS FIREWALL
Inbound TCP $FactoryAgentPort is restricted to Receiver VPN IP $ReceiverVpnIp.
WireGuard uses outbound UDP to $WireGuardServerEndpoint.

AFTER SERVER AND RECEIVER CONFIGURATION
Run the same Factory bootstrap EXE again, or verify:

1. On the Ubuntu server:
   sudo wg

2. From the Receiver computer:
   Test-NetConnection $factoryVpnIp -Port $FactoryAgentPort
   curl.exe $factoryUrl/health

3. From the Factory computer:
   Test-NetConnection $ReceiverVpnIp -Port $ReceiverPort
   curl.exe $receiverUrl/health
"@

    Set-Content -LiteralPath $SharePath -Value $share -Encoding UTF8
    Protect-PathForAdmins -Path $SharePath
    Grant-ReadAccessToAllUsers -Path $SharePath

    $report = @"
INDUSTRIAL DATA TRANSFER - FACTORY DEPLOYMENT REPORT
Generated: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Bootstrap version: $ScriptVersion

Install root: $InstallRoot
Project directory: $FactoryDir
SQLite database: $DatabasePath
Python: $PythonVersion
Factory VPN address: $FactoryVpnAddress
Receiver VPN IP: $ReceiverVpnIp
WireGuard server endpoint: $WireGuardServerEndpoint
Factory Agent URL: $factoryUrl
Receiver URL: $receiverUrl
Automatic startup: Disabled. WireGuard and the Factory Agent start only when FactoryServiceManager.exe is run manually.
WireGuard tunnel: $TunnelName
Local health check succeeded: $HealthSucceeded

Sensitive values are stored separately and protected for Administrators and SYSTEM.
Share file: $SharePath
Log file: $LogPath
"@

    Set-Content -LiteralPath $ReportPath -Value $report -Encoding UTF8

    $state = [ordered]@{
        BootstrapVersion = $ScriptVersion
        LastRunUtc = [DateTime]::UtcNow.ToString("o")
        InstallRoot = $InstallRoot
        DatabasePath = $DatabasePath
        FactoryVpnAddress = $FactoryVpnAddress
        ReceiverVpnIp = $ReceiverVpnIp
        FactoryAgentPort = $FactoryAgentPort
        ReceiverPort = $ReceiverPort
        AutomaticStartup = $false
        TaskName = $null
        TunnelName = $TunnelName
        WireGuardServiceName = ('WireGuardTunnel$' + $TunnelName)
        RunnerPath = $RunnerPath
        WireGuardServerEndpoint = $WireGuardServerEndpoint
        FactoryWireGuardPublicKey = $FactoryPublicKey
        HealthSucceeded = $HealthSucceeded
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8

    Write-Ok "Deployment report: $ReportPath"
    Write-Ok "Secure share file: $SharePath"
}

try {
    if (-not (Test-IsAdministrator)) {
        Write-Host "This installer must be run as Administrator." -ForegroundColor Red
        if (-not $NonInteractive) {
            Read-Host "Press Enter to close"
        }
        exit 1
    }

    if ($InstallRoot.StartsWith("\\")) {
        throw "The installer cannot run from a network or UNC path. Copy the EXE to a permanent folder on the local Windows drive and run it again."
    }

    $installRootPathRoot = [System.IO.Path]::GetPathRoot($InstallRoot)
    $installDrive = New-Object System.IO.DriveInfo($installRootPathRoot)

    if ($installDrive.DriveType -ne [System.IO.DriveType]::Fixed) {
        throw "The EXE is not located on a fixed local drive. Copy it to a permanent local folder, for example the Desktop or D:\FactoryInstaller, and run it again. Do not run it directly from a USB drive or network share."
    }

    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    Start-Transcript -Path $LogPath -Append | Out-Null

    Write-Host ""
    Write-Host "Industrial Data Transfer Factory Bootstrap" -ForegroundColor Cyan
    Write-Host "Version $ScriptVersion" -ForegroundColor Gray
    Write-Host "Run as Administrator: confirmed" -ForegroundColor Green
    Write-Host "Installer location: $BootstrapLauncherDirectory" -ForegroundColor Gray
    Write-Host "Installation folder: $InstallRoot" -ForegroundColor Gray
    Write-ProgressLog "Industrial Data Transfer Factory Bootstrap"
    Write-ProgressLog "Version $ScriptVersion"
    Write-ProgressLog "Run as Administrator: confirmed"
    Write-ProgressLog "Installer location: $BootstrapLauncherDirectory"
    Write-ProgressLog "Installation folder: $InstallRoot"
    Flush-ConsoleOutput

    $WireGuardServerEndpoint = Read-RequiredValue `
        -Prompt "Enter the public WireGuard server endpoint, for example 35.10.20.30:51820" `
        -CurrentValue $WireGuardServerEndpoint

    $WireGuardServerPublicKey = Read-RequiredValue `
        -Prompt "Enter the public key of the Ubuntu WireGuard server" `
        -CurrentValue $WireGuardServerPublicKey

    if (-not (Test-Base64Key $WireGuardServerPublicKey)) {
        throw "The WireGuard server public key is not a valid 32-byte Base64 key."
    }

    $endpointPort = Get-EndpointPort -Endpoint $WireGuardServerEndpoint
    if ($endpointPort -lt 1 -or $endpointPort -gt 65535) {
        throw "The WireGuard endpoint port is invalid."
    }

    Write-Step "Selecting the SQLite database"
    $databasePath = Select-SqliteDatabase
    Write-Ok "Selected database: $databasePath"

    $wireGuardTools = Ensure-WireGuard
    $runtime = Ensure-UvAndPython

    if (-not $runtime -or -not $runtime.UvExe -or -not $runtime.PythonExe) {
        throw "Runtime detection did not return valid uv and Python executable paths."
    }

    Ensure-ProjectSource `
        -UvExe $runtime.UvExe `
        -PythonExe $runtime.PythonExe

    $secrets = Get-OrCreateSecrets
    Update-FactoryConfig -DatabasePath $databasePath

    Test-SqliteDatabase `
        -UvExe $runtime.UvExe `
        -PythonExe $runtime.PythonExe `
        -DatabasePath $databasePath

    $wg = Configure-WireGuardTunnel `
        -WireGuardExe $wireGuardTools.WireGuardExe `
        -WgExe $wireGuardTools.WgExe

    Configure-WindowsFirewall
    Prepare-FactoryAgentManualRunner `
        -UvExe $runtime.UvExe `
        -PythonExe $runtime.PythonExe `
        -Secrets $secrets
    Start-FactoryAgentManualProcess
    $healthSucceeded = Test-FactoryAgentHealth -Secrets $secrets

    Write-DeploymentFiles `
        -Secrets $secrets `
        -FactoryPublicKey $wg.PublicKey `
        -DatabasePath $databasePath `
        -PythonVersion $runtime.PythonVersion `
        -HealthSucceeded $healthSucceeded

    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor Green
    Write-Host "LOCAL FACTORY INSTALLATION COMPLETED" -ForegroundColor Green
    Write-Host ("=" * 78) -ForegroundColor Green
    Write-Host ""
    Write-Host "The Factory computer is configured and the Factory Agent was started." -ForegroundColor White
    Write-Host ""
    Write-Host "Required next action:" -ForegroundColor Yellow
    Write-Host "Send this file securely to the system administrator:" -ForegroundColor Yellow
    Write-Host $SharePath -ForegroundColor Cyan
    Write-Host ""
    Write-Host "The administrator must add the Factory public key to the Ubuntu WireGuard server" -ForegroundColor Yellow
    Write-Host "and configure the same API keys on the Receiver computer." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Deployment report: $ReportPath" -ForegroundColor Gray
    Write-Host "Log file: $LogPath" -ForegroundColor Gray
    Write-ProgressLog ""
    Write-ProgressLog ("=" * 78)
    Write-ProgressLog "LOCAL FACTORY INSTALLATION COMPLETED"
    Write-ProgressLog ("=" * 78)
    Write-ProgressLog "The Factory computer is configured and the Factory Agent was started."
    Write-ProgressLog "Required next action:"
    Write-ProgressLog "Send this file securely to the system administrator:"
    Write-ProgressLog $SharePath
    Write-ProgressLog "Deployment report: $ReportPath"
    Write-ProgressLog "Log file: $LogPath"
    Flush-ConsoleOutput

    Stop-Transcript | Out-Null
    if (-not $NonInteractive) {
        Read-Host "Press Enter to close"
    }
    exit 0
}
catch {
    Stop-WithError -Message $_.Exception.Message
}
