"""The install SQL ships inside the package."""

import subprocess
import sys

from pg_bitemporal import functions_sql


def test_functions_sql_is_packaged():
    sql = functions_sql()
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in sql
    assert "FUNCTION public.record_txn_history" in sql


def test_module_prints_the_install_sql():
    printed = subprocess.run(
        [sys.executable, "-m", "pg_bitemporal"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert printed == functions_sql()
