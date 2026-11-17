"""Student Service - registration and lookup.

Run:  FT_MODE=ft DATABASE_URL=... uvicorn app:app --app-dir services/student-service
"""
import os
import sys

sys.path.insert(0, os.environ.get("FTLIB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared")))

from fastapi import HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ftlib.app import create_app
from ftlib.db import DatabaseUnavailable

app, ctx = create_app("student-service", "Student Service")
db, s = ctx.db, ctx.settings


class StudentInLoose(BaseModel):
    student_id: str | None = Field(None, description="Optional client-chosen id (used for seeding)")
    full_name: str = Field(..., min_length=1)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    program: str = "General"


SID_RE = r"^S[0-9A-F]{4,8}$"


class StudentInStrict(BaseModel):
    """FT_MODE=final (C4): strict patterns - no markup/script characters, bounded lengths."""
    student_id: str | None = Field(None, pattern=SID_RE)
    full_name: str = Field(..., min_length=1, max_length=100, pattern=r"^[^<>{}\\;\"`]+$")
    email: str = Field(..., max_length=254, pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    program: str = Field("General", max_length=40, pattern=r"^[A-Za-z0-9 ._-]+$")


StudentIn = StudentInStrict if s.is_secure else StudentInLoose
SID_PATH = Path(..., pattern=SID_RE) if s.is_secure else Path(...)


@app.post("/students", status_code=201, tags=["students"], summary="Register a student")
def register_student(body: StudentIn):
    import uuid
    ctx.require("student:write")
    sid = body.student_id or "S" + uuid.uuid4().hex[:8].upper()

    if s.is_ft:
        # Duplicate-registration protection: UNIQUE(email) + ON CONFLICT DO NOTHING makes the
        # operation idempotent, so a retried request returns the original record.
        def op(conn):
            row = conn.execute(
                "INSERT INTO student.students(student_id, full_name, email, program) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING *", (sid, body.full_name, body.email, body.program)).fetchone()
            if row:
                ctx.audit_in_tx(conn, "student:write", f"/students/{sid}", 201, f"registered {sid}")
                return row, False
            row = conn.execute("SELECT * FROM student.students WHERE email=%s OR student_id=%s",
                               (body.email, sid)).fetchone()
            return row, True

        row, existed = db.run(op)
        if existed:
            if row["email"] != body.email:
                raise HTTPException(409, "student_id already used by another student")
            from ftlib.logging import current_state
            current_state().replay = True
            ctx.metrics.replays.labels(s.service_name, s.instance_id).inc()
            ctx.cache.put(row["student_id"], row)
            return JSONResponse(_json(row), status_code=200)
        ctx.cache.put(row["student_id"], row)
        return JSONResponse(_json(row), status_code=201)

    # baseline: naive insert, no duplicate protection
    def op_b(conn):
        return conn.execute(
            "INSERT INTO student.students(student_id, full_name, email, program) VALUES (%s,%s,%s,%s) RETURNING *",
            (sid, body.full_name, body.email, body.program)).fetchone()

    return JSONResponse(_json(db.run(op_b)), status_code=201)


@app.get("/students/{student_id}", tags=["students"], summary="Look up a student")
def get_student(student_id: str = SID_PATH):
    ctx.require("student:read", student_id)

    def op(conn):
        return conn.execute("SELECT * FROM student.students WHERE student_id=%s", (student_id,)).fetchone()

    if s.is_ft:
        try:
            row = db.run(op, deadline=1.0, fail_fast=True)
        except DatabaseUnavailable:
            # Graceful degradation: serve the last known good copy instead of failing.
            cached = ctx.cache.get(student_id)
            if cached is None:
                raise
            ctx.mark_degraded("stale-cache")
            return _json(cached)
        if row:
            ctx.cache.put(student_id, row)
    else:
        row = db.run(op)
    if not row:
        raise HTTPException(404, "student not found")
    return _json(row)


def _json(row: dict) -> dict:
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(row)
