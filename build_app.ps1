<#  Build Scoring Agent into dist\ScoringAgent\ScoringAgent.exe (windowed GUI app).
    Needs: pip install pyinstaller selenium openpyxl   (the window is Chrome itself)
    The teammate needs Google Chrome + the Edge/IE WebView2 runtime (ships with Windows 11).

    NOTE: this is a --onedir build on purpose. The old --onefile build unpacked ~48 MB into a
    temp folder on EVERY launch, so the window sat greyed out as "Not Responding" for ~25 s
    before it came up. onedir starts in a couple of seconds; the installer ships the folder,
    so nothing changes for the user.  #>
[CmdletBinding()] param([switch]$Clean)
$ErrorActionPreference='Stop'
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ($Clean){ Remove-Item build,dist -Recurse -Force -ErrorAction SilentlyContinue }

# The updater compares the running app's version with the newest GitHub release, so the three
# places the version appears must agree or the app will offer people the wrong thing.
$pyVer   = ([regex]'__version__\s*=\s*"([^"]+)"').Match((Get-Content scoring_agent\version.py -Raw)).Groups[1].Value
$issVer  = ([regex]'#define AppVersion\s+"([^"]+)"').Match((Get-Content installer\scoring-agent.iss -Raw)).Groups[1].Value
$htmlVer = ([regex]'Version ([0-9.]+)').Match((Get-Content scoring_agent\ui\index.html -Raw)).Groups[1].Value
if ($pyVer -ne $issVer -or $pyVer -ne $htmlVer) {
  Write-Host "Version mismatch - fix these before building:" -ForegroundColor Red
  Write-Host ("  scoring_agent\version.py      {0}" -f $pyVer)
  Write-Host ("  installer\scoring-agent.iss   {0}" -f $issVer)
  Write-Host ("  ui\index.html (About)         {0}" -f $htmlVer)
  exit 1
}
Write-Host ("Version: {0}" -f $pyVer)
python -m pip install --quiet --upgrade pyinstaller selenium openpyxl
python -m PyInstaller --noconfirm --clean --onedir --windowed `
  --name ScoringAgent `
  --icon assets\scoring-agent.ico `
  --add-data "scoring_agent\ui;ui" `
  --collect-all selenium `
  --collect-submodules openpyxl `
  --exclude-module webview --exclude-module clr --exclude-module clr_loader `
  --exclude-module numpy --exclude-module pandas --exclude-module PIL `
  --exclude-module matplotlib --exclude-module tkinter --exclude-module scipy `
  --exclude-module IPython --exclude-module pytest --exclude-module setuptools `
  --exclude-module openpyxl.utils.dataframe --exclude-module lxml `
  run_app.py
$exe = "dist\ScoringAgent\ScoringAgent.exe"
if (Test-Path $exe){
  $mb = [math]::Round((Get-ChildItem dist\ScoringAgent -Recurse -File | Measure-Object Length -Sum).Sum/1MB,1)
  Write-Host "Built: $exe  (folder $mb MB)"
} else { Write-Host "Build failed - see PyInstaller output above." }
