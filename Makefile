# Earlybird — developer & judge entrypoints.
# Everything a judge needs runs with one command and no cloud services.

# Use the project virtualenv automatically when present, so `make test` /
# `make demo-judge` work without manually activating it. Falls back to `python`.
PYTHON := $(shell [ -x ./.venv/bin/python ] && echo ./.venv/bin/python || echo python)

.PHONY: help install test migrate up down demo demo-judge

help:
	@echo "Earlybird make targets (using PYTHON=$(PYTHON)):"
	@echo "  make install     Install Python dependencies"
	@echo "  make test        Run the full test suite (in-memory SQLite; no Postgres/Redis)"
	@echo "  make migrate     Apply database migrations (alembic upgrade head; needs Postgres)"
	@echo "  make up          docker-compose up --build (full stack)"
	@echo "  make down        docker-compose down"
	@echo "  make demo-judge  Offline end-to-end proof of a WIN (no cloud services needed)"
	@echo "  make demo        HTTP demo against a running API (uvicorn must be up)"

install:
	$(PYTHON) -m pip install -r requirements.txt

test:
	$(PYTHON) -m pytest -q

migrate:
	$(PYTHON) -m alembic upgrade head

up:
	docker-compose up --build

down:
	docker-compose down

# Self-contained: runs the real pipeline on SQLite + a fake delivered alert and
# prints a verifiable WIN read back from the judge audit endpoint.
demo-judge:
	$(PYTHON) demo_judge.py

# Hits a running API (uvicorn app.main:app) — for a live Slack/dashboard demo.
demo:
	$(PYTHON) simulate_demo.py
