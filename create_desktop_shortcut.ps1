# create_desktop_shortcut.ps1
# Creates a desktop shortcut for the MLB VIP Model control panel.

$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "MLB VIP Model.lnk"
$TargetPath = Join-Path $PSScriptRoot "launch_mlb_model.bat"
$WorkDir = $PSScriptRoot

if (-not (Test-Path $TargetPath)) {
    Write-Host "[ERROR] launch_mlb_model.bat not found at: $TargetPath"
    exit 1
}

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.WorkingDirectory = $WorkDir
$Shortcut.Description = "MLB VIP Model Control Panel"
$Shortcut.WindowStyle = 1  # Normal window

# Use a generic icon if available, otherwise use the bat file icon
$IconPath = Join-Path $WorkDir "output\icon.ico"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
} else {
    $Shortcut.IconLocation = "shell32.dll,175"
}

$Shortcut.Save()
Write-Host "[OK] Desktop shortcut created: $ShortcutPath"
