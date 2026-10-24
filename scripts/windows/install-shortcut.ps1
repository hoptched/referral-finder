# Creates "Referral Finder" shortcuts on the Desktop and in the Start menu that run
# start-referral-finder.ps1. Run once from Windows PowerShell, from this folder:
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\<path to repo>\scripts\windows\install-shortcut.ps1
# Then right-click the Desktop shortcut and choose "Pin to taskbar".
param(
    [string]$Distro = "Ubuntu",
    [string]$Folder = "~/temp-scripts/referral-finder"
)

# Windows won't run a script from a \\wsl.localhost path without prompting, so keep a local copy.
$dir = Join-Path $env:LOCALAPPDATA "ReferralFinder"
New-Item -ItemType Directory -Force $dir | Out-Null
$script = Join-Path $dir "start-referral-finder.ps1"
Copy-Item (Join-Path $PSScriptRoot "start-referral-finder.ps1") $script -Force

$shell = New-Object -ComObject WScript.Shell
foreach ($dest in [Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs")) {
    $lnk = $shell.CreateShortcut((Join-Path $dest "Referral Finder.lnk"))
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Distro $Distro -Folder `"$Folder`""
    $lnk.IconLocation = "%SystemRoot%\System32\shell32.dll,13"
    $lnk.Description = "Start the Referral Finder site and open it"
    $lnk.Save()
    Write-Output "Created $($lnk.FullName)"
}
