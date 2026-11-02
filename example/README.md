prereqs:

1. create database `example` with username/password `example`, allowed to create
   extensions (the app installs `btree_gist`, `hstore`, and the functions itself)
1. run `uv run python example/sqlalchemy/app.py` from the repository root

## The Late Letter

`example/web/` is a short game that teaches bitemporal data one idea at a
time, at http://127.0.0.1:8080:

1. One timeline: where did Ada live?
2. A second axis: when did the bank find out?
3. Your first write, previewed on the chart before it lands.
4. Correcting a mistake without erasing it. "Under the hood" opens here and
   shows the real `residence` and `residence_history` rows, the SQL each
   write sent, and what the history trigger did.
5. Backdating, and why Postgres refuses it.
6. Free play with every tool.

It needs a local Postgres. It creates `pg_bitemporal_example` on port 5432 and
installs the functions with `install_functions`. The chart uses
d3 from cdnjs.

    uv run python example/web/app.py

Set `PG_BITEMPORAL_EXAMPLE_URL` to use a different server.
Set `PORT` to listen somewhere other than 8080.
