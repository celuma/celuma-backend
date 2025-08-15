from fastapi import FastAPI
from app.api.v1.users import router as users_router
from app.api.v1.auth import router as auth_router

app = FastAPI(title="Celuma API")
app.include_router(users_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
