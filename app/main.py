from fastapi import FastAPI
from app.api.v1.users import router as users_router
from app.api.v1.auth import router as auth_router
from app.api.v1.tenants import router as tenants_router
from app.api.v1.branches import router as branches_router
from app.api.v1.patients import router as patients_router
from app.api.v1.laboratory import router as laboratory_router
from app.api.v1.reports import router as reports_router
from app.api.v1.billing import router as billing_router

app = FastAPI(title="Celuma API", description="Multi-tenant Laboratory Management System")

app.include_router(users_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(tenants_router, prefix="/api/v1")
app.include_router(branches_router, prefix="/api/v1")
app.include_router(patients_router, prefix="/api/v1")
app.include_router(laboratory_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
app.include_router(billing_router, prefix="/api/v1")

@app.get("/")
def root():
    return {
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
