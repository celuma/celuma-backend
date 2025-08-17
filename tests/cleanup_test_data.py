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
            ("tenants", "/api/v1/tenants/")
        ]
        
        for name, endpoint in endpoints:
            response = requests.get(f"{BASE_URL}{endpoint}")
            if response.status_code == 200:
                entities[name] = response.json()
                print(f"📊 Found {len(entities[name])} {name}")
            else:
                print(f"❌ Failed to get {name}: {response.status_code}")
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
    
    print("\n" + "=" * 40)
    print("✅ Cleanup analysis completed!")
    print("💡 To implement actual deletion, add DELETE endpoints to the API")

if __name__ == "__main__":
    main()
