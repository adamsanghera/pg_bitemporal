-- The advisory lock id for `row_value`'s key: a hash of the table and the typed key
-- values, so it does not depend on DateStyle or TimeZone. `row_value` is a row of the
-- current table, and only its key columns are read. Two keys that happen to share a
-- hash only make each other wait.
CREATE OR REPLACE FUNCTION public._bitemporal_lock_id (row_value anyelement, key_columns text[])
    RETURNS bigint
    AS $$
DECLARE
    lock_id bigint;
BEGIN
    EXECUTE format('SELECT hash_record_extended(ROW(%s::oid, %s), 0)',
        pg_typeof(row_value)::text::regclass::oid,
        (SELECT string_agg(format('($1).%I', col), ', ') FROM unnest(key_columns) AS col))
    INTO lock_id
    USING row_value;
    RETURN lock_id;
END;
$$ LANGUAGE plpgsql;

-- Lock one bitemporal key until the transaction ends. The trigger passes OLD or NEW.
CREATE OR REPLACE FUNCTION public.lock_bitemporal_key (row_value anyelement, key_columns text[])
    RETURNS void
    AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(public._bitemporal_lock_id(row_value, key_columns));
END;
$$ LANGUAGE plpgsql;

-- Lock several keys of one table until the transaction ends, in one call. The locks are
-- taken in lock-id order, so two transactions locking overlapping sets queue on the
-- first key they share instead of deadlocking, whatever order the caller listed them.
-- Writer.lock_keys builds `row_values` from the keys' values.
CREATE OR REPLACE FUNCTION public.lock_bitemporal_keys (row_values anyarray, key_columns text[])
    RETURNS void
    AS $$
DECLARE
    lock_id bigint;
BEGIN
    FOR lock_id IN
        SELECT DISTINCT public._bitemporal_lock_id(r, key_columns)
        FROM unnest(row_values) AS r
        ORDER BY 1
    LOOP
        PERFORM pg_advisory_xact_lock(lock_id);
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- The latest transaction time at which any version of `row_value`'s key was written:
-- a current row's start, or a history row's end. NULL for a key never written.
CREATE OR REPLACE FUNCTION public._bitemporal_key_last_write (
    row_value anyelement, history_table regclass, key_columns text[])
    RETURNS timestamptz
    AS $$
DECLARE
    matches text;
    last_write timestamptz;
BEGIN
    SELECT string_agg(format('t.%1$I = ($1).%1$I', col), ' AND ') INTO matches
    FROM unnest(key_columns) AS col;
    EXECUTE format(
        'SELECT greatest('
        '(SELECT max(lower(txn_period)) FROM %1$s AS t WHERE %3$s), '
        '(SELECT max(upper(txn_period)) FROM %2$s AS t WHERE %3$s))',
        pg_typeof(row_value)::text::regclass, history_table, matches)
    INTO last_write
    USING row_value;
    RETURN last_write;
END;
$$ LANGUAGE plpgsql;
