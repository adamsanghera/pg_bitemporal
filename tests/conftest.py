"""Shared pytest settings and the Postgres fixture."""

import os
from collections.abc import Callable, Iterator

import pytest
from psycopg import OperationalError as PsycopgOperationalError
from sqlalchemy import Engine, MetaData, create_engine, event, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError

from pg_bitemporal.sqlalchemy import install_functions


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


def database_url() -> str:
    """The database the tests rebuild. ``PG_BITEMPORAL_TEST_URL`` overrides it."""
    return os.environ.get(
        "PG_BITEMPORAL_TEST_URL", "postgresql+psycopg:///pg_bitemporal_test"
    )


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    dbname = parsed.database
    if dbname is None or not dbname.isidentifier():
        raise RuntimeError(
            "PG_BITEMPORAL_TEST_URL must name a simple database to create"
        )

    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": dbname},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="module")
def make_engine() -> Iterator[Callable[[MetaData], Engine]]:
    """Return a function that rebuilds the test database for one metadata.

    It drops and recreates schema ``public``, installs the functions, and creates
    the tables. Sessions run in UTC. Skips the test when Postgres is unreachable.
    """
    engines: list[Engine] = []

    def build(metadata: MetaData) -> Engine:
        url = database_url()
        try:
            _ensure_database(url)
            engine = create_engine(url)
            engines.append(engine)

            @event.listens_for(engine, "connect")
            def _utc(dbapi_connection, _record) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("SET TIME ZONE 'UTC'")
                cursor.close()

            with engine.begin() as conn:
                conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
            install_functions(engine)
            metadata.create_all(engine)
        except (OperationalError, PsycopgOperationalError) as exc:
            pytest.skip(f"PostgreSQL is not reachable at {url}: {exc}")
        return engine

    yield build
    for engine in engines:
        engine.dispose()
