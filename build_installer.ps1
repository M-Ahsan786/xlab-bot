<#  Compile the Windows installer (needs Inno Setup 6 + a prior build_app.ps1).
    Output: installer\Output\ScoringAgent-Setup-<version>.exe  #>
[CmdletBinding()] param()
$ErrorActionPreference='Stop'
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path dist\ScoringAgent\ScoringAgent.exe)){ Write-Host "Build the app first: build_app.ps1"; exit 1 }
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe","$env:ProgramFiles\Inno Setup 6\ISCC.exe","$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
  Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc){ Write-Host "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup"; exit 1 }
& $iscc installer\scoring-agent.iss
Write-Host "Installer -> installer\Output\"
