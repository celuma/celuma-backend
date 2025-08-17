#!/usr/bin/env python3
"""
Test script to verify error handling and validation in the Celuma API
Tests various error scenarios and edge cases
"""

import requests
import json
from typing import Dict, Any

BASE_URL = "http://localhost:8000"

def test_invalid_tenant_id():
    """Test creating entities with invalid tenant ID"""
    print("🔍 Testing invalid tenant ID scenarios...")
    
    invalid_tenant_id = "00000000-0000-0000-0000-000000000000"
    
    # Test creating branch with invalid tenant
    try:
        data = {
            "tenant_id": invalid_tenant_id,
            "code": "INVALID",
            "name": "Invalid Branch",
            "city": "Test City"
        }
        response = requests.post(f"{BASE_URL}/api/v1/branches/", params=data)
        print(f"   ✅ Branch with invalid tenant: {response.status_code}")
        if response.status_code == 404:
            print("      Correctly returned 404 for invalid tenant")
        else:
            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing invalid tenant: {e}")
    
    # Test creating patient with invalid tenant
    try:
        data = {
            "tenant_id": invalid_tenant_id,
            "branch_id": "00000000-0000-0000-0000-000000000000",
            "patient_code": "INVALID",
            "first_name": "Invalid",
            "last_name": "Patient"
        }
        response = requests.post(f"{BASE_URL}/api/v1/patients/", params=data)
        print(f"   ✅ Patient with invalid tenant: {response.status_code}")
        if response.status_code == 404:
            print("      Correctly returned 404 for invalid tenant")
        else:
            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing invalid patient: {e}")

def test_duplicate_codes():
    """Test creating entities with duplicate codes"""
    print("\n🔍 Testing duplicate code scenarios...")
    
    # First, get existing data to use for testing
    try:
        response = requests.get(f"{BASE_URL}/api/v1/tenants/")
        if response.status_code == 200:
            tenants = response.json()
            if tenants:
                tenant_id = tenants[0]['id']
                
                # Test creating branch with duplicate code
                response = requests.get(f"{BASE_URL}/api/v1/branches/")
                if response.status_code == 200:
                    branches = response.json()
                    if branches:
                        existing_branch = branches[0]
                        
                        # Try to create another branch with same code
                        data = {
                            "tenant_id": tenant_id,
                            "code": existing_branch['code'],
                            "name": "Duplicate Branch",
                            "city": "Test City"
                        }
                        response = requests.post(f"{BASE_URL}/api/v1/branches/", params=data)
                        print(f"   ✅ Duplicate branch code: {response.status_code}")
                        if response.status_code == 400:
                            print("      Correctly returned 400 for duplicate code")
                        else:
                            print(f"      Unexpected status: {response.status_code}")
                
                # Test creating patient with duplicate code
                response = requests.get(f"{BASE_URL}/api/v1/patients/")
                if response.status_code == 200:
                    patients = response.json()
                    if patients:
                        existing_patient = patients[0]
                        
                        # Try to create another patient with same code
                        data = {
                            "tenant_id": tenant_id,
                            "branch_id": existing_patient['branch_id'],
                            "patient_code": existing_patient['patient_code'],
                            "first_name": "Duplicate",
                            "last_name": "Patient"
                        }
                        response = requests.post(f"{BASE_URL}/api/v1/patients/", params=data)
                        print(f"   ✅ Duplicate patient code: {response.status_code}")
                        if response.status_code == 400:
                            print("      Correctly returned 400 for duplicate code")
                        else:
                            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing duplicate codes: {e}")

def test_missing_required_fields():
    """Test creating entities with missing required fields"""
    print("\n🔍 Testing missing required fields...")
    
    # Test creating tenant without name
    try:
        data = {
            "legal_name": "Test Legal Name"
            # Missing 'name' field
        }
        response = requests.post(f"{BASE_URL}/api/v1/tenants/", params=data)
        print(f"   ✅ Tenant without name: {response.status_code}")
        if response.status_code == 422:
            print("      Correctly returned 422 for missing required field")
        else:
            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing missing tenant name: {e}")
    
    # Test creating branch without tenant_id
    try:
        data = {
            "code": "TEST",
            "name": "Test Branch",
            "city": "Test City"
            # Missing 'tenant_id' field
        }
        response = requests.post(f"{BASE_URL}/api/v1/branches/", params=data)
        print(f"   ✅ Branch without tenant_id: {response.status_code}")
        if response.status_code == 422:
            print("      Correctly returned 422 for missing required field")
        else:
            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing missing branch tenant_id: {e}")

def test_invalid_enum_values():
    """Test creating entities with invalid enum values"""
    print("\n🔍 Testing invalid enum values...")
    
    # Get existing data for testing
    try:
        response = requests.get(f"{BASE_URL}/api/v1/tenants/")
        if response.status_code == 200:
            tenants = response.json()
            if tenants:
                tenant_id = tenants[0]['id']
                
                response = requests.get(f"{BASE_URL}/api/v1/branches/")
                if response.status_code == 200:
                    branches = response.json()
                    if branches:
                        branch_id = branches[0]['id']
                        
                        response = requests.get(f"{BASE_URL}/api/v1/patients/")
                        if response.status_code == 200:
                            patients = response.json()
                            if patients:
                                patient_id = patients[0]['id']
                                
                                # Test creating order with invalid status
                                data = {
                                    "tenant_id": tenant_id,
                                    "branch_id": branch_id,
                                    "patient_id": patient_id,
                                    "order_code": "INVALID_ORDER",
                                    "status": "INVALID_STATUS"  # Invalid enum value
                                }
                                response = requests.post(f"{BASE_URL}/api/v1/laboratory/orders/", params=data)
                                print(f"   ✅ Order with invalid status: {response.status_code}")
                                if response.status_code == 422:
                                    print("      Correctly returned 422 for invalid enum")
                                else:
                                    print(f"      Unexpected status: {response.status_code}")
                                
                                # Test creating sample with invalid type
                                data = {
                                    "tenant_id": tenant_id,
                                    "branch_id": branch_id,
                                    "order_id": "00000000-0000-0000-0000-000000000000",
                                    "sample_code": "INVALID_SAMPLE",
                                    "type": "INVALID_TYPE"  # Invalid enum value
                                }
                                response = requests.post(f"{BASE_URL}/api/v1/laboratory/samples/", params=data)
                                print(f"   ✅ Sample with invalid type: {response.status_code}")
                                if response.status_code == 422:
                                    print("      Correctly returned 422 for invalid enum")
                                else:
                                    print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing invalid enums: {e}")

def test_nonexistent_entities():
    """Test accessing entities that don't exist"""
    print("\n🔍 Testing access to nonexistent entities...")
    
    invalid_id = "00000000-0000-0000-0000-000000000000"
    
    # Test getting nonexistent tenant
    try:
        response = requests.get(f"{BASE_URL}/api/v1/tenants/{invalid_id}")
        print(f"   ✅ Nonexistent tenant: {response.status_code}")
        if response.status_code == 404:
            print("      Correctly returned 404 for nonexistent entity")
        else:
            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing nonexistent tenant: {e}")
    
    # Test getting nonexistent branch
    try:
        response = requests.get(f"{BASE_URL}/api/v1/branches/{invalid_id}")
        print(f"   ✅ Nonexistent branch: {response.status_code}")
        if response.status_code == 404:
            print("      Correctly returned 404 for nonexistent entity")
        else:
            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing nonexistent branch: {e}")
    
    # Test getting nonexistent patient
    try:
        response = requests.get(f"{BASE_URL}/api/v1/patients/{invalid_id}")
        print(f"   ✅ Nonexistent patient: {response.status_code}")
        if response.status_code == 404:
            print("      Correctly returned 404 for nonexistent entity")
        else:
            print(f"      Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Error testing nonexistent patient: {e}")

def main():
    """Run all validation error tests"""
    print("🚀 Testing Celuma API Validation and Error Handling")
    print("=" * 60)
    
    tests = [
        test_invalid_tenant_id,
        test_duplicate_codes,
        test_missing_required_fields,
        test_invalid_enum_values,
        test_nonexistent_entities
    ]
    
    print("🔍 Running validation error tests...")
    
    for test in tests:
        try:
            test()
            print()
        except Exception as e:
            print(f"❌ Test failed with error: {e}")
            print()
    
    print("=" * 60)
    print("✅ Validation error tests completed!")
    print("💡 These tests verify that the API properly handles error cases")

if __name__ == "__main__":
    main()
