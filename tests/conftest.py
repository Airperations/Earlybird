"""
Test-only shim: render Postgres JSONB as plain JSON when the bind is SQLite.

Production runs on PostgreSQL, so models legitimately use JSONB. The in-memory
SQLite used by the dedup tests can't compile JSONB, so we teach it to fall back
to JSON here. This does NOT affect production behavior.
"""
import os

# app.database now requires DATABASE_URL at import time (no local fallback).
# Provide a dummy so importing app modules under test never crashes; the dedup
# tests build their own in-memory SQLite engine and don't use this value.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):
    return "JSON"
