"""Shared pytest settings."""

import os

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """With PG_BITEMPORAL_REQUIRE_DB set, a test skipped for lack of Postgres fails.

    Locally the database tests skip when no server is up. CI sets the variable so
    a broken service container cannot pass as green.
    """
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("PG_BITEMPORAL_REQUIRE_DB") and report.skipped:
        report.outcome = "failed"
        report.longrepr = (
            f"skipped, but PG_BITEMPORAL_REQUIRE_DB is set: {report.longrepr}"
        )
