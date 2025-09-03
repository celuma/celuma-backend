# 🚀 Celuma Backend Deployment Guide

This guide covers all deployment scenarios for the Celuma Backend API, from development to production with both local and remote databases.

## 📋 Table of Contents

- [Quick Start](#-quick-start)
- [Development Environment](#-development-environment)
- [Production Deployment](#-production-deployment)
- [Remote Database Deployment](#-remote-database-deployment)
- [Single Container Deployment](#-single-container-deployment)
- [Database Management](#-database-management)
- [Environment Variables](#-environment-variables)
- [Troubleshooting](#-troubleshooting)

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose installed
- Access to PostgreSQL database (local or remote)
- Git repository cloned

### 1. Development (Local Database)
```bash
# Start everything with automatic migrations
docker-compose up --build

# API will be available at: http://localhost:8000
# Database will be available at: localhost:5432
```

### 2. GHCR (Local Database)
```bash
# Use production configuration
docker-compose -f docker-compose.ghcr.yml up --build
```

### 3. Remote Database
```bash
# Set environment variables
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export JWT_SECRET="your-secret-key"

# Deploy with remote database
docker-compose -f docker-compose.remote-db.yml up --build
```

## 🧪 Development Environment

### Local Development Setup
```bash
# Clone repository
git clone <repository-url>
cd celuma-backend

# Start development environment
docker-compose up --build

# The system automatically:
# 1. Creates PostgreSQL database container
# 2. Waits for database to be ready
# 3. Runs all Alembic migrations
# 4. Starts the FastAPI server
```

### Development Features
- **Hot Reload**: Code changes automatically restart the server
- **Local Database**: PostgreSQL runs in Docker container
- **Automatic Migrations**: Database schema is always up to date
- **Volume Mounting**: Code changes are reflected immediately

### Stopping Development Environment
```bash
# Stop all services
docker-compose down

# Stop and remove volumes (clean slate)
docker-compose down -v
```

## 🏭 Production Deployment

### GHCR with Local Database
```bash
# Use production compose file
docker-compose -f docker-compose.ghcr.yml up --build -d

# This includes:
# - Database initialization service
# - Automatic migration execution
# - Production-ready configuration
# - Restart policies
```

### Production Features
- **Automatic Restarts**: `restart: unless-stopped`
- **Database Initialization**: Ensures migrations are applied
- **Production Environment**: Optimized for production use
- **Health Checks**: Built-in health monitoring

## 🌐 Remote Database Deployment

### Option 1: Docker Compose with Remote Database
```bash
# 1. Set environment variables
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export JWT_SECRET="your-super-secret-key"
export ENV="production"

# 2. Deploy using remote compose file
docker-compose -f docker-compose.remote-db.yml up --build -d

# 3. Verify deployment
docker ps
curl http://localhost:8000/
```

### Option 2: Single Container Deployment
```bash
# 1. Set environment variables
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export JWT_SECRET="your-super-secret-key"

# 2. Run deployment script
./deploy_remote.sh

# This script:
# - Pulls latest image
# - Initializes database (runs migrations)
# - Starts API container
# - Provides deployment status
```

### Remote Database Requirements
- **Network Access**: Container must be able to reach the database
- **Credentials**: Valid database user with necessary permissions
- **Database**: Database must exist (migrations will create tables)
- **SSL**: Configure SSL if required by your database provider

## 🗄️ Database Management

### Automatic Migration System
The Celuma Backend includes an intelligent migration system:

1. **Database Initialization Service** (`db-init`):
   - Waits for database to be ready
   - Checks if migrations are needed
   - Runs `alembic upgrade head` automatically
   - Exits after successful completion

2. **API Service**:
   - Depends on `db-init` service
   - Only starts after migrations are complete
   - Connects to fully configured database

### Manual Migration Management
```bash
# Check current migration status
alembic current

# View migration history
alembic history

# Create new migration (after model changes)
alembic revision --autogenerate -m "Add new field to user table"

# Apply specific migration
alembic upgrade <revision_id>

# Rollback to previous migration
alembic downgrade -1

# Rollback to specific migration
alembic downgrade <revision_id>
```

### Migration Best Practices
- **Always backup** before running migrations in production
- **Test migrations** in development environment first
- **Use descriptive names** for migration files
- **Review generated migrations** before applying
- **Run migrations during maintenance windows**

## 🔧 Environment Variables

### Required Variables
```bash
# Database connection string
DATABASE_URL=postgresql://username:password@host:port/database

# JWT secret for authentication
JWT_SECRET=your-super-secret-jwt-key-change-in-production
```

### Optional Variables
```bash
# JWT token expiration (default: 480 minutes)
JWT_EXPIRES_MIN=480

# Application name (default: celuma)
APP_NAME=celuma

# Environment (default: production)
ENV=production

# AWS / Media (image uploads)
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret
AWS_REGION=us-east-1
S3_BUCKET_NAME=your-bucket

# Public media base URL (CDN/CloudFront)
MEDIA_PUBLIC_BASE_URL=https://dxxxxxxxxxxxx.cloudfront.net

# Presigned expiry (if using presigned links)
MEDIA_PRESIGNED_EXPIRE_SECONDS=3600

# Custom S3 endpoint (LocalStack/MinIO)
# S3_ENDPOINT_URL=http://localhost:4566
```

### Environment File Example
```env
# .env file
DATABASE_URL=postgresql://postgres:postgres@db:5432/celumadb
JWT_SECRET=your-super-secret-jwt-key-change-in-production
JWT_EXPIRES_MIN=480
APP_NAME=celuma
ENV=production
```

## 🧹 System Cleanup

### Complete Cleanup
```bash
# Remove everything and start fresh
./cleanup.sh

# This script:
# 1. Stops all containers
# 2. Removes database volumes
# 3. Cleans up dangling images
# 4. Provides restart instructions
```

### Selective Cleanup
```bash
# Stop containers only
docker-compose down

# Stop and remove volumes
docker-compose down -v

# Remove specific containers
docker rm celuma-backend

# Remove specific volumes
docker volume rm celuma-backend_pgdata
```

## 🔍 Troubleshooting

### Common Issues

#### 1. Database Connection Failed
```bash
# Check if database is running
docker ps | grep postgres

# Check database logs
docker logs celuma-backend-db-1

# Verify connection string
echo $DATABASE_URL
```

#### 2. Migrations Not Running
```bash
# Check initialization service logs
docker logs celuma-backend-db-init-1

# Check if service completed
docker ps | grep db-init

# Manual migration check
docker exec celuma-backend-api-1 alembic current
```

#### 3. API Not Starting
```bash
# Check API logs
docker logs celuma-backend-api-1

# Check service dependencies
docker-compose ps

# Verify environment variables
docker exec celuma-backend-api-1 env | grep DATABASE
```

#### 4. Port Already in Use
```bash
# Check what's using port 8000
lsof -i :8000

# Stop conflicting service
sudo systemctl stop conflicting-service

# Or use different port
docker-compose up -p 8001:8000
```

### Debug Commands
```bash
# View all container logs
docker-compose logs

# View specific service logs
docker-compose logs api
docker-compose logs db-init

# Check container status
docker-compose ps

# Execute commands in running container
docker exec -it celuma-backend-api-1 bash
docker exec -it celuma-backend-api-1 alembic current
```

### Performance Monitoring
```bash
# Check container resource usage
docker stats

# Monitor API performance
curl -w "@curl-format.txt" -o /dev/null -s "http://localhost:8000/"

# Check database performance
docker exec celuma-backend-db-1 psql -U postgres -d celumadb -c "SELECT * FROM pg_stat_activity;"
```

## 📚 Additional Resources

- [API Documentation](API_ENDPOINTS.md) - Complete API reference
- [Testing Guide](tests/TESTING_README.md) - Comprehensive testing documentation
- [Database Schema](DATABASE_README.md) - Database design and structure
- [API Examples](API_EXAMPLES.md) - Usage examples and patterns

## 🆘 Support

### Getting Help
1. **Check logs**: Most issues can be diagnosed from container logs
2. **Verify configuration**: Ensure environment variables are set correctly
3. **Test connectivity**: Verify database and network connectivity
4. **Review documentation**: Check this guide and other documentation files
5. **Run tests**: Use the testing suite to verify system functionality

### Useful Commands Reference
```bash
# System status
docker-compose ps
docker stats

# View logs
docker-compose logs -f
docker logs celuma-backend-api-1

# Database operations
docker exec celuma-backend-db-1 psql -U postgres -d celumadb -c "\dt"
docker exec celuma-backend-api-1 alembic current

# If migrations don't run automatically
docker-compose exec api alembic upgrade head

# Container management
docker-compose up --build
docker-compose down
docker-compose restart api
```

---

**Happy Deploying! 🚀**

For more information, check the main [README.md](README.md) and other documentation files.
