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
