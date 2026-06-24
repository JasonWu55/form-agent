"""Survey config: Pydantic models, YAML loader, and answer validation.

The trust boundary is here — both the survey config (loaded once at startup)
and every raw user answer pass through this module. Failures raise plain
``ValueError`` with human-readable messages so the agent can relay them and
startup fails fast with a useful error rather than a stack trace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError, field_validator, model_validator


class Question(BaseModel):
    id: str
    prompt: str
    type: Literal["text", "int", "choice"] = "text"
    required: bool = True
    min: int | None = None
    max: int | None = None
    options: list[str] | None = None

    @field_validator("id")
    @classmethod
    def _id_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question id must not be blank")
        return v

    @model_validator(mode="after")
    def _check_type_constraints(self) -> "Question":
        if self.type == "int":
            if self.options is not None:
                raise ValueError(f"question {self.id!r}: 'options' is only valid for type 'choice'")
            if self.min is not None and self.max is not None and self.min > self.max:
                raise ValueError(f"question {self.id!r}: min ({self.min}) must be <= max ({self.max})")
        elif self.type == "choice":
            if not self.options:
                raise ValueError(f"question {self.id!r}: type 'choice' requires a non-empty 'options' list")
            if self.min is not None or self.max is not None:
                raise ValueError(f"question {self.id!r}: 'min'/'max' are only valid for type 'int'")
        else:  # text
            if self.min is not None or self.max is not None:
                raise ValueError(f"question {self.id!r}: 'min'/'max' are only valid for type 'int'")
            if self.options is not None:
                raise ValueError(f"question {self.id!r}: 'options' is only valid for type 'choice'")
        return self


class Survey(BaseModel):
    title: str
    description: str | None = None
    worksheet: str | None = None
    questions: list[Question]

    @field_validator("questions")
    @classmethod
    def _nonempty_unique(cls, qs: list[Question]) -> list[Question]:
        if not qs:
            raise ValueError("survey must have at least one question")
        seen: set[str] = set()
        for q in qs:
            if q.id in seen:
                raise ValueError(f"duplicate question id: {q.id!r}")
            seen.add(q.id)
        return qs

    @property
    def ids(self) -> list[str]:
        """Question ids in order (column order for the Sheets writer / agent)."""
        return [q.id for q in self.questions]


def question_ids(survey: Survey) -> list[str]:
    """Question ids in order (free-function alias for ``Survey.ids``)."""
    return survey.ids


def load_survey(path: str | Path) -> Survey:
    """Load and validate a survey from a YAML file.

    ``path`` may be relative to the project root. Re-raises pydantic
    ``ValidationError`` as a readable ``ValueError`` so startup fails fast.
    """
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        # try relative to the project root (parent of this package)
        alt = Path(__file__).resolve().parent.parent / p
        if alt.exists():
            p = alt
    if not p.exists():
        raise ValueError(f"survey file not found: {path}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"survey file {p} must contain a YAML mapping at the top level")

    try:
        return Survey(**raw)
    except ValidationError as e:
        raise ValueError(f"invalid survey config in {p}:\n{e}") from e


def validate_answer(question: Question, raw: str) -> Any:
    """Coerce/validate a raw user answer. Returns the cleaned value (str or int).

    Raises ``ValueError`` with a human-friendly message on any failure.
    """
    text = (raw or "").strip()

    if question.type == "text":
        if not text:
            if question.required:
                raise ValueError("This answer is required, please provide a response.")
            return ""
        return text

    if question.type == "int":
        if not text:
            raise ValueError("This answer is required and must be a whole number.")
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"'{text}' must be a whole number.") from None
        if question.min is not None and value < question.min:
            raise ValueError(f"Value must be at least {question.min}.")
        if question.max is not None and value > question.max:
            raise ValueError(f"Value must be at most {question.max}.")
        return value

    # choice
    options = question.options or []
    if text in options:
        return text
    # accept a trimmed, case-insensitive match, returning the canonical option
    lowered = text.lower()
    for opt in options:
        if opt.strip().lower() == lowered:
            return opt
    # fallback: resolve only if the reply is a substring of EXACTLY ONE option
    # (case-insensitive). e.g. "yes" -> "會 / Yes", "不會" -> "不會 / No". A reply
    # that matches several (e.g. "會" is inside BOTH "會 / Yes" and "不會 / No")
    # stays ambiguous and is rejected — the agent disambiguates via its
    # instructions before calling the tool. ponytail: unique-substring only;
    # add synonym/keyword matching if real replies need it.
    matches = [opt for opt in options if lowered in opt.lower()]
    if lowered and len(matches) == 1:  # `lowered` guard: "" is a substring of every option
        return matches[0]
    raise ValueError("Please choose one of: " + ", ".join(options) + ".")


if __name__ == "__main__":
    # Runnable self-check: `uv run python agent/survey.py`
    s = load_survey("agent/survey.yaml")
    assert len(s.questions) == 4, f"expected 4 questions, got {len(s.questions)}"
    assert s.ids == ["name", "rating", "recommend", "comments"], s.ids
    assert question_ids(s) == s.ids
    print("OK: loaded survey with 4 questions:", s.ids)

    # duplicate id must be rejected
    try:
        Survey(
            title="dup",
            questions=[Question(id="x", prompt="a"), Question(id="x", prompt="b")],
        )
        raise AssertionError("duplicate id should have raised")
    except ValueError as e:
        assert "duplicate" in str(e).lower(), e
    print("OK: duplicate id rejected")

    # choice without options must be rejected
    try:
        Question(id="c", prompt="?", type="choice")
        raise AssertionError("choice without options should have raised")
    except ValueError as e:
        assert "options" in str(e).lower(), e
    print("OK: choice without options rejected")

    # a malformed config file must fail fast with a clear ValueError
    rating = next(q for q in s.questions if q.id == "rating")
    recommend = next(q for q in s.questions if q.id == "recommend")
    name = next(q for q in s.questions if q.id == "name")
    comments = next(q for q in s.questions if q.id == "comments")

    # int bounds
    assert validate_answer(rating, "3") == 3
    for bad in ("9", "0", "abc", ""):
        try:
            validate_answer(rating, bad)
            raise AssertionError(f"rating {bad!r} should have been rejected")
        except ValueError:
            pass
    print("OK: int validation (3 accepted; 9/0/abc/'' rejected)")

    # choice
    assert validate_answer(recommend, "會 / Yes") == "會 / Yes"
    assert validate_answer(recommend, "  不會 / no  ") == "不會 / No"  # trimmed + case-insensitive
    # natural-language fallback: a reply that is a substring of exactly ONE option
    # resolves to that canonical option.
    assert validate_answer(recommend, "Yes") == "會 / Yes"
    assert validate_answer(recommend, "no") == "不會 / No"
    assert validate_answer(recommend, "不會") == "不會 / No"
    # "會" is inside BOTH options -> ambiguous; "" is inside every option -> both
    # must still be rejected (the latter guards future single-option choices).
    for bad in ("Maybe", "會", ""):
        try:
            validate_answer(recommend, bad)
            raise AssertionError(f"choice {bad!r} should have been rejected")
        except ValueError:
            pass
    print("OK: choice validation (exact + ci + unique-substring; ambiguous/bad rejected)")

    # text required vs optional
    assert validate_answer(name, "  Ada  ") == "Ada"
    try:
        validate_answer(name, "")
        raise AssertionError("empty required text should have been rejected")
    except ValueError:
        pass
    assert validate_answer(comments, "") == ""  # optional empty allowed
    print("OK: text validation (required rejects empty; optional allows empty)")

    print("ALL CHECKS PASSED")
