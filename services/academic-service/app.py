"""Academic Records / Transcript Service - grades and transcripts."""
import os
import sys
import uuid

sys.path.insert(0, os.environ.get("FTLIB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared")))

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ftlib.app import create_app
from ftlib.client import ServiceClient, ServiceUnavailable
from ftlib.db import DatabaseUnavailable
from ftlib.logging import current_state

app, ctx = create_app("academic-service", "Academic Records Service")
db, s = ctx.db, ctx.settings
student_client = ServiceClient("student-service", s.student_urls, s, ctx.metrics, ctx.log) if s.student_urls else None
if student_client:
    ctx.clients["student-service"] = student_client


class GradeIn(BaseModel):
    student_id: str
    course_code: str
    term: str
    grade: float = Field(..., ge=0, le=100)


class BatchIn(BaseModel):
    batch_id: str | None = None
    records: list[GradeIn] = Field(..., min_length=1)


_INSERT_FT = ("INSERT INTO academic.records(student_id, course_code, term, grade, batch_id) "
              "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (student_id, course_code, term) DO NOTHING RETURNING *")
_INSERT_B = ("INSERT INTO academic.records(student_id, course_code, term, grade, batch_id) "
             "VALUES (%s,%s,%s,%s,%s) RETURNING *")
_SELECT = "SELECT * FROM academic.records WHERE student_id=%s AND course_code=%s AND term=%s"


@app.post("/grades", status_code=201, tags=["academic"], summary="Record a grade")
def post_grade(g: GradeIn):
    args = (g.student_id, g.course_code, g.term, g.grade, None)
    if s.is_ft:
        def op(conn):
            row = conn.execute(_INSERT_FT, args).fetchone()
            if row:
                return row, False
            return conn.execute(_SELECT, args[:3]).fetchone(), True

        row, existed = db.run(op)
        if existed:
            if float(row["grade"]) != g.grade:
                raise HTTPException(409, "a different grade is already recorded for this course/term")
            current_state().replay = True
            ctx.metrics.replays.labels(s.service_name, s.instance_id).inc()
            return JSONResponse(jsonable_encoder(row), status_code=200)
        return JSONResponse(jsonable_encoder(row), status_code=201)
    # baseline: a retried request inserts a second row
    return JSONResponse(jsonable_encoder(db.run(lambda c: c.execute(_INSERT_B, args).fetchone())), status_code=201)


@app.post("/grades/batch", status_code=201, tags=["academic"],
          summary="Record several grades (e.g. a whole class) as one unit")
def post_batch(b: BatchIn, request: Request):
    fault = request.headers.get("x-fault")
    batch_id = b.batch_id or "B" + uuid.uuid4().hex[:8]
    half = max(1, len(b.records) // 2)

    if s.is_ft:
        # Atomic: all rows or none (single transaction). A mid-batch interruption rolls everything
        # back; the client's retry then inserts the complete batch exactly once (ON CONFLICT).
        def op(conn):
            inserted = 0
            for i, g in enumerate(b.records):
                if i == half:
                    ctx.maybe_fault(fault, "mid_batch")
                row = conn.execute(_INSERT_FT, (g.student_id, g.course_code, g.term, g.grade, batch_id)).fetchone()
                inserted += 1 if row else 0
            return inserted

        inserted = db.run(op)
        if inserted == 0:
            current_state().replay = True
            ctx.metrics.replays.labels(s.service_name, s.instance_id).inc()
        return JSONResponse({"batch_id": batch_id, "inserted": inserted, "total": len(b.records)},
                            status_code=201 if inserted else 200)

    # baseline: every row is its own autocommit statement -> partial batches survive interruption
    def op_b(conn):
        n = 0
        for i, g in enumerate(b.records):
            if i == half:
                ctx.maybe_fault(fault, "mid_batch")
            conn.execute(_INSERT_B, (g.student_id, g.course_code, g.term, g.grade, batch_id))
            n += 1
        return n

    n = db.run(op_b, autocommit=True)
    return JSONResponse({"batch_id": batch_id, "inserted": n, "total": len(b.records)}, status_code=201)


@app.get("/students/{student_id}/transcript", tags=["academic"], summary="Transcript with student details")
def transcript(student_id: str, request: Request):
    rid = current_state().request_id

    def op(conn):
        return conn.execute("SELECT course_code, term, grade FROM academic.records WHERE student_id=%s "
                            "ORDER BY term, course_code", (student_id,)).fetchall()

    if s.is_ft:
        try:
            rows = db.run(op, deadline=1.0, fail_fast=True)
        except DatabaseUnavailable:
            cached = ctx.cache.get(student_id)
            if cached is None:
                raise
            ctx.mark_degraded("stale-cache")
            return jsonable_encoder({**cached, "degraded": "stale-cache"})
    else:
        rows = db.run(op)

    # enrich with student details from the Student Service (inter-service dependency)
    student, degraded = None, None
    if student_client is not None:
        if ctx.overloaded:
            degraded = "skipped-student-lookup-overload"      # shed optional work under load
            ctx.mark_degraded("overload")
        else:
            try:
                r = student_client.get(f"/students/{student_id}", request_id=rid)
                if r.status_code == 404:
                    raise HTTPException(404, "student not found")
                r.raise_for_status()          # baseline: a 5xx from the dependency becomes our 502
                student = r.json()
            except ServiceUnavailable:
                # Graceful degradation: transcript is still useful without the profile data.
                degraded = "student-service-unavailable"
                ctx.mark_degraded("student-unavailable")
    body = {"student_id": student_id, "student": student, "records": rows,
            "gpa": round(sum(float(r["grade"]) for r in rows) / len(rows), 2) if rows else None,
            "degraded": degraded}
    if s.is_ft:
        ctx.cache.put(student_id, body)
    return jsonable_encoder(body)
