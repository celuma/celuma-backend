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
- **Logo URL**: Optional organization logo URL (max 500 characters)
- **Active Status**: Boolean flag for tenant activation state
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
- **Avatar URL**: Optional profile picture URL (max 500 characters)
- Can be associated with multiple branches through UserBranch
- Roles are assigned through the RBAC system (see section 8 below) — the `app_user` table does **not** have a `role` column

#### UserInvitation
- User invitation system for onboarding
- Generates unique tokens for invitation links
- Tracks invitation status and expiration
- Links to inviting user for audit trail

#### PasswordResetToken
- Secure password reset system
- Time-limited tokens for password recovery
- Tracks usage and IP addresses
- Automatic expiration for security

### 2. Patient Management

#### Patient
- Patient information scoped to tenant and branch
- Unique patient codes per tenant
- Basic demographic and contact information

### 3. Laboratory Operations

#### Order (formerly LabOrder)
- Laboratory test orders
- Tracks order status through workflow (OrderStatus enum)
- Links patients to samples, reports, and invoices via direct FK fields
- Supports billing locks for payment control
- Supports collaboration via the `Assignment` and `ReportReview` tables (not column arrays)
- Fields:
  - `order_code` (string, max 100 chars): Visible order identifier, unique per branch
  - `status` (OrderStatus): Current workflow status
  - `requested_by` (string, optional): External requesting physician name or email
  - `notes` (text, optional): Order notes
  - `billed_lock` (boolean): When true, blocks report PDF access due to pending payments
  - `created_by` (UUID, optional): User who created the order
  - `report_id` (UUID, optional): Direct FK to linked Report
  - `invoice_id` (UUID, optional): Direct FK to linked Invoice
  - `study_type_id` (UUID, optional): FK to associated StudyType
- Assignees are managed through the `Assignment` table (`item_type = lab_order`)
- Reviewers are managed through the `ReportReview` table
- Lock is removed when all invoices for the order are fully paid

#### Sample
- Physical samples associated with orders
- Tracks sample type and state using dedicated SampleState enum
- Tracks collection and reception timestamps
- Can have multiple images with renditions
- Can have labels for categorization (plus inherits order labels)
- Supports collaboration via the `Assignment` table (not a column array)
- Fields:
  - `sample_code` (string, max 100 chars): Unique identifier per order
  - `type` (SampleType): Type of sample (SANGRE, BIOPSIA, etc.)
  - `state` (SampleState): Current sample state (RECEIVED, PROCESSING, READY, DAMAGED, CANCELLED)
  - `collected_at` (timestamp, optional): When sample was collected
  - `received_at` (timestamp, optional): When sample was received at lab
  - `notes` (text, optional): Sample notes
- Assignees are managed through the `Assignment` table (`item_type = sample`)

#### OrderComment
- Comments and conversation threads on laboratory orders
- Supports mentions to notify users
- Tracks creation, editing, and soft deletion
- Fields:
  - `text` (required, max 5000 characters)
  - `comment_metadata` (optional JSON for additional data)
  - `edited_at` (nullable timestamp)
  - `deleted_at` (nullable timestamp for soft deletion)
  - `created_by` (user who created the comment)

#### OrderCommentMention
- Junction table for user mentions in order comments
- Links comments to mentioned users
- Composite primary key (comment_id, user_id)

#### Label
- Categorization labels for orders and samples
- Tenant-scoped label definitions
- Fields:
  - `name` (required, max 100 characters)
  - `color` (hex color code, e.g., #FF5733)
  - `updated_at` (timestamp)

#### LabOrderLabel
- Junction table for order-label many-to-many relationship
- Composite primary key (order_id, label_id)
- Cascade delete on order or label removal

#### SampleLabel
- Junction table for sample-label many-to-many relationship
- Additional labels specific to samples (order labels are inherited)
- Composite primary key (sample_id, label_id)
- Cascade delete on sample or label removal

#### SampleImage
- Links samples to storage objects (S3)
- Supports labeling and primary image designation
- Enables image renditions for different purposes
- Fields:
  - `storage_id` (UUID): Reference to StorageObject
  - `label` (string, max 255 chars, optional): Image label (macro/micro, staining type, etc.)
  - `is_primary` (boolean): Whether this is the primary image for the sample
  - `created_by` (UUID, optional): User who uploaded the image

#### SampleImageRendition
- Processed versions of sample images for different use cases
- Linked to parent SampleImage
- Fields:
  - `sample_image_id` (UUID): Reference to parent SampleImage
  - `kind` (string, max 50 chars): Type of rendition
    - `thumbnail`: Small preview image
    - `processed`: Standard processed JPEG
    - `raw`: Original RAW file (CR2, NEF, ARW, DNG, etc.)
    - `tile`: Tiled version for deep zoom
    - `webp`: WebP optimized version
  - `storage_id` (UUID): Reference to StorageObject containing the rendition

### 4. Reporting System

#### Report
- Laboratory reports with status tracking
- Links to orders and contains diagnosis text
- Supports versioning through ReportVersion

#### ReportVersion
- Versioned reports with JSON (report body), PDF and HTML storage
- Tracks authorship and changes
- Maintains current version flags
- Supports digital signatures for published reports
- Fields:
  - `json_storage_id` (nullable, S3 JSON body)
  - `pdf_storage_id` (nullable)
  - `html_storage_id` (nullable)
  - `authored_by` (user who created the version)
  - `signed_by` (pathologist who signed, nullable)
  - `signed_at` (timestamp of signature, nullable)
  - `is_current` (boolean flag for active version)
  - `changelog` (optional notes about version changes)

#### ReportTemplate
- Report templates stored in JSON format
- Tenant-scoped template definitions
- Supports active/inactive status for template management
- Fields:
  - `name` (required, max 255 characters)
  - `description` (optional)
  - `template_json` (required, JSON object storing template structure)
  - `created_by` (user who created the template, nullable)
  - `is_active` (boolean, default true)
  - `created_at` (timestamp)

#### Assignment
- Tracks user assignments to orders, samples, and reports
- Replaces the legacy `assignees`/`reviewers` UUID array columns
- Fields:
  - `item_type` (AssignmentItemType): `lab_order`, `sample`, or `report`
  - `item_id` (UUID): ID of the assigned item
  - `assignee_user_id` (UUID): User being assigned
  - `assigned_by_user_id` (UUID, optional): User who performed the assignment
  - `assigned_at` (timestamp): When the assignment was created
  - `unassigned_at` (timestamp, optional): When the assignment was removed

#### ReportReview
- Tracks individual reviewer decisions per lab order
- Reviewers can be assigned before a report exists (`report_id` is nullable)
- MVP rule: ≥1 approved decision promotes the report to APPROVED status
- Fields:
  - `order_id` (UUID): Order being reviewed
  - `report_id` (UUID, optional): Specific report version under review
  - `reviewer_user_id` (UUID): User performing the review
  - `assigned_by_user_id` (UUID, optional): User who assigned the reviewer
  - `assigned_at` (timestamp): When the reviewer was assigned
  - `decision_at` (timestamp, optional): When the decision was made
  - `status` (ReviewStatus): `PENDING`, `APPROVED`, or `REJECTED`

### 5. Storage Management

#### StorageObject
- Abstracts cloud storage (S3, GCP, Azure)
- Stores metadata about files (size, hash, content type)
- Supports versioning and deduplication

### 6. Billing System

#### PriceCatalog
- Pricing catalog per study type
- Links study types to unit prices
- Tenant-scoped pricing definitions
- Supports time-based pricing validity (effective_from/effective_to)
- Active/inactive status for price management
- Currency support (default MXN)

#### Invoice
- Billing records for laboratory orders
- Tracks amounts, currency, and payment status
- Unique invoice numbers per branch

#### InvoiceItem
- Detailed line items for invoices
- Custom descriptions and quantities
- Calculates subtotals for each item

#### Payment
- Payment records linked to invoices
- Supports multiple payment methods
- Tracks payment amounts and timing

### 8. RBAC System (Role-Based Access Control)

The RBAC system replaces the legacy `role` column on `app_user`. Roles are now separate entities with granular permissions, allowing custom role creation in the future.

#### Permission
- Atomic capability granted to users via roles
- Globally defined (not tenant-scoped)
- Fields:
  - `code` (unique string, e.g., `lab:read`, `reports:sign`, `admin:manage_users`)
  - `domain` (grouping, e.g., `lab`, `reports`, `admin`, `billing`, `portal`, `audit`)
  - `display_name` (human-readable label)
  - `description` (optional detail)

#### Role
- Named collection of permissions, optionally scoped to a tenant
- System roles (`is_system=true`) are seeded and available to all tenants
- Tenant-scoped custom roles (`tenant_id != null`) are for future use
- Fields:
  - `code` (unique string, e.g., `admin`, `pathologist`, `lab_tech`)
  - `name` (display name)
  - `description` (optional)
  - `is_system` (boolean)
  - `is_protected` (boolean — system roles cannot be deleted)
  - `tenant_id` (nullable UUID — null for global system roles)

**Seeded system roles:**

| Code | Name | Key Permissions |
|------|------|-----------------|
| `superuser` | Superadministrador | All permissions |
| `admin` | Administrador | All except `reports:sign` / clinical actions |
| `pathologist` | Patólogo | Full reports domain + lab read |
| `lab_tech` | Técnico de Laboratorio | Lab operations, images, samples |
| `billing` | Facturación | Billing domain + lab read |
| `assistant` | Asistente | Lab read + create orders/patients/samples |
| `viewer` | Solo Lectura | Read-only (`lab:read`, `reports:read`, `billing:read`) |
| `physician` | Médico Solicitante | `portal:physician_access` |
| `auditor` | Auditor | `audit:read_auditlog`, `audit:read_events` |

#### RolePermission
- Junction table linking roles to their permissions
- Composite primary key (`role_id`, `permission_id`)

#### UserRoleLink
- Junction table assigning roles to users
- Unique constraint `uq_user_role` on (`user_id`, `role_id`)
- A user can hold multiple roles simultaneously (role union for permission resolution)

### 9. Audit and Event System

#### AuditLog
- Complete audit trail for compliance
- Tracks all entity changes with before/after values
- Links to users, branches, and specific entities

#### OrderEvent
- Timeline tracking for order and sample workflow
- Records all significant events for orders and samples
- Stores event metadata as JSON for flexibility
- Links to orders, samples, users, and branches for context
- Fields:
  - `order_id` (UUID): The order this event belongs to
  - `sample_id` (UUID, optional): Specific sample for sample-level events
  - `event_type` (EventType): Type of event
  - `description` (string, max 500 chars): Human-readable description
  - `event_metadata` (JSON, optional): Additional structured event data
  - `created_by` (UUID, optional): User who triggered the event

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
AppUser (N) ←→ (N) Role (through UserRoleLink)
Role (N) ←→ (N) Permission (through RolePermission)
AppUser (1) ←→ (N) Order (created_by)
AppUser (1) ←→ (N) Report (created_by)
AppUser (1) ←→ (N) ReportVersion (authored_by)
AppUser (1) ←→ (N) Assignment (assignee_user_id)
AppUser (1) ←→ (N) ReportReview (reviewer_user_id)
AppUser (1) ←→ (N) OrderComment (created_by)

Patient (1) ←→ (N) Order
Order (1) ←→ (N) Sample
Order (1) ←→ (1) Report (via order.report_id)
Order (1) ←→ (1) Invoice (via order.invoice_id)
Order (1) ←→ (N) Assignment (item_type=lab_order)
Order (1) ←→ (N) ReportReview
Sample (1) ←→ (N) Assignment (item_type=sample)

Sample (1) ←→ (N) SampleImage
SampleImage (1) ←→ (N) SampleImageRendition

Report (1) ←→ (N) ReportVersion
ReportVersion (1) ←→ (1) StorageObject (JSON body)
ReportVersion (1) ←→ (1) StorageObject (PDF)
ReportVersion (1) ←→ (1) StorageObject (HTML)

Tenant (1) ←→ (N) ReportTemplate

Order (1) ←→ (N) OrderComment
OrderComment (N) ←→ (N) AppUser (through OrderCommentMention)

Tenant (1) ←→ (N) Label
Order (N) ←→ (N) Label (through OrderLabel)
Sample (N) ←→ (N) Label (through SampleLabel)

Invoice (1) ←→ (N) Payment
```

## Enums

### User Roles (RBAC — not a DB enum)
Roles are stored as rows in the `role` table, not as a PostgreSQL enum. See section 8 for the full seeded role catalog. The `app_user` table does not have a `role` column.

**System role codes:** `superuser`, `admin`, `pathologist`, `lab_tech`, `billing`, `assistant`, `viewer`, `physician`, `auditor`

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

### SampleState
Sample-specific lifecycle states (independent from OrderStatus):
- `RECEIVED`: Sample received at lab (default when created)
- `PROCESSING`: Sample is being processed (e.g., images uploaded)
- `READY`: Sample is ready for diagnosis
- `DAMAGED`: Sample is physically damaged and cannot be used
- `CANCELLED`: Sample cancelled for other reasons

**State Transitions:**
- RECEIVED → PROCESSING (when processing begins or images uploaded)
- PROCESSING → READY (when sample preparation is complete)
- RECEIVED/PROCESSING → DAMAGED (if sample is damaged)
- RECEIVED/PROCESSING → CANCELLED (if sample is cancelled)

### ReportStatus
- `DRAFT`: Initial draft (can be edited freely)
- `IN_REVIEW`: Under review by pathologist
- `APPROVED`: Approved by pathologist (ready for signing)
- `PUBLISHED`: Signed and published to patient (immutable)
- `RETRACTED`: Report retracted (withdrawn after publication)

**Workflow Transitions:**
- DRAFT → IN_REVIEW (submit for review)
- IN_REVIEW → APPROVED (pathologist approves)
- IN_REVIEW → DRAFT (pathologist requests changes)
- APPROVED → PUBLISHED (pathologist signs)
- PUBLISHED → RETRACTED (pathologist retracts)

### ReviewStatus
Status for individual reviewer decisions on a `ReportReview` record:
- `PENDING`: Reviewer has been assigned but has not yet decided
- `APPROVED`: Reviewer approved the report
- `REJECTED`: Reviewer rejected the report (changes requested)

### AssignmentItemType
Item types that can have assignments in the `Assignment` table:
- `lab_order`: Assignment applies to a laboratory order
- `sample`: Assignment applies to a sample
- `report`: Assignment applies to a report

### PaymentStatus
- `PENDING`: Payment pending
- `PAID`: Payment received
- `FAILED`: Payment failed
- `REFUNDED`: Payment refunded
- `PARTIAL`: Partial payment
- `VOID`: Payment voided/cancelled

### EventType
- **Order Events**:
  - `ORDER_CREATED`: New order created
  - `ORDER_DELIVERED`: Results delivered
  - `ORDER_CANCELLED`: Order cancelled
  - `ORDER_STATUS_CHANGED`: Order status changed
  - `ORDER_NOTES_UPDATED`: Order notes updated
- **Sample Events**:
  - `SAMPLE_CREATED`: Sample created
  - `SAMPLE_RECEIVED`: Sample received at lab
  - `SAMPLE_PREPARED`: Sample preparation complete
  - `SAMPLE_STATE_CHANGED`: Sample state changed
  - `SAMPLE_NOTES_UPDATED`: Sample notes updated
  - `SAMPLE_DAMAGED`: Sample marked as damaged
  - `SAMPLE_CANCELLED`: Sample cancelled
- **Image Events**:
  - `IMAGE_UPLOADED`: Sample image uploaded
  - `IMAGE_DELETED`: Sample image deleted
- **Report Events**:
  - `REPORT_CREATED`: Report created
  - `REPORT_VERSION_CREATED`: New report version created
  - `REPORT_SUBMITTED`: Report submitted for review
  - `REPORT_APPROVED`: Report approved
  - `REPORT_CHANGES_REQUESTED`: Changes requested on report
  - `REPORT_SIGNED`: Report digitally signed
  - `REPORT_PUBLISHED`: Report published
  - `REPORT_RETRACTED`: Report retracted
- **Billing Events**:
  - `INVOICE_CREATED`: Invoice generated
  - `PAYMENT_RECEIVED`: Payment received
- **Collaboration Events**:
  - `ASSIGNEES_ADDED`: Assignees added to order
  - `ASSIGNEES_REMOVED`: Assignees removed from order
  - `REVIEWERS_ADDED`: Reviewers added to order
  - `REVIEWERS_REMOVED`: Reviewers removed from order
  - `LABELS_ADDED`: Labels added to order/sample
  - `LABELS_REMOVED`: Labels removed from order/sample
- **Generic Events**:
  - `STATUS_CHANGED`: General status change (legacy)
  - `NOTE_ADDED`: Note added to case
  - `COMMENT_ADDED`: Comment added to order

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

### Service Catalog
The service catalog system enables flexible pricing management:
- **Service Definitions**: Name, code, description for each service
- **Pricing**: Decimal precision for accurate amounts
- **Validity Periods**: Time-based pricing with start/end dates
- **Active Management**: Enable/disable services without deletion

### Event Timeline
Complete case history tracking:
- **Event Types**: 33 predefined event types for workflow tracking (see EventType enum)
- **Flexible Metadata**: JSON storage for event-specific data
- **Audit Trail**: Links to users and timestamps for compliance
- **Query Optimization**: Indexed for fast timeline reconstruction

### User Management Enhancements
- **Invitations**: Email-based user onboarding with expiring tokens
- **Password Reset**: Secure token-based password recovery
- **Avatar Support**: Profile picture URLs for user identification
- **Tenant Branding**: Logo URLs for organization identity

### Invoice Line Items
Detailed billing with:
- **Service Linkage**: Connect items to catalog for consistency
- **Custom Descriptions**: Override catalog descriptions as needed
- **Quantity Support**: Handle multiple units of same service
- **Automatic Subtotals**: Calculated from unit price and quantity

### Usage Examples
```sql
-- Create user (no role column — roles are assigned via RBAC)
INSERT INTO app_user (tenant_id, username, email, full_name, hashed_password)
VALUES ('tenant-uuid', 'johndoe', 'john@example.com', 'John Doe', 'hashed_pass');

-- Assign the 'admin' role via the RBAC junction table
INSERT INTO user_role_link (user_id, role_id)
SELECT 'user-uuid', id FROM role WHERE code = 'admin';

-- Query user roles
SELECT r.code, r.name
FROM role r
JOIN user_role_link url ON url.role_id = r.id
WHERE url.user_id = 'user-uuid';
```

## Future Enhancements

- Materialized views for common queries
- Partitioning for large audit logs
- Advanced search indexing
- Real-time notifications
- API rate limiting per tenant
- Email notification system integration
- Event-driven workflows
