# Digital Detox restart helper. Runs elevated and hidden (launched by
# restart.bat). Kills EVERY process holding any of our three ports
# (8850 control panel / 8851 black-hole proxy / 80 block page) PLUS any
# pythonw/python process still running our app.py by command line —
# belt-and-suspenders, since a prior instance whose port binds all
# failed can still be alive without holding any of our ports. Only
# looking at port 8850 (the old logic) let orphans holding 80/8851
# survive indefinitely across repeated restarts. Then relaunches
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

$appPath = Join-Path $dir "app.py"
$byPort = Get-NetTCPConnection -State Listen -LocalPort 8850, 8851, 80 -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess
$byCmdline = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @("pythonw.exe", "python.exe") -and $_.CommandLine -and $_.CommandLine -like "*$appPath*" } |
    Select-Object -ExpandProperty ProcessId
$targets = @($byPort) + @($byCmdline) | Where-Object { $_ } | Select-Object -Unique
foreach ($p in $targets) {
    Log "stopping old instance pid $p"
    Stop-Process -Id $p -Force
}
if ($targets.Count -gt 0) { Start-Sleep -Seconds 2 }  # 讓作業系統真正釋放埠，避免新舊搶埠

$exe = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\pythonw.exe"
if (-not (Test-Path $exe)) { $exe = "pythonw.exe" }
Start-Process -FilePath $exe -ArgumentList ('"' + $appPath + '"') -WorkingDirectory $dir -WindowStyle Hidden
Log "launched $exe app.py"

# 驗證三個埠最後確實由同一個行程持有，方便下次除錯時快速定位殭屍行程
Start-Sleep -Seconds 2
$final = Get-NetTCPConnection -State Listen -LocalPort 8850, 8851, 80 -ErrorAction SilentlyContinue |
    Select-Object LocalPort, OwningProcess -Unique
$pids = $final | Select-Object -ExpandProperty OwningProcess -Unique
if ($pids.Count -gt 1) {
    $detail = ($final | ForEach-Object { "$($_.LocalPort)=$($_.OwningProcess)" }) -join ", "
    Log "WARNING: 8850/8851/80 owned by different pids after restart: $detail"
} else {
    Log "OK: all three ports owned by single pid $pids"
}
