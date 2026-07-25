"""Pytest-only end-to-end tests."""


def load_tests(loader, tests, pattern):
    """Keep unittest discovery from importing pytest-only modules."""
    return tests
