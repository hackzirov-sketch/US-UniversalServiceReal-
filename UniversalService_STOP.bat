@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if /I "%~1"=="--syntax-check" exit /b 0

set "COMPOSE_FILE="
if exist "compose.yaml" set "COMPOSE_FILE=compose.yaml"
if not defined COMPOSE_FILE if exist "compose.yml" set "COMPOSE_FILE=compose.yml"
if not defined COMPOSE_FILE if exist "docker-compose.yml" set "COMPOSE_FILE=docker-compose.yml"
if not defined COMPOSE_FILE if exist "docker-compose.yaml" set "COMPOSE_FILE=docker-compose.yaml"

if not defined COMPOSE_FILE goto compose_missing
docker --version >nul 2>&1
if errorlevel 1 goto docker_missing
docker compose version >nul 2>&1
if errorlevel 1 goto docker_missing

echo UniversalService servislarini o'chirish...
docker compose -f "%COMPOSE_FILE%" down
if errorlevel 1 goto stop_failed
echo UniversalService o'chirildi. Database volumelari saqlandi.
endlocal
exit /b 0

:compose_missing
echo XATO: Docker Compose konfiguratsiya fayli topilmadi.
endlocal
exit /b 1

:docker_missing
echo XATO: Docker yoki Docker Compose topilmadi.
endlocal
exit /b 1

:stop_failed
echo XATO: Servislarni o'chirish muvaffaqiyatsiz tugadi.
echo Docker Desktop ishlayotganini tekshiring.
endlocal
exit /b 1
