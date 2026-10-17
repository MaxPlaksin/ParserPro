@echo off
REM Запуск Pull-API для основной платформы (слушает 0.0.0.0:8765)
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Create venv first: python -m venv .venv
  exit /b 1
)
if not exist .env (
  echo Copy .env.example to .env and set PARSER_API_KEY
  exit /b 1
)
echo Starting counterparty API on 0.0.0.0:8765 ...
.\.venv\Scripts\python.exe -m counterparty.api --host 0.0.0.0 --port 8765
