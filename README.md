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

## 🧪 Testing

The project includes a comprehensive testing suite located in the `tests/` directory.

### Quick Test Commands
```bash
# Run all tests
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
├── test_endpoints.py          # Complete flow tests
├── test_validation_errors.py  # Validation tests
├── test_performance.py        # Performance tests
├── cleanup_test_data.py       # Data cleanup utilities
├── run_all_tests.py          # Master test runner
└── TESTING_README.md         # Detailed testing documentation
```

### Running Tests Manually
```bash
# From project root
python run_tests.py

# From tests directory
cd tests
python run_all_tests.py
python test_endpoints.py
python test_validation_errors.py
python test_performance.py
```

## 📚 Documentation

- [API Endpoints](API_ENDPOINTS.md) - Complete API reference
- [Database Schema](DATABASE_README.md) - Database design and migrations
- [Testing Guide](tests/TESTING_README.md) - Comprehensive testing documentation
- [API Examples](API_EXAMPLES.md) - Usage examples and patterns

## 🏗️ Architecture

### Core Components
- **FastAPI**: Modern, fast web framework
- **SQLModel**: SQL databases in Python, designed for simplicity
- **PostgreSQL**: Robust, production-ready database
- **Alembic**: Database migration management
- **Docker**: Containerized deployment

### Multi-tenant Features
- Tenant isolation
- Branch management
- User role-based access control
- Audit logging

### Laboratory Management
- Patient management
- Sample tracking
- Order processing
- Report generation
- Billing system

## 🔧 Development

### Project Structure
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

### API Endpoints
- **Health**: `GET /api/v1/health`
- **Authentication**: `POST /api/v1/auth/login`, `POST /api/v1/auth/register`
- **Tenants**: `GET/POST /api/v1/tenants/`
- **Branches**: `GET/POST /api/v1/branches/`
- **Patients**: `GET/POST /api/v1/patients/`
- **Laboratory**: `GET/POST /api/v1/laboratory/orders/`, `GET/POST /api/v1/laboratory/samples/`
- **Reports**: `GET/POST /api/v1/reports/`
- **Billing**: `GET/POST /api/v1/billing/invoices/`, `GET/POST /api/v1/billing/payments/`

## 🚀 Deployment

### Production
```bash
# Use production compose file
docker-compose -f docker-compose.prod.yml up -d
```

### Environment Variables
Create a `.env` file with:
```env
APP_NAME=celuma
ENV=prod
DATABASE_URL=postgresql+psycopg2://user:pass@host:port/db
JWT_SECRET=your-secret-key
JWT_EXPIRES_MIN=480
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
