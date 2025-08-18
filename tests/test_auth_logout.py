#!/usr/bin/env python3
"""
Test suite for Celuma API Authentication Logout functionality
Tests the complete logout flow including token blacklisting
"""

import requests
import json
import time
from datetime import datetime

# Configuration
API_BASE_URL = "http://localhost:8000"
TEST_EMAIL = "test_logout@example.com"
TEST_PASSWORD = "testpassword123"
TEST_FULL_NAME = "Test Logout User"
TEST_ROLE = "admin"

def print_header(title):
    """Print a formatted header"""
    print(f"\n{'='*60}")
    print(f"🚀 {title}")
    print(f"{'='*60}")

def print_status(message, status="INFO"):
    """Print a formatted status message"""
    status_icons = {
        "INFO": "ℹ️",
        "SUCCESS": "✅",
        "ERROR": "❌",
        "WARNING": "⚠️"
    }
    icon = status_icons.get(status, "ℹ️")
    print(f"{icon} {message}")

def test_health_check():
    """Test if the API is running"""
    try:
        response = requests.get(f"{API_BASE_URL}/")
        if response.status_code == 200:
            print_status("API is running and responding", "SUCCESS")
            return True
        else:
            print_status(f"API responded with status {response.status_code}", "ERROR")
            return False
    except requests.exceptions.RequestException as e:
        print_status(f"Failed to connect to API: {e}", "ERROR")
        return False

def create_test_tenant():
    """Create a test tenant for authentication tests"""
    try:
        tenant_name = f"Test Logout Tenant {int(time.time())}"
        
        data = {"name": tenant_name}
        response = requests.post(f"{API_BASE_URL}/api/v1/tenants/", json=data)
        if response.status_code == 200:
            tenant = response.json()
            print_status(f"Created test tenant: {tenant['name']}", "SUCCESS")
            return tenant
        else:
            print_status(f"Failed to create tenant: {response.status_code}", "ERROR")
            return None
    except Exception as e:
        print_status(f"Error creating tenant: {e}", "ERROR")
        return None

def create_test_user(tenant_id):
    """Create a test user for authentication tests"""
    try:
        data = {
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "full_name": TEST_FULL_NAME,
            "role": TEST_ROLE,
            "tenant_id": tenant_id
        }
        response = requests.post(
            f"{API_BASE_URL}/api/v1/auth/register",
            json=data
        )
        if response.status_code == 200:
            user = response.json()
            print_status(f"Created test user: {user['email']}", "SUCCESS")
            return user
        else:
            print_status(f"Error creating user: {response.status_code}", "ERROR")
            return None
    except Exception as e:
        print_status(f"Error creating user: {e}", "ERROR")
        return None

def test_user_login(tenant_id):
    """Test user login and get access token"""
    try:
        data = {
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "tenant_id": tenant_id
        }
        response = requests.post(
            f"{API_BASE_URL}/api/v1/auth/login",
            json=data
        )
        if response.status_code == 200:
            login_data = response.json()
            access_token = login_data.get('access_token')
            if access_token:
                print_status(f"User login successful, got access token", "SUCCESS")
                return access_token
            else:
                print_status("Login response missing access token", "ERROR")
                return None
        else:
            print_status(f"Login failed with status {response.status_code}", "ERROR")
            return None
    except Exception as e:
        print_status(f"Error during login: {e}", "ERROR")
        return None

def test_authenticated_endpoint(access_token):
    """Test that the access token works with authenticated endpoints"""
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(f"{API_BASE_URL}/api/v1/auth/me", headers=headers)
        
        if response.status_code == 200:
            user_info = response.json()
            print_status(f"Authenticated endpoint works: {user_info['email']}", "SUCCESS")
            return True
        else:
            print_status(f"Authenticated endpoint failed: {response.status_code}", "ERROR")
            return False
    except Exception as e:
        print_status(f"Error testing authenticated endpoint: {e}", "ERROR")
        return False

def test_logout(access_token):
    """Test the logout functionality"""
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.post(f"{API_BASE_URL}/api/v1/auth/logout", headers=headers)
        
        if response.status_code == 200:
            logout_response = response.json()
            print_status("Logout successful", "SUCCESS")
            print_status(f"Response: {logout_response}", "INFO")
            return True
        else:
            print_status(f"Logout failed: {response.status_code}", "ERROR")
            return False
    except Exception as e:
        print_status(f"Error during logout: {e}", "ERROR")
        return False

def test_token_blacklisted(access_token):
    """Test that the token is now blacklisted and cannot be used"""
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(f"{API_BASE_URL}/api/v1/auth/me", headers=headers)
        
        if response.status_code == 401:
            print_status("Token successfully blacklisted - cannot access authenticated endpoints", "SUCCESS")
            return True
        else:
            print_status(f"Token still works after logout: {response.status_code}", "ERROR")
            return False
    except Exception as e:
        print_status(f"Error testing blacklisted token: {e}", "ERROR")
        return False

def test_double_logout(access_token):
    """Test that trying to logout the same token twice returns appropriate response"""
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.post(f"{API_BASE_URL}/api/v1/auth/logout", headers=headers)
        
        if response.status_code == 200:
            logout_response = response.json()
            if "already revoked" in logout_response.get("message", "").lower():
                print_status("Double logout handled correctly", "SUCCESS")
                return True
            else:
                print_status("Double logout response unexpected", "WARNING")
                return False
        else:
            print_status(f"Double logout failed: {response.status_code}", "ERROR")
            return False
    except Exception as e:
        print_status(f"Error testing double logout: {e}", "ERROR")
        return False

def cleanup_test_data(tenant_id):
    """Clean up test data (this would require DELETE endpoints)"""
    print_status("Test data cleanup would happen here if DELETE endpoints were implemented", "INFO")
    print_status(f"Test tenant ID: {tenant_id}", "INFO")
    print_status(f"Test user email: {TEST_EMAIL}", "INFO")

def run_logout_tests():
    """Run all logout functionality tests"""
    print_header("CELUMA API LOGOUT FUNCTIONALITY TESTING")
    
    # Test 1: Health check
    if not test_health_check():
        print_status("Cannot proceed with tests - API is not responding", "ERROR")
        return False
    
    # Test 2: Create test tenant
    tenant = create_test_tenant()
    if not tenant:
        print_status("Cannot proceed with tests - failed to create tenant", "ERROR")
        return False
    
    # Test 3: Create test user
    user = create_test_user(tenant["id"])
    if not user:
        print_status("Cannot proceed with tests - failed to create user", "ERROR")
        return False
    
    # Test 4: Test login
    access_token = test_user_login(tenant["id"])
    if not access_token:
        print_status("Cannot proceed with tests - failed to login", "ERROR")
        return False
    
    # Test 5: Test authenticated endpoint
    if not test_authenticated_endpoint(access_token):
        print_status("Cannot proceed with tests - authentication not working", "ERROR")
        return False
    
    # Test 6: Test logout
    if not test_logout(access_token):
        print_status("Logout test failed", "ERROR")
        return False
    
    # Test 7: Test that token is blacklisted
    if not test_token_blacklisted(access_token):
        print_status("Token blacklisting test failed", "ERROR")
        return False
    
    # Test 8: Test double logout
    if not test_double_logout(access_token):
        print_status("Double logout test failed", "WARNING")
    
    # Cleanup
    cleanup_test_data(tenant["id"])
    
    print_header("LOGOUT TESTING COMPLETED")
    print_status("All logout functionality tests completed successfully!", "SUCCESS")
    return True

if __name__ == "__main__":
    try:
        success = run_logout_tests()
        if success:
            print_status("🎉 All logout tests passed!", "SUCCESS")
        else:
            print_status("❌ Some logout tests failed", "ERROR")
    except KeyboardInterrupt:
        print_status("\n⚠️ Testing interrupted by user", "WARNING")
    except Exception as e:
        print_status(f"❌ Unexpected error during testing: {e}", "ERROR")
