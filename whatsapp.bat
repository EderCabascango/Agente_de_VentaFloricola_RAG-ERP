@echo off
REM Arranca el webhook de WhatsApp en el puerto 8000. En otra ventana, abre el tunel:
REM   ngrok http --url=TU-DOMINIO.ngrok-free.app 8000
cd /d "%~dp0"
set PY=%USERPROFILE%\miniconda3\envs\floricola-rag\python.exe
if not exist "%PY%" set PY=%USERPROFILE%\miniconda3\envs\seidor-b2b\python.exe
if not exist erp.db "%PY%" seed.py
"%PY%" -m uvicorn webhook:crear_app --factory --host 127.0.0.1 --port 8000
