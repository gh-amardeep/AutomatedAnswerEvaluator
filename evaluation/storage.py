"""SQLite persistence and audit history for Automated Answer Evaluator.

Uses only Python's standard library. The original AI evaluation is immutable;
teacher decisions and revisions are stored separately.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = PROJECT_ROOT / "data" / "evaluations.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False)


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create persistence tables if they do not already exist."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id TEXT NOT NULL,
                assessment_title TEXT,
                subject TEXT,
                roll_number TEXT NOT NULL,
                student_name TEXT,
                question_id TEXT NOT NULL,
                question_text TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                max_marks REAL NOT NULL,
                ai_marks REAL NOT NULL,
                ai_result_json TEXT NOT NULL,
                assessment_json TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                teacher_marks REAL,
                teacher_note TEXT,
                teacher_approved INTEGER NOT NULL DEFAULT 0,
                review_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'Teacher',
                old_marks REAL,
                new_marks REAL,
                note TEXT,
                details_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_evaluations_assessment_roll
                ON evaluations(assessment_id, roll_number);
            CREATE INDEX IF NOT EXISTS idx_audit_evaluation_id
                ON audit_log(evaluation_id, id);
            """
        )


def save_ai_evaluation(
    *, assessment: Any, question: Any, answer: Any,
    student_name: str | None, result: Any,
) -> int:
    """Insert a new AI evaluation and its first audit event; return row ID."""
    init_db()
    timestamp = _now()
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO evaluations (
                assessment_id, assessment_title, subject, roll_number,
                student_name, question_id, question_text, answer_text,
                max_marks, ai_marks, ai_result_json, assessment_json,
                answer_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                assessment.assessment_id,
                assessment.title,
                assessment.subject,
                answer.roll_number,
                student_name or None,
                question.question_id,
                question.question_text,
                answer.answer_text,
                float(result.max_marks),
                float(result.marks_awarded),
                _json_dump(result),
                _json_dump(assessment),
                _json_dump(answer),
                timestamp,
                timestamp,
            ),
        )
        evaluation_id = int(cursor.lastrowid)
        conn.execute(
            """INSERT INTO audit_log
               (evaluation_id, action, actor, old_marks, new_marks, note,
                details_json, created_at)
               VALUES (?, 'AI_EVALUATION_SAVED', 'System', NULL, ?, ?, ?, ?)""",
            (
                evaluation_id,
                float(result.marks_awarded),
                "Initial AI evaluation saved",
                json.dumps({"max_marks": float(result.max_marks)}),
                timestamp,
            ),
        )
    return evaluation_id


def save_teacher_decision(
    *, evaluation_id: int, teacher_marks: float, teacher_note: str | None,
    actor: str = "Teacher", teacher_result: Any = None,
) -> dict[str, Any]:
    """Save or revise teacher marks and append an audit event atomically."""
    init_db()
    timestamp = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT ai_marks, max_marks, teacher_marks, teacher_approved "
            "FROM evaluations WHERE id = ?",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Evaluation record {evaluation_id} was not found.")

        maximum = float(row["max_marks"])
        marks = float(teacher_marks)
        if marks < 0 or marks > maximum:
            raise ValueError(f"Teacher marks must be between 0 and {maximum:g}.")

        previous_marks = row["teacher_marks"]
        previous_approved = bool(row["teacher_approved"])
        is_revision = previous_approved
        old_marks = float(previous_marks) if previous_marks is not None else float(row["ai_marks"])
        changed = abs(old_marks - marks) > 1e-6
        if changed and not (teacher_note or "").strip():
            raise ValueError("A review note is required when changing the marks.")

        status = "REVISED" if is_revision else (
            "APPROVED_WITH_OVERRIDE"
            if abs(marks - float(row["ai_marks"])) > 1e-6
            else "APPROVED"
        )
        conn.execute(
            """UPDATE evaluations
               SET teacher_marks = ?, teacher_note = ?, teacher_approved = 1,
                   review_status = ?, updated_at = ?
               WHERE id = ?""",
            (marks, (teacher_note or "").strip() or None, status, timestamp, evaluation_id),
        )
        action = "TEACHER_DECISION_REVISED" if is_revision else "TEACHER_DECISION_SAVED"
        conn.execute(
            """INSERT INTO audit_log
               (evaluation_id, action, actor, old_marks, new_marks, note,
                details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evaluation_id,
                action,
                (actor or "Teacher").strip(),
                old_marks,
                marks,
                (teacher_note or "").strip() or None,
                _json_dump(teacher_result) if teacher_result is not None else None,
                timestamp,
            ),
        )
    return {
        "evaluation_id": evaluation_id,
        "old_marks": old_marks,
        "teacher_marks": marks,
        "review_status": status,
        "action": action,
        "updated_at": timestamp,
    }


def list_evaluations(limit: int = 100) -> list[dict[str, Any]]:
    """Return recent evaluations for the in-app history view."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, assessment_id, assessment_title, subject,
                      roll_number, student_name, question_id, max_marks,
                      ai_marks, teacher_marks, teacher_approved, review_status,
                      created_at, updated_at
               FROM evaluations ORDER BY id DESC LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_audit_history(evaluation_id: int) -> list[dict[str, Any]]:
    """Return the audit history for one evaluation record."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, action, actor, old_marks, new_marks, note,
                      details_json, created_at
               FROM audit_log WHERE evaluation_id = ? ORDER BY id ASC""",
            (int(evaluation_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_evaluation(evaluation_id: int) -> dict[str, Any] | None:
    """Retrieve a persisted evaluation, including original AI JSON."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (int(evaluation_id),)
        ).fetchone()
    return dict(row) if row else None
