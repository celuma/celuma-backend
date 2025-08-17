from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.patient import Patient
from app.models.tenant import Tenant, Branch

router = APIRouter(prefix="/patients")

@router.get("/")
def list_patients(session: Session = Depends(get_session)):
    """List all patients"""
    patients = session.exec(select(Patient)).all()
    return [{
        "id": str(p.id),
        "patient_code": p.patient_code,
        "first_name": p.first_name,
        "last_name": p.last_name,
        "tenant_id": str(p.tenant_id),
        "branch_id": str(p.branch_id)
    } for p in patients]

@router.post("/")
def create_patient(
    tenant_id: str,
    branch_id: str,
    patient_code: str,
    first_name: str,
    last_name: str,
    dob: str = None,
    sex: str = None,
    phone: str = None,
    email: str = None,
    session: Session = Depends(get_session)
):
    """Create a new patient"""
    # Verify tenant and branch exist
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    # Check if patient_code is unique for this tenant
    existing_patient = session.exec(
        select(Patient).where(
            Patient.patient_code == patient_code,
            Patient.tenant_id == tenant_id
        )
    ).first()
    
    if existing_patient:
        raise HTTPException(400, "Patient code already exists for this tenant")
    
    patient = Patient(
        tenant_id=tenant_id,
        branch_id=branch_id,
        patient_code=patient_code,
        first_name=first_name,
        last_name=last_name,
        dob=dob,
        sex=sex,
        phone=phone,
        email=email
    )
    
    session.add(patient)
    session.commit()
    session.refresh(patient)
    
    return {
        "id": str(patient.id),
        "patient_code": patient.patient_code,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "tenant_id": str(patient.tenant_id),
        "branch_id": str(patient.branch_id)
    }

@router.get("/{patient_id}")
def get_patient(patient_id: str, session: Session = Depends(get_session)):
    """Get patient details"""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")
    
    return {
        "id": str(patient.id),
        "patient_code": patient.patient_code,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "dob": patient.dob,
        "sex": patient.sex,
        "phone": patient.phone,
        "email": patient.email,
        "tenant_id": str(patient.tenant_id),
        "branch_id": str(patient.branch_id)
    }
