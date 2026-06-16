#requires -version 5.1
<##
.SYNOPSIS
    Graphical installer for the Industrial Data Transfer Factory Agent.
.DESCRIPTION
    This script is compiled to FactoryDeploymentInstaller.exe.

    The same EXE is copied after installation as FactoryServiceManager.exe. When the copied EXE is
    started under that file name, it starts WireGuard and the Factory Agent manually and does not reinstall anything.
##>

[CmdletBinding()]
param(
    [string]$WireGuardServerEndpoint = @@WG_ENDPOINT_LITERAL@@,
    [string]$WireGuardServerPublicKey = @@WG_SERVER_KEY_LITERAL@@,
    [string]$DefaultInstallRoot = @@DEFAULT_INSTALL_ROOT_LITERAL@@,
    [string]$FactoryVpnIp = '10.10.0.2',
    [int]$FactoryAgentPort = 9000,
    [switch]$ServiceManager
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# The build script replaces this placeholder with a human-readable PowerShell string array.
# Each item is one original line from src\FactoryBootstrap.ps1.
$script:EmbeddedFactoryBootstrapLines = @(
@@FACTORY_BOOTSTRAP_LINES@@
)

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-CurrentExecutablePath {
    try {
        $path = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) { return $path }
    }
    catch {}

    if ($PSCommandPath -and (Test-Path -LiteralPath $PSCommandPath -PathType Leaf)) { return $PSCommandPath }
    return $MyInvocation.MyCommand.Path
}

function Test-ServiceManagerMode {
    if ($ServiceManager) { return $true }
    $currentExe = Get-CurrentExecutablePath
    $fileName = [System.IO.Path]::GetFileNameWithoutExtension($currentExe)
    return ($fileName -ieq 'FactoryServiceManager')
}

function Test-WireGuardPublicKey {
    param([string]$Value)
    try { return ([Convert]::FromBase64String($Value)).Length -eq 32 }
    catch { return $false }
}

function Test-WireGuardEndpoint {
    param([string]$Endpoint)

    if ($Endpoint -notmatch '^.+:\d+$') {
        throw 'WireGuard endpoint must look like PUBLIC_SERVER_IP_OR_DNS:51820.'
    }

    $hostPart = $Endpoint.Substring(0, $Endpoint.LastIndexOf(':'))
    if ($hostPart -match '^10\.10\.') {
        throw 'Do not use 10.10.0.1 as the WireGuard endpoint. Use the public IP address or public DNS name of the Ubuntu WireGuard server, for example 35.10.20.30:51820.'
    }

    if ($hostPart -match '^(10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.)') {
        throw 'The WireGuard endpoint looks like a private IP address. The endpoint must normally be the public IP address or public DNS name of the Ubuntu WireGuard server.'
    }

    return $true
}

function Quote-Argument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Get-EmbeddedTempDirectory {
    $dir = Join-Path $env:TEMP 'IndustrialDataTransferFactoryBootstrap'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    return $dir
}

function Get-EmbeddedFactoryBootstrapPath {
    $dir = Get-EmbeddedTempDirectory
    $path = Join-Path $dir 'FactoryBootstrap.embedded.ps1'
    Set-Content -LiteralPath $path -Value $script:EmbeddedFactoryBootstrapLines -Encoding UTF8
    return $path
}

function Get-FactoryState {
    param([string]$InstallRoot)
    $statePath = Join-Path $InstallRoot 'bootstrap-state.json'
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        return (Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json)
    }
    return $null
}

function Start-RequiredFactoryServices {
    param([string]$InstallRoot)

    $messages = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
        throw "Installation folder does not exist: $InstallRoot"
    }

    $state = Get-FactoryState -InstallRoot $InstallRoot
    $wireGuardServiceName = 'WireGuardTunnel$factory-agent'
    $runnerPath = Join-Path $InstallRoot 'runtime\Start-FactoryAgent.ps1'
    $healthIp = $FactoryVpnIp
    $healthPort = $FactoryAgentPort

    if ($state) {
        if ($state.WireGuardServiceName) { $wireGuardServiceName = [string]$state.WireGuardServiceName }
        if ($state.RunnerPath) { $runnerPath = [string]$state.RunnerPath }
        if ($state.FactoryVpnAddress) { $healthIp = ([string]$state.FactoryVpnAddress -split '/')[0] }
        if ($state.FactoryAgentPort) { $healthPort = [int]$state.FactoryAgentPort }
        [void]$messages.Add("Loaded installation state: $(Join-Path $InstallRoot 'bootstrap-state.json')")
    }
    else {
        [void]$messages.Add('No bootstrap-state.json found. Default service and runner paths will be used.')
    }

    $service = Get-Service -Name $wireGuardServiceName -ErrorAction SilentlyContinue
    if ($service) {
        if ($service.Status -ne 'Running') {
            Start-Service -Name $wireGuardServiceName
            Start-Sleep -Seconds 2
            [void]$messages.Add("Started WireGuard service: $wireGuardServiceName")
        }
        else {
            [void]$messages.Add("WireGuard service is already running: $wireGuardServiceName")
        }
    }
    else {
        [void]$messages.Add("WireGuard service was not found: $wireGuardServiceName")
    }

    if (Test-Path -LiteralPath $runnerPath -PathType Leaf) {
        $healthUrlBeforeStart = "http://$healthIp`:$healthPort/health"
        $alreadyRunning = $false
        try {
            $response = Invoke-RestMethod -Uri $healthUrlBeforeStart -TimeoutSec 3
            if ($response.status -eq 'ok') { $alreadyRunning = $true }
        }
        catch {}

        if ($alreadyRunning) {
            [void]$messages.Add("Factory Agent is already running: $healthUrlBeforeStart")
        }
        else {
            $stdoutPath = Join-Path $InstallRoot 'logs\FactoryAgent-process.stdout.log'
            $stderrPath = Join-Path $InstallRoot 'logs\FactoryAgent-process.stderr.log'
            New-Item -ItemType Directory -Path (Split-Path -Parent $stdoutPath) -Force | Out-Null

            Start-Process `
                -FilePath 'powershell.exe' `
                -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$runnerPath`"") `
                -WindowStyle Hidden `
                -RedirectStandardOutput $stdoutPath `
                -RedirectStandardError $stderrPath | Out-Null

            Start-Sleep -Seconds 3
            [void]$messages.Add("Started Factory Agent manually using: $runnerPath")
        }
    }
    else {
        [void]$messages.Add("Factory Agent runner was not found: $runnerPath")
    }

    $healthUrl = "http://$healthIp`:$healthPort/health"
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 8
        if ($response.status -eq 'ok') {
            [void]$messages.Add("Factory Agent health check succeeded: $healthUrl")
        }
        else {
            [void]$messages.Add("Factory Agent answered, but status was not ok: $healthUrl")
        }
    }
    catch {
        [void]$messages.Add("Factory Agent health check did not succeed yet: $healthUrl")
        [void]$messages.Add($_.Exception.Message)
    }

    return ($messages -join [Environment]::NewLine)
}

function Copy-SelfAsServiceManager {
    param([Parameter(Mandatory=$true)][string]$InstallRoot)

    $currentExe = Get-CurrentExecutablePath
    if (-not (Test-Path -LiteralPath $currentExe -PathType Leaf)) {
        throw "Could not find current executable path: $currentExe"
    }

    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    $target = Join-Path $InstallRoot 'FactoryServiceManager.exe'
    Copy-Item -LiteralPath $currentExe -Destination $target -Force

    $configPath = $currentExe + '.config'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        Copy-Item -LiteralPath $configPath -Destination ($target + '.config') -Force
    }

    return $target
}

function Write-GuiLog {
    param(
        [Parameter(Mandatory=$true)][string]$InstallRoot,
        [Parameter(Mandatory=$true)][string]$Text
    )

    try {
        New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
        $path = Join-Path $InstallRoot 'FactoryDeploymentInstaller-GUI.log'
        Set-Content -LiteralPath $path -Value $Text -Encoding UTF8
    }
    catch {}
}

function Test-InstallationLooksComplete {
    param(
        [Parameter(Mandatory=$true)][string]$InstallRoot,
        [string]$StdoutText
    )

    $requiredFiles = @(
        (Join-Path $InstallRoot 'DEPLOYMENT_REPORT.txt'),
        (Join-Path $InstallRoot 'PLEASE_SEND_THIS_FILE_TO_US.txt'),
        (Join-Path $InstallRoot 'bootstrap-state.json')
    )

    foreach ($path in $requiredFiles) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    }

    if ($StdoutText -match 'LOCAL FACTORY INSTALLATION COMPLETED') { return $true }
    return $true
}

function Show-ServiceManagerWindow {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.Application]::EnableVisualStyles()

    if (-not (Test-IsAdministrator)) {
        [System.Windows.Forms.MessageBox]::Show('FactoryServiceManager.exe must be run as Administrator.', 'Factory Service Manager', 'OK', 'Error') | Out-Null
        return
    }

    $currentExe = Get-CurrentExecutablePath
    $installRoot = Split-Path -Parent $currentExe

    try {
        $result = Start-RequiredFactoryServices -InstallRoot $installRoot
        [System.Windows.Forms.MessageBox]::Show($result, 'Factory Service Manager', 'OK', 'Information') | Out-Null
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Factory Service Manager', 'OK', 'Error') | Out-Null
    }
}

if (Test-ServiceManagerMode) {
    Show-ServiceManagerWindow
    return
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Industrial Data Transfer Factory Installer'
$form.Width = 850
$form.Height = 620
$form.StartPosition = 'CenterScreen'
$form.MinimumSize = New-Object System.Drawing.Size(800, 580)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)

$title = New-Object System.Windows.Forms.Label
$title.Text = 'Industrial Data Transfer Factory Installer'
$title.Font = New-Object System.Drawing.Font('Segoe UI', 13, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Left = 20
$title.Top = 18
$form.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = 'This single EXE installs the Factory Agent and creates FactoryServiceManager.exe for manual startup.'
$subtitle.AutoSize = $true
$subtitle.Left = 20
$subtitle.Top = 52
$subtitle.Width = 790
$form.Controls.Add($subtitle)

$y = 92

$lblInstall = New-Object System.Windows.Forms.Label
$lblInstall.Text = 'Installation folder'
$lblInstall.Left = 20
$lblInstall.Top = $y
$lblInstall.Width = 160
$form.Controls.Add($lblInstall)

$txtInstall = New-Object System.Windows.Forms.TextBox
$txtInstall.Left = 185
$txtInstall.Top = $y - 4
$txtInstall.Width = 520
$txtInstall.Text = $DefaultInstallRoot
$form.Controls.Add($txtInstall)

$btnBrowseInstall = New-Object System.Windows.Forms.Button
$btnBrowseInstall.Text = 'Browse...'
$btnBrowseInstall.Left = 715
$btnBrowseInstall.Top = $y - 6
$btnBrowseInstall.Width = 90
$form.Controls.Add($btnBrowseInstall)

$y += 42
$lblDb = New-Object System.Windows.Forms.Label
$lblDb.Text = 'SQLite database'
$lblDb.Left = 20
$lblDb.Top = $y
$lblDb.Width = 160
$form.Controls.Add($lblDb)

$txtDatabase = New-Object System.Windows.Forms.TextBox
$txtDatabase.Left = 185
$txtDatabase.Top = $y - 4
$txtDatabase.Width = 520
$form.Controls.Add($txtDatabase)

$btnBrowseDb = New-Object System.Windows.Forms.Button
$btnBrowseDb.Text = 'Browse...'
$btnBrowseDb.Left = 715
$btnBrowseDb.Top = $y - 6
$btnBrowseDb.Width = 90
$form.Controls.Add($btnBrowseDb)

$y += 42
$lblEndpoint = New-Object System.Windows.Forms.Label
$lblEndpoint.Text = 'WireGuard endpoint'
$lblEndpoint.Left = 20
$lblEndpoint.Top = $y
$lblEndpoint.Width = 160
$form.Controls.Add($lblEndpoint)

$txtEndpoint = New-Object System.Windows.Forms.TextBox
$txtEndpoint.Left = 185
$txtEndpoint.Top = $y - 4
$txtEndpoint.Width = 620
$txtEndpoint.Text = $WireGuardServerEndpoint
$form.Controls.Add($txtEndpoint)

$y += 42
$lblKey = New-Object System.Windows.Forms.Label
$lblKey.Text = 'Server public key'
$lblKey.Left = 20
$lblKey.Top = $y
$lblKey.Width = 160
$form.Controls.Add($lblKey)

$txtKey = New-Object System.Windows.Forms.TextBox
$txtKey.Left = 185
$txtKey.Top = $y - 4
$txtKey.Width = 620
$txtKey.Text = $WireGuardServerPublicKey
$form.Controls.Add($txtKey)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = 'Tip: use the public IP/DNS of the Ubuntu WireGuard server, not 10.10.0.1.'
$hint.Left = 185
$hint.Top = $y + 26
$hint.Width = 620
$form.Controls.Add($hint)

$y += 62
$btnInstall = New-Object System.Windows.Forms.Button
$btnInstall.Text = 'Install'
$btnInstall.Left = 185
$btnInstall.Top = $y
$btnInstall.Width = 150
$btnInstall.Height = 34
$form.Controls.Add($btnInstall)

$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = 'Close'
$btnClose.Left = 350
$btnClose.Top = $y
$btnClose.Width = 110
$btnClose.Height = 34
$form.Controls.Add($btnClose)

$y += 52
$logBox = New-Object System.Windows.Forms.RichTextBox
$logBox.Left = 20
$logBox.Top = $y
$logBox.Width = 785
$logBox.Height = 280
$logBox.ReadOnly = $true
$logBox.Anchor = 'Top,Bottom,Left,Right'
$logBox.Font = New-Object System.Drawing.Font('Consolas', 9)
$form.Controls.Add($logBox)

$status = New-Object System.Windows.Forms.Label
$status.Text = 'Ready'
$status.Left = 20
$status.Top = 558
$status.Width = 790
$status.Anchor = 'Bottom,Left,Right'
$form.Controls.Add($status)

# Installation runtime state used by the background installer process.
# The process events can run on a background thread, so UI updates are routed
# through Append-LogThreadSafe and the form's UI thread.
$script:InstallInProgress = $false
$script:CoreInstallerProcess = $null
$script:CoreInstallerStderrHeaderShown = $false
$script:CoreInstallerInstallRoot = $null
$script:CoreInstallerProgressLogPath = $null
$script:CoreInstallerProgressLogOffset = 0
$script:CoreInstallerStderrPath = $null
$script:InstallTimer = $null


function Append-Log {
    param([string]$Message)
    if ($null -eq $Message) { return }
    $logBox.AppendText($Message + [Environment]::NewLine)
    $logBox.SelectionStart = $logBox.Text.Length
    $logBox.ScrollToCaret()
    try { [System.Windows.Forms.Application]::DoEvents() } catch {}
}

function Append-LogThreadSafe {
    param([string]$Message)
    if ($null -eq $Message) { return }

    try {
        if ($form.InvokeRequired) {
            $messageCopy = $Message
            [void]$form.BeginInvoke([System.Windows.Forms.MethodInvoker]{
                Append-Log $messageCopy
            })
        }
        else {
            Append-Log $Message
        }
    }
    catch {}
}

function Write-CrashLog {
    param([string]$Message)
    try {
        $crashLog = Join-Path $env:TEMP 'FactoryDeploymentInstaller-crash.log'
        $text = @(
            ('Time: ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')),
            $Message,
            '',
            'Current GUI log:',
            $logBox.Text
        ) -join [Environment]::NewLine
        Set-Content -LiteralPath $crashLog -Value $text -Encoding UTF8
    }
    catch {}
}

if (-not (Test-IsAdministrator)) {
    Append-Log 'This installer must be run as Administrator.'
    Append-Log 'Close it, right-click the EXE, and select Run as administrator.'
    $btnInstall.Enabled = $false
    $status.Text = 'Administrator rights are required.'
}

function Read-TextFileSafely {
    param([Parameter(Mandatory=$true)][string]$Path)

    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8, $true)
            try { return $reader.ReadToEnd() }
            finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
    }
    catch {
        return ''
    }
}

function Get-FileTextFromOffset {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][ref]$Offset
    )

    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            if ($Offset.Value -gt $stream.Length) { $Offset.Value = 0 }
            [void]$stream.Seek([int64]$Offset.Value, [System.IO.SeekOrigin]::Begin)
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8, $true)
            try {
                $text = $reader.ReadToEnd()
                $Offset.Value = $stream.Position
                return $text
            }
            finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
    }
    catch {
        return ''
    }
}

function Append-ProgressLogDelta {
    if ([string]::IsNullOrWhiteSpace($script:CoreInstallerProgressLogPath)) { return }
    $offsetRef = [ref]$script:CoreInstallerProgressLogOffset
    $newText = Get-FileTextFromOffset -Path $script:CoreInstallerProgressLogPath -Offset $offsetRef
    $script:CoreInstallerProgressLogOffset = $offsetRef.Value
    if (-not [string]::IsNullOrEmpty($newText)) {
        $logBox.AppendText($newText)
        if (-not $newText.EndsWith([Environment]::NewLine)) {
            $logBox.AppendText([Environment]::NewLine)
        }
        $logBox.SelectionStart = $logBox.Text.Length
        $logBox.ScrollToCaret()
    }
}

function Complete-CoreInstallerRun {
    param(
        [Parameter(Mandatory=$true)][string]$InstallRoot,
        [Parameter(Mandatory=$true)][System.Diagnostics.Process]$Process
    )

    Append-ProgressLogDelta
    $exitCode = $Process.ExitCode
    $looksComplete = Test-InstallationLooksComplete -InstallRoot $InstallRoot -StdoutText $logBox.Text

    Append-Log ''
    Append-Log "Installer process finished with exit code: $exitCode"

    if (($exitCode -eq 0) -or $looksComplete) {
        try {
            $manager = Copy-SelfAsServiceManager -InstallRoot $InstallRoot
            Append-Log "Created service-only executable: $manager"
        }
        catch {
            Append-Log "Could not create FactoryServiceManager.exe: $($_.Exception.Message)"
        }

        Write-GuiLog -InstallRoot $InstallRoot -Text $logBox.Text
        $status.Text = 'Installation completed successfully.'
        [System.Windows.Forms.MessageBox]::Show("Installation completed successfully.`n`nFactoryServiceManager.exe was created in the installation folder.", 'Factory Installer', 'OK', 'Information') | Out-Null
    }
    else {
        $stderrText = Read-TextFileSafely -Path $script:CoreInstallerStderrPath
        if (-not [string]::IsNullOrWhiteSpace($stderrText)) {
            Append-Log ''
            Append-Log 'Installer messages:'
            Append-Log $stderrText
        }

        Write-GuiLog -InstallRoot $InstallRoot -Text $logBox.Text
        $status.Text = "Installation failed. Exit code: $exitCode"
        [System.Windows.Forms.MessageBox]::Show("Installation failed. Check the log box and FactoryDeploymentInstaller-GUI.log in the installation folder.", 'Factory Installer', 'OK', 'Error') | Out-Null
    }

    $btnInstall.Enabled = $true
    $btnClose.Enabled = $true
    $script:InstallInProgress = $false
    $script:CoreInstallerProcess = $null
}

function Update-InstallerProgress {
    try {
        if (-not $script:InstallInProgress) { return }
        Append-ProgressLogDelta
        if ($null -ne $script:CoreInstallerProcess -and $script:CoreInstallerProcess.HasExited) {
            if ($null -ne $script:InstallTimer) { $script:InstallTimer.Stop() }
            Complete-CoreInstallerRun -InstallRoot $script:CoreInstallerInstallRoot -Process $script:CoreInstallerProcess
        }
    }
    catch {
        try { if ($null -ne $script:InstallTimer) { $script:InstallTimer.Stop() } } catch {}
        Write-CrashLog $_.Exception.ToString()
        try { Write-GuiLog -InstallRoot $script:CoreInstallerInstallRoot -Text ($logBox.Text + [Environment]::NewLine + $_.Exception.ToString()) } catch {}
        $btnInstall.Enabled = $true
        $btnClose.Enabled = $true
        $script:InstallInProgress = $false
        $status.Text = 'Installation failed.'
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Factory Installer', 'OK', 'Error') | Out-Null
    }
}

function Invoke-CoreInstaller {
    param(
        [Parameter(Mandatory=$true)][string]$InstallRoot,
        [Parameter(Mandatory=$true)][string]$DatabasePath,
        [Parameter(Mandatory=$true)][string]$Endpoint,
        [Parameter(Mandatory=$true)][string]$ServerKey
    )

    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    $coreInstaller = Get-EmbeddedFactoryBootstrapPath

    $script:CoreInstallerInstallRoot = $InstallRoot
    $script:CoreInstallerProgressLogPath = Join-Path $InstallRoot 'FactoryDeploymentInstaller-progress.log'
    $script:CoreInstallerProgressLogOffset = 0
    $script:CoreInstallerStderrPath = Join-Path $InstallRoot 'FactoryDeploymentInstaller-stderr.log'
    Remove-Item -LiteralPath $script:CoreInstallerProgressLogPath, $script:CoreInstallerStderrPath -Force -ErrorAction SilentlyContinue

    $argumentList = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', (Quote-Argument $coreInstaller),
        '-InstallRoot', (Quote-Argument $InstallRoot),
        '-DatabasePath', (Quote-Argument $DatabasePath),
        '-WireGuardServerEndpoint', (Quote-Argument $Endpoint),
        '-WireGuardServerPublicKey', (Quote-Argument $ServerKey),
        '-ProgressLogPath', (Quote-Argument $script:CoreInstallerProgressLogPath),
        '-NonInteractive'
    )

    Append-Log 'Starting installation...'
    Append-Log "Installation folder: $InstallRoot"
    Append-Log "SQLite database: $DatabasePath"
    Append-Log "WireGuard endpoint: $Endpoint"
    Append-Log ''
    Append-Log 'Please wait...'
    Append-Log ''

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = 'powershell.exe'
    $process.StartInfo.Arguments = ($argumentList -join ' ')
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardOutput = $false
    $process.StartInfo.RedirectStandardError = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.WorkingDirectory = $InstallRoot
    $process.EnableRaisingEvents = $false

    $script:CoreInstallerProcess = $process
    $script:InstallInProgress = $true

    [void]$process.Start()

    if ($null -eq $script:InstallTimer) {
        $script:InstallTimer = New-Object System.Windows.Forms.Timer
        $script:InstallTimer.Interval = 700
        $script:InstallTimer.Add_Tick({ Update-InstallerProgress })
    }
    $script:InstallTimer.Start()
}

$btnBrowseInstall.Add_Click({
    try {
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Select the installation folder'
        $dialog.SelectedPath = $txtInstall.Text
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $txtInstall.Text = $dialog.SelectedPath
        }
    }
    catch {
        Write-CrashLog $_.Exception.ToString()
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Factory Installer', 'OK', 'Error') | Out-Null
    }
})

$btnBrowseDb.Add_Click({
    try {
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = 'Select the factory SQLite database'
        $dialog.Filter = 'SQLite databases (*.db;*.sqlite;*.sqlite3)|*.db;*.sqlite;*.sqlite3|All files (*.*)|*.*'
        $dialog.CheckFileExists = $true
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $txtDatabase.Text = $dialog.FileName
        }
    }
    catch {
        Write-CrashLog $_.Exception.ToString()
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Factory Installer', 'OK', 'Error') | Out-Null
    }
})

$btnClose.Add_Click({ $form.Close() })

$btnInstall.Add_Click({
    try {
        if ($script:InstallInProgress) { return }

        $installRoot = $txtInstall.Text.Trim().Trim('"')
        $databasePath = $txtDatabase.Text.Trim().Trim('"')
        $endpoint = $txtEndpoint.Text.Trim()
        $serverKey = $txtKey.Text.Trim()

        if (-not $installRoot) { throw 'Please select an installation folder.' }
        if (-not $databasePath) { throw 'Please select the SQLite database.' }
        if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) { throw "SQLite database does not exist: $databasePath" }
        [void](Test-WireGuardEndpoint -Endpoint $endpoint)
        if (-not (Test-WireGuardPublicKey $serverKey)) { throw 'The WireGuard server public key is not a valid 32-byte Base64 public key.' }

        $btnInstall.Enabled = $false
        $btnClose.Enabled = $false
        $status.Text = 'Installation is running. Do not close this window.'
        [System.Windows.Forms.Application]::DoEvents()

        Invoke-CoreInstaller -InstallRoot $installRoot -DatabasePath $databasePath -Endpoint $endpoint -ServerKey $serverKey
    }
    catch {
        Write-CrashLog $_.Exception.ToString()
        try { Write-GuiLog -InstallRoot ($txtInstall.Text.Trim().Trim('"')) -Text ($logBox.Text + [Environment]::NewLine + $_.Exception.ToString()) } catch {}
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Factory Installer', 'OK', 'Error') | Out-Null
        $status.Text = 'Installation failed.'
        $btnInstall.Enabled = $true
        $btnClose.Enabled = $true
        $script:InstallInProgress = $false
    }
})


$form.Add_FormClosing({
    if ($script:InstallInProgress) {
        $result = [System.Windows.Forms.MessageBox]::Show(
            'Installation is still running. Closing this window may leave the installation incomplete. Do you really want to close it?',
            'Factory Installer',
            'YesNo',
            'Warning'
        )
        if ($result -ne [System.Windows.Forms.DialogResult]::Yes) {
            $_.Cancel = $true
        }
    }
})

$script:InstallInProgress = $false

Append-Log 'Ready.'
Append-Log 'Only this EXE is required on the factory computer.'
Append-Log 'The readable source files are kept in the source package for IT review.'
Append-Log 'Version: v11 stable progress-log polling.'
[void]$form.ShowDialog()
