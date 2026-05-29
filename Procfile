web: alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
worker: celery -A app.celery_app worker --loglevel=info -Q events,freshdesk --concurrency=4 --max-tasks-per-child=200
beat: celery -A app.celery_app beat --loglevel=info
