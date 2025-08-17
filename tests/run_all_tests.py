#!/usr/bin/env python3
"""
Master test runner for Celuma API
Runs all test suites and generates comprehensive report
"""

import subprocess
import sys
import time
from datetime import datetime

def print_header(title):
    """Print a formatted header"""
    print(f"\n{'='*80}")
    print(f"🚀 {title}")
    print(f"{'='*80}")

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

def run_test_script(script_name, description):
    """Run a test script and return success status"""
    print_header(f"Running {description}")
    print(f"📁 Script: {script_name}")
    print(f"⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        result = subprocess.run([sys.executable, script_name], 
                              capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            print_status(f"✅ {description} completed successfully", "SUCCESS")
            if result.stdout:
                print("📋 Output:")
                print(result.stdout)
            return True
        else:
            print_status(f"❌ {description} failed with return code {result.returncode}", "ERROR")
            if result.stderr:
                print("❌ Error output:")
                print(result.stderr)
            if result.stdout:
                print("📋 Standard output:")
                print(result.stdout)
            return False
            
    except subprocess.TimeoutExpired:
        print_status(f"⏰ {description} timed out after 5 minutes", "ERROR")
        return False
    except Exception as e:
        print_status(f"❌ Error running {description}: {e}", "ERROR")
        return False

def generate_test_report(results):
    """Generate a comprehensive test report"""
    print_header("CELUMA API COMPREHENSIVE TEST REPORT")
    print(f"📅 Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    total_tests = len(results)
    passed_tests = sum(1 for result in results.values() if result)
    failed_tests = total_tests - passed_tests
    success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0
    
    print(f"\n📊 TEST SUMMARY:")
    print(f"   Total test suites: {total_tests}")
    print(f"   ✅ Passed: {passed_tests}")
    print(f"   ❌ Failed: {failed_tests}")
    print(f"   Success rate: {success_rate:.1f}%")
    
    print(f"\n📋 DETAILED RESULTS:")
    for test_name, result in results.items():
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"   {test_name}: {status}")
    
    print(f"\n🎯 FINAL STATUS:")
    if failed_tests == 0:
        print_status("🎉 ALL TESTS PASSED! The Celuma API is working perfectly!", "SUCCESS")
        print("\n💡 Recommendations:")
        print("   🚀 System is ready for production deployment")
        print("   📊 Continue monitoring performance and error rates")
        print("   🔄 Run tests regularly to maintain quality")
    elif failed_tests <= 2:
        print_status("⚠️ Most tests passed, but some issues were found", "WARNING")
        print("\n💡 Recommendations:")
        print("   🔍 Review failed tests to identify minor issues")
        print("   🧪 Fix issues and re-run specific test suites")
        print("   ✅ System may be ready for production after fixes")
    else:
        print_status("❌ Multiple test failures indicate significant issues", "ERROR")
        print("\n💡 Recommendations:")
        print("   🚨 System has significant issues requiring immediate attention")
        print("   🔧 Fix critical issues before proceeding")
        print("   🧪 Re-run tests after fixes to verify resolution")
    
    return success_rate == 100

def main():
    """Main test runner function"""
    start_time = datetime.now()
    
    print_header("CELUMA API COMPREHENSIVE TESTING SUITE")
    print(f"📅 Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Define test suites
    test_suites = {
        "Complete Flow Tests": "test_endpoints.py",
        "Validation & Error Tests": "test_validation_errors.py", 
        "Performance Tests": "test_performance.py",
        "Logout Functionality Tests": "test_auth_logout.py"
    }
    
    # Run all test suites
    results = {}
    for description, script_name in test_suites.items():
        success = run_test_script(script_name, description)
        results[description] = success
        
        # Add separator between tests
        if description != list(test_suites.keys())[-1]:
            print("\n" + "-" * 80)
    
    # Generate comprehensive report
    all_passed = generate_test_report(results)
    
    # Final summary
    end_time = datetime.now()
    duration = end_time - start_time
    
    print(f"\n⏱️ Total testing duration: {duration}")
    print(f"🏁 Testing completed at: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Exit with appropriate code for CI systems
    sys.exit(0 if all_passed else 1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_status("\n⚠️ Testing interrupted by user", "WARNING")
        sys.exit(1)
    except Exception as e:
        print_status(f"❌ Unexpected error during testing: {e}", "ERROR")
        sys.exit(1)
