# Starts the Referral Finder site in WSL (if it isn't already running) and opens it in the browser.
# The Windows shortcut runs this; scripts/windows/install-shortcut.ps1 sets that up. The site runs
# in a minimized WSL window: close that window to stop it.
param(
    [string]$Distro = "Ubuntu",
    [string]$Folder = "~/temp-scripts/referral-finder"
)

$url = "http://localhost:8000"

function Test-Site {
    # 127.0.0.1, not localhost: Windows tries IPv6 (::1) first, which WSL doesn't forward, and times out.
    try { Invoke-WebRequest "http://127.0.0.1:8000" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

if (-not (Test-Site)) {
    # One string with explicit quotes: Start-Process joins an array with spaces and drops the quoting.
    Start-Process wsl.exe -WindowStyle Minimized `
        -ArgumentList "-d $Distro -e bash -lc `"cd $Folder && exec .venv/bin/refer web`""
    for ($i = 0; $i -lt 60 -and -not (Test-Site); $i++) { Start-Sleep -Seconds 1 }
}
Start-Process $url
