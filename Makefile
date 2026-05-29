# Earlybird — developer & judge entrypoints.
# Everything a judge needs runs with one command and no cloud services.

.PHONY: help install test migrate up down demo demo-judge

help:
	@echo "Earlybird make targets:"
	@echo "  make install     Install Python dependencies"
	@echo "  make test        Run the full test suite (in-memory SQLite; no Postgres/Redis)"
	@echo "  make migrate     Apply database migrations (alembic upgrade head; needs Postgres)"
	@echo "  make up          docker-compose up --build (full stack)"
	@echo "  make down        docker-compose down"
	@echo "  make demo-judge  Offline end-to-end proof of a WIN (no cloud services needed)"
	@echo "  make demo        HTTP demo against a running API (uvicorn must be up)"

install:
	pip install -r requirements.txt

test:
	pytest -q

migrate:
	alembic upgrade head

up:
	docker-compose up --build

down:
	docker-compose down

# Self-contained: runs the real pipeline on SQLite + a fake delivered alert and
# prints a verifiable WIN read back from the judge audit endpoint.
demo-judge:
	python demo_judge.py

# Hits a running API (uvicorn app.main:app) — for a live Slack/dashboard demo.
demo:
	python simulate_demo.py
