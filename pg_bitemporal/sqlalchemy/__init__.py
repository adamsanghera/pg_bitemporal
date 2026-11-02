"""SQLAlchemy bindings for bitemporal tables."""

from sqlalchemy import Connection, Engine

from pg_bitemporal import functions_sql

__all__ = ["install_functions"]


def install_functions(bind: Engine | Connection) -> None:
    """Install or upgrade the pg_bitemporal functions and extensions.

    Run it before ``create_all``, which creates triggers that call these
    functions. Given an Engine, it commits on its own. Given a Connection, it
    runs inside that connection's transaction and leaves the commit to you.
    """
    if isinstance(bind, Engine):
        with bind.begin() as connection:
            _run_script(connection)
    else:
        _run_script(bind)


def _run_script(connection: Connection) -> None:
    # Straight to the driver: the script holds several statements and literal
    # `%` signs, which SQLAlchemy's parameter handling would misread.
    cursor = connection.connection.cursor()
    try:
        cursor.execute(functions_sql())
    finally:
        cursor.close()
