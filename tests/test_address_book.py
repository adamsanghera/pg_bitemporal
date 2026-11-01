"""The Late Letter serves each chapter's story from a local Postgres."""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.types.range import TimestamptzRange
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError

from example.web.app import boot, serve, show_sql, sql_literal, stop


def _book_url() -> str:
    raw = os.environ.get(
        "PG_BITEMPORAL_TEST_URL",
        "postgresql+psycopg://127.0.0.1:5432/pg_bitemporal_test",
    )
    return str(make_url(raw).set(database="pg_bitemporal_example_test"))


def _open(url: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    deadline = time.monotonic() + 3
    while True:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()
        except urllib.error.URLError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _json(url: str, body: dict | None = None) -> dict:
    status, text = _open(url, body)
    assert status == 200, text
    return json.loads(text)


@pytest.fixture(scope="module")
def base():
    try:
        boot(_book_url())
    except (OperationalError, PsycopgOperationalError) as exc:
        pytest.skip(f"PostgreSQL is not reachable: {exc}")
    httpd, thread = serve(0)
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        stop(httpd, thread)


def _ask(base: str, person: str, app: str, txn: str) -> str | None:
    query = urlencode({"person": person, "app": app, "txn": txn})
    return _json(f"{base}/api/ask?{query}")["street"]


def test_sql_reads_like_psql():
    """Captured statements show their parameters inline, ranges included."""
    january = datetime(2022, 1, 1, tzinfo=UTC)
    assert sql_literal(TimestamptzRange(january, None)) == "'[2022-01-01,)'"
    assert sql_literal("O'Hare") == "'O''Hare'"
    shown = show_sql(
        "SELECT *\n  FROM residence WHERE person = %(person_1)s",
        {"person_1": "Ada"},
    )
    assert shown == "SELECT * FROM residence WHERE person = 'Ada'"


def test_late_letter(base):
    """Ada's late letter reads back on both axes."""
    _json(f"{base}/api/reset", {"person": "Ada"})
    assert _ask(base, "Ada", "2022-06-16", "2022-06-16") == "123 Apple St"
    assert _ask(base, "Ada", "2022-06-16", "2022-07-01") == "456 Banana St"
    assert _ask(base, "Ada", "2022-06-01", "2022-07-01") == "123 Apple St"

    book = _json(f"{base}/api/book?person=Ada")
    tables = {row["table"] for row in book["rows"]}
    assert tables == {"residence", "residence_history"}
    assert {"Ada", "Bo", "Cy", "Dee"} <= set(book["people"])

    asked = _json(f"{base}/api/ask?person=Ada&app=2022-06-16&txn=2022-06-16")
    assert any("residence_history" in line for line in asked["sql"])


def test_open_the_mail(base):
    """Bo's first write lands on 10 March and leaves 9 March alone."""
    _json(f"{base}/api/reset", {"person": "Bo"})
    res = _json(
        f"{base}/api/record",
        {
            "person": "Bo",
            "street": "9 Elm Ave",
            "from": "2022-03-01",
            "until": "",
            "written": "2022-03-10",
        },
    )
    assert res["ok"], res
    assert res["sql"][0] == "BEGIN;" and res["sql"][-1] == "COMMIT;"
    assert any(
        "set_transaction_time('2022-03-10'::timestamptz)" in s for s in res["sql"]
    )
    assert _ask(base, "Bo", "2022-03-01", "2022-03-11") == "9 Elm Ave"
    assert _ask(base, "Bo", "2022-02-28", "2022-03-11") == "12 Oak Rd"
    assert _ask(base, "Bo", "2022-03-05", "2022-03-09") == "12 Oak Rd"
    archived = [r for r in res["rows"] if r["table"] == "residence_history"]
    assert archived and archived[0]["txn"] == ["2022-01-01", "2022-03-10"]


def test_clerks_mistake_stays_on_record(base):
    """Correcting Cy keeps the Maple Dr mistake in history."""
    _json(f"{base}/api/reset", {"person": "Cy"})
    res = _json(
        f"{base}/api/record",
        {
            "person": "Cy",
            "street": "3 Birch Ln",
            "from": "2022-04-01",
            "written": "2022-04-15",
        },
    )
    assert res["ok"], res
    assert _ask(base, "Cy", "2022-04-10", "2022-04-16") == "3 Birch Ln"
    assert _ask(base, "Cy", "2022-04-10", "2022-04-05") == "88 Maple Dr"
    maple = [r for r in res["rows"] if r["street"] == "88 Maple Dr"]
    assert [r["table"] for r in maple] == ["residence_history"]


def test_no_backdating(base):
    """Writing before an existing belief is refused and rolled back."""
    _json(f"{base}/api/reset", {"person": "Dee"})
    form = {
        "person": "Dee",
        "street": "40 Walnut Way",
        "from": "2022-04-01",
        "written": "2022-04-01",
    }
    refused = _json(f"{base}/api/record", form)
    assert not refused["ok"]
    assert refused["code"] == "backdated"
    assert refused["sql"][-1] == "ROLLBACK;"
    assert refused["rows"] == refused["before"]

    honest = _json(f"{base}/api/record", {**form, "written": "2022-05-20"})
    assert honest["ok"], honest
    assert _ask(base, "Dee", "2022-04-15", "2022-05-21") == "40 Walnut Way"
    assert _ask(base, "Dee", "2022-04-15", "2022-05-19") == "5 Cedar Ct"


def test_forget_splits_a_period(base):
    """Clearing the middle of an address leaves the two ends."""
    _json(f"{base}/api/reset", {"person": "Bo"})
    res = _json(
        f"{base}/api/retract",
        {
            "person": "Bo",
            "from": "2022-02-01",
            "until": "2022-03-01",
            "written": "2022-03-10",
        },
    )
    assert res["ok"], res
    assert any("split_record" in s for s in res["sql"])
    assert _ask(base, "Bo", "2022-02-15", "2022-03-11") is None
    assert _ask(base, "Bo", "2022-01-15", "2022-03-11") == "12 Oak Rd"
    assert _ask(base, "Bo", "2022-03-15", "2022-03-11") == "12 Oak Rd"


def test_bad_requests_say_why(base):
    res = _json(
        f"{base}/api/record",
        {"person": "Bo", "street": "", "from": "2022-03-01", "written": "2022-03-10"},
    )
    assert res["code"] == "invalid"
    assert res["error"] == "Name the street."

    status, page = _open(base + "/")
    assert status == 200 and "The Late Letter" in page
    status, _text = _open(base + "/app.js")
    assert status == 200
    status, _text = _open(base + "/missing")
    assert status == 404
