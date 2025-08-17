#!/usr/bin/env python3
"""
Cleanup utility for expired blacklisted tokens
This script removes tokens that have expired from the blacklist
"""

from datetime import datetime
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.user import BlacklistedToken

def cleanup_expired_tokens(session: Session) -> int:
    """
    Remove expired tokens from the blacklist
    
    Args:
        session: Database session
        
    Returns:
        int: Number of tokens removed
    """
    try:
        # Find all expired tokens
        expired_tokens = session.exec(
            select(BlacklistedToken).where(BlacklistedToken.expires_at < datetime.utcnow())
        ).all()
        
        if not expired_tokens:
            return 0
        
        # Remove expired tokens
        count = len(expired_tokens)
        for token in expired_tokens:
            session.delete(token)
        
        session.commit()
        return count
        
    except Exception as e:
        session.rollback()
        raise e

def cleanup_old_blacklisted_tokens(session: Session, days_old: int = 30) -> int:
    """
    Remove blacklisted tokens older than specified days
    
    Args:
        session: Database session
        days_old: Remove tokens older than this many days
        
    Returns:
        int: Number of tokens removed
    """
    try:
        from datetime import timedelta
        
        cutoff_date = datetime.utcnow() - timedelta(days=days_old)
        
        old_tokens = session.exec(
            select(BlacklistedToken).where(BlacklistedToken.blacklisted_at < cutoff_date)
        ).all()
        
        if not old_tokens:
            return 0
        
        # Remove old tokens
        count = len(old_tokens)
        for token in old_tokens:
            session.delete(token)
        
        session.commit()
        return count
        
    except Exception as e:
        session.rollback()
        raise e

def get_blacklist_stats(session: Session) -> dict:
    """
    Get statistics about the blacklisted tokens
    
    Args:
        session: Database session
        
    Returns:
        dict: Statistics about blacklisted tokens
    """
    try:
        total_tokens = session.exec(select(BlacklistedToken)).all()
        
        expired_tokens = session.exec(
            select(BlacklistedToken).where(BlacklistedToken.expires_at < datetime.utcnow())
        ).all()
        
        active_tokens = session.exec(
            select(BlacklistedToken).where(BlacklistedToken.expires_at >= datetime.utcnow())
        ).all()
        
        return {
            "total_tokens": len(total_tokens),
            "expired_tokens": len(expired_tokens),
            "active_tokens": len(active_tokens),
            "cleanup_recommended": len(expired_tokens) > 0
        }
        
    except Exception as e:
        raise e

if __name__ == "__main__":
    """Run cleanup as standalone script"""
    try:
        with next(get_session()) as session:
            print("🧹 Celuma API Token Cleanup Utility")
            print("=" * 50)
            
            # Get current stats
            stats = get_blacklist_stats(session)
            print(f"📊 Current Blacklist Status:")
            print(f"   Total tokens: {stats['total_tokens']}")
            print(f"   Expired tokens: {stats['expired_tokens']}")
            print(f"   Active tokens: {stats['active_tokens']}")
            
            if stats['cleanup_recommended']:
                print(f"\n🧹 Cleaning up expired tokens...")
                removed = cleanup_expired_tokens(session)
                print(f"✅ Removed {removed} expired tokens")
                
                # Get updated stats
                updated_stats = get_blacklist_stats(session)
                print(f"\n📊 Updated Status:")
                print(f"   Total tokens: {updated_stats['total_tokens']}")
                print(f"   Active tokens: {updated_stats['active_tokens']}")
            else:
                print(f"\n✅ No cleanup needed - all tokens are current")
                
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")
        exit(1)
