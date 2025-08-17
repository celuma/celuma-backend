#!/usr/bin/env python3
"""
Main test runner script for Celuma API
This script provides easy access to all test suites from the project root
"""

import sys
import os

# Add the tests directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'tests'))

def main():
    """Main test runner with menu interface"""
    print("🚀 Celuma API Test Runner")
    print("=" * 40)
    print("Available test suites:")
    print("1. Complete Flow Tests")
    print("2. Validation & Error Tests") 
    print("3. Performance Tests")
    print("4. Test Data Cleanup")
    print("5. Run All Tests")
    print("6. Exit")
    print("=" * 40)
    
    while True:
        try:
            choice = input("\nSelect an option (1-6): ").strip()
            
            if choice == "1":
                print("\n🔍 Running Complete Flow Tests...")
                from tests.test_endpoints import main as run_flow
                run_flow()
                break
                
            elif choice == "2":
                print("\n🔍 Running Validation & Error Tests...")
                from tests.test_validation_errors import main as run_validation
                run_validation()
                break
                
            elif choice == "3":
                print("\n🔍 Running Performance Tests...")
                from tests.test_performance import main as run_performance
                run_performance()
                break
                
            elif choice == "4":
                print("\n🔍 Running Test Data Cleanup...")
                from tests.cleanup_test_data import main as run_cleanup
                run_cleanup()
                break
                
            elif choice == "5":
                print("\n🔍 Running All Tests...")
                from tests.run_all_tests import main as run_all
                run_all()
                break
                
            elif choice == "6":
                print("👋 Goodbye!")
                break
                
            else:
                print("❌ Invalid option. Please select 1-6.")
                
        except KeyboardInterrupt:
            print("\n\n👋 Test execution interrupted. Goodbye!")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            print("Please make sure you're running this from the project root directory.")
            break

if __name__ == "__main__":
    main()
