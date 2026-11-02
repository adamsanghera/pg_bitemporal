"""Two transactions writing one bitemporal key.

Each test drives two sessions on separate connections, step by step, so the
interleaving is fixed. Where one side has to block, the other sets a short
``lock_timeout`` and the test checks that the wait timed out.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.range import TimestamptzRange
from sqlalchemy import Engine, String, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from pg_bitemporal.sqlalchemy.base import Bitemporal, BitemporalHistory
from pg_bitemporal.sqlalchemy.writer import TimeRange, Writer

APP_PERIOD = TimestamptzRange(datetime(2022, 1, 1, tzinfo=UTC), None)


class _Base(DeclarativeBase):
    pass


class AccountHistory(BitemporalHistory):
    account: Mapped[str]
    owner: Mapped[str]


class Account(Bitemporal, _Base):
    __tablename__ = "account"
    __bitemporal_key__ = ("account",)
    __history_model__ = AccountHistory
    account: Mapped[str] = mapped_column(String)
    owner: Mapped[str] = mapped_column(String)


@pytest.fixture(scope="module")
def engine(make_engine) -> Engine:
    return make_engine(_Base.metadata)


@pytest.fixture
def two(engine: Engine) -> Iterator[tuple[Session, Session]]:
    """Two sessions, each on its own connection, rolled back afterwards."""
    first, second = Session(engine), Session(engine)
    try:
        yield first, second
    finally:
        for session in (first, second):
            session.rollback()
            session.close()


def _open(session: Session, account: str, owner: str) -> None:
    session.add(Account(account=account, owner=owner, app_period=APP_PERIOD))
    session.commit()


def _rename(session: Session, account: str, owner: str) -> None:
    row = session.query(Account).filter(Account.account == account).one()
    row.owner = owner
    session.flush()


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return orig.sqlstate if isinstance(orig, psycopg.Error) else None


def _txn_starts(session: Session, account: str) -> list[datetime]:
    """The txn_period lower bound of every version of ``account``, oldest first."""
    rows = session.execute(
        text(
            """
            SELECT lower(txn_period) AS start FROM account WHERE account = :a
            UNION ALL
            SELECT lower(txn_period) FROM account_history WHERE account = :a
            ORDER BY start
            """
        ),
        {"a": account},
    )
    return [row.start for row in rows]


def test_a_transaction_that_started_earlier_can_still_write_after_a_later_one(two):
    """T1 starts, T2 writes the key and commits, then T1 writes it.

    T1's transaction began before T2's write, so a transaction time taken from
    the start of the transaction would land before T2's version and be refused.
    T1's write comes after T2's in fact, so it has to succeed and sort after it.
    """
    t1, t2 = two
    _open(t2, "acct-1", "Ada")

    t1.execute(text("SELECT 1"))  # T1's transaction starts here
    _rename(t2, "acct-1", "Bea")
    t2.commit()
    _rename(t1, "acct-1", "Cy")
    t1.commit()

    starts = _txn_starts(t1, "acct-1")
    assert len(starts) == 3
    assert starts == sorted(starts)
    assert t1.query(Account).filter(Account.account == "acct-1").one().owner == "Cy"


def test_a_writer_holds_its_key_until_commit(two):
    """While T1 has written a key, T2 can't take that key's lock."""
    t1, t2 = two
    _open(t1, "acct-2", "Ada")

    _rename(t1, "acct-2", "Bea")
    t2.execute(text("SET LOCAL lock_timeout = '100ms'"))
    with pytest.raises(DBAPIError) as info:
        Writer(Account, t2).lock_keys([{"account": "acct-2"}])
    assert _sqlstate(info.value) == "55P03"  # lock_not_available


def test_the_lock_covers_a_key_with_no_rows_yet(two):
    """Two transactions inserting a brand-new key queue on the lock."""
    t1, t2 = two
    Writer(Account, t1).lock_keys([{"account": "acct-new"}])
    t2.execute(text("SET LOCAL lock_timeout = '100ms'"))
    with pytest.raises(DBAPIError) as info:
        t2.add(Account(account="acct-new", owner="Bea", app_period=APP_PERIOD))
        t2.flush()
    assert _sqlstate(info.value) == "55P03"


def test_other_keys_are_not_blocked(two):
    t1, t2 = two
    _open(t1, "acct-3", "Ada")
    _open(t1, "acct-4", "Ada")

    _rename(t1, "acct-3", "Bea")
    t2.execute(text("SET LOCAL lock_timeout = '100ms'"))
    _rename(t2, "acct-4", "Cy")
    t2.commit()


def test_a_later_key_written_after_the_transaction_time_is_a_serialization_failure(
    two,
):
    """T1 writes key A, which fixes its transaction time. T2 then writes key B and
    commits. When T1 writes key B, its time is earlier than B's last write.
    That's a retryable serialization failure, not a backdating error.
    """
    t1, t2 = two
    _open(t1, "acct-a", "Ada")
    _open(t1, "acct-b", "Ada")

    _rename(t1, "acct-a", "Bea")
    _rename(t2, "acct-b", "Cy")
    t2.commit()
    with pytest.raises(DBAPIError) as info:
        _rename(t1, "acct-b", "Dee")
    assert _sqlstate(info.value) == "40001"
    assert "Writer.lock_keys" in str(info.value)


def test_locking_every_key_first_avoids_the_failure(two):
    """Locking A and B up front makes T2 wait instead, so T1 succeeds."""
    t1, t2 = two
    _open(t1, "acct-c", "Ada")
    _open(t1, "acct-d", "Ada")

    Writer(Account, t1).lock_keys([{"account": "acct-d"}, {"account": "acct-c"}])
    _rename(t1, "acct-c", "Bea")

    t2.execute(text("SET LOCAL lock_timeout = '100ms'"))
    with pytest.raises(DBAPIError) as info:
        _rename(t2, "acct-d", "Cy")
    assert _sqlstate(info.value) == "55P03"
    t2.rollback()

    _rename(t1, "acct-d", "Dee")
    t1.commit()


def test_clear_key_period_waits_for_the_key_before_reading(two):
    """clear_key_period locks first, so it can't act on a version about to change."""
    t1, t2 = two
    _open(t1, "acct-e", "Ada")

    _rename(t1, "acct-e", "Bea")
    t2.execute(text("SET LOCAL lock_timeout = '100ms'"))
    with pytest.raises(DBAPIError) as info:
        Writer(Account, t2).clear_key_period(
            {"account": "acct-e"},
            TimeRange(datetime(2022, 6, 1, tzinfo=UTC), None),
        )
    assert _sqlstate(info.value) == "55P03"


def test_lock_keys_takes_every_key_in_one_call(two):
    """Each listed key is held, including duplicates and keys with no rows yet."""
    t1, t2 = two
    Writer(Account, t1).lock_keys(
        [{"account": "acct-x"}, {"account": "acct-y"}, {"account": "acct-x"}]
    )
    for account in ("acct-x", "acct-y"):
        t2.execute(text("SAVEPOINT probe"))
        t2.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError) as info:
            Writer(Account, t2).lock_keys([{"account": account}])
        assert _sqlstate(info.value) == "55P03"
        t2.execute(text("ROLLBACK TO SAVEPOINT probe"))


def test_lock_keys_with_no_keys_does_nothing(two):
    t1, _ = two
    Writer(Account, t1).lock_keys([])
