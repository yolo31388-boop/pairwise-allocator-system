"""Give plain test classes the unittest-style assertions they use.

The test files must not be modified, so the missing assertion helpers
are injected here at collection time.
"""


def _assert_is_not_none(self, obj, msg=None):
    if obj is None:
        raise AssertionError(msg or "unexpectedly None")


def pytest_pycollect_makeitem(collector, name, obj):
    if isinstance(obj, type) and name.startswith("Test"):
        if not hasattr(obj, "assertIsNotNone"):
            obj.assertIsNotNone = _assert_is_not_none
