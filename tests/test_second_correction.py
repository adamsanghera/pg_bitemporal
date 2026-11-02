"""Corrections stay readable at the transaction time they were believed.

The fixture expects PostgreSQL. Set ``PG_BITEMPORAL_TEST_URL`` to the target
database. When unset, the test uses ``postgresql+psycopg:///pg_bitemporal_test`` and
creates that database if the server is up.
"""

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.types.range import TimestamptzRange
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    bindparam,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import NoResultFound, OperationalError
from sqlalchemy.orm import sessionmaker

from pg_bitemporal.sqlalchemy.base import (
    CurrentBase,
    HistoryBase,
    history_class_of,
    to_history_table,
)
from pg_bitemporal.sqlalchemy.writer import TimeRange, Writer

JAN_1 = datetime(2022, 1, 1, tzinfo=UTC)
JAN_15 = datetime(2022, 1, 15, tzinfo=UTC)
MAY_1 = datetime(2022, 5, 1, tzinfo=UTC)
JUN_1 = datetime(2022, 6, 1, tzinfo=UTC)
JUN_5 = datetime(2022, 6, 5, tzinfo=UTC)
JUN_13 = datetime(2022, 6, 13, tzinfo=UTC)
JUN_14 = datetime(2022, 6, 14, tzinfo=UTC)
JUN_19 = datetime(2022, 6, 19, tzinfo=UTC)
JUN_20 = datetime(2022, 6, 20, tzinfo=UTC)
JUN_21 = datetime(2022, 6, 21, tzinfo=UTC)
JUL_1 = datetime(2022, 7, 1, tzinfo=UTC)
JUL_2 = datetime(2022, 7, 2, tzinfo=UTC)
BEFORE_APP = datetime(2021, 12, 31, tzinfo=UTC)
BEFORE_JUN_20 = JUN_20 - timedelta(microseconds=1)
BEFORE_JUL_1 = JUL_1 - timedelta(microseconds=1)

APP_PERIOD = TimestamptzRange(JAN_1, None)

_TIMESTAMPTZ = DateTime(timezone=True)


class _AddressMixin:
    key_fields_and_equality_operators = [("user_id", "=")]
    user_id = Column(Integer, nullable=False)
    street = Column(String, nullable=False)


class Address(_AddressMixin, CurrentBase):
    pass


AddressHistory = to_history_table(Address, [_AddressMixin])


def _ident(name: str) -> str:
    if not re.fullmatch(r"[a-z_]+", name):
        raise ValueError(f"unexpected table name {name!r}")
    return name


_CURRENT = _ident(Address.__tablename__)
_HISTORY = _ident(AddressHistory.__tablename__)


def _test_url() -> str:
    return os.environ.get(
        "PG_BITEMPORAL_TEST_URL", "postgresql+psycopg:///pg_bitemporal_test"
    )


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    dbname = parsed.database
    if dbname is None or not dbname.isidentifier():
        raise RuntimeError(
            "PG_BITEMPORAL_TEST_URL must name a simple database to create"
        )

    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": dbname},
            ).scalar()
            if exists:
                return
            conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        admin.dispose()


def _rebuild(engine) -> None:
    functions = Path(__file__).resolve().parents[1] / "pg_bitemporal" / "functions.sql"
    sql = functions.read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cursor.execute("CREATE SCHEMA public")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS hstore")
        cursor.execute(sql)
        raw.commit()
    finally:
        raw.close()

    CurrentBase.metadata.create_all(engine)
    HistoryBase.metadata.create_all(engine)


@pytest.fixture(scope="module")
def engine():
    """Create the bitemporal tables against the test database."""
    url = _test_url()
    try:
        _ensure_database(url)
    except (OperationalError, PsycopgOperationalError) as exc:
        pytest.skip(f"PostgreSQL is not reachable at {url}: {exc}")

    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _utc(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.close()

    try:
        _rebuild(engine)
    except (OperationalError, PsycopgOperationalError) as exc:
        engine.dispose()
        pytest.skip(f"PostgreSQL is not reachable at {url}: {exc}")

    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    """Yield a session bound to the rebuilt test database."""
    bound = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield bound
    finally:
        bound.rollback()
        bound.close()


def _write_at(session, when, mutate) -> None:
    session.execute(
        text("SELECT public.set_transaction_time(:when)").bindparams(
            bindparam("when", type_=_TIMESTAMPTZ)
        ),
        {"when": when},
    ).scalar()
    mutate()
    session.commit()


def _set_street(session, user_id: int, street: str) -> None:
    row = session.query(Address).filter(Address.user_id == user_id).one()
    row.street = street


@pytest.fixture(scope="module")
def user_id(engine) -> int:
    """Record Apple, then Banana, then Cherry for one user."""
    recorded = sessionmaker(bind=engine, autoflush=False)()
    uid = 42
    try:
        _write_at(
            recorded,
            JAN_1,
            lambda: recorded.add(
                Address(user_id=uid, street="123 Apple St", app_period=APP_PERIOD)
            ),
        )
        _write_at(recorded, JUN_20, lambda: _set_street(recorded, uid, "456 Banana St"))
        _write_at(recorded, JUL_1, lambda: _set_street(recorded, uid, "789 Cherry St"))
    finally:
        recorded.close()
    return uid


def _street(session, user_id: int, *, txn_time, app_time) -> str:
    belief = Writer(Address, session).as_of(
        {"user_id": user_id}, app_time=app_time, txn_time=txn_time
    )
    return belief.street


def test_corrections_record_closed_transaction_periods(session, user_id):
    """Two in-place corrections retire each previous belief into history."""
    timeline = session.execute(
        text(
            f"""
            SELECT street, lower(txn_period) AS txn_start, upper(txn_period) AS txn_end
            FROM (
                SELECT street, txn_period FROM {_CURRENT} WHERE user_id = :user_id
                UNION ALL
                SELECT street, txn_period FROM {_HISTORY} WHERE user_id = :user_id
            ) AS versions
            ORDER BY txn_start
            """
        ),
        {"user_id": user_id},
    ).all()

    assert [(row.street, row.txn_start, row.txn_end) for row in timeline] == [
        ("123 Apple St", JAN_1, JUN_20),
        ("456 Banana St", JUN_20, JUL_1),
        ("789 Cherry St", JUL_1, None),
    ]


def test_as_of_returns_the_belief_held_at_that_transaction_time(session, user_id):
    """Each correction is what an as-of read returns during its transaction period."""
    assert _street(session, user_id, txn_time=JAN_15, app_time=JUN_1) == "123 Apple St"
    assert _street(session, user_id, txn_time=JUN_21, app_time=JUN_1) == "456 Banana St"
    assert _street(session, user_id, txn_time=JUL_2, app_time=JUN_1) == "789 Cherry St"


def test_as_of_follows_half_open_periods(session, user_id):
    """A period contains its lower bound and excludes its upper bound."""
    assert _street(session, user_id, txn_time=JAN_1, app_time=JAN_1) == "123 Apple St"
    assert (
        _street(session, user_id, txn_time=BEFORE_JUN_20, app_time=JUN_1)
        == "123 Apple St"
    )
    assert _street(session, user_id, txn_time=JUN_20, app_time=JUN_1) == "456 Banana St"
    assert (
        _street(session, user_id, txn_time=BEFORE_JUL_1, app_time=JUN_1)
        == "456 Banana St"
    )
    assert _street(session, user_id, txn_time=JUL_1, app_time=JUN_1) == "789 Cherry St"


def _record_apple(session, uid: int) -> None:
    _write_at(
        session,
        JAN_1,
        lambda: session.add(
            Address(user_id=uid, street="123 Apple St", app_period=APP_PERIOD)
        ),
    )


def test_move_is_visible_only_after_it_was_recorded(session):
    """Trimming at June 13, recorded June 20, keeps the old belief until then.

    On June 14 in application time, the bank still believed Apple St on
    June 19. From June 21 it believes Banana St. June 1 stays Apple St.
    """
    uid = 7
    _record_apple(session, uid)

    def move() -> None:
        Writer(Address, session).clear_key_period(
            {"user_id": uid}, TimeRange(JUN_13, None)
        )
        session.add(
            Address(
                user_id=uid,
                street="456 Banana St",
                app_period=TimestamptzRange(JUN_13, None),
            )
        )

    _write_at(session, JUN_20, move)

    assert _street(session, uid, txn_time=JUN_19, app_time=JUN_14) == "123 Apple St"
    assert _street(session, uid, txn_time=JUN_21, app_time=JUN_14) == "456 Banana St"
    assert _street(session, uid, txn_time=JUN_21, app_time=JUN_1) == "123 Apple St"


def test_clearing_an_open_ended_period_deletes_the_current_row(session):
    """Clearing ``[start, )`` retires the row instead of writing an empty range."""
    uid = 8
    _record_apple(session, uid)
    _write_at(
        session,
        JUN_20,
        lambda: Writer(Address, session).clear_key_period(
            {"user_id": uid}, TimeRange(JAN_1, None)
        ),
    )

    assert session.query(Address).filter(Address.user_id == uid).count() == 0
    assert _street(session, uid, txn_time=JAN_15, app_time=JUN_1) == "123 Apple St"
    with pytest.raises(NoResultFound):
        _street(session, uid, txn_time=JUN_21, app_time=JUN_1)


def test_clearing_a_shared_start_keeps_the_open_suffix(session):
    """Clearing the opening span leaves the open suffix, not an empty range."""
    uid = 9
    _record_apple(session, uid)
    _write_at(
        session,
        JUN_20,
        lambda: Writer(Address, session).clear_key_period(
            {"user_id": uid}, TimeRange(JAN_1, JUN_13)
        ),
    )

    kept = session.query(Address).filter(Address.user_id == uid).one()
    assert kept.street == "123 Apple St"
    assert kept.app_period.lower == JUN_13
    assert kept.app_period.upper is None
    with pytest.raises(NoResultFound):
        _street(session, uid, txn_time=JUN_21, app_time=JUN_1)
    assert _street(session, uid, txn_time=JUN_21, app_time=JUN_14) == "123 Apple St"


def test_clearing_an_interior_window_splits_the_row(session):
    """A window strictly inside an open period leaves a prefix and a suffix.

    Apple St covers [Jan 1, ). Clearing [Jun 1, Jun 13) on June 20 punches a
    hole. Until that recording, June 5 is still Apple St. Afterwards, May 1
    and June 14 stay Apple St, and June 5 is empty.
    """
    uid = 10
    _record_apple(session, uid)

    def punch() -> None:
        original = session.query(Address).filter(Address.user_id == uid).one()
        original_id = original.row_id
        Writer(Address, session).clear_key_period(
            {"user_id": uid}, TimeRange(JUN_1, JUN_13)
        )
        assert session.get(Address, original_id) is None

    _write_at(session, JUN_20, punch)

    assert _street(session, uid, txn_time=JUN_19, app_time=JUN_5) == "123 Apple St"
    assert _street(session, uid, txn_time=JUN_21, app_time=MAY_1) == "123 Apple St"
    assert _street(session, uid, txn_time=JUN_21, app_time=JUN_14) == "123 Apple St"
    with pytest.raises(NoResultFound):
        _street(session, uid, txn_time=JUN_21, app_time=JUN_5)

    current = (
        session.query(Address)
        .filter(Address.user_id == uid)
        .order_by(Address.app_period)
        .all()
    )
    assert [(row.app_period.lower, row.app_period.upper) for row in current] == [
        (JAN_1, JUN_1),
        (JUN_13, None),
    ]


def test_as_of_raises_when_no_row_contains_both_times(session, user_id):
    """A miss on either time axis raises, including an unknown key."""
    writer = Writer(Address, session)
    with pytest.raises(NoResultFound):
        writer.as_of({"user_id": user_id}, app_time=BEFORE_APP, txn_time=JAN_15)
    with pytest.raises(NoResultFound):
        writer.as_of({"user_id": user_id}, app_time=JUN_1, txn_time=BEFORE_APP)
    with pytest.raises(NoResultFound):
        writer.as_of({"user_id": user_id + 1}, app_time=JUN_1, txn_time=JAN_15)


def test_to_history_table_returns_the_same_class_when_called_again():
    """A second call hands back the mapped class, never a bare Table."""
    assert to_history_table(Address, [_AddressMixin]) is AddressHistory
    assert history_class_of(Address) is AddressHistory
    assert not hasattr(Address, "history_class")


@pytest.mark.parametrize(
    ("verb", "mutate"),
    [
        ("insert into", lambda s, row: s.add(AddressHistory(**row))),
        ("update", lambda s, row: setattr(row, "street", "999 Elm St")),
        ("delete from", lambda s, row: s.delete(row)),
    ],
)
def test_orm_writes_to_history_name_the_table(session, user_id, verb, mutate):
    """The ORM refuses history writes, and the message names the history table."""
    if verb == "insert into":
        target = {
            "user_id": 999,
            "street": "1 Nowhere",
            "app_period": APP_PERIOD,
            "txn_period": TimestamptzRange(JAN_1, JUN_1),
        }
    else:
        target = (
            session.query(AddressHistory)
            .filter(AddressHistory.user_id == user_id)
            .first()
        )
    mutate(session, target)
    with pytest.raises(ValueError, match=f"^May not {verb} {_HISTORY}$"):
        session.flush()
