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
from .run_all_tests import run_all_tests

__all__ = [
    "run_all_tests"
]
