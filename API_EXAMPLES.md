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

## 🛒 Service Catalog Management

### Create Service Catalog Item
```bash
curl -X POST "http://localhost:8000/api/v1/billing/catalog" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "service_name": "Biopsy Analysis",
    "service_code": "BIO-001",
    "description": "Complete biopsy analysis service",
    "price": 1500.00,
    "currency": "MXN",
    "is_active": true,
    "valid_from": "2025-01-01T00:00:00Z"
  }'
```

**Python Example:**
```python
service_response = requests.post(
    f"{BASE_URL}/api/v1/billing/catalog",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "service_name": "Biopsy Analysis",
        "service_code": "BIO-001",
        "description": "Complete biopsy analysis service",
        "price": 1500.00,
        "currency": "MXN",
        "is_active": True,
        "valid_from": "2025-01-01T00:00:00Z"
    }
)
service = service_response.json()
print(f"✅ Service created: {service['service_name']}")
```

### List Service Catalog
```bash
curl "http://localhost:8000/api/v1/billing/catalog" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

### Update Service Catalog Item
```bash
curl -X PUT "http://localhost:8000/api/v1/billing/catalog/{service_id}" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "price": 1800.00,
    "is_active": false
  }'
```

## 📄 Invoice Line Items

### Add Item to Invoice
```bash
curl -X POST "http://localhost:8000/api/v1/billing/invoices/{invoice_id}/items" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "service_id": "service-uuid",
    "description": "Biopsy Analysis",
    "quantity": 1,
    "unit_price": 1500.00
  }'
```

**Python Example:**
```python
item_response = requests.post(
    f"{BASE_URL}/api/v1/billing/invoices/{invoice_id}/items",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "service_id": service_id,
        "description": "Biopsy Analysis",
        "quantity": 1,
        "unit_price": 1500.00
    }
)
item = item_response.json()
print(f"✅ Invoice item added: {item['description']}")
```

### List Invoice Items
```bash
curl "http://localhost:8000/api/v1/billing/invoices/{invoice_id}/items" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

## 👥 User Invitations and Portal

### Send User Invitation
```bash
curl -X POST "http://localhost:8000/api/v1/portal/invite" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "email": "newuser@example.com",
    "full_name": "New User",
    "role": "lab_tech"
  }'
```

**Python Example:**
```python
invitation_response = requests.post(
    f"{BASE_URL}/api/v1/portal/invite",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "email": "newuser@example.com",
        "full_name": "New User",
        "role": "lab_tech"
    }
)
invitation = invitation_response.json()
print(f"✅ Invitation sent to: {invitation['email']}")
print(f"   Token: {invitation['token']}")
```

### Accept Invitation
```bash
curl -X POST "http://localhost:8000/api/v1/portal/accept-invitation" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "invitation-token",
    "password": "SecurePassword123!"
  }'
```

### Request Password Reset
```bash
curl -X POST "http://localhost:8000/api/v1/portal/request-password-reset" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com"
  }'
```

### Reset Password
```bash
curl -X POST "http://localhost:8000/api/v1/portal/reset-password" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "reset-token",
    "new_password": "NewSecurePassword123!"
  }'
```

## 📋 Event Timeline

### Get Order Events
```bash
curl "http://localhost:8000/api/v1/laboratory/orders/{order_id}/events" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
events_response = requests.get(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/events",
    headers={"Authorization": f"Bearer {token}"}
)
events = events_response.json()
print(f"📋 Timeline has {len(events)} events:")
for event in events:
    print(f"  - {event['event_type']}: {event['description']}")
```

### Add Event to Timeline
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/orders/{order_id}/events" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "event_type": "STATUS_CHANGED",
    "description": "Order status changed to PROCESSING",
    "event_metadata": {
      "old_status": "RECEIVED",
      "new_status": "PROCESSING",
      "reason": "Sample preparation started"
    }
  }'
```

**Python Example:**
```python
event_response = requests.post(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/events",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "event_type": "STATUS_CHANGED",
        "description": "Order status changed to PROCESSING",
        "event_metadata": {
            "old_status": "RECEIVED",
            "new_status": "PROCESSING",
            "reason": "Sample preparation started"
        }
    }
)
event = event_response.json()
print(f"✅ Event added: {event['event_type']}")
```

## 👤 User Management (Admin Only)

### Create User
```bash
curl -X POST "http://localhost:8000/api/v1/users/" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "newuser@example.com",
    "username": "newuser",
    "password": "SecurePass123!",
    "full_name": "New User",
    "role": "lab_tech",
    "branch_ids": ["branch-uuid-1", "branch-uuid-2"]
  }'
```

**Python Example:**
```python
# Create a new user
user_data = {
    "email": "labtech@example.com",
    "username": "labtech1",
    "password": "SecurePass123!",
    "full_name": "Lab Technician 1",
    "role": "lab_tech",
    "branch_ids": ["branch-uuid-1"]
}

user_response = requests.post(
    f"{BASE_URL}/api/v1/users/",
    headers=headers,
    json=user_data,
)
user_response.raise_for_status()
user = user_response.json()
print(f"✅ User created: {user['email']} (ID: {user['id']})")
print(f"   Branches: {user['branch_ids']}")
```

### List Users
```python
# List all users in tenant
users_response = requests.get(
    f"{BASE_URL}/api/v1/users/",
    headers=headers,
)
users = users_response.json()["users"]
print(f"Found {len(users)} users")
for user in users:
    print(f"- {user['full_name']} ({user['email']}) - {user['role']}")
```

### Update User
```python
# Update user information
update_data = {
    "full_name": "Updated Name",
    "role": "pathologist",
    "is_active": True,
    "password": "NewPassword123!", # Optional: Reset user password
    "branch_ids": ["branch-uuid-1", "branch-uuid-2"] # Replace existing branches
}

update_response = requests.put(
    f"{BASE_URL}/api/v1/users/{user_id}",
    headers=headers,
    json=update_data,
)
updated_user = update_response.json()
print(f"✅ User updated: {updated_user['full_name']}")
print(f"   Branches: {updated_user['branch_ids']}")
```

### Send User Invitation
```python
# Send invitation to new user
invitation_data = {
    "email": "newpathologist@example.com",
    "full_name": "Dr. New Pathologist",
    "role": "pathologist"
}

invitation_response = requests.post(
    f"{BASE_URL}/api/v1/users/invitations",
    headers=headers,
    json=invitation_data,
)
invitation = invitation_response.json()
print(f"✅ Invitation sent to {invitation['email']}")
print(f"Token: {invitation['token']}")
print(f"Expires at: {invitation['expires_at']}")
```

### Upload User Avatar
```python
# Upload avatar for user
with open("avatar.jpg", "rb") as f:
    files = {"file": ("avatar.jpg", f, "image/jpeg")}
    avatar_response = requests.post(
        f"{BASE_URL}/api/v1/users/{user_id}/avatar",
        headers=headers,
        files=files,
    )
    avatar_data = avatar_response.json()
    print(f"✅ Avatar uploaded: {avatar_data['avatar_url']}")
```

## 💬 Order Comments & Conversation

### Get Order Comments
```bash
# Get comments with default pagination (20 items)
curl "http://localhost:8000/api/v1/laboratory/orders/{order_id}/comments" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Get older comments using cursor
curl "http://localhost:8000/api/v1/laboratory/orders/{order_id}/comments?before=CURSOR_STRING&limit=50" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Get comments for an order
order_id = "ORDER_UUID"
resp = requests.get(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/comments",
    headers={"Authorization": f"Bearer {token}"},
    params={"limit": 50}
)
resp.raise_for_status()
data = resp.json()

print(f"Comments: {len(data['items'])}")
for comment in data['items']:
    author = comment['created_by']['name']
    mentions = [m['name'] for m in comment.get('mentions', [])]
    print(f"- {author}: {comment['text'][:50]}...")
    if mentions:
        print(f"  Mentions: {', '.join(mentions)}")

# Handle pagination
if data['page_info']['has_next_page']:
    next_cursor = data['page_info']['end_cursor']
    # Fetch more with: params={"after": next_cursor}
```

### Create Order Comment
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/orders/{order_id}/comments" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "text": "Sample received in good condition. @drsmith please review when ready.",
    "mentions": ["user-uuid-of-drsmith"]
  }'
```

**Python Example:**
```python
# Create a comment with mentions
comment_data = {
    "text": "Sample received in good condition. @drsmith please review when ready.",
    "mentions": ["user-uuid-of-drsmith"],
    "metadata": {"priority": "high"}
}

resp = requests.post(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/comments",
    headers={"Authorization": f"Bearer {token}"},
    json=comment_data
)
resp.raise_for_status()
comment = resp.json()
print(f"✅ Comment created: {comment['id']}")
print(f"   Mentions: {len(comment.get('mentions', []))}")
```

### Search Users for Mentions
```bash
curl "http://localhost:8000/api/v1/laboratory/users/search?q=smith" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Search users for mention autocomplete
search_resp = requests.get(
    f"{BASE_URL}/api/v1/laboratory/users/search",
    headers={"Authorization": f"Bearer {token}"},
    params={"q": "smith"}
)
search_resp.raise_for_status()
users = search_resp.json()["users"]
for user in users:
    print(f"- @{user['username'] or user['id']}: {user['name']}")
```

## 🔄 Sample State & Notes Management

### Update Sample State
```bash
curl -X PATCH "http://localhost:8000/api/v1/laboratory/samples/{sample_id}/state" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{"state": "PROCESSING"}'
```

**Python Example:**
```python
# Update sample state
sample_id = "SAMPLE_UUID"
state_resp = requests.patch(
    f"{BASE_URL}/api/v1/laboratory/samples/{sample_id}/state",
    headers={"Authorization": f"Bearer {token}"},
    json={"state": "PROCESSING"}
)
state_resp.raise_for_status()
sample = state_resp.json()
print(f"✅ Sample state updated to: {sample['state']}")

# Valid states: RECEIVED, PROCESSING, READY, DAMAGED, CANCELLED
```

### Update Sample Notes
```bash
curl -X PATCH "http://localhost:8000/api/v1/laboratory/samples/{sample_id}/notes" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{"notes": "Additional observations: tissue appears well-preserved"}'
```

**Python Example:**
```python
# Update sample notes
notes_resp = requests.patch(
    f"{BASE_URL}/api/v1/laboratory/samples/{sample_id}/notes",
    headers={"Authorization": f"Bearer {token}"},
    json={"notes": "Additional observations: tissue appears well-preserved"}
)
notes_resp.raise_for_status()
sample = notes_resp.json()
print(f"✅ Sample notes updated")
```

### Update Order Notes
```bash
curl -X PATCH "http://localhost:8000/api/v1/laboratory/orders/{order_id}/notes" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{"notes": "Patient requested expedited processing"}'
```

**Python Example:**
```python
# Update order notes
order_id = "ORDER_UUID"
notes_resp = requests.patch(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/notes",
    headers={"Authorization": f"Bearer {token}"},
    json={"notes": "Patient requested expedited processing"}
)
notes_resp.raise_for_status()
order = notes_resp.json()
print(f"✅ Order notes updated")
```

### Delete Sample Image
```bash
curl -X DELETE "http://localhost:8000/api/v1/laboratory/samples/{sample_id}/images/{image_id}" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Delete a sample image
sample_id = "SAMPLE_UUID"
image_id = "IMAGE_UUID"
del_resp = requests.delete(
    f"{BASE_URL}/api/v1/laboratory/samples/{sample_id}/images/{image_id}",
    headers={"Authorization": f"Bearer {token}"}
)
del_resp.raise_for_status()
print(f"✅ Image deleted successfully")
```

### Get Sample Events
```bash
curl "http://localhost:8000/api/v1/laboratory/samples/{sample_id}/events" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Get timeline events for a sample
sample_id = "SAMPLE_UUID"
events_resp = requests.get(
    f"{BASE_URL}/api/v1/laboratory/samples/{sample_id}/events",
    headers={"Authorization": f"Bearer {token}"}
)
events_resp.raise_for_status()
events = events_resp.json()["events"]
print(f"📋 Sample timeline ({len(events)} events):")
for event in events:
    print(f"  - {event['event_type']}: {event.get('description', '')}")
```

## 🏷️ Labels Management

### List Labels
```bash
curl "http://localhost:8000/api/v1/laboratory/labels/" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# List all labels for the tenant
labels_resp = requests.get(
    f"{BASE_URL}/api/v1/laboratory/labels/",
    headers={"Authorization": f"Bearer {token}"}
)
labels_resp.raise_for_status()
labels = labels_resp.json()["labels"]
print(f"Labels ({len(labels)}):")
for label in labels:
    print(f"  - {label['name']} ({label['color']})")
```

### Create Label
```bash
curl -X POST "http://localhost:8000/api/v1/laboratory/labels/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "name": "Urgent",
    "color": "#FF0000"
  }'
```

**Python Example:**
```python
# Create a new label
label_data = {
    "name": "Urgent",
    "color": "#FF0000"
}
label_resp = requests.post(
    f"{BASE_URL}/api/v1/laboratory/labels/",
    headers={"Authorization": f"Bearer {token}"},
    json=label_data
)
label_resp.raise_for_status()
label = label_resp.json()
print(f"✅ Label created: {label['name']} ({label['id']})")
```

### Delete Label
```bash
curl -X DELETE "http://localhost:8000/api/v1/laboratory/labels/{label_id}" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Delete a label
label_id = "LABEL_UUID"
del_resp = requests.delete(
    f"{BASE_URL}/api/v1/laboratory/labels/{label_id}",
    headers={"Authorization": f"Bearer {token}"}
)
del_resp.raise_for_status()
print(f"✅ Label deleted")
```

## 👥 Collaboration (Assignees/Reviewers/Labels)

### Update Order Assignees
```bash
curl -X PUT "http://localhost:8000/api/v1/laboratory/orders/{order_id}/assignees" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "assignee_ids": ["user-uuid-1", "user-uuid-2"]
  }'
```

**Python Example:**
```python
# Assign users to an order
order_id = "ORDER_UUID"
assignees_resp = requests.put(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/assignees",
    headers={"Authorization": f"Bearer {token}"},
    json={"assignee_ids": ["user-uuid-1", "user-uuid-2"]}
)
assignees_resp.raise_for_status()
order = assignees_resp.json()
print(f"✅ Assignees updated: {len(order['assignees'])} users")
for assignee in order['assignees']:
    print(f"  - {assignee['name']}")
```

### Update Order Reviewers
```bash
curl -X PUT "http://localhost:8000/api/v1/laboratory/orders/{order_id}/reviewers" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "reviewer_ids": ["pathologist-uuid"]
  }'
```

**Python Example:**
```python
# Assign reviewers to an order (required before REVIEW status)
reviewers_resp = requests.put(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/reviewers",
    headers={"Authorization": f"Bearer {token}"},
    json={"reviewer_ids": ["pathologist-uuid"]}
)
reviewers_resp.raise_for_status()
order = reviewers_resp.json()
print(f"✅ Reviewers updated: {len(order['reviewers'])} users")
```

### Update Order Labels
```bash
curl -X PUT "http://localhost:8000/api/v1/laboratory/orders/{order_id}/labels" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "label_ids": ["label-uuid-1", "label-uuid-2"]
  }'
```

**Python Example:**
```python
# Add labels to an order
labels_resp = requests.put(
    f"{BASE_URL}/api/v1/laboratory/orders/{order_id}/labels",
    headers={"Authorization": f"Bearer {token}"},
    json={"label_ids": ["label-uuid-1", "label-uuid-2"]}
)
labels_resp.raise_for_status()
order = labels_resp.json()
print(f"✅ Labels updated: {len(order['labels'])} labels")
for label in order['labels']:
    print(f"  - {label['name']} ({label['color']})")
```

### Update Sample Assignees
```bash
curl -X PUT "http://localhost:8000/api/v1/laboratory/samples/{sample_id}/assignees" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "assignee_ids": ["user-uuid-1"]
  }'
```

**Python Example:**
```python
# Assign users to a sample
sample_id = "SAMPLE_UUID"
assignees_resp = requests.put(
    f"{BASE_URL}/api/v1/laboratory/samples/{sample_id}/assignees",
    headers={"Authorization": f"Bearer {token}"},
    json={"assignee_ids": ["user-uuid-1"]}
)
assignees_resp.raise_for_status()
sample = assignees_resp.json()
print(f"✅ Sample assignees updated: {len(sample['assignees'])} users")
```

### Update Sample Labels
```bash
curl -X PUT "http://localhost:8000/api/v1/laboratory/samples/{sample_id}/labels" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "label_ids": ["label-uuid-3"]
  }'
```

**Python Example:**
```python
# Add additional labels to a sample (order labels are inherited)
labels_resp = requests.put(
    f"{BASE_URL}/api/v1/laboratory/samples/{sample_id}/labels",
    headers={"Authorization": f"Bearer {token}"},
    json={"label_ids": ["label-uuid-3"]}
)
labels_resp.raise_for_status()
sample = labels_resp.json()
print(f"✅ Sample labels updated")
for label in sample['labels']:
    inherited = "(inherited)" if label.get('inherited') else "(own)"
    print(f"  - {label['name']} {inherited}")
```

## 🩺 Physician Portal

### List Physician Orders
```bash
curl "http://localhost:8000/api/v1/portal/physician/orders" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# List orders where current user is the requesting physician
orders_resp = requests.get(
    f"{BASE_URL}/api/v1/portal/physician/orders",
    headers={"Authorization": f"Bearer {token}"}
)
orders_resp.raise_for_status()
orders = orders_resp.json()
print(f"My requested orders ({len(orders)}):")
for order in orders:
    status_icon = "✅" if order['has_report'] else "⏳"
    print(f"  {status_icon} {order['order_code']}: {order['patient_name']} - {order['status']}")
```

### Get Physician Report
```bash
curl "http://localhost:8000/api/v1/portal/physician/orders/{order_id}/report" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Get published report for an order (physician must be the requestor)
order_id = "ORDER_UUID"
report_resp = requests.get(
    f"{BASE_URL}/api/v1/portal/physician/orders/{order_id}/report",
    headers={"Authorization": f"Bearer {token}"}
)
if report_resp.status_code == 200:
    report = report_resp.json()
    print(f"✅ Report: {report['title']}")
    print(f"   Published: {report['published_at']}")
    print(f"   PDF URL (valid 10 min): {report['pdf_url']}")
elif report_resp.status_code == 403:
    print("⚠️ Report not available (not published or payment pending)")
else:
    print(f"❌ Error: {report_resp.status_code}")
```

## 👤 Patient Portal

### Get Patient Report (Public)
```bash
# No authentication required - uses access code
curl "http://localhost:8000/api/v1/portal/patient/report?code=A1B2C3D4E5F67890"
```

**Python Example:**
```python
import hashlib

# Generate patient access code
def generate_access_code(order_code: str, patient_code: str) -> str:
    combined = f"{order_code}:{patient_code}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16].upper()

# Example
order_code = "ORD001"
patient_code = "P001"
access_code = generate_access_code(order_code, patient_code)
print(f"Access code: {access_code}")

# Get report (no auth needed)
report_resp = requests.get(
    f"{BASE_URL}/api/v1/portal/patient/report",
    params={"code": access_code}
)
if report_resp.status_code == 200:
    report = report_resp.json()
    print(f"✅ Report for: {report['patient_name']}")
    print(f"   Order: {report['order_code']}")
    print(f"   Title: {report['title']}")
    print(f"   PDF URL (valid 10 min): {report['pdf_url']}")
elif report_resp.status_code == 403:
    print("⚠️ Report access blocked (payment pending)")
elif report_resp.status_code == 404:
    print("❌ Report not found or not yet published")
```

## 🏢 Tenant Management

### Update Tenant
```bash
curl -X PATCH "http://localhost:8000/api/v1/tenants/{tenant_id}" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "name": "Updated Laboratory Name",
    "legal_name": "Updated Legal Name Inc."
  }'
```

**Python Example:**
```python
# Update tenant details (admin only)
tenant_id = "TENANT_UUID"
update_resp = requests.patch(
    f"{BASE_URL}/api/v1/tenants/{tenant_id}",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "name": "Updated Laboratory Name",
        "legal_name": "Updated Legal Name Inc.",
        "tax_id": "NEW123456789"
    }
)
update_resp.raise_for_status()
tenant = update_resp.json()
print(f"✅ Tenant updated: {tenant['name']}")
```

### Upload Tenant Logo
```bash
curl -X POST "http://localhost:8000/api/v1/tenants/{tenant_id}/logo" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "file=@logo.png"
```

**Python Example:**
```python
# Upload tenant logo (admin only)
tenant_id = "TENANT_UUID"
with open("logo.png", "rb") as f:
    logo_resp = requests.post(
        f"{BASE_URL}/api/v1/tenants/{tenant_id}/logo",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("logo.png", f, "image/png")}
    )
logo_resp.raise_for_status()
result = logo_resp.json()
print(f"✅ Logo uploaded: {result['logo_url']}")
```

### Toggle Tenant Active Status
```bash
curl -X POST "http://localhost:8000/api/v1/tenants/{tenant_id}/toggle" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Toggle tenant active status (admin only, cannot toggle own tenant)
other_tenant_id = "OTHER_TENANT_UUID"
toggle_resp = requests.post(
    f"{BASE_URL}/api/v1/tenants/{other_tenant_id}/toggle",
    headers={"Authorization": f"Bearer {token}"}
)
if toggle_resp.status_code == 200:
    result = toggle_resp.json()
    print(f"✅ Tenant {result['message']}")
    print(f"   is_active: {result['is_active']}")
elif toggle_resp.status_code == 400:
    print("❌ Cannot deactivate your own tenant")
```

## 📋 Report Templates Management

### List Report Templates
```bash
curl "http://localhost:8000/api/v1/reports/templates/" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# List only active templates (default)
templates_response = requests.get(
    f"{BASE_URL}/api/v1/reports/templates/",
    headers={"Authorization": f"Bearer {token}"},
    params={"active_only": True}
)
templates = templates_response.json()
print(f"Active templates: {len(templates['templates'])}")

for template in templates['templates']:
    print(f"- {template['name']}: {template['description']}")

# List all templates including inactive
all_templates_response = requests.get(
    f"{BASE_URL}/api/v1/reports/templates/",
    headers={"Authorization": f"Bearer {token}"},
    params={"active_only": False}
)
all_templates = all_templates_response.json()
print(f"\nAll templates: {len(all_templates['templates'])}")
```

### Get Template Details
```bash
curl "http://localhost:8000/api/v1/reports/templates/{template_id}" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
template_response = requests.get(
    f"{BASE_URL}/api/v1/reports/templates/{template_id}",
    headers={"Authorization": f"Bearer {token}"}
)
template = template_response.json()

print(f"Template: {template['name']}")
print(f"Description: {template['description']}")
print(f"JSON Structure: {json.dumps(template['template_json'], indent=2)}")
print(f"Created by: {template['created_by']}")
print(f"Active: {template['is_active']}")
```

### Create Report Template
```bash
curl -X POST "http://localhost:8000/api/v1/reports/templates/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "name": "Citología Mamaria",
    "description": "Template para reportes de citología mamaria",
    "template_json": {
      "sections": [
        {
          "title": "Datos del Paciente",
          "fields": ["nombre", "edad", "fecha_muestra"]
        },
        {
          "title": "Resultados",
          "fields": ["diagnostico", "hallazgos", "recomendaciones"]
        }
      ],
      "metadata": {
        "version": "1.0",
        "type": "citologia_mamaria"
      }
    }
  }'
```

**Python Example:**
```python
template_data = {
    "name": "Citología Mamaria",
    "description": "Template para reportes de citología mamaria",
    "template_json": {
        "sections": [
            {
                "title": "Datos del Paciente",
                "fields": ["nombre", "edad", "fecha_muestra"]
            },
            {
                "title": "Resultados",
                "fields": ["diagnostico", "hallazgos", "recomendaciones"]
            }
        ],
        "metadata": {
            "version": "1.0",
            "type": "citologia_mamaria"
        }
    }
}

template_response = requests.post(
    f"{BASE_URL}/api/v1/reports/templates/",
    headers={"Authorization": f"Bearer {token}"},
    json=template_data
)
template = template_response.json()
print(f"✅ Template created: {template['name']}")
print(f"   ID: {template['id']}")
```

### Update Report Template
```bash
curl -X PUT "http://localhost:8000/api/v1/reports/templates/{template_id}" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -d '{
    "name": "Citología Mamaria Actualizada",
    "description": "Versión actualizada del template",
    "is_active": true
  }'
```

**Python Example:**
```python
# Update template name and description
update_data = {
    "name": "Citología Mamaria Actualizada",
    "description": "Versión actualizada del template"
}

update_response = requests.put(
    f"{BASE_URL}/api/v1/reports/templates/{template_id}",
    headers={"Authorization": f"Bearer {token}"},
    json=update_data
)
updated_template = update_response.json()
print(f"✅ Template updated: {updated_template['name']}")

# Deactivate template (soft delete)
deactivate_response = requests.put(
    f"{BASE_URL}/api/v1/reports/templates/{template_id}",
    headers={"Authorization": f"Bearer {token}"},
    json={"is_active": False}
)
print(f"✅ Template deactivated")

# Reactivate template
reactivate_response = requests.put(
    f"{BASE_URL}/api/v1/reports/templates/{template_id}",
    headers={"Authorization": f"Bearer {token}"},
    json={"is_active": True}
)
print(f"✅ Template reactivated")
```

### Delete Report Template
```bash
# Soft delete (default - sets is_active to false)
curl -X DELETE "http://localhost:8000/api/v1/reports/templates/{template_id}" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Hard delete (permanently remove)
curl -X DELETE "http://localhost:8000/api/v1/reports/templates/{template_id}?hard_delete=true" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Python Example:**
```python
# Soft delete (recommended - preserves data)
delete_response = requests.delete(
    f"{BASE_URL}/api/v1/reports/templates/{template_id}",
    headers={"Authorization": f"Bearer {token}"}
)
result = delete_response.json()
print(f"✅ {result['message']}")

# Hard delete (permanent removal)
hard_delete_response = requests.delete(
    f"{BASE_URL}/api/v1/reports/templates/{template_id}",
    headers={"Authorization": f"Bearer {token}"},
    params={"hard_delete": True}
)
result = hard_delete_response.json()
print(f"⚠️ {result['message']}")
```

**Notes:**
- **Soft Delete (default)**: Sets `is_active=false`, template is preserved but hidden from active lists
- **Hard Delete**: Permanently removes template from database (use with caution)
- **Recommendation**: Use PUT with `is_active: false` for soft delete instead of DELETE endpoint

## 📋 Report Workflow Management

### Submit Report for Review
```python
# Submit a draft report for pathologist review
submit_data = {
    "changelog": "Initial submission for pathologist review"
}

submit_response = requests.post(
    f"{BASE_URL}/api/v1/reports/{report_id}/submit",
    headers=headers,
    json=submit_data,
)
result = submit_response.json()
print(f"✅ {result['message']}")
print(f"New status: {result['status']}")  # IN_REVIEW
```

### Get Pathologist Worklist
```python
# Get all reports waiting for review
worklist_response = requests.get(
    f"{BASE_URL}/api/v1/reports/worklist",
    headers=headers,
)
worklist = worklist_response.json()["reports"]
print(f"Found {len(worklist)} reports in review")
for report in worklist:
    patient = report["order"]["patient"]["full_name"]
    order_code = report["order"]["order_code"]
    print(f"- {report['title']} (Order: {order_code}, Patient: {patient})")
```

### Approve Report (Pathologist Only)
```python
# Approve a report after review
approve_data = {
    "changelog": "Report reviewed and approved"
}

approve_response = requests.post(
    f"{BASE_URL}/api/v1/reports/{report_id}/approve",
    headers=headers,
    json=approve_data,
)
result = approve_response.json()
print(f"✅ {result['message']}")
print(f"New status: {result['status']}")  # APPROVED
```

### Request Changes (Pathologist Only)
```python
# Request changes on a report
changes_data = {
    "comment": "Please add more details about the tissue morphology and revise the diagnosis section"
}

changes_response = requests.post(
    f"{BASE_URL}/api/v1/reports/{report_id}/request-changes",
    headers=headers,
    json=changes_data,
)
result = changes_response.json()
print(f"✅ {result['message']}")
print(f"New status: {result['status']}")  # DRAFT
```

### Sign and Publish Report (Pathologist Only)
```python
# Sign and publish an approved report
sign_data = {
    "changelog": "Final review complete, report signed and published"
}

sign_response = requests.post(
    f"{BASE_URL}/api/v1/reports/{report_id}/sign",
    headers=headers,
    json=sign_data,
)
result = sign_response.json()
print(f"✅ {result['message']}")
print(f"New status: {result['status']}")  # PUBLISHED

# The report is now immutable and accessible to patients
```

### Retract Published Report (Pathologist Only)
```python
# Retract a published report
retract_data = {
    "changelog": "Report retracted due to error in diagnosis - needs revision"
}

retract_response = requests.post(
    f"{BASE_URL}/api/v1/reports/{report_id}/retract",
    headers=headers,
    json=retract_data,
)
result = retract_response.json()
print(f"✅ {result['message']}")
print(f"New status: {result['status']}")  # RETRACTED
```

## 💰 Advanced Billing Management

### Get Invoice with Items and Payments
```python
# Get complete invoice details including line items and payment history
invoice_full_response = requests.get(
    f"{BASE_URL}/api/v1/billing/invoices/{invoice_id}/full",
    headers=headers,
)
invoice_full = invoice_full_response.json()

print(f"Invoice: {invoice_full['invoice_number']}")
print(f"Total: {invoice_full['amount_total']} {invoice_full['currency']}")
print(f"Status: {invoice_full['status']}")
print(f"Balance: {invoice_full['balance']} {invoice_full['currency']}")

print("\nLine Items:")
for item in invoice_full["items"]:
    print(f"- {item['description']}: {item['quantity']} x {item['unit_price']} = {item['subtotal']}")

print("\nPayments:")
for payment in invoice_full["payments"]:
    print(f"- {payment['method']}: {payment['amount_paid']}")
```

### Get Order Payment Balance
```python
# Check payment status for an order
balance_response = requests.get(
    f"{BASE_URL}/api/v1/billing/orders/{order_id}/balance",
    headers=headers,
)
balance = balance_response.json()

print(f"Order: {balance['order_id']}")
print(f"Total Invoiced: {balance['total_invoiced']}")
print(f"Total Paid: {balance['total_paid']}")
print(f"Balance: {balance['balance']}")
print(f"Is Locked: {balance['is_locked']}")

if balance['is_locked']:
    print("⚠️ Report access is blocked due to pending payment")

print("\nInvoices:")
for invoice in balance["invoices"]:
    status_icon = "✅" if invoice['status'] == "PAID" else "⏳"
    print(f"{status_icon} {invoice['invoice_number']}: {invoice['amount_total']} (Balance: {invoice['balance']})")
```

### Add Invoice Item
```python
# Add a line item to an existing invoice
item_data = {
    "service_id": service_catalog_id,
    "description": "Additional Test - Immunohistochemistry",
    "quantity": 2,
    "unit_price": 500.00
}

item_response = requests.post(
    f"{BASE_URL}/api/v1/billing/invoices/{invoice_id}/items",
    headers=headers,
    json=item_data,
)
item = item_response.json()
print(f"✅ Item added: {item['description']}")
print(f"Subtotal: {item['subtotal']}")

# Note: Invoice total is automatically recalculated
# Order payment lock is automatically updated if needed
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
