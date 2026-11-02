-- Both helpers below keep their state in custom settings set with
-- `set_config(..., is_local => true)`. Postgres resets a local setting when the
-- transaction ends, commit or rollback, so nothing outlives the transaction. An empty
-- string means "unset": `current_setting(name, true)` returns NULL before the first
-- `set_config` in a session, and '' after a reset.

-- force the transaction time for the sql transaction to be a particular timestamptz.
-- Pass NULL to go back to CURRENT_TIMESTAMP.
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

    -- ISO 8601 in UTC reads back exactly whatever DateStyle and TimeZone are.
    PERFORM
        set_config('pg_bitemporal.txn_time',
            coalesce(to_char(fixed_time AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), ''),
            true);
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
-- * When setting the old transaction period to the measured timestamp, it is possible
--   for the period to post-date the measured timestamp. In this case, we barf.
-- * It is sometimes helpful to pause history tracking, if we want to make a migration
--   without impacting history (although this is obviously discouraged).
--   `toggle_record_history` sets the transaction-local `pg_bitemporal.history_disabled`.
CREATE OR REPLACE FUNCTION public.record_txn_history ()
    RETURNS TRIGGER
    AS $$
DECLARE
    measured_time timestamptz;
    fixed_time text;
BEGIN
    -- validate the argument (the transaction history table)
    -- TODO: validate table exists; although we'll raise further down if it doesn't.
    IF (tg_nargs != 1) THEN
        RAISE EXCEPTION 'record_txn_history expects 1 argument'
            USING hint = 'history table name required';
    END IF;

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

    -- measure time from either fixed value (if set) or current_timestamp.
    fixed_time := nullif(current_setting('pg_bitemporal.txn_time', true), '');
    measured_time := coalesce(fixed_time::timestamptz, CURRENT_TIMESTAMP);

    -- insert: very simple! new row beginning at current time.
    -- no need to update `old`, since it does not exist.
    IF (tg_op = 'INSERT') THEN
        NEW.txn_period := tstzrange(measured_time, NULL);
        RETURN new;
    END IF;

    -- not an insert, so we may truncate `old.txn_period`
    IF (measured_time < lower(OLD.txn_period)) THEN
        RAISE EXCEPTION 'transaction time % is before this row''s last write at %',
            measured_time, lower(OLD.txn_period)
            USING hint = 'Transaction time only moves forward. If set_transaction_time '
                'pinned it, pass a time no earlier than the row''s last write.';
        elsif (measured_time = lower(OLD.txn_period)) THEN
            -- We don't need to persist data that's replaced in the *same DB transaction*,
            -- so we allow it to either be deleted or replaced with the latest value.
        ELSE
            -- most commoon scenario: clear space for new value's transaction period
            OLD.txn_period := tstzrange(lower(OLD.txn_period), measured_time);
            EXECUTE format('insert into %s select $1.*', TG_ARGV[0])
            USING old;
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

The function deletes the existing record, and creates two new records with updated ids.
*/
CREATE OR REPLACE FUNCTION public.split_record(
    tmp_record ANYELEMENT,
    row_id BIGINT,
    app_period_1 TSTZRANGE,
    app_period_2 TSTZRANGE
) RETURNS BOOL AS $$
DECLARE
    found_rows INT;
BEGIN
    -- fetch the existing row
    EXECUTE format(
        'SELECT * FROM %s WHERE row_id = $1', pg_typeof(tmp_record)
    ) INTO tmp_record USING row_id;

    -- validate that the row was loaded (it could have been deleted / never existed!)
    GET DIAGNOSTICS found_rows = ROW_COUNT;
    IF found_rows != 1 THEN
        RETURN FALSE;
    END IF;

    -- delete the existing row (i.e. expire it to the history table)
    EXECUTE format(
        'DELETE FROM %s WHERE row_id = $1', pg_typeof(tmp_record)
    ) USING row_id;

    -- insert the first row, replacing `row_id` and `app_period` as appropriate
    SELECT (
        tmp_record #= hstore(
            ARRAY['row_id', 'app_period'],
            ARRAY[
                nextval(
                    pg_get_serial_sequence(
                        pg_typeof(tmp_record)::text, 'row_id'
                    )
                )::text,
                app_period_1::text
            ]
        )
    ).* INTO tmp_record;
    EXECUTE format(
        'INSERT INTO %s SELECT ($1).*', pg_typeof(tmp_record)
    ) USING tmp_record;

    -- insert the second row, replacing `row_id` and `app_period` as appropriate
    SELECT (
        tmp_record #= hstore(
            ARRAY['row_id', 'app_period'],
            ARRAY[
                nextval(
                    pg_get_serial_sequence(
                        pg_typeof(tmp_record)::text, 'row_id'
                    )
                )::text,
                app_period_2::text
            ]
        )
    ).* INTO tmp_record;
    EXECUTE format(
        'INSERT INTO %s SELECT ($1).*', pg_typeof(tmp_record)
    ) USING tmp_record;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;
