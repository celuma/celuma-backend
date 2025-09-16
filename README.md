# Celuma API - Multi-tenant Laboratory Management System

A comprehensive, multi-tenant laboratory management system built with FastAPI, SQLModel, and PostgreSQL.

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose
- Python 3.11+
- Make (optional, for convenience commands)

### 1. Clone and Setup
```bash
git clone <repository-url>
cd celuma-backend
```

### 2. Start the System
```bash
# Using Make (recommended)
make run

# Or manually
docker-compose up -d
```

### 3. Verify Installation
```bash
# Check status
make status

# Or manually
curl http://localhost:8000/api/v1/health
```

## ✨ Features (v1.0.0)

### 🚀 Core Features
- **JSON Payloads**: All POST endpoints use JSON request bodies for optimal data handling
- **Pydantic Schemas**: Complete type safety and automatic validation for all endpoints
- **Enhanced Authentication**: Robust JWT system with token blacklisting
- **Auto-generated Documentation**: Complete OpenAPI/Swagger documentation
- **Comprehensive Testing**: Full test suite with automatic cleanup

### 🎯 Design Principles
The API is designed with JSON request bodies for all POST endpoints, providing:
- Excellent data validation with Pydantic schemas
- Strong type safety and developer experience
- Consistent API design following REST best practices
- Auto-generated documentation and examples

## 🧪 Testing

The project includes a comprehensive testing suite located in the `tests/` directory.

### Quick Test Commands
```bash
# Run all tests with automatic cleanup
make test

# Run specific test suites
make test-flow          # Complete flow tests
make test-validation    # Validation and error handling
make test-performance   # Performance tests
make test-cleanup       # Test data analysis

# Interactive test menu
make test-interactive
```

### Test Structure
```
tests/
├── __init__.py                 # Package initialization
├── test_endpoints.py          # Complete flow tests (JSON updated)
├── test_validation_errors.py  # Validation tests
├── test_performance.py        # Performance tests
├── test_auth_logout.py        # Authentication and logout tests
├── cleanup_test_data.py       # Data cleanup utilities
├── cleanup_blacklisted_tokens.py # Token cleanup utilities
├── run_all_tests.py          # Master test runner with cleanup
└── TESTING_README.md         # Detailed testing documentation
```

### Running Tests Manually
```bash
# From project root
python run_tests.py

# From tests directory
cd tests
python run_all_tests.py          # Complete test suite with cleanup
python test_endpoints.py         # Endpoint flow tests
python test_validation_errors.py # Validation tests
python test_performance.py       # Performance tests
python test_auth_logout.py       # Authentication tests
```

## 📚 Documentation

- [API Endpoints](API_ENDPOINTS.md) - Complete API reference (JSON updated)
- [API Examples](API_EXAMPLES.md) - Usage examples with JSON payloads
- [Database Schema](DATABASE_README.md) - Database design and migrations
- [Testing Guide](tests/TESTING_README.md) - Comprehensive testing documentation
- [Logging Guide](LOGGING.md) - Iconography and logging guidelines
- **[Deployment Guide](DEPLOYMENT_README.md)** - Complete deployment documentation

## 🏗️ Architecture

### Core Components
- **FastAPI**: Modern, fast web framework
- **SQLModel**: SQL databases in Python, designed for simplicity
- **PostgreSQL**: Robust, production-ready database
- **Alembic**: Database migration management
- **Docker**: Containerized deployment
- **Pydantic**: Data validation and serialization

### Multi-tenant Features
- Tenant isolation
- Branch management
- User role-based access control
- JWT authentication with token blacklisting
- Comprehensive audit logging

### API Design Features
- **JSON-first**: All POST endpoints use JSON request bodies
- **Type-safe**: Complete Pydantic schema validation
- **Auto-documented**: OpenAPI/Swagger documentation
- **RESTful**: Consistent API design patterns
- **Validation**: Automatic request/response validation

## 🗄️ Database Management

### Automatic Migrations
The system automatically handles database migrations on startup:
- **Development**: `docker-compose up --build` runs migrations automatically
- **GHCR**: `docker-compose.ghcr.yml` includes initialization service
- **Remote DB**: All deployment options include automatic migration execution

### Manual Migration Management
```bash
# Check current migration status
alembic current

# Create new migration (after model changes)
alembic revision --autogenerate -m "Description of changes"

# Apply migrations manually
alembic upgrade head

# Rollback to previous migration
alembic downgrade -1
```

### Database Cleanup
```bash
# Complete cleanup (removes all data)
./cleanup.sh

# This removes:
# - All containers
# - Database volumes
# - Dangling images
```

## 🔧 Development
```
celuma-backend/
├── app/                    # Main application code
│   ├── api/              # API endpoints
│   ├── core/             # Core configuration
│   ├── models/           # Database models
│   └── schemas/          # Pydantic schemas
├── tests/                 # Testing suite
├── alembic/              # Database migrations
├── docker-compose.yml    # Development environment
├── Makefile              # Development commands
└── requirements.txt      # Python dependencies
```

### Available Commands
```bash
make help                 # Show all available commands
make install              # Install Python dependencies
make run                  # Start the API server
make stop                 # Stop the API server
make logs                 # Show API logs
make status               # Check system status
make test                 # Run all tests
make clean                # Clean up containers and data
```

### Available Scripts
```bash
# Database Management
./init_db.sh             # Initialize database and run migrations
./cleanup.sh              # Complete system cleanup

# Deployment
./deploy_remote.sh        # Deploy single container with remote database
./start.sh                # Start API with database checks (used in containers)
```

### API Endpoints
- **Health**: `GET /api/v1/health`
- **Authentication**: 
  - `POST /api/v1/auth/login` - Flexible login with username or email
  - `POST /api/v1/auth/register` - User registration with optional username
  - `GET /api/v1/auth/me` - Get current user profile
  - `POST /api/v1/auth/logout` - Logout and token blacklisting
- **Tenants**: `GET/POST /api/v1/tenants/`
- **Branches**: `GET/POST /api/v1/branches/`
- **Patients**: `GET/POST /api/v1/patients/`
- **Laboratory**: `GET/POST /api/v1/laboratory/orders/`, `GET/POST /api/v1/laboratory/samples/`
- **Reports**: `GET/POST /api/v1/reports/`
- **Billing**: `GET/POST /api/v1/billing/invoices/`, `GET/POST /api/v1/billing/payments/`

### 🔐 Authentication Features
- **Flexible Login**: Users can authenticate using either username or email
- **Optional Username**: Username field is completely optional during registration
- **Multi-tenant Support**: All authentication is tenant-scoped
- **JWT Tokens**: Secure stateless authentication with configurable expiration

## 🚀 Deployment

### Development Environment (Local Database)
```bash
# Start with local PostgreSQL database
docker-compose up --build

# This automatically:
# 1. Creates PostgreSQL database
# 2. Runs all Alembic migrations
# 3. Starts the API server
```

### GHCR Environment (Local Database)
```bash
# Use GHCR compose file with local database
docker-compose -f docker-compose.ghcr.yml up --build

# This includes:
# - Database initialization service
# - Automatic migrations
# - Production-ready configuration
```

### Remote Database Deployment
```bash
# Option 1: Using docker-compose with remote database
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export JWT_SECRET="your-secret-key"
docker-compose -f docker-compose.remote-db.yml up --build

# Option 2: Deploy single container with remote database
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export JWT_SECRET="your-secret-key"
./deploy_remote.sh
```

### Environment Variables
Create a `.env` file or set environment variables:
```env
# Required
DATABASE_URL=postgresql://user:pass@host:5432/dbname
JWT_SECRET=your-super-secret-jwt-key

# Optional (with defaults)
JWT_EXPIRES_MIN=480
APP_NAME=celuma
ENV=production

# AWS S3 (required for image uploads)
AWS_ACCESS_KEY_ID=your-aws-access-key
AWS_SECRET_ACCESS_KEY=your-aws-secret
AWS_REGION=us-east-1
S3_BUCKET_NAME=your-bucket-name

# Media / CDN (optional)
# Use CloudFront or CDN base URL for permanent public links
MEDIA_PUBLIC_BASE_URL=https://dxxxxxxxxxxxx.cloudfront.net

# Presigned URL expiry in seconds (used if generating presigned links)
MEDIA_PRESIGNED_EXPIRE_SECONDS=3600

# Custom S3 endpoint (LocalStack/MinIO)
# S3_ENDPOINT_URL=http://localhost:4566
```

## 📊 Monitoring

### Health Checks
- API health: `GET /api/v1/health`
- Database connectivity: Built into health endpoint
- Docker container status: `make status`

### Logs
```bash
# API logs
make logs

# Docker logs
docker logs celuma-backend-api-1
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass: `make test`
6. Submit a pull request

### Testing Requirements
- All new features must include tests
- Run the full test suite before submitting: `make test`
- Maintain or improve test coverage

## 📄 License

[Add your license information here]

## 🆘 Support

For issues and questions:
- Check the [documentation](tests/TESTING_README.md)
- Review [API examples](API_EXAMPLES.md)
- Open an issue in the repository

---

**Built with ❤️ for modern laboratory management**
