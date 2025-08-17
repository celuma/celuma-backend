from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin

class StorageObject(BaseModel, TimestampMixin, table=True):
    """Storage object model for S3 and other cloud storage"""
    __tablename__ = "storage_object"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    provider: str = Field(default="aws", max_length=50)  # aws, gcp, azure, etc.
    region: str = Field(max_length=100)  # us-east-1, etc.
    bucket: str = Field(max_length=255)
    object_key: str = Field(max_length=1000)  # Path/key in the bucket
    version_id: Optional[str] = Field(max_length=255, default=None)
    etag: Optional[str] = Field(max_length=255, default=None)
    sha256_hex: Optional[str] = Field(max_length=64, default=None)
    content_type: Optional[str] = Field(max_length=255, default=None)
    size_bytes: Optional[int] = Field(default=None)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # No relationships for now - will add back as we fix the models

class SampleImageRendition(BaseModel, table=True):
    """Sample image rendition model for processed images"""
    __tablename__ = "sample_image_rendition"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    sample_image_id: UUID = Field(foreign_key="sample_image.id")
    kind: str = Field(max_length=50)  # thumbnail, tile, webp, etc.
    storage_id: UUID = Field(foreign_key="storage_object.id")
    
    # Basic relationships only
    sample_image: "SampleImage" = Relationship(back_populates="renditions")
