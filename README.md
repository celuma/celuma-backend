# Celuma Backend

A FastAPI-based backend service with user authentication, PostgreSQL database, and Docker containerization. **Automatically runs database migrations on startup** to ensure the database is always properly configured.

## 🚀 Features

- **FastAPI** - Modern, fast web framework for building APIs
- **User Authentication** - JWT-based authentication system
- **PostgreSQL** - Robust relational database
- **SQLModel** - SQL databases in Python, designed for simplicity
- **Alembic** - Database migration tool with **automatic execution**
- **Docker** - Containerized development and production environments
- **Password Hashing** - Secure password storage with bcrypt
- **CI/CD Pipeline** - Automated testing and Docker image building
- **Production Ready** - Optimized Docker images for deployment
- **Auto-migrations** - Database schema automatically updated on startup

## 📋 Requirements

### System Requirements
- **Docker** 20.0+ (recommended)
- **Docker Compose** 2.0+ (for local development)
- **Python** 3.11+ (only if running without Docker)

### Python Dependencies
- FastAPI
- Uvicorn
- SQLModel
- SQLAlchemy
- Alembic
- Pydantic Settings
- psycopg2-binary
- python-jose[cryptography]
- passlib[bcrypt]
- python-multipart
- pytest

## 🛠️ Installation & Usage

### 🐳 **Option 1: Docker (Recommended)**

#### **Local Development with Docker Compose**
```bash
# Clone the repository
git clone <repository-url>
cd celuma-backend

# Start all services (API + PostgreSQL)
docker compose up --build

# The API will be available at http://localhost:8000
# Database will be available at localhost:5432
```

#### **Production with Docker Compose (Local Database)**
```bash
# Use production configuration
docker compose -f docker-compose.prod.yml up -d

# Check status
docker compose -f docker-compose.prod.yml ps

# View logs
docker compose -f docker-compose.prod.yml logs -f api
```

#### **Production with External Database (No Docker Compose)**
```bash
# Pull the latest image
docker pull ghcr.io/celuma/celuma-backend:latest

# Run with your external database
docker run -d \
  --name celuma-api \
  -p 8000:8000 \
  -e DATABASE_URL="postgresql://user:password@your-db-host:5432/your-db-name" \
  -e JWT_SECRET="your-super-secret-jwt-key-change-this-in-production" \
  -e JWT_EXPIRES_MIN=480 \
  -e APP_NAME=celuma \
  -e ENV=production \
  ghcr.io/celuma/celuma-backend:latest
```

### 🐍 **Option 2: Python Native (Development Only)**

```bash
# Clone the repository
git clone <repository-url>
cd celuma-backend

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export DATABASE_URL="postgresql://user:password@localhost:5432/celumadb"
export JWT_SECRET="your-secret-key"
export JWT_EXPIRES_MIN=480

# Run migrations manually (required for native Python)
alembic upgrade head

# Start the application
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 🔧 **Environment Variables**

### **Required Variables**
```bash
# Database connection
DATABASE_URL=postgresql://username:password@host:port/database_name

# JWT configuration
JWT_SECRET=your-super-secret-jwt-key-change-this-in-production

# Optional variables
JWT_EXPIRES_MIN=480          # JWT expiration time in minutes
APP_NAME=celuma              # Application name
ENV=production               # Environment (production/development)
```

### **Example .env file**
```bash
# .env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/celumadb
JWT_SECRET=your-super-secret-jwt-key-change-this-in-production
JWT_EXPIRES_MIN=480
APP_NAME=celuma
ENV=development
```

## 🗄️ **Database & Migrations**

### **Automatic Migrations (Docker)**
✅ **When using Docker**: Migrations run automatically on startup
✅ **No manual intervention required**: Database schema is always up-to-date
✅ **Safe execution**: Alembic handles schema changes gracefully

### **Manual Migrations (Native Python)**
```bash
# Check current migration status
alembic current

# View migration history
alembic history

# Create new migration
alembic revision -m "description of changes"

# Apply migrations
alembic upgrade head

# Rollback migrations
alembic downgrade -1
```

### **Troubleshooting Migrations**

#### **When Automatic Migrations Fail (Docker)**
```bash
# 1. Check container logs for errors
docker compose logs api

# 2. Verify database connection
docker compose exec api python -c "
import psycopg2
import os
try:
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    print('✅ Database connection successful')
    conn.close()
except Exception as e:
    print(f'❌ Database connection failed: {e}')
"

# 3. Run migrations manually
docker compose exec api alembic upgrade head

# 4. Check migration status
docker compose exec api alembic current
```

#### **Common Migration Issues**
- **Database not ready**: Wait for database to be fully initialized
- **Permission denied**: Check database user permissions
- **Connection timeout**: Verify DATABASE_URL and network connectivity
- **Migration conflicts**: Check if multiple containers are running migrations

### **Database Connection Examples**

#### **Local PostgreSQL**
```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/celumadb
```

#### **External PostgreSQL (AWS RDS, Google Cloud, etc.)**
```bash
DATABASE_URL=postgresql://username:password@your-rds-endpoint:5432/database_name
```

#### **Docker Network (when using docker-compose)**
```bash
DATABASE_URL=postgresql://postgres:postgres@db:5432/celumadb
```

## 🚀 **Deployment Scenarios**

### **1. Local Development**
```bash
# Start with hot-reload
docker compose up --build
```

### **2. Production with Local Database**
```bash
# Use production image with local PostgreSQL
docker compose -f docker-compose.prod.yml up -d
```

### **3. Production with External Database**
```bash
# Just run the API container
docker run -d \
  --name celuma-api \
  -p 8000:8000 \
  -e DATABASE_URL="postgresql://user:pass@external-db:5432/db" \
  -e JWT_SECRET="your-secret" \
  ghcr.io/celuma/celuma-backend:latest
```

### **4. Kubernetes/Cloud Deployment**
```yaml
# Example Kubernetes deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: celuma-api
spec:
  replicas: 3
  selector:
    matchLabels:
      app: celuma-api
  template:
    metadata:
      labels:
        app: celuma-api
    spec:
      containers:
      - name: api
        image: ghcr.io/celuma/celuma-backend:latest
        env:
        - name: DATABASE_URL
          value: "postgresql://user:pass@your-db:5432/db"
        - name: JWT_SECRET
          valueFrom:
            secretKeyRef:
              name: celuma-secrets
              key: jwt-secret
        ports:
        - containerPort: 8000
```

## 📡 **API Endpoints**

Once running, the API will be available at:
- **API Base URL**: http://localhost:8000
- **Interactive Documentation**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/api/v1/health

### **Authentication Endpoints**
- `POST /api/v1/auth/register` - User registration
- `POST /api/v1/auth/login` - User login
- `GET /api/v1/auth/me` - Get current user info

### **User Endpoints**
- `GET /api/v1/health` - Health check endpoint

## 🧪 **Testing**

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=app

# Run specific test file
pytest tests/test_auth.py
```

## 🔍 **Troubleshooting**

### **Common Issues**

#### **Database Connection Failed**
```bash
# Check if database is running
docker compose ps

# Check database logs
docker compose logs db

# Verify DATABASE_URL format
echo $DATABASE_URL
```

#### **Migrations Not Running**
```bash
# Check container logs
docker compose logs api

# Should see: "Running database migrations..."
# If not, check environment variables
```

#### **Running Migrations Manually**
If automatic migrations fail, you can run them manually:

**Option 1: From outside the container**
```bash
# If using docker-compose
docker compose exec api alembic upgrade head

# If using docker run
docker exec -it nombre-del-contenedor alembic upgrade head
```

**Option 2: Enter the container and run**
```bash
# Enter the container
docker exec -it nombre-del-contenedor bash

# Inside the container, run:
alembic upgrade head
```

**Option 3: Check migration status first**
```bash
# Check current migration status
docker compose exec api alembic current

# View migration history
docker compose exec api alembic history

# View pending migrations
docker compose exec api alembic show head
```

#### **Port Already in Use**
```bash
# Check what's using port 8000
lsof -i :8000

# Kill process or change port in docker-compose.yml
```

## 📁 **Project Structure**
```
celuma-backend/
├── app/
│   ├── api/v1/
│   │   ├── auth.py      # Authentication endpoints
│   │   └── users.py     # User endpoints
│   ├── core/
│   │   ├── config.py    # Configuration settings
│   │   ├── db.py        # Database connection
│   │   └── security.py  # Security utilities
│   ├── models/
│   │   └── user.py      # User model
│   ├── schemas/
│   │   └── user.py      # User schemas
│   └── main.py          # FastAPI application
├── alembic/              # Database migrations
├── docker-compose.yml    # Development Docker services
├── docker-compose.prod.yml # Production Docker services
├── Dockerfile           # Docker image definition
├── start.sh             # Startup script with migrations
├── Makefile             # Development commands
└── requirements.txt     # Python dependencies
```

## 📝 **License**

This project is licensed under the MIT License.
