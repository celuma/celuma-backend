#!/bin/bash

set -e  # Exit on any error

echo "Starting Celuma Backend..."

# Function to check database connection
check_db() {
    echo "Checking database connection..."
    python -c "
import psycopg2
import os
import sys

try:
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    conn.close()
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

# Run migrations
echo "Running database migrations..."
if alembic upgrade head; then
    echo "Migrations completed successfully!"
else
    echo "ERROR: Migrations failed!"
    echo "You can run migrations manually with: alembic upgrade head"
    echo "Continuing anyway..."
fi

# Start the application
echo "Starting application..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
