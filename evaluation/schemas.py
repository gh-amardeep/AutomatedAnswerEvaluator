
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------
# 1. Evaluation flags
# --------------------------------------------------

class FlagCode(str, Enum):
    OCR_UNCERTAIN = "OCR_UNCERTAIN"
    ANSWER_MISSING = "ANSWER_MISSING"
    QUESTION_UNMATCHED = "QUESTION_UNMATCHED"
    EVALUATION_UNCERTAIN = "EVALUATION_UNCERTAIN"
    RUBRIC_COVERAGE_ISSUE = "RUBRIC_COVERAGE_ISSUE"
    MARKS_OUT_OF_RANGE = "MARKS_OUT_OF_RANGE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class FlagSeverity(str, Enum):
    WARNING = "WARNING"
    ERROR = "ERROR"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class EvaluationFlag(BaseModel):
    code: FlagCode
    severity: FlagSeverity
    reason: str

    question_id: Optional[str] = None
    page_number: Optional[int] = None

    resolved: bool = False
    resolution_note: Optional[str] = None


# --------------------------------------------------
# 2. Rubric models
# --------------------------------------------------

class RubricCriterion(BaseModel):
    criterion_id: str
    description: str
    max_marks: float = Field(ge=0)

    # Evidence that may earn marks for this criterion.
    expected_evidence: list[str] = Field(default_factory=list)


class MarkingRubric(BaseModel):
    rubric_id: str
    question_id: str

    criteria: list[RubricCriterion]

    # Alternative valid approaches or answers.
    accepted_alternatives: list[str] = Field(default_factory=list)

    # Optional instructions for a particular subject or question.
    evaluation_instructions: Optional[str] = None

    approved_by: Optional[str] = None
    approved: bool = False

    @model_validator(mode="after")
    def validate_rubric(self):
        if not self.criteria:
            raise ValueError(
                "A rubric must contain at least one criterion."
            )

        total = sum(c.max_marks for c in self.criteria)

        if total <= 0:
            raise ValueError(
                "Rubric maximum marks must be positive."
            )

        if len({c.criterion_id for c in self.criteria}) != len(
            self.criteria
        ):
            raise ValueError(
                "Criterion IDs must be unique within a rubric."
            )

        return self

    @property
    def total_max_marks(self) -> float:
        return sum(c.max_marks for c in self.criteria)


# --------------------------------------------------
# 3. Assessment and question models
# --------------------------------------------------

class Question(BaseModel):
    # Can represent a main question or subquestion,
    # e.g. Q1, Q1(a), Q1(a)(i).
    question_id: str
    question_text: str
    max_marks: float = Field(gt=0)

    rubric: Optional[MarkingRubric] = None

    # Optional instructions for this question.
    evaluation_instructions: Optional[str] = None

    @model_validator(mode="after")
    def validate_question_rubric(self):
        if self.rubric is not None:
            if self.rubric.question_id != self.question_id:
                raise ValueError(
                    "Rubric question_id must match the question."
                )

            if abs(
                self.rubric.total_max_marks - self.max_marks
            ) > 1e-6:
                raise ValueError(
                    "Rubric total must match question maximum marks."
                )

        return self


class Assessment(BaseModel):
    assessment_id: str
    title: str

    # Optional: the system must still work when unspecified.
    subject: Optional[str] = None

    # Optional general context for the evaluation engine.
    subject_instructions: Optional[str] = None

    questions: list[Question]
    total_max_marks: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_assessment(self):
        if not self.questions:
            raise ValueError(
                "An assessment must contain questions."
            )

        question_ids = [q.question_id for q in self.questions]

        if len(question_ids) != len(set(question_ids)):
            raise ValueError(
                "Question IDs must be unique."
            )

        calculated_total = sum(
            q.max_marks for q in self.questions
        )

        if abs(
            calculated_total - self.total_max_marks
        ) > 1e-6:
            raise ValueError(
                "Assessment total does not match question marks."
            )

        return self


# --------------------------------------------------
# 4. Student and answer models
# --------------------------------------------------

class Student(BaseModel):
    student_name: Optional[str] = None
    roll_number: str
    assessment_id: str


class StudentAnswer(BaseModel):
    answer_id: str
    roll_number: str
    assessment_id: str

    # None when the answer has not yet been matched.
    question_id: Optional[str] = None

    answer_text: str = ""

    # Examples: typed, scanned, handwritten_ocr.
    source_type: str = "typed"

    page_number: Optional[int] = None

    # Reserved for later OCR and answer extraction.
    extraction_uncertain: bool = False


# --------------------------------------------------
# 5. Evaluation result models
# --------------------------------------------------

class CriterionResult(BaseModel):
    criterion_id: str
    marks_awarded: float = Field(ge=0)
    rationale: str


class EvaluationResult(BaseModel):
    answer_id: str
    question_id: str

    max_marks: float = Field(gt=0)
    marks_awarded: float = Field(ge=0)

    criterion_results: list[CriterionResult] = Field(
        default_factory=list
    )

    correct_points: list[str] = Field(default_factory=list)
    missing_or_incorrect_points: list[str] = Field(
        default_factory=list
    )
    full_marks_requirements: list[str] = Field(
        default_factory=list
    )

    rationale: str
    student_feedback: str

    flags: list[EvaluationFlag] = Field(default_factory=list)

    # Teacher review and override fields.
    teacher_approved: bool = False
    teacher_marks: Optional[float] = Field(
        default=None,
        ge=0
    )
    teacher_review_note: Optional[str] = None

    @model_validator(mode="after")
    def flag_invalid_marks(self):
        if self.marks_awarded > self.max_marks:
            flag_exists = any(
                flag.code == FlagCode.MARKS_OUT_OF_RANGE
                for flag in self.flags
            )

            if not flag_exists:
                self.flags.append(
                    EvaluationFlag(
                        code=FlagCode.MARKS_OUT_OF_RANGE,
                        severity=FlagSeverity.ERROR,
                        reason=(
                            f"Awarded marks ({self.marks_awarded}) "
                            f"exceed the maximum ({self.max_marks})."
                        ),
                        question_id=self.question_id,
                    )
                )

        if (
            self.teacher_marks is not None
            and self.teacher_marks > self.max_marks
        ):
            raise ValueError(
                "Teacher-awarded marks cannot exceed maximum marks."
            )

        return self
