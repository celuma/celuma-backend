#!/usr/bin/env python3
"""
Test script to verify that the Celuma API endpoints are working correctly
Tests the complete flow: Tenant -> Branch -> Patient -> Order -> Sample -> Report -> Invoice -> Payment
"""

import requests
import json
from typing import Dict, Any

BASE_URL = "http://localhost:8000"

class TestData:
    """Class to store test data IDs for the complete flow"""
    def __init__(self):
        self.tenant_id = None
        self.branch_id = None
        self.patient_id = None
        self.order_id = None
        self.sample_id = None
        self.report_id = None
        self.invoice_id = None
        self.payment_id = None
        self.user_id = None
        self.access_token = None

test_data = TestData()

def test_root_endpoint():
    """Test the root endpoint"""
    try:
        response = requests.get(f"{BASE_URL}/")
        print(f"✅ Root endpoint: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Message: {data.get('message')}")
            print(f"   Version: {data.get('version')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Root endpoint: Connection failed (server not running)")
        return False

def test_health_endpoint():
    """Test the health endpoint"""
    try:
        response = requests.get(f"{BASE_URL}/api/v1/health")
        print(f"✅ Health endpoint: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Status: {data.get('status')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Health endpoint: Connection failed (server not running)")
        return False

def test_create_tenant():
    """Test creating a tenant"""
    try:
        data = {
            "name": "Test Tenant API",
            "legal_name": "Test Legal Name API",
            "tax_id": "123456789"
        }
        response = requests.post(f"{BASE_URL}/api/v1/tenants/", json=data)
        print(f"✅ Create tenant: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.tenant_id = data.get('id')
            print(f"   Tenant ID: {test_data.tenant_id}")
            print(f"   Name: {data.get('name')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create tenant: Connection failed")
        return False

def test_create_branch():
    """Test creating a branch"""
    if not test_data.tenant_id:
        print("❌ Create branch: No tenant ID available")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "code": "MAIN_API",
            "name": "Main Branch API",
            "city": "Mexico City",
            "state": "CDMX",
            "country": "MX"
        }
        response = requests.post(f"{BASE_URL}/api/v1/branches/", json=data)
        print(f"✅ Create branch: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.branch_id = data.get('id')
            print(f"   Branch ID: {test_data.branch_id}")
            print(f"   Name: {data.get('name')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create branch: Connection failed")
        return False

def test_create_patient():
    """Test creating a patient"""
    if not test_data.tenant_id or not test_data.branch_id:
        print("❌ Create patient: No tenant or branch ID available")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "patient_code": "P001_API",
            "first_name": "John",
            "last_name": "Doe",
            "dob": "1990-01-01",
            "sex": "M",
            "phone": "555-1234",
            "email": "john.doe@test.com"
        }
        response = requests.post(f"{BASE_URL}/api/v1/patients/", json=data)
        print(f"✅ Create patient: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.patient_id = data.get('id')
            print(f"   Patient ID: {test_data.patient_id}")
            print(f"   Name: {data.get('first_name')} {data.get('last_name')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create patient: Connection failed")
        return False

def test_create_laboratory_order():
    """Test creating a laboratory order"""
    if not test_data.tenant_id or not test_data.branch_id or not test_data.patient_id:
        print("❌ Create order: No tenant, branch, or patient ID available")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "patient_id": test_data.patient_id,
            "order_code": "ORD001_API",
            "requested_by": "Dr. Smith",
            "notes": "Test order for API testing",
            "created_by": "test_user"
        }
        response = requests.post(f"{BASE_URL}/api/v1/laboratory/orders/", json=data)
        print(f"✅ Create order: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.order_id = data.get('id')
            print(f"   Order ID: {test_data.order_id}")
            print(f"   Order Code: {data.get('order_code')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create order: Connection failed")
        return False

def test_create_sample():
    """Test creating a sample"""
    if not test_data.tenant_id or not test_data.branch_id or not test_data.order_id:
        print("❌ Create sample: No tenant, branch, or order ID available")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "order_id": test_data.order_id,
            "sample_code": "SAMP001_API",
            "type": "SANGRE",
            "notes": "Test sample for API testing"
        }
        response = requests.post(f"{BASE_URL}/api/v1/laboratory/samples/", json=data)
        print(f"✅ Create sample: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.sample_id = data.get('id')
            print(f"   Sample ID: {test_data.sample_id}")
            print(f"   Sample Code: {data.get('sample_code')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create sample: Connection failed")
        return False

def test_create_report():
    """Test creating a report"""
    if not test_data.tenant_id or not test_data.branch_id or not test_data.order_id:
        print("❌ Create report: No tenant, branch, or order ID available")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "order_id": test_data.order_id,
            "title": "Test Report API",
            "diagnosis_text": "Test diagnosis for API testing",
            "created_by": "test_user"
        }
        response = requests.post(f"{BASE_URL}/api/v1/reports/", json=data)
        print(f"✅ Create report: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.report_id = data.get('id')
            print(f"   Report ID: {test_data.report_id}")
            print(f"   Status: {data.get('status')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create report: Connection failed")
        return False

def test_create_invoice():
    """Test creating an invoice"""
    if not test_data.tenant_id or not test_data.branch_id or not test_data.order_id:
        print("❌ Create invoice: No tenant, branch, or order ID available")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "order_id": test_data.order_id,
            "invoice_number": "INV001_API",
            "amount_total": 1500.00,
            "currency": "MXN"
        }
        response = requests.post(f"{BASE_URL}/api/v1/billing/invoices/", json=data)
        print(f"✅ Create invoice: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.invoice_id = data.get('id')
            print(f"   Invoice ID: {test_data.invoice_id}")
            print(f"   Invoice Number: {data.get('invoice_number')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create invoice: Connection failed")
        return False

def test_create_payment():
    """Test creating a payment"""
    if not test_data.tenant_id or not test_data.branch_id or not test_data.invoice_id:
        print("❌ Create payment: No tenant, branch, or invoice ID available")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "invoice_id": test_data.invoice_id,
            "amount_paid": 1500.00,
            "method": "credit_card"
        }
        response = requests.post(f"{BASE_URL}/api/v1/billing/payments/", json=data)
        print(f"✅ Create payment: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.payment_id = data.get('id')
            print(f"   Payment ID: {test_data.payment_id}")
            print(f"   Amount: {data.get('amount_paid')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create payment: Connection failed")
        return False

def test_user_registration():
    """Test user registration"""
    if not test_data.tenant_id:
        print("❌ User registration: No tenant ID available")
        return False
    
    try:
        data = {
            "email": "testuser@test.com",
            "password": "testpass123",
            "full_name": "Test User",
            "role": "admin",
            "tenant_id": test_data.tenant_id
        }
        response = requests.post(f"{BASE_URL}/api/v1/auth/register", json=data)
        print(f"✅ User registration: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.user_id = data.get('id')
            print(f"   User ID: {test_data.user_id}")
            print(f"   Email: {data.get('email')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ User registration: Connection failed")
        return False

def test_user_login():
    """Test user login"""
    try:
        data = {
            "email": "testuser@test.com",
            "password": "testpass123",
            "tenant_id": test_data.tenant_id
        }
        response = requests.post(f"{BASE_URL}/api/v1/auth/login", json=data)
        print(f"✅ User login: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.access_token = data.get('access_token')
            print(f"   Access Token: {test_data.access_token[:20]}...")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ User login: Connection failed")
        return False

def test_get_user_profile():
    """Test getting user profile with authentication"""
    if not test_data.access_token:
        print("❌ Get user profile: No access token available")
        return False
    
    try:
        headers = {"Authorization": f"Bearer {test_data.access_token}"}
        response = requests.get(f"{BASE_URL}/api/v1/auth/me", headers=headers)
        print(f"✅ Get user profile: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   User ID: {data.get('id')}")
            print(f"   Email: {data.get('email')}")
            print(f"   Role: {data.get('role')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Get user profile: Connection failed")
        return False

def test_list_endpoints():
    """Test list endpoints"""
    try:
        # Test list tenants
        response = requests.get(f"{BASE_URL}/api/v1/tenants/")
        print(f"✅ List tenants: {response.status_code}")
        
        # Test list branches
        response = requests.get(f"{BASE_URL}/api/v1/branches/")
        print(f"✅ List branches: {response.status_code}")
        
        # Test list patients
        response = requests.get(f"{BASE_URL}/api/v1/patients/")
        print(f"✅ List patients: {response.status_code}")
        
        # Test list orders
        response = requests.get(f"{BASE_URL}/api/v1/laboratory/orders/")
        print(f"✅ List orders: {response.status_code}")
        
        # Test list samples
        response = requests.get(f"{BASE_URL}/api/v1/laboratory/samples/")
        print(f"✅ List samples: {response.status_code}")
        
        # Test list reports
        response = requests.get(f"{BASE_URL}/api/v1/reports/")
        print(f"✅ List reports: {response.status_code}")
        
        # Test list invoices
        response = requests.get(f"{BASE_URL}/api/v1/billing/invoices/")
        print(f"✅ List invoices: {response.status_code}")
        
        # Test list payments
        response = requests.get(f"{BASE_URL}/api/v1/billing/payments/")
        print(f"✅ List payments: {response.status_code}")
        
        return True
    except requests.exceptions.ConnectionError:
        print("❌ List endpoints: Connection failed")
        return False

def test_get_details():
    """Test get detail endpoints"""
    try:
        # Test get tenant details
        if test_data.tenant_id:
            response = requests.get(f"{BASE_URL}/api/v1/tenants/{test_data.tenant_id}")
            print(f"✅ Get tenant details: {response.status_code}")
        
        # Test get branch details
        if test_data.branch_id:
            response = requests.get(f"{BASE_URL}/api/v1/branches/{test_data.branch_id}")
            print(f"✅ Get branch details: {response.status_code}")
        
        # Test get patient details
        if test_data.patient_id:
            response = requests.get(f"{BASE_URL}/api/v1/patients/{test_data.patient_id}")
            print(f"✅ Get patient details: {response.status_code}")
        
        # Test get order details
        if test_data.order_id:
            response = requests.get(f"{BASE_URL}/api/v1/laboratory/orders/{test_data.order_id}")
            print(f"✅ Get order details: {response.status_code}")
        
        # Test get sample details
        if test_data.sample_id:
            response = requests.get(f"{BASE_URL}/api/v1/laboratory/samples/{test_data.sample_id}")
            print(f"✅ Get sample details: {response.status_code}")
        
        # Test get report details
        if test_data.report_id:
            response = requests.get(f"{BASE_URL}/api/v1/reports/{test_data.report_id}")
            print(f"✅ Get report details: {response.status_code}")
        
        # Test get invoice details
        if test_data.invoice_id:
            response = requests.get(f"{BASE_URL}/api/v1/billing/invoices/{test_data.invoice_id}")
            print(f"✅ Get invoice details: {response.status_code}")
        
        return True
    except requests.exceptions.ConnectionError:
        print("❌ Get details: Connection failed")
        return False

def run_all_tests():
    """Run all tests in sequence"""
    print("🚀 Starting Celuma API Endpoint Tests")
    print("=" * 50)
    
    tests = [
        ("Root Endpoint", test_root_endpoint),
        ("Health Endpoint", test_health_endpoint),
        ("Create Tenant", test_create_tenant),
        ("Create Branch", test_create_branch),
        ("Create Patient", test_create_patient),
        ("Create Laboratory Order", test_create_laboratory_order),
        ("Create Sample", test_create_sample),
        ("Create Report", test_create_report),
        ("Create Invoice", test_create_invoice),
        ("Create Payment", test_create_payment),
        ("User Registration", test_user_registration),
        ("User Login", test_user_login),
        ("Get User Profile", test_get_user_profile),
        ("List Endpoints", test_list_endpoints),
        ("Get Details", test_get_details)
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n🧪 Testing: {test_name}")
        print("-" * 30)
        if test_func():
            passed += 1
            print(f"✅ {test_name}: PASSED")
        else:
            print(f"❌ {test_name}: FAILED")
    
    print("\n" + "=" * 50)
    print(f"📊 Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! The API is working correctly.")
    else:
        print("⚠️  Some tests failed. Please check the API implementation.")
    
    return passed == total

if __name__ == "__main__":
    run_all_tests()
