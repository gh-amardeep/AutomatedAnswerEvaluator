
from dotenv import load_dotenv
load_dotenv()
import streamlit as st

from evaluation.schemas import (
    Assessment,
    Question,
    MarkingRubric,
    RubricCriterion,
    StudentAnswer,
)
from evaluation.model_client import ModelClient
from evaluation.evaluator import AnswerEvaluator
from evaluation.flagging import collect_pre_evaluation_flags


st.set_page_config(
    page_title="Automated Answer Evaluator",
    page_icon="📝",
    layout="wide",
)

st.title("Automated Answer Script Evaluation System")
st.caption(
    "Subject-independent grading with teacher-approved rubrics "
    "and review flags."
)


# --------------------------------------------------
# Assessment details
# --------------------------------------------------

st.header("1. Assessment details")

assessment_id = st.text_input("Assessment ID", "ASSESSMENT-001")
assessment_title = st.text_input("Assessment title", "Sample Assessment")
subject = st.text_input(
    "Subject (optional)",
    placeholder="e.g. Machine Learning, Control Systems",
)

subject_instructions = st.text_area(
    "Subject-level evaluation instructions (optional)",
    placeholder="Enter any general instructions for this subject.",
)

question_id = st.text_input("Question ID", "Q1")
question_text = st.text_area("Question text")
question_max_marks = st.number_input(
    "Question maximum marks",
    min_value=0.5,
    value=5.0,
    step=0.5,
)

# --------------------------------------------------
# Rubric
# --------------------------------------------------

st.header("2. Teacher-approved rubric")

criterion_count = st.number_input(
    "Number of rubric criteria",
    min_value=1,
    max_value=10,
    value=2,
    step=1,
)

criteria = []

for i in range(int(criterion_count)):
    with st.expander(f"Criterion {i + 1}", expanded=True):
        criterion_id = st.text_input(
            "Criterion ID",
            value=f"C{i + 1}",
            key=f"criterion_id_{i}",
        )
        description = st.text_input(
            "What earns marks?",
            key=f"description_{i}",
        )
        max_marks = st.number_input(
            "Maximum marks",
            min_value=0.0,
            value=question_max_marks / criterion_count,
            step=0.5,
            key=f"criterion_marks_{i}",
        )
        evidence_text = st.text_area(
            "Expected evidence (one item per line)",
            key=f"evidence_{i}",
        )

        criteria.append(
            RubricCriterion(
                criterion_id=criterion_id,
                description=description,
                max_marks=max_marks,
                expected_evidence=[
                    line.strip()
                    for line in evidence_text.splitlines()
                    if line.strip()
                ],
            )
        )

accepted_alternatives = st.text_area(
    "Accepted alternative approaches or answers",
    placeholder="Enter one alternative per line.",
)

rubric_instructions = st.text_area(
    "Rubric-specific instructions (optional)"
)

approved_by = st.text_input("Teacher name / approver")

rubric_approved = st.checkbox(
    "I confirm that this rubric has been reviewed and approved."
)

# --------------------------------------------------
# Student answer
# --------------------------------------------------

st.header("3. Student details and answer")

student_name = st.text_input("Student name (optional)")
roll_number = st.text_input("Roll number")
answer_text = st.text_area(
    "Student answer",
    height=250,
    placeholder="Enter the student's answer here.",
)

# --------------------------------------------------
# Evaluate
# --------------------------------------------------

if st.button("Evaluate answer", type="primary"):
    try:
        if not assessment_id.strip():
            st.error("Enter an assessment ID.")
            st.stop()

        if not question_id.strip() or not question_text.strip():
            st.error("Enter the question ID and question text.")
            st.stop()

        if not roll_number.strip():
            st.error("Enter the student's roll number.")
            st.stop()

        if not all(c.description.strip() for c in criteria):
            st.error("Enter a description for every rubric criterion.")
            st.stop()

        if len({c.criterion_id for c in criteria}) != len(criteria):
            st.error("Rubric criterion IDs must be unique.")
            st.stop()

        rubric = MarkingRubric(
            rubric_id=f"{assessment_id}-{question_id}-R1",
            question_id=question_id,
            criteria=criteria,
            accepted_alternatives=[
                line.strip()
                for line in accepted_alternatives.splitlines()
                if line.strip()
            ],
            evaluation_instructions=rubric_instructions or None,
            approved_by=approved_by or None,
            approved=rubric_approved,
        )

        question = Question(
            question_id=question_id,
            question_text=question_text,
            max_marks=question_max_marks,
            rubric=rubric,
        )

        assessment = Assessment(
            assessment_id=assessment_id,
            title=assessment_title,
            subject=subject or None,
            subject_instructions=subject_instructions or None,
            questions=[question],
            total_max_marks=question_max_marks,
        )

        answer = StudentAnswer(
            answer_id=f"{assessment_id}-{roll_number}-{question_id}",
            roll_number=roll_number,
            assessment_id=assessment_id,
            question_id=question_id,
            answer_text=answer_text,
            source_type="typed",
        )

        st.session_state["assessment"] = assessment
        st.session_state["student_name"] = student_name
        st.session_state["answer"] = answer

        pre_flags = collect_pre_evaluation_flags(
            assessment,
            [answer],
        )

        st.subheader("Pre-evaluation checks")

        if pre_flags:
            for flag in pre_flags:
                st.warning(
                    f"{flag.code.value}: {flag.reason}"
                )

        elif rubric_approved and answer_text.strip():
            st.success("No pre-evaluation issues detected.")

        # A missing or unapproved rubric must not reach the model.
        if not rubric_approved:
            st.error(
                "Evaluation blocked: obtain teacher approval "
                "for the rubric first."
            )
            st.stop()

        if not answer_text.strip():
            st.error("Enter the student answer before evaluating.")
            st.stop()

        with st.spinner("Evaluating answer..."):
            evaluator = AnswerEvaluator(ModelClient())
            result = evaluator.evaluate(
                assessment,
                question,
                answer,
            )

        st.session_state["evaluation_result"] = result

        st.header("4. Evaluation result")

        st.metric(
            "AI-awarded marks",
            f"{result.marks_awarded:g} / {result.max_marks:g}",
        )

        st.info(
            "This is an AI-generated result. It is not final until "
            "reviewed and approved by the teacher."
        )

        st.subheader("Marking rationale")
        st.write(result.rationale)

        st.subheader("Correct points")
        if result.correct_points:
            for item in result.correct_points:
                st.write(f"- {item}")
        else:
            st.write("No correct points were listed.")

        st.subheader("Missing or incorrect points")
        if result.missing_or_incorrect_points:
            for item in result.missing_or_incorrect_points:
                st.write(f"- {item}")
        else:
            st.write("No missing or incorrect points were listed.")

        st.subheader("What is required for full marks")
        if result.full_marks_requirements:
            for item in result.full_marks_requirements:
                st.write(f"- {item}")
        else:
            st.write("No additional requirements were listed.")

        st.subheader("Criterion-wise marks")
        for item in result.criterion_results:
            st.write(
                f"**{item.criterion_id}:** "
                f"{item.marks_awarded:g} marks — {item.rationale}"
            )

        st.subheader("Student feedback")
        st.write(result.student_feedback)

        st.subheader("Review flags")
        if result.flags:
            for flag in result.flags:
                st.warning(
                    f"{flag.severity.value} — {flag.code.value}: "
                    f"{flag.reason}"
                )
        else:
            st.write("No flags generated.")

    except ValueError as exc:
        st.error(f"Input validation error: {exc}")

    except Exception as exc:
        st.error(
            "Evaluation could not be completed. "
            "Check the configuration and error details."
        )
        st.exception(exc)
