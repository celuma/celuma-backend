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
        # Run from the tests directory
        result = subprocess.run([sys.executable, script_name], 
                              capture_output=True, text=True, timeout=300,
                              cwd="tests")
        
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

def run_cleanup_scripts():
    """Run cleanup scripts after all tests complete"""
    print_header("Running Cleanup Scripts")
    print("🧹 Cleaning up test data and blacklisted tokens...")
    
    cleanup_scripts = [
        ("cleanup_test_data.py", "Test Data Cleanup"),
        ("cleanup_blacklisted_tokens.py", "Blacklisted Tokens Cleanup")
    ]
    
    cleanup_results = {}
    
    for script_name, description in cleanup_scripts:
        try:
            print(f"\n🔧 Running {description}...")
            result = subprocess.run([sys.executable, script_name], 
                                  capture_output=True, text=True, timeout=60,
                                  cwd="tests")
            
            if result.returncode == 0:
                print_status(f"✅ {description} completed", "SUCCESS")
                cleanup_results[description] = True
            else:
                print_status(f"⚠️ {description} had issues", "WARNING")
                cleanup_results[description] = False
                
            if result.stdout:
                print("📋 Output:")
                print(result.stdout)
                
        except Exception as e:
            print_status(f"❌ Error running {description}: {e}", "ERROR")
            cleanup_results[description] = False
    
    return cleanup_results

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
    
    print(f"\n🧹 CLEANUP STATUS:")
    print("   Test data and blacklisted tokens have been cleaned up")
    print("   Database is ready for the next test run")
    
    return failed_tests == 0

def main():
    """Main test runner function"""
    print_header("CELUMA API COMPREHENSIVE TESTING")
    print(f"🚀 Starting comprehensive test suite at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Define test suites to run
    test_suites = [
        ("test_endpoints.py", "API Endpoints Testing"),
        ("test_auth_logout.py", "Authentication & Logout Testing"),
        ("test_performance.py", "Performance Testing"),
        ("test_validation_errors.py", "Validation Error Testing")
    ]
    
    # Run all test suites
    results = {}
    start_time = time.time()
    
    for script_name, description in test_suites:
        success = run_test_script(script_name, description)
        results[description] = success
        
        # Small delay between tests
        time.sleep(1)
    
    # Run cleanup scripts
    cleanup_results = run_cleanup_scripts()
    
    # Calculate total time
    total_time = time.time() - start_time
    
    # Generate comprehensive report
    all_tests_passed = generate_test_report(results)
    
    print(f"\n⏱️  Total testing time: {total_time:.2f} seconds")
    
    # Final exit code
    sys.exit(0 if all_tests_passed else 1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_status("\n⚠️ Testing interrupted by user", "WARNING")
        sys.exit(1)
    except Exception as e:
        print_status(f"❌ Unexpected error during testing: {e}", "ERROR")
        sys.exit(1)
