@echo off
rem Digital Detox — 以系統管理員身分在背景啟動（寫入 hosts 檔需要提權）
rem 用 pyw（pythonw）執行：無主控台視窗，輸出寫到 detox.log
rem 若已在執行，會直接開啟控制台頁面，不會開第二個實例
powershell -NoProfile -Command "Start-Process pyw -ArgumentList '\"%~dp0app.py\"' -Verb RunAs"
