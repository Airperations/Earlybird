"""
Test-only shim: render Postgres JSONB as plain JSON when the bind is SQLite.

Production runs on PostgreSQL, so models legitimately use JSONB. The in-memory
SQLite used by the dedup tests can't compile JSONB, so we teach it to fall back
to JSON here. This does NOT affect production behavior.
"""
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):
    return "JSON"
