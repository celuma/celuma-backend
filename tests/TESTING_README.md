# Celuma API Testing Suite

This directory contains a comprehensive testing suite to verify the functionality of the Celuma API.

## 📋 Available Testing Scripts

### 1. `test_endpoints.py` - Complete Flow Tests
**Purpose:** Verifies that the complete API flow works correctly.

**Features:**
- ✅ Basic endpoint tests (health, root)
- ✅ Tenant creation
- ✅ Branch creation
- ✅ Patient creation
- ✅ Laboratory order creation
- ✅ Sample creation
- ✅ Report creation
- ✅ Invoice creation
- ✅ Payment creation
- ✅ Verification of all created entities

### 2. `test_auth_logout.py` - Authentication Logout Tests
**Purpose:** Verifies that the logout functionality works correctly.

**Features:**
- ✅ User registration and login
- ✅ Token authentication verification
- ✅ Logout endpoint functionality
- ✅ Token blacklisting verification
- ✅ Double logout handling
- ✅ Blacklisted token access prevention

**Usage:**
```bash
python test_endpoints.py
```

**Expected Output:**
```
🚀 Testing Celuma API Complete Flow
==================================================
🔍 Running basic endpoint tests...
✅ Root endpoint: 200
✅ Health endpoint: 200

🔄 Running complete flow tests...
✅ Create tenant: 200
✅ Create branch: 200
✅ Create patient: 200
✅ Create order: 200
✅ Create sample: 200
✅ Create report: 200
✅ Create invoice: 200
✅ Create payment: 200

🎉 All tests passed! The complete flow is working correctly!
```

### 3. `test_validation_errors.py` - Validation and Error Handling Tests
**Purpose:** Verifies that the API correctly handles error cases and validations.

**Features:**
- 🔍 Invalid tenant ID tests
- 🔍 Duplicate code tests
- 🔍 Missing required field tests
- 🔍 Invalid enum value tests
- 🔍 Non-existent entity tests

**Usage:**
```bash
python test_validation_errors.py
```

**Expected Output:**
```
🚀 Testing Celuma API Validation and Error Handling
============================================================
🔍 Testing invalid tenant ID scenarios...
✅ Branch with invalid tenant: 404
✅ Patient with invalid tenant: 404

🔍 Testing duplicate code scenarios...
✅ Duplicate branch code: 400
✅ Duplicate patient code: 400

✅ Validation error tests completed!
```

### 4. `test_performance.py` - Performance Tests
**Purpose:** Measures the performance of API endpoints.

**Features:**
- ⚡ Basic endpoint performance tests (GET)
- ⚡ Creation endpoint performance tests (POST)
- ⚡ Response time statistics (average, median, min, max)
- 📊 Performance report with recommendations

**Usage:**
```bash
python test_performance.py
```

**Expected Output:**
```
🚀 Celuma API Performance Testing
============================================================
🔍 Testing GET /...
✅ Successful requests: 10/10
📊 Response times (ms):
   Average: 7.79
   Median:  7.45
   Min:     6.12
   Max:     12.34

🎯 Performance Summary:
   🚀 Excellent performance across all endpoints
   💡 Consider implementing caching for frequently accessed data
```

### 5. `cleanup_test_data.py` - Test Data Analysis
**Purpose:** Analyzes and reports on test data that would be deleted.

**Features:**
- 📊 Counts all entities in the system
- 🔍 Shows data relationships and dependencies
- 📋 Provides deletion order recommendations
- ⚠️ Note: No actual deletion occurs (DELETE endpoints not implemented)

**Usage:**
```bash
python cleanup_test_data.py
```

**Expected Output:**
```
🧹 Celuma API Test Data Cleanup
============================================================
📊 Current System Status:
   Tenants: 5
   Branches: 12
   Patients: 45
   Orders: 67
   Samples: 89
   Reports: 56
   Invoices: 34
   Payments: 23

🔍 Deletion Analysis:
   Would delete 23 payments first
   Would delete 34 invoices next
   Would delete 56 reports next
   ... (continues with deletion order)

⚠️  Note: No actual deletion occurs - DELETE endpoints not implemented
```

### 6. `run_all_tests.py` - Master Test Runner
**Purpose:** Orchestrates all test suites and generates a comprehensive report.

**Features:**
- 🚀 Runs all test suites sequentially
- 📊 Generates comprehensive test report
- 📈 Provides success/failure statistics
- 💡 Offers recommendations based on results
- 🎯 Final status summary

**Usage:**
```bash
python run_all_tests.py
```

**Expected Output:**
```
🚀 CELUMA API COMPREHENSIVE TESTING SUITE
================================================================================
📅 Started: 2025-01-17 17:14:21

============================================================
🚀 Running Complete Flow Tests
============================================================
✅ Test script completed successfully

============================================================
🚀 Running Validation and Error Handling Tests
============================================================
✅ Test script completed successfully

============================================================
🚀 Running Performance Tests
============================================================
✅ Test script completed successfully

================================================================================
📊 CELUMA API COMPREHENSIVE TEST REPORT
================================================================================
📅 Generated: 2025-01-17 17:14:24

📈 TEST SUMMARY:
   Total test suites: 3
   ✅ Passed: 3
   ❌ Failed: 0
   Success rate: 100.0%

🎯 FINAL STATUS:
   🎉 ALL TESTS PASSED! The Celuma API is working perfectly!
```

## 🚀 Quick Start

### From Project Root
```bash
# Run all tests
make test

# Run specific test suites
make test-flow          # Complete flow tests
make test-logout        # Authentication logout tests
make test-validation    # Validation and error handling
make test-performance   # Performance tests
make test-cleanup       # Test data analysis

# Interactive test menu
make test-interactive
```

### From Tests Directory
```bash
cd tests

# Run all tests
python run_all_tests.py

# Run individual test suites
python test_endpoints.py
python test_auth_logout.py
python test_validation_errors.py
python test_performance.py
python cleanup_test_data.py
```

### Interactive Menu
```bash
# From project root
python run_tests.py

# Select from menu:
# 1. Complete Flow Tests
# 2. Authentication Logout Tests
# 3. Validation & Error Tests
# 4. Performance Tests
# 5. Test Data Cleanup
# 6. Run All Tests
# 7. Exit
```

## 📊 Understanding Test Results

### Test Status Indicators
- ✅ **PASS**: Test completed successfully
- ❌ **FAIL**: Test failed or encountered errors
- ⚠️ **WARNING**: Test completed with warnings
- 🔍 **RUNNING**: Test currently executing

### Performance Metrics
- **Response Time**: Time taken for API to respond (in milliseconds)
- **Success Rate**: Percentage of successful requests
- **Throughput**: Number of requests processed per second
- **Error Rate**: Percentage of failed requests

### Validation Test Results
- **404**: Correctly returned for non-existent resources
- **400**: Correctly returned for bad requests
- **422**: Correctly returned for validation errors
- **500**: Unexpected server errors (may indicate issues)

## 🔧 Test Configuration

### Environment Variables
```bash
# API base URL (default: http://localhost:8000)
export CELUMA_API_URL="http://localhost:8000"

# Test data cleanup (default: false)
export CLEANUP_TEST_DATA="true"

# Performance test iterations (default: 10)
export PERFORMANCE_ITERATIONS="20"
```

### Test Data Management
- Tests create real data in the database
- Each test run generates unique identifiers
- Data persists between test runs
- Use `cleanup_test_data.py` to analyze current data

## 🚨 Troubleshooting

### Common Issues

#### API Not Responding
```bash
# Check if API is running
make status

# Check API logs
make logs

# Verify Docker containers
docker ps
```

#### Database Connection Issues
```bash
# Check database status
docker logs celuma-backend-db-1

# Verify environment variables
cat .env | grep DATABASE_URL
```

#### Test Failures
```bash
# Run individual tests to isolate issues
python test_endpoints.py
python test_validation_errors.py
python test_performance.py

# Check test output for specific error messages
```

### Performance Issues
- **Slow Response Times**: Check database performance and API server resources
- **High Error Rates**: Verify API stability and database connectivity
- **Memory Issues**: Monitor Docker container resource usage

## 📈 Test Results Interpretation

### Success Criteria
- **Complete Flow Tests**: All entities created successfully
- **Validation Tests**: Correct HTTP status codes returned
- **Performance Tests**: Response times under acceptable thresholds

### Performance Benchmarks
- **Health Endpoint**: < 50ms
- **List Endpoints**: < 200ms
- **Creation Endpoints**: < 500ms
- **Complex Queries**: < 1000ms

### Recommendations Based on Results
- **All Tests Pass**: System is ready for production
- **Most Tests Pass**: Review failed tests for minor issues
- **Multiple Test Failures**: System has significant issues requiring attention

## 🔄 Continuous Integration

### Automated Testing
```bash
# Run tests in CI/CD pipeline
make test

# Exit with error code for CI systems
python tests/run_all_tests.py
exit_code=$?
exit $exit_code
```

### Test Reporting
- Tests generate detailed output for CI systems
- Exit codes indicate overall success/failure
- Performance metrics can be tracked over time
- Validation results help identify API issues

## 📚 Additional Resources

### API Documentation
- [API Endpoints](../API_ENDPOINTS.md) - Complete endpoint reference
- [API Examples](../API_EXAMPLES.md) - Usage examples and patterns
- [Database Schema](../DATABASE_README.md) - Database design and migrations

### Development Commands
```bash
make help                 # Show all available commands
make run                  # Start the API server
make stop                 # Stop the API server
make logs                 # Show API logs
make status               # Check system status
make clean                # Clean up containers and data
```

### Testing Best Practices
1. **Run tests before making changes** to establish baseline
2. **Run tests after changes** to verify functionality
3. **Use performance tests** to identify bottlenecks
4. **Monitor validation tests** to catch API issues early
5. **Clean up test data** regularly to maintain system performance

## 🤝 Contributing to Tests

### Adding New Tests
1. Create test file in the `tests/` directory
2. Follow naming convention: `test_*.py`
3. Include comprehensive test coverage
4. Add to `run_all_tests.py` if appropriate
5. Update this README with new test information

### Test Standards
- **Descriptive names**: Test functions should clearly describe what they test
- **Error handling**: Tests should handle and report errors gracefully
- **Performance**: Tests should complete within reasonable time limits
- **Documentation**: Include usage examples and expected outputs

---

**For questions or issues with the testing suite, please refer to the main project documentation or open an issue in the repository.**
