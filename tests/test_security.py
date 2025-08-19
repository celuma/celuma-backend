"""
Unit tests for Celuma API security functions
"""
from app.core.security import verify_password, hash_password

class TestPasswordSecurity:
    """Test password hashing and verification"""
    
    def test_password_hashing(self):
        """Test that password hashing works correctly"""
        password = "testpassword123"
        hashed = hash_password(password)
        
        # Hash should be different from original
        assert hashed != password
        # Hash should be a string
        assert isinstance(hashed, str)
        # Hash should be longer than original
        assert len(hashed) > len(password)
    
    def test_password_verification(self):
        """Test that password verification works correctly"""
        password = "testpassword123"
        hashed = hash_password(password)
        
        # Should verify correctly
        assert verify_password(password, hashed) is True
        # Should fail with wrong password
        assert verify_password("wrongpassword", hashed) is False
    
    def test_password_verification_edge_cases(self):
        """Test password verification edge cases"""
        password = "testpassword123"
        hashed = hash_password(password)
        
        # Empty password
        assert verify_password("", hashed) is False
        # Very long password
        long_password = "a" * 1000
        long_hashed = hash_password(long_password)
        assert verify_password(long_password, long_hashed) is True

class TestSecurityEdgeCases:
    """Test security edge cases and error handling"""
    
    def test_password_hash_edge_cases(self):
        """Test password hashing edge cases"""
        # Empty password
        empty_hash = hash_password("")
        assert isinstance(empty_hash, str)
        assert verify_password("", empty_hash) is True
        
        # Very short password
        short_hash = hash_password("a")
        assert isinstance(short_hash, str)
        assert verify_password("a", short_hash) is True
        
        # Very long password
        long_password = "a" * 1000
        long_hash = hash_password(long_password)
        assert isinstance(long_hash, str)
        assert verify_password(long_password, long_hash) is True
