@echo off
echo Iniciando API de Produtos (Modo Local)...
uvicorn api:app --reload
pause
