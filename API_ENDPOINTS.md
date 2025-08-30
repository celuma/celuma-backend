# Celuma API Endpoints

Complete reference for all available API endpoints in the Celuma laboratory management system.

## 🚀 JSON-First API Design

**All POST endpoints use JSON request bodies for optimal data handling and validation.**

### Benefits of JSON Payloads
- ✅ **Excellent Data Validation**: Pydantic schemas provide automatic validation
- ✅ **Type Safety**: Strong typing for all request and response data
- ✅ **Consistent API Design**: All endpoints follow the same pattern
- ✅ **Superior Developer Experience**: Clear data structure and validation errors
- ✅ **Auto-generated Documentation**: OpenAPI/Swagger documentation with examples

### Design Principles
The API is designed with JSON request bodies for all POST endpoints, providing:
- Excellent data validation with Pydantic schemas
- Strong type safety and developer experience
- Consistent API design following REST best practices
- Auto-generated documentation and examples

## 🔐 Authentication Endpoints

### POST /api/v1/auth/register
**Register a new user**

**Request Body:**
```json
{
  "email": "user@example.com",
  "username": "johndoe",
  "password": "securepassword123",
  "full_name": "John Doe",
  "role": "admin",
  "tenant_id": "tenant-uuid-here"
}
```

**Notes:**
- `username` field is **optional** - users can register with or without a username
- If `username` is provided, it must be unique within the tenant
- `email` is always required and must be unique within the tenant

**Response:**
```json
{
  "id": "user-uuid",
  "email": "user@example.com",
  "username": "johndoe",
  "full_name": "John Doe",
  "role": "admin"
}
```

### POST /api/v1/auth/login
**Authenticate a user and return an access token. Supports username or email.**

**Request Body:**
```json
{
  "username_or_email": "johndoe",
  "password": "securepassword123"
}
```

**Behavior:**
- `username_or_email` accepts a username or an email.
- If the user is associated with exactly one tenant, authentication returns a token.
- If the user matches multiple tenants, the response will request tenant selection.
- To finalize login for a specific tenant, send the same request including `tenant_id`.

**Response (single-tenant login):**
```json
{
  "access_token": "jwt-token-here",
  "token_type": "bearer"
}
```

**Response (tenant selection required):**
```json
{
  "need_tenant_selection": true,
  "options": [
    { "tenant_id": "tenant-uuid-1", "tenant_name": "Acme Labs" },
    { "tenant_id": "tenant-uuid-2", "tenant_name": "Beta Diagnostics" }
  ]
}
```

**Request Body (finalize with tenant_id):**
```json
{
  "username_or_email": "johndoe",
  "password": "securepassword123",
  "tenant_id": "tenant-uuid-here"
}
```

**Response (finalized login):**
```json
{
  "access_token": "jwt-token-here",
  "token_type": "bearer"
}
```

**Notes:**
- Authentication prioritizes username match first, then falls back to email.
- The same password is used regardless of whether you log in with username or email.
- On error, the endpoint returns `401 Unauthorized`.

### POST /api/v1/auth/logout
**Logout user and blacklist token**

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{
  "message": "Logout successful",
  "token_revoked": true
}
```

### GET /api/v1/auth/me
**Get current user profile**

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{
  "id": "user-uuid",
  "email": "user@example.com",
  "username": "johndoe",
  "full_name": "John Doe",
  "role": "admin",
  "tenant_id": "tenant-uuid-here"
}
```

**Notes:**
- `username` field will be `null` if the user doesn't have a username set
- All other user information is always included

## 🏢 Tenant Management

### POST /api/v1/tenants/
**Create a new tenant**

**Request Body:**
```json
{
  "name": "Acme Laboratories",
  "legal_name": "Acme Laboratories Inc.",
  "tax_id": "123456789"
}
```

**Response:**
```json
{
  "id": "tenant-uuid",
  "name": "Acme Laboratories",
  "legal_name": "Acme Laboratories Inc."
}
```

### GET /api/v1/tenants/
**List all tenants**

**Response:**
```json
[
  {
    "id": "tenant-uuid",
    "name": "Acme Laboratories",
    "legal_name": "Acme Laboratories Inc."
  }
]
```

### GET /api/v1/tenants/{tenant_id}
**Get tenant details**

**Response:**
```json
{
  "id": "tenant-uuid",
  "name": "Acme Laboratories",
  "legal_name": "Acme Laboratories Inc.",
  "tax_id": "123456789"
}
```

### GET /api/v1/tenants/{tenant_id}/branches
**List branches for a specific tenant**

**Response:**
```json
[
  {
    "id": "branch-uuid",
    "name": "Main Branch",
    "code": "MAIN"
  }
]
```

### GET /api/v1/tenants/{tenant_id}/users
**List users for a specific tenant**

**Response:**
```json
[
  {
    "id": "user-uuid",
    "email": "user@example.com",
    "full_name": "John Doe",
    "role": "admin"
  }
]
```

## 🏥 Branch Management

### POST /api/v1/branches/
**Create a new branch**

**Request Body:**
```json
{
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
}
```

**Response:**
```json
{
  "id": "branch-uuid",
  "code": "MAIN",
  "name": "Main Branch",
  "tenant_id": "tenant-uuid-here"
}
```

### GET /api/v1/branches/
**List all branches**

**Response:**
```json
[
  {
    "id": "branch-uuid",
    "code": "MAIN",
    "name": "Main Branch",
    "tenant_id": "tenant-uuid-here"
  }
]
```

### GET /api/v1/branches/{branch_id}
**Get branch details**

**Response:**
```json
{
  "id": "branch-uuid",
  "code": "MAIN",
  "name": "Main Branch",
  "timezone": "America/Mexico_City",
  "address_line1": "Av. Reforma 123",
  "address_line2": "Piso 4",
  "city": "Mexico City",
  "state": "CDMX",
  "postal_code": "06000",
  "country": "MX",
  "is_active": true,
  "tenant_id": "tenant-uuid-here"
}
```

### GET /api/v1/branches/{branch_id}/users
**List users for a specific branch**

**Response:**
```json
[
  {
    "id": "user-uuid",
    "email": "user@example.com",
    "full_name": "John Doe",
    "role": "admin"
  }
]
```

## 👥 Patient Management

### POST /api/v1/patients/
**Create a new patient**

**Request Body:**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "patient_code": "P001",
  "first_name": "John",
  "last_name": "Doe",
  "dob": "1990-01-01",
  "sex": "M",
  "phone": "555-1234",
  "email": "john.doe@example.com"
}
```

**Response:**
```json
{
  "id": "patient-uuid",
  "patient_code": "P001",
  "first_name": "John",
  "last_name": "Doe",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here"
}
```

### GET /api/v1/patients/
**List all patients**

**Response:**
```json
[
  {
    "id": "patient-uuid",
    "patient_code": "P001",
    "first_name": "John",
    "last_name": "Doe",
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here"
  }
]
```

### GET /api/v1/patients/{patient_id}
**Get patient details**

**Response:**
```json
{
  "id": "patient-uuid",
  "patient_code": "P001",
  "first_name": "John",
  "last_name": "Doe",
  "dob": "1990-01-01",
  "sex": "M",
  "phone": "555-1234",
  "email": "john.doe@example.com",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here"
}
```

## 🧪 Laboratory Management

### POST /api/v1/laboratory/orders/
**Create a new laboratory order**

**Request Body:**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "patient_id": "patient-uuid-here",
  "order_code": "ORD001",
  "requested_by": "Dr. Smith",
  "notes": "Complete blood count requested"
}
```

**Response:**
```json
{
  "id": "order-uuid",
  "order_code": "ORD001",
  "status": "RECEIVED",
  "patient_id": "patient-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here"
}
```

### GET /api/v1/laboratory/orders/
**List all laboratory orders**

**Response:**
```json
[
  {
    "id": "order-uuid",
    "order_code": "ORD001",
    "status": "RECEIVED",
    "patient_id": "patient-uuid-here",
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here"
  }
]
```

### GET /api/v1/laboratory/orders/{order_id}
**Get laboratory order details**

**Response:**
```json
{
  "id": "order-uuid",
  "order_code": "ORD001",
  "status": "RECEIVED",
  "patient_id": "patient-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "requested_by": "Dr. Smith",
  "notes": "Complete blood count requested",
  "billed_lock": false
}
```

### POST /api/v1/laboratory/samples/
**Create a new sample**

**Request Body:**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "order_id": "order-uuid-here",
  "sample_code": "SAMP001",
  "type": "SANGRE",
  "notes": "Blood sample for CBC",
  "collected_at": "2025-08-18T10:00:00Z",
  "received_at": "2025-08-18T11:00:00Z"
}
```

**Response:**
```json
{
  "id": "sample-uuid",
  "sample_code": "SAMP001",
  "type": "SANGRE",
  "state": "RECEIVED",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here"
}
```

### GET /api/v1/laboratory/samples/
**List all samples**

**Response:**
```json
[
  {
    "id": "sample-uuid",
    "sample_code": "SAMP001",
    "type": "SANGRE",
    "state": "RECEIVED",
    "order_id": "order-uuid-here",
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here"
  }
]
```

## 📋 Report Management

### POST /api/v1/reports/
**Create a new report**

**Request Body:**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "order_id": "order-uuid-here",
  "title": "Blood Test Report",
  "diagnosis_text": "Normal blood count results",
  "published_at": "2025-08-18T12:00:00Z"
}
```

**Response:**
```json
{
  "id": "report-uuid",
  "status": "DRAFT",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here"
}
```

### GET /api/v1/reports/
**List all reports**

**Response:**
```json
[
  {
    "id": "report-uuid",
    "status": "DRAFT",
    "order_id": "order-uuid-here",
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here"
  }
]
```

### GET /api/v1/reports/{report_id}
**Get report details**

**Response:**
```json
{
  "id": "report-uuid",
  "status": "DRAFT",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "title": "Blood Test Report",
  "diagnosis_text": "Normal blood count results",
  "published_at": null
}
```

### POST /api/v1/reports/versions/
**Create a new report version**

**Request Body:**
```json
{
  "report_id": "report-uuid-here",
  "version_no": 1,
  "pdf_storage_id": "storage-uuid-here",
  "html_storage_id": "storage-uuid-here",
  "changelog": "Initial report version",
  "authored_by": "user-uuid-here",
  "authored_at": "2025-08-18T12:30:00Z"
}
```

**Response:**
```json
{
  "id": "version-uuid",
  "version_no": 1,
  "report_id": "report-uuid-here",
  "is_current": true
}
```

### GET /api/v1/reports/versions/
**List all report versions**

**Response:**
```json
[
  {
    "id": "version-uuid",
    "version_no": 1,
    "report_id": "report-uuid-here",
    "is_current": true
  }
]
```

## 💰 Billing Management

### POST /api/v1/billing/invoices/
**Create a new invoice**

**Request Body:**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "order_id": "order-uuid-here",
  "invoice_number": "INV001",
  "amount_total": 1500.00,
  "currency": "MXN",
  "issued_at": "2025-08-18T01:50:51.386774"
}
```

**Response:**
```json
{
  "id": "invoice-uuid",
  "invoice_number": "INV001",
  "amount_total": 1500.0,
  "currency": "MXN",
  "status": "PENDING",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here"
}
```

### GET /api/v1/billing/invoices/
**List all invoices**

**Response:**
```json
[
  {
    "id": "invoice-uuid",
    "invoice_number": "INV001",
    "amount_total": 1500.0,
    "currency": "MXN",
    "status": "PENDING",
    "order_id": "order-uuid-here",
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here"
  }
]
```

### GET /api/v1/billing/invoices/{invoice_id}
**Get invoice details**

**Response:**
```json
{
  "id": "invoice-uuid",
  "invoice_number": "INV001",
  "amount_total": 1500.0,
  "currency": "MXN",
  "status": "PENDING",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "issued_at": "2025-08-18T01:50:51.386774"
}
```

### POST /api/v1/billing/payments/
**Create a new payment**

**Request Body:**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "invoice_id": "invoice-uuid-here",
  "amount_paid": 1500.00,
  "method": "credit_card",
  "paid_at": "2025-08-18T02:10:00Z"
}
```

**Response:**
```json
{
  "id": "payment-uuid",
  "amount_paid": 1500.0,
  "method": "credit_card",
  "invoice_id": "invoice-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here"
}
```

### GET /api/v1/billing/payments/
**List all payments**

**Response:**
```json
[
  {
    "id": "payment-uuid",
    "amount_paid": 1500.0,
    "method": "credit_card",
    "invoice_id": "invoice-uuid-here",
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here"
  }
]
```

## 🔍 System Endpoints

### GET /
**Get system information**

**Response:**
```json
{
  "message": "Welcome to Celuma API",
  "version": "1.0.0",
  "features": [
    "Multi-tenant support",
    "Laboratory management",
    "Patient management",
    "Sample tracking",
    "Report generation",
    "Billing system",
    "Audit logging"
  ]
}
```

### GET /api/v1/health
**Health check endpoint**

**Response:**
```json
{
  "status": "ok"
}
```

## 📚 Data Models

### Common Fields
All entities include these common fields:
- `id`: Unique identifier (UUID)
- `tenant_id`: Associated tenant
- `branch_id`: Associated branch (where applicable)
- `created_at`: Creation timestamp
- `updated_at`: Last update timestamp

### Status Enums
- **Order Status**: `RECEIVED`, `PROCESSING`, `COMPLETED`, `CANCELLED`
- **Sample State**: `RECEIVED`, `PROCESSING`, `COMPLETED`, `DISCARDED`
- **Report Status**: `DRAFT`, `REVIEW`, `PUBLISHED`, `ARCHIVED`
- **Invoice Status**: `PENDING`, `PAID`, `OVERDUE`, `CANCELLED`
- **User Role**: `admin`, `technician`, `doctor`, `receptionist`

## 🔐 Authentication

### JWT Token Format
All authenticated endpoints require a Bearer token in the Authorization header:
```
Authorization: Bearer <jwt_token>
```

### Token Expiration
JWT tokens expire after 24 hours. Use the logout endpoint to invalidate tokens before expiration.

### Token Blacklisting
When users logout, their tokens are automatically blacklisted and cannot be used for subsequent requests.

## 📊 Response Formats

### Success Responses
All successful responses return HTTP 200 status with JSON data matching the endpoint's response model.

### Error Responses
- **400 Bad Request**: Invalid request data
- **401 Unauthorized**: Missing or invalid authentication token
- **403 Forbidden**: Insufficient permissions
- **404 Not Found**: Resource not found
- **422 Unprocessable Entity**: Validation errors
- **500 Internal Server Error**: Server-side error

### Validation Errors
When validation fails, the API returns detailed error information:
```json
{
  "detail": [
    {
      "loc": ["body", "email"],
      "msg": "invalid email format",
      "type": "value_error.email"
    }
  ]
}
```

## 🚀 Rate Limiting

Currently, no rate limiting is implemented. However, it's recommended to:
- Limit requests to reasonable frequencies
- Implement exponential backoff for failed requests
- Cache responses when appropriate

## 📖 Additional Documentation

- [API Examples](API_EXAMPLES.md) - Usage examples and patterns
- [Testing Guide](tests/TESTING_README.md) - Comprehensive testing documentation
- [Database Schema](DATABASE_README.md) - Database design and migrations
- [Swagger Documentation](http://localhost:8000/docs) - Interactive API documentation

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

---

**Note: All POST endpoints use JSON request bodies for optimal data handling, validation, and consistent API design.**
