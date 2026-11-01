.PHONY: init check test

init:
	uv sync --all-groups

check:
	uv run ruff check pg_bitemporal tests example
	uv run ruff format --check pg_bitemporal tests example
	uv run ty check
	uv run pytest -q

test:
	uv run pytest -q
