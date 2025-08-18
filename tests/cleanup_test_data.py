#!/usr/bin/env python3
"""
Script to clean up test data created during testing
This will remove all test entities in the correct order to avoid foreign key constraints
"""

import requests
import json
from typing import List, Dict, Any

BASE_URL = "http://localhost:8000"

def get_all_entities():
    """Get all entities from the system"""
    entities = {}
    
    try:
        # Get all entities
        endpoints = [
            ("payments", "/api/v1/billing/payments/"),
            ("invoices", "/api/v1/billing/invoices/"),
            ("reports", "/api/v1/reports/"),
            ("samples", "/api/v1/laboratory/samples/"),
            ("orders", "/api/v1/laboratory/orders/"),
            ("patients", "/api/v1/patients/"),
            ("branches", "/api/v1/branches/"),
            ("tenants", "/api/v1/tenants/"),
            ("users", "/api/v1/auth/users")  # Note: This endpoint might not exist yet
        ]
        
        for name, endpoint in endpoints:
            try:
                response = requests.get(f"{BASE_URL}{endpoint}")
                if response.status_code == 200:
                    entities[name] = response.json()
                    print(f"📊 Found {len(entities[name])} {name}")
                else:
                    print(f"⚠️  Endpoint {endpoint} not available (status: {response.status_code})")
                    entities[name] = []
            except Exception as e:
                print(f"⚠️  Could not access {endpoint}: {e}")
                entities[name] = []
        
        return entities
    except Exception as e:
        print(f"❌ Error getting entities: {e}")
        return {}

def cleanup_entities(entities: Dict[str, List]):
    """Clean up entities in the correct order to avoid foreign key constraints"""
    print("\n🧹 Starting cleanup process...")
    
    # Order of deletion (respecting foreign key constraints)
    deletion_order = [
        "payments",
        "invoices", 
        "reports",
        "samples",
        "orders",
        "patients",
        "branches",
        "users",
        "tenants"
    ]
    
    total_deleted = 0
    
    for entity_type in deletion_order:
        if entity_type in entities and entities[entity_type]:
            print(f"\n🗑️  Cleaning up {entity_type}...")
            
            for entity in entities[entity_type]:
                entity_id = entity.get('id')
                if entity_id:
                    try:
                        # For now, we'll just list what would be deleted
                        # Since we don't have DELETE endpoints implemented yet
                        print(f"   Would delete {entity_type} ID: {entity_id}")
                        total_deleted += 1
                    except Exception as e:
                        print(f"   ❌ Error deleting {entity_type} {entity_id}: {e}")
    
    print(f"\n📊 Total entities that would be deleted: {total_deleted}")
    print("💡 Note: DELETE endpoints are not implemented yet, so no actual deletion occurred")

def cleanup_blacklisted_tokens():
    """Clean up blacklisted tokens from logout tests"""
    print("\n🔒 Checking for blacklisted tokens...")
    
    try:
        # This would require a direct database cleanup or a cleanup endpoint
        # For now, we'll just inform about the cleanup process
        print("💡 Blacklisted tokens cleanup would happen here")
        print("   - These are created during logout tests")
        print("   - They should be cleaned up to prevent database bloat")
        print("   - Consider implementing a cleanup endpoint or scheduled task")
        
    except Exception as e:
        print(f"⚠️  Could not check blacklisted tokens: {e}")

def cleanup_test_users():
    """Clean up test users created during testing"""
    print("\n👥 Checking for test users...")
    
    try:
        # This would require user management endpoints
        print("💡 Test users cleanup would happen here")
        print("   - Test users are created during authentication tests")
        print("   - They should be cleaned up after tests complete")
        print("   - Consider implementing user deletion endpoints")
        
    except Exception as e:
        print(f"⚠️  Could not check test users: {e}")

def main():
    """Main cleanup function"""
    print("🧹 Celuma API Test Data Cleanup")
    print("=" * 40)
    
    # Get all entities
    print("🔍 Scanning system for test data...")
    entities = get_all_entities()
    
    if not entities:
        print("❌ No entities found or error occurred")
        return
    
    # Show summary
    print(f"\n📋 Summary of entities found:")
    total_entities = sum(len(entity_list) for entity_list in entities.values())
    print(f"   Total entities: {total_entities}")
    
    for entity_type, entity_list in entities.items():
        if entity_list:
            print(f"   {entity_type.capitalize()}: {len(entity_list)}")
    
    # Ask for confirmation
    print(f"\n⚠️  This will show what would be deleted (no actual deletion)")
    print("   To actually delete, you would need to implement DELETE endpoints")
    
    # Cleanup
    cleanup_entities(entities)
    
    # Additional cleanup tasks
    cleanup_blacklisted_tokens()
    cleanup_test_users()
    
    print("\n" + "=" * 40)
    print("✅ Cleanup analysis completed!")
    print("💡 To implement actual deletion, add DELETE endpoints to the API")
    print("💡 Consider implementing scheduled cleanup tasks for test data")

if __name__ == "__main__":
    main()
