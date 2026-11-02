"""Print the install SQL: ``python -m pg_bitemporal | psql <database>``.

Python runs this file only for ``python -m pg_bitemporal``. ``import pg_bitemporal``
never loads it, and importing it directly prints nothing either.
"""

from pg_bitemporal import functions_sql


def main() -> None:
    print(functions_sql(), end="")


if __name__ == "__main__":
    main()
