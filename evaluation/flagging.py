from evaluation.schemas import (
    Assessment,
    StudentAnswer,
    EvaluationFlag,
    EvaluationResult,
    FlagCode,
    FlagSeverity,
)
from evaluation.schemas import Question

def check_evaluation_marks(
    result: EvaluationResult,
) -> list[EvaluationFlag]:
    """Validate awarded marks and return any required flags."""

    flags: list[EvaluationFlag] = []

    if result.marks_awarded < 0 or result.marks_awarded > result.max_marks:
        flags.append(
            EvaluationFlag(
                code=FlagCode.MARKS_OUT_OF_RANGE,
                severity=FlagSeverity.ERROR,
                reason=(
                    f"Awarded marks ({result.marks_awarded}) "
                    f"are outside the permitted range "
                    f"(0 to {result.max_marks})."
                ),
                question_id=result.question_id,
            )
        )

    if not result.rationale.strip() or not result.student_feedback.strip():
        flags.append(
            EvaluationFlag(
                code=FlagCode.EVALUATION_UNCERTAIN,
                severity=FlagSeverity.REVIEW_REQUIRED,
                reason=(
                    "Evaluation rationale or student feedback is missing."
                ),
                question_id=result.question_id,
            )
        )

    return flags


def collect_post_evaluation_flags(
    results: list[EvaluationResult],
) -> list[EvaluationFlag]:
    """Collect and deduplicate flags after grading."""

    flags: list[EvaluationFlag] = []

    for result in results:
        flags.extend(result.flags)
        flags.extend(check_evaluation_marks(result))

    # Deduplicate flags by issue and location.
    unique_flags: dict[tuple, EvaluationFlag] = {}

    for flag in flags:
        key = (
            flag.code,
            flag.question_id,
            flag.page_number,
            flag.reason,
        )
        unique_flags[key] = flag

    return list(unique_flags.values())


def add_flag(
    result: EvaluationResult,
    code: FlagCode,
    reason: str,
    severity: FlagSeverity = FlagSeverity.REVIEW_REQUIRED,
    page_number: int | None = None,
) -> EvaluationFlag:
    """Create and attach a flag to an evaluation result."""

    flag = EvaluationFlag(
        code=code,
        severity=severity,
        reason=reason,
        question_id=result.question_id,
        page_number=page_number,
    )

    # Avoid adding the same flag twice.
    if flag not in result.flags:
        result.flags.append(flag)

    return flag




def check_rubric_coverage(
    question: Question,
) -> list[EvaluationFlag]:
    """Require an existing, teacher-approved rubric."""

    rubric = question.rubric

    if rubric is None:
        return [
            EvaluationFlag(
                code=FlagCode.RUBRIC_COVERAGE_ISSUE,
                severity=FlagSeverity.REVIEW_REQUIRED,
                reason="No marking rubric is attached to this question.",
                question_id=question.question_id,
            )
        ]

    if not rubric.approved:
        return [
            EvaluationFlag(
                code=FlagCode.RUBRIC_COVERAGE_ISSUE,
                severity=FlagSeverity.REVIEW_REQUIRED,
                reason="The marking rubric has not been teacher-approved.",
                question_id=question.question_id,
            )
        ]

    return []


def collect_pre_evaluation_flags(
    assessment: Assessment,
    answers: list[StudentAnswer],
) -> list[EvaluationFlag]:
    """Collect flags before grading."""

    flags: list[EvaluationFlag] = []

    question_ids = {
        question.question_id
        for question in assessment.questions
    }

    for answer in answers:
        question_id = answer.question_id

        if answer.extraction_uncertain:
            flags.append(
                EvaluationFlag(
                    code=FlagCode.OCR_UNCERTAIN,
                    severity=FlagSeverity.REVIEW_REQUIRED,
                    reason="Answer extraction is marked as uncertain.",
                    question_id=question_id,
                    page_number=answer.page_number,
                )
            )

        if not answer.answer_text.strip():
            flags.append(
                EvaluationFlag(
                    code=FlagCode.ANSWER_MISSING,
                    severity=FlagSeverity.WARNING,
                    reason="Answer text is empty or missing.",
                    question_id=question_id,
                    page_number=answer.page_number,
                )
            )

        if not question_id or question_id not in question_ids:
            flags.append(
                EvaluationFlag(
                    code=FlagCode.QUESTION_UNMATCHED,
                    severity=FlagSeverity.REVIEW_REQUIRED,
                    reason=(
                        "Answer has not been matched to a valid "
                        "question in this assessment."
                    ),
                    question_id=question_id,
                    page_number=answer.page_number,
                )
            )

    return flags
