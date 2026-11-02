"""Helpful wrapper of SQLAlchemy.session, for interfacing with bitemporal models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal

from psycopg.types.range import TimestamptzRange
from sqlalchemy import Table, Text, sql
from sqlalchemy.dialects.postgresql import ARRAY, Range
from sqlalchemy.exc import MultipleResultsFound, NoResultFound
from sqlalchemy.orm import Query, Session, class_mapper

from pg_bitemporal.sqlalchemy.base import Bitemporal, BitemporalHistory, bitemporal_key


def _qualified_name(table: Table) -> str:
    name = f'"{table.name}"'
    return f'"{table.schema}".{name}' if table.schema is not None else name


def _get_split_record_call(schema: str | None, tablename: str) -> str:
    full_name = f'"{schema}"."{tablename}"' if schema is not None else f'"{tablename}"'

    return (
        f"SELECT split_record(NULL::{full_name}, :row_id, :app_period_1, :app_period_2)"
    )


class TimeRange(TimestamptzRange):
    """Application-time range with containment that knows about open bounds.

    A missing bound is infinity. ``contains(..., non_zero_gaps=True)`` is true
    only when subtracting the other range leaves a non-empty piece on both sides.
    """

    __slots__ = ()

    def contains(
        self, other_range: TimeRange | TimestamptzRange, non_zero_gaps: bool = True
    ) -> bool:
        """Return `True` if this range contains ``other_range``.

        With ``non_zero_gaps``, equal open-ended ranges do not count: there is
        no remainder past the shared infinite bound.
        """
        return self._covers_lower(
            other_range, strict=non_zero_gaps
        ) and self._covers_upper(other_range, strict=non_zero_gaps)

    def starts_before(self, other: TimestamptzRange) -> bool:
        """Return `True` if this range's lower bound is strictly earlier."""
        if self.lower is None and other.lower is None:
            return False
        if self.lower is None:
            return True
        if other.lower is None:
            return False
        return self.lower < other.lower

    def _covers_lower(self, other: TimestamptzRange, *, strict: bool) -> bool:
        if self.lower is None and other.lower is None:
            return not strict
        if self.lower is None:
            return True
        if other.lower is None:
            return False
        if self.lower < other.lower:
            return True
        if self.lower > other.lower:
            return False
        if strict:
            return self.lower_inc and not other.lower_inc
        return not (other.lower_inc and not self.lower_inc)

    def _covers_upper(self, other: TimestamptzRange, *, strict: bool) -> bool:
        if self.upper is None and other.upper is None:
            return not strict
        if self.upper is None:
            return True
        if other.upper is None:
            return False
        if self.upper > other.upper:
            return True
        if self.upper < other.upper:
            return False
        if strict:
            return self.upper_inc and not other.upper_inc
        return not (other.upper_inc and not self.upper_inc)


_BOUNDS: dict[tuple[bool, bool], Literal["()", "[)", "(]", "[]"]] = {
    (False, False): "()",
    (True, False): "[)",
    (False, True): "(]",
    (True, True): "[]",
}


def _range(
    lower: datetime | None,
    upper: datetime | None,
    *,
    lower_inc: bool,
    upper_inc: bool,
) -> Range[datetime]:
    return Range(lower, upper, bounds=_BOUNDS[lower_inc, upper_inc])


# A key maps each key column to its value, whatever the column's type.
Key = Mapping[str, object]


class Writer[ModelT: Bitemporal[object]]:
    """Queries and mutations for a current table and its history table."""

    def __init__(self, model_cls: type[ModelT], session: Session) -> None:
        self.model_cls: type[ModelT] = model_cls
        self.session = session
        # Checked at runtime too, for callers without a type checker.
        table = class_mapper(model_cls).local_table
        if not (issubclass(model_cls, Bitemporal) and isinstance(table, Table)):
            raise ValueError("`model_cls` must be a mapped Bitemporal model")
        self._table = table

    def lock_keys(self, keys: Iterable[Key]) -> None:
        """Lock every key in ``keys`` until the session's transaction ends.

        One statement, however many keys. Postgres takes the locks in its own fixed
        order, so the order of ``keys`` doesn't matter and two transactions locking
        overlapping keys queue rather than deadlock.

        Every write locks its own key inside the trigger. Call this first when a
        transaction writes several keys: otherwise a key that another transaction
        writes in between makes this one fail with a serialization failure
        (SQLSTATE 40001), to be retried.
        """
        keys = list(keys)
        if not keys:
            return
        names = [name for name, _ in bitemporal_key(self.model_cls)]
        for key in keys:
            missing = set(names) - key.keys()
            if missing:
                raise ValueError(f"Missing values for bitemporal key: {missing}")

        # One array per key column, unnested in parallel into rows of the table.
        # Each value is cast through its column's type, so the lock id matches the
        # one the trigger computes from a stored row.
        columns = [f"c{i}" for i in range(len(names))]
        statement = sql.text(
            "SELECT public.lock_bitemporal_keys(ARRAY("
            f"SELECT populate_record(NULL::{_qualified_name(self._table)}, "
            "hstore(CAST(:names AS text[]), "
            f"ARRAY[{', '.join(f'CAST(k.{c} AS text)' for c in columns)}])) "
            f"FROM unnest({', '.join(f':{c}' for c in columns)}) "
            f"AS k({', '.join(columns)})"
            "), CAST(:names AS text[]))"
        ).bindparams(
            sql.bindparam("names", names, type_=ARRAY(Text)),
            *(
                sql.bindparam(
                    column,
                    [key[name] for key in keys],
                    type_=ARRAY(self._table.c[name].type),
                )
                for column, name in zip(columns, names, strict=True)
            ),
        )
        self.session.execute(statement)

    def query_key(self, key: Key) -> Query[ModelT]:
        """Build a query into the current table of a model, given a bitemporal key."""
        return self._query_key(self.model_cls, key)

    def as_of(
        self, key: Key, *, app_time: datetime, txn_time: datetime
    ) -> ModelT | BitemporalHistory:
        """Return the belief for ``key`` that contains both instants.

        ``app_time`` is when the fact was true. ``txn_time`` is when the
        database believed it. Periods are half-open: the lower bound is
        included and the upper bound is not. Exactly one current or history
        row must match. It is a current-model or a history-model instance.
        """
        current, history = self.model_cls, self.model_cls.__history_model__
        found: list[ModelT | BitemporalHistory] = [
            *self._query_key(current, key)
            .filter(current.app_period.contains(app_time))
            .filter(current.txn_period.contains(txn_time)),
            *self._query_key(history, key)
            .filter(history.app_period.contains(app_time))
            .filter(history.txn_period.contains(txn_time)),
        ]
        if not found:
            raise NoResultFound(
                f"no belief for {key} at app {app_time} as of txn {txn_time}"
            )
        if len(found) > 1:
            raise MultipleResultsFound(
                f"{len(found)} beliefs for {key} at app {app_time} as of txn {txn_time}"
            )
        return found[0]

    def _query_key[R](self, model_cls: type[R], key: Key) -> Query[R]:
        query = self.session.query(model_cls)

        # validate bitemporal key is a complete key. The history model shares the
        # current model's key.
        key_pairs = bitemporal_key(self.model_cls)
        key_fields = {key_field for key_field, _ in key_pairs}
        missing_fields = key_fields - key.keys()
        if missing_fields:
            raise ValueError(f"Missing values for bitemporal key: {missing_fields}")

        # iterate through fields, applying filters. raise on inappropriate nulls
        for key_field, _ in key_pairs:
            key_col = getattr(model_cls, key_field)

            # build the query line, and prevent unintuitive null-y problems
            if key[key_field] is None:
                if not key_col.nullable:
                    raise ValueError(f"Column {str(key_col)} may not have a null value")
                key_filter = key_col.is_(None)
            else:
                key_filter = key_col == key[key_field]

            query = query.filter(key_filter)

        return query

    def clear_key_period(self, key: Key, free_period: TimeRange) -> None:
        """Free up values for the given key in application-time.

        For each record that overlaps with the given bitemporal key-space, we either:
            1. Delete the record.
            2. Partition the record into two distinct application periods.
            3. Trim the application period's prefix or suffix.
        """
        # Lock first: the first read is part of the critical region.
        self.lock_keys([key])
        # Load first. Deleting or updating while the query streams skips rows.
        overlaps = (
            self.query_key(key=key)
            .filter(self.model_cls.app_period.overlaps(free_period))
            .all()
        )
        for record in overlaps:
            stored_period = record.app_period
            original_period = TimeRange(
                stored_period.lower,
                stored_period.upper,
                bounds=stored_period.bounds,
            )

            # The free span covers this row, including an identical open end.
            if free_period.contains(original_period, non_zero_gaps=False):
                self.session.delete(record)
                continue

            # Strictly inside: a non-empty prefix and suffix both remain.
            # app_p : [-----)
            # free_p:   [-)
            # result: [-) [-)
            if original_period.contains(free_period):
                # split_record deletes the wide row and inserts the two remnants.
                # The session still holds the deleted instance, so drop it.
                split = self.session.execute(
                    sql.text(
                        _get_split_record_call(
                            schema=self._table.schema,
                            tablename=self._table.name,
                        )
                    ).bindparams(
                        app_period_1=TimestamptzRange(
                            lower=original_period.lower,
                            upper=free_period.lower,
                            bounds=("[" if original_period.lower_inc else "(")
                            + (")" if free_period.lower_inc else "]"),
                        ),
                        app_period_2=TimestamptzRange(
                            lower=free_period.upper,
                            upper=original_period.upper,
                            bounds=("(" if free_period.upper_inc else "[")
                            + ("]" if original_period.upper_inc else ")"),
                        ),
                        # text: split_record casts it to the row_id column's type
                        row_id=str(record.row_id),
                    )
                ).scalar()
                if not split:
                    raise ValueError(f"row_id {record.row_id} was not split")
                self.session.expunge(record)
                continue

            # Partial overlap. Keep the side that starts earlier.
            # app_period : [---)
            # free_period:   [----
            # kept       : [-)
            if original_period.starts_before(free_period):
                record.app_period = _range(
                    original_period.lower,
                    free_period.lower,
                    lower_inc=original_period.lower_inc,
                    upper_inc=not free_period.lower_inc,
                )
            else:
                # app_period :   [---)
                # free_period: [--)
                # kept       :     [-)
                record.app_period = _range(
                    free_period.upper,
                    original_period.upper,
                    lower_inc=not free_period.upper_inc,
                    upper_inc=original_period.upper_inc,
                )

        self.session.flush()

    def clear_and_record(
        self, key: Key, values: Mapping[str, object], app_period: TimeRange
    ) -> ModelT:
        """Clear ``key`` over ``app_period``, then record ``values`` for that period.

        This is how a new fact is written: whatever was believed about ``key``
        during ``app_period`` is retired (trimmed, split, or deleted, as in
        ``clear_key_period``), and one row holding ``values`` takes its place.
        Belief outside ``app_period`` is left alone. Returns the new row, flushed
        but not committed.

        ``values`` names the model's other columns. The key, ``row_id``, and the
        two periods are set here and may not appear in it.
        """
        key_names = {name for name, _ in bitemporal_key(self.model_cls)}
        reserved = key_names | {"row_id", "app_period", "txn_period"}
        clashes = reserved & values.keys()
        if clashes:
            raise ValueError(f"`values` may not set {sorted(clashes)}")
        unknown = values.keys() - self._table.c.keys()
        if unknown:
            raise ValueError(
                f"{self.model_cls.__name__} has no columns {sorted(unknown)}"
            )

        self.clear_key_period(key, app_period)
        row = self.model_cls()
        for name in key_names:
            setattr(row, name, key[name])
        for name, value in values.items():
            setattr(row, name, value)
        row.app_period = _range(
            app_period.lower,
            app_period.upper,
            lower_inc=app_period.lower_inc,
            upper_inc=app_period.upper_inc,
        )
        self.session.add(row)
        self.session.flush()
        return row
