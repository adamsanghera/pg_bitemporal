"""The ``Bitemporal`` mixin: declare a model bitemporal on your own declarative base.

>>> class Base(DeclarativeBase):
...     pass
...
>>> class Address(Bitemporal, Base):
...     __tablename__ = "address"
...     __bitemporal_key__ = ("user_id",)
...     user_id: Mapped[int]
...     street: Mapped[str]
...
>>> Address.__history_model__  # AddressHistory, mapped to "address_history"
>>> Base.metadata.create_all(engine)   # both tables, and the versioning trigger

The mixin adds ``row_id``, ``app_period`` and ``txn_period``, the key's exclusion
constraint and indexes, and a history model built from the finished table. The
history model is read-only through the ORM: rows reach it only via the trigger.

``row_id`` is a ``BIGSERIAL`` by default. Redeclare it to use another type, as long
as the database fills it in, since ``split_record`` inserts rows without one:

>>> class Order(Bitemporal[uuid.UUID], Base):
...     __tablename__ = "order"
...     __bitemporal_key__ = ("order_id",)
...     row_id: Mapped[uuid.UUID] = mapped_column(
...         primary_key=True, server_default=func.gen_random_uuid()
...     )
...     order_id: Mapped[str]
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import ClassVar, Generic, TypeVar

from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Table,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, class_mapper, mapped_column
from sqlalchemy.schema import DDL

__all__ = ["Bitemporal", "BitemporalHistory", "bitemporal_key"]


def _period_column() -> Mapped[Range[datetime]]:
    return mapped_column(
        TSTZRANGE, nullable=False, server_default=func.tstzrange(func.now(), None)
    )


def bitemporal_key(model_cls: type) -> list[tuple[str, str]]:
    """Return ``model_cls``'s key as ``(column, exclusion operator)`` pairs.

    ``__bitemporal_key__`` is a tuple of column names, compared with ``=``, or a
    dict from column name to the operator to exclude on (``{"tags": "&&"}``).
    """
    key = getattr(model_cls, "__bitemporal_key__", None)
    if isinstance(key, dict):
        pairs = list(key.items())
    elif isinstance(key, (tuple, list)):
        pairs = [(name, "=") for name in key]
    else:
        raise TypeError(
            f"{model_cls.__name__} must set __bitemporal_key__ to a tuple of column "
            "names, or a dict from column name to exclusion operator"
        )
    if not pairs:
        raise TypeError(f"{model_cls.__name__}.__bitemporal_key__ is empty")
    return pairs


# The row_id type. A model that redeclares row_id as, say, a uuid names it here
# (`Bitemporal[uuid.UUID]`), so the override type-checks. Plain `Bitemporal` is int.
# Covariant, so code that takes any model can ask for `Bitemporal[object]`.
RowIdT = TypeVar("RowIdT", default=int, covariant=True)


class BitemporalHistory:
    """Base of every history model: retired versions, read-only.

    The periods are typed here. The other columns are mapped from the current table
    with the same names, but a generated class can't tell a type checker about them.
    To get typed access, declare the history model yourself, with annotations only,
    and point the current model at it. Each annotation must name a column:

    >>> class AddressHistory(BitemporalHistory):
    ...     user_id: Mapped[int]
    ...     street: Mapped[str]
    ...
    >>> class Address(Bitemporal, Base):
    ...     __history_model__ = AddressHistory
    ...     ...
    """

    __tablename__: ClassVar[str]
    __table__: ClassVar[Table]

    row_id: Mapped[object]
    app_period: Mapped[Range[datetime]]
    txn_period: Mapped[Range[datetime]]

    def __init__(self, **values: object) -> None:
        for name, value in values.items():
            setattr(self, name, value)


class Bitemporal(Generic[RowIdT]):
    """Mixin for a bitemporal model. Put it before your declarative base."""

    __bitemporal_key__: ClassVar[tuple[str, ...] | dict[str, str]]
    __history_model__: ClassVar[type[BitemporalHistory]]

    # Primary key. The bitemporal key repeats across app periods, so it can't be one.
    # Redeclare it for another type; the database must fill it in (see module doc).
    row_id: Mapped[RowIdT] = mapped_column(BigInteger, primary_key=True)

    # When the fact was true.
    app_period: Mapped[Range[datetime]] = _period_column()

    # When the database believed it. The trigger sets it on every write.
    txn_period: Mapped[Range[datetime]] = _period_column()

    def __init_subclass__(cls, **kwargs: object) -> None:
        # The declarative base maps the class inside this super() call, so the
        # table exists afterwards.
        super().__init_subclass__(**kwargs)
        table = cls.__dict__.get("__table__")
        if not isinstance(table, Table):
            return  # abstract, or single-table inheritance off a bitemporal parent
        _check_row_id(cls, table)
        key = bitemporal_key(cls)
        _add_current_constraints(table, key)
        history = _build_history_table(table, key)
        declared = cls.__dict__.get("__history_model__")
        cls.__history_model__ = _map_history_model(cls, history, declared)


def _check_row_id(cls: type, table: Table) -> None:
    if [col.name for col in table.primary_key.columns] != ["row_id"]:
        raise TypeError(f"{cls.__name__}: row_id must be the only primary key column")
    row_id = table.c.row_id
    filled_by_database = (
        table.autoincrement_column is row_id
        or row_id.server_default is not None
        or row_id.identity is not None
    )
    if not filled_by_database:
        raise TypeError(
            f"{cls.__name__}.row_id needs a database default (an integer sequence, "
            "an Identity, or a server_default such as gen_random_uuid()): "
            "split_record inserts rows without one"
        )


def _key_indexes(table: Table, key: list[tuple[str, str]]) -> None:
    for name, _ in key:
        Index(f"{table.name}_{name}_idx", table.c[name])
    for period in ("app_period", "txn_period"):
        Index(f"{table.name}_{period}_idx", table.c[period], postgresql_using="gist")


def _add_current_constraints(table: Table, key: list[tuple[str, str]]) -> None:
    names = [name for name, _ in key]
    # (key, app_period) is unique, so other tables can reference a version.
    table.append_constraint(
        UniqueConstraint(
            *names, "app_period", name=f"{table.name}_bitemporal_unique_key"
        )
    )
    # One version per key at any instant of application time.
    table.append_constraint(
        ExcludeConstraint(
            *key, ("app_period", "&&"), name=f"{table.name}_bitemporal_key_exclusion"
        )
    )
    _key_indexes(table, key)


def _build_history_table(current: Table, key: list[tuple[str, str]]) -> Table:
    """The history table for ``current``, on the same metadata, with its trigger."""
    # Same columns by name and type. No defaults, foreign keys, or unique flags:
    # history rows are copies, and what they referenced may since have gone.
    # (row_id, txn_period) is the key, since history repeats a row_id per version.
    history = Table(
        f"{current.name}_history",
        current.metadata,
        *(
            Column(
                col.name,
                col.type,
                nullable=col.nullable,
                primary_key=col.name in ("row_id", "txn_period"),
                autoincrement=False,
            )
            for col in current.columns
        ),
        # A correction retires the previous belief, so history rows for one key
        # share app_period and differ in txn_period. Exclude a row only when both
        # periods overlap: adjacent transaction ranges don't, which is what lets
        # the next correction land.
        ExcludeConstraint(
            *key,
            ("app_period", "&&"),
            ("txn_period", "&&"),
            name=f"{current.name}_history_bitemporal_key_exclusion",
        ),
        schema=current.schema,
    )
    _key_indexes(history, key)
    history.add_is_dependent_on(current)
    event.listen(
        history,
        "after_create",
        DDL(
            "CREATE TRIGGER versioning_trigger "
            f"BEFORE INSERT OR UPDATE OR DELETE ON {_qualified(current)} "
            "FOR EACH ROW EXECUTE PROCEDURE "
            f"public.record_txn_history({_trigger_args(history, key)})"
        ),
    )
    return history


def _map_history_model(
    current_cls: type, history: Table, declared: object
) -> type[BitemporalHistory]:
    """Map the history model onto ``history``, refusing ORM writes.

    ``declared`` is the class the model named in ``__history_model__``, if any.
    Otherwise a ``<Name>History`` class is generated.
    """
    if declared is None:
        history_cls = type(
            f"{current_cls.__name__}History",
            (BitemporalHistory,),
            {
                "__module__": current_cls.__module__,
                "__doc__": f"Retired versions of {current_cls.__name__}. Read-only.",
            },
        )
    elif isinstance(declared, type) and issubclass(declared, BitemporalHistory):
        history_cls = declared
        unknown = set(inspect.get_annotations(declared)) - set(history.c.keys())
        if unknown:
            raise TypeError(
                f"{declared.__name__} annotates {sorted(unknown)}, which "
                f"{current_cls.__name__} has no columns for"
            )
    else:
        raise TypeError(
            f"{current_cls.__name__}.__history_model__ must be a "
            "BitemporalHistory subclass"
        )
    history_cls.__tablename__ = history.name
    # Same registry as the current model, so relationships and lookups work alike.
    class_mapper(current_cls).registry.map_imperatively(history_cls, history)

    def refuse(verb: str):
        def listener(mapper, connection, target) -> None:
            raise ValueError(f"May not {verb} {history.fullname}")

        return listener

    event.listen(history_cls, "before_insert", refuse("insert into"))
    event.listen(history_cls, "before_update", refuse("update"))
    event.listen(history_cls, "before_delete", refuse("delete from"))
    return history_cls


def _trigger_args(history: Table, key: list[tuple[str, str]]) -> str:
    """The history table, then the key columns, as SQL string literals."""
    values = [_qualified(history), *(name for name, _ in key)]
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _qualified(table: Table) -> str:
    name = f'"{table.name}"'
    return f'"{table.schema}".{name}' if table.schema else name
