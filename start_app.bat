@echo off
echo Iniciando API...
start /min cmd /c "python api.py"
timeout /t 3 >nul
echo Abrindo interface web...
start index.html
echo Pronto! A API esta rodando em background.
pause
