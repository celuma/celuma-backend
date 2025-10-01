from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext
from app.models.patient import Patient
from app.models.tenant import Tenant, Branch
from app.schemas.patient import PatientCreate, PatientResponse, PatientDetailResponse, PatientFullResponse

router = APIRouter(prefix="/patients")

@router.get("/", response_model=list[PatientFullResponse])
def list_patients(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all patients with full profile"""
    patients = session.exec(select(Patient).where(Patient.tenant_id == ctx.tenant_id)).all()
    return [
        PatientFullResponse(
            id=str(p.id),
            tenant_id=str(p.tenant_id),
            branch_id=str(p.branch_id),
            patient_code=p.patient_code,
            first_name=p.first_name,
            last_name=p.last_name,
            dob=p.dob,
            sex=p.sex,
            phone=p.phone,
            email=p.email,
        )
        for p in patients
    ]

@router.post("/", response_model=PatientResponse)
def create_patient(patient_data: PatientCreate, session: Session = Depends(get_session)):
    """Create a new patient"""
    # Verify tenant and branch exist
    tenant = session.get(Tenant, patient_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, patient_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    # Check if patient_code is unique for this tenant
    existing_patient = session.exec(
        select(Patient).where(
            Patient.patient_code == patient_data.patient_code,
            Patient.tenant_id == patient_data.tenant_id
        )
    ).first()
    
    if existing_patient:
        raise HTTPException(400, "Patient code already exists for this tenant")
    
    patient = Patient(
        tenant_id=patient_data.tenant_id,
        branch_id=patient_data.branch_id,
        patient_code=patient_data.patient_code,
        first_name=patient_data.first_name,
        last_name=patient_data.last_name,
        dob=patient_data.dob,
        sex=patient_data.sex,
        phone=patient_data.phone,
        email=patient_data.email
    )
    
    session.add(patient)
    session.commit()
    session.refresh(patient)
    
    return PatientResponse(
        id=str(patient.id),
        patient_code=patient.patient_code,
        first_name=patient.first_name,
        last_name=patient.last_name,
        tenant_id=str(patient.tenant_id),
        branch_id=str(patient.branch_id)
    )

@router.get("/{patient_id}", response_model=PatientDetailResponse)
def get_patient(
    patient_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get patient details"""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")
    if str(patient.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Patient not found")
    
    return PatientDetailResponse(
        id=str(patient.id),
        patient_code=patient.patient_code,
        first_name=patient.first_name,
        last_name=patient.last_name,
        dob=patient.dob,
        sex=patient.sex,
        phone=patient.phone,
        email=patient.email,
        tenant_id=str(patient.tenant_id),
        branch_id=str(patient.branch_id)
    )
