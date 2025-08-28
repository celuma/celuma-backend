# Celuma Backend Database Schema

## Overview

This document describes the complete database schema for the Celuma laboratory management system. The system is designed with multi-tenant architecture, supporting multiple laboratory organizations (tenants) with multiple branches each.

## Architecture Principles

- **Multi-tenant**: Each tenant operates independently with their own data
- **Multi-branch**: Each tenant can have multiple physical locations
- **Role-based access**: Users have specific roles and can access multiple branches
- **Audit trail**: Complete tracking of all system changes
- **Cloud storage integration**: S3-compatible storage for images and documents

## Core Entities

### 1. Multi-tenant Foundation

#### Tenant
- Represents a laboratory organization
- Contains basic organization information (name, legal name, tax ID)
- All other entities are scoped to a tenant

#### Branch
- Physical location within a tenant
- Each branch has its own address, timezone, and operational settings
- Users can operate across multiple branches of the same tenant

#### AppUser
- Application users with role-based access
- Scoped to a tenant
- **Authentication**: Supports both username and email authentication
- **Username Field**: Optional unique identifier per tenant (max 50 characters)
- **Email Field**: Required unique identifier per tenant
- **Flexible Login**: Users can authenticate using username OR email
- Can be associated with multiple branches through UserBranch

### 2. Patient Management

#### Patient
- Patient information scoped to tenant and branch
- Unique patient codes per tenant
- Basic demographic and contact information

### 3. Laboratory Operations

#### LabOrder
- Laboratory test orders
- Tracks order status through workflow
- Links patients to samples and reports
- Supports billing locks for payment control

#### Sample
- Physical samples associated with orders
- Tracks sample type, state, and collection/reception times
- Can have multiple images

#### SampleImage
- Links samples to storage objects (S3)
- Supports labeling and primary image designation
- Enables image renditions for different purposes

### 4. Reporting System

#### Report
- Laboratory reports with status tracking
- Links to orders and contains diagnosis text
- Supports versioning through ReportVersion

#### ReportVersion
- Versioned reports with PDF and HTML storage
- Tracks authorship and changes
- Maintains current version flags

### 5. Storage Management

#### StorageObject
- Abstracts cloud storage (S3, GCP, Azure)
- Stores metadata about files (size, hash, content type)
- Supports versioning and deduplication

### 6. Billing System

#### Invoice
- Billing records for laboratory orders
- Tracks amounts, currency, and payment status
- Unique invoice numbers per branch

#### Payment
- Payment records linked to invoices
- Supports multiple payment methods
- Tracks payment amounts and timing

### 7. Audit System

#### AuditLog
- Complete audit trail for compliance
- Tracks all entity changes with before/after values
- Links to users, branches, and specific entities

## Data Relationships

```
Tenant (1) ←→ (N) Branch
Tenant (1) ←→ (N) AppUser
Tenant (1) ←→ (N) Patient
Tenant (1) ←→ (N) LabOrder
Tenant (1) ←→ (N) Report
Tenant (1) ←→ (N) Invoice

Branch (1) ←→ (N) Patient
Branch (1) ←→ (N) LabOrder
Branch (1) ←→ (N) Report
Branch (1) ←→ (N) Invoice

AppUser (N) ←→ (N) Branch (through UserBranch)
AppUser (1) ←→ (N) LabOrder (created_by)
AppUser (1) ←→ (N) Report (created_by)
AppUser (1) ←→ (N) ReportVersion (authored_by)

Patient (1) ←→ (N) LabOrder
LabOrder (1) ←→ (N) Sample
LabOrder (1) ←→ (N) Report
LabOrder (1) ←→ (N) Invoice

Sample (1) ←→ (N) SampleImage
SampleImage (1) ←→ (N) SampleImageRendition

Report (1) ←→ (N) ReportVersion
ReportVersion (1) ←→ (1) StorageObject (PDF)
ReportVersion (1) ←→ (1) StorageObject (HTML)

Invoice (1) ←→ (N) Payment
```

## Enums

### UserRole
- `admin`: System administrator
- `pathologist`: Medical professional
- `lab_tech`: Laboratory technician
- `assistant`: Administrative assistant
- `billing`: Billing specialist
- `viewer`: Read-only access

### OrderStatus
- `RECEIVED`: Order received
- `PROCESSING`: Sample processing
- `DIAGNOSIS`: Under diagnosis
- `REVIEW`: Under review
- `RELEASED`: Results released
- `CLOSED`: Order completed
- `CANCELLED`: Order cancelled

### SampleType
- `SANGRE`: Blood sample
- `BIOPSIA`: Biopsy sample
- `LAMINILLA`: Slide sample
- `TEJIDO`: Tissue sample
- `OTRO`: Other sample types

### ReportStatus
- `DRAFT`: Initial draft
- `IN_REVIEW`: Under review
- `APPROVED`: Approved by reviewer
- `PUBLISHED`: Published to patient
- `RETRACTED`: Report retracted

### PaymentStatus
- `PENDING`: Payment pending
- `PAID`: Payment received
- `FAILED`: Payment failed
- `REFUNDED`: Payment refunded
- `PARTIAL`: Partial payment

## Performance Considerations

### Indexes
- Multi-tenant indexes on tenant_id
- Branch-specific indexes for operational queries
- Status-based indexes for workflow queries
- Storage object deduplication indexes

### Constraints
- Foreign key constraints for referential integrity
- Unique constraints for business rules
- Check constraints for data validation

## Security Features

- Tenant isolation through foreign key constraints
- Role-based access control
- Audit logging for compliance
- Secure storage object references

## Migration Notes

The database schema has been migrated from the previous simple user table to this comprehensive multi-tenant system. All existing data should be migrated to the new structure before deployment.

## Username Feature (v1.0.0)

### Overview
The username feature provides flexible authentication options for users, allowing them to authenticate using either username or email while maintaining backward compatibility.

### Database Changes
- **New Column**: `username VARCHAR(50)` added to `app_user` table
- **Index**: `ix_app_user_username` created for efficient lookups
- **Nullable**: Username field is optional and can be NULL
- **Unique per Tenant**: Username uniqueness enforced within tenant scope

### Authentication Flow
1. **Username Priority**: System first attempts authentication using username
2. **Email Fallback**: If username not found, falls back to email authentication
3. **Same Password**: Both authentication methods use the same password
4. **Tenant Scoping**: All authentication remains tenant-scoped

### Migration Safety
- **Backward Compatible**: Existing email-only users continue to work unchanged
- **No Data Loss**: All existing user data preserved
- **Gradual Adoption**: Users can add usernames later without breaking functionality

### Usage Examples
```sql
-- User with username
INSERT INTO app_user (tenant_id, username, email, full_name, role, hashed_password)
VALUES ('tenant-uuid', 'johndoe', 'john@example.com', 'John Doe', 'admin', 'hashed_pass');

-- User without username (existing behavior)
INSERT INTO app_user (tenant_id, email, full_name, role, hashed_password)
VALUES ('tenant-uuid', 'jane@example.com', 'Jane Smith', 'user', 'hashed_pass');
```

## Future Enhancements

- Materialized views for common queries
- Partitioning for large audit logs
- Advanced search indexing
- Real-time notifications
- API rate limiting per tenant
