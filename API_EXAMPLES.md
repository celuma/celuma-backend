# Celuma API Usage Examples

This document provides comprehensive examples of how to use the Celuma API with JSON payloads.

## 🚀 JSON-First API Design

**All POST endpoints use JSON request bodies for optimal data handling and validation.**

### Benefits of JSON Payloads
- ✅ **Excellent Data Validation**: Pydantic schemas provide automatic validation
- ✅ **Type Safety**: Strong typing for all request and response data
- ✅ **Consistent API Design**: All endpoints follow the same pattern
- ✅ **Superior Developer Experience**: Clear data structure and validation errors
- ✅ **Auto-generated Documentation**: OpenAPI/Swagger documentation with examples

## 🔐 Authentication

### User Registration
```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "securepassword123",
    "full_name": "John Doe",
    "role": "admin",
    "tenant_id": "tenant-uuid-here"
  }'
```

**Python Example:**
```python
import requests

response = requests.post(
    "http://localhost:8000/api/v1/auth/register",
    json={
        "email": "user@example.com",
        "password": "securepassword123",
        "full_name": "John Doe",
        "role": "admin",
        "tenant_id": "tenant-uuid-here"
    }
)

if response.status_code == 200:
    user_data = response.json()
    print(f"User created: {user_data['email']}")
```

### User Login
```bash
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "securepassword123",
    "tenant_id": "tenant-uuid-here"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/auth/login",
    json={
        "email": "user@example.com",
        "password": "securepassword123",
        "tenant_id": "tenant-uuid-here"
    }
)

if response.status_code == 200:
    auth_data = response.json()
    access_token = auth_data['access_token']
    print(f"Login successful, token: {access_token}")
```

### Using Authentication Token
```bash
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  http://localhost:8000/api/v1/auth/me
```

**Python Example:**
```python
headers = {"Authorization": f"Bearer {access_token}"}
response = requests.get(
    "http://localhost:8000/api/v1/auth/me",
    headers=headers
)

if response.status_code == 200:
    profile = response.json()
    print(f"User profile: {profile['full_name']}")
```

### User Logout
```bash
curl -X POST "http://localhost:8000/api/v1/auth/logout" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/auth/logout",
    headers=headers
)

if response.status_code == 200:
    logout_data = response.json()
    print(f"Logout successful: {logout_data['message']}")
```

## 🏢 Tenant Management

### Create Tenant
```bash
curl -X POST "http://localhost:8000/api/v1/tenants/" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Acme Laboratories",
    "legal_name": "Acme Laboratories Inc.",
    "tax_id": "123456789"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/tenants/",
    json={
        "name": "Acme Laboratories",
        "legal_name": "Acme Laboratories Inc.",
        "tax_id": "123456789"
    }
)

if response.status_code == 200:
    tenant = response.json()
    tenant_id = tenant['id']
    print(f"Tenant created: {tenant['name']}")
```

### List All Tenants
```bash
curl http://localhost:8000/api/v1/tenants/
```

**Python Example:**
```python
response = requests.get("http://localhost:8000/api/v1/tenants/")
if response.status_code == 200:
    tenants = response.json()
    for tenant in tenants:
        print(f"- {tenant['name']} ({tenant['id']})")
```

### Get Tenant Details
```bash
curl http://localhost:8000/api/v1/tenants/TENANT_UUID
```

**Python Example:**
```python
response = requests.get(f"http://localhost:8000/api/v1/tenants/{tenant_id}")
if response.status_code == 200:
    tenant = response.json()
    print(f"Tenant: {tenant['name']}")
    print(f"Legal Name: {tenant['legal_name']}")
    print(f"Tax ID: {tenant['tax_id']}")
```

## 🏥 Branch Management

### Create Branch
```bash
curl -X POST "http://localhost:8000/api/v1/branches/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "code": "MAIN",
    "name": "Main Branch",
    "city": "Mexico City",
    "state": "CDMX",
    "country": "MX"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/branches/",
    json={
        "tenant_id": tenant_id,
        "code": "MAIN",
        "name": "Main Branch",
        "city": "Mexico City",
        "state": "CDMX",
        "country": "MX"
    }
)

if response.status_code == 200:
    branch = response.json()
    branch_id = branch['id']
    print(f"Branch created: {branch['name']}")
```

### List Tenant Branches
```bash
curl http://localhost:8000/api/v1/tenants/TENANT_UUID/branches
```

**Python Example:**
```python
response = requests.get(f"http://localhost:8000/api/v1/tenants/{tenant_id}/branches")
if response.status_code == 200:
    branches = response.json()
    for branch in branches:
        print(f"- {branch['name']} ({branch['code']})")
```

## 👥 Patient Management

### Create Patient
```bash
curl -X POST "http://localhost:8000/api/v1/patients/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "patient_code": "P001",
    "first_name": "John",
    "last_name": "Doe",
    "dob": "1990-01-01",
    "sex": "M",
    "phone": "555-1234",
    "email": "john.doe@example.com"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/patients/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "patient_code": "P001",
        "first_name": "John",
        "last_name": "Doe",
        "dob": "1990-01-01",
        "sex": "M",
        "phone": "555-1234",
        "email": "john.doe@example.com"
    }
)

if response.status_code == 200:
    patient = response.json()
    patient_id = patient['id']
    print(f"Patient created: {patient['first_name']} {patient['last_name']}")
```

### List All Patients
```bash
curl http://localhost:8000/api/v1/patients/
```

**Python Example:**
```python
response = requests.get("http://localhost:8000/api/v1/patients/")
if response.status_code == 200:
    patients = response.json()
    for patient in patients:
        print(f"- {patient['first_name']} {patient['last_name']} ({patient['patient_code']})")
```

## 🧪 Laboratory Management

### Create Laboratory Order
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/orders/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "patient_id": "patient-uuid-here",
    "order_code": "ORD001",
    "requested_by": "Dr. Smith",
    "notes": "Complete blood count requested"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/laboratory/orders/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "patient_id": patient_id,
        "order_code": "ORD001",
        "requested_by": "Dr. Smith",
        "notes": "Complete blood count requested"
    }
)

if response.status_code == 200:
    order = response.json()
    order_id = order['id']
    print(f"Order created: {order['order_code']}")
```

### Create Sample
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/samples/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "order_id": "order-uuid-here",
    "sample_code": "SAMP001",
    "type": "SANGRE",
    "notes": "Blood sample for CBC"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/laboratory/samples/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "order_id": order_id,
        "sample_code": "SAMP001",
        "type": "SANGRE",
        "notes": "Blood sample for CBC"
    }
)

if response.status_code == 200:
    sample = response.json()
    sample_id = sample['id']
    print(f"Sample created: {sample['sample_code']}")
```

## 📋 Report Management

### Create Report
```bash
curl -X POST "http://localhost:8000/api/v1/reports/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "order_id": "order-uuid-here",
    "title": "Blood Test Report",
    "diagnosis_text": "Normal blood count results"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/reports/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "order_id": order_id,
        "title": "Blood Test Report",
        "diagnosis_text": "Normal blood count results"
    }
)

if response.status_code == 200:
    report = response.json()
    report_id = report['id']
    print(f"Report created: {report['title']}")
```

### List All Reports
```bash
curl http://localhost:8000/api/v1/reports/
```

**Python Example:**
```python
response = requests.get("http://localhost:8000/api/v1/reports/")
if response.status_code == 200:
    reports = response.json()
    for report in reports:
        print(f"- {report['title']} (Status: {report['status']})")
```

## 💰 Billing Management

### Create Invoice
```bash
curl -X POST "http://localhost:8000/api/v1/billing/invoices/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "order_id": "order-uuid-here",
    "invoice_number": "INV001",
    "amount_total": 1500.00,
    "currency": "MXN"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/billing/invoices/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "order_id": order_id,
        "invoice_number": "INV001",
        "amount_total": 1500.00,
        "currency": "MXN"
    }
)

if response.status_code == 200:
    invoice = response.json()
    invoice_id = invoice['id']
    print(f"Invoice created: {invoice['invoice_number']}")
```

### Create Payment
```bash
curl -X POST "http://localhost:8000/api/v1/billing/payments/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "invoice_id": "invoice-uuid-here",
    "amount_paid": 1500.00,
    "method": "credit_card"
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/billing/payments/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "invoice_id": invoice_id,
        "amount_paid": 1500.00,
        "method": "credit_card"
    }
)

if response.status_code == 200:
    payment = response.json()
    print(f"Payment created: ${payment['amount_paid']}")
```

## 📚 API Usage Examples

### JSON Payload Format
```bash
# JSON Payload Format
curl -X POST "http://localhost:8000/api/v1/tenants/" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Acme Lab",
    "legal_name": "Acme Lab Inc",
    "tax_id": "123456789"
  }'
```

### Python Implementation
```python
# Python JSON Implementation
response = requests.post(
    "http://localhost:8000/api/v1/tenants/",
    json={
        "name": "Acme Lab",
        "legal_name": "Acme Lab Inc",
        "tax_id": "123456789"
    }
)
```

## 📊 Complete Flow Example

Here's a complete example of creating a full laboratory workflow:

```python
import requests
import json

# Base URL
BASE_URL = "http://localhost:8000"

# 1. Create Tenant
tenant_response = requests.post(
    f"{BASE_URL}/api/v1/tenants/",
    json={
        "name": "Test Laboratory",
        "legal_name": "Test Laboratory Inc",
        "tax_id": "987654321"
    }
)
tenant = tenant_response.json()
tenant_id = tenant['id']
print(f"✅ Tenant created: {tenant['name']}")

# 2. Create Branch
branch_response = requests.post(
    f"{BASE_URL}/api/v1/branches/",
    json={
        "tenant_id": tenant_id,
        "code": "MAIN",
        "name": "Main Branch",
        "city": "Mexico City",
        "state": "CDMX",
        "country": "MX"
    }
)
branch = branch_response.json()
branch_id = branch['id']
print(f"✅ Branch created: {branch['name']}")

# 3. Create Patient
patient_response = requests.post(
    f"{BASE_URL}/api/v1/patients/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "patient_code": "P001",
        "first_name": "Jane",
        "last_name": "Smith",
        "dob": "1985-05-15",
        "sex": "F",
        "phone": "555-9876",
        "email": "jane.smith@example.com"
    }
)
patient = patient_response.json()
patient_id = patient['id']
print(f"✅ Patient created: {patient['first_name']} {patient['last_name']}")

# 4. Create Laboratory Order
order_response = requests.post(
    f"{BASE_URL}/api/v1/laboratory/orders/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "patient_id": patient_id,
        "order_code": "ORD001",
        "requested_by": "Dr. Johnson",
        "notes": "Complete blood count and chemistry panel"
    }
)
order = order_response.json()
order_id = order['id']
print(f"✅ Order created: {order['order_code']}")

# 5. Create Sample
sample_response = requests.post(
    f"{BASE_URL}/api/v1/laboratory/samples/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "order_id": order_id,
        "sample_code": "SAMP001",
        "type": "SANGRE",
        "notes": "Blood sample for CBC and chemistry"
    }
)
sample = sample_response.json()
print(f"✅ Sample created: {sample['sample_code']}")

# 6. Create Report
report_response = requests.post(
    f"{BASE_URL}/api/v1/reports/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "order_id": order_id,
        "title": "Blood Test Results",
        "diagnosis_text": "Normal blood count and chemistry values"
    }
)
report = report_response.json()
print(f"✅ Report created: {report['title']}")

# 7. Create Invoice
invoice_response = requests.post(
    f"{BASE_URL}/api/v1/billing/invoices/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "order_id": order_id,
        "invoice_number": "INV001",
        "amount_total": 2500.00,
        "currency": "MXN"
    }
)
invoice = invoice_response.json()
print(f"✅ Invoice created: {invoice['invoice_number']}")

# 8. Create Payment
payment_response = requests.post(
    f"{BASE_URL}/api/v1/billing/payments/",
    json={
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "invoice_id": invoice['id'],
        "amount_paid": 2500.00,
        "method": "credit_card"
    }
)
payment = payment_response.json()
print(f"✅ Payment created: ${payment['amount_paid']}")

print("\n🎉 Complete laboratory workflow created successfully!")
```

## 🔍 Error Handling

### Validation Errors
When sending invalid data, you'll receive detailed validation errors:

```python
# Invalid email format
response = requests.post(
    "http://localhost:8000/api/v1/patients/",
    json={
        "tenant_id": "invalid-uuid",
        "branch_id": "invalid-uuid",
        "patient_code": "P001",
        "first_name": "John",
        "last_name": "Doe",
        "email": "invalid-email"  # Invalid email format
    }
)

if response.status_code == 422:
    errors = response.json()
    print("Validation errors:")
    for error in errors['detail']:
        print(f"- {error['loc'][1]}: {error['msg']}")
```

### Common HTTP Status Codes
- **200**: Success
- **201**: Created
- **400**: Bad Request (validation errors)
- **401**: Unauthorized (missing or invalid token)
- **403**: Forbidden (insufficient permissions)
- **404**: Not Found
- **422**: Unprocessable Entity (data validation failed)
- **500**: Internal Server Error

## 📚 Additional Resources

- [API Endpoints](API_ENDPOINTS.md) - Complete endpoint reference
- [Testing Guide](tests/TESTING_README.md) - Comprehensive testing documentation
- [Database Schema](DATABASE_README.md) - Database design and migrations
- [Swagger Documentation](http://localhost:8000/docs) - Interactive API documentation

## 🚀 Getting Started

1. **Start the API**: `docker-compose up -d`
2. **Check Health**: `curl http://localhost:8000/api/v1/health`
3. **View Documentation**: Open `http://localhost:8000/docs` in your browser
4. **Run Examples**: Use the examples above to test the API
5. **Run Tests**: Execute the comprehensive test suite

---

**The Celuma API is designed with JSON-first architecture, providing comprehensive validation, type safety, and automatic documentation generation.**
