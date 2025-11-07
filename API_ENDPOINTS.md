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

### Global Authentication Policy
- All API endpoints require a Bearer token in the `Authorization` header, except:
  - `GET /`
  - `GET /health`
  - `GET /api/v1/health`
  - `POST /api/v1/auth/login`
  - `POST /api/v1/auth/register`
  - `POST /api/v1/auth/register/unified`
- For all other endpoints, include:
```
Authorization: Bearer <jwt_token>
```

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

**Notes:**
- `username` field is **optional** - users can register with or without a username
- If `username` is provided, it must be unique within the tenant
- `email` is always required and must be unique within the tenant

### POST /api/v1/auth/register/unified
**Unified registration: create tenant, branch and admin user**

Request body:

```json
{
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
}
```

Response body:

```json
{
  "tenant_id": "...",
  "branch_id": "...",
  "user_id": "..."
}
```

**Notes:**
- The operation is atomic. If any step fails, nothing is created.
- The created user has role "admin" and is linked to the created branch.
- `branch.code` must be unique per tenant.

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
- All successful login responses include `tenant_id`.

**Response (single-tenant login):**
```json
{
  "access_token": "jwt-token-here",
  "token_type": "bearer",
  "tenant_id": "tenant-uuid-here"
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
  "token_type": "bearer",
  "tenant_id": "tenant-uuid-here"
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

### PUT /api/v1/auth/me
**Update current user profile and/or password**

**Headers:** `Authorization: Bearer <token>`

**Request Body:**
```json
{
  "full_name": "New Name",
  "username": "new-username",
  "email": "new.email@example.com",
  "current_password": "oldpass",
  "new_password": "newpass123"
}
```

**Response:**
```json
{
  "id": "user-uuid",
  "email": "new.email@example.com",
  "username": "new-username",
  "full_name": "New Name",
  "role": "admin",
  "tenant_id": "tenant-uuid-here"
}
```

**Notes:**
- To change the password, you must provide both `current_password` and `new_password`. The current password must match the existing one.
- `username` and `email` must be unique within the tenant.
- All fields are optional; only the provided fields will be updated.

## 👤 User Management

Headers: `Authorization: Bearer <token>`

**Note:** All user management endpoints require Admin role unless otherwise specified.

### GET /api/v1/users/
**List all users in the tenant (Admin only)**

**Response:**
```json
{
  "users": [
    {
      "id": "user-uuid",
      "tenant_id": "tenant-uuid",
      "email": "user@example.com",
      "username": "johndoe",
      "full_name": "John Doe",
      "role": "lab_tech",
      "is_active": true,
      "created_at": "2025-01-15T10:00:00Z",
      "updated_at": "2025-01-15T10:00:00Z"
    }
  ]
}
```

### POST /api/v1/users/
**Create a new user (Admin only)**

**Request Body:**
```json
{
  "email": "newuser@example.com",
  "username": "newuser",
  "password": "SecurePass123!",
  "full_name": "New User",
  "role": "lab_tech"
}
```

**Response:**
```json
{
  "id": "user-uuid",
  "tenant_id": "tenant-uuid",
  "email": "newuser@example.com",
  "username": "newuser",
  "full_name": "New User",
  "role": "lab_tech",
  "is_active": true,
  "created_at": "2025-01-15T10:00:00Z",
  "updated_at": "2025-01-15T10:00:00Z"
}
```

**Notes:**
- Email must be unique within the tenant
- Username is optional but must be unique if provided
- Password must meet security requirements
- Role must be one of: `admin`, `pathologist`, `lab_tech`, `assistant`, `billing`, `viewer`

### PUT /api/v1/users/{user_id}
**Update a user (Admin only)**

**Request Body:**
```json
{
  "email": "updated@example.com",
  "username": "updateduser",
  "full_name": "Updated Name",
  "role": "pathologist",
  "is_active": true
}
```

**Response:**
```json
{
  "id": "user-uuid",
  "tenant_id": "tenant-uuid",
  "email": "updated@example.com",
  "username": "updateduser",
  "full_name": "Updated Name",
  "role": "pathologist",
  "is_active": true,
  "created_at": "2025-01-15T10:00:00Z",
  "updated_at": "2025-01-16T14:30:00Z"
}
```

**Notes:**
- All fields are optional; only provided fields will be updated
- Cannot update password through this endpoint (use PUT /api/v1/auth/me)
- User cannot update their own account

### DELETE /api/v1/users/{user_id}
**Deactivate a user (Admin only)**

**Response:**
```json
{
  "message": "User deactivated successfully"
}
```

**Notes:**
- This endpoint sets `is_active` to `false` rather than deleting the user
- Users cannot deactivate themselves
- Deactivated users cannot log in but their data is preserved

### POST /api/v1/users/{user_id}/toggle-active
**Toggle user active status (Admin only)**

**Response:**
```json
{
  "message": "User activated",
  "is_active": true
}
```

**Notes:**
- Toggles the `is_active` status of a user
- Users cannot toggle their own status
- Useful for quickly enabling/disabling user access

### POST /api/v1/users/invitations
**Create and send user invitation (Admin only)**

**Request Body:**
```json
{
  "email": "newuser@example.com",
  "full_name": "New User",
  "role": "lab_tech"
}
```

**Response:**
```json
{
  "id": "invitation-uuid",
  "email": "newuser@example.com",
  "full_name": "New User",
  "role": "lab_tech",
  "token": "secure-token-string",
  "expires_at": "2025-01-22T10:00:00Z"
}
```

**Notes:**
- Generates a secure invitation token valid for 7 days
- Sends an email invitation to the specified address
- Cannot invite email addresses that already exist in the tenant
- Only one pending invitation per email at a time

### GET /api/v1/users/invitations/{token}
**Get invitation details (Public endpoint)**

**Response:**
```json
{
  "email": "newuser@example.com",
  "full_name": "New User",
  "role": "lab_tech",
  "tenant_name": "Acme Laboratories",
  "expires_at": "2025-01-22T10:00:00Z"
}
```

**Notes:**
- Public endpoint for verifying invitation tokens
- Does not require authentication
- Returns 404 if invitation not found or already used
- Returns 400 if invitation has expired

### POST /api/v1/users/invitations/{token}/accept
**Accept invitation and create user account (Public endpoint)**

**Request Body:**
```json
{
  "password": "SecurePass123!",
  "username": "myusername"
}
```

**Response:**
```json
{
  "message": "Account created successfully",
  "user_id": "user-uuid",
  "email": "newuser@example.com"
}
```

**Notes:**
- Public endpoint, does not require authentication
- Username is optional
- Password must meet security requirements
- Marks invitation as used after successful account creation
- User can immediately log in after account creation

### POST /api/v1/users/{user_id}/avatar
**Upload user avatar**

**Content-Type:** `multipart/form-data`

**Form Data:**
- `file`: Image file (JPEG, PNG, or WEBP)

**Response:**
```json
{
  "message": "Avatar uploaded successfully",
  "avatar_url": "https://s3.amazonaws.com/bucket/avatars/user-uuid/avatar.jpg"
}
```

**Notes:**
- Users can upload their own avatar
- Admins can upload avatars for any user
- Only image files are accepted (JPEG, PNG, WEBP)
- Avatar is stored in S3 and URL is saved to user profile

## 🏢 Tenant Management

Headers: `Authorization: Bearer <token>`

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

Headers: `Authorization: Bearer <token>`

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

Headers: `Authorization: Bearer <token>`

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
**List all patients (full profile)**

Returns a list of full patient profiles (`PatientFullResponse`).

**Response:**
```json
[
  {
    "id": "patient-uuid",
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

Headers: `Authorization: Bearer <token>`

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
  "notes": "Complete blood count requested",
  "created_by": "user-uuid-here"
}
```

**Notes:**
- `created_by` is optional and must be a UUID (user id) if provided.

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
**List all laboratory orders (enriched)**

Returns `orders` array with enriched `branch` and `patient` objects and summary fields.

**Response:**
```json
{
  "orders": [
    {
      "id": "order-uuid",
      "order_code": "ORD001",
      "status": "RECEIVED",
      "tenant_id": "tenant-uuid",
      "branch": { "id": "branch-uuid", "name": "Main Branch", "code": "MAIN" },
      "patient": { "id": "patient-uuid", "full_name": "John Doe", "patient_code": "P001" },
      "requested_by": "Dr. Smith",
      "notes": "...",
      "created_at": "2025-08-18T10:00:00Z",
      "sample_count": 2,
      "has_report": true
    }
  ]
}
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
### POST /api/v1/laboratory/orders/unified
**Create a laboratory order and multiple samples in one operation**

**Request Body:**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "patient_id": "patient-uuid-here",
  "order_code": "ORD001",
  "requested_by": "Dr. Smith",
  "notes": "Complete blood count requested",
  "created_by": "user-uuid-here",
  "samples": [
    {
      "sample_code": "SAMP001",
      "type": "SANGRE",
      "notes": "",
      "collected_at": "2025-08-18T10:00:00Z",
      "received_at": "2025-08-18T11:00:00Z"
    },
    {
      "sample_code": "SAMP002",
      "type": "TEJIDO"
    }
  ]
}
```

**Response:**
```json
{
  "order": {
    "id": "order-uuid",
    "order_code": "ORD001",
    "status": "RECEIVED",
    "patient_id": "patient-uuid-here",
    "tenant_id": "tenant-uuid-here",
    "branch_id": "branch-uuid-here"
  },
  "samples": [
    {
      "id": "sample-uuid-1",
      "sample_code": "SAMP001",
      "type": "SANGRE",
      "state": "RECEIVED",
      "order_id": "order-uuid",
      "tenant_id": "tenant-uuid-here",
      "branch_id": "branch-uuid-here"
    },
    {
      "id": "sample-uuid-2",
      "sample_code": "SAMP002",
      "type": "TEJIDO",
      "state": "RECEIVED",
      "order_id": "order-uuid",
      "tenant_id": "tenant-uuid-here",
      "branch_id": "branch-uuid-here"
    }
  ]
}
```

**Errors:**
- 400 if `order_code` already exists for the branch, a `sample_code` is duplicated in the request, or already exists in the order
- 404 if tenant, branch, or patient not found

---

### GET /api/v1/laboratory/orders/{order_id}/full
**Get complete order details including patient (full) and samples**

**Response:**
```json
{
  "order": {
    "id": "order-uuid",
    "order_code": "ORD001",
    "status": "RECEIVED",
    "patient_id": "patient-uuid",
    "tenant_id": "tenant-uuid",
    "branch_id": "branch-uuid",
    "requested_by": "Dr. Smith",
    "notes": "...",
    "billed_lock": false
  },
  "patient": {
    "id": "patient-uuid",
    "tenant_id": "tenant-uuid",
    "branch_id": "branch-uuid",
    "patient_code": "P001",
    "first_name": "John",
    "last_name": "Doe",
    "dob": "1990-01-01",
    "sex": "M",
    "phone": "555-1234",
    "email": "john.doe@example.com"
  },
  "samples": [
    {
      "id": "sample-uuid",
      "sample_code": "SAMP001",
      "type": "SANGRE",
      "state": "RECEIVED",
      "order_id": "order-uuid",
      "tenant_id": "tenant-uuid",
      "branch_id": "branch-uuid"
    }
  ]
}
```

**Errors:**
- 404 if order or patient is not found

### GET /api/v1/laboratory/patients/{patient_id}/orders
**List all orders for a given patient (summary) and return patient (full)**

**Response:**
```json
{
  "patient": {
    "id": "patient-uuid",
    "tenant_id": "tenant-uuid",
    "branch_id": "branch-uuid",
    "patient_code": "P001",
    "first_name": "John",
    "last_name": "Doe",
    "dob": "1990-01-01",
    "sex": "M",
    "phone": "555-1234",
    "email": "john.doe@example.com"
  },
  "orders": [
    {
      "id": "order-uuid",
      "order_code": "ORD001",
      "status": "RECEIVED",
      "tenant_id": "tenant-uuid",
      "branch_id": "branch-uuid",
      "patient_id": "patient-uuid",
      "requested_by": "Dr. Smith",
      "notes": "...",
      "created_at": "2025-08-18T10:00:00Z",
      "sample_count": 2,
      "has_report": true
    }
  ]
}
```

**Errors:**
- 404 if patient not found

### GET /api/v1/laboratory/patients/{patient_id}/cases
**List complete cases for a given patient (order + samples + report meta) and return patient (full)**

**Response:**
```json
{
  "patient": {
    "id": "patient-uuid",
    "tenant_id": "tenant-uuid",
    "branch_id": "branch-uuid",
    "patient_code": "P001",
    "first_name": "John",
    "last_name": "Doe",
    "dob": "1990-01-01",
    "sex": "M",
    "phone": "555-1234",
    "email": "john.doe@example.com"
  },
  "patient_id": "patient-uuid",
  "cases": [
    {
      "order": {
        "id": "order-uuid",
        "order_code": "ORD001",
        "status": "RECEIVED",
        "patient_id": "patient-uuid",
        "tenant_id": "tenant-uuid",
        "branch_id": "branch-uuid",
        "requested_by": "Dr. Smith",
        "notes": "...",
        "billed_lock": false
      },
      "samples": [
        {
          "id": "sample-uuid",
          "sample_code": "SAMP001",
          "type": "SANGRE",
          "state": "RECEIVED",
          "order_id": "order-uuid",
          "tenant_id": "tenant-uuid",
          "branch_id": "branch-uuid"
        }
      ],
      "report": {
        "id": "report-uuid",
        "status": "DRAFT",
        "title": "Blood Test Report",
        "published_at": null,
        "version_no": 2,
        "has_pdf": true
      }
    }
  ]
}
```

**Errors:**
- 404 if patient not found

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
**List all samples (enriched)**

Returns `samples` array where `branch` and `order` are objects. The `order` object now includes `requested_by` and a `patient` object. `tenant_id` remains an id string.

**Response:**
```json
{
  "samples": [
    {
      "id": "sample-uuid",
      "sample_code": "SAMP001",
      "type": "SANGRE",
      "state": "RECEIVED",
      "tenant_id": "tenant-uuid",
      "branch": { "id": "branch-uuid", "name": "Main Branch", "code": "MAIN" },
      "order": {
        "id": "order-uuid",
        "order_code": "ORD001",
        "status": "RECEIVED",
        "requested_by": "Juan Carlos bodoque",
        "patient": {
          "id": "d595498b-621d-4edd-9a5d-aacd4e26cf05",
          "full_name": "Rafael Magaña",
          "patient_code": "Rima2510"
        }
      }
    }
  ]
}
```

### GET /api/v1/laboratory/samples/{sample_id}
**Get sample detail (enriched)**

**Response:**
```json
{
  "id": "sample-uuid",
  "sample_code": "SAMP001",
  "type": "SANGRE",
  "state": "RECEIVED",
  "collected_at": "2025-08-18T10:00:00Z",
  "received_at": "2025-08-18T11:00:00Z",
  "notes": "Blood sample",
  "tenant_id": "tenant-uuid",
  "branch": { "id": "branch-uuid", "name": "Main Branch", "code": "MAIN" },
  "order": { "id": "order-uuid", "order_code": "ORD001", "status": "RECEIVED" },
  
  "patient": { "id": "patient-uuid", "full_name": "John Doe", "patient_code": "P001" }
}
```

### POST /api/v1/laboratory/samples/{sample_id}/images
**Upload a sample image (RAW or regular) and create renditions**

**Request:**
- Content-Type: `multipart/form-data`
- Body: field `file` with the image file

**Size Limits:**
- Regular images (JPG/PNG/WebP, etc.): up to 50MB
- RAW formats (`.cr2`, `.cr3`, `.nef`, `.nrw`, `.arw`, `.sr2`, `.raf`, `.rw2`, `.orf`, `.pef`, `.dng`): up to 500MB

If the file exceeds the limit, the server returns `413` with a message: `{"detail": "Request body too large...", "type": "request_entity_too_large"}`.

**Behavior:**
- RAW formats (e.g., CR2/NEF/ARW/DNG): store RAW original + processed JPEG + thumbnail
- Regular images (e.g., JPG/PNG/WebP): store processed JPEG + thumbnail

**Response:**
```json
{
  "message": "Image uploaded successfully",
  "sample_image_id": "image-uuid",
  "filename": "photo.dng",
  "is_raw": true,
  "file_size": 4567890,
  "urls": {
    "processed": "https://cdn.example.com/samples/<tenant>/<branch>/<sample>/processed/file.jpg",
    "thumbnail": "https://cdn.example.com/samples/<tenant>/<branch>/<sample>/thumbnails/file.jpg",
    "raw": "https://cdn.example.com/samples/<tenant>/<branch>/<sample>/raw/file.dng"
  }
}
```

### GET /api/v1/laboratory/samples/{sample_id}/images
**List sample images and rendition URLs**

**Response:**
```json
{
  "sample_id": "sample-uuid",
  "images": [
    {
      "id": "image-uuid",
      "label": null,
      "is_primary": false,
      "created_at": "2025-08-18T10:00:00Z",
      "urls": {
        "processed": "https://cdn.example.com/.../processed/file.jpg",
        "thumbnail": "https://cdn.example.com/.../thumbnails/file.jpg",
        "original_raw": "https://cdn.example.com/.../raw/file.dng"
      }
    }
  ]
}
```

## 📋 Report Management

Headers: `Authorization: Bearer <token>`

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
  "published_at": "2025-08-18T12:00:00Z",
  "created_by": "user-uuid-here",
  "report": {
    "tipo": "citologia_mamaria",
    "base": {},
    "secciones": {},
    "flags": {},
    "images": []
  }
}
```

**Notes:**
- `created_by` is optional and must be a UUID (user id) if provided.
- `published_at` is optional ISO 8601 datetime.
- If `report` is provided, the JSON body is stored in S3 and an initial version (version_no=1) is created and marked as current (`is_current=true`).

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
**List all reports (enriched)**

Returns `reports` array with enriched `branch`, `order`, and `patient` objects plus version metadata.

**Response:**
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

### GET /api/v1/reports/{report_id}
**Get report details**

**Response:**
```json
{
  "id": "report-uuid",
  "version_no": 2,
  "status": "DRAFT",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "title": "Blood Test Report",
  "diagnosis_text": "Normal blood count results",
  "published_at": null,
  "created_by": "user-uuid-here",
  "report": {
    "tipo": "citologia_mamaria",
    "base": {},
    "secciones": {},
    "flags": {},
    "images": []
  }
}
```

### POST /api/v1/reports/{report_id}/new_version
**Create a new version for an existing report**

Path param:
- `report_id`: UUID of the report

**Request Body (same shape as report creation):**
```json
{
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "order_id": "order-uuid-here",
  "title": "Blood Test Report",
  "diagnosis_text": "Normal blood count results",
  "published_at": "2025-08-18T12:00:00Z",
  "created_by": "user-uuid-here",
  "report": { "tipo": "citologia_mamaria", "base": {}, "secciones": {}, "flags": {}, "images": [] }
}
```

**Behavior:**
- Increments `version_no` from the current version.
- Marks previous `is_current` as false; new version becomes `is_current=true`.
- If `report` is included, uploads JSON to S3 and links it to the version.

**Response:**
```json
{
  "id": "version-uuid",
  "version_no": 2,
  "report_id": "report-uuid-here",
  "is_current": true
}
```

### GET /api/v1/reports/{report_id}/versions
**List versions for a specific report**

**Response:**
```json
[
  { "id": "version-uuid-1", "version_no": 1, "report_id": "report-uuid-here", "is_current": false },
  { "id": "version-uuid-2", "version_no": 2, "report_id": "report-uuid-here", "is_current": true }
]
```

### GET /api/v1/reports/{report_id}/{version_no}
**Get details for a specific report version (same shape as detail)**

**Response:**
```json
{
  "id": "report-uuid",
  "version_no": 1,
  "status": "DRAFT",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "title": "Blood Test Report",
  "diagnosis_text": "Normal blood count results",
  "published_at": null,
  "created_by": "user-uuid-here",
  "report": { "tipo": "citologia_mamaria", "base": {}, "secciones": {}, "flags": {}, "images": [] }
}
```

### POST /api/v1/reports/{report_id}/versions/{version_no}/pdf
**Upload a PDF file to a specific report version**

**Request:**
- Content-Type: `multipart/form-data`
- Body: field `file` with the PDF file

**Behavior:**
- Validates that the report and version exist
- Uploads the PDF to S3 under a deterministic key
- Creates a `storage_object` record and assigns `pdf_storage_id` in the target version

**Size Limit:**
- PDF up to 50MB. Larger uploads will return `413`.

**Response:**
```json
{
  "version_id": "version-uuid",
  "version_no": 2,
  "report_id": "report-uuid",
  "pdf_storage_id": "storage-uuid",
  "pdf_key": "reports/<tenant>/<branch>/<report>/versions/2/report.pdf",
  "pdf_url": "https://bucket.s3.region.amazonaws.com/reports/.../report.pdf"
}
```

**Errors:**
- 400 if uploaded file is not a PDF or is empty
- 404 if report or version is not found

### GET /api/v1/reports/{report_id}/versions/{version_no}/pdf
**Get a presigned URL for the PDF of a specific report version**

**Response:**
```json
{
  "version_id": "version-uuid",
  "version_no": 2,
  "report_id": "report-uuid",
  "pdf_storage_id": "storage-uuid",
  "pdf_key": "reports/<tenant>/<branch>/<report>/versions/2/report.pdf",
  "pdf_url": "https://...presigned-url..."
}
```

**Errors:**
- 404 if report, version, or PDF is not found

### POST /api/v1/reports/{report_id}/pdf
**Upload a PDF file to the newest version of a report**

**Request:**
- Content-Type: `multipart/form-data`
- Body: field `file` with the PDF file

**Behavior:**
- Selects the newest version by highest `version_no`
- If no versions exist, returns 404
- Uploads the PDF to S3 and updates `pdf_storage_id` on that version

**Size Limit:**
- PDF up to 50MB. Larger uploads will return `413`.

**Response:**
```json
{
  "version_id": "version-uuid",
  "version_no": 3,
  "report_id": "report-uuid",
  "pdf_storage_id": "storage-uuid",
  "pdf_key": "reports/<tenant>/<branch>/<report>/versions/3/report.pdf",
  "pdf_url": "https://bucket.s3.region.amazonaws.com/reports/.../report.pdf"
}
```

**Errors:**
- 400 if uploaded file is not a PDF or is empty
- 404 if report not found or it has no versions yet

### GET /api/v1/reports/{report_id}/pdf
**Get a presigned URL for the PDF of the newest report version**

**Response:**
```json
{
  "version_id": "version-uuid",
  "version_no": 3,
  "report_id": "report-uuid",
  "pdf_storage_id": "storage-uuid",
  "pdf_key": "reports/<tenant>/<branch>/<report>/versions/3/report.pdf",
  "pdf_url": "https://...presigned-url..."
}
```

**Errors:**
- 404 if report not found, report has no versions, or the latest version has no PDF
- 403 if report access is blocked due to pending payment

### POST /api/v1/reports/{report_id}/submit
**Submit a report for review (DRAFT → IN_REVIEW)**

**Request Body:**
```json
{
  "changelog": "Initial submission for pathologist review"
}
```

**Response:**
```json
{
  "id": "report-uuid",
  "status": "IN_REVIEW",
  "message": "Report submitted for review"
}
```

**Notes:**
- Transitions report from DRAFT to IN_REVIEW status
- Creates an audit log entry
- Changelog is optional but recommended
- Only reports in DRAFT status can be submitted

### POST /api/v1/reports/{report_id}/approve
**Approve a report (IN_REVIEW → APPROVED) - Pathologist only**

**Request Body:**
```json
{
  "changelog": "Report approved after review"
}
```

**Response:**
```json
{
  "id": "report-uuid",
  "status": "APPROVED",
  "message": "Report approved"
}
```

**Notes:**
- Only pathologists can approve reports
- Transitions report from IN_REVIEW to APPROVED status
- Creates an audit log entry
- Changelog is optional but recommended

### POST /api/v1/reports/{report_id}/request-changes
**Request changes on a report (IN_REVIEW → DRAFT) - Pathologist only**

**Request Body:**
```json
{
  "comment": "Please revise the diagnosis section and add more details about the findings"
}
```

**Response:**
```json
{
  "id": "report-uuid",
  "status": "DRAFT",
  "message": "Changes requested, report returned to draft"
}
```

**Notes:**
- Only pathologists can request changes
- Transitions report from IN_REVIEW back to DRAFT status
- Comment is required to explain what changes are needed
- Creates an audit log entry with the comment

### POST /api/v1/reports/{report_id}/sign
**Sign and publish a report (APPROVED → PUBLISHED) - Pathologist only**

**Request Body:**
```json
{
  "changelog": "Final review complete, report signed and published"
}
```

**Response:**
```json
{
  "id": "report-uuid",
  "status": "PUBLISHED",
  "message": "Report signed and published"
}
```

**Notes:**
- Only pathologists can sign reports
- Transitions report from APPROVED to PUBLISHED status
- Sets `signed_by` to current pathologist's user ID
- Sets `signed_at` timestamp
- Sets `published_at` timestamp on the report
- Creates an audit log entry
- Changelog is optional but recommended

### POST /api/v1/reports/{report_id}/retract
**Retract a published report (PUBLISHED → RETRACTED) - Pathologist only**

**Request Body:**
```json
{
  "changelog": "Report retracted due to error in diagnosis"
}
```

**Response:**
```json
{
  "id": "report-uuid",
  "status": "RETRACTED",
  "message": "Report retracted"
}
```

**Notes:**
- Only pathologists can retract reports
- Transitions report from PUBLISHED to RETRACTED status
- Creates an audit log entry
- Changelog is optional but recommended
- Used when a published report needs to be withdrawn

### GET /api/v1/reports/worklist
**Get worklist of reports in review for pathologist**

**Query Parameters:**
- `branch_id` (optional): Filter by specific branch

**Response:**
```json
{
  "reports": [
    {
      "id": "report-uuid",
      "status": "IN_REVIEW",
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
      "published_at": null,
      "created_at": "2025-08-18T10:00:00Z",
      "created_by": "user-uuid",
      "version_no": 2,
      "has_pdf": true
    }
  ]
}
```

**Notes:**
- Returns all reports with IN_REVIEW status
- Primarily for pathologists to see what needs review
- Optional branch filter for multi-branch organizations
- Returns enriched data with branch, order, and patient information

## 💰 Billing Management

Headers: `Authorization: Bearer <token>`

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

### GET /api/v1/billing/invoices/{invoice_id}/full
**Get invoice details with items and payments**

**Response:**
```json
{
  "id": "invoice-uuid",
  "invoice_number": "INV001",
  "amount_total": 1500.0,
  "currency": "MXN",
  "status": "PARTIAL",
  "order_id": "order-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "branch_id": "branch-uuid-here",
  "issued_at": "2025-08-18T01:50:51.386774",
  "items": [
    {
      "id": "item-uuid",
      "invoice_id": "invoice-uuid",
      "service_id": "service-uuid",
      "description": "Biopsy Analysis",
      "quantity": 1,
      "unit_price": 1500.0,
      "subtotal": 1500.0
    }
  ],
  "payments": [
    {
      "id": "payment-uuid",
      "amount_paid": 750.0,
      "method": "credit_card",
      "invoice_id": "invoice-uuid",
      "tenant_id": "tenant-uuid",
      "branch_id": "branch-uuid"
    }
  ],
  "balance": 750.0
}
```

**Notes:**
- Returns complete invoice information including all line items and payments
- `balance` is calculated as total minus sum of all payments
- Useful for displaying detailed invoice information with payment history

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

### GET /api/v1/billing/orders/{order_id}/balance
**Get payment balance for an order**

**Response:**
```json
{
  "order_id": "order-uuid",
  "total_invoiced": 3000.0,
  "total_paid": 2250.0,
  "balance": 750.0,
  "is_locked": true,
  "invoices": [
    {
      "id": "invoice-uuid-1",
      "invoice_number": "INV001",
      "amount_total": 1500.0,
      "status": "PAID",
      "balance": 0.0
    },
    {
      "id": "invoice-uuid-2",
      "invoice_number": "INV002",
      "amount_total": 1500.0,
      "status": "PARTIAL",
      "balance": 750.0
    }
  ]
}
```

**Notes:**
- Returns aggregated payment information for all invoices related to an order
- `total_invoiced`: Sum of all invoice amounts for the order
- `total_paid`: Sum of all payments made across all invoices
- `balance`: Remaining amount to be paid (always >= 0)
- `is_locked`: Indicates if the order is locked due to pending payment (report access blocked)
- `invoices`: Array of all invoices with their individual balances
- Useful for checking order payment status and managing billing locks

## 📊 Dashboard Endpoints

Headers: `Authorization: Bearer <token>`

### GET /api/v1/dashboard/
**Get dashboard statistics and recent activity**

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
    "Service catalog",
    "Invoice line items",
    "Event timeline tracking",
    "User invitations",
    "Password reset",
    "User profiles with avatars",
    "Tenant branding",
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

## 🛒 Service Catalog Endpoints

### GET /api/v1/billing/catalog
**List all service catalog items**

**Response:**
```json
[
  {
    "id": "service-uuid",
    "tenant_id": "tenant-uuid",
    "service_name": "Biopsy Analysis",
    "service_code": "BIO-001",
    "description": "Complete biopsy analysis service",
    "price": 1500.00,
    "currency": "MXN",
    "is_active": true,
    "valid_from": "2025-01-01T00:00:00Z",
    "valid_until": null,
    "created_at": "2025-01-01T00:00:00Z"
  }
]
```

### POST /api/v1/billing/catalog
**Create a new service catalog item**

**Request Body:**
```json
{
  "service_name": "Biopsy Analysis",
  "service_code": "BIO-001",
  "description": "Complete biopsy analysis service",
  "price": 1500.00,
  "currency": "MXN",
  "is_active": true,
  "valid_from": "2025-01-01T00:00:00Z",
  "valid_until": null
}
```

**Response:** Returns the created service catalog item.

### PUT /api/v1/billing/catalog/{id}
**Update a service catalog item**

**Request Body:**
```json
{
  "service_name": "Updated Service Name",
  "price": 1800.00,
  "is_active": false
}
```

**Response:** Returns the updated service catalog item.

### DELETE /api/v1/billing/catalog/{id}
**Delete a service catalog item**

**Response:**
```json
{
  "message": "Service deleted successfully"
}
```

## 📄 Invoice Items Endpoints

### GET /api/v1/billing/invoices/{invoice_id}/items
**List all items for an invoice**

**Response:**
```json
[
  {
    "id": "item-uuid",
    "tenant_id": "tenant-uuid",
    "invoice_id": "invoice-uuid",
    "service_id": "service-uuid",
    "description": "Biopsy Analysis",
    "quantity": 1,
    "unit_price": 1500.00,
    "subtotal": 1500.00,
    "created_at": "2025-01-01T00:00:00Z"
  }
]
```

### POST /api/v1/billing/invoices/{invoice_id}/items
**Add an item to an invoice**

**Request Body:**
```json
{
  "service_id": "service-uuid",
  "description": "Biopsy Analysis",
  "quantity": 1,
  "unit_price": 1500.00
}
```

**Response:** Returns the created invoice item.

## 👥 Portal Endpoints

### POST /api/v1/portal/invite
**Send a user invitation**

**Request Body:**
```json
{
  "email": "newuser@example.com",
  "full_name": "New User",
  "role": "lab_tech"
}
```

**Response:**
```json
{
  "id": "invitation-uuid",
  "email": "newuser@example.com",
  "token": "invitation-token",
  "expires_at": "2025-01-08T00:00:00Z",
  "message": "Invitation sent successfully"
}
```

### POST /api/v1/portal/accept-invitation
**Accept an invitation and create user account**

**Request Body:**
```json
{
  "token": "invitation-token",
  "password": "SecurePassword123!"
}
```

**Response:**
```json
{
  "id": "user-uuid",
  "email": "newuser@example.com",
  "full_name": "New User",
  "role": "lab_tech",
  "message": "Account created successfully"
}
```

### POST /api/v1/portal/request-password-reset
**Request a password reset token**

**Request Body:**
```json
{
  "email": "user@example.com"
}
```

**Response:**
```json
{
  "message": "Password reset email sent"
}
```

### POST /api/v1/portal/reset-password
**Reset password using a token**

**Request Body:**
```json
{
  "token": "reset-token",
  "new_password": "NewSecurePassword123!"
}
```

**Response:**
```json
{
  "message": "Password reset successfully"
}
```

## 📋 Event Timeline Endpoints

### GET /api/v1/laboratory/orders/{order_id}/events
**Get event timeline for an order**

**Response:**
```json
[
  {
    "id": "event-uuid",
    "tenant_id": "tenant-uuid",
    "branch_id": "branch-uuid",
    "order_id": "order-uuid",
    "event_type": "ORDER_CREATED",
    "description": "Order created by lab tech",
    "event_metadata": {
      "order_code": "ORD-001",
      "patient_name": "John Doe"
    },
    "created_by": "user-uuid",
    "created_at": "2025-01-01T10:00:00Z"
  },
  {
    "id": "event-uuid-2",
    "tenant_id": "tenant-uuid",
    "branch_id": "branch-uuid",
    "order_id": "order-uuid",
    "event_type": "SAMPLE_RECEIVED",
    "description": "Sample received and logged",
    "event_metadata": {
      "sample_code": "SMP-001",
      "sample_type": "BIOPSIA"
    },
    "created_by": "user-uuid",
    "created_at": "2025-01-01T11:30:00Z"
  }
]
```

### POST /api/v1/laboratory/orders/{order_id}/events
**Add an event to the order timeline**

**Request Body:**
```json
{
  "event_type": "STATUS_CHANGED",
  "description": "Order status changed to PROCESSING",
  "event_metadata": {
    "old_status": "RECEIVED",
    "new_status": "PROCESSING",
    "reason": "Sample preparation started"
  }
}
```

**Response:** Returns the created event.

## 📚 Data Models

### Common Fields
All entities include these common fields:
- `id`: Unique identifier (UUID)
- `tenant_id`: Associated tenant
- `branch_id`: Associated branch (where applicable)
- `created_at`: Creation timestamp
- `updated_at`: Last update timestamp

### ID and Date Types
- All `id` and `*_id` fields are UUID strings.
- Date-time fields use ISO 8601 format (e.g., `2025-08-18T12:30:00Z`).
- `created_at` and `updated_at` are server-generated and MUST NOT be sent in request bodies.

### Status Enums
- **Order Status**: `RECEIVED`, `PROCESSING`, `DIAGNOSIS`, `REVIEW`, `RELEASED`, `CLOSED`, `CANCELLED`
- **Sample State**: uses Order Status values for lifecycle: `RECEIVED`, `PROCESSING`, `DIAGNOSIS`, `REVIEW`, `RELEASED`, `CLOSED`, `CANCELLED`
- **Report Status**: `DRAFT`, `IN_REVIEW`, `APPROVED`, `PUBLISHED`, `RETRACTED`
- **Invoice Status**: `PENDING`, `PAID`, `FAILED`, `REFUNDED`, `PARTIAL`
- **User Role**: `admin`, `pathologist`, `lab_tech`, `assistant`, `billing`, `viewer`

## 🔐 Authentication

### JWT Token Format
All authenticated endpoints require a Bearer token in the Authorization header:
```
Authorization: Bearer <jwt_token>
```

### Token Expiration
JWT tokens expire after 480 minutes (8 hours) by default. Use the logout endpoint to invalidate tokens before expiration.

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

**Note: All POST endpoints use JSON request bodies for optimal data handling, validation, and consistent API design, except the image upload endpoint and the report PDF upload endpoints which use multipart/form-data.**
