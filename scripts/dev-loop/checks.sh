#!/usr/bin/env bash
set -euo pipefail
ruff check .
ruff format --check .
pytest --cov --cov-report=term-missing
