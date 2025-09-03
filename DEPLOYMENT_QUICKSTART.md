# 🚀 Celuma Backend - Quick Deployment Reference

Quick reference for common deployment scenarios.

## 🧪 Development (Local Database)

```bash
# Start everything with automatic migrations
docker-compose up --build

# API: http://localhost:8000
# Database: localhost:5432
```

## 🏭 GHCR (Local Database)

```bash
# GHCR with local database
docker-compose -f docker-compose.ghcr.yml up --build -d
```

## 🌐 Remote Database

### Option 1: Docker Compose
```bash
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export JWT_SECRET="your-secret-key"
docker-compose -f docker-compose.remote-db.yml up --build -d
```

### Option 2: Single Container
```bash
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export JWT_SECRET="your-secret-key"
./deploy_remote.sh
```

## 🗄️ Database Management

```bash
# Check migration status
alembic current

# Create new migration
alembic revision --autogenerate -m "Description"

# Apply migrations
alembic upgrade head

# Complete cleanup
./cleanup.sh
```

## 🔧 Environment Variables

```bash
# Required
DATABASE_URL=postgresql://user:pass@host:5432/dbname
JWT_SECRET=your-secret-key

# Optional
JWT_EXPIRES_MIN=480
APP_NAME=celuma
ENV=production

# AWS / Media (for image uploads)
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret
AWS_REGION=us-east-1
S3_BUCKET_NAME=your-bucket
MEDIA_PUBLIC_BASE_URL=https://dxxxxxxxxxxxx.cloudfront.net
# MEDIA_PRESIGNED_EXPIRE_SECONDS=3600
# S3_ENDPOINT_URL=http://localhost:4566
```

## 📚 Full Documentation

- **[Complete Deployment Guide](DEPLOYMENT_README.md)** - Detailed deployment documentation
- **[Main README](README.md)** - Project overview and setup
- **[API Documentation](API_ENDPOINTS.md)** - API reference
- **[Testing Guide](tests/TESTING_README.md)** - Testing documentation

---

**For detailed information, see [DEPLOYMENT_README.md](DEPLOYMENT_README.md)**
