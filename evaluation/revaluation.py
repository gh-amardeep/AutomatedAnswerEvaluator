"""Import and group Google Forms re-evaluation requests.

This module uses only the Python standard library. It matches requests against
saved evaluation records and flags question/subpart groups with a concern rate
strictly greater than the configured threshold.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from typing import Any, Iterable

DEFAULT_THRESHOLD_PERCENT = 50.0

FIELD_ALIASES = {
    "roll_number": {
        "roll_number", "roll number", "student roll number", "roll no",
        "roll no.", "student roll no", "student roll no.",
    },
    "email": {
        "email", "gmail", "email address", "college gmail",
        "college email", "email id", "student email",
    },
    "question_id": {
        "question", "question number", "question no", "question id",
        "question number (e.g. q1)", "question number (e.g., q1)",
    },
    "subpart": {
        "subpart", "sub-part", "question subpart", "question part",
        "part", "question part/subpart", "sub part",
    },
    "reason": {
        "reason", "issue", "reason for re-evaluation",
        "reason for revaluation", "explain your concern",
        "please explain your concern", "describe your issue",
    },
    "assessment_id": {"assessment_id", "assessment id", "assessment"},
    "student_name": {"student_name", "student name", "name"},
}

def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))

def _canonical_headers(fieldnames: Iterable[str]) -> dict[str, str]:
    aliases = {
        field: {_norm_header(alias) for alias in values} | {_norm_header(field)}
        for field, values in FIELD_ALIASES.items()
    }
    result = {}
    for original in fieldnames:
        normalized = _norm_header(original)
        for canonical, options in aliases.items():
            if normalized in options and canonical not in result:
                result[canonical] = original
    return result

def _norm_id(value: Any) -> str:
    """Normalize IDs like 'Q1 (a)', 'q1a', and 'Q1(a)' for matching."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())

def _clean(value: Any) -> str:
    return str(value or "").strip()

def parse_revaluation_csv(
    file_bytes: bytes,
    *,
    default_assessment_id: str | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Parse Google Forms CSV using common heading aliases.

    Expected logical fields: roll number, question, subpart and reason.
    Email is strongly recommended but not mandatory to parse.
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["The CSV is empty or has no header row."]
    headers = _canonical_headers(reader.fieldnames)
    required = ("roll_number", "question_id", "reason")
    missing = [field for field in required if field not in headers]
    if missing:
        labels = {
            "roll_number": "student roll number",
            "question_id": "question number",
            "reason": "reason for re-evaluation",
        }
        return [], [
            "Could not identify required column(s): "
            + ", ".join(labels[item] for item in missing)
            + ". Rename the Google Form questions to clear labels or use the provided template."
        ]
    if "subpart" not in headers:
        # A blank subpart is valid when a whole question is being disputed.
        pass

    requests: list[dict[str, str]] = []
    errors: list[str] = []
    for line_no, row in enumerate(reader, start=2):
        if not any(_clean(v) for v in row.values()):
            continue
        get = lambda field: _clean(row.get(headers[field])) if field in headers else ""
        roll = get("roll_number")
        question = get("question_id")
        subpart = get("subpart")
        reason = get("reason")
        if not roll:
            errors.append(f"CSV row {line_no}: roll number is blank; row skipped.")
            continue
        if not question:
            errors.append(f"CSV row {line_no}: question number is blank; row skipped.")
            continue
        if not reason:
            errors.append(f"CSV row {line_no}: reason is blank; row skipped.")
            continue
        requests.append({
            "assessment_id": get("assessment_id") or _clean(default_assessment_id),
            "roll_number": roll,
            "student_name": get("student_name"),
            "email": get("email").lower(),
            "question_id": question,
            "subpart": subpart,
            "reason": reason,
            "source_row": str(line_no),
        })
    return requests, errors

def build_review_queue(
    requests: list[dict[str, str]],
    evaluations: list[dict[str, Any]],
    *,
    assessment_id: str,
    threshold_percent: float = DEFAULT_THRESHOLD_PERCENT,
) -> dict[str, Any]:
    """Match requests to evaluation records and aggregate by question/subpart.

    The concern-rate denominator is the number of unique students with a saved
    evaluation for the parent question in the selected assessment. If the
    assessment stores each subpart as its own question ID, the exact combined
    ID is preferred. Only unique roll numbers count toward the numerator.
    """
    selected_assessment = _clean(assessment_id)
    relevant = [
        dict(e) for e in evaluations
        if _clean(e.get("assessment_id")) == selected_assessment
    ]
    eval_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for evaluation in relevant:
        roll = _clean(evaluation.get("roll_number"))
        qid = _clean(evaluation.get("question_id"))
        eval_index[(roll, _norm_id(qid))].append(evaluation)

    # Student denominator per question identifier.
    evaluated_by_question: dict[str, set[str]] = defaultdict(set)
    for evaluation in relevant:
        evaluated_by_question[_norm_id(evaluation.get("question_id"))].add(
            _clean(evaluation.get("roll_number"))
        )

    matched_requests = []
    unmatched_requests = []
    seen_request_keys = set()
    for request in requests:
        request_assessment = _clean(request.get("assessment_id"))
        if request_assessment and request_assessment != selected_assessment:
            unmatched_requests.append({**request, "match_issue": "Assessment ID does not match selected assessment."})
            continue
        roll = _clean(request.get("roll_number"))
        question = _clean(request.get("question_id"))
        subpart = _clean(request.get("subpart"))
        # Prefer an exact combined ID, then the parent question ID.
        candidates = []
        combined = f"{question}{subpart}"
        candidates.extend(eval_index.get((roll, _norm_id(combined)), []))
        if not candidates:
            candidates.extend(eval_index.get((roll, _norm_id(question)), []))
        if not candidates:
            unmatched_requests.append({**request, "match_issue": "No saved evaluation matched this roll number and question."})
            continue
        # If multiple records share an ID, choose the newest one.
        evaluation = sorted(candidates, key=lambda e: int(e.get("id") or 0), reverse=True)[0]
        dedupe_key = (
            selected_assessment, roll, _norm_id(question), _norm_id(subpart),
            re.sub(r"\s+", " ", _clean(request.get("reason")).lower()),
        )
        duplicate = dedupe_key in seen_request_keys
        seen_request_keys.add(dedupe_key)
        matched_requests.append({
            **request,
            "assessment_id": selected_assessment,
            "evaluation_id": evaluation.get("id"),
            "evaluation_question_id": evaluation.get("question_id"),
            "ai_marks": evaluation.get("ai_marks"),
            "teacher_marks": evaluation.get("teacher_marks"),
            "max_marks": evaluation.get("max_marks"),
            "review_status": evaluation.get("review_status"),
            "answer_text": evaluation.get("answer_text", ""),
            "question_text": evaluation.get("question_text", ""),
            "duplicate": duplicate,
            "duplicate_key": "|".join(map(str, dedupe_key)),
        })

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for request in matched_requests:
        groups[(_clean(request.get("question_id")), _clean(request.get("subpart")))].append(request)

    queue = []
    for (question, subpart), group_requests in groups.items():
        question_norm = _norm_id(question)
        # Parent question is used for denominator unless the exact combined
        # question ID is present in the evaluation records.
        combined_norm = _norm_id(f"{question}{subpart}")
        exact_count = len(evaluated_by_question.get(combined_norm, set()))
        parent_count = len(evaluated_by_question.get(question_norm, set()))
        denominator = exact_count if exact_count else parent_count
        unique_rolls = {
            _clean(r.get("roll_number"))
            for r in group_requests if not r.get("duplicate")
        }
        numerator = len(unique_rolls)
        rate = (100.0 * numerator / denominator) if denominator else 0.0
        queue.append({
            "question_id": question,
            "subpart": subpart,
            "label": f"{question}{f'({subpart})' if subpart else ''}",
            "request_count": len(group_requests),
            "unique_student_count": numerator,
            "evaluated_student_count": denominator,
            "concern_rate_percent": round(rate, 2),
            "majority_flag": bool(denominator and rate > float(threshold_percent)),
            "requests": sorted(group_requests, key=lambda r: (_clean(r.get("roll_number")), int(r.get("source_row") or 0))),
        })
    queue.sort(key=lambda item: (-item["majority_flag"], -item["concern_rate_percent"], item["label"].lower()))
    return {
        "assessment_id": selected_assessment,
        "threshold_percent": float(threshold_percent),
        "matched_requests": matched_requests,
        "unmatched_requests": unmatched_requests,
        "queue": queue,
    }
