from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import csv
import io
import json
import streamlit as st

from evaluation.schemas import Assessment, Question, MarkingRubric, RubricCriterion, StudentAnswer
from evaluation.model_client import ModelClient
from evaluation.evaluator import AnswerEvaluator
from evaluation.flagging import collect_pre_evaluation_flags
from evaluation.revaluation import parse_revaluation_csv, build_review_queue
from evaluation.storage import (
    init_db,
    save_ai_evaluation,
    save_teacher_decision,
    list_evaluations,
    get_audit_history,
    get_evaluation,
    save_revaluation_requests,
    list_revaluation_requests,
    update_revaluation_request,
)

st.set_page_config(page_title="Automated Answer Evaluator", page_icon="📝", layout="wide")
init_db()

st.title("Automated Answer Script Evaluation System")
st.caption("Configure one examination paper, submit answers for one or many students, and review question-wise AI evaluations.")

# Helpers ---------------------------------------------------------------------
def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def make_template_csv(question_ids: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["roll_number", "student_name", *question_ids])
    writer.writerow(["101", "Example Student", *[f"Write the answer to {qid} here" for qid in question_ids]])
    return output.getvalue().encode("utf-8-sig")


def parse_submission_csv(uploaded_file, question_ids: list[str]) -> tuple[list[dict], list[str]]:
    """CSV format: one row per student; one answer column per question ID."""
    raw = uploaded_file.getvalue()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["The CSV file is empty or has no header row."]
    normalized = {name.strip().lower(): name for name in reader.fieldnames if name}
    if "roll_number" not in normalized:
        return [], ["CSV must contain a 'roll_number' column."]
    missing_qids = [qid for qid in question_ids if qid.lower() not in normalized]
    if missing_qids:
        return [], ["Missing answer column(s) for question ID(s): " + ", ".join(missing_qids)]

    rows, errors = [], []
    seen_rolls = set()
    for line_no, raw_row in enumerate(reader, start=2):
        row = {str(k).strip().lower(): (v or "").strip() for k, v in raw_row.items() if k}
        roll = row.get("roll_number", "").strip()
        name = row.get("student_name", "").strip()
        if not any(row.values()):
            continue
        if not roll:
            errors.append(f"CSV row {line_no}: roll_number is blank; row skipped.")
            continue
        if roll in seen_rolls:
            errors.append(f"CSV row {line_no}: duplicate roll number '{roll}' in this file; row skipped.")
            continue
        seen_rolls.add(roll)
        answers = {qid: row.get(qid.lower(), "").strip() for qid in question_ids}
        if not any(answers.values()):
            errors.append(f"CSV row {line_no}: student {roll} has no answers; row skipped.")
            continue
        rows.append({"roll_number": roll, "student_name": name or None, "answers": answers, "source_row": line_no})
    return rows, errors


def validate_rubric(question_id: str, question_text: str, max_marks: float, criteria: list[RubricCriterion], approved_by: str, alternatives: str, instructions: str, assessment_id: str) -> MarkingRubric:
    if not question_id.strip():
        raise ValueError("Every question needs a question ID.")
    if not question_text.strip():
        raise ValueError(f"Enter the question text for {question_id}.")
    if not criteria:
        raise ValueError(f"Add at least one criterion for {question_id}.")
    if any(not c.criterion_id.strip() or not c.description.strip() for c in criteria):
        raise ValueError(f"Every criterion for {question_id} needs an ID and description.")
    ids = [c.criterion_id for c in criteria]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Criterion IDs must be unique within {question_id}.")
    total = sum(float(c.max_marks) for c in criteria)
    if abs(total - float(max_marks)) > 1e-6:
        raise ValueError(f"Rubric for {question_id} totals {total:g}, but question maximum is {max_marks:g}.")
    return MarkingRubric(
        rubric_id=f"{assessment_id}-{question_id}-R1",
        question_id=question_id,
        criteria=criteria,
        accepted_alternatives=split_lines(alternatives),
        evaluation_instructions=instructions.strip() or None,
        approved_by=approved_by.strip() or None,
        approved=True,
    )

# 1. Assessment / common question paper ---------------------------------------
st.header("1. Examination paper and approved rubrics")
left, right = st.columns(2)
with left:
    assessment_id = st.text_input("Assessment ID", "ASSESSMENT-001")
    assessment_title = st.text_input("Examination title", "Sample Assessment")
    subject = st.text_input("Subject (optional)", placeholder="e.g. Machine Learning, Control Systems")
with right:
    question_count = int(st.number_input("Number of questions in this paper", min_value=1, max_value=20, value=2, step=1))
    approved_by = st.text_input("Teacher / approver name")
subject_instructions = st.text_area("General subject-level instructions (optional)", placeholder="Instructions that apply to all questions.")

question_configs = []
question_ids_seen = []
for qi in range(question_count):
    with st.expander(f"Question {qi + 1} and marking rubric", expanded=(qi == 0)):
        qcol1, qcol2, qcol3 = st.columns([1, 3, 1])
        with qcol1:
            qid = st.text_input("Question ID", value=f"Q{qi + 1}", key=f"paper_qid_{qi}")
        with qcol2:
            qtext = st.text_area("Question text", key=f"paper_qtext_{qi}", height=100)
        with qcol3:
            qmax = float(st.number_input("Maximum marks", min_value=0.5, value=5.0, step=0.5, key=f"paper_qmax_{qi}"))
        criterion_count = int(st.number_input("Number of rubric criteria", min_value=1, max_value=10, value=2, step=1, key=f"paper_criterion_count_{qi}"))
        qcriteria = []
        ccols = st.columns(2)
        for ci in range(criterion_count):
            with st.container(border=True):
                st.markdown(f"**Criterion {ci + 1}**")
                c1, c2 = st.columns([1, 2])
                with c1:
                    cid = st.text_input("Criterion ID", value=f"C{ci + 1}", key=f"paper_cid_{qi}_{ci}")
                    cmax = float(st.number_input("Criterion maximum", min_value=0.0, value=qmax / criterion_count, step=0.5, key=f"paper_cmax_{qi}_{ci}"))
                with c2:
                    cdesc = st.text_input("What earns marks?", key=f"paper_cdesc_{qi}_{ci}")
                    cevidence = st.text_area("Expected evidence (one item per line)", key=f"paper_cevidence_{qi}_{ci}", height=75)
                qcriteria.append(RubricCriterion(criterion_id=cid, description=cdesc, max_marks=cmax, expected_evidence=split_lines(cevidence)))
        alternatives = st.text_area("Accepted alternative approaches / answers (one per line)", key=f"paper_alternatives_{qi}")
        rubric_instructions = st.text_area("Question-specific marking instructions (optional)", key=f"paper_instructions_{qi}")
        question_ids_seen.append(qid.strip())
        question_configs.append({"question_id": qid.strip(), "question_text": qtext.strip(), "max_marks": qmax, "criteria": qcriteria, "alternatives": alternatives, "instructions": rubric_instructions})

rubric_approved = st.checkbox("I confirm that every question and its marking rubric have been reviewed and approved by the teacher.")

# 2. Student submissions -------------------------------------------------------
st.header("2. Student submissions")
st.write("Use the same workflow for one student or a whole class. For multiple students, upload one CSV: one row per student and one answer column per question ID.")
submission_mode = st.radio("How would you like to provide answers?", ["Enter one student's answers", "Upload answers for multiple students (CSV)"], horizontal=True, key="submission_mode")

single_name = ""
single_roll = ""
single_answers = {}
uploaded_file = None
csv_preview_rows = []
csv_parse_errors = []

if submission_mode == "Enter one student's answers":
    s1, s2 = st.columns(2)
    with s1:
        single_roll = st.text_input("Student roll number", key="single_roll")
    with s2:
        single_name = st.text_input("Student name (optional)", key="single_name")
    for cfg in question_configs:
        with st.expander(f"Answer for {cfg['question_id'] or 'question'}", expanded=True):
            single_answers[cfg["question_id"]] = st.text_area("Student answer", key=f"single_answer_{cfg['question_id']}", height=140, placeholder="Enter this student's answer.")
else:
    st.download_button("Download CSV template for this question paper", data=make_template_csv([q["question_id"] for q in question_configs if q["question_id"]]), file_name="student_answers_template.csv", mime="text/csv")
    uploaded_file = st.file_uploader("Upload student answers CSV", type=["csv"], key="multi_student_csv")
    if uploaded_file is not None:
        csv_preview_rows, csv_parse_errors = parse_submission_csv(uploaded_file, [q["question_id"] for q in question_configs if q["question_id"]])
        for issue in csv_parse_errors:
            st.warning(issue)
        if csv_preview_rows:
            preview = []
            for row in csv_preview_rows:
                preview.append({"Roll number": row["roll_number"], "Student": row["student_name"] or "—", "Answers provided": sum(bool(a.strip()) for a in row["answers"].values()), "Questions in paper": len(question_configs)})
            st.dataframe(preview, use_container_width=True, hide_index=True)
            st.info(f"Ready to process {len(csv_preview_rows)} student submission(s). Each non-empty answer will receive its own evaluation record.")

# 3. Validate, build common assessment, evaluate all submissions -------------
st.header("3. Evaluate submissions")
if st.button("Evaluate submitted answer script(s)", type="primary", use_container_width=True):
    try:
        if not assessment_id.strip():
            raise ValueError("Enter an assessment ID.")
        if not assessment_title.strip():
            raise ValueError("Enter an examination title.")
        if not approved_by.strip():
            raise ValueError("Enter the teacher / approver name.")
        if len(question_ids_seen) != len(set(question_ids_seen)) or any(not qid for qid in question_ids_seen):
            raise ValueError("Question IDs must be non-empty and unique.")
        if not rubric_approved:
            raise ValueError("Evaluation is blocked until the teacher approves the complete paper and all rubrics.")

        built_questions = []
        for cfg in question_configs:
            rubric = validate_rubric(cfg["question_id"], cfg["question_text"], cfg["max_marks"], cfg["criteria"], approved_by, cfg["alternatives"], cfg["instructions"], assessment_id)
            built_questions.append(Question(question_id=cfg["question_id"], question_text=cfg["question_text"], max_marks=cfg["max_marks"], rubric=rubric, evaluation_instructions=cfg["instructions"].strip() or None))
        paper_total = sum(float(q.max_marks) for q in built_questions)
        assessment = Assessment(assessment_id=assessment_id, title=assessment_title, subject=subject.strip() or None, subject_instructions=subject_instructions.strip() or None, questions=built_questions, total_max_marks=paper_total)

        if submission_mode == "Enter one student's answers":
            if not single_roll.strip():
                raise ValueError("Enter the student's roll number.")
            if not any(text.strip() for text in single_answers.values()):
                raise ValueError("Enter at least one answer for this student.")
            submissions = [{"roll_number": single_roll.strip(), "student_name": single_name.strip() or None, "answers": {qid: text.strip() for qid, text in single_answers.items()}, "source_row": None}]
        else:
            if uploaded_file is None:
                raise ValueError("Upload a CSV file first.")
            submissions, csv_errors = parse_submission_csv(uploaded_file, [q.question_id for q in built_questions])
            if not submissions:
                raise ValueError("No valid student rows were found in the CSV. Check the template and required columns.")
            for issue in csv_errors:
                st.warning(issue)

        evaluator = AnswerEvaluator(ModelClient())
        total_expected = sum(sum(bool(sub["answers"].get(q.question_id, "").strip()) for q in built_questions) for sub in submissions)
        completed = 0
        saved_results = []
        errors = []
        progress = st.progress(0, text="Preparing evaluations...")
        status = st.empty()
        for student_index, submission in enumerate(submissions, start=1):
            for question in built_questions:
                answer_text = submission["answers"].get(question.question_id, "").strip()
                if not answer_text:
                    continue
                answer = StudentAnswer(answer_id=f"{assessment_id}-{submission['roll_number']}-{question.question_id}", roll_number=submission["roll_number"], assessment_id=assessment_id, question_id=question.question_id, answer_text=answer_text, source_type="typed")
                try:
                    pre_flags = collect_pre_evaluation_flags(assessment , [answer])
                    with st.spinner(f"Evaluating student {student_index}/{len(submissions)} — {submission['roll_number']} — {question.question_id}..."):
                        result = evaluator.evaluate(assessment, question, answer)
                    record_id = save_ai_evaluation(assessment=assessment, question=question, answer=answer, student_name=submission["student_name"], result=result, pre_flags=pre_flags)
                    saved_results.append({
                        "record_id": record_id,
                        "roll_number": submission["roll_number"],
                        "student_name": submission["student_name"] or "—",
                        "question_id": question.question_id,
                        "ai_marks": float(result.marks_awarded),
                        "max_marks": float(result.max_marks),
                        "flags": len(result.flags) + len(pre_flags),
                        "flag_details": [
                            f"{flag.code.value}: {flag.reason}" for flag in pre_flags
                        ] + [
                            f"{flag.code.value}: {flag.reason}" for flag in result.flags
                        ],
                        "status": "AUTO_EVALUATED",
                    })
                except Exception as exc:
                    errors.append({"roll_number": submission["roll_number"], "question_id": question.question_id, "error": str(exc)})
                completed += 1
                progress.progress(min(completed / max(total_expected, 1), 1.0), text=f"Processed {completed}/{total_expected} non-empty answer(s)")
                status.write(f"Student {student_index}/{len(submissions)} · {submission['roll_number']} · {question.question_id}")
        progress.progress(1.0, text="Evaluation run finished.")
        st.session_state["last_batch_results"] = saved_results
        st.session_state["last_batch_errors"] = errors
        st.session_state["last_batch_assessment_id"] = assessment_id
        st.session_state["last_batch_title"] = assessment_title
        st.session_state["last_batch_student_count"] = len(submissions)
        st.session_state["last_batch_answer_count"] = total_expected
        if saved_results:
            st.success(f"Automatically evaluated and saved {len(saved_results)} answer(s) for {len(submissions)} student submission(s). No individual teacher approval is required. Review flagged answers or disputes when needed.")
        if errors:
            st.warning(f"{len(errors)} answer evaluation(s) failed. Successful records were retained; failed rows are listed below.")
    except ValueError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error("Could not process the submissions. Check the error details below.")
        st.exception(exc)

# 4. Most recent run results ---------------------------------------------------
if st.session_state.get("last_batch_results") is not None:
    st.header("4. Results from the latest evaluation run")
    st.caption("Each row is one student's answer to one question. A student with four answered questions will normally have four records.")
    results = st.session_state["last_batch_results"]
    if results:
        display_results = [
            {key: value for key, value in row.items() if key != "flag_details"}
            for row in results
        ]
        st.dataframe(display_results, use_container_width=True, hide_index=True)
        flagged_results = [row for row in results if row.get("flag_details")]
        if flagged_results:
            with st.expander("Flag details from the latest run", expanded=False):
                for row in flagged_results:
                    st.markdown(
                        f"**Student {row['roll_number']} — {row['question_id']} "
                        f"(record #{row['record_id']})**"
                    )
                    for detail in row["flag_details"]:
                        st.warning(detail)
        csv_rows = [
            {key: ("; ".join(value) if isinstance(value, list) else value)
             for key, value in row.items()}
            for row in results
        ]
        csv_out = io.StringIO()
        writer = csv.DictWriter(csv_out, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)
        st.download_button("Download latest evaluation results (CSV)", data=csv_out.getvalue().encode("utf-8-sig"), file_name="evaluation_results.csv", mime="text/csv")
    errors = st.session_state.get("last_batch_errors", [])
    if errors:
        st.subheader("Evaluation errors")
        st.dataframe(errors, use_container_width=True, hide_index=True)

# 5. Persistent history --------------------------------------------------------
st.divider()
st.header("5. Persistent evaluation history")
st.caption("One database record is stored per student-question pair. AI marks are saved automatically; teacher decisions and revisions are separate and optional.")
try:
    records = list_evaluations(limit=1000)
    if records:
        history_table = []
        for record in records:
            history_table.append({"Record ID": record["id"], "Assessment": record["assessment_id"], "Student": record.get("student_name") or "—", "Roll number": record["roll_number"], "Question": record["question_id"], "AI marks": f"{record['ai_marks']:g} / {record['max_marks']:g}", "Teacher marks": f"{record['teacher_marks']:g} / {record['max_marks']:g}" if record["teacher_marks"] is not None else "Not reviewed (AI marks apply)", "Status": record["review_status"], "Updated (UTC)": record["updated_at"]})
        st.dataframe(history_table, use_container_width=True, hide_index=True)
        record_options = [int(r["id"]) for r in records]
        selected_id = st.selectbox("Select a saved answer evaluation to review", options=record_options, format_func=lambda rid: next(f"#{r['id']} — {r['assessment_id']} / {r.get('student_name') or r['roll_number']} / {r['question_id']} ({r['review_status']})" for r in records if int(r["id"]) == int(rid)), key="review_record_selector")
        selected = get_evaluation(int(selected_id))
        if selected:
            with st.expander("Selected evaluation details and teacher review", expanded=False):
                st.write(f"**Assessment:** {selected['assessment_id']} — {selected.get('assessment_title') or ''}")
                st.write(f"**Student:** {selected.get('student_name') or '—'} · **Roll number:** {selected['roll_number']}")
                st.write(f"**Question:** {selected['question_id']} — {selected['question_text']}")
                st.write("**Student answer**")
                st.text_area("Saved answer (read-only)", value=selected["answer_text"], disabled=True, key=f"saved_answer_{selected_id}", height=120)
                try:
                    ai_json = json.loads(selected["ai_result_json"])
                    st.metric("Original AI marks", f"{selected['ai_marks']:g} / {selected['max_marks']:g}")
                    st.write("**AI rationale:**", ai_json.get("rationale", "No rationale saved."))
                    st.write("**AI feedback:**", ai_json.get("student_feedback", "No feedback saved."))

                    st.write("**Marking rationale:**")
                    st.write(ai_json.get("rationale", "No rationale saved."))

                    st.write("**Correct points:**")
                    correct_points = ai_json.get("correct_points") or []
                    if correct_points:
                        for point in correct_points:
                            st.write(f"- {point}")
                    else:
                        st.write("No correct points were listed.")

                    st.write("**Missing or incorrect points:**")
                    missing_points = ai_json.get("missing_or_incorrect_points") or []
                    if missing_points:
                        for point in missing_points:
                            st.write(f"- {point}")
                    else:
                        st.write("No missing or incorrect points were listed.")

                    st.write("**What is required for full marks:**")
                    full_requirements = ai_json.get("full_marks_requirements") or []
                    if full_requirements:
                        for item in full_requirements:
                            st.write(f"- {item}")
                    else:
                        st.write("No additional requirements were listed.")

                    st.write("**Criterion-wise marks:**")
                    criterion_results = ai_json.get("criterion_results") or []
                    if criterion_results:
                        for item in criterion_results:
                            st.write(
                                f"**{item.get('criterion_id', 'Criterion')}:** "
                                f"{item.get('marks_awarded', 0):g} marks — "
                                f"{item.get('rationale', '')}"
                            )
                    else:
                        st.write("No criterion-wise details were saved.")

                    if ai_json.get("flags"):
                        st.write("**AI review flags:**")
                        for flag in ai_json["flags"]:
                            code = flag.get("code", "REVIEW")
                            reason = flag.get("reason", "")
                            severity = flag.get("severity", "WARNING")
                            st.warning(f"{severity} — {code}: {reason}")
                except (TypeError, ValueError):
                    st.warning("Could not decode the saved AI result details.")
                current_teacher_marks = selected.get("teacher_marks")
                if current_teacher_marks is None:
                    current_teacher_marks = float(selected["ai_marks"])
                st.write(f"**Current effective marks:** {float(current_teacher_marks):g} / {float(selected['max_marks']):g}")
                st.write(f"**Review status:** {selected['review_status']}")
                if not selected.get("teacher_approved"):
                    st.info("These marks are the saved AI evaluation. Teacher approval is optional; use this form only if you want to review or change the result.")
                if selected.get("teacher_note"):
                    st.write(f"**Latest teacher note:** {selected['teacher_note']}")
                with st.form(f"teacher_review_form_{selected_id}"):
                    teacher_name = st.text_input("Teacher name", value=approved_by.strip() or "Teacher", key=f"teacher_name_{selected_id}")
                    teacher_marks = st.number_input("Teacher marks", min_value=0.0, max_value=float(selected["max_marks"]), value=float(current_teacher_marks), step=0.5, key=f"teacher_marks_{selected_id}")
                    teacher_note = st.text_area("Review note (required when changing marks)", value=selected.get("teacher_note") or "", key=f"teacher_note_{selected_id}")
                    save_decision = st.form_submit_button("Save optional teacher review / revision")
                if save_decision:
                    changed = abs(float(teacher_marks) - float(current_teacher_marks)) > 1e-6
                    if changed and not teacher_note.strip():
                        st.error("Enter a reason when changing the marks.")
                    else:
                        try:
                            event = save_teacher_decision(evaluation_id=int(selected_id), teacher_marks=float(teacher_marks), teacher_note=teacher_note.strip() or None, actor=teacher_name.strip() or "Teacher", teacher_result=None)
                            st.success(f"Saved. {event['action']}: {event['old_marks']:g} → {event['teacher_marks']:g}. Audit history updated.")
                            st.rerun()
                        except (ValueError, RuntimeError) as exc:
                            st.error(f"Could not save teacher decision: {exc}")
                with st.expander("Audit history", expanded=False):
                    events = get_audit_history(int(selected_id))
                    if not events:
                        st.info("No audit events found.")
                    for event in events:
                        st.markdown(f"**{event['action']}** · {event['created_at']}  \nActor: {event['actor']} · Marks: {event['old_marks'] if event['old_marks'] is not None else '—'} → {event['new_marks'] if event['new_marks'] is not None else '—'}")
                        if event.get("note"):
                            st.write(f"Note: {event['note']}")
    else:
        st.info("No evaluations have been saved yet. Configure a paper and submit student answers above.")
except Exception as exc:
    st.error("Could not load persistent evaluation history.")
    st.exception(exc)

# 6. Flagged-answer teacher review queue ----------------------------------------
st.divider()
st.header("6. Teacher review queue — flagged answers")
st.caption(
    "Only answers with saved AI or pre-evaluation flags are listed here. "
    "AI marks remain saved automatically; this queue is for optional teacher attention."
)

try:
    queue_records = list_evaluations(limit=1000)
    flagged_items = []
    for item in queue_records:
        full_record = get_evaluation(int(item["id"]))
        if not full_record:
            continue
        try:
            ai_payload = json.loads(full_record.get("ai_result_json") or "{}")
        except (TypeError, ValueError):
            ai_payload = {}
        try:
            pre_payload = json.loads(full_record.get("pre_flags_json") or "[]")
        except (TypeError, ValueError):
            pre_payload = []

        combined_flags = []
        for flag in pre_payload if isinstance(pre_payload, list) else []:
            if isinstance(flag, dict):
                combined_flags.append({
                    "source": "Pre-evaluation",
                    "code": flag.get("code", "PRE_EVALUATION_FLAG"),
                    "reason": flag.get("reason", str(flag)),
                    "severity": flag.get("severity", "WARNING"),
                })
            else:
                combined_flags.append({
                    "source": "Pre-evaluation",
                    "code": getattr(getattr(flag, "code", None), "value", "PRE_EVALUATION_FLAG"),
                    "reason": getattr(flag, "reason", str(flag)),
                    "severity": getattr(getattr(flag, "severity", None), "value", "WARNING"),
                })

        ai_flags = ai_payload.get("flags") or []
        if isinstance(ai_flags, list):
            for flag in ai_flags:
                if isinstance(flag, dict):
                    combined_flags.append({
                        "source": "AI evaluation",
                        "code": flag.get("code", "AI_REVIEW_FLAG"),
                        "reason": flag.get("reason", str(flag)),
                        "severity": flag.get("severity", "WARNING"),
                    })

        if combined_flags:
            flagged_items.append((full_record, ai_payload, combined_flags))

    if not flagged_items:
        st.success("No saved flagged answers found. Answers without flags do not require routine teacher review.")
    else:
        st.warning(f"{len(flagged_items)} answer(s) need attention. This is a review suggestion, not proof that the AI marks are incorrect.")
        queue_table = []
        for record, ai_payload, flags in flagged_items:
            queue_table.append({
                "Record ID": record["id"],
                "Assessment": record["assessment_id"],
                "Roll number": record["roll_number"],
                "Student": record.get("student_name") or "—",
                "Question": record["question_id"],
                "AI marks": f"{float(record['ai_marks']):g} / {float(record['max_marks']):g}",
                "Flag count": len(flags),
                "Flag reasons": "; ".join(
                    f"{f['code']}: {f['reason']}" for f in flags
                ),
                "Status": record["review_status"],
            })
        st.dataframe(queue_table, use_container_width=True, hide_index=True)

        flagged_ids = [int(record["id"]) for record, _, _ in flagged_items]
        selected_flagged_id = st.selectbox(
            "Choose a flagged answer to inspect",
            options=flagged_ids,
            format_func=lambda rid: next(
                f"#{record['id']} — {record['assessment_id']} / roll {record['roll_number']} / "
                f"{record['question_id']} — {float(record['ai_marks']):g}/{float(record['max_marks']):g}"
                for record, _, _ in flagged_items if int(record["id"]) == int(rid)
            ),
            key="flagged_answer_selector",
        )
        chosen = next(
            (record, ai_payload, flags)
            for record, ai_payload, flags in flagged_items
            if int(record["id"]) == int(selected_flagged_id)
        )
        chosen_record, chosen_ai, chosen_flags = chosen
        with st.expander("Flag details, answer and rubric", expanded=True):
            st.write(f"**Student:** {chosen_record.get('student_name') or '—'} · **Roll number:** {chosen_record['roll_number']}")
            st.write(f"**Question ID:** {chosen_record['question_id']}")
            st.write(f"**Question:** {chosen_record['question_text']}")
            st.write(f"**AI marks:** {float(chosen_record['ai_marks']):g} / {float(chosen_record['max_marks']):g}")
            st.write("**Student answer:**")
            st.text_area(
                "Flagged student answer (read-only)",
                value=chosen_record.get("answer_text") or "",
                disabled=True,
                key=f"flagged_answer_text_{selected_flagged_id}",
                height=160,
            )
            st.write("**Why this answer was flagged:**")
            for flag in chosen_flags:
                st.warning(f"{flag['source']} · {flag['severity']} · {flag['code']}: {flag['reason']}")
            st.write("**AI rationale:**", chosen_ai.get("rationale", "No rationale saved."))
            st.write("**AI feedback:**", chosen_ai.get("student_feedback", "No feedback saved."))
            try:
                saved_assessment = json.loads(chosen_record.get("assessment_json") or "{}")
                saved_questions = saved_assessment.get("questions") or []
                saved_question = next(
                    (q for q in saved_questions if str(q.get("question_id", "")).strip() == str(chosen_record["question_id"]).strip()),
                    None,
                )
                if saved_question:
                    st.write("**Saved rubric:**")
                    st.json(saved_question.get("rubric") or {})
            except (TypeError, ValueError):
                st.info("Saved rubric details are unavailable.")

            st.info("If you decide to change the marks, use the existing optional teacher review form in Persistent evaluation history. A reason is required when marks change.")

except Exception as exc:
    st.error("Could not build the flagged-answer review queue.")
    st.exception(exc)

# 7. Google Forms re-evaluation request import ---------------------------------

st.divider()
st.header("7. Re-evaluation review queue")
st.caption(
    "Import the CSV exported from Google Forms. Requests are matched to saved "
    "evaluations, grouped by question/subpart, and flagged when more than the "
    "configured percentage of evaluated students raise a concern."
)

all_history = list_evaluations(limit=1000)
assessment_options = sorted({
    str(row.get("assessment_id", "")).strip()
    for row in all_history if str(row.get("assessment_id", "")).strip()
})
if not assessment_options:
    st.info("Evaluate and save at least one assessment before importing re-evaluation requests.")
else:
    selected_assessment_for_requests = st.selectbox(
        "Assessment for these requests",
        options=assessment_options,
        key="reval_assessment_select",
    )
    threshold = float(st.number_input(
        "Majority concern flag threshold (%)",
        min_value=0.0, max_value=100.0, value=50.0, step=5.0,
        help="A question/subpart is flagged only when its unique-student concern rate is strictly greater than this threshold.",
        key="reval_threshold",
    ))
    st.write(
        "Expected Google Form fields: student roll number, college email, "
        "question number, subpart (optional), and reason for re-evaluation. "
        "Header labels can vary; common Google Forms headings are recognized."
    )
    request_csv = st.file_uploader(
        "Upload Google Forms responses CSV",
        type=["csv"],
        key="revaluation_requests_csv",
    )
    if request_csv is not None:
        parsed_requests, parse_issues = parse_revaluation_csv(
            request_csv.getvalue(),
            default_assessment_id=selected_assessment_for_requests,
        )
        for issue in parse_issues:
            st.warning(issue)
        if parsed_requests:
            existing_evaluations = [
                row for row in all_history
                if str(row.get("assessment_id", "")).strip() == selected_assessment_for_requests
            ]
            queue_preview = build_review_queue(
                parsed_requests,
                existing_evaluations,
                assessment_id=selected_assessment_for_requests,
                threshold_percent=threshold,
            )
            st.write(
                f"Parsed **{len(parsed_requests)}** request(s): "
                f"**{len(queue_preview['matched_requests'])}** matched and "
                f"**{len(queue_preview['unmatched_requests'])}** unmatched."
            )
            if queue_preview["queue"]:
                st.subheader("Preview: grouped question/subpart queue")
                st.dataframe(
                    [{
                        "Question / subpart": group["label"],
                        "Unique requesting students": group["unique_student_count"],
                        "Requests": group["request_count"],
                        "Evaluated students": group["evaluated_student_count"],
                        "Concern rate (%)": group["concern_rate_percent"],
                        "Majority flag": "FLAGGED" if group["majority_flag"] else "Normal",
                    } for group in queue_preview["queue"]],
                    use_container_width=True,
                    hide_index=True,
                )
                flagged_groups = [g for g in queue_preview["queue"] if g["majority_flag"]]
                if flagged_groups:
                    st.warning(
                        "Majority concern flag(s): "
                        + ", ".join(f"{g['label']} ({g['concern_rate_percent']}%)" for g in flagged_groups)
                    )
                else:
                    st.success("No question/subpart exceeds the current majority concern threshold.")
            if queue_preview["unmatched_requests"]:
                with st.expander("Unmatched requests — require correction", expanded=True):
                    st.dataframe(
                        [{
                            "CSV row": item.get("source_row"),
                            "Roll number": item.get("roll_number"),
                            "Question": item.get("question_id"),
                            "Subpart": item.get("subpart"),
                            "Issue": item.get("match_issue"),
                        } for item in queue_preview["unmatched_requests"]],
                        use_container_width=True,
                        hide_index=True,
                    )
            if st.button("Import matched requests and create review queue", type="primary", key="import_reval_requests"):
                saved = save_revaluation_requests(
                    assessment_id=selected_assessment_for_requests,
                    requests=queue_preview["matched_requests"],
                )
                st.success(
                    f"Imported {saved['inserted']} new request(s). "
                    f"Skipped {saved['duplicates']} duplicate(s); "
                    f"{saved['unmatched']} unmatched request(s) were not saved."
                )
                st.rerun()

    st.subheader("Saved re-evaluation requests")
    saved_requests = list_revaluation_requests(selected_assessment_for_requests)
    if not saved_requests:
        st.info("No re-evaluation requests have been imported for this assessment yet.")
    else:
        # Rebuild the grouped queue from persistent requests so it remains
        # available after a rerun or a later visit to the app.
        persisted_request_rows = [{
            "assessment_id": r.get("assessment_id") or selected_assessment_for_requests,
            "roll_number": str(r.get("roll_number") or ""),
            "student_name": str(r.get("student_name") or ""),
            "email": str(r.get("student_email") or ""),
            "question_id": str(r.get("question_id") or ""),
            "subpart": str(r.get("subpart") or ""),
            "reason": str(r.get("reason") or ""),
            "source_row": str(r.get("source_row") or "0"),
        } for r in saved_requests]
        persistent_queue = build_review_queue(
            persisted_request_rows,
            [row for row in all_history if str(row.get("assessment_id", "")).strip() == selected_assessment_for_requests],
            assessment_id=selected_assessment_for_requests,
            threshold_percent=threshold,
        )
        st.subheader("Grouped review queue")
        st.dataframe([{
            "Question / subpart": group["label"],
            "Unique requesting students": group["unique_student_count"],
            "Requests": group["request_count"],
            "Evaluated students": group["evaluated_student_count"],
            "Concern rate (%)": group["concern_rate_percent"],
            "Majority flag": "FLAGGED" if group["majority_flag"] else "Normal",
        } for group in persistent_queue["queue"]], use_container_width=True, hide_index=True)
        persistent_flags = [group for group in persistent_queue["queue"] if group["majority_flag"]]
        if persistent_flags:
            st.warning("Majority concern flag(s): " + ", ".join(
                f"{group['label']} ({group['concern_rate_percent']}%)" for group in persistent_flags
            ))
        else:
            st.success("No saved question/subpart group exceeds the current majority concern threshold.")
        def format_question_label(row):
            question = str(row.get("question_id", ""))
            subpart = str(row.get("subpart") or "").strip()
            return f"{question}({subpart})" if subpart else question
        st.dataframe(
            [{
                "Request ID": r["id"],
                "Question / subpart": format_question_label(r),
                "Roll number": r["roll_number"],
                "Email": r.get("student_email") or "—",
                "Status": r["status"],
                "AI marks": f"{float(r['ai_marks']):g} / {float(r['max_marks']):g}" if r.get("ai_marks") is not None else "Unmatched",
            } for r in saved_requests],
            use_container_width=True,
            hide_index=True,
        )
        request_ids = [int(r["id"]) for r in saved_requests]
        selected_request_id = st.selectbox(
            "Select a request to inspect",
            options=request_ids,
            format_func=lambda rid: next(
                f"#{r['id']} — {format_question_label(r)} — {r['roll_number']} — {r['status']}"
                for r in saved_requests if int(r["id"]) == int(rid)
            ),
            key="selected_revaluation_request",
        )
        selected_request = next(r for r in saved_requests if int(r["id"]) == int(selected_request_id))
        with st.expander("Request details and original evaluation", expanded=True):
            st.write(f"**Student:** {selected_request.get('student_name') or '—'} · **Roll number:** {selected_request['roll_number']}")
            st.write(f"**Email:** {selected_request.get('student_email') or '—'}")
            st.write(f"**Question / subpart:** {format_question_label(selected_request)}")
            st.write("**Student's concern:**")
            st.write(selected_request["reason"])
            st.write("**Original question:**")
            st.write(selected_request.get("question_text") or "Question text unavailable.")
            st.write("**Original answer:**")
            st.text_area(
                "Saved student answer (read-only)",
                value=selected_request.get("answer_text") or "",
                disabled=True,
                key=f"reval_answer_{selected_request_id}",
                height=140,
            )
            if selected_request.get("evaluation_id"):
                linked_eval = get_evaluation(int(selected_request["evaluation_id"]))
                if linked_eval:
                    try:
                        ai_data = json.loads(linked_eval.get("ai_result_json") or "{}")
                        st.metric("Original AI marks", f"{float(linked_eval['ai_marks']):g} / {float(linked_eval['max_marks']):g}")
                        st.write("**AI rationale:**", ai_data.get("rationale", "No rationale saved."))
                        st.write("**AI feedback:**", ai_data.get("student_feedback", "No feedback saved."))
                        st.write("**Rubric / assessment context:**")
                        assessment_data = json.loads(linked_eval.get("assessment_json") or "{}")
                        questions = assessment_data.get("questions") or []
                        matching_question = next(
                            (q for q in questions if str(q.get("question_id", "")).strip() == str(linked_eval.get("question_id", "")).strip()),
                            None,
                        )
                        if matching_question:
                            st.json(matching_question.get("rubric") or {})
                        else:
                            st.info("Rubric details could not be located in the saved assessment JSON.")
                    except (TypeError, ValueError):
                        st.warning("Could not decode the saved evaluation/rubric details.")
            st.divider()
            current_status = selected_request["status"]
            status_options = ["SUBMITTED", "UNDER_REVIEW", "RESOLVED_UPHELD", "RESOLVED_REVISED"]
            status_index = status_options.index(current_status) if current_status in status_options else 0
            with st.form(f"reval_decision_form_{selected_request_id}"):
                request_status = st.selectbox("Request status", options=status_options, index=status_index)
                teacher_decision = st.text_input(
                    "Decision summary",
                    value=selected_request.get("teacher_decision") or "",
                    help="For example: Original marks upheld, or marks revised through the teacher review workflow.",
                )
                teacher_note = st.text_area(
                    "Teacher note",
                    value=selected_request.get("teacher_note") or "",
                    help="Record the reasoning for the request decision.",
                )
                save_request_decision = st.form_submit_button("Save request status")
            if save_request_decision:
                try:
                    update_revaluation_request(
                        request_id=int(selected_request_id),
                        status=request_status,
                        teacher_decision=teacher_decision,
                        teacher_note=teacher_note,
                        actor=approved_by.strip() or "Teacher",
                    )
                    st.success("Request status saved.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        # Downloadable request-level review sheet.
        review_rows = []
        for request in saved_requests:
            review_rows.append({
                "request_id": request["id"],
                "assessment_id": request["assessment_id"],
                "roll_number": request["roll_number"],
                "student_name": request.get("student_name") or "",
                "email": request.get("student_email") or "",
                "question_id": request["question_id"],
                "subpart": request.get("subpart") or "",
                "reason": request["reason"],
                "evaluation_id": request.get("evaluation_id") or "",
                "ai_marks": request.get("ai_marks") if request.get("ai_marks") is not None else "",
                "teacher_marks": request.get("teacher_marks") if request.get("teacher_marks") is not None else "",
                "max_marks": request.get("max_marks") if request.get("max_marks") is not None else "",
                "status": request["status"],
                "teacher_decision": request.get("teacher_decision") or "",
                "teacher_note": request.get("teacher_note") or "",
            })
        review_csv = io.StringIO(newline="")
        review_writer = csv.DictWriter(review_csv, fieldnames=list(review_rows[0].keys()))
        review_writer.writeheader()
        review_writer.writerows(review_rows)
        st.download_button(
            "Download consolidated re-evaluation review sheet (CSV)",
            data=review_csv.getvalue().encode("utf-8-sig"),
            file_name=f"{selected_assessment_for_requests}_revaluation_review.csv",
            mime="text/csv",
        )