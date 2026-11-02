"""The Late Letter: a small game about two kinds of time, on a local Postgres.

A bank keeps an address book. Every belief in it has two periods: when the
address was true in the customer's life (application time) and when the bank
believed it (transaction time). The page walks through that one chapter at a
time. Every answer and every write goes to a real bitemporal table.

From the repository root:

    uv run python example/web/app.py

The default database is postgresql+psycopg://127.0.0.1:5432/pg_bitemporal_example.
Set PG_BITEMPORAL_EXAMPLE_URL to point somewhere else. The process creates that
database when Postgres is up and this user can create databases. Set PORT to
listen somewhere other than 8080.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from psycopg import OperationalError as PsycopgOperationalError
from psycopg.types.range import Range, TimestamptzRange
from sqlalchemy import (
    Column,
    DateTime,
    String,
    bindparam,
    create_engine,
    delete,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import MultipleResultsFound, NoResultFound, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from pg_bitemporal.sqlalchemy.base import CurrentBase, HistoryBase, to_history_table
from pg_bitemporal.sqlalchemy.writer import TimeRange, Writer

DEFAULT_URL = "postgresql+psycopg://127.0.0.1:5432/pg_bitemporal_example"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLACEHOLDER = re.compile(r"%\((\w+)\)s")
_ALIAS = re.compile(r"(\w+\.\w+) AS \w+")
_CASTS = (
    ("::TIMESTAMP WITH TIME ZONE", "::timestamptz"),
    ("::TSTZRANGE", "::tstzrange"),
    ("::VARCHAR", ""),
    ("::BIGINT", ""),
    ("::INTEGER", ""),
)
_TIMESTAMPTZ = DateTime(timezone=True)
_HERE = Path(__file__).resolve().parent
_FUNCTIONS = _HERE.parents[1] / "pg_bitemporal" / "functions.sql"
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

# Each chapter of the game starts from one customer's story.
# (street, true from, true until, written down on)
SEEDS: dict[str, list[tuple[str, str, str | None, str]]] = {
    "Ada": [
        ("123 Apple St", "2022-01-01", None, "2022-01-01"),
        ("456 Banana St", "2022-06-13", None, "2022-06-20"),
    ],
    "Bo": [
        ("12 Oak Rd", "2022-01-01", None, "2022-01-01"),
    ],
    "Cy": [
        ("3 Birch Ln", "2022-01-01", None, "2022-01-01"),
        ("88 Maple Dr", "2022-04-01", None, "2022-04-03"),
    ],
    "Dee": [
        ("5 Cedar Ct", "2022-01-01", None, "2022-01-01"),
        ("40 Walnut Way", "2022-05-01", None, "2022-05-02"),
    ],
}

ENGINE: Engine | None = None
SessionFactory: sessionmaker[Session] | None = None
_capture = threading.local()


class ResidenceMixin:
    key_fields_and_equality_operators = [("person", "=")]
    person = Column(String, nullable=False)
    street = Column(String, nullable=False)


class Residence(ResidenceMixin, CurrentBase):
    pass


ResidenceHistory = to_history_table(Residence, [ResidenceMixin])


class Invalid(ValueError):
    """The request itself is wrong, before Postgres sees it."""


# ---------------------------------------------------------------- formatting


def iso(value: datetime | None) -> str | None:
    """A UTC day as YYYY-MM-DD, or the full instant when it is not midnight."""
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    value = value.astimezone(UTC)
    if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
        return value.date().isoformat()
    return value.isoformat()


def sql_literal(value: object) -> str:
    """Show a bound parameter the way it would read in psql."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, datetime):
        return f"'{iso(value)}'"
    if isinstance(value, Range):
        if value.isempty:
            return "'empty'"
        lower = iso(value.lower) if value.lower is not None else ""
        upper = iso(value.upper) if value.upper is not None else ""
        bounds = value.bounds
        return f"'{bounds[0]}{lower},{upper}{bounds[1]}'"
    return "'" + str(value).replace("'", "''") + "'"


def show_sql(statement: str, parameters: object) -> str:
    """Inline the parameters of one statement, for people to read."""
    statement = " ".join(statement.split())
    if isinstance(parameters, list | tuple) and parameters:
        first = parameters[0]
        if isinstance(first, dict):
            shown = show_sql(statement, first)
            more = len(parameters) - 1
            return shown + (f"  -- and {more} more" if more else "")
        parameters = None
    if not isinstance(parameters, dict):
        return _tidy(statement)

    params: dict[str, Any] = parameters

    def swap(match: re.Match[str]) -> str:
        name = match.group(1)
        return sql_literal(params[name]) if name in params else match.group(0)

    return _tidy(_PLACEHOLDER.sub(swap, statement))


def _tidy(statement: str) -> str:
    """Drop column aliases and obvious casts that only the driver needs."""
    statement = _ALIAS.sub(r"\1", statement)
    for cast, shown in _CASTS:
        statement = statement.replace(cast, shown)
    return statement


@contextmanager
def capture() -> Generator[list[str]]:
    """Collect every statement this thread sends to Postgres."""
    log: list[str] = []
    _capture.log = log
    try:
        yield log
    finally:
        _capture.log = None


def parse_day(raw: object, *, label: str, required: bool) -> datetime | None:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        if required:
            raise Invalid(f"Choose a date for “{label}”.")
        return None
    if not isinstance(raw, str):
        raise Invalid(f"“{label}” should be a date.")
    try:
        day = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise Invalid(f"“{label}” should look like 2022-06-13.") from exc
    return day.replace(tzinfo=UTC)


def clean_person(raw: object) -> str:
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        raise Invalid("Name the customer.")
    if len(name) > 80:
        raise Invalid("That name is too long.")
    return name


def clean_street(raw: object) -> str:
    street = raw.strip() if isinstance(raw, str) else ""
    if not street:
        raise Invalid("Name the street.")
    if len(street) > 120:
        raise Invalid("That street is too long.")
    return street


def explain(exc: BaseException) -> tuple[str, str]:
    """A short reason and a stable code for a rejected write."""
    orig = getattr(exc, "orig", None)
    detail = f"{orig}\n{exc}" if orig is not None else str(exc)
    lowered = detail.lower()
    if "before this row's last write" in lowered:
        return (
            "backdated",
            "The ledger refused. Something in this period was already written "
            "down later than the day you chose, and the ledger only moves "
            "forward.",
        )
    if "exclusion" in lowered:
        return (
            "overlap",
            "That period overlaps another belief written down at the same moment.",
        )
    if "could not connect" in lowered or "connection refused" in lowered:
        return ("offline", "Postgres is not accepting connections.")
    first = detail.splitlines()[0].strip() if detail else ""
    if len(first) > 200:
        first = first[:199] + "…"
    return ("db", first or "The database rejected that write.")


# ---------------------------------------------------------------- database


def _session() -> Session:
    if SessionFactory is None:
        raise RuntimeError("database is not ready")
    return SessionFactory()


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    dbname = parsed.database
    if dbname is None or _NAME.fullmatch(dbname) is None:
        raise RuntimeError(
            "PG_BITEMPORAL_EXAMPLE_URL must name a simple database to create"
        )

    admin = create_engine(
        parsed.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
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


def _load_functions(engine: Engine) -> None:
    sql = _FUNCTIONS.read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS hstore")
        cursor.execute(sql)
        raw.commit()
    finally:
        raw.close()


def boot(url: str) -> None:
    """Create the database and tables, and seed every story the book lacks."""
    global ENGINE, SessionFactory
    _ensure_database(url)
    engine = create_engine(url, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def _utc(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _log(_conn, _cursor, statement, parameters, _context, _many) -> None:
        log = getattr(_capture, "log", None)
        if log is not None and statement.strip().upper() != "SELECT 1":
            log.append(show_sql(statement, parameters))

    _load_functions(engine)
    CurrentBase.metadata.create_all(engine)
    HistoryBase.metadata.create_all(engine)
    ENGINE = engine
    SessionFactory = sessionmaker(bind=engine, autoflush=False)
    known = set(people())
    for person in SEEDS:
        if person not in known:
            seed(person)


def write_at(when: datetime, mutate: Callable[[Session], None]) -> None:
    session = _session()
    try:
        session.execute(
            text("SELECT public.set_transaction_time(:when)").bindparams(
                bindparam("when", type_=_TIMESTAMPTZ)
            ),
            {"when": when},
        )
        mutate(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_belief(
    person: str,
    street: str,
    true_from: datetime,
    true_until: datetime | None,
    written_on: datetime,
) -> None:
    period = TimeRange(true_from, true_until)
    stored = TimestamptzRange(true_from, true_until)

    def mutate(session: Session) -> None:
        Writer(Residence, session).clear_key_period({"person": person}, period)
        session.add(Residence(person=person, street=street, app_period=stored))

    write_at(written_on, mutate)


def retract_period(
    person: str,
    true_from: datetime,
    true_until: datetime | None,
    written_on: datetime,
) -> None:
    period = TimeRange(true_from, true_until)

    def mutate(session: Session) -> None:
        Writer(Residence, session).clear_key_period({"person": person}, period)

    write_at(written_on, mutate)


def erase(person: str | None) -> None:
    """Remove rows without writing history. Only resets use this.

    Deleting a current row archives it at the wall clock. Pause that trigger
    first, or the archive overlaps the story written next.
    """
    session = _session()
    try:
        session.execute(text("SELECT public.toggle_record_history(false)"))
        current = delete(Residence)
        past = delete(ResidenceHistory)
        if person is not None:
            current = current.where(Residence.person == person)
            past = past.where(ResidenceHistory.person == person)
        session.execute(current)
        session.execute(past)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def seed(person: str) -> None:
    for street, true_from, true_until, written_on in SEEDS.get(person, []):
        start = parse_day(true_from, label="from", required=True)
        until = parse_day(true_until, label="until", required=False)
        written = parse_day(written_on, label="written", required=True)
        assert start is not None and written is not None
        record_belief(person, street, start, until, written)


def reset(person: str | None) -> None:
    """Put one customer, or the whole book, back to the start of the story."""
    erase(person)
    for name in SEEDS if person is None else [person]:
        seed(name)


def _row(row: object, table: str) -> dict[str, Any]:
    app_period = getattr(row, "app_period")
    txn_period = getattr(row, "txn_period")
    return {
        "table": table,
        "row_id": getattr(row, "row_id"),
        "person": getattr(row, "person"),
        "street": getattr(row, "street"),
        "app": [iso(app_period.lower), iso(app_period.upper)],
        "txn": [iso(txn_period.lower), iso(txn_period.upper)],
    }


def rows_for(person: str) -> list[dict[str, Any]]:
    session = _session()
    try:
        current = session.query(Residence).filter(Residence.person == person).all()
        past = (
            session.query(ResidenceHistory)
            .filter(ResidenceHistory.person == person)
            .all()
        )
    finally:
        session.close()
    rows = [_row(row, Residence.__tablename__) for row in current]
    rows.extend(_row(row, ResidenceHistory.__tablename__) for row in past)
    rows.sort(key=lambda r: (r["txn"][0] or "", r["app"][0] or "", r["row_id"]))
    return rows


def people() -> list[str]:
    session = _session()
    try:
        names = {name for (name,) in session.query(Residence.person).distinct()}
        names |= {name for (name,) in session.query(ResidenceHistory.person).distinct()}
    finally:
        session.close()
    return sorted(names)


def ask(person: str, app_time: datetime, txn_time: datetime) -> dict[str, Any]:
    """What the bank believed, at ``txn_time``, about ``app_time``."""
    session = _session()
    with capture() as log:
        try:
            row = Writer(Residence, session).as_of(
                {"person": person}, app_time=app_time, txn_time=txn_time
            )
            street: str | None = str(getattr(row, "street"))
            error = None
        except NoResultFound:
            street, error = None, None
        except MultipleResultsFound:
            street, error = None, "More than one belief matches that pair of days."
        finally:
            session.close()
    return {"street": street, "error": error, "sql": log}


# ---------------------------------------------------------------- api


def api_book(query: dict[str, list[str]]) -> dict[str, Any]:
    person = clean_person((query.get("person") or [""])[0])
    return {"person": person, "rows": rows_for(person), "people": people()}


def api_ask(query: dict[str, list[str]]) -> dict[str, Any]:
    person = clean_person((query.get("person") or [""])[0])
    app_time = parse_day((query.get("app") or [""])[0], label="app", required=True)
    txn_time = parse_day((query.get("txn") or [""])[0], label="txn", required=True)
    assert app_time is not None and txn_time is not None
    return {"person": person, **ask(person, app_time, txn_time)}


def api_write(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Record or clear one period, and report every statement it took."""
    try:
        person = clean_person(body.get("person"))
        street = clean_street(body.get("street")) if path == "/api/record" else ""
        true_from = parse_day(body.get("from"), label="from", required=True)
        true_until = parse_day(body.get("until"), label="until", required=False)
        written = parse_day(body.get("written"), label="written down", required=True)
        assert true_from is not None and written is not None
        if true_until is not None and true_until <= true_from:
            raise Invalid("The period has to end after it starts.")
    except Invalid as exc:
        return {"ok": False, "code": "invalid", "error": str(exc), "sql": []}

    before = rows_for(person)
    with capture() as log:
        try:
            if path == "/api/record":
                record_belief(person, street, true_from, true_until, written)
            else:
                retract_period(person, true_from, true_until, written)
            ok, code, error = True, None, None
        except Exception as exc:
            print(f"write failed: {exc}", file=sys.stderr)
            ok = False
            code, error = explain(exc)
    sql = ["BEGIN;", *log, "COMMIT;" if ok else "ROLLBACK;"]
    return {
        "ok": ok,
        "code": code,
        "error": error,
        "sql": sql,
        "before": before,
        "rows": rows_for(person),
        "people": people(),
    }


def api_reset(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("person")
    person = None if raw in (None, "") else clean_person(raw)
    reset(person)
    shown = person or "Ada"
    return {"ok": True, "person": shown, "rows": rows_for(shown), "people": people()}


class Handler(BaseHTTPRequestHandler):
    server_version = "late-letter"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("LATE_LETTER_QUIET"):
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in _STATIC:
            name, kind = _STATIC[parsed.path]
            self._send(200, (_HERE / name).read_bytes(), kind)
            return
        if parsed.path == "/favicon.ico":
            self._send(204, b"", "text/plain")
            return
        routes = {"/api/book": api_book, "/api/ask": api_ask}
        handler = routes.get(parsed.path)
        if handler is None:
            self._json(404, {"ok": False, "error": "Not found."})
            return
        self._run(lambda: handler(parse_qs(parsed.query)))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/record", "/api/retract", "/api/reset"}:
            self._json(404, {"ok": False, "error": "Not found."})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(400, {"ok": False, "error": "Bad length."})
            return
        if length > 10_000:
            self._json(413, {"ok": False, "error": "Too large."})
            return
        raw = self.rfile.read(length).decode() if length else "{}"
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "Send JSON."})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": "Send a JSON object."})
            return
        if parsed.path == "/api/reset":
            self._run(lambda: api_reset(body))
        else:
            self._run(lambda: api_write(parsed.path, body))

    def _run(self, work: Callable[[], dict[str, Any]]) -> None:
        try:
            self._json(200, work())
        except Invalid as exc:
            self._json(400, {"ok": False, "code": "invalid", "error": str(exc)})
        except (OperationalError, PsycopgOperationalError) as exc:
            code, error = explain(exc)
            self._json(503, {"ok": False, "code": code, "error": error})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def serve(port: int) -> tuple[ThreadingHTTPServer, threading.Thread]:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def stop(httpd: ThreadingHTTPServer, thread: threading.Thread) -> None:
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()
    if ENGINE is not None:
        ENGINE.dispose()


def main() -> None:
    url = os.environ.get("PG_BITEMPORAL_EXAMPLE_URL", DEFAULT_URL)
    port = int(os.environ.get("PORT", "8080"))
    try:
        boot(url)
    except (OperationalError, PsycopgOperationalError) as exc:
        raise SystemExit(
            "Postgres is not accepting connections. "
            "Start a local server or set PG_BITEMPORAL_EXAMPLE_URL.\n"
            f"{exc}"
        ) from exc
    try:
        httpd, _thread = serve(port)
    except OSError as exc:
        raise SystemExit(f"Could not listen on 127.0.0.1:{port}: {exc}") from exc
    shown = make_url(url).render_as_string(hide_password=True)
    print(f"http://127.0.0.1:{port}")
    print(f"Database {shown}")
    try:
        _thread.join()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        stop(httpd, _thread)


if __name__ == "__main__":
    main()
