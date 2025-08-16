# Celuma Backend

A FastAPI-based backend service with user authentication, PostgreSQL database, and Docker containerization.

## 🚀 Features

- **FastAPI** - Modern, fast web framework for building APIs
- **User Authentication** - JWT-based authentication system
- **PostgreSQL** - Robust relational database
- **SQLModel** - SQL databases in Python, designed for simplicity
- **Alembic** - Database migration tool
- **Docker** - Containerized development environment
- **Password Hashing** - Secure password storage with bcrypt
- **CI/CD Pipeline** - Automated testing and Docker image building
- **Production Ready** - Optimized Docker images for deployment

## 📋 Requirements

### System Requirements
- **Python** 3.11+ (tested with 3.11.8)
- **Docker** 20.0+
- **Docker Compose** 2.0+

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

## 🛠️ Installation

### 1. Clone the repository
```bash
git clone <repository-url>
cd celuma-backend
```

### 2. Create and activate virtual environment
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure environment variables
```bash
cp .env.example .env
# Edit .env with your configuration values
```

## 🚀 Usage

### Quick Start

#### Development Environment
```bash
# Start the application with Docker (development)
make run

# Or manually
docker compose up --build
```

#### Production Environment
```bash
# Start the application with production Docker Compose
docker compose -f docker-compose.prod.yml up -d

# Or with custom environment file
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

**Note**: The production environment uses the pre-built Docker image from GitHub Container Registry (GHCR) instead of building locally.

### Available Commands
```bash
# Start the application
make run

# Create a new migration
make revision m="description of changes"

# Apply migrations
make migrate
```

### API Endpoints

Once running, the API will be available at:
- **API Base URL**: http://localhost:8000
- **Interactive Documentation**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/api/v1/health

#### Authentication Endpoints
- `POST /api/v1/auth/register` - User registration
- `POST /api/v1/auth/login` - User login
- `GET /api/v1/auth/me` - Get current user info

#### User Endpoints
- `GET /api/v1/health` - Health check endpoint

## 🗄️ Database

The application uses PostgreSQL with automatic migrations via Alembic:

```bash
# Check migration status
docker compose exec api alembic current

# View database tables
docker compose exec db psql -U postgres -d celumadb -c "\dt"
```

## 🔧 Development

## 🚀 Production Deployment

### Using Production Docker Compose

The project includes a production-ready Docker Compose configuration that uses the pre-built Docker image from GitHub Container Registry.

#### 1. Production Environment Variables
Create a `.env.prod` file with your production settings:
```bash
# .env.prod
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/celumadb
JWT_SECRET=your-super-secret-jwt-key-change-this-in-production
JWT_EXPIRES_MIN=480
APP_NAME=celuma
ENV=production
```

#### 2. Start Production Services
```bash
# Start all services (database + API)
docker compose -f docker-compose.prod.yml up -d

# Check service status
docker compose -f docker-compose.prod.yml ps

# View logs
docker compose -f docker-compose.prod.yml logs -f api
```

#### 3. Production Features
- **Pre-built Image**: Uses optimized Docker image from CI/CD pipeline
- **No Hot Reload**: Production-optimized without development overhead
- **Persistent Database**: PostgreSQL data persisted in Docker volumes
- **Health Checks**: Automatic restart policies for production reliability

#### 4. Scaling and Management
```bash
# Scale API service (if needed)
docker compose -f docker-compose.prod.yml up -d --scale api=3

# Update to latest image
docker compose -f docker-compose.prod.yml pull api
docker compose -f docker-compose.prod.yml up -d api

# Stop production services
docker compose -f docker-compose.prod.yml down
```

### Project Structure
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
├── docker-compose.yml    # Docker services
├── Dockerfile           # Docker image
├── Makefile             # Development commands
└── requirements.txt     # Python dependencies
```

### Environment Variables
```bash
APP_NAME=celuma
ENV=dev
DATABASE_URL=postgresql+psycopg2://postgres:postgres@db:5432/celumadb
JWT_SECRET=your-secret-key
JWT_EXPIRES_MIN=480
```

## 🧪 Testing

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=app
```

## 📝 License

This project is licensed under the MIT License.
