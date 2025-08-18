#!/bin/bash

set -e  # Exit on any error

echo "🚀 Deploying Celuma Backend with Remote Database..."

# Check if required environment variables are set
if [ -z "$DATABASE_URL" ]; then
    echo "❌ ERROR: DATABASE_URL environment variable is required"
    echo "Example: export DATABASE_URL=postgresql://user:pass@host:5432/dbname"
    exit 1
fi

if [ -z "$JWT_SECRET" ]; then
    echo "❌ ERROR: JWT_SECRET environment variable is required"
    echo "Example: export JWT_SECRET=your-super-secret-key"
    exit 1
fi

echo "✅ Environment variables configured:"
echo "   Database: ${DATABASE_URL}"
echo "   JWT Secret: ${JWT_SECRET:0:10}..."
echo ""

# Pull the latest image
echo "📥 Pulling latest Celuma Backend image..."
docker pull ghcr.io/celuma/celuma-backend:latest

# Create a temporary container for database initialization
echo "🔧 Initializing database..."
docker run --rm \
    -e DATABASE_URL="$DATABASE_URL" \
    -e JWT_SECRET="$JWT_SECRET" \
    -e JWT_EXPIRES_MIN="${JWT_EXPIRES_MIN:-480}" \
    -e APP_NAME="${APP_NAME:-celuma}" \
    -e ENV="${ENV:-production}" \
    ghcr.io/celuma/celuma-backend:latest \
    bash init_db.sh

if [ $? -eq 0 ]; then
    echo "✅ Database initialization completed successfully!"
else
    echo "❌ Database initialization failed!"
    exit 1
fi

# Start the main API container
echo "🚀 Starting Celuma Backend API..."
docker run -d \
    --name celuma-backend \
    -e DATABASE_URL="$DATABASE_URL" \
    -e JWT_SECRET="$JWT_SECRET" \
    -e JWT_EXPIRES_MIN="${JWT_EXPIRES_MIN:-480}" \
    -e APP_NAME="${APP_NAME:-celuma}" \
    -e ENV="${ENV:-production}" \
    -p 8000:8000 \
    --restart unless-stopped \
    ghcr.io/celuma/celuma-backend:latest

echo "✅ Celuma Backend deployed successfully!"
echo "🌐 API available at: http://localhost:8000"
echo "📚 Documentation at: http://localhost:8000/docs"
echo ""
echo "📋 Container info:"
echo "   Name: celuma-backend"
echo "   Status: $(docker ps --filter name=celuma-backend --format '{{.Status}}')"
echo ""
echo "🔧 Useful commands:"
echo "   View logs: docker logs celuma-backend"
echo "   Stop: docker stop celuma-backend"
echo "   Remove: docker rm celuma-backend"
