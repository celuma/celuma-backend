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
        response = requests.post(f"{BASE_URL}/api/v1/tenants/", params=data)
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
        response = requests.post(f"{BASE_URL}/api/v1/branches/", params=data)
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
            "patient_code": "P002",
            "first_name": "Jane",
            "last_name": "Smith",
            "dob": "1985-05-15",
            "sex": "F",
            "phone": "555-9876",
            "email": "jane.smith@example.com"
        }
        response = requests.post(f"{BASE_URL}/api/v1/patients/", params=data)
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

def test_create_order():
    """Test creating a laboratory order"""
    if not test_data.tenant_id or not test_data.branch_id or not test_data.patient_id:
        print("❌ Create order: Missing required IDs")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "patient_id": test_data.patient_id,
            "order_code": "ORD002",
            "requested_by": "Dr. Johnson",
            "notes": "Complete blood count and chemistry panel"
        }
        response = requests.post(f"{BASE_URL}/api/v1/laboratory/orders/", params=data)
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
        print("❌ Create sample: Missing required IDs")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "order_id": test_data.order_id,
            "sample_code": "SAMP002",
            "type": "SANGRE",
            "notes": "Blood sample for CBC and chemistry"
        }
        response = requests.post(f"{BASE_URL}/api/v1/laboratory/samples/", params=data)
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
        print("❌ Create report: Missing required IDs")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "order_id": test_data.order_id,
            "title": "Complete Blood Count Report",
            "diagnosis_text": "Normal CBC results, all parameters within reference range"
        }
        response = requests.post(f"{BASE_URL}/api/v1/reports/", params=data)
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
        print("❌ Create invoice: Missing required IDs")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "order_id": test_data.order_id,
            "invoice_number": "INV002",
            "amount_total": 2500.00,
            "currency": "MXN"
        }
        response = requests.post(f"{BASE_URL}/api/v1/billing/invoices/", params=data)
        print(f"✅ Create invoice: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.invoice_id = data.get('id')
            print(f"   Invoice ID: {test_data.invoice_id}")
            print(f"   Amount: {data.get('amount_total')} {data.get('currency')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create invoice: Connection failed")
        return False

def test_create_payment():
    """Test creating a payment"""
    if not test_data.tenant_id or not test_data.branch_id or not test_data.invoice_id:
        print("❌ Create payment: Missing required IDs")
        return False
    
    try:
        data = {
            "tenant_id": test_data.tenant_id,
            "branch_id": test_data.branch_id,
            "invoice_id": test_data.invoice_id,
            "amount_paid": 2500.00,
            "method": "credit_card"
        }
        response = requests.post(f"{BASE_URL}/api/v1/billing/payments/", params=data)
        print(f"✅ Create payment: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            test_data.payment_id = data.get('id')
            print(f"   Payment ID: {test_data.payment_id}")
            print(f"   Amount Paid: {data.get('amount_paid')}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Create payment: Connection failed")
        return False

def test_list_all_entities():
    """Test listing all entities to verify the complete flow"""
    print("\n📋 Verifying all entities in the system:")
    
    try:
        # List tenants
        response = requests.get(f"{BASE_URL}/api/v1/tenants/")
        if response.status_code == 200:
            tenants = response.json()
            print(f"   📊 Tenants: {len(tenants)}")
        
        # List branches
        response = requests.get(f"{BASE_URL}/api/v1/branches/")
        if response.status_code == 200:
            branches = response.json()
            print(f"   📊 Branches: {len(branches)}")
        
        # List patients
        response = requests.get(f"{BASE_URL}/api/v1/patients/")
        if response.status_code == 200:
            patients = response.json()
            print(f"   📊 Patients: {len(patients)}")
        
        # List orders
        response = requests.get(f"{BASE_URL}/api/v1/laboratory/orders/")
        if response.status_code == 200:
            orders = response.json()
            print(f"   📊 Orders: {len(orders)}")
        
        # List samples
        response = requests.get(f"{BASE_URL}/api/v1/laboratory/samples/")
        if response.status_code == 200:
            samples = response.json()
            print(f"   📊 Samples: {len(samples)}")
        
        # List reports
        response = requests.get(f"{BASE_URL}/api/v1/reports/")
        if response.status_code == 200:
            reports = response.json()
            print(f"   📊 Reports: {len(reports)}")
        
        # List invoices
        response = requests.get(f"{BASE_URL}/api/v1/billing/invoices/")
        if response.status_code == 200:
            invoices = response.json()
            print(f"   📊 Invoices: {len(invoices)}")
        
        # List payments
        response = requests.get(f"{BASE_URL}/api/v1/billing/payments/")
        if response.status_code == 200:
            payments = response.json()
            print(f"   📊 Payments: {len(payments)}")
        
        return True
    except Exception as e:
        print(f"❌ Error listing entities: {e}")
        return False

def main():
    """Run all endpoint tests"""
    print("🚀 Testing Celuma API Complete Flow")
    print("=" * 50)
    
    # Basic endpoint tests
    basic_tests = [
        test_root_endpoint,
        test_health_endpoint
    ]
    
    # Flow tests
    flow_tests = [
        test_create_tenant,
        test_create_branch,
        test_create_patient,
        test_create_order,
        test_create_sample,
        test_create_report,
        test_create_invoice,
        test_create_payment
    ]
    
    # Run basic tests first
    print("🔍 Running basic endpoint tests...")
    basic_passed = 0
    for test in basic_tests:
        if test():
            basic_passed += 1
        print()
    
    # Run flow tests
    print("🔄 Running complete flow tests...")
    flow_passed = 0
    for test in flow_tests:
        if test():
            flow_passed += 1
        print()
    
    # Verify all entities
    test_list_all_entities()
    
    # Summary
    print("\n" + "=" * 50)
    print(f"📊 Test Results:")
    print(f"   Basic endpoints: {basic_passed}/{len(basic_tests)} working")
    print(f"   Flow tests: {flow_passed}/{len(flow_tests)} working")
    
    total_tests = len(basic_tests) + len(flow_tests)
    total_passed = basic_passed + flow_passed
    
    print(f"   Total: {total_passed}/{total_tests} tests passed")
    
    if total_passed == total_tests:
        print("🎉 All tests passed! The complete flow is working correctly!")
    else:
        print("⚠️  Some tests failed. Check the output above for details.")
    
    print(f"\n💡 Test data created:")
    print(f"   Tenant ID: {test_data.tenant_id}")
    print(f"   Branch ID: {test_data.branch_id}")
    print(f"   Patient ID: {test_data.patient_id}")
    print(f"   Order ID: {test_data.order_id}")
    print(f"   Sample ID: {test_data.sample_id}")
    print(f"   Report ID: {test_data.report_id}")
    print(f"   Invoice ID: {test_data.invoice_id}")
    print(f"   Payment ID: {test_data.payment_id}")

if __name__ == "__main__":
    main()
