#!/usr/bin/env python3
"""
Master test script for the Celuma API
Runs all test suites and generates a comprehensive report
"""

import subprocess
import sys
import time
import os
from datetime import datetime

def run_test_script(script_name: str, description: str):
    """Run a test script and capture its output"""
    print(f"\n{'='*60}")
    print(f"🚀 Running {description}")
    print(f"{'='*60}")
    
    try:
        # Run the test script from the current directory (tests/)
        script_path = os.path.join(os.path.dirname(__file__), script_name)
        result = subprocess.run([sys.executable, script_path], 
                              capture_output=True, 
                              text=True, 
                              timeout=300)  # 5 minute timeout
        
        if result.returncode == 0:
            print("✅ Test script completed successfully")
            print("\n📋 Output:")
            print(result.stdout)
            if result.stderr:
                print("\n⚠️  Warnings/Errors:")
                print(result.stderr)
            return True, result.stdout
        else:
            print("❌ Test script failed")
            print(f"Return code: {result.returncode}")
            print("\n📋 Output:")
            print(result.stdout)
            if result.stderr:
                print("\n❌ Errors:")
                print(result.stderr)
            return False, result.stdout + "\n" + result.stderr
            
    except subprocess.TimeoutExpired:
        print("⏰ Test script timed out after 5 minutes")
        return False, "TIMEOUT"
    except Exception as e:
        print(f"❌ Error running test script: {e}")
        return False, str(e)

def generate_test_report(results: dict):
    """Generate a comprehensive test report"""
    print(f"\n{'='*80}")
    print("📊 CELUMA API COMPREHENSIVE TEST REPORT")
    print(f"{'='*80}")
    print(f"📅 Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Summary
    total_tests = len(results)
    passed_tests = sum(1 for result in results.values() if result['success'])
    failed_tests = total_tests - passed_tests
    
    print(f"\n📈 TEST SUMMARY:")
    print(f"   Total test suites: {total_tests}")
    print(f"   ✅ Passed: {passed_tests}")
    print(f"   ❌ Failed: {failed_tests}")
    print(f"   Success rate: {(passed_tests/total_tests)*100:.1f}%")
    
    # Detailed results
    print(f"\n🔍 DETAILED RESULTS:")
    for test_name, result in results.items():
        status = "✅ PASS" if result['success'] else "❌ FAIL"
        print(f"   {status} {test_name}")
        if not result['success']:
            print(f"      Error: {result['error']}")
    
    # Recommendations
    print(f"\n💡 RECOMMENDATIONS:")
    if failed_tests == 0:
        print("   🎉 All tests passed! The API is working correctly.")
        print("   💪 The system is ready for production use.")
    elif failed_tests <= 2:
        print("   ⚠️  Most tests passed. Review failed tests for minor issues.")
        print("   🔧 Fix the identified issues before production deployment.")
    else:
        print("   ❌ Multiple tests failed. The API has significant issues.")
        print("   🚨 Do not deploy to production until all issues are resolved.")
    
    # Performance insights
    if 'performance' in results and results['performance']['success']:
        print(f"\n⚡ PERFORMANCE INSIGHTS:")
        output = results['performance']['output']
        if "Excellent performance" in output:
            print("   🚀 API performance is excellent!")
        elif "Good performance" in output:
            print("   ✅ API performance is good.")
        elif "Moderate performance" in output:
            print("   ⚠️  API performance is moderate. Consider optimizations.")
        else:
            print("   ❌ API performance needs improvement.")
    
    # Validation insights
    if 'validation' in results and results['validation']['success']:
        print(f"\n🔍 VALIDATION INSIGHTS:")
        output = results['validation']['output']
        if "Correctly returned 404" in output and "Correctly returned 422" in output:
            print("   ✅ API validation is working correctly.")
        else:
            print("   ⚠️  Some validation tests may need attention.")
    
    print(f"\n{'='*80}")

def main():
    """Run all test suites"""
    print("🚀 CELUMA API COMPREHENSIVE TESTING SUITE")
    print("=" * 80)
    print(f"📅 Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Define test suites
    test_suites = {
        "test_endpoints.py": "Complete Flow Tests",
        "test_validation_errors.py": "Validation and Error Handling Tests", 
        "test_performance.py": "Performance Tests"
    }
    
    results = {}
    
    # Run each test suite
    for script_name, description in test_suites.items():
        success, output = run_test_script(script_name, description)
        results[description] = {
            'success': success,
            'output': output,
            'error': output if not success else None
        }
        
        # Small delay between tests
        time.sleep(1)
    
    # Generate comprehensive report
    generate_test_report(results)
    
    # Final status
    total_tests = len(results)
    passed_tests = sum(1 for result in results.values() if result['success'])
    
    print(f"\n🎯 FINAL STATUS:")
    if passed_tests == total_tests:
        print("   🎉 ALL TESTS PASSED! The Celuma API is working perfectly!")
        return 0
    else:
        print(f"   ⚠️  {failed_tests} out of {total_tests} test suites failed.")
        print("   🔧 Please review the failed tests above and fix the issues.")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
