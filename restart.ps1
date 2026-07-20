# Digital Detox restart helper. Runs elevated and hidden (launched by
# restart.bat). Kills whatever listens on port 8850, then relaunches
# app.py with pythonw. Writes every step to restart.log. Never
# relaunches itself, so it cannot loop.
$ErrorActionPreference = "SilentlyContinue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $dir "restart.log"
function Log($m) {
    Add-Content -Path $log -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m)
}
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Log "restart requested (admin=$admin)"
if (-not $admin) {
    Log "not elevated - abort without relaunching (loop guard)"
    exit 1
}
$owners = Get-NetTCPConnection -State Listen -LocalPort 8850 |
    Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $owners) {
    Log "stopping old instance pid $p"
    Stop-Process -Id $p -Force
}
Start-Sleep -Seconds 1
$exe = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\pythonw.exe"
if (-not (Test-Path $exe)) { $exe = "pythonw.exe" }
Start-Process -FilePath $exe -ArgumentList ('"' + (Join-Path $dir "app.py") + '"') -WorkingDirectory $dir -WindowStyle Hidden
Log "launched $exe app.py"
