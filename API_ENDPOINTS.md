# Celuma API - Complete Endpoints Reference

This document provides a comprehensive reference for all available API endpoints in the Celuma system.

## 🔗 Base URL

```
http://localhost:8000
```

## 📚 Interactive Documentation

- **Swagger UI**: `/docs`
- **ReDoc**: `/redoc`
- **OpenAPI JSON**: `/openapi.json`

## 🏥 API Endpoints

### Health & Status

#### GET `/`
**Description**: Root endpoint with system information  
**Response**: System overview and version information

```json
{
  "message": "Welcome to Celuma API",
  "version": "2.0.0",
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

#### GET `/api/v1/health`
**Description**: Health check endpoint  
**Response**: System health status

```json
{
  "status": "ok"
}
```

### Authentication

#### POST `/api/v1/auth/register`
**Description**: User registration  
**Parameters**:
- `email` (string, required): User email address
- `password` (string, required): User password
- `full_name` (string, required): User full name
- `role` (string, required): User role (admin, pathologist, lab_tech, assistant, billing, viewer)
- `tenant_id` (string, required): Tenant UUID

**Response**:
```json
{
  "id": "uuid",
  "email": "user@example.com",
  "full_name": "John Doe",
  "role": "admin"
}
```

#### POST `/api/v1/auth/login`
**Description**: User authentication  
**Parameters**:
- `email` (string, required): User email address
- `password` (string, required): User password
- `tenant_id` (string, required): Tenant UUID

**Response**:
```json
{
  "access_token": "jwt_token_here",
  "token_type": "bearer"
}
```

#### GET `/api/v1/auth/me`
**Description**: Get current user information  
**Authentication**: Bearer token required  
**Response**:
```json
{
  "id": "uuid",
  "email": "user@example.com",
  "full_name": "John Doe",
  "role": "admin",
  "tenant_id": "tenant_uuid"
}
```

### Tenant Management

#### GET `/api/v1/tenants/`
**Description**: List all tenants  
**Response**: Array of tenant objects

```json
[
  {
    "id": "uuid",
    "name": "Tenant Name",
    "legal_name": "Legal Business Name"
  }
]
```

#### POST `/api/v1/tenants/`
**Description**: Create a new tenant  
**Parameters**:
- `name` (string, required): Tenant name
- `legal_name` (string, optional): Legal business name
- `tax_id` (string, optional): Tax identification number

**Response**: Created tenant object

#### GET `/api/v1/tenants/{tenant_id}`
**Description**: Get tenant details  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID

**Response**: Tenant object

#### GET `/api/v1/tenants/{tenant_id}/branches`
**Description**: List all branches for a specific tenant  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID

**Response**: Array of branch objects

#### GET `/api/v1/tenants/{tenant_id}/users`
**Description**: List all users for a specific tenant  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID

**Response**: Array of user objects

### Branch Management

#### GET `/api/v1/branches/`
**Description**: List all branches  
**Response**: Array of branch objects

```json
[
  {
    "id": "uuid",
    "name": "Branch Name",
    "code": "BRANCH_CODE",
    "city": "Mexico City",
    "tenant_id": "tenant_uuid"
  }
]
```

#### POST `/api/v1/branches/`
**Description**: Create a new branch  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID
- `code` (string, required): Branch code (unique per tenant)
- `name` (string, required): Branch name
- `timezone` (string, optional): Timezone (default: "America/Mexico_City")
- `address_line1` (string, optional): Street address
- `city` (string, optional): City name
- `state` (string, optional): State/province
- `country` (string, optional): Country code (default: "MX")

**Response**: Created branch object

#### GET `/api/v1/branches/{branch_id}`
**Description**: Get branch details  
**Parameters**:
- `branch_id` (string, required): Branch UUID

**Response**: Branch object

#### GET `/api/v1/branches/{branch_id}/users`
**Description**: List all users for a specific branch  
**Parameters**:
- `branch_id` (string, required): Branch UUID

**Response**: Array of user objects

### Patient Management

#### GET `/api/v1/patients/`
**Description**: List all patients  
**Response**: Array of patient objects

```json
[
  {
    "id": "uuid",
    "patient_code": "P001",
    "first_name": "John",
    "last_name": "Doe",
    "tenant_id": "tenant_uuid",
    "branch_id": "branch_uuid"
  }
]
```

#### POST `/api/v1/patients/`
**Description**: Create a new patient  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID
- `branch_id` (string, required): Branch UUID
- `patient_code` (string, required): Patient code (unique per tenant)
- `first_name` (string, required): Patient first name
- `last_name` (string, required): Patient last name
- `dob` (string, optional): Date of birth (YYYY-MM-DD)
- `sex` (string, optional): Gender (M/F)
- `phone` (string, optional): Phone number
- `email` (string, optional): Email address

**Response**: Created patient object

#### GET `/api/v1/patients/{patient_id}`
**Description**: Get patient details  
**Parameters**:
- `patient_id` (string, required): Patient UUID

**Response**: Patient object with full details

### Laboratory Management

#### GET `/api/v1/laboratory/orders/`
**Description**: List all laboratory orders  
**Response**: Array of order objects

```json
[
  {
    "id": "uuid",
    "order_code": "ORD001",
    "status": "RECEIVED",
    "patient_id": "patient_uuid",
    "tenant_id": "tenant_uuid",
    "branch_id": "branch_uuid"
  }
]
```

#### POST `/api/v1/laboratory/orders/`
**Description**: Create a new laboratory order  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID
- `branch_id` (string, required): Branch UUID
- `patient_id` (string, required): Patient UUID
- `order_code` (string, required): Order code (unique per branch)
- `requested_by` (string, optional): Requesting physician
- `notes` (string, optional): Order notes
- `created_by` (string, optional): User UUID who created the order

**Response**: Created order object

#### GET `/api/v1/laboratory/orders/{order_id}`
**Description**: Get order details  
**Parameters**:
- `order_id` (string, required): Order UUID

**Response**: Order object with full details

#### GET `/api/v1/laboratory/samples/`
**Description**: List all samples  
**Response**: Array of sample objects

```json
[
  {
    "id": "uuid",
    "sample_code": "SAMP001",
    "type": "SANGRE",
    "state": "RECEIVED",
    "order_id": "order_uuid",
    "tenant_id": "tenant_uuid",
    "branch_id": "branch_uuid"
  }
]
```

#### POST `/api/v1/laboratory/samples/`
**Description**: Create a new sample  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID
- `branch_id` (string, required): Branch UUID
- `order_id` (string, required): Order UUID
- `sample_code` (string, required): Sample code (unique per order)
- `type` (string, required): Sample type (SANGRE, BIOPSIA, LAMINILLA, TEJIDO, OTRO)
- `notes` (string, optional): Sample notes

**Response**: Created sample object

### Report Management

#### GET `/api/v1/reports/`
**Description**: List all reports  
**Response**: Array of report objects

```json
[
  {
    "id": "uuid",
    "status": "DRAFT",
    "order_id": "order_uuid",
    "tenant_id": "tenant_uuid",
    "branch_id": "branch_uuid"
  }
]
```

#### POST `/api/v1/reports/`
**Description**: Create a new report  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID
- `branch_id` (string, required): Branch UUID
- `order_id` (string, required): Order UUID
- `title` (string, optional): Report title
- `diagnosis_text` (string, optional): Diagnosis text
- `created_by` (string, optional): User UUID who created the report

**Response**: Created report object

#### GET `/api/v1/reports/{report_id}`
**Description**: Get report details  
**Parameters**:
- `report_id` (string, required): Report UUID

**Response**: Report object with full details

#### GET `/api/v1/reports/versions/`
**Description**: List all report versions  
**Response**: Array of version objects

#### POST `/api/v1/reports/versions/`
**Description**: Create a new report version  
**Parameters**:
- `report_id` (string, required): Report UUID
- `version_no` (integer, required): Version number
- `pdf_storage_id` (string, required): PDF storage object UUID
- `html_storage_id` (string, optional): HTML storage object UUID
- `changelog` (string, optional): Version changelog
- `authored_by` (string, optional): User UUID who authored the version

**Response**: Created version object

### Billing Management

#### GET `/api/v1/billing/invoices/`
**Description**: List all invoices  
**Response**: Array of invoice objects

```json
[
  {
    "id": "uuid",
    "invoice_number": "INV001",
    "amount_total": 1500.00,
    "currency": "MXN",
    "status": "PENDING",
    "order_id": "order_uuid",
    "tenant_id": "tenant_uuid",
    "branch_id": "branch_uuid"
  }
]
```

#### POST `/api/v1/billing/invoices/`
**Description**: Create a new invoice  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID
- `branch_id` (string, required): Branch UUID
- `order_id` (string, required): Order UUID
- `invoice_number` (string, required): Invoice number (unique per branch)
- `amount_total` (float, required): Total invoice amount
- `currency` (string, optional): Currency code (default: "MXN")

**Response**: Created invoice object

#### GET `/api/v1/billing/invoices/{invoice_id}`
**Description**: Get invoice details  
**Parameters**:
- `invoice_id` (string, required): Invoice UUID

**Response**: Invoice object with full details

#### GET `/api/v1/billing/payments/`
**Description**: List all payments  
**Response**: Array of payment objects

```json
[
  {
    "id": "uuid",
    "amount_paid": 1500.00,
    "method": "transfer",
    "invoice_id": "invoice_uuid",
    "tenant_id": "tenant_uuid",
    "branch_id": "branch_uuid"
  }
]
```

#### POST `/api/v1/billing/payments/`
**Description**: Create a new payment  
**Parameters**:
- `tenant_id` (string, required): Tenant UUID
- `branch_id` (string, required): Branch UUID
- `invoice_id` (string, required): Invoice UUID
- `amount_paid` (float, required): Payment amount
- `method` (string, optional): Payment method

**Response**: Created payment object

## 🔐 Authentication

Most endpoints require authentication using JWT Bearer tokens. Include the token in the Authorization header:

```
Authorization: Bearer <your_jwt_token>
```

## 📊 Response Formats

### Success Response
```json
{
  "id": "uuid",
  "field1": "value1",
  "field2": "value2"
}
```

### Error Response
```json
{
  "detail": "Error message description"
}
```

### Validation Error Response
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "field_name"],
      "msg": "Field required",
      "input": null
    }
  ]
}
```

## 🚨 HTTP Status Codes

- **200**: Success
- **201**: Created
- **400**: Bad Request (validation errors)
- **401**: Unauthorized (authentication required)
- **404**: Not Found
- **422**: Unprocessable Entity (validation errors)
- **500**: Internal Server Error

## 🔄 Pagination

Currently, list endpoints return all results. Pagination will be implemented in future versions.

## 📝 Notes

- All UUIDs are in standard UUID v4 format
- Dates are in ISO 8601 format (YYYY-MM-DD)
- Monetary amounts are stored as decimal numbers
- Sample types and order statuses use predefined enum values
- All endpoints support CORS for web applications
