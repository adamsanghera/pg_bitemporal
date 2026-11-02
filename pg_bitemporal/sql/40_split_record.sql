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
