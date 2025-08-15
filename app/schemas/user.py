from pydantic import BaseModel, EmailStr

class UserRead(BaseModel):
    id: int
    email: EmailStr

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
