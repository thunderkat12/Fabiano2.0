@echo off
echo Iniciando API...
start /min cmd /c "python api.py"
timeout /t 3 >nul
echo Abrindo interface web...
start http://127.0.0.1:8000
echo Pronto! A API esta rodando em background.
pause
