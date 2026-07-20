@echo off
rem Digital Detox restart. This bat only triggers ONE UAC prompt; all
rem real work (kill old instance on 8850, relaunch app.py) happens in
rem a hidden elevated PowerShell running restart.ps1, so no extra
rem console windows appear and nothing can loop. See restart.log.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File','%~dp0restart.ps1'"
