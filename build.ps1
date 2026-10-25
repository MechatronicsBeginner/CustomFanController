# Builds CustomFanController.exe into .\dist\CustomFanController\
#
# Why an .exe: Windows remembers tray icons per program. Run as a script, the
# icon belongs to "pythonw.exe" (shared with every Python tray app and listed
# under that name in Settings). As its own .exe it has its own name and its
# own "always show on taskbar" setting.
#
# Run from a normal (non-admin) PowerShell in this folder:
#   pwsh -ExecutionPolicy Bypass -File .\build.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not (Test-Path (Join-Path $root "LpcACPIEC.bin"))) {
    throw "LpcACPIEC.bin is missing. It is not included in the repository - see 'Requirements and setup' in README.md."
}
$out  = Join-Path $root "dist\CustomFanController"
$work = Join-Path $env:TEMP "CustomFanController-build"

if (Get-Process CustomFanController -ErrorAction SilentlyContinue) {
    throw "CustomFanController.exe is running. Exit it from the tray first (fans go back to the BIOS)."
}

py -3 -m pip install --upgrade pyinstaller pystray pillow
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

py -3 -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name CustomFanController `
    --icon (Join-Path $root "assets\app.ico") `
    --version-file (Join-Path $root "assets\version_info.txt") `
    --hidden-import pystray._win32 `
    --distpath (Join-Path $root "dist") --workpath $work --specpath $work `
    (Join-Path $root "CustomFanController.pyw")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

# Data files live next to the exe (the module was checked for above)
Copy-Item (Join-Path $root "LpcACPIEC.bin") $out -Force
New-Item -ItemType Directory (Join-Path $out "assets") -Force | Out-Null
Copy-Item (Join-Path $root "assets\app.ico") (Join-Path $out "assets") -Force
# Carry existing settings over from script mode (first build only)
$cfg = Join-Path $root "config.json"
if ((Test-Path $cfg) -and -not (Test-Path (Join-Path $out "config.json"))) {
    Copy-Item $cfg $out
}

Write-Host ""
Write-Host "Built: $out\CustomFanController.exe" -ForegroundColor Green
