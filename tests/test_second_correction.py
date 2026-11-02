"""Corrections stay readable at the transaction time they were believed.

The fixture expects PostgreSQL. Set ``PG_BITEMPORAL_TEST_URL`` to the target
database. When unset, the test uses ``postgresql+psycopg:///pg_bitemporal_test`` and
creates that database if the server is up.
"""

import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.types.range import TimestamptzRange
from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Table,
    bindparam,
    create_engine,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError, NoResultFound, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from pg_bitemporal.sqlalchemy import install_functions
from pg_bitemporal.sqlalchemy.base import Bitemporal, BitemporalHistory
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


class _Base(DeclarativeBase):
    pass


class AddressHistory(BitemporalHistory):
    user_id: Mapped[int]
    street: Mapped[str]


class Address(Bitemporal, _Base):
    __tablename__ = "address"
    __bitemporal_key__ = ("user_id",)
    __history_model__ = AddressHistory
    user_id: Mapped[int] = mapped_column(Integer)
    street: Mapped[str] = mapped_column(String)


class Ticket(Bitemporal[uuid.UUID], _Base):
    """A uuid primary key, filled in by the database."""

    __tablename__ = "ticket"
    __bitemporal_key__ = ("code",)
    row_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    code: Mapped[str]


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
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    install_functions(engine)
    _Base.metadata.create_all(engine)


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


def test_the_mixin_builds_a_history_model_on_the_same_metadata():
    """Declaring the model is enough: its history is mapped beside it."""
    history = AddressHistory.__table__
    assert Address.__history_model__ is AddressHistory
    assert history.name == "address_history"
    assert history.metadata is _Base.metadata
    assert [c.name for c in history.primary_key] == ["row_id", "txn_period"]
    assert [c.name for c in history.columns] == [
        c.name for c in inspect(Address).local_table.columns
    ]


def test_table_args_do_not_drop_the_bitemporal_constraints():
    """The mixin adds its constraints to the table, so __table_args__ is free."""

    class Base(DeclarativeBase):
        pass

    class Tagged(Bitemporal, Base):
        __tablename__ = "tagged"
        __table_args__ = {"comment": "mine"}
        __bitemporal_key__ = {"label": "="}
        label: Mapped[str]

    table = inspect(Tagged).local_table
    assert isinstance(table, Table)
    names = {c.name for c in table.constraints}
    assert {"tagged_bitemporal_unique_key", "tagged_bitemporal_key_exclusion"} <= names
    assert table.comment == "mine"


def test_a_model_without_a_key_is_refused():
    class Base(DeclarativeBase):
        pass

    with pytest.raises(TypeError, match="must set __bitemporal_key__"):

        class Keyless(Bitemporal, Base):
            __tablename__ = "keyless"
            name: Mapped[str]


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


def _history_count(session, uid: int) -> int:
    return session.execute(
        text(f"SELECT count(*) FROM {_HISTORY} WHERE user_id = :uid"), {"uid": uid}
    ).scalar_one()


def test_a_table_named_like_the_old_flag_does_not_disable_history(session):
    """The flags are settings now, so no table on search_path can flip them."""
    uid = 20
    _record_apple(session, uid)
    session.execute(text("CREATE TABLE public.record_history_disabled ()"))
    try:
        _write_at(session, JUN_20, lambda: _set_street(session, uid, "456 Banana St"))
        assert _history_count(session, uid) == 1
    finally:
        session.execute(text("DROP TABLE public.record_history_disabled"))
        session.commit()


def test_pinned_time_reads_back_under_another_datestyle_and_zone(session):
    """The pinned instant does not depend on the session's display settings."""
    uid = 21

    def insert_elsewhere() -> None:
        session.execute(text("SET LOCAL DateStyle = 'SQL, DMY'"))
        session.execute(text("SET LOCAL TIME ZONE 'America/New_York'"))
        # Plain SQL: psycopg cannot parse timestamps returned under this DateStyle.
        session.execute(
            text(
                f"INSERT INTO {_CURRENT} (user_id, street, app_period) "
                "VALUES (:uid, '1 Oak St', '[2022-01-01Z,)')"
            ),
            {"uid": uid},
        )

    _write_at(session, JUN_20 + timedelta(microseconds=7), insert_elsewhere)
    row = session.query(Address).filter(Address.user_id == uid).one()
    assert row.txn_period.lower == JUN_20 + timedelta(microseconds=7)


def test_flags_end_with_the_transaction(session):
    """A pinned time and a paused history both reset at commit."""
    uid = 22
    _record_apple(session, uid)

    session.execute(text("SELECT public.toggle_record_history(false)"))
    _set_street(session, uid, "456 Banana St")
    session.commit()
    assert _history_count(session, uid) == 0

    # The next transaction tracks history again, at the wall clock.
    _set_street(session, uid, "789 Cherry St")
    session.commit()
    assert _history_count(session, uid) == 1
    row = session.query(Address).filter(Address.user_id == uid).one()
    assert row.txn_period.lower > JUL_2


def test_pinning_while_history_is_off_says_why(session):
    session.execute(text("SELECT public.toggle_record_history(false)"))
    with pytest.raises(DBAPIError, match="no effect while history tracking is off"):
        session.execute(text("SELECT public.set_transaction_time(now())"))


def test_backdating_names_both_times(session):
    uid = 23
    _record_apple(session, uid)
    with pytest.raises(DBAPIError, match="is before this row's last write") as info:
        _write_at(session, BEFORE_APP, lambda: _set_street(session, uid, "Nope"))
    orig = info.value.orig
    assert isinstance(orig, psycopg.Error)
    hint = orig.diag.message_hint or ""
    assert hint.startswith("Transaction time only moves forward")


def _raw_pair(session, history_columns: str) -> None:
    """A current table and a hand-written history table with its own column order."""
    session.execute(
        text(
            """
            CREATE TABLE swap (
                row_id bigserial PRIMARY KEY,
                app_period tstzrange NOT NULL,
                txn_period tstzrange NOT NULL DEFAULT tstzrange(now(), NULL),
                user_id int NOT NULL,
                street text NOT NULL,
                note text
            )
            """
        )
    )
    session.execute(text(f"CREATE TABLE swap_history ({history_columns})"))
    session.execute(
        text(
            "CREATE TRIGGER versioning_trigger BEFORE INSERT OR UPDATE OR DELETE "
            "ON swap FOR EACH ROW EXECUTE PROCEDURE record_txn_history('swap_history')"
        )
    )


@pytest.fixture
def raw_tables(session):
    yield session
    session.rollback()
    session.execute(text("DROP TABLE IF EXISTS swap, swap_history"))
    session.commit()


def _insert_and_update_swap(session) -> None:
    _write_at(
        session,
        JAN_1,
        lambda: session.execute(
            text(
                "INSERT INTO swap (app_period, user_id, street, note) "
                "VALUES ('[2022-01-01Z,)', 1, '123 Apple St', 'first')"
            )
        ),
    )
    _write_at(
        session,
        JUN_20,
        lambda: session.execute(text("UPDATE swap SET street = '456 Banana St'")),
    )


def test_history_copy_matches_columns_by_name(raw_tables):
    """A history table with columns in another order still gets the right values."""
    session = raw_tables
    _raw_pair(
        session,
        "note text, street text NOT NULL, user_id int NOT NULL, "
        "txn_period tstzrange NOT NULL, app_period tstzrange NOT NULL, "
        "row_id bigint NOT NULL",
    )
    _insert_and_update_swap(session)

    row = session.execute(
        text("SELECT street, note, user_id, txn_period FROM swap_history")
    ).one()
    assert (row.street, row.note, row.user_id) == ("123 Apple St", "first", 1)
    assert (row.txn_period.lower, row.txn_period.upper) == (JAN_1, JUN_20)


def test_history_copy_refuses_to_drop_a_column(raw_tables):
    """A current column with nowhere to go in history fails instead of vanishing."""
    session = raw_tables
    _raw_pair(
        session,
        "row_id bigint, app_period tstzrange, txn_period tstzrange, "
        "user_id int, street text",
    )
    with pytest.raises(DBAPIError, match=r"swap_history has no column for \{note\}"):
        _insert_and_update_swap(session)


def test_install_functions_can_run_again(engine):
    """Installing over an existing install changes nothing and does not fail."""
    install_functions(engine)
    with engine.connect() as conn:
        install_functions(conn)
        conn.rollback()
        assert conn.execute(text("SELECT public.toggle_record_history(true)")).scalar()


def test_a_generated_history_model_is_named_after_the_model():
    history = Ticket.__history_model__
    assert history.__name__ == "TicketHistory"
    assert history.__tablename__ == "ticket_history"
    assert issubclass(history, BitemporalHistory)


def test_a_uuid_row_id_survives_a_split(session):
    """split_record casts the text id to uuid, and new rows take the default."""
    _write_at(
        session,
        JAN_1,
        lambda: session.add(Ticket(code="T-1", app_period=APP_PERIOD)),
    )
    original = session.query(Ticket).one()
    original_id = original.row_id

    _write_at(
        session,
        JUN_20,
        lambda: Writer(Ticket, session).clear_key_period(
            {"code": "T-1"}, TimeRange(JUN_1, JUN_13)
        ),
    )

    pieces = session.query(Ticket).order_by(Ticket.app_period).all()
    assert [(p.app_period.lower, p.app_period.upper) for p in pieces] == [
        (JAN_1, JUN_1),
        (JUN_13, None),
    ]
    ids = {p.row_id for p in pieces}
    assert len(ids) == 2 and original_id not in ids
    assert all(isinstance(i, uuid.UUID) for i in ids)
    retired = session.query(Ticket.__history_model__).one()
    assert retired.row_id == original_id


def test_a_row_id_the_database_cannot_fill_is_refused():
    class Base(DeclarativeBase):
        pass

    with pytest.raises(TypeError, match="row_id needs a database default"):

        class Handmade(Bitemporal[uuid.UUID], Base):
            __tablename__ = "handmade"
            __bitemporal_key__ = ("name",)
            row_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
            name: Mapped[str]


def test_a_declared_history_model_must_match_the_columns():
    class Base(DeclarativeBase):
        pass

    class WrongHistory(BitemporalHistory):
        nmae: Mapped[str]

    with pytest.raises(TypeError, match=r"annotates \['nmae'\]"):

        class Right(Bitemporal, Base):
            __tablename__ = "right"
            __bitemporal_key__ = ("name",)
            __history_model__ = WrongHistory
            name: Mapped[str]
