@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if /I "%~1"=="--syntax-check" exit /b 0

rem Service names and API port verified from this repository's compose.yaml.
set "COMPOSE_FILE="
set "DB_SERVICE=db"
set "CACHE_SERVICE=redis"
set "MIGRATION_SERVICE=api"
set "API_SERVICE=api"
set "WORKER_SERVICE=worker"
set "BOT_SERVICE=bot"
set "API_URL=http://localhost:8000/health"
set "WAIT_LIMIT=90"

if /I "%~1"=="--preflight-check" goto command_preflight
if /I "%~1"=="--start" goto command_start
goto menu

:command_preflight
call :safe_preflight
exit /b !errorlevel!

:command_start
call :start_services
exit /b !errorlevel!

:menu
cls
echo ========================================
echo UniversalService xavfsiz boshqaruv menyusi
echo ========================================
echo [1] Servislarni ishga tushirish
echo [2] Servislarni qayta ishga tushirish
echo [3] Servislar holatini ko'rish
echo [4] Oxirgi loglarni ko'rish
echo [5] Migration bajarish
echo [6] Servislarni to'xtatish
echo [7] To'liq o'chirish va volumelarni saqlash
echo [0] Chiqish
echo.
choice /C 12345670 /N /M "Tanlang: "
if errorlevel 8 goto end
if errorlevel 7 goto menu_down
if errorlevel 6 goto menu_stop
if errorlevel 5 goto menu_migrate
if errorlevel 4 goto menu_logs
if errorlevel 3 goto menu_status
if errorlevel 2 goto menu_restart
if errorlevel 1 goto menu_start
goto menu

:menu_start
call :start_services
pause
goto menu

:menu_restart
call :restart_services
pause
goto menu

:menu_status
call :show_status
pause
goto menu

:menu_logs
call :show_logs
pause
goto menu

:menu_migrate
call :migration_action
pause
goto menu

:menu_stop
call :stop_services
pause
goto menu

:menu_down
call :down_services
pause
goto menu

:start_services
echo.
echo [1/8] Xavfsizlik va konfiguratsiya tekshirilmoqda...
call :safe_preflight
if errorlevel 1 exit /b 1

echo [2/8] PostgreSQL va Redis ishga tushirilmoqda...
call :start_dependencies
if errorlevel 1 exit /b 1

echo [3/8] API image migration uchun build qilinmoqda...
docker compose -f "%COMPOSE_FILE%" build "%MIGRATION_SERVICE%"
if errorlevel 1 goto start_build_failed

echo [4/8] Database migration bajarilmoqda...
call :run_migration
if errorlevel 1 exit /b 1

echo [5/8] Barcha servislar build qilinib ishga tushirilmoqda...
docker compose -f "%COMPOSE_FILE%" up -d --build
if errorlevel 1 goto start_build_failed

echo [6/8] API health endpoint kutilmoqda...
call :wait_api
if errorlevel 1 exit /b 1

echo [7/8] Database, Redis, worker va bot tekshirilmoqda...
call :wait_database
if errorlevel 1 exit /b 1
call :wait_redis
if errorlevel 1 exit /b 1
call :require_running "%WORKER_SERVICE%"
if errorlevel 1 exit /b 1
call :require_running "%BOT_SERVICE%"
if errorlevel 1 exit /b 1

echo [8/8] Yakuniy container holati:
docker compose -f "%COMPOSE_FILE%" ps
if errorlevel 1 exit /b 1
echo.
echo ========================================
echo UniversalService muvaffaqiyatli ishga tushdi
echo Myxvest read-only: yoqilgan
echo Myxvest purchase: o'chirilgan
echo ========================================
echo API URL: %API_URL%
echo Containerlar holati: docker compose -f "%COMPOSE_FILE%" ps
echo Loglarni ko'rish: docker compose -f "%COMPOSE_FILE%" logs -f --tail 150
echo Servislarni o'chirish: docker compose -f "%COMPOSE_FILE%" down
exit /b 0

:start_build_failed
echo.
echo XATO: Build yoki servislarni ishga tushirish muvaffaqiyatsiz tugadi.
call :show_all_logs
exit /b 1

:restart_services
echo Xavfsiz qayta ishga tushirish tayyorlanmoqda...
call :safe_preflight
if errorlevel 1 exit /b 1
docker compose -f "%COMPOSE_FILE%" down
if errorlevel 1 goto restart_failed
call :start_services
exit /b !errorlevel!

:restart_failed
echo XATO: Mavjud servislarni to'xtatib bo'lmadi. Qayta ishga tushirish bekor qilindi.
call :show_all_logs
exit /b 1

:show_status
call :basic_preflight
if errorlevel 1 exit /b 1
docker compose -f "%COMPOSE_FILE%" ps
if errorlevel 1 exit /b 1
exit /b 0

:show_logs
call :basic_preflight
if errorlevel 1 exit /b 1
docker compose -f "%COMPOSE_FILE%" logs --tail 150
if errorlevel 1 exit /b 1
exit /b 0

:migration_action
echo Xavfsiz migration tayyorlanmoqda...
call :safe_preflight
if errorlevel 1 exit /b 1
call :start_dependencies
if errorlevel 1 exit /b 1
docker compose -f "%COMPOSE_FILE%" build "%MIGRATION_SERVICE%"
if errorlevel 1 goto migration_build_failed
call :run_migration
exit /b !errorlevel!

:migration_build_failed
echo XATO: Migration service image build qilinmadi.
call :show_all_logs
exit /b 1

:stop_services
call :basic_preflight
if errorlevel 1 exit /b 1
docker compose -f "%COMPOSE_FILE%" stop
if errorlevel 1 exit /b 1
echo Servislar to'xtatildi. Volumelar saqlandi.
exit /b 0

:down_services
call :basic_preflight
if errorlevel 1 exit /b 1
echo Containerlar va network o'chiriladi. Database volumelari saqlanadi.
docker compose -f "%COMPOSE_FILE%" down --remove-orphans
if errorlevel 1 exit /b 1
echo To'liq o'chirildi. Volumelar o'chirilmadi.
exit /b 0

:safe_preflight
call :basic_preflight
if errorlevel 1 exit /b 1
call :check_purchase_flag
if errorlevel 1 exit /b 1
exit /b 0

:basic_preflight
call :check_required_files
if errorlevel 1 exit /b 1
call :check_docker
if errorlevel 1 exit /b 1
echo Docker Compose konfiguratsiyasi tekshirilmoqda...
docker compose -f "%COMPOSE_FILE%" config -q
if errorlevel 1 goto compose_invalid
call :verify_compose_services
if errorlevel 1 exit /b 1
exit /b 0

:compose_invalid
echo XATO: Docker Compose konfiguratsiyasi yaroqsiz. Ish davom ettirilmadi.
exit /b 1

:check_required_files
if not exist ".env" goto env_missing
if not exist "alembic.ini" goto alembic_missing
set "COMPOSE_FILE="
if exist "compose.yaml" set "COMPOSE_FILE=compose.yaml"
if not defined COMPOSE_FILE if exist "compose.yml" set "COMPOSE_FILE=compose.yml"
if not defined COMPOSE_FILE if exist "docker-compose.yml" set "COMPOSE_FILE=docker-compose.yml"
if not defined COMPOSE_FILE if exist "docker-compose.yaml" set "COMPOSE_FILE=docker-compose.yaml"
if not defined COMPOSE_FILE goto compose_missing
exit /b 0

:env_missing
echo.
echo XATO: .env fayli topilmadi.
echo .env.example faylidan .env nusxa oling va secret qiymatlarni xavfsiz kiriting.
echo Ishga tushirish to'xtatildi.
exit /b 1

:alembic_missing
echo XATO: alembic.ini topilmadi. Migration xavfsiz bajarilmaydi.
exit /b 1

:compose_missing
echo XATO: compose.yaml, compose.yml, docker-compose.yml yoki docker-compose.yaml topilmadi.
exit /b 1

:check_purchase_flag
powershell -NoProfile -ExecutionPolicy Bypass -Command "$line = Get-Content -LiteralPath '.env' ^| Where-Object { $_ -match '^\s*MYXVEST_PURCHASE_ENABLED\s*=' } ^| Select-Object -Last 1; if ($line) { $raw = ($line -split '=', 2)[1]; $raw = ($raw -split '#', 2)[0].Trim().Trim([char]34).Trim([char]39); if ($raw -match '^(?i:true^|1^|yes^|on)$') { exit 42 } }; exit 0" >nul 2>&1
set "FLAG_CHECK_EXIT=!errorlevel!"
if "!FLAG_CHECK_EXIT!"=="42" goto purchase_enabled
if not "!FLAG_CHECK_EXIT!"=="0" goto purchase_check_failed
exit /b 0

:purchase_enabled
echo.
echo XATO: Real Myxvest xaridlari yoqilgan. Hozir xavfsiz rejimda
echo MYXVEST_PURCHASE_ENABLED=false bo'lishi kerak.
echo Ishga tushirish bloklandi.
exit /b 1

:purchase_check_failed
echo XATO: .env xavfsizlik holatini tekshirib bo'lmadi. Ishga tushirish bloklandi.
exit /b 1

:check_docker
docker --version >nul 2>&1
if errorlevel 1 goto docker_missing
docker compose version >nul 2>&1
if errorlevel 1 goto compose_command_missing
docker info >nul 2>&1
if errorlevel 1 goto docker_daemon_missing
exit /b 0

:docker_missing
echo XATO: Docker topilmadi. Docker Desktopni o'rnating.
exit /b 1

:compose_command_missing
echo XATO: Docker Compose v2 topilmadi. Docker Desktopni yangilang.
exit /b 1

:docker_daemon_missing
echo XATO: Docker daemon ishlamayapti. Docker Desktopni oching va to'liq ishga tushishini kuting.
exit /b 1

:verify_compose_services
set "SERVICE_LIST=%TEMP%\UniversalService_services_%RANDOM%_%RANDOM%.txt"
docker compose -f "%COMPOSE_FILE%" config --services >"!SERVICE_LIST!" 2>nul
if errorlevel 1 goto service_list_failed
call :assert_service "%DB_SERVICE%"
if errorlevel 1 goto service_verification_failed
call :assert_service "%CACHE_SERVICE%"
if errorlevel 1 goto service_verification_failed
call :assert_service "%MIGRATION_SERVICE%"
if errorlevel 1 goto service_verification_failed
call :assert_service "%WORKER_SERVICE%"
if errorlevel 1 goto service_verification_failed
call :assert_service "%BOT_SERVICE%"
if errorlevel 1 goto service_verification_failed
if exist "!SERVICE_LIST!" del /q "!SERVICE_LIST!" >nul 2>&1
exit /b 0

:assert_service
set "SERVICE_FOUND="
for /f "usebackq delims=" %%S in ("!SERVICE_LIST!") do if /I "%%S"=="%~1" set "SERVICE_FOUND=1"
if not defined SERVICE_FOUND goto service_missing
exit /b 0

:service_missing
echo XATO: Compose konfiguratsiyasida "%~1" servisi topilmadi.
exit /b 1

:service_list_failed
echo XATO: Compose service nomlarini aniqlab bo'lmadi.
if exist "!SERVICE_LIST!" del /q "!SERVICE_LIST!" >nul 2>&1
exit /b 1

:service_verification_failed
if exist "!SERVICE_LIST!" del /q "!SERVICE_LIST!" >nul 2>&1
exit /b 1

:start_dependencies
docker compose -f "%COMPOSE_FILE%" up -d "%DB_SERVICE%" "%CACHE_SERVICE%"
if errorlevel 1 goto dependencies_failed
call :wait_database
if errorlevel 1 exit /b 1
call :wait_redis
if errorlevel 1 exit /b 1
exit /b 0

:dependencies_failed
echo XATO: PostgreSQL yoki Redis ishga tushmadi.
call :show_service_logs "%DB_SERVICE%"
call :show_service_logs "%CACHE_SERVICE%"
exit /b 1

:wait_database
set "WAITED=0"
:wait_database_loop
set "TARGET_CONTAINER="
for /f "delims=" %%I in ('docker compose -f "%COMPOSE_FILE%" ps --all -q "%DB_SERVICE%" 2^>nul') do set "TARGET_CONTAINER=%%I"
if not defined TARGET_CONTAINER goto wait_database_again
set "TARGET_STATE="
for /f "delims=" %%I in ('docker inspect --format "{{.State.Status}}" "!TARGET_CONTAINER!" 2^>nul') do set "TARGET_STATE=%%I"
if /I "!TARGET_STATE!"=="exited" goto database_failed
if /I "!TARGET_STATE!"=="dead" goto database_failed
set "TARGET_HEALTH="
for /f "delims=" %%I in ('docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" "!TARGET_CONTAINER!" 2^>nul') do set "TARGET_HEALTH=%%I"
if /I "!TARGET_HEALTH!"=="healthy" exit /b 0
if /I "!TARGET_HEALTH!"=="none" docker compose -f "%COMPOSE_FILE%" exec -T "%DB_SERVICE%" pg_isready -U postgres -d universal_service >nul 2>&1
if /I "!TARGET_HEALTH!"=="none" if not errorlevel 1 exit /b 0
:wait_database_again
if !WAITED! GEQ %WAIT_LIMIT% goto database_timeout
powershell -NoProfile -Command "Start-Sleep -Seconds 3" >nul 2>&1
set /a WAITED+=3
goto wait_database_loop

:database_failed
echo XATO: PostgreSQL containeri !TARGET_STATE! holatiga o'tdi.
call :show_service_logs "%DB_SERVICE%"
exit /b 1

:database_timeout
echo XATO: PostgreSQL %WAIT_LIMIT% soniyada tayyor bo'lmadi.
call :show_service_logs "%DB_SERVICE%"
exit /b 1

:wait_redis
set "WAITED=0"
:wait_redis_loop
set "TARGET_CONTAINER="
for /f "delims=" %%I in ('docker compose -f "%COMPOSE_FILE%" ps --all -q "%CACHE_SERVICE%" 2^>nul') do set "TARGET_CONTAINER=%%I"
if not defined TARGET_CONTAINER goto wait_redis_again
set "TARGET_STATE="
for /f "delims=" %%I in ('docker inspect --format "{{.State.Status}}" "!TARGET_CONTAINER!" 2^>nul') do set "TARGET_STATE=%%I"
if /I "!TARGET_STATE!"=="exited" goto redis_failed
if /I "!TARGET_STATE!"=="dead" goto redis_failed
set "TARGET_HEALTH="
for /f "delims=" %%I in ('docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" "!TARGET_CONTAINER!" 2^>nul') do set "TARGET_HEALTH=%%I"
if /I "!TARGET_HEALTH!"=="healthy" exit /b 0
if /I "!TARGET_HEALTH!"=="none" docker compose -f "%COMPOSE_FILE%" exec -T "%CACHE_SERVICE%" redis-cli ping >nul 2>&1
if /I "!TARGET_HEALTH!"=="none" if not errorlevel 1 exit /b 0
:wait_redis_again
if !WAITED! GEQ %WAIT_LIMIT% goto redis_timeout
powershell -NoProfile -Command "Start-Sleep -Seconds 3" >nul 2>&1
set /a WAITED+=3
goto wait_redis_loop

:redis_failed
echo XATO: Redis containeri !TARGET_STATE! holatiga o'tdi.
call :show_service_logs "%CACHE_SERVICE%"
exit /b 1

:redis_timeout
echo XATO: Redis %WAIT_LIMIT% soniyada tayyor bo'lmadi.
call :show_service_logs "%CACHE_SERVICE%"
exit /b 1

:run_migration
set "MIGRATION_LOG=%TEMP%\UniversalService_migration_%RANDOM%_%RANDOM%.log"
docker compose -f "%COMPOSE_FILE%" run --rm --no-deps "%MIGRATION_SERVICE%" alembic upgrade head >"!MIGRATION_LOG!" 2>&1
set "MIGRATION_EXIT=!errorlevel!"
if not "!MIGRATION_EXIT!"=="0" goto migration_failed
if exist "!MIGRATION_LOG!" del /q "!MIGRATION_LOG!" >nul 2>&1
echo Migration muvaffaqiyatli bajarildi.
exit /b 0

:migration_failed
echo XATO: Migration bajarilmadi. Barcha servislar ishga tushirilmaydi.
if exist "!MIGRATION_LOG!" type "!MIGRATION_LOG!"
if exist "!MIGRATION_LOG!" del /q "!MIGRATION_LOG!" >nul 2>&1
call :show_service_logs "%DB_SERVICE%"
exit /b 1

:wait_api
set "WAITED=0"
:wait_api_loop
call :require_running_silent "%API_SERVICE%"
if errorlevel 1 goto api_not_ready
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $response = Invoke-WebRequest -UseBasicParsing -Uri '%API_URL%' -TimeoutSec 5; if ($response.StatusCode -eq 200) { exit 0 } } catch { }; exit 1" >nul 2>&1
if not errorlevel 1 exit /b 0
:api_not_ready
if !WAITED! GEQ %WAIT_LIMIT% goto api_timeout
powershell -NoProfile -Command "Start-Sleep -Seconds 3" >nul 2>&1
set /a WAITED+=3
goto wait_api_loop

:api_timeout
echo XATO: FastAPI health endpoint %WAIT_LIMIT% soniyada javob bermadi.
echo Tekshirilgan URL: %API_URL%
call :show_all_logs
echo OGOHLANTIRISH: UniversalService to'liq ishga tushmadi.
exit /b 1

:require_running
call :require_running_silent "%~1"
if not errorlevel 1 exit /b 0
echo XATO: "%~1" servisi running holatida emas.
call :show_service_logs "%~1"
echo Startup muvaffaqiyatsiz deb belgilandi.
exit /b 1

:require_running_silent
set "TARGET_CONTAINER="
for /f "delims=" %%I in ('docker compose -f "%COMPOSE_FILE%" ps --all -q "%~1" 2^>nul') do set "TARGET_CONTAINER=%%I"
if not defined TARGET_CONTAINER exit /b 1
set "TARGET_STATE="
for /f "delims=" %%I in ('docker inspect --format "{{.State.Status}}" "!TARGET_CONTAINER!" 2^>nul') do set "TARGET_STATE=%%I"
if /I not "!TARGET_STATE!"=="running" exit /b 1
exit /b 0

:show_service_logs
echo.
echo ----- %~1 service loglari, oxirgi 150 qator -----
docker compose -f "%COMPOSE_FILE%" logs --tail 150 "%~1"
exit /b !errorlevel!

:show_all_logs
echo.
echo ----- Barcha servislar loglari, oxirgi 150 qator -----
docker compose -f "%COMPOSE_FILE%" logs --tail 150
exit /b !errorlevel!

:end
endlocal
exit /b 0
