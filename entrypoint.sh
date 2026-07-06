#!/bin/bash
set -e

echo "[goberna-dashboard] Waiting for database..."
until python -c "
import pymysql, os, time
for _ in range(30):
    try:
        pymysql.connect(
            host=os.environ.get('DB_HOST','localhost'),
            port=int(os.environ.get('DB_PORT','3306')),
            user=os.environ.get('DB_USER','root'),
            password=os.environ.get('DB_PASSWORD',''),
            database=os.environ.get('DB_NAME','goberna_db'),
        )
        break
    except Exception as e:
        time.sleep(2)
else:
    raise RuntimeError('DB not ready')
"; do sleep 2; done

echo "[goberna-dashboard] Running migrations..."
python manage.py migrate --noinput

echo "[goberna-dashboard] Collecting static files..."
python manage.py collectstatic --noinput

echo "[goberna-dashboard] Starting gunicorn..."
exec gunicorn dashboard_project.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 3 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
