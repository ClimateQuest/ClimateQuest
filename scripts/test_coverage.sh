#!/usr/bin/env bash
# Validates the whole project with one command: no missing migrations, every test passes, 100 % coverage.
# The database is chosen with TEST_DB=sqlite (default) or TEST_DB=postgres, see ClimateQuest/settingstest.py.
# Another interpreter can be selected with PYTHON=/path/to/python.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON="${PYTHON:-python}"
export TEST_DB="${TEST_DB:-sqlite}"

make migrations-check PYTHON="${PYTHON}"
"${PYTHON}" -m coverage erase
"${PYTHON}" -m coverage run -m pytest -q
"${PYTHON}" -m coverage report --fail-under=100
