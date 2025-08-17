#!/usr/bin/env python3
"""
Basic performance test script for the Celuma API
Tests response times for various endpoints
"""

import requests
import time
import statistics
from typing import List, Dict, Any

BASE_URL = "http://localhost:8000"

def measure_response_time(endpoint: str, method: str = "GET", data: Dict = None) -> float:
    """Measure response time for an endpoint"""
    start_time = time.time()
    
    try:
        if method == "GET":
            response = requests.get(f"{BASE_URL}{endpoint}")
        elif method == "POST":
            response = requests.post(f"{BASE_URL}{endpoint}", params=data)
        else:
            return -1
        
        end_time = time.time()
        response_time = (end_time - start_time) * 1000  # Convert to milliseconds
        
        return response_time if response.status_code == 200 else -1
    except Exception:
        return -1

def test_endpoint_performance(endpoint: str, method: str = "GET", data: Dict = None, iterations: int = 10):
    """Test performance of a specific endpoint"""
    print(f"🔍 Testing {method} {endpoint}...")
    
    response_times = []
    successful_requests = 0
    
    for i in range(iterations):
        response_time = measure_response_time(endpoint, method, data)
        if response_time >= 0:
            response_times.append(response_time)
            successful_requests += 1
        time.sleep(0.1)  # Small delay between requests
    
    if response_times:
        avg_time = statistics.mean(response_times)
        min_time = min(response_times)
        max_time = max(response_times)
        median_time = statistics.median(response_times)
        
        print(f"   ✅ Successful requests: {successful_requests}/{iterations}")
        print(f"   📊 Response times (ms):")
        print(f"      Average: {avg_time:.2f}")
        print(f"      Median:  {median_time:.2f}")
        print(f"      Min:     {min_time:.2f}")
        print(f"      Max:     {max_time:.2f}")
        
        return {
            "endpoint": endpoint,
            "method": method,
            "successful": successful_requests,
            "total": iterations,
            "avg_time": avg_time,
            "median_time": median_time,
            "min_time": min_time,
            "max_time": max_time
        }
    else:
        print(f"   ❌ No successful requests")
        return None

def test_basic_endpoints():
    """Test performance of basic endpoints"""
    print("🚀 Testing Basic Endpoints Performance")
    print("=" * 50)
    
    basic_endpoints = [
        ("/", "GET"),
        ("/api/v1/health", "GET"),
        ("/api/v1/tenants/", "GET"),
        ("/api/v1/branches/", "GET"),
        ("/api/v1/patients/", "GET"),
        ("/api/v1/laboratory/orders/", "GET"),
        ("/api/v1/laboratory/samples/", "GET"),
        ("/api/v1/reports/", "GET"),
        ("/api/v1/billing/invoices/", "GET"),
        ("/api/v1/billing/payments/", "GET")
    ]
    
    results = []
    
    for endpoint, method in basic_endpoints:
        result = test_endpoint_performance(endpoint, method)
        if result:
            results.append(result)
        print()
    
    return results

def test_creation_performance():
    """Test performance of creation endpoints"""
    print("🚀 Testing Creation Endpoints Performance")
    print("=" * 50)
    
    # Get existing data for creation tests
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
                                
                                # Test creation endpoints
                                creation_endpoints = [
                                    ("/api/v1/tenants/", "POST", {
                                        "name": f"PerfTest_{int(time.time())}",
                                        "legal_name": "Performance Test Tenant"
                                    }),
                                    ("/api/v1/branches/", "POST", {
                                        "tenant_id": tenant_id,
                                        "code": f"PERF_{int(time.time())}",
                                        "name": "Performance Test Branch",
                                        "city": "Test City"
                                    }),
                                    ("/api/v1/patients/", "POST", {
                                        "tenant_id": tenant_id,
                                        "branch_id": branch_id,
                                        "patient_code": f"PERF_{int(time.time())}",
                                        "first_name": "Performance",
                                        "last_name": "Test"
                                    })
                                ]
                                
                                results = []
                                
                                for endpoint, method, data in creation_endpoints:
                                    result = test_endpoint_performance(endpoint, method, data, iterations=5)
                                    if result:
                                        results.append(result)
                                    print()
                                
                                return results
    except Exception as e:
        print(f"❌ Error setting up creation tests: {e}")
        return []

def generate_performance_report(basic_results: List, creation_results: List):
    """Generate a performance report"""
    print("📊 Performance Test Report")
    print("=" * 50)
    
    if basic_results:
        print("\n🔍 Basic Endpoints Performance:")
        avg_response_times = [r['avg_time'] for r in basic_results]
        overall_avg = statistics.mean(avg_response_times)
        print(f"   Overall average response time: {overall_avg:.2f} ms")
        
        # Find fastest and slowest endpoints
        fastest = min(basic_results, key=lambda x: x['avg_time'])
        slowest = max(basic_results, key=lambda x: x['avg_time'])
        
        print(f"   Fastest endpoint: {fastest['endpoint']} ({fastest['avg_time']:.2f} ms)")
        print(f"   Slowest endpoint: {slowest['endpoint']} ({slowest['avg_time']:.2f} ms)")
    
    if creation_results:
        print("\n🔍 Creation Endpoints Performance:")
        avg_response_times = [r['avg_time'] for r in creation_results]
        overall_avg = statistics.mean(avg_response_times)
        print(f"   Overall average creation time: {overall_avg:.2f} ms")
        
        # Find fastest and slowest creation endpoints
        fastest = min(creation_results, key=lambda x: x['avg_time'])
        slowest = max(creation_results, key=lambda x: x['avg_time'])
        
        print(f"   Fastest creation: {fastest['endpoint']} ({fastest['avg_time']:.2f} ms)")
        print(f"   Slowest creation: {slowest['endpoint']} ({slowest['avg_time']:.2f} ms)")
    
    print("\n💡 Performance Recommendations:")
    if basic_results:
        avg_response_times = [r['avg_time'] for r in basic_results]
        overall_avg = statistics.mean(avg_response_times)
        
        if overall_avg < 100:
            print("   ✅ Excellent performance! All endpoints respond quickly.")
        elif overall_avg < 500:
            print("   ✅ Good performance! Most endpoints respond within acceptable time.")
        elif overall_avg < 1000:
            print("   ⚠️  Moderate performance. Consider optimizing slower endpoints.")
        else:
            print("   ❌ Poor performance. Endpoints are too slow and need optimization.")

def main():
    """Run all performance tests"""
    print("🚀 Celuma API Performance Testing")
    print("=" * 60)
    
    # Test basic endpoints
    basic_results = test_basic_endpoints()
    
    print("\n" + "=" * 60)
    
    # Test creation endpoints
    creation_results = test_creation_performance()
    
    print("\n" + "=" * 60)
    
    # Generate report
    generate_performance_report(basic_results, creation_results)
    
    print("\n" + "=" * 60)
    print("✅ Performance testing completed!")

if __name__ == "__main__":
    main()
