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
--   serialization failure (40001), which the caller retries. Locking every key first
--   (lock_bitemporal_keys) prevents it.
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
                    'transaction writes before writing any of them (Writer.lock_keys).';
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
