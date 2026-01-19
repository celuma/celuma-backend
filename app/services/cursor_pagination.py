"""Cursor pagination utilities for stable pagination."""

import base64
from typing import Tuple
from datetime import datetime


def encode_cursor(created_at: datetime, comment_id: str) -> str:
    """Encode cursor from timestamp and ID.
    
    Args:
        created_at: Timestamp of the item
        comment_id: UUID of the comment
    
    Returns:
        Base64-encoded cursor string
    """
    cursor_str = f"{created_at.isoformat()}|{comment_id}"
    return base64.urlsafe_b64encode(cursor_str.encode()).decode()


def decode_cursor(cursor: str) -> Tuple[datetime, str]:
    """Decode cursor to timestamp and ID.
    
    Args:
        cursor: Base64-encoded cursor string
    
    Returns:
        Tuple of (timestamp, comment_id)
    
    Raises:
        ValueError: If cursor format is invalid
    """
    try:
        cursor_str = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp_str, comment_id = cursor_str.split('|', 1)
        timestamp = datetime.fromisoformat(timestamp_str)
        return timestamp, comment_id
    except Exception as e:
        raise ValueError(f"Invalid cursor format: {str(e)}")
