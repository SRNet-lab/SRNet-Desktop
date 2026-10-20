# SRNet client installer for Windows (per-user, no admin rights needed).
#   powershell -ExecutionPolicy Bypass -File install.ps1              install or upgrade
#   powershell -ExecutionPolicy Bypass -File install.ps1 -WithExits   also install the exits add-on
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
param([switch]$Uninstall, [string]$Prefix = "$env:LOCALAPPDATA\srnet-client", [switch]$NoShortcut, [switch]$WithExits)
$ErrorActionPreference = "Stop"
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bin = Join-Path $Prefix "bin"
$Menu = Join-Path ([Environment]::GetFolderPath("Programs")) "SRNet"

function Remove-FromUserPath($dir) {
  $p = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($p) { [Environment]::SetEnvironmentVariable("Path", (($p -split ";") | Where-Object { $_ -and $_ -ne $dir }) -join ";", "User") }
}

if ($Uninstall) {
  try { if (Test-Path $Prefix) { Remove-Item -Recurse -Force $Prefix } }
  catch { Write-Error "SRNet is still running. Close the SRNet window (and any 'srnet' terminals), then run -Uninstall again." }
  if (Test-Path $Menu) { Remove-Item -Recurse -Force $Menu }
  if (Test-Path "HKCU:\Software\Classes\srnet") { Remove-Item -Recurse -Force "HKCU:\Software\Classes\srnet" }
  Remove-FromUserPath $Bin
  Write-Host "Removed the SRNet client. Your settings in $HOME\.srnet were left in place."
  exit 0
}

# Find Python 3.10+ (the py launcher first, then python on PATH).
$Py = $null
foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
  try {
    $exe = $c[0]; $args0 = @($c | Select-Object -Skip 1)
    & $exe @args0 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = $c; break }
  } catch { }
}
if (-not $Py) {
  Write-Error "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH') or: winget install Python.Python.3.12"
}
$PyExe = $Py[0]; $PyArgs = @($Py | Select-Object -Skip 1)

Write-Host "Installing to $Prefix"
New-Item -ItemType Directory -Force -Path $Prefix, $Bin | Out-Null
$App = Join-Path $Prefix "app"
if (Test-Path $App) { Remove-Item -Recurse -Force $App }
New-Item -ItemType Directory -Force -Path $App | Out-Null
Copy-Item -Recurse (Join-Path $Src "client-cli"), (Join-Path $Src "snapps") $App
Copy-Item (Join-Path $Src "LICENSE"), (Join-Path $Src "LICENSE-APACHE-2.0.txt"), (Join-Path $Src "README.md") $App
Get-ChildItem -Path $App -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

$Venv = Join-Path $Prefix "venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
# Windows PowerShell 5.1 can turn a native program's stderr (pip notices, warnings) into a
# terminating error under $ErrorActionPreference = "Stop". Run Python with "Continue" and judge
# by the exit code only.
function Invoke-Py {
  $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { & $VPy @args 2>&1 | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { Write-Host $_.ToString() } else { $_ } } }
  finally { $ErrorActionPreference = $old }
}
function Test-Module($name) {
  if (-not (Test-Path $VPy)) { return $false }
  Invoke-Py -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('$name') else 1)" | Out-Null
  return ($LASTEXITCODE -eq 0)
}
$HadExits = Test-Module "srnet_exits"
if ((Test-Module "eth_account") -or (Test-Path (Join-Path $Bin "srnet-relayer.cmd"))) {
  # An install from before the split: start a clean venv so no wallet libraries are left in it.
  Write-Host "Removing the old client's wallet libraries"
  Remove-Item -Recurse -Force $Venv
}
if (-not (Test-Path $VPy)) { & $PyExe @PyArgs -m venv $Venv; if ($LASTEXITCODE) { throw "venv failed" } }
Invoke-Py -m pip install --quiet --disable-pip-version-check --upgrade pip
Invoke-Py -m pip install --quiet --disable-pip-version-check "$App\client-cli[qr]"
if ($LASTEXITCODE) { throw "pip install failed" }
$ExitsSrc = if ($env:SRNET_EXITS_SRC) { $env:SRNET_EXITS_SRC } else { "srnet-exits @ git+https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack" }
if ($WithExits -or $HadExits) {
  Write-Host "Installing the exits add-on"
  Invoke-Py -m pip install --quiet --disable-pip-version-check --upgrade $ExitsSrc
  if ($LASTEXITCODE) { throw "pip install of the exits add-on failed" }
}

$Launcher = @"
@echo off
set "PYTHONUTF8=1"
set "SRNET_SNAPPS_DIR=$App\snapps"
"$VPy" -m srnet %*
"@
Set-Content -Path (Join-Path $Bin "srnet.cmd") -Value $Launcher -Encoding ASCII
$OldRelayer = Join-Path $Bin "srnet-relayer.cmd"
if (Test-Path $OldRelayer) { Remove-Item -Force $OldRelayer }

$p = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($p -split ";") -contains $Bin)) {
  [Environment]::SetEnvironmentVariable("Path", (@($p, $Bin) | Where-Object { $_ }) -join ";", "User")
}

if (-not $NoShortcut) {
  New-Item -ItemType Directory -Force -Path $Menu | Out-Null
  $sh = New-Object -ComObject WScript.Shell
  $lnk = $sh.CreateShortcut((Join-Path $Menu "SRNet.lnk"))
  $lnk.TargetPath = Join-Path $Bin "srnet.cmd"
  $lnk.Arguments = "ui"
  $lnk.WorkingDirectory = $HOME
  $lnk.WindowStyle = 7
  $lnk.Description = "SNApps for SRNet"
  $lnk.Save()
}

if (-not $NoShortcut) {
  # srnet:// link handler (per user). It only opens the join sheet; joining needs a click.
  $K = "HKCU:\Software\Classes\srnet"
  New-Item -Force -Path "$K\shell\open\command" | Out-Null
  Set-ItemProperty -Path $K -Name "(default)" -Value "URL:SRNet link"
  Set-ItemProperty -Path $K -Name "URL Protocol" -Value ""
  Set-ItemProperty -Path "$K\shell\open\command" -Name "(default)" -Value ('"' + (Join-Path $Bin "srnet.cmd") + '" open "%1"')
}

Write-Host ""
Write-Host "Done. Open a new terminal, then:"
Write-Host "  srnet ui           open the app in your browser (also in the Start menu: SRNet)"
Write-Host "  srnet doctor       check my setup (writes a report you can send)"
if ($WithExits) { Write-Host "Exits add-on: 'srnet exits' and 'srnet buy' work here; 'srnet connect' is Linux-only for now." }
