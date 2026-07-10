@echo off
rem Digital Detox — 重啟（更新程式碼後套用）
rem 自我提權 → 精準關掉佔用 8850 的舊實例（不影響其他程式）→ 用新碼啟動
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8850"') do taskkill /f /pid %%p >nul 2>&1
timeout /t 1 >nul
start "" pyw "%~dp0app.py"
