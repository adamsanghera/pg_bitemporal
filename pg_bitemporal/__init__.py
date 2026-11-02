"""Bitemporal tables in Postgres, with SQLAlchemy and Django bindings."""

from importlib.resources import files

__all__ = ["functions_sql"]


def functions_sql() -> str:
    """Return the SQL that installs the trigger and helper functions.

    The script is idempotent: it creates the btree_gist and hstore extensions if
    they are missing and replaces each function. ``python -m pg_bitemporal``
    prints it, for ``psql`` or a migration tool.
    """
    return files(__package__).joinpath("functions.sql").read_text(encoding="utf-8")
