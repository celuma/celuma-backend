# Celuma API Usage Examples

This document provides comprehensive examples of how to use the Celuma API with JSON payloads.

## 🚀 JSON-First API Design

**All POST endpoints use JSON request bodies for optimal data handling and validation (except the image upload endpoint which uses multipart/form-data).**

### Benefits of JSON Payloads
- ✅ **Excellent Data Validation**: Pydantic schemas provide automatic validation
- ✅ **Type Safety**: Strong typing for all request and response data
- ✅ **Consistent API Design**: All endpoints follow the same pattern
- ✅ **Superior Developer Experience**: Clear data structure and validation errors
- ✅ **Auto-generated Documentation**: OpenAPI/Swagger documentation with examples

## 🔐 Authentication

Note: Except for `GET /`, `GET /health`, `GET /api/v1/health`, and `POST /api/v1/auth/*` (login/register), all endpoints require a Bearer token in the header:
```
Authorization: Bearer YOUR_ACCESS_TOKEN
```

### Unified Registration (tenant + branch + admin)
```bash
curl -X POST "http://localhost:8000/api/v1/auth/register/unified" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant": {
      "name": "Acme Labs",
      "legal_name": "Acme Laboratories S.A. de C.V.",
      "tax_id": "ACM010101ABC"
    },
    "branch": {
      "code": "HQ",
      "name": "Headquarters",
      "timezone": "America/Mexico_City",
      "address_line1": "Av. Siempre Viva 123",
      "city": "CDMX",
      "state": "CDMX",
      "postal_code": "01234",
      "country": "MX"
    },
    "admin_user": {
      "email": "admin@acme.com",
      "username": "admin",
      "password": "Secret123!",
      "full_name": "Admin User"
    }
  }'
```

**Notes:**
- Operation is atomic; on failure nothing is created.
- The created user has role `admin` and is linked to the created branch.
- `branch.code` must be unique per tenant.

**Python Example:**
```python
import requests

payload = {
    "tenant": {
        "name": "Acme Labs",
        "legal_name": "Acme Laboratories S.A. de C.V.",
        "tax_id": "ACM010101ABC",
    },
    "branch": {
        "code": "HQ",
        "name": "Headquarters",
        "timezone": "America/Mexico_City",
        "address_line1": "Av. Siempre Viva 123",
        "city": "CDMX",
        "state": "CDMX",
        "postal_code": "01234",
        "country": "MX",
    },
    "admin_user": {
        "email": "admin@acme.com",
        "username": "admin",
        "password": "Secret123!",
        "full_name": "Admin User",
    },
}

resp = requests.post(
    "http://localhost:8000/api/v1/auth/register/unified",
    json=payload,
)
resp.raise_for_status()
data = resp.json()
tenant_id = data["tenant_id"]
branch_id = data["branch_id"]
user_id = data["user_id"]
print("Tenant:", tenant_id)
print("Branch:", branch_id)
print("Admin user:", user_id)
```

### User Registration
```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "username": "johndoe",
    "password": "securepassword123",
    "full_name": "John Doe",
    "role": "admin",
    "tenant_id": "tenant-uuid-here"
  }'
```

**Notes:**
- `username` field is **optional** - you can omit it if you don't want a username
- If provided, username must be unique within the tenant
- Email is always required and must be unique within the tenant

**Python Example:**
```python
import requests

response = requests.post(
    "http://localhost:8000/api/v1/auth/register",
    json={
        "email": "user@example.com",
        "username": "johndoe",  # Optional field
        "password": "securepassword123",
        "full_name": "John Doe",
        "role": "admin",
        "tenant_id": "tenant-uuid-here"
    }
)

if response.status_code == 200:
    user_data = response.json()
    print(f"User created: {user_data['email']}")
    if user_data.get('username'):
        print(f"Username: {user_data['username']}")
```

### User Login (username or email)
```bash
# Login with username (single-tenant or selection flow)
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username_or_email": "johndoe",
    "password": "securepassword123"
  }'

# Login with email (same endpoint)
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username_or_email": "user@example.com",
    "password": "securepassword123"
  }'
```

**Behavior:**
- `username_or_email` can be a username or an email.
- If multiple tenants match, the API returns `need_tenant_selection` with `options`.
- Finalize by including `tenant_id` in the same endpoint.
- Successful login responses include `tenant_id`.

```bash
# Finalize login selecting a tenant
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username_or_email": "johndoe",
    "password": "securepassword123",
    "tenant_id": "tenant-uuid-here"
  }'
```

**Python Example:**
```python
import requests

login_resp = requests.post(
    "http://localhost:8000/api/v1/auth/login",
    json={
        "username_or_email": "johndoe",  # username or email
        "password": "securepassword123"
    }
)

login_resp.raise_for_status()
data = login_resp.json()

if data.get("need_tenant_selection"):
    chosen_tenant_id = data["options"][0]["tenant_id"]
    finalize_resp = requests.post(
        "http://localhost:8000/api/v1/auth/login",
        json={
            "username_or_email": "johndoe",
            "password": "securepassword123",
            "tenant_id": chosen_tenant_id
        }
    )
    finalize_resp.raise_for_status()
    data = finalize_resp.json()

access_token = data["access_token"]
tenant_id = data.get("tenant_id")
print(f"Login successful, token: {access_token}")
if tenant_id:
    print(f"Tenant: {tenant_id}")
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
    print(f"Email: {profile['email']}")
    if profile.get('username'):
        print(f"Username: {profile['username']}")
    else:
        print("No username set")
```

### Update Profile (PUT)
```bash
# Update profile details (name, username, email)
curl -X PUT "http://localhost:8000/api/v1/auth/me" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "full_name": "New Name",
    "username": "new-username",
    "email": "new.email@example.com"
  }'

# Change password (requires current_password and new_password)
curl -X PUT "http://localhost:8000/api/v1/auth/me" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "current_password": "oldpass",
    "new_password": "newpass123"
  }'
```

**Python Example:**
```python
import requests

headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

# Update profile fields
resp = requests.put(
    "http://localhost:8000/api/v1/auth/me",
    headers=headers,
    json={
        "full_name": "New Name",
        "username": "new-username",
        "email": "new.email@example.com",
    },
)
resp.raise_for_status()
updated = resp.json()
print("Updated name:", updated["full_name"])  # New Name

# Change password
pwd_resp = requests.put(
    "http://localhost:8000/api/v1/auth/me",
    headers=headers,
    json={
        "current_password": "oldpass",
        "new_password": "newpass123",
    },
)
pwd_resp.raise_for_status()
print("Password changed successfully")
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
    "timezone": "America/Mexico_City",
    "address_line1": "Av. Reforma 123",
    "address_line2": "Piso 4",
    "city": "Mexico City",
    "state": "CDMX",
    "postal_code": "06000",
    "country": "MX",
    "is_active": true
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
        "timezone": "America/Mexico_City",
        "address_line1": "Av. Reforma 123",
        "address_line2": "Piso 4",
        "city": "Mexico City",
        "state": "CDMX",
        "postal_code": "06000",
        "country": "MX",
        "is_active": True
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

### List All Patients (full profile)
```bash
curl http://localhost:8000/api/v1/patients/
```

**Python Example:**
```python
response = requests.get("http://localhost:8000/api/v1/patients/")
if response.status_code == 200:
    patients = response.json()
    for patient in patients:
        print(
            f"- {patient['first_name']} {patient['last_name']} | code={patient['patient_code']} | phone={patient.get('phone')}"
        )
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
    "notes": "Complete blood count requested",
    "created_by": "user-uuid-here"
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
        "notes": "Complete blood count requested",
        "created_by": user_id
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
    "notes": "Blood sample for CBC",
    "collected_at": "2025-08-18T10:00:00Z",
    "received_at": "2025-08-18T11:00:00Z"
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
        "notes": "Blood sample for CBC",
        "collected_at": "2025-08-18T10:00:00Z",
        "received_at": "2025-08-18T11:00:00Z"
    }
)

if response.status_code == 200:
    sample = response.json()
    sample_id = sample['id']
    print(f"Sample created: {sample['sample_code']}")
```

### Create Order with Samples (Unified)
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/orders/unified" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "TENANT_UUID",
    "branch_id": "BRANCH_UUID",
    "patient_id": "PATIENT_UUID",
    "order_code": "ORD001",
    "requested_by": "Dr. Smith",
    "samples": [
      { "sample_code": "SAMP001", "type": "SANGRE" },
      { "sample_code": "SAMP002", "type": "TEJIDO", "notes": "Fragment" }
    ]
  }'
```

```python
import requests

payload = {
    "tenant_id": tenant_id,
    "branch_id": branch_id,
    "patient_id": patient_id,
    "order_code": "ORD001",
    "requested_by": "Dr. Smith",
    "samples": [
        {"sample_code": "SAMP001", "type": "SANGRE"},
        {"sample_code": "SAMP002", "type": "TEJIDO", "notes": "Fragment"},
    ],
}

resp = requests.post(
    f"{BASE_URL}/api/v1/laboratory/orders/unified",
    json=payload,
)
resp.raise_for_status()
data = resp.json()
print("Order:", data["order"]["order_code"], "Samples:", len(data["samples"]))
```

### Get Full Order Detail (order + patient full + samples)
```bash
ORDER_ID=order-uuid-here
curl "http://localhost:8000/api/v1/laboratory/orders/$ORDER_ID/full"
```

```python
import requests

order_id = "ORDER_UUID"
resp = requests.get(f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/full")
resp.raise_for_status()
full = resp.json()
print("Order:", full["order"]["order_code"], "Patient:", full["patient"]["first_name"]) 
print("Samples:", [s["sample_code"] for s in full["samples"]])
```

### List Cases for a Patient (includes patient full profile)
```bash
PATIENT_ID=patient-uuid-here
curl "http://localhost:8000/api/v1/laboratory/patients/$PATIENT_ID/cases"
```

```python
import requests

patient_id = "PATIENT_UUID"
resp = requests.get(f"{BASE_URL}/api/v1/laboratory/patients/{patient_id}/cases")
resp.raise_for_status()
data = resp.json()
print("Patient:", data["patient"]["first_name"], data["patient"]["last_name"], "code:", data["patient"]["patient_code"]) 
cases = data["cases"]
for case in cases:
    order = case["order"]
    report = case.get("report")
    print("Order:", order["order_code"], "samples:", len(case["samples"]), "has_report:", bool(report))

### List Orders for a Patient (summary + patient full)
```bash
PATIENT_ID=patient-uuid-here
curl "http://localhost:8000/api/v1/laboratory/patients/$PATIENT_ID/orders"
```

### List All Orders (enriched)
```bash
curl "http://localhost:8000/api/v1/laboratory/orders/"
```

```python
import requests

resp = requests.get(f"{BASE_URL}/api/v1/laboratory/orders/")
resp.raise_for_status()
data = resp.json()["orders"]
for o in data:
    print(o["order_code"], "branch:", o["branch"]["name"], "patient:", o["patient"]["full_name"], "samples:", o["sample_count"]) 
```

### List All Samples (enriched)
```bash
curl "http://localhost:8000/api/v1/laboratory/samples/"
```

```python
import requests

resp = requests.get(f"{BASE_URL}/api/v1/laboratory/samples/")
resp.raise_for_status()
samples = resp.json()["samples"]
for s in samples:
    print(
        s["sample_code"],
        "order:", s["order"]["order_code"],
        "branch:", s["branch"]["name"],
        "patient:", s["order"]["patient"]["full_name"] if s["order"].get("patient") else None,
        "requested_by:", s["order"].get("requested_by"),
    ) 
```

### Get Sample Detail (enriched)
```bash
SAMPLE_ID=sample-uuid-here
curl "http://localhost:8000/api/v1/laboratory/samples/$SAMPLE_ID"
```

```python
import requests

sample_id = "SAMPLE_UUID"
resp = requests.get(f"{BASE_URL}/api/v1/laboratory/samples/{sample_id}")
resp.raise_for_status()
detail = resp.json()
print("Sample:", detail["sample_code"], "Order:", detail["order"]["order_code"], "Patient:", detail["patient"]["full_name"]) 
```
```python
import requests

patient_id = "PATIENT_UUID"
resp = requests.get(f"{BASE_URL}/api/v1/laboratory/patients/{patient_id}/orders")
resp.raise_for_status()
data = resp.json()
print("Patient:", data["patient"]["first_name"], data["patient"]["last_name"], data["patient"]["patient_code"]) 
orders = data["orders"]
for o in orders:
    print(o["order_code"], "samples:", o["sample_count"], "has_report:", o["has_report"]) 
```
```

### Upload Sample Image (multipart/form-data)
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/samples/SAMPLE_UUID/images" \
  -F "file=@/path/to/image.dng"
```

Notes:
- Regular images (JPG/PNG/WebP, etc.) up to 50MB.
- RAW formats (`.cr2`, `.cr3`, `.nef`, `.nrw`, `.arw`, `.sr2`, `.raf`, `.rw2`, `.orf`, `.pef`, `.dng`) up to 500MB.
- If the limit is exceeded, the API returns `413`.

**Python Example:**
```python
import requests

sample_id = "SAMPLE_UUID"
filepath = "/path/to/image.dng"

with open(filepath, "rb") as f:
    resp = requests.post(
        f"http://localhost:8000/api/v1/laboratory/samples/{sample_id}/images",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": (filepath.split("/")[-1], f, "application/octet-stream")},
    )

resp.raise_for_status()
data = resp.json()
print("Uploaded:", data["filename"], "Processed URL:", data["urls"].get("processed"))
```

### List Sample Images
```bash
curl "http://localhost:8000/api/v1/laboratory/samples/SAMPLE_UUID/images" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
resp = requests.get(
    f"http://localhost:8000/api/v1/laboratory/samples/{sample_id}/images",
)
resp.raise_for_status()
images = resp.json()["images"]
for img in images:
    print("Thumb:", img["urls"].get("thumbnail"), "Processed:", img["urls"].get("processed"))
```

## 📋 Report Management

### Create Report (with JSON body stored in S3)
```bash
curl -X POST "http://localhost:8000/api/v1/reports/" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "order_id": "order-uuid-here",
    "title": "Blood Test Report",
    "diagnosis_text": "Normal blood count results",
    "published_at": "2025-08-18T12:00:00Z",
    "created_by": "user-uuid-here",
    "report": {
      "tipo": "citologia_mamaria",
      "base": { "paciente": "Jane Doe" },
      "secciones": { "interpretacion": "..." },
      "flags": { "incluirDiagnostico": true },
      "images": []
    }
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
        "diagnosis_text": "Normal blood count results",
        "published_at": "2025-08-18T12:00:00Z",
        "created_by": user_id
    }
)

### Create New Report Version (JSON body uploaded to S3)
```bash
REPORT_ID=report-uuid-here
curl -X POST "http://localhost:8000/api/v1/reports/$REPORT_ID/new_version" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here",
    "order_id": "order-uuid-here",
    "title": "Blood Test Report",
    "diagnosis_text": "Normal blood count results",
    "published_at": "2025-08-18T12:00:00Z",
    "created_by": "user-uuid-here",
    "report": { "tipo": "citologia_mamaria", "base": {}, "secciones": {}, "flags": {}, "images": [] }
  }'
```

**Python Example:**
```python
response = requests.post(
    "http://localhost:8000/api/v1/reports/versions/",
    json={
        "report_id": report_id,
        "version_no": 1,
        "pdf_storage_id": pdf_id,
        "html_storage_id": html_id,
        "changelog": "Initial report version",
        "authored_by": user_id,
        "authored_at": "2025-08-18T12:30:00Z"
    }
)
```

if response.status_code == 200:
    report = response.json()
    report_id = report['id']
    print(f"Report created: {report['title']}")
```

### List All Reports (enriched)
```bash
curl "http://localhost:8000/api/v1/reports/" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response includes enriched branch, order, patient, and version information:**
```json
{
  "reports": [
    {
      "id": "report-uuid",
      "status": "PUBLISHED",
      "tenant_id": "tenant-uuid",
      "branch": {
        "id": "branch-uuid",
        "name": "Main Branch",
        "code": "MAIN"
      },
      "order": {
        "id": "order-uuid",
        "order_code": "ORD001",
        "status": "COMPLETED",
        "requested_by": "Dr. Smith",
        "patient": {
          "id": "patient-uuid",
          "full_name": "John Doe",
          "patient_code": "P001"
        }
      },
      "title": "Blood Test Report",
      "diagnosis_text": "Normal blood count results",
      "published_at": "2025-08-18T12:00:00Z",
      "created_at": "2025-08-18T10:00:00Z",
      "created_by": "user-uuid",
      "version_no": 2,
      "has_pdf": true
    }
  ]
}
```

**Python Example:**
```python
import requests

resp = requests.get(
    f"{BASE_URL}/api/v1/reports/",
    headers={"Authorization": f"Bearer {access_token}"}
)
resp.raise_for_status()
data = resp.json()["reports"]
for r in data:
    print(
        f"Report: {r['title']} | Status: {r['status']} | "
        f"Order: {r['order']['order_code']} | "
        f"Patient: {r['order']['patient']['full_name']} | "
        f"Branch: {r['branch']['name']} | "
        f"Version: {r['version_no']} | "
        f"Has PDF: {r['has_pdf']}"
    )
```

### Get Report Details (includes JSON body from S3 of current version)
```bash
curl http://localhost:8000/api/v1/reports/REPORT_UUID
```
Expected response contains the `report` field reconstructed from S3 for the current version.

### List Versions for a Report
```bash
curl http://localhost:8000/api/v1/reports/REPORT_UUID/versions | jq .
```

### Get Specific Report Version
```bash
curl http://localhost:8000/api/v1/reports/REPORT_UUID/2 | jq .
```

### Upload Report PDF to Specific Version (multipart/form-data)
```bash
REPORT_ID=report-uuid-here
VERSION_NO=2
PDF_PATH=/path/to/report.pdf
curl -X POST "http://localhost:8000/api/v1/reports/$REPORT_ID/versions/$VERSION_NO/pdf" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "file=@${PDF_PATH};type=application/pdf"
```

Notes:
- PDF size up to 50MB.
- If the limit is exceeded, the API returns `413`.

**Python Example:**
```python
import requests

report_id = "REPORT_UUID"
version_no = 2
pdf_path = "/path/to/report.pdf"

with open(pdf_path, "rb") as f:
    resp = requests.post(
        f"http://localhost:8000/api/v1/reports/{report_id}/versions/{version_no}/pdf",
        files={"file": (pdf_path.split("/")[-1], f, "application/pdf")},
    )

resp.raise_for_status()
print(resp.json())
```

### Upload Report PDF to Newest Version (multipart/form-data)
```bash
REPORT_ID=report-uuid-here
PDF_PATH=/path/to/report.pdf
curl -X POST "http://localhost:8000/api/v1/reports/$REPORT_ID/pdf" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "file=@${PDF_PATH};type=application/pdf"
```

Notes:
- PDF size up to 50MB.
- If the limit is exceeded, the API returns `413`.

**Python Example:**
```python
import requests

report_id = "REPORT_UUID"
pdf_path = "/path/to/report.pdf"

with open(pdf_path, "rb") as f:
    resp = requests.post(
        f"http://localhost:8000/api/v1/reports/{report_id}/pdf",
        files={"file": (pdf_path.split("/")[-1], f, "application/pdf")},
    )

resp.raise_for_status()
print(resp.json())
```

### Get PDF (presigned URL) for Specific Version
```bash
REPORT_ID=report-uuid-here
VERSION_NO=2
curl "http://localhost:8000/api/v1/reports/$REPORT_ID/versions/$VERSION_NO/pdf" | jq .
```

**Python Example:**
```python
import requests

report_id = "REPORT_UUID"
version_no = 2
resp = requests.get(f"http://localhost:8000/api/v1/reports/{report_id}/versions/{version_no}/pdf")
resp.raise_for_status()
data = resp.json()
print("Presigned URL:", data["pdf_url"])  # Use this URL to download the PDF
```

### Get PDF (presigned URL) for Newest Version
```bash
REPORT_ID=report-uuid-here
curl "http://localhost:8000/api/v1/reports/$REPORT_ID/pdf" | jq .
```

**Python Example:**
```python
import requests

report_id = "REPORT_UUID"
resp = requests.get(f"http://localhost:8000/api/v1/reports/{report_id}/pdf")
resp.raise_for_status()
data = resp.json()
print("Presigned URL:", data["pdf_url"])  # Use this URL to download the PDF
```


**Python Example:**
```python
response = requests.get(
    "http://localhost:8000/api/v1/reports/",
    headers={"Authorization": f"Bearer {access_token}"}
)
if response.status_code == 200:
    data = response.json()
    reports = data['reports']
    for report in reports:
        patient_name = report['order']['patient']['full_name'] if report['order'].get('patient') else 'N/A'
        print(
            f"- {report['title']} | Status: {report['status']} | "
            f"Patient: {patient_name} | Order: {report['order']['order_code']} | "
            f"Version: {report['version_no']} | Has PDF: {report['has_pdf']}"
        )
```

## 📊 Dashboard Management

### Get Dashboard Statistics
```bash
curl "http://localhost:8000/api/v1/dashboard/" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
import requests

headers = {"Authorization": f"Bearer {access_token}"}
response = requests.get(
    "http://localhost:8000/api/v1/dashboard/",
    headers=headers
)

if response.status_code == 200:
    dashboard_data = response.json()
    stats = dashboard_data['stats']
    print(f"Total patients: {stats['total_patients']}")
    print(f"Total orders: {stats['total_orders']}")
    print(f"Pending orders: {stats['pending_orders']}")
    print(f"Published reports: {stats['published_reports']}")
    
    print("\nRecent Activity:")
    for activity in dashboard_data['recent_activity']:
        print(f"- {activity['title']}: {activity['description']}")
```

**Response:**
```json
{
  "stats": {
    "total_patients": 150,
    "total_orders": 89,
    "total_samples": 234,
    "total_reports": 67,
    "pending_orders": 12,
    "draft_reports": 8,
    "published_reports": 59
  },
  "recent_activity": [
    {
      "id": "order-uuid",
      "title": "Orden ORD001",
      "description": "Paciente: Juan Pérez • Solicitado por: Dr. García",
      "timestamp": "2025-01-16T10:30:00Z",
      "type": "order",
      "status": "RECEIVED"
    },
    {
      "id": "report-uuid",
      "title": "Citología Mamaria",
      "description": "Orden: ORD002 • Paciente: María López",
      "timestamp": "2025-01-16T09:15:00Z",
      "type": "report",
      "status": "PUBLISHED"
    }
  ]
}
```

**Notes:**
- Returns aggregated statistics for the current tenant
- Recent activity includes the 8 most recent items across orders, reports, and samples
- Optimized endpoint that combines multiple queries for better performance

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
    "currency": "MXN",
    "issued_at": "2025-08-18T01:50:51.386774"
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
        "currency": "MXN",
        "issued_at": "2025-08-18T01:50:51.386774"
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
    "method": "credit_card",
    "paid_at": "2025-08-18T02:10:00Z"
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
        "method": "credit_card",
        "paid_at": "2025-08-18T02:10:00Z"
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
