#!/usr/bin/env python3
"""
Script to clean up blacklisted tokens created during logout tests
This helps prevent database bloat from accumulated test tokens
"""

import requests
import json
from typing import List, Dict, Any

BASE_URL = "http://localhost:8000"

def cleanup_blacklisted_tokens():
    """Clean up blacklisted tokens from the database"""
    print("🔒 Celuma API Blacklisted Tokens Cleanup")
    print("=" * 50)
    
    try:
        # Note: This would require a cleanup endpoint or direct database access
        # For now, we'll provide guidance on how to implement this
        
        print("💡 Blacklisted tokens cleanup process:")
        print("   1. These tokens are created when users logout")
        print("   2. They accumulate in the database over time")
        print("   3. They should be cleaned up periodically")
        
        print("\n🔧 Implementation options:")
        print("   Option 1: Add a cleanup endpoint to the API")
        print("   Option 2: Implement a scheduled cleanup task")
        print("   Option 3: Direct database cleanup script")
        
        print("\n📝 Example cleanup endpoint implementation:")
        print("   POST /api/v1/admin/cleanup/blacklisted-tokens")
        print("   - Requires admin privileges")
        print("   - Removes expired blacklisted tokens")
        print("   - Returns count of cleaned tokens")
        
        print("\n🗄️  Database cleanup considerations:")
        print("   - Remove tokens older than X days")
        print("   - Keep recent tokens for audit purposes")
        print("   - Log cleanup activities")
        print("   - Handle cleanup failures gracefully")
        
        print("\n⏰ Recommended cleanup schedule:")
        print("   - Daily: Remove tokens older than 7 days")
        print("   - Weekly: Remove tokens older than 30 days")
        print("   - Monthly: Full cleanup of old tokens")
        
        print("\n⚠️  Current status:")
        print("   - No automatic cleanup implemented")
        print("   - Manual cleanup required")
        print("   - Consider implementing one of the options above")
        
    except Exception as e:
        print(f"❌ Error during cleanup analysis: {e}")

def suggest_cleanup_endpoint():
    """Suggest implementation for a cleanup endpoint"""
    print("\n🔧 Suggested Cleanup Endpoint Implementation:")
    print("=" * 50)
    
    print("""
# Add to app/api/v1/admin.py or create new admin module

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.user import BlacklistedToken
from app.api.v1.auth import current_user
from datetime import datetime, timedelta

router = APIRouter(prefix="/admin")

@router.post("/cleanup/blacklisted-tokens")
def cleanup_blacklisted_tokens(
    days_old: int = 7,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_session)
):
    \"\"\"Clean up blacklisted tokens older than specified days\"\"\"
    
    # Check if user has admin privileges
    if user.role != "admin":
        raise HTTPException(403, "Admin privileges required")
    
    try:
        # Calculate cutoff date
        cutoff_date = datetime.utcnow() - timedelta(days=days_old)
        
        # Find and delete old blacklisted tokens
        old_tokens = session.exec(
            select(BlacklistedToken).where(
                BlacklistedToken.expires_at < cutoff_date
            )
        ).all()
        
        count = len(old_tokens)
        for token in old_tokens:
            session.delete(token)
        
        session.commit()
        
        return {
            "message": f"Cleaned up {count} blacklisted tokens older than {days_old} days",
            "tokens_removed": count,
            "cutoff_date": cutoff_date.isoformat()
        }
        
    except Exception as e:
        session.rollback()
        raise HTTPException(500, f"Cleanup failed: {str(e)}")
""")

def main():
    """Main function"""
    cleanup_blacklisted_tokens()
    suggest_cleanup_endpoint()
    
    print("\n" + "=" * 50)
    print("✅ Blacklisted tokens cleanup analysis completed!")
    print("💡 Implement one of the suggested cleanup methods")
    print("💡 Regular cleanup will improve database performance")

if __name__ == "__main__":
    main()
