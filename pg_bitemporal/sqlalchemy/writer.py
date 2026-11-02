"""Helpful wrapper of SQLAlchemy.session, for interfacing with bitemporal models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.range import TimestamptzRange
from sqlalchemy import orm, sql
from sqlalchemy.exc import MultipleResultsFound, NoResultFound

from pg_bitemporal.sqlalchemy.base import Bitemporal, bitemporal_key


def _get_split_record_call(schema: str, tablename: str):
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


class Writer:
    """Queries and mutations for a current table and its history table."""

    def __init__(self, model_cls, session):
        if not (isinstance(model_cls, type) and issubclass(model_cls, Bitemporal)):
            raise ValueError(
                f"`model_cls` must be a mapped {Bitemporal.__name__} model"
            )

        self.model_cls = model_cls
        self.session = session

    def query_key(self, key: dict[str, Any]) -> orm.query.Query:
        """Build a query into the current table of a model, given a bitemporal key."""
        return self._query_key(self.model_cls, key)

    def as_of(self, key: dict[str, Any], *, app_time: datetime, txn_time: datetime):
        """Return the belief for ``key`` that contains both instants.

        ``app_time`` is when the fact was true. ``txn_time`` is when the
        database believed it. Periods are half-open: the lower bound is
        included and the upper bound is not. Exactly one current or history
        row must match.
        """
        found = []
        for model_cls in (self.model_cls, self.model_cls.__history__):
            found.extend(
                self._query_key(model_cls, key)
                .filter(model_cls.app_period.contains(app_time))
                .filter(model_cls.txn_period.contains(txn_time))
                .all()
            )
        if not found:
            raise NoResultFound(
                f"no belief for {key} at app {app_time} as of txn {txn_time}"
            )
        if len(found) > 1:
            raise MultipleResultsFound(
                f"{len(found)} beliefs for {key} at app {app_time} as of txn {txn_time}"
            )
        return found[0]

    def _query_key(self, model_cls, key: dict[str, Any]) -> orm.query.Query:
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

    def clear_key_period(self, key: dict[str, Any], free_period: TimeRange) -> None:
        """Free up values for the given key in application-time.

        For each record that overlaps with the given bitemporal key-space, we either:
            1. Delete the record.
            2. Partition the record into two distinct application periods.
            3. Trim the application period's prefix or suffix.
        """
        # Load first. Deleting or updating while the query streams skips rows.
        overlaps = (
            self.query_key(key=key)
            .filter(self.model_cls.app_period.overlaps(free_period))
            .all()
        )
        for record in overlaps:
            stored_period: TimestamptzRange = record.app_period
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
                            schema=self.model_cls.__table__.schema,
                            tablename=self.model_cls.__tablename__,
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
                        row_id=record.row_id,
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
                record.app_period = TimestamptzRange(
                    lower=original_period.lower,
                    upper=free_period.lower,
                    bounds=("[" if original_period.lower_inc else "(")
                    + (")" if free_period.lower_inc else "]"),
                )
            else:
                # app_period :   [---)
                # free_period: [--)
                # kept       :     [-)
                record.app_period = TimestamptzRange(
                    lower=free_period.upper,
                    upper=original_period.upper,
                    bounds=("(" if free_period.upper_inc else "[")
                    + ("]" if original_period.upper_inc else ")"),
                )

        self.session.flush()
