from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from typing import Dict
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.laboratory import LabOrder, Sample, SampleImage
from app.models.storage import StorageObject, SampleImageRendition
from app.models.tenant import Tenant, Branch
from app.models.patient import Patient
from app.models.report import Report, ReportVersion
from app.models.user import AppUser
from app.schemas.laboratory import (
    LabOrderCreate,
    LabOrderResponse,
    LabOrderDetailResponse,
    SampleCreate,
    SampleResponse,
    SampleImagesListResponse,
    SampleImageInfo,
    SampleImageUploadResponse,
    LabOrderUnifiedCreate,
    LabOrderUnifiedResponse,
    LabOrderFullDetailResponse,
    PatientCasesListResponse,
    PatientCaseSummary,
    PatientOrdersListResponse,
    PatientOrderSummary,
)
from app.services.s3 import S3Service
from app.services.image_processing import process_image_bytes
from uuid import uuid4
import os
from sqlmodel import select

router = APIRouter(prefix="/laboratory")

@router.get("/orders/")
def list_orders(session: Session = Depends(get_session)):
    """List all laboratory orders"""
    orders = session.exec(select(LabOrder)).all()
    return [{
        "id": str(o.id),
        "order_code": o.order_code,
        "status": o.status,
        "patient_id": str(o.patient_id),
        "tenant_id": str(o.tenant_id),
        "branch_id": str(o.branch_id)
    } for o in orders]

@router.post("/orders/", response_model=LabOrderResponse)
def create_order(order_data: LabOrderCreate, session: Session = Depends(get_session)):
    """Create a new laboratory order"""
    # Verify tenant, branch, and patient exist
    tenant = session.get(Tenant, order_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, order_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    patient = session.get(Patient, order_data.patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")
    
    # Check if order_code is unique for this branch
    existing_order = session.exec(
        select(LabOrder).where(
            LabOrder.order_code == order_data.order_code,
            LabOrder.branch_id == order_data.branch_id
        )
    ).first()
    
    if existing_order:
        raise HTTPException(400, "Order code already exists for this branch")
    
    order = LabOrder(
        tenant_id=order_data.tenant_id,
        branch_id=order_data.branch_id,
        patient_id=order_data.patient_id,
        order_code=order_data.order_code,
        requested_by=order_data.requested_by,
        notes=order_data.notes,
        created_by=order_data.created_by
    )
    
    session.add(order)
    session.commit()
    session.refresh(order)
    
    return LabOrderResponse(
        id=str(order.id),
        order_code=order.order_code,
        status=order.status,
        patient_id=str(order.patient_id),
        tenant_id=str(order.tenant_id),
        branch_id=str(order.branch_id)
    )

@router.get("/orders/{order_id}", response_model=LabOrderDetailResponse)
def get_order(order_id: str, session: Session = Depends(get_session)):
    """Get order details"""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    return LabOrderDetailResponse(
        id=str(order.id),
        order_code=order.order_code,
        status=order.status,
        patient_id=str(order.patient_id),
        tenant_id=str(order.tenant_id),
        branch_id=str(order.branch_id),
        requested_by=order.requested_by,
        notes=order.notes,
        billed_lock=order.billed_lock
    )

@router.get("/samples/")
def list_samples(session: Session = Depends(get_session)):
    """List all samples"""
    samples = session.exec(select(Sample)).all()
    return [{
        "id": str(s.id),
        "sample_code": s.sample_code,
        "type": s.type,
        "state": s.state,
        "order_id": str(s.order_id),
        "tenant_id": str(s.tenant_id),
        "branch_id": str(s.branch_id)
    } for s in samples]

@router.post("/samples/", response_model=SampleResponse)
def create_sample(sample_data: SampleCreate, session: Session = Depends(get_session)):
    """Create a new sample"""
    # Verify tenant, branch, and order exist
    tenant = session.get(Tenant, sample_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, sample_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    order = session.get(LabOrder, sample_data.order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    # Check if sample_code is unique for this order
    existing_sample = session.exec(
        select(Sample).where(
            Sample.sample_code == sample_data.sample_code,
            Sample.order_id == sample_data.order_id
        )
    ).first()
    
    if existing_sample:
        raise HTTPException(400, "Sample code already exists for this order")
    
    sample = Sample(
        tenant_id=sample_data.tenant_id,
        branch_id=sample_data.branch_id,
        order_id=sample_data.order_id,
        sample_code=sample_data.sample_code,
        type=sample_data.type,
        notes=sample_data.notes,
        collected_at=sample_data.collected_at,
        received_at=sample_data.received_at
    )
    
    session.add(sample)
    session.commit()
    session.refresh(sample)
    
    return SampleResponse(
        id=str(sample.id),
        sample_code=sample.sample_code,
        type=sample.type,
        state=sample.state,
        order_id=str(sample.order_id),
        tenant_id=str(sample.tenant_id),
        branch_id=str(sample.branch_id)
    )


@router.post("/orders/unified", response_model=LabOrderUnifiedResponse)
def create_order_with_samples(payload: LabOrderUnifiedCreate, session: Session = Depends(get_session)):
    """Create a new laboratory order and multiple samples in one operation.

    - Validates tenant, branch, and patient
    - Ensures `order_code` uniqueness per branch
    - Creates order and all samples atomically
    """
    # Validate tenant, branch, and patient
    tenant = session.get(Tenant, payload.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    branch = session.get(Branch, payload.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    patient = session.get(Patient, payload.patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    # Ensure order_code unique per branch
    existing = session.exec(
        select(LabOrder).where(
            LabOrder.order_code == payload.order_code,
            LabOrder.branch_id == payload.branch_id,
        )
    ).first()
    if existing:
        raise HTTPException(400, "Order code already exists for this branch")

    # Create order
    order = LabOrder(
        tenant_id=payload.tenant_id,
        branch_id=payload.branch_id,
        patient_id=payload.patient_id,
        order_code=payload.order_code,
        requested_by=payload.requested_by,
        notes=payload.notes,
        created_by=payload.created_by,
    )
    session.add(order)
    session.flush()  # get order.id for samples

    # Create samples
    created_samples: list[Sample] = []
    sample_codes_in_order: set[str] = set()
    for s in payload.samples:
        # Prevent duplicate sample_code within this unified request
        if s.sample_code in sample_codes_in_order:
            raise HTTPException(400, f"Duplicate sample_code in request: {s.sample_code}")
        sample_codes_in_order.add(s.sample_code)

        # Ensure sample_code unique for this order
        existing_sample = session.exec(
            select(Sample).where(
                Sample.sample_code == s.sample_code,
                Sample.order_id == order.id,
            )
        ).first()
        if existing_sample:
            raise HTTPException(400, f"Sample code already exists for this order: {s.sample_code}")

        sample = Sample(
            tenant_id=payload.tenant_id,
            branch_id=payload.branch_id,
            order_id=order.id,
            sample_code=s.sample_code,
            type=s.type,
            notes=s.notes,
            collected_at=s.collected_at,
            received_at=s.received_at,
        )
        session.add(sample)
        created_samples.append(sample)

    # Commit transaction
    session.commit()
    session.refresh(order)
    for s in created_samples:
        session.refresh(s)

    return LabOrderUnifiedResponse(
        order=LabOrderResponse(
            id=str(order.id),
            order_code=order.order_code,
            status=order.status,
            patient_id=str(order.patient_id),
            tenant_id=str(order.tenant_id),
            branch_id=str(order.branch_id),
        ),
        samples=[
            SampleResponse(
                id=str(s.id),
                sample_code=s.sample_code,
                type=s.type,
                state=s.state,
                order_id=str(s.order_id),
                tenant_id=str(s.tenant_id),
                branch_id=str(s.branch_id),
            )
            for s in created_samples
        ],
    )


@router.post("/samples/{sample_id}/images", response_model=SampleImageUploadResponse)
def upload_sample_image(sample_id: str, file: UploadFile = File(...), session: Session = Depends(get_session)):
    """Upload an image (regular or RAW) for a sample, store S3 keys and DB mapping.

    Stores:
    - RAW original (if applicable)
    - processed JPEG
    - thumbnail JPEG
    """
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")

    filename = file.filename or "upload"
    # Basic size guard: read up to 50MB for non-RAW, 500MB for RAW candidates
    # We decide limit by extension to avoid over-reading large unexpected files
    lower = filename.lower()
    raw_exts = (".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".raf", ".rw2", ".orf", ".pef", ".dng")
    max_bytes = 500 * 1024 * 1024 if any(lower.endswith(ext) for ext in raw_exts) else 50 * 1024 * 1024

    # Read in chunks to avoid memory spikes and enforce limit
    chunk_size = 1024 * 1024
    received = 0
    chunks = []
    while True:
        chunk = file.file.read(chunk_size)
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise HTTPException(413, f"File too large. Limit is {max_bytes // (1024*1024)}MB for this file type")
        chunks.append(chunk)
    data = b"".join(chunks)

    processed = process_image_bytes(filename, data)
    is_raw = processed.original_bytes is not None

    s3 = S3Service()
    unique_id = uuid4().hex[:8]
    base_name, ext = os.path.splitext(filename)

    # Build S3 keys (tenant/branch/sample grouping)
    base_prefix = f"samples/{sample.tenant_id}/{sample.branch_id}/{sample.id}"

    # Upload processed and thumbnail
    processed_key = f"{base_prefix}/processed/{base_name}_{unique_id}.jpg"
    processed_info = s3.upload_bytes(
        processed.processed_jpeg_bytes,
        key=processed_key,
        content_type="image/jpeg",
        acl=None,
    )

    thumb_key = f"{base_prefix}/thumbnails/{base_name}_{unique_id}.jpg"
    thumb_info = s3.upload_bytes(
        processed.thumbnail_jpeg_bytes,
        key=thumb_key,
        content_type="image/jpeg",
        acl=None,
    )

    # Create StorageObject rows
    processed_storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=processed_info.bucket,
        object_key=processed_info.key,
        version_id=processed_info.version_id,
        etag=processed_info.etag,
        content_type="image/jpeg",
        size_bytes=processed_info.size_bytes,
    )
    session.add(processed_storage)
    session.flush()

    thumb_storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=thumb_info.bucket,
        object_key=thumb_info.key,
        version_id=thumb_info.version_id,
        etag=thumb_info.etag,
        content_type="image/jpeg",
        size_bytes=thumb_info.size_bytes,
    )
    session.add(thumb_storage)
    session.flush()

    # Link as SampleImage and renditions
    sample_image = SampleImage(
        tenant_id=sample.tenant_id,
        branch_id=sample.branch_id,
        sample_id=sample.id,
        storage_id=processed_storage.id,
        label=None,
        is_primary=False,
    )
    session.add(sample_image)
    session.flush()

    rendition_thumb = SampleImageRendition(
        sample_image_id=sample_image.id,
        kind="thumbnail",
        storage_id=thumb_storage.id,
    )
    session.add(rendition_thumb)

    original_storage = None
    if is_raw and processed.original_bytes:
        raw_key = f"{base_prefix}/raw/{base_name}_{unique_id}{ext.lower()}"
        raw_info = s3.upload_bytes(
            processed.original_bytes,
            key=raw_key,
            content_type=file.content_type or "application/octet-stream",
            acl=None,
        )
        original_storage = StorageObject(
            provider="aws",
            region=s3.region,
            bucket=raw_info.bucket,
            object_key=raw_info.key,
            version_id=raw_info.version_id,
            etag=raw_info.etag,
            content_type=file.content_type,
            size_bytes=raw_info.size_bytes,
        )
        session.add(original_storage)
        session.flush()
        rendition_original = SampleImageRendition(
            sample_image_id=sample_image.id,
            kind="original_raw",
            storage_id=original_storage.id,
        )
        session.add(rendition_original)

    session.commit()

    urls: Dict[str, str] = {
        "processed": s3.object_public_url(processed_key),
        "thumbnail": s3.object_public_url(thumb_key),
    }
    if is_raw and original_storage is not None:
        urls["raw"] = s3.object_public_url(original_storage.object_key)

    return SampleImageUploadResponse(
        message="Image uploaded successfully",
        sample_image_id=str(sample_image.id),
        filename=filename,
        is_raw=is_raw,
        file_size=len(data),
        urls=urls,
    )


@router.get("/samples/{sample_id}/images", response_model=SampleImagesListResponse)
def list_sample_images(sample_id: str, session: Session = Depends(get_session)):
    """List images and their URLs for a sample, similar to file_mapping.json idea."""
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")

    s3 = S3Service()

    # Fetch images and renditions with simple selects
    images = session.exec(
        select(SampleImage).where(SampleImage.sample_id == sample.id)
    ).all()

    results = []
    for img in images:
        # main processed image
        storage = session.get(StorageObject, img.storage_id)
        urls: Dict[str, str] = {}
        if storage:
            urls["processed"] = s3.object_public_url(storage.object_key)

        # renditions
        renditions = session.exec(
            select(SampleImageRendition).where(SampleImageRendition.sample_image_id == img.id)
        ).all()
        for r in renditions:
            r_storage = session.get(StorageObject, r.storage_id)
            if r_storage:
                urls[r.kind] = s3.object_public_url(r_storage.object_key)

        results.append(
            SampleImageInfo(
                id=str(img.id),
                label=img.label,
                is_primary=img.is_primary,
                created_at=str(img.created_at),
                urls=urls,
            )
        )

    return SampleImagesListResponse(sample_id=str(sample.id), images=results)


@router.get("/orders/{order_id}/full", response_model=LabOrderFullDetailResponse)
def get_order_full_detail(order_id: str, session: Session = Depends(get_session)):
    """Return complete information for an order: order details, patient details, and samples."""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    patient = session.get(Patient, order.patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    # Fetch samples for this order
    samples = session.exec(select(Sample).where(Sample.order_id == order.id)).all()

    # Build response objects
    order_resp = LabOrderDetailResponse(
        id=str(order.id),
        order_code=order.order_code,
        status=order.status,
        patient_id=str(order.patient_id),
        tenant_id=str(order.tenant_id),
        branch_id=str(order.branch_id),
        requested_by=order.requested_by,
        notes=order.notes,
        billed_lock=order.billed_lock,
    )

    patient_resp = {
        "id": str(patient.id),
        "patient_code": patient.patient_code,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "dob": str(patient.dob) if getattr(patient, "dob", None) else None,
        "sex": getattr(patient, "sex", None),
        "phone": getattr(patient, "phone", None),
        "email": getattr(patient, "email", None),
        "tenant_id": str(patient.tenant_id),
        "branch_id": str(patient.branch_id),
    }

    sample_resps = [
        SampleResponse(
            id=str(s.id),
            sample_code=s.sample_code,
            type=s.type,
            state=s.state,
            order_id=str(s.order_id),
            tenant_id=str(s.tenant_id),
            branch_id=str(s.branch_id),
        )
        for s in samples
    ]

    return LabOrderFullDetailResponse(order=order_resp, patient=patient_resp, samples=sample_resps)


@router.get("/patients/{patient_id}/orders", response_model=PatientOrdersListResponse)
def list_patient_orders(patient_id: str, session: Session = Depends(get_session)):
    """List all orders for a given patient (enriched summary)."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    orders = session.exec(select(LabOrder).where(LabOrder.patient_id == patient.id)).all()
    summaries = []
    for o in orders:
        sample_count = len(session.exec(select(Sample).where(Sample.order_id == o.id)).all())
        has_report = session.exec(select(Report).where(Report.order_id == o.id)).first() is not None
        summaries.append(
            PatientOrderSummary(
                id=str(o.id),
                order_code=o.order_code,
                status=o.status,
                tenant_id=str(o.tenant_id),
                branch_id=str(o.branch_id),
                patient_id=str(o.patient_id),
                requested_by=o.requested_by,
                notes=o.notes,
                created_at=str(getattr(o, "created_at", "")) if getattr(o, "created_at", None) else None,
                sample_count=sample_count,
                has_report=has_report,
            )
        )

    return PatientOrdersListResponse(patient_id=str(patient.id), orders=summaries)


@router.get("/patients/{patient_id}/cases", response_model=PatientCasesListResponse)
def list_patient_cases(patient_id: str, session: Session = Depends(get_session)):
    """List all cases for a patient with full details: order, samples, and report metadata (if any)."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    orders = session.exec(select(LabOrder).where(LabOrder.patient_id == patient.id)).all()
    cases = []
    for order in orders:
        # Order detail
        order_resp = LabOrderDetailResponse(
            id=str(order.id),
            order_code=order.order_code,
            status=order.status,
            patient_id=str(order.patient_id),
            tenant_id=str(order.tenant_id),
            branch_id=str(order.branch_id),
            requested_by=order.requested_by,
            notes=order.notes,
            billed_lock=order.billed_lock,
        )

        # Samples list
        samples = session.exec(select(Sample).where(Sample.order_id == order.id)).all()
        sample_resps = [
            SampleResponse(
                id=str(s.id),
                sample_code=s.sample_code,
                type=s.type,
                state=s.state,
                order_id=str(s.order_id),
                tenant_id=str(s.tenant_id),
                branch_id=str(s.branch_id),
            )
            for s in samples
        ]

        # Report meta (if any) using latest/current version
        report_meta = None
        report = session.exec(select(Report).where(Report.order_id == order.id)).first()
        if report:
            current_version = session.exec(
                select(ReportVersion)
                .where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
                .order_by(ReportVersion.version_no.desc())
            ).first()
            has_pdf = bool(current_version and current_version.pdf_storage_id)
            from app.schemas.report import ReportMetaResponse  # local import to avoid cycles
            report_meta = ReportMetaResponse(
                id=str(report.id),
                status=report.status,
                title=report.title,
                published_at=report.published_at,
                version_no=current_version.version_no if current_version else None,
                has_pdf=has_pdf,
            )

        cases.append({
            "order": order_resp,
            "samples": sample_resps,
            "report": report_meta,
        })

    return PatientCasesListResponse(patient_id=str(patient.id), cases=cases)
