@echo off
echo Iniciando API de Produtos...
uvicorn api:app --reload --host 0.0.0.0 --port 8000
pause
