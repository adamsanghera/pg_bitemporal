"""Bitemporal tables in Postgres, with SQLAlchemy and Django bindings."""

from importlib.resources import files

__all__ = ["functions_sql"]


def functions_sql() -> str:
    """Return the SQL that installs the trigger and helper functions.

    The script is the files in ``pg_bitemporal/sql/``, joined in name order: the
    numeric prefixes put the extensions first. It is idempotent: it creates the
    btree_gist and hstore extensions if they are missing and replaces each
    function. ``python -m pg_bitemporal`` prints it, for ``psql`` or a migration
    tool.
    """
    scripts = sorted(
        (entry for entry in files(__package__).joinpath("sql").iterdir()),
        key=lambda entry: entry.name,
    )
    return "\n".join(
        entry.read_text(encoding="utf-8")
        for entry in scripts
        if entry.name.endswith(".sql")
    )
