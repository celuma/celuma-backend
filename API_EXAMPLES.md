# Celuma API - Usage Examples and Patterns

This document provides practical examples of how to use the Celuma API for common laboratory management tasks.

## 🚀 Getting Started

### 1. Health Check
```bash
curl http://localhost:8000/api/v1/health
```

### 2. System Information
```bash
curl http://localhost:8000/
```

## 🔐 Authentication Examples

### User Registration
```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "email=admin@lab.com&password=secure123&full_name=Admin%20User&role=admin&tenant_id=your-tenant-uuid"
```

### User Login
```bash
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "email=admin@lab.com&password=secure123&tenant_id=your-tenant-uuid"
```

### Using Authentication Token
```bash
# Store the token from login response
TOKEN="your_jwt_token_here"

# Use the token for authenticated requests
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/auth/me
```

## 🏢 Tenant Management Examples

### Create a New Tenant
```bash
curl -X POST "http://localhost:8000/api/v1/tenants/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "name=Acme%20Laboratory&legal_name=Acme%20Lab%20Corp&tax_id=123456789"
```

### List All Tenants
```bash
curl http://localhost:8000/api/v1/tenants/
```

### Get Tenant Details
```bash
curl http://localhost:8000/api/v1/tenants/tenant-uuid-here
```

## 🏥 Branch Management Examples

### Create a New Branch
```bash
curl -X POST "http://localhost:8000/api/v1/branches/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=tenant-uuid&code=MAIN&name=Main%20Branch&city=Mexico%20City&state=CDMX&country=MX"
```

### List All Branches
```bash
curl http://localhost:8000/api/v1/branches/
```

### Get Branch Details
```bash
curl http://localhost:8000/api/v1/branches/branch-uuid-here
```

### List Branches for a Specific Tenant
```bash
curl http://localhost:8000/api/v1/tenants/tenant-uuid/branches
```

## 👥 Patient Management Examples

### Create a New Patient
```bash
curl -X POST "http://localhost:8000/api/v1/patients/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=tenant-uuid&branch_id=branch-uuid&patient_code=P001&first_name=John&last_name=Doe&dob=1990-01-01&sex=M&phone=555-1234&email=john.doe@example.com"
```

### List All Patients
```bash
curl http://localhost:8000/api/v1/patients/
```

### Get Patient Details
```bash
curl http://localhost:8000/api/v1/patients/patient-uuid-here
```

## 🧪 Laboratory Management Examples

### Create a Laboratory Order
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/orders/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=tenant-uuid&branch_id=branch-uuid&patient_id=patient-uuid&order_code=ORD001&requested_by=Dr.%20Smith&notes=Complete%20blood%20count%20requested"
```

### List All Orders
```bash
curl http://localhost:8000/api/v1/laboratory/orders/
```

### Get Order Details
```bash
curl http://localhost:8000/api/v1/laboratory/orders/order-uuid-here
```

### Create a Sample
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/samples/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=tenant-uuid&branch_id=branch-uuid&order_id=order-uuid&sample_code=SAMP001&type=SANGRE&notes=Blood%20sample%20for%20CBC"
```

### List All Samples
```bash
curl http://localhost:8000/api/v1/laboratory/samples/
```

## 📋 Report Management Examples

### Create a Report
```bash
curl -X POST "http://localhost:8000/api/v1/reports/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=tenant-uuid&branch_id=branch-uuid&order_id=order-uuid&title=Blood%20Test%20Report&diagnosis_text=Normal%20blood%20count%20results"
```

### List All Reports
```bash
curl http://localhost:8000/api/v1/reports/
```

### Get Report Details
```bash
curl http://localhost:8000/api/v1/reports/report-uuid-here
```

### Create Report Version
```bash
curl -X POST "http://localhost:8000/api/v1/reports/versions/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "report_id=report-uuid&version_no=1&pdf_storage_id=storage-uuid&changelog=Initial%20report%20version"
```

## 💰 Billing Management Examples

### Create an Invoice
```bash
curl -X POST "http://localhost:8000/api/v1/billing/invoices/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=tenant-uuid&branch_id=branch-uuid&order_id=order-uuid&invoice_number=INV001&amount_total=1500.00&currency=MXN"
```

### List All Invoices
```bash
curl http://localhost:8000/api/v1/billing/invoices/
```

### Get Invoice Details
```bash
curl http://localhost:8000/api/v1/billing/invoices/invoice-uuid-here
```

### Create a Payment
```bash
curl -X POST "http://localhost:8000/api/v1/billing/payments/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=tenant-uuid&branch_id=branch-uuid&invoice_id=invoice-uuid&amount_paid=1500.00&method=credit_card"
```

### List All Payments
```bash
curl http://localhost:8000/api/v1/billing/payments/
```

## 🔄 Complete Workflow Examples

### Complete Laboratory Process Flow

#### Step 1: Create Tenant and Branch
```bash
# Create tenant
TENANT_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/tenants/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "name=Test%20Lab&legal_name=Test%20Laboratory%20Inc&tax_id=987654321")

TENANT_ID=$(echo $TENANT_RESPONSE | jq -r '.id')

# Create branch
BRANCH_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/branches/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=$TENANT_ID&code=MAIN&name=Main%20Branch&city=Mexico%20City&state=CDMX&country=MX")

BRANCH_ID=$(echo $BRANCH_RESPONSE | jq -r '.id')
```

#### Step 2: Create Patient
```bash
PATIENT_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/patients/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=$TENANT_ID&branch_id=$BRANCH_ID&patient_code=P001&first_name=Jane&last_name=Smith&dob=1985-05-15&sex=F&phone=555-9876&email=jane.smith@example.com")

PATIENT_ID=$(echo $PATIENT_RESPONSE | jq -r '.id')
```

#### Step 3: Create Laboratory Order
```bash
ORDER_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/laboratory/orders/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=$TENANT_ID&branch_id=$BRANCH_ID&patient_id=$PATIENT_ID&order_code=ORD001&requested_by=Dr.%20Johnson&notes=Complete%20blood%20count%20and%20chemistry%20panel")

ORDER_ID=$(echo $ORDER_RESPONSE | jq -r '.id')
```

#### Step 4: Create Sample
```bash
SAMPLE_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/laboratory/samples/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=$TENANT_ID&branch_id=$BRANCH_ID&order_id=$ORDER_ID&sample_code=SAMP001&type=SANGRE&notes=Blood%20sample%20for%20CBC%20and%20chemistry")

SAMPLE_ID=$(echo $SAMPLE_RESPONSE | jq -r '.id')
```

#### Step 5: Create Report
```bash
REPORT_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/reports/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=$TENANT_ID&branch_id=$BRANCH_ID&order_id=$ORDER_ID&title=Complete%20Blood%20Count%20Report&diagnosis_text=Normal%20CBC%20results%2C%20all%20parameters%20within%20reference%20range")

REPORT_ID=$(echo $REPORT_RESPONSE | jq -r '.id')
```

#### Step 6: Create Invoice and Payment
```bash
# Create invoice
INVOICE_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/billing/invoices/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=$TENANT_ID&branch_id=$BRANCH_ID&order_id=$ORDER_ID&invoice_number=INV001&amount_total=2500.00&currency=MXN")

INVOICE_ID=$(echo $INVOICE_RESPONSE | jq -r '.id')

# Create payment
PAYMENT_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/billing/payments/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "tenant_id=$TENANT_ID&branch_id=$BRANCH_ID&invoice_id=$INVOICE_ID&amount_paid=2500.00&method=credit_card")

PAYMENT_ID=$(echo $PAYMENT_RESPONSE | jq -r '.id')
```

#### Step 7: Verify Complete Process
```bash
echo "Complete laboratory process created:"
echo "Tenant ID: $TENANT_ID"
echo "Branch ID: $BRANCH_ID"
echo "Patient ID: $PATIENT_ID"
echo "Order ID: $ORDER_ID"
echo "Sample ID: $SAMPLE_ID"
echo "Report ID: $REPORT_ID"
echo "Invoice ID: $INVOICE_ID"
echo "Payment ID: $PAYMENT_ID"
```

## 🐍 Python Examples

### Basic API Client
```python
import requests
import json

class CelumaAPI:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.token = None
    
    def login(self, email, password, tenant_id):
        """Authenticate user and get token"""
        response = requests.post(f"{self.base_url}/api/v1/auth/login", 
                               params={"email": email, "password": password, "tenant_id": tenant_id})
        if response.status_code == 200:
            self.token = response.json()["access_token"]
            return True
        return False
    
    def get_headers(self):
        """Get headers with authentication token"""
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}
    
    def create_tenant(self, name, legal_name=None, tax_id=None):
        """Create a new tenant"""
        data = {"name": name}
        if legal_name:
            data["legal_name"] = legal_name
        if tax_id:
            data["tax_id"] = tax_id
        
        response = requests.post(f"{self.base_url}/api/v1/tenants/", params=data)
        return response.json() if response.status_code == 200 else None
    
    def create_branch(self, tenant_id, code, name, city=None, state=None):
        """Create a new branch"""
        data = {
            "tenant_id": tenant_id,
            "code": code,
            "name": name
        }
        if city:
            data["city"] = city
        if state:
            data["state"] = state
        
        response = requests.post(f"{self.base_url}/api/v1/branches/", params=data)
        return response.json() if response.status_code == 200 else None

# Usage example
api = CelumaAPI()

# Create tenant and branch
tenant = api.create_tenant("Test Lab", "Test Laboratory Inc", "123456789")
if tenant:
    branch = api.create_branch(tenant["id"], "MAIN", "Main Branch", "Mexico City", "CDMX")
    print(f"Created tenant: {tenant['name']}")
    print(f"Created branch: {branch['name']}")
```

### Complete Workflow in Python
```python
def create_complete_laboratory_workflow(api, lab_name, patient_name):
    """Create a complete laboratory workflow"""
    
    # Step 1: Create tenant
    tenant = api.create_tenant(lab_name, f"{lab_name} Inc", "123456789")
    if not tenant:
        print("Failed to create tenant")
        return None
    
    # Step 2: Create branch
    branch = api.create_branch(tenant["id"], "MAIN", "Main Branch", "Mexico City", "CDMX")
    if not branch:
        print("Failed to create branch")
        return None
    
    # Step 3: Create patient
    patient_data = {
        "tenant_id": tenant["id"],
        "branch_id": branch["id"],
        "patient_code": "P001",
        "first_name": patient_name.split()[0],
        "last_name": patient_name.split()[1],
        "dob": "1990-01-01",
        "sex": "M",
        "phone": "555-1234",
        "email": f"{patient_name.lower().replace(' ', '.')}@example.com"
    }
    
    patient = api.create_patient(**patient_data)
    if not patient:
        print("Failed to create patient")
        return None
    
    print(f"✅ Complete workflow created for {lab_name}")
    print(f"   Tenant: {tenant['name']}")
    print(f"   Branch: {branch['name']}")
    print(f"   Patient: {patient['first_name']} {patient['last_name']}")
    
    return {
        "tenant": tenant,
        "branch": branch,
        "patient": patient
    }

# Usage
api = CelumaAPI()
workflow = create_complete_laboratory_workflow(api, "Acme Lab", "John Doe")
```

## 🔧 Error Handling Examples

### Handle Validation Errors
```python
def create_tenant_with_error_handling(api, name, legal_name=None):
    """Create tenant with proper error handling"""
    try:
        response = requests.post(f"{api.base_url}/api/v1/tenants/", 
                               params={"name": name, "legal_name": legal_name})
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 422:
            errors = response.json()["detail"]
            print("Validation errors:")
            for error in errors:
                print(f"  - {error['loc'][-1]}: {error['msg']}")
        else:
            print(f"Error: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
    
    return None
```

### Handle Authentication Errors
```python
def authenticated_request(api, endpoint, method="GET", **kwargs):
    """Make authenticated request with error handling"""
    if not api.token:
        print("Not authenticated. Please login first.")
        return None
    
    headers = api.get_headers()
    try:
        if method.upper() == "GET":
            response = requests.get(f"{api.base_url}{endpoint}", headers=headers)
        elif method.upper() == "POST":
            response = requests.post(f"{api.base_url}{endpoint}", headers=headers, **kwargs)
        
        if response.status_code == 401:
            print("Authentication failed. Token may have expired.")
            api.token = None
            return None
        elif response.status_code == 200:
            return response.json()
        else:
            print(f"Request failed: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
    
    return None
```

## 📊 Data Validation Examples

### Validate Required Fields
```python
def validate_tenant_data(name, legal_name=None, tax_id=None):
    """Validate tenant data before API call"""
    errors = []
    
    if not name or len(name.strip()) == 0:
        errors.append("Tenant name is required")
    elif len(name) > 255:
        errors.append("Tenant name must be less than 255 characters")
    
    if legal_name and len(legal_name) > 500:
        errors.append("Legal name must be less than 500 characters")
    
    if tax_id and len(tax_id) > 50:
        errors.append("Tax ID must be less than 50 characters")
    
    return errors

# Usage
errors = validate_tenant_data("", "Very long legal name that exceeds the limit", "123")
if errors:
    print("Validation errors:")
    for error in errors:
        print(f"  - {error}")
else:
    print("Data is valid")
```

## 🚀 Performance Testing Examples

### Load Testing
```python
import time
import concurrent.futures

def test_endpoint_performance(api, endpoint, iterations=100):
    """Test endpoint performance"""
    times = []
    
    for i in range(iterations):
        start_time = time.time()
        response = requests.get(f"{api.base_url}{endpoint}")
        end_time = time.time()
        
        if response.status_code == 200:
            times.append((end_time - start_time) * 1000)  # Convert to milliseconds
        
        # Small delay to avoid overwhelming the API
        time.sleep(0.1)
    
    if times:
        avg_time = sum(times) / len(times)
        min_time = min(times)
        max_time = max(times)
        
        print(f"Performance results for {endpoint}:")
        print(f"  Average: {avg_time:.2f} ms")
        print(f"  Min: {min_time:.2f} ms")
        print(f"  Max: {max_time:.2f} ms")
        print(f"  Successful requests: {len(times)}/{iterations}")
    
    return times

# Test multiple endpoints concurrently
def test_all_endpoints_performance(api):
    """Test performance of all main endpoints"""
    endpoints = [
        "/api/v1/health",
        "/api/v1/tenants/",
        "/api/v1/branches/",
        "/api/v1/patients/"
    ]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(test_endpoint_performance, api, endpoint): endpoint 
                  for endpoint in endpoints}
        
        for future in concurrent.futures.as_completed(futures):
            endpoint = futures[future]
            try:
                result = future.result()
                print(f"Completed performance test for {endpoint}")
            except Exception as e:
                print(f"Performance test failed for {endpoint}: {e}")
```

## 📝 Best Practices

### 1. Always Handle Errors
```python
# Good
try:
    response = api.create_tenant("Lab Name")
    if response:
        print("Tenant created successfully")
    else:
        print("Failed to create tenant")
except Exception as e:
    print(f"Error: {e}")

# Bad
response = api.create_tenant("Lab Name")
print("Tenant created successfully")  # May fail silently
```

### 2. Use Environment Variables
```python
import os

API_BASE_URL = os.getenv("CELUMA_API_URL", "http://localhost:8000")
API_USERNAME = os.getenv("CELUMA_USERNAME")
API_PASSWORD = os.getenv("CELUMA_PASSWORD")
```

### 3. Implement Retry Logic
```python
import time
from functools import wraps

def retry_on_failure(max_retries=3, delay=1):
    """Decorator to retry failed requests"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    print(f"Attempt {attempt + 1} failed, retrying in {delay} seconds...")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

@retry_on_failure(max_retries=3, delay=2)
def create_tenant_with_retry(api, name):
    """Create tenant with automatic retry on failure"""
    return api.create_tenant(name)
```

### 4. Log All API Calls
```python
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def log_api_call(func):
    """Decorator to log API calls"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger.info(f"Calling {func.__name__} with args: {args}, kwargs: {kwargs}")
        try:
            result = func(*args, **kwargs)
            logger.info(f"API call {func.__name__} successful")
            return result
        except Exception as e:
            logger.error(f"API call {func.__name__} failed: {e}")
            raise
    return wrapper
```

These examples provide a comprehensive guide to using the Celuma API effectively. Remember to always handle errors gracefully, implement proper authentication, and follow the established patterns for consistent API usage.
