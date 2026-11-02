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
>>> Address.__history__        # AddressHistory, mapped to "address_history"
>>> Base.metadata.create_all(engine)   # both tables, and the versioning trigger

The mixin adds ``row_id``, ``app_period`` and ``txn_period``, the key's exclusion
constraint and indexes, and a history model built from the finished table. The
history model is read-only through the ORM: rows reach it only via the trigger.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Table,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint
from sqlalchemy.orm import Mapped, class_mapper, mapped_column
from sqlalchemy.schema import DDL

__all__ = ["Bitemporal", "bitemporal_key"]


def _period_column() -> Mapped[Any]:
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


class Bitemporal:
    """Mixin for a bitemporal model. Put it before your declarative base."""

    __bitemporal_key__: ClassVar[tuple[str, ...] | dict[str, str]]
    __history__: ClassVar[type[Any]]

    # Primary key. The bitemporal key repeats across app periods, so it can't be one.
    row_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # When the fact was true.
    app_period: Mapped[Any] = _period_column()

    # When the database believed it. The trigger sets it on every write.
    txn_period: Mapped[Any] = _period_column()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # The declarative base maps the class inside this super() call, so the
        # table exists afterwards.
        super().__init_subclass__(**kwargs)
        table = cls.__dict__.get("__table__")
        if not isinstance(table, Table):
            return  # abstract, or single-table inheritance off a bitemporal parent
        key = bitemporal_key(cls)
        _add_current_constraints(table, key)
        cls.__history__ = _build_history(cls, table, key)


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


def _build_history(
    current_cls: type, current: Table, key: list[tuple[str, str]]
) -> type[Any]:
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
            f"public.record_txn_history('{_qualified(history)}')"
        ),
    )

    history_cls = type(
        f"{current_cls.__name__}History",
        (),
        {
            "__module__": current_cls.__module__,
            "__doc__": f"Retired versions of {current_cls.__name__}. Read-only.",
            "__tablename__": history.name,
            "__init__": _assign_attributes,
        },
    )
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


def _assign_attributes(self: Any, **values: Any) -> None:
    for name, value in values.items():
        setattr(self, name, value)


def _qualified(table: Table) -> str:
    name = f'"{table.name}"'
    return f'"{table.schema}".{name}' if table.schema else name
