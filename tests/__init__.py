"""
Celuma API Testing Suite

This package contains comprehensive tests for the Celuma API including:
- Complete flow tests
- Validation and error handling tests
- Performance tests
- Test data cleanup utilities
- Master test runner
"""

__version__ = "1.0.0"
__author__ = "Celuma Team"

# Import main test functions for easy access
from .test_endpoints import main as run_flow_tests
from .test_validation_errors import main as run_validation_tests
from .test_performance import main as run_performance_tests
from .cleanup_test_data import main as run_cleanup
from .run_all_tests import main as run_all_tests

__all__ = [
    "run_flow_tests",
    "run_validation_tests", 
    "run_performance_tests",
    "run_cleanup",
    "run_all_tests"
]
