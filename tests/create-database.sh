# NOTE: if trying
if [ -z "$DB_PASS" ]; then
    DB_PASS=password
fi

pg_ctl -D tmp-pg stop
rm -rf tmp-pg
pg_ctl init -D tmp-pg
pg_ctl -D tmp-pg start
psql postgres -c 'create database example;'
uv run python -m pg_bitemporal | psql example
psql postgres -c "create user example with password '$DB_PASS';"
psql example -c 'grant all on all tables in schema public to example;'
psql example -c 'grant all on all sequences in schema public to example;'
psql example -c 'grant all on all functions in schema public to example;'
psql example -c 'alter default privileges for role example in schema public grant all on tables to example;'
