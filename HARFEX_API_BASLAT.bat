@echo off
title Harfex - Kapatmayın!
cd /d "%~dp0"

echo Eski sunucular kapatiliyor...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8080 " ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":3000 " ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo.
echo  HARFEX - Sunucular baslatiliyor...
echo  BU PENCEREYI KAPATMAYIN!
echo  -----------------------------------------------

start "Harfex API" cmd /k "cd /d %~dp0 && python -m uvicorn api.main:app --host 127.0.0.1 --port 8080"
start "Harfex Web" cmd /k "cd /d %~dp0\web_extracted && python -m http.server 3000"

timeout /t 3 /nobreak >nul
start http://localhost:3000/tool.html

echo.
echo  Tarayici acildi: http://localhost:3000/tool.html
echo  API: http://127.0.0.1:8080
echo.
echo  Kapat: iki pencereyi de kapat
pause
