"""Data-consistency checker: runs invariants directly against PostgreSQL.

Invariants
  I1  an invoice is charged at most once              (no duplicate payments)
  I2  accounts.total_paid == SUM(COMPLETED payments)   (ledger matches payments)
  I3  no payment is stuck PENDING after recovery settled (no half-finished transaction)
  I4  (student, course, term) appears at most once      (no duplicate academic records)
  I5  every acknowledged (2xx) payment/grade exists     (no lost acknowledged writes)
  I6  a grade batch is all-or-nothing                    (no partial batches), if expectations given
"""
from __future__ import annotations

import psycopg


def check(dsn: str, acked_payment_ids=(), acked_grade_keys=(), expected_batches: dict | None = None) -> dict:
    out: dict = {}
    with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchall()
        dup = q("SELECT invoice_id, count(*) FROM payment.payments WHERE status='COMPLETED' GROUP BY invoice_id HAVING count(*)>1")
        out["duplicate_payments"] = sum(n - 1 for _, n in dup)
        mism = q("""SELECT a.student_id, a.total_paid, COALESCE(p.s,0) FROM payment.accounts a
                    LEFT JOIN (SELECT student_id, SUM(amount) s FROM payment.payments WHERE status='COMPLETED' GROUP BY student_id) p
                    USING (student_id) WHERE a.total_paid <> COALESCE(p.s,0)
                    UNION ALL
                    SELECT p.student_id, 0, p.s FROM (SELECT student_id, SUM(amount) s FROM payment.payments WHERE status='COMPLETED' GROUP BY student_id) p
                    WHERE NOT EXISTS (SELECT 1 FROM payment.accounts a WHERE a.student_id=p.student_id)""")
        out["ledger_mismatch_students"] = len(mism)
        out["ledger_mismatch_amount"] = float(sum(abs(float(a) - float(b)) for _, a, b in mism))
        out["stuck_pending_payments"] = q("SELECT count(*) FROM payment.payments WHERE status='PENDING'")[0][0]
        out["rolled_back_payments"] = q("SELECT count(*) FROM payment.payments WHERE status='ROLLED_BACK'")[0][0]
        out["completed_payments"] = q("SELECT count(*) FROM payment.payments WHERE status='COMPLETED'")[0][0]
        dupa = q("SELECT count(*) - count(DISTINCT (student_id, course_code, term)) FROM academic.records")
        out["duplicate_academic_records"] = int(dupa[0][0])
        out["academic_records"] = q("SELECT count(*) FROM academic.records")[0][0]

        acked_p = list(acked_payment_ids)
        if acked_p:
            found = {r[0] for r in q("SELECT payment_id FROM payment.payments WHERE payment_id = ANY(%s) AND status='COMPLETED'", acked_p)}
            out["lost_acknowledged_payments"] = len(set(acked_p) - found)
        else:
            out["lost_acknowledged_payments"] = 0
        keys = list(acked_grade_keys)
        if keys:
            have = {f"{a}|{b}|{t}" for a, b, t in q("SELECT student_id, course_code, term FROM academic.records")}
            out["lost_acknowledged_grades"] = len(set(keys) - have)
        else:
            out["lost_acknowledged_grades"] = 0
        if expected_batches:
            counts = dict(q("SELECT batch_id, count(*) FROM academic.records WHERE batch_id IS NOT NULL GROUP BY batch_id"))
            partial = [b for b, n in expected_batches.items() if 0 < counts.get(b, 0) < n]
            overfull = [b for b, n in expected_batches.items() if counts.get(b, 0) > n]
            missing = [b for b, n in expected_batches.items() if counts.get(b, 0) == 0]
            out["partial_batches"] = len(partial)
            out["batches_with_duplicates"] = len(overfull)
            out["missing_batches"] = len(missing)
        else:
            out["partial_batches"] = out["batches_with_duplicates"] = out["missing_batches"] = 0
    violations = {k: out[k] for k in ("duplicate_payments", "ledger_mismatch_students", "stuck_pending_payments",
                                      "duplicate_academic_records", "lost_acknowledged_payments", "lost_acknowledged_grades",
                                      "partial_batches", "batches_with_duplicates") if out[k]}
    out["violations"] = violations
    out["consistent"] = not violations
    return out
