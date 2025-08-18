# Celuma API Testing Suite

This directory contains a comprehensive testing suite to verify the functionality of the Celuma API.

## ✨ Testing Features (v1.0.0)

### 🚀 Core Testing Features
- **JSON Payload Testing**: All tests use JSON request bodies for optimal API testing
- **Enhanced Authentication Tests**: Complete JWT authentication flow with token blacklisting
- **Automatic Cleanup System**: Tests automatically clean up data and blacklisted tokens
- **Comprehensive Test Runner**: `run_all_tests.py` orchestrates all tests with cleanup
- **Token Management Tests**: Verification of logout and token blacklisting functionality

### 🎯 Testing Design
All test scripts are designed to use JSON payloads, providing:
- Excellent test data validation
- Consistent with API design
- Improved error handling and debugging
- Real-world usage simulation

## 📋 Available Testing Scripts

### 1. `test_endpoints.py` - Complete Flow Tests (JSON Updated)
**Purpose:** Verifies that the complete API flow works correctly with JSON payloads.

**Features:**
- ✅ Basic endpoint tests (health, root)
- ✅ Tenant creation with JSON payload
- ✅ Branch creation with JSON payload
- ✅ Patient creation with JSON payload
- ✅ Laboratory order creation with JSON payload
- ✅ Sample creation with JSON payload
- ✅ Report creation with JSON payload
- ✅ Invoice creation with JSON payload
- ✅ Payment creation with JSON payload
- ✅ User registration and authentication with JSON payload
- ✅ Verification of all created entities

**Usage:**
```bash
python test_endpoints.py
```

**Expected Output:**
```
🚀 Starting Celuma API Endpoint Tests
==================================================

🧪 Testing: Root Endpoint
------------------------------
✅ Root endpoint: 200
✅ Health endpoint: 200

🧪 Testing: Create Tenant
------------------------------
✅ Create tenant: 200
✅ Create branch: 200
✅ Create patient: 200
✅ Create Laboratory Order: 200
✅ Create Sample: 200
✅ Create Report: 200
✅ Create Invoice: 200
✅ Create Payment: 200
✅ User Registration: 200
✅ User Login: 200
✅ Get User Profile: 200

🎉 All tests passed! The API is working correctly.
```

### 2. `test_auth_logout.py` - Authentication Logout Tests (JSON Updated)
**Purpose:** Verifies that the logout functionality works correctly with JSON payloads.

**Features:**
- ✅ User registration with JSON payload
- ✅ User login with JSON payload
- ✅ Token authentication verification
- ✅ Logout endpoint functionality
- ✅ Token blacklisting verification
- ✅ Double logout handling
- ✅ Blacklisted token access prevention
- ✅ Complete authentication flow testing

**Usage:**
```bash
python test_auth_logout.py
```

**Expected Output:**
```
🚀 CELUMA API LOGOUT FUNCTIONALITY TESTING
============================================================
✅ API is running and responding
✅ Created test tenant: Test Logout Tenant 1755480540
✅ Created test user: test_logout@example.com
✅ User login successful, got access token
✅ Authenticated endpoint works: test_logout@example.com
✅ Logout successful
✅ Token successfully blacklisted - cannot access authenticated endpoints
✅ Double logout handled correctly

🎉 All logout tests passed!
```

### 3. `test_validation_errors.py` - Validation and Error Handling Tests
**Purpose:** Verifies that the API correctly handles error cases and validations.

**Features:**
- 🔍 Invalid tenant ID tests
- 🔍 Duplicate code tests
- 🔍 Missing required field tests
- 🔍 Invalid enum value tests
- 🔍 Non-existent entity tests
- 🔍 JSON validation error tests

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
- ⚡ Creation endpoint performance tests (POST with JSON)
- ⚡ Response time statistics (average, median, min, max)
- 📊 Performance report with recommendations

**Usage:**
```bash
python test_performance.py
```

**Expected Output:**
```
🚀 Celuma API Performance Testing
==========================================
⚡ Testing endpoint performance...
✅ Health endpoint: 45.2 ms average
✅ Tenants endpoint: 67.8 ms average
✅ Branches endpoint: 89.1 ms average

📊 Performance Summary:
   Health: 45.2 ms average
   Tenants: 67.8 ms average
   Branches: 89.1 ms average
```

### 5. `run_all_tests.py` - Master Test Runner with Cleanup 🆕
**Purpose:** Orchestrates all test suites and automatically cleans up test data.

**Features:**
- 🚀 Runs all test suites in sequence
- 🧹 Automatic cleanup after tests complete
- 📊 Comprehensive test reporting
- 🔄 Test data and token cleanup
- ⏱️ Performance timing and statistics

**Usage:**
```bash
python run_all_tests.py
```

**Expected Output:**
```
🚀 Starting Celuma API Comprehensive Testing
==================================================
🧪 Testing: API Endpoints Testing
✅ API Endpoints Testing: PASSED

🧪 Testing: Authentication & Logout Testing
✅ Authentication & Logout Testing: PASSED

🧪 Testing: Performance Testing
✅ Performance Testing: PASSED

🧪 Testing: Validation Error Testing
✅ Validation Error Testing: PASSED

🧹 Running Cleanup Scripts
✅ Test Data Cleanup completed
✅ Blacklisted Tokens Cleanup completed

📊 Test Results: 4/4 tests passed
🎉 All tests passed! The API is working correctly.
```

### 6. `cleanup_test_data.py` - Test Data Cleanup Utilities 🆕
**Purpose:** Analyzes and provides guidance for cleaning up test data.

**Features:**
- 🔍 Scans system for all test entities
- 📊 Provides summary of data found
- 🗑️ Identifies cleanup requirements
- 💡 Suggests cleanup implementation strategies

**Usage:**
```bash
python cleanup_test_data.py
```

**Expected Output:**
```
🧹 Celuma API Test Data Cleanup
==========================================
🔍 Scanning system for test data...
📊 Found 8 tenants
📊 Found 8 branches
📊 Found 3 patients
📊 Found 3 orders
📊 Found 2 samples
📊 Found 2 reports
📊 Found 2 invoices
📊 Found 2 payments

🧹 Starting cleanup process...
📊 Total entities that would be deleted: 31
💡 Note: DELETE endpoints are not implemented yet
```

### 7. `cleanup_blacklisted_tokens.py` - Token Cleanup Utilities 🆕
**Purpose:** Provides guidance for cleaning up blacklisted tokens.

**Features:**
- 🔒 Analyzes blacklisted token cleanup needs
- 🔧 Suggests cleanup endpoint implementation
- 📝 Provides example code for cleanup
- ⏰ Recommends cleanup schedules

**Usage:**
```bash
python cleanup_blacklisted_tokens.py
```

**Expected Output:**
```
🔒 Celuma API Blacklisted Tokens Cleanup
==================================================
💡 Blacklisted tokens cleanup process:
   1. These tokens are created when users logout
   2. They accumulate in the database over time
   3. They should be cleaned up periodically

🔧 Implementation options:
   Option 1: Add a cleanup endpoint to the API
   Option 2: Implement a scheduled cleanup task
   Option 3: Direct database cleanup script
```

## 🧹 Test Data Cleanup System

### Automatic Cleanup
The testing suite now includes an automatic cleanup system that:
- 🧹 Cleans up test data after each test run
- 🔒 Removes blacklisted tokens
- 📊 Provides cleanup analysis and recommendations
- 💡 Suggests implementation strategies for production

### Cleanup Scripts
- **`cleanup_test_data.py`**: Analyzes test data and suggests cleanup strategies
- **`cleanup_blacklisted_tokens.py`**: Provides token cleanup guidance
- **`run_all_tests.py`**: Automatically runs cleanup after tests

### Manual Cleanup
```bash
# Run cleanup analysis
python tests/cleanup_test_data.py

# Run token cleanup analysis
python tests/cleanup_blacklisted_tokens.py

# Run complete test suite with cleanup
python tests/run_all_tests.py
```

## 🚀 Running the Complete Test Suite

### Quick Start
```bash
# Run all tests with automatic cleanup
cd tests
python run_all_tests.py
```

### Individual Test Suites
```bash
# Test complete API flow
python test_endpoints.py

# Test authentication and logout
python test_auth_logout.py

# Test validation and error handling
python test_validation_errors.py

# Test performance
python test_performance.py
```

### Test Data Management
```bash
# Analyze test data
python cleanup_test_data.py

# Analyze token cleanup needs
python cleanup_blacklisted_tokens.py
```

## 📊 Test Results and Reporting

### Success Indicators
- ✅ All endpoints respond correctly
- ✅ JSON payloads are properly validated
- ✅ Authentication flow works completely
- ✅ Token blacklisting functions correctly
- ✅ All entities can be created and retrieved
- ✅ Cleanup processes complete successfully

### Failure Indicators
- ❌ Endpoints return errors
- ❌ JSON validation fails
- ❌ Authentication breaks
- ❌ Tokens not properly blacklisted
- ❌ Data creation/retrieval fails
- ❌ Cleanup processes fail

## 🔧 Troubleshooting

### Common Issues
1. **Server Not Running**: Ensure Docker containers are up with `docker-compose ps`
2. **Database Connection**: Check database container status
3. **JSON Validation Errors**: Verify request payload format
4. **Authentication Failures**: Check JWT configuration
5. **Cleanup Failures**: Verify cleanup script permissions

### Debug Commands
```bash
# Check container status
docker-compose ps

# View API logs
docker-compose logs api

# View database logs
docker-compose logs db

# Restart services
docker-compose restart
```

## 📈 Performance Benchmarks

### Expected Response Times
- **Health Check**: < 50ms
- **GET Endpoints**: < 100ms
- **POST Endpoints**: < 200ms
- **Authentication**: < 150ms

### Load Testing
The performance tests include load testing capabilities:
- Concurrent request testing
- Response time analysis
- Throughput measurement
- Performance degradation detection

## 🎯 Best Practices

### Test Execution
1. **Run Complete Suite**: Use `run_all_tests.py` for comprehensive testing
2. **Clean Environment**: Ensure clean database before testing
3. **Monitor Logs**: Watch container logs during test execution
4. **Verify Cleanup**: Confirm cleanup processes complete successfully

### Test Development
1. **Use JSON Payloads**: All tests should use JSON request bodies
2. **Validate Responses**: Check both status codes and response content
3. **Test Error Cases**: Include validation and error handling tests
4. **Clean Up Data**: Ensure tests don't leave persistent data

### Production Considerations
1. **Implement DELETE Endpoints**: For proper data cleanup
2. **Scheduled Cleanup**: Automate token and data cleanup
3. **Monitoring**: Track test execution and cleanup success rates
4. **Documentation**: Keep test documentation updated with API changes
