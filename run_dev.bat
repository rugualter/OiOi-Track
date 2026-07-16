@echo off
setlocal

REM Start Tailwind
start "Tailwind" cmd /k "npm run tailwind"

REM Start Django development server
start "Django" cmd /k "cd src && python manage.py runserver"

REM Start Celery worker
start "Celery Worker" cmd /k "cd src && celery -A config worker --loglevel DEBUG --pool=solo"

REM Start Celery Beat
start "Celery Beat" cmd /k "cd src && celery -A config beat --scheduler django --loglevel DEBUG"

echo All services started ready for tests at http://127.0.0.1:8000/.