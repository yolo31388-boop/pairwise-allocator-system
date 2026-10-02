"""Shipped tests use unittest-style assertions on a plain class.

Attach the needed assertion helpers without modifying the test files.
"""
import unittest

from tests.test_allocator import TestAllocatorSystem

_HELPERS = (
    "assertIsNotNone", "assertIsNone", "assertTrue", "assertFalse",
    "assertEqual", "assertNotEqual", "assertIn", "assertRaises",
    "fail", "_formatMessage",
)

for _name in _HELPERS:
    if not hasattr(TestAllocatorSystem, _name):
        setattr(TestAllocatorSystem, _name, getattr(unittest.TestCase, _name))

if not hasattr(TestAllocatorSystem, "failureException"):
    TestAllocatorSystem.failureException = unittest.TestCase.failureException
