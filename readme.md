# Automated Answer Script Evaluation System

 

**Project README — Features, Workflow, Architecture and Current Scope**  

*Prepared: October 2026*

 

## 1. Project Overview

 

The Automated Answer Script Evaluation System is a Streamlit-based application that uses a large language model (LLM) to evaluate student answers against teacher-defined questions, marking rubrics, and assessment instructions. It stores evaluation results and review decisions in SQLite and highlights answers that may need teacher attention.

 

The system is designed to assist teachers, not replace their judgement. AI evaluation results are saved automatically; teacher review is optional for ordinary answers, while flagged answers are collected in a dedicated review queue.

 

## 2. Key Features Implemented

 

### 2.1 Assessment and rubric configuration

- Create an assessment with an ID/title, subject, instructions, and multiple questions.

- Define maximum marks and rubric criteria for each question.

- Record accepted alternatives and question-specific instructions.

- Require teacher approval of the assessment paper/rubrics before starting evaluation.

 

### 2.2 Student submissions

- Enter a single student answer directly in the application.

- Upload a CSV containing multiple students, with one row per student and answer columns associated with question IDs.

- Use student/roll identifiers to associate answers with students.

- The CSV workflow supports typed answer text; OCR extraction from handwritten scripts is not implemented.

 

### 2.3 AI-powered answer evaluation

- Send answers, question details, marking rubric, and instructions to the configured LLM API.

- Produce structured evaluation output for each answer.

- Provide AI-assigned marks, rationale, correct points, missing or incorrect points, criterion-wise marks, and feedback.

- Support criterion-based marking and assessment-specific full-mark requirements.

 

### 2.4 Automatic flagging

- Run pre-evaluation checks and collect pre-evaluation flags.

- Store AI-generated flags alongside evaluation results.

- Highlight concerns such as missing answers, possible question mismatch, uncertainty, suspicious/off-topic content, or rubric-coverage issues when detected by the configured checks/model.

- Use flags to identify answers that may need human attention; flags are signals for review, not proof of an error.

 

### 2.5 Automatic saving and optional teacher review

- Save AI evaluation results automatically without requiring teacher approval for every answer.

- Keep unflagged answers in the saved evaluation history.

- Provide a dedicated Teacher Review Queue for answers with pre-evaluation and/or AI flags.

- Show the selected flagged answer's student/question details, answer text, AI marks, flag reasons, rationale/feedback, and rubric information.

 

### 2.6 Teacher mark overrides and revisions

- Allow teachers to edit or override AI-assigned marks.

- Require a reason when marks are changed.

- Validate revised marks against the question's maximum marks.

- Preserve previous decisions and record revisions in the audit history.

 

### 2.7 Persistent storage and history

- Use SQLite to persist assessments/evaluation records and related decisions.

- Store the original AI evaluation output and the relevant answer/rubric context for later inspection.

- Persist pre-evaluation flags separately in the `pre_flags_json` field.

- Include a backward-compatible database migration that adds `pre_flags_json` to older databases.

- Retain audit events such as `AI_EVALUATION_SAVED`, `TEACHER_DECISION_SAVED`, and `TEACHER_DECISION_REVISED`.

 

### 2.8 Re-evaluation request workflow

- Import re-evaluation requests from a Google Forms response CSV.

- Match requests to saved evaluations using available identifiers such as roll number and question/subpart.

- Detect duplicate requests and track request status, teacher decision, and teacher note.

- Group requests for review by question/subpart.

- Use a configurable majority-concern threshold; the default concern condition is strictly greater than 50%, based on unique rolls among evaluated students.

 

### 2.9 Results and exports

- View latest evaluation results and saved evaluation history in the application.

- Review re-evaluation requests and their status.

- Download/export evaluation results as CSV where the relevant export control is available in the current application.

 

## 3. Technology Stack

 

| Component | Technology |

|---|---|

| Language | Python |

| Web interface | Streamlit |

| Structured validation | Pydantic v2 |

| LLM client | OpenAI Python SDK using an OpenAI-compatible API endpoint |

| Configured model provider | Groq Cloud API (configured through environment variables) |

| Database | SQLite |

| Configuration/secrets | `.env` file; API keys should not be hard-coded or committed to Git |

 

## 4. High-Level Architecture

 

The application follows a simple layered flow:

 

1. **Input layer:** teacher-approved assessment and rubrics, student answers (single entry or CSV), and re-evaluation request CSV.

2. **Pre-evaluation checks:** completeness and other rule-based checks generate pre-evaluation flags.

3. **AI evaluation layer:** sends the question, answer, rubric, and instructions to the configured LLM and receives structured evaluation output.

4. **Persistence layer:** saves results, flags, teacher decisions, re-evaluation requests, and audit events in SQLite.

5. **Streamlit application layer:** lets the user configure assessments, submit answers, inspect results/history, review flagged answers, and manage optional revisions.

6. **Output layer:** evaluation results, flagged-answer queue, re-evaluation review workflow, and saved history.

 

## 5. Typical Workflow

 

1. Create an assessment and enter questions, maximum marks, rubrics, alternatives, and instructions.

2. Review and approve the paper/rubrics before evaluation.

3. Enter a single answer or upload the student-submission CSV.

4. Run evaluation. Pre-evaluation checks and the AI model produce flags and evaluation details.

5. The application saves AI results automatically.

6. Inspect the results and history. Review flagged answers in the Teacher Review Queue.

7. Optionally override marks or revise a previous teacher decision, providing a reason when marks change.

8. Import Google Forms re-evaluation requests when needed and process the resulting queue.

 

## 6. Project Structure

 

```text

AutomatedAnswerEvaluator/

├── app/

│   └── main.py              # Streamlit user interface and workflow

├── evaluation/

│   ├── schemas.py           # Structured data schemas

│   ├── flagging.py          # Pre-evaluation checks/flagging

│   ├── model_client.py      # LLM API client

│   ├── evaluator.py         # Evaluation orchestration

│   ├── storage.py           # SQLite persistence and audit history

│   └── revaluation.py       # Re-evaluation request workflow

├── data/

│   └── evaluations.db       # Local SQLite database (runtime data)

├── .env                     # Local API/configuration values

├── requirements.txt         # Python dependencies

└── .gitignore               # Files excluded from Git

```

 

## 7. Configuration

 

Example `.env` configuration (replace the placeholder with a valid key; never publish the real key):

 

```dotenv

GROQ_API_KEY=your_actual_groq_api_key

MODEL_BASE_URL=https://api.groq.com/openai/v1

MODEL_NAME=openai/gpt-oss-120b

MODEL_API_KEY_ENV=GROQ_API_KEY

```

 

**Security:** Never commit a real API key or other secrets to GitHub. Keep `.env` excluded through `.gitignore`.

 

## 8. Setup and Run

 

From the project directory on Ubuntu:

 

```bash

cd ~/AutomatedAnswerEvaluator

source .venv/bin/activate

python -m py_compile app/main.py evaluation/storage.py

python -m streamlit run app/main.py

```

 

If Streamlit is already installed in the project virtual environment, the final command launches the web application. Keep the terminal running while using the app.

 

## 9. Important Data and Review Rules

 

- The assessment paper/rubric must be approved before evaluation starts.

- AI results are saved automatically; routine teacher approval of every answer is not required.

- The Teacher Review Queue is intended for flagged answers; unflagged answers remain available in history.

- A mark change must include a reason, and revised marks cannot exceed the question's maximum marks.

- Original AI results and later teacher decisions/revisions are retained for traceability.

- API credentials belong in `.env` and should be excluded from version control.

 

## 10. Current Limitations and Future Work

 

- OCR for handwritten answer scripts is not implemented; current CSV ingestion relies on answer text already being available.

- The quality of AI marks and flags depends on the quality of the rubric, model response, and configured checks; teacher oversight remains important.

- Some views currently query a limited recent set of up to 1,000 evaluation records, so very large datasets may need pagination or query improvements.

- Future improvements could include OCR with confidence scores, larger-scale analytics, richer reporting, additional file formats, and role-based access controls.

 

## 11. Project Status Note

 

This document describes the implemented design and features based on the project state recorded during development. The application still needs end-to-end testing in the local environment, including a real evaluation request, database persistence, flagged-answer queue behaviour, optional mark revision, and re-evaluation CSV processing.
