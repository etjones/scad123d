default: test

run: test

test:
    uv run pytest -q

# Only the tiers that need no OpenSCAD binary (what CI runs)
test-ci:
    uv run pytest -q -m "not needs_openscad"

watch:
    uv run watchfiles 'uv run pytest -q' src tests

# Regenerate committed .csg fixtures and reference metrics (needs OpenSCAD)
fixtures:
    uv run python -m tests.regen_fixtures
