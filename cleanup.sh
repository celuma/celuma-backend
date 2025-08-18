#!/bin/bash

echo "🧹 Cleaning up Celuma Backend completely..."

# Stop and remove containers
echo "🛑 Stopping and removing containers..."
docker-compose down

# Remove volumes
echo "🗑️ Removing volumes..."
docker volume rm celuma-backend_pgdata 2>/dev/null || echo "Volume already removed"

# Remove any dangling images
echo "🧹 Cleaning up dangling images..."
docker image prune -f

echo "✅ Cleanup completed!"
echo ""
echo "🚀 To start fresh, run: docker-compose up --build"
echo "📝 This will:"
echo "   1. Create a new PostgreSQL database"
echo "   2. Run all Alembic migrations automatically"
echo "   3. Start the API with a fresh database"
