# Adds the launcher to the Start menu so it is searchable by name.
#
#   powershell -ExecutionPolicy Bypass -File tools\install_start_menu.ps1
#
# Installs per-user (no admin needed). Pass -Uninstall to remove it again.

param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$link = Join-Path $startMenu 'Delta-RetroArch Synchronizer.lnk'

if ($Uninstall) {
    if (Test-Path $link) { Remove-Item $link; "Removed $link" } else { "Nothing to remove." }
    exit 0
}

# pythonw runs the GUI without a console window behind it. Resolve it from the
# python on PATH rather than guessing an install location.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python not found on PATH" }
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$target = Join-Path $root 'launch_gui.pyw'
$icon = Join-Path $root 'assets\synchronizer.ico'
if (-not (Test-Path $target)) { throw "missing $target" }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $pythonw
# Quoted: the project path contains spaces.
$shortcut.Arguments = '"' + $target + '"'
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'Sync Delta and RetroArch saves, then play'
if (Test-Path $icon) { $shortcut.IconLocation = $icon }
$shortcut.Save()

"Installed: $link"
"  target : $pythonw"
"  args   : $($shortcut.Arguments)"
"  icon   : $icon"
""
"Search the Start menu for 'Delta' to find it."
