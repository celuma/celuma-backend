#!/bin/bash

set -e  # Exit on any error

echo "🚀 Initializing Celuma Database..."

# Function to check database connection
check_db() {
    echo "🔍 Checking database connection..."
    python -c "
import psycopg2
import os
import sys

try:
    # Extract connection parameters from DATABASE_URL
    db_url = os.environ['DATABASE_URL']
    if db_url.startswith('postgresql+psycopg2://'):
        db_url = db_url.replace('postgresql+psycopg2://', '')
    
    # Parse the URL manually
    if '@' in db_url:
        auth_part, rest = db_url.split('@')
        if ':' in auth_part:
            user, password = auth_part.split(':')
        else:
            user, password = auth_part, ''
        
        if '/' in rest:
            host_port, database = rest.split('/')
            if ':' in host_port:
                host, port = host_port.split(':')
            else:
                host, port = host_port, '5432'
        else:
            host, port, database = rest, '5432', ''
        
        conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password
        )
        conn.close()
        print('✅ Database connection successful!')
    else:
        print('❌ Invalid DATABASE_URL format')
        sys.exit(1)
except Exception as e:
    print(f'❌ Database connection failed: {e}')
    sys.exit(1)
"
}

# Wait for database to be ready
echo "⏳ Waiting for database to be ready..."
max_attempts=60  # Increased to 60 attempts (2 minutes)
attempt=1

while [ $attempt -le $max_attempts ]; do
    if check_db; then
        echo "✅ Database is ready!"
        break
    else
        echo "⏳ Attempt $attempt/$max_attempts: Database not ready, waiting..."
        sleep 2
        attempt=$((attempt + 1))
    fi
done

if [ $attempt -gt $max_attempts ]; then
    echo "❌ ERROR: Database connection failed after $max_attempts attempts"
    exit 1
fi

# Check if migrations are needed
echo "🔍 Checking if migrations are needed..."
if alembic current | grep -q "head"; then
    echo "✅ Database is up to date, no migrations needed"
else
    echo "🔄 Running database migrations..."
    if alembic upgrade head; then
        echo "✅ Migrations completed successfully!"
    else
        echo "❌ ERROR: Migrations failed!"
        echo "You can run migrations manually with: alembic upgrade head"
        exit 1
    fi
fi

echo "🎉 Database initialization completed successfully!"
