@echo off
REM Arranca la demo sin activar conda: usa el Python del entorno floricola-rag (o seidor-b2b, que tiene las mismas librerias).
cd /d "%~dp0"
set PY=%USERPROFILE%\miniconda3\envs\floricola-rag\python.exe
if not exist "%PY%" set PY=%USERPROFILE%\miniconda3\envs\seidor-b2b\python.exe
if not exist "%PY%" (
  echo No encuentro el entorno floricola-rag en %USERPROFILE%\miniconda3\envs
  echo Crealo con: conda create -n floricola-rag python=3.11 -y  y luego  pip install -r requirements.txt
  pause
  exit /b 1
)
"%PY%" seed.py
"%PY%" -m streamlit run app.py --browser.gatherUsageStats false
