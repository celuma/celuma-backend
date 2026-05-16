#!/bin/bash

set -e  # Exit on any error

echo "Starting Celuma Backend..."

# If DATABASE_URL is not provided (e.g. on ECS, where the cluster credentials
# arrive split across DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME), build it
# from those parts. Local docker-compose still works because it sets
# DATABASE_URL directly.
if [ -z "${DATABASE_URL}" ]; then
    if [ -n "${DB_HOST}" ] && [ -n "${DB_USER}" ] && [ -n "${DB_PASSWORD}" ]; then
        DB_PORT="${DB_PORT:-5432}"
        DB_NAME="${DB_NAME:-celumadb}"
        export DATABASE_URL="postgresql+psycopg2://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
        echo "DATABASE_URL composed from DB_* environment variables"
    else
        echo "ERROR: DATABASE_URL is not set and DB_* variables are incomplete"
        exit 1
    fi
fi

# Function to check database connection
check_db() {
    echo "Checking database connection..."
    python -c "
import os
import sys
from sqlalchemy import create_engine, text

try:
    database_url = os.environ['DATABASE_URL']
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    engine.dispose()
    print('Database connection successful!')
except Exception as e:
    print(f'Database connection failed: {e}')
    sys.exit(1)
"
}

# Wait for database to be ready
echo "Waiting for database to be ready..."
max_attempts=30
attempt=1

while [ $attempt -le $max_attempts ]; do
    if check_db; then
        echo "Database is ready!"
        break
    else
        echo "Attempt $attempt/$max_attempts: Database not ready, waiting..."
        sleep 2
        attempt=$((attempt + 1))
    fi
done

if [ $attempt -gt $max_attempts ]; then
    echo "ERROR: Database connection failed after $max_attempts attempts"
    exit 1
fi

# Run migrations (only if not already at head)
echo "Checking migration status..."
if alembic current 2>/dev/null | grep -q "(head)"; then
    echo "Database is already up to date, skipping migrations."
else
    echo "Running database migrations..."
    if alembic upgrade head; then
        echo "Migrations completed successfully!"
    else
        echo "ERROR: Migrations failed!"
        echo "You can run migrations manually with: alembic upgrade head"
        exit 1
    fi
fi

# Start the application
echo "Starting application..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
