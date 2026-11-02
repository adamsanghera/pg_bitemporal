"""The README quickstart runs and prints what the README says it prints."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg import OperationalError as PsycopgOperationalError
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError

README = Path(__file__).resolve().parents[1] / "README.md"
QUICKSTART_URL = "postgresql+psycopg:///bank"


def _quickstart() -> tuple[str, str]:
    section = README.read_text(encoding="utf-8").split("## Quickstart", 1)[1]
    code, output = re.findall(r"```(?:python)?\n(.*?)```", section, re.DOTALL)[:2]
    return code, output


def _fresh_database() -> str:
    raw = os.environ.get(
        "PG_BITEMPORAL_TEST_URL", "postgresql+psycopg:///pg_bitemporal_test"
    )
    url = make_url(raw).set(database="pg_bitemporal_readme_test")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    return url.render_as_string(hide_password=False)


def test_readme_quickstart_runs(tmp_path):
    code, expected = _quickstart()
    assert QUICKSTART_URL in code
    try:
        url = _fresh_database()
    except (OperationalError, PsycopgOperationalError) as exc:
        pytest.skip(f"PostgreSQL is not reachable: {exc}")

    script = tmp_path / "quickstart.py"
    script.write_text(code.replace(QUICKSTART_URL, url), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
