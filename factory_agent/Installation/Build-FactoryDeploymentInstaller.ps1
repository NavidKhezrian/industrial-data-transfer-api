#requires -version 5.1
<##
.SYNOPSIS
    Builds one transferable FactoryDeploymentInstaller.exe from readable source files.
.DESCRIPTION
    The final EXE is a single transferable installer for the factory computer.
    During the build, src\FactoryBootstrap.ps1 is embedded in the generated GUI script
    as a readable line-by-line PowerShell string array, so IT can inspect the generated
    source if needed.

    Source files:
      src\FactoryDeploymentInstallerGui.ps1
      src\FactoryBootstrap.ps1

    Output:
      dist\FactoryDeploymentInstaller.exe
##>

[CmdletBinding()]
param(
    [string]$SourceDirectory = (Join-Path $PSScriptRoot 'src'),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'dist'),
    [string]$BuildDirectory = (Join-Path $PSScriptRoot 'build'),
    [string]$OutputExeName = 'FactoryDeploymentInstaller.exe',
    [string]$WireGuardServerEndpoint = '',
    [string]$WireGuardServerPublicKey = '',
    [string]$DefaultInstallRoot = 'C:\IndustrialDataTransfer',
    [switch]$KeepBuildFiles
)

$ErrorActionPreference = 'Stop'

function New-PowerShellSingleQuotedLiteral {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { $Value = '' }
    return "'" + $Value.Replace("'", "''") + "'"
}

function Convert-TextFileToPowerShellStringArrayItems {
    param([Parameter(Mandatory=$true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Source file not found: $Path"
    }

    $lines = Get-Content -LiteralPath $Path -Encoding UTF8
    $items = foreach ($line in $lines) {
        "    '" + $line.Replace("'", "''") + "'"
    }
    return ($items -join [Environment]::NewLine)
}

function Ensure-Ps2Exe {
    if (-not (Get-Module -ListAvailable -Name ps2exe)) {
        Write-Host 'Installing the open-source ps2exe module for the current user...' -ForegroundColor Cyan
        if (-not (Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue)) {
            Install-PackageProvider -Name NuGet -Scope CurrentUser -Force | Out-Null
        }
        Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
        Install-Module -Name ps2exe -Scope CurrentUser -Force
    }
    Import-Module ps2exe -Force
}

function Build-Exe {
    param(
        [Parameter(Mandatory=$true)][string]$InputFile,
        [Parameter(Mandatory=$true)][string]$OutputFile,
        [Parameter(Mandatory=$true)][string]$Title,
        [Parameter(Mandatory=$true)][string]$Description,
        [switch]$NoConsole
    )

    $arguments = @{
        InputFile = $InputFile
        OutputFile = $OutputFile
        x64 = $true
        STA = $true
        RequireAdmin = $true
        SupportOS = $true
        LongPaths = $true
        Title = $Title
        Description = $Description
        Product = 'Industrial Data Transfer Factory Deployment'
        Company = 'Project deployment utility'
        Version = '3.0.0.0'
    }
    if ($NoConsole) { $arguments.NoConsole = $true }
    Invoke-PS2EXE @arguments
}

$guiTemplatePath = Join-Path $SourceDirectory 'FactoryDeploymentInstallerGui.ps1'
$factoryBootstrapPath = Join-Path $SourceDirectory 'FactoryBootstrap.ps1'

if (-not (Test-Path -LiteralPath $guiTemplatePath -PathType Leaf)) {
    throw "GUI source file not found: $guiTemplatePath"
}
if (-not (Test-Path -LiteralPath $factoryBootstrapPath -PathType Leaf)) {
    throw "Factory bootstrap source file not found: $factoryBootstrapPath"
}

Ensure-Ps2Exe

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null

$generatedGuiPath = Join-Path $BuildDirectory 'FactoryDeploymentInstaller.generated.ps1'
$outputExePath = Join-Path $OutputDirectory $OutputExeName

Write-Host 'Reading source files...' -ForegroundColor Cyan
$guiSource = Get-Content -LiteralPath $guiTemplatePath -Raw -Encoding UTF8
$factoryBootstrapLines = Convert-TextFileToPowerShellStringArrayItems -Path $factoryBootstrapPath

Write-Host 'Generating readable combined installer script...' -ForegroundColor Cyan
$guiSource = $guiSource.Replace('@@WG_ENDPOINT_LITERAL@@', (New-PowerShellSingleQuotedLiteral $WireGuardServerEndpoint))
$guiSource = $guiSource.Replace('@@WG_SERVER_KEY_LITERAL@@', (New-PowerShellSingleQuotedLiteral $WireGuardServerPublicKey))
$guiSource = $guiSource.Replace('@@DEFAULT_INSTALL_ROOT_LITERAL@@', (New-PowerShellSingleQuotedLiteral $DefaultInstallRoot))
$guiSource = $guiSource.Replace('@@FACTORY_BOOTSTRAP_LINES@@', $factoryBootstrapLines)

Set-Content -LiteralPath $generatedGuiPath -Value $guiSource -Encoding UTF8

Write-Host 'Building final all-in-one installer EXE...' -ForegroundColor Cyan
Build-Exe `
    -InputFile $generatedGuiPath `
    -OutputFile $outputExePath `
    -Title 'Industrial Data Transfer Factory Installer' `
    -Description 'All-in-one graphical installer for the Factory Agent' `
    -NoConsole

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host 'Generated EXE:' -ForegroundColor Yellow
Write-Host "  $outputExePath" -ForegroundColor Cyan
Write-Host ''
Write-Host 'Readable generated source:' -ForegroundColor Yellow
Write-Host "  $generatedGuiPath" -ForegroundColor Cyan
Write-Host ''
Write-Host 'Transfer only the EXE to the factory computer.' -ForegroundColor Yellow
Write-Host 'On the factory computer, right-click the EXE and select Run as administrator.'
Write-Host 'After installation, the installer copies itself as:' -ForegroundColor Yellow
Write-Host '  <Installation folder>\FactoryServiceManager.exe' -ForegroundColor Cyan
Write-Host 'That file starts the required services later without reinstalling.'

if (-not $KeepBuildFiles) {
    Write-Host ''
    Write-Host 'Build files were kept because they are useful for IT review.' -ForegroundColor DarkGray
    Write-Host 'Delete the build folder manually if you do not need the generated readable source.' -ForegroundColor DarkGray
}
