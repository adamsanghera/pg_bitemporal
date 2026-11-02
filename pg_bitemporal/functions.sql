-- Exclusion constraints on the key need btree_gist, and record_txn_history and
-- split_record use hstore. Creating an extension needs CREATE on the database.
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS hstore;

-- Both helpers below keep their state in custom settings set with
-- `set_config(..., is_local => true)`. Postgres resets a local setting when the
-- transaction ends, commit or rollback, so nothing outlives the transaction. An empty
-- string means "unset": `current_setting(name, true)` returns NULL before the first
-- `set_config` in a session, and '' after a reset.

-- force the transaction time for the sql transaction to be a particular timestamptz.
-- Pass NULL to unpin it: the next write then takes the clock, as described at
-- `bitemporal_transaction_time`.
CREATE OR REPLACE FUNCTION public.set_transaction_time (fixed_time timestamptz)
    RETURNS bool
    AS $$
BEGIN
    -- this operation is meaningless if history-tracking is disabled
    IF (coalesce(current_setting('pg_bitemporal.history_disabled', true), '') <> '') THEN
        RAISE EXCEPTION 'set_transaction_time has no effect while history tracking is off'
            USING hint = 'Call toggle_record_history(true) first.';
    END IF;

    IF (NOT isfinite(fixed_time)) THEN
        RAISE EXCEPTION 'set_transaction_time needs a finite time, got %', fixed_time;
    END IF;

    PERFORM public._pin_transaction_time(fixed_time, 'user');
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- This is an "application layer" enable/disable flag. While postgres functions are
-- "global" and shared by more than one transaction, this function allows us to toggle
-- the function on and off within a single transaction. It lives and dies inside the
-- transaction, and holds no global state outside the transaction.
CREATE OR REPLACE FUNCTION public.toggle_record_history (is_enabled bool)
    RETURNS bool
    AS $$
BEGIN
    PERFORM
        set_config('pg_bitemporal.history_disabled',
            CASE WHEN is_enabled THEN '' ELSE 'on' END, true);
    RETURN TRUE;
END;
$$
LANGUAGE plpgsql;


-- Pin (or, with NULL, unpin) this transaction's time. `pinned_by` records who did it:
-- 'user' for set_transaction_time, 'lock' for the first write after taking its locks.
-- ISO 8601 in UTC reads back exactly whatever DateStyle and TimeZone are.
CREATE OR REPLACE FUNCTION public._pin_transaction_time (fixed_time timestamptz, pinned_by text)
    RETURNS void
    AS $$
BEGIN
    PERFORM
        set_config('pg_bitemporal.txn_time',
            coalesce(to_char(fixed_time AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), ''),
            true);
    PERFORM
        set_config('pg_bitemporal.txn_time_pinned_by',
            CASE WHEN fixed_time IS NULL THEN '' ELSE pinned_by END, true);
END;
$$ LANGUAGE plpgsql;

-- This transaction's time: the pinned time if there is one. Otherwise the clock,
-- pinned so that every later write in the transaction shares it.
--
-- The trigger calls this only after locking the keys it writes. So a transaction that
-- waited for another to release a key reads the clock after that one committed, and its
-- time sorts after the other's writes. CURRENT_TIMESTAMP, the transaction's start, can
-- be earlier than a write that committed while this transaction was waiting.
CREATE OR REPLACE FUNCTION public.bitemporal_transaction_time ()
    RETURNS timestamptz
    AS $$
DECLARE
    pinned text := nullif(current_setting('pg_bitemporal.txn_time', true), '');
    now_ timestamptz;
BEGIN
    IF (pinned IS NOT NULL) THEN
        RETURN pinned::timestamptz;
    END IF;
    now_ := clock_timestamp();
    PERFORM public._pin_transaction_time(now_, 'lock');
    RETURN now_;
END;
$$ LANGUAGE plpgsql;

-- Lock one bitemporal key until the transaction ends. `row_value` is a row of the
-- current table (only its key columns are read), so the trigger can pass OLD or NEW and
-- Writer.lock_key can pass a row built from the key's values. The lock id hashes the
-- table and the typed key values, so it does not depend on DateStyle or TimeZone. Two
-- keys that happen to share a hash only make each other wait.
CREATE OR REPLACE FUNCTION public.lock_bitemporal_key (row_value anyelement, key_columns text[])
    RETURNS void
    AS $$
DECLARE
    lock_id bigint;
BEGIN
    EXECUTE format('SELECT hash_record_extended(ROW(%s::oid, %s), 0)',
        pg_typeof(row_value)::text::regclass::oid,
        (SELECT string_agg(format('($1).%I', col), ', ') FROM unnest(key_columns) AS col))
    INTO lock_id
    USING row_value;
    PERFORM pg_advisory_xact_lock(lock_id);
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

-- `record_txn_history` turns `update` and `delete` into non-destructive mutations.
--
-- How `update` looks, executed at transaction time `X`:
-- Before:                      After:
-- Current txn_time:     [--->  Current txn_time:         [--->
-- History txn_time: [---)      History txn_time: [---[---)
--                     1   2   X                      1   2   X
--
-- How `delete` looks, executed at transaction time `X`:
-- Before:                      After:
-- Current txn_time:     [--->  Current txn_time:
-- History txn_time: [---)      History txn_time: [---[---)
--                     1   2   X                      1   2   X
--
-- This operation is simple in theory, there are some complications in practice.
--
-- * In a single transaction, "time" is frozen. So there may be competing updates that
--   occur at the same "time". In these situations, we preserve the most recent data
--   and do not write the intermediary information to the history table. This helps us
--   avoid zero-sized ranges, but is a limitation worth knowing for app clients! 
-- * Sometimes, we want to "freeze" the transaction time ourselves. Either for testing
--   reasons or for application reasons. `set_transaction_time` stores it in the
--   transaction-local setting `pg_bitemporal.txn_time`.
-- * Arguments after the history table name are the key's columns. With them, the
--   trigger locks each key it writes (`lock_bitemporal_key`) before reading the
--   transaction time, and checks the time against the key's latest write in either
--   table. A trigger created with only the history table (before keys were passed)
--   takes no lock and checks against OLD alone.
-- * A transaction time earlier than the key's latest write is refused. If
--   set_transaction_time pinned it, that's backdating. If the clock pinned it, another
--   transaction wrote the key after this one's time was fixed by an earlier write: a
--   serialization failure (40001), which the caller retries. Locking every key first,
--   in sorted order, prevents it.
-- * It is sometimes helpful to pause history tracking, if we want to make a migration
--   without impacting history (although this is obviously discouraged).
--   `toggle_record_history` sets the transaction-local `pg_bitemporal.history_disabled`.
CREATE OR REPLACE FUNCTION public.record_txn_history ()
    RETURNS TRIGGER
    AS $$
DECLARE
    measured_time timestamptz;
    old_values hstore;
    history_columns text[];
    unmatched text[];
    key_columns text[];
    last_write timestamptz;
BEGIN
    -- arguments: the history table, then the key's columns
    IF (tg_nargs < 1) THEN
        RAISE EXCEPTION 'record_txn_history expects the history table name'
            USING hint = 'Pass the history table, then the key columns.';
    END IF;
    key_columns := TG_ARGV[1:tg_nargs - 1];

    -- early-exit if trigger is disabled
    IF (coalesce(current_setting('pg_bitemporal.history_disabled', true), '') <> '') THEN
        IF (tg_op = 'INSERT') THEN
            RETURN new;
        elsif (tg_op = 'UPDATE') THEN
            RETURN new;
        elsif (tg_op = 'DELETE') THEN
            RETURN old;
        ELSE
            RAISE EXCEPTION 'record_txn_history only works on insert, update, or delete';
        END IF;
    END IF;

    -- Lock the keys first, then read the time, so a wait for another writer comes
    -- before this transaction's time is fixed. An UPDATE may move a row between keys.
    IF (cardinality(key_columns) > 0) THEN
        IF (tg_op IN ('UPDATE', 'DELETE')) THEN
            PERFORM public.lock_bitemporal_key(OLD, key_columns);
        END IF;
        IF (tg_op IN ('INSERT', 'UPDATE')) THEN
            PERFORM public.lock_bitemporal_key(NEW, key_columns);
        END IF;
    END IF;

    measured_time := public.bitemporal_transaction_time();

    -- Refuse a time earlier than the key's latest write. Equal is this transaction.
    IF (cardinality(key_columns) > 0) THEN
        last_write := greatest(
            CASE WHEN tg_op IN ('UPDATE', 'DELETE') THEN
                public._bitemporal_key_last_write(OLD, TG_ARGV[0]::regclass, key_columns)
            END,
            CASE WHEN tg_op IN ('INSERT', 'UPDATE') THEN
                public._bitemporal_key_last_write(NEW, TG_ARGV[0]::regclass, key_columns)
            END);
    ELSIF (tg_op IN ('UPDATE', 'DELETE')) THEN
        last_write := lower(OLD.txn_period);
    END IF;

    IF (measured_time < last_write) THEN
        IF (current_setting('pg_bitemporal.txn_time_pinned_by', true) = 'user') THEN
            RAISE EXCEPTION 'transaction time % is before this key''s last write at %',
                measured_time, last_write
                USING hint = 'Transaction time only moves forward. Pass '
                    'set_transaction_time a time no earlier than the key''s last write.';
        END IF;
        RAISE EXCEPTION 'another transaction wrote this key at %, after this '
            'transaction''s time %', last_write, measured_time
            USING errcode = 'serialization_failure',
                hint = 'Retry the transaction. To avoid this, lock every key the '
                    'transaction writes, in sorted order, before writing any of them.';
    END IF;

    -- insert: very simple! new row beginning at current time.
    -- no need to update `old`, since it does not exist.
    IF (tg_op = 'INSERT') THEN
        NEW.txn_period := tstzrange(measured_time, NULL);
        RETURN new;
    END IF;

    -- not an insert, so we may truncate `old.txn_period`
    IF (measured_time = lower(OLD.txn_period)) THEN
            -- We don't need to persist data that's replaced in the *same DB transaction*,
            -- so we allow it to either be deleted or replaced with the latest value.
        ELSE
            -- most commoon scenario: clear space for new value's transaction period
            OLD.txn_period := tstzrange(lower(OLD.txn_period), measured_time);
            -- Copy by column name, not position: the history table's columns may be
            -- in another order. hstore round-trips each value through its type's
            -- text form. A current column with no history column would be dropped,
            -- so refuse it; a missing NOT NULL value fails on insert by itself.
            old_values := hstore(OLD);
            SELECT
                array_agg(attname::text) INTO history_columns
            FROM
                pg_attribute
            WHERE
                attrelid = TG_ARGV[0]::regclass
                AND attnum > 0
                AND NOT attisdropped;
            unmatched := akeys(old_values - history_columns);
            IF (cardinality(unmatched) > 0) THEN
                RAISE EXCEPTION '% has no column for %', TG_ARGV[0], unmatched
                    USING hint = 'Add the columns to the history table, or drop '
                        'them from the current table.';
            END IF;
            EXECUTE format(
                'INSERT INTO %1$s SELECT (populate_record(NULL::%1$s, $1)).*',
                TG_ARGV[0])
            USING old_values;
    END IF;

    -- update: basically an insert, since we've cleared `old.txn_period`
    IF (tg_op = 'UPDATE') THEN
        NEW.txn_period := tstzrange(measured_time, NULL);
        RETURN new;
    END IF;
    -- delete: no-op, since `old.txn_period` was closed earlier
    IF (tg_op = 'DELETE') THEN
        RETURN old;
    END IF;
    RAISE EXCEPTION 'record_txn_history only works on insert, update, or delete';
END;
$$ LANGUAGE plpgsql;

/* Split an existing row with some `app_period` into two non-overlapping chunks.

If the app period arguments look like:
    app_period_1: [a, b)
    app_period_2: [c, d)

It's assumed that the existing row's valid period *begins at* `a` and *ends at* `d`.
and that `a < b < c < d`. For example:

    a    b  c    d
    [-+1-)  [-+2-)
    [---`row_id`--)

The function deletes the existing record, and inserts two new records. `row_id` is
passed as text and cast to the column's own type, so it can be a bigint, a uuid,
or anything else. The new rows leave `row_id` out of the INSERT, so they take the
column's database default: a sequence, an identity, `gen_random_uuid()`, and so on.
*/

-- Earlier versions took `row_id BIGINT`. Drop that overload so upgrades can't call it.
DROP FUNCTION IF EXISTS public.split_record(anyelement, bigint, tstzrange, tstzrange);

CREATE OR REPLACE FUNCTION public.split_record(
    tmp_record ANYELEMENT,
    row_id TEXT,
    app_period_1 TSTZRANGE,
    app_period_2 TSTZRANGE
) RETURNS BOOL AS $$
DECLARE
    tbl regclass := pg_typeof(tmp_record)::text::regclass;
    row_id_type text;
    columns text;
    found_rows INT;
    old_values hstore;
    period TSTZRANGE;
BEGIN
    SELECT
        format_type(atttypid, atttypmod) INTO row_id_type
    FROM
        pg_attribute
    WHERE
        attrelid = tbl
        AND attname = 'row_id'
        AND NOT attisdropped;

    -- every column except row_id, which takes its default on insert
    SELECT
        string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO columns
    FROM
        pg_attribute
    WHERE
        attrelid = tbl
        AND attnum > 0
        AND NOT attisdropped
        AND attname <> 'row_id';

    -- fetch the existing row
    EXECUTE format(
        'SELECT * FROM %s WHERE row_id = CAST($1 AS %s)', tbl, row_id_type
    ) INTO tmp_record USING row_id;

    -- validate that the row was loaded (it could have been deleted / never existed!)
    GET DIAGNOSTICS found_rows = ROW_COUNT;
    IF found_rows != 1 THEN
        RETURN FALSE;
    END IF;

    -- delete the existing row (i.e. expire it to the history table)
    EXECUTE format(
        'DELETE FROM %s WHERE row_id = CAST($1 AS %s)', tbl, row_id_type
    ) USING row_id;

    -- insert the two remnants, each with its own app_period and a fresh row_id
    old_values := hstore(tmp_record);
    FOREACH period IN ARRAY ARRAY[app_period_1, app_period_2] LOOP
        EXECUTE format(
            'INSERT INTO %1$s (%2$s) SELECT %2$s FROM populate_record(NULL::%1$s, $1)',
            tbl, columns
        ) USING old_values || hstore('app_period', period::text);
    END LOOP;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;
