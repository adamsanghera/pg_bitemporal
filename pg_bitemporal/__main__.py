"""Print the install SQL: ``python -m pg_bitemporal | psql <database>``."""

from pg_bitemporal import functions_sql

print(functions_sql(), end="")
