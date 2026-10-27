"""Provider-independent, persisted course and session contracts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceChunk(Model):
    title: str
    text: str
    locator: str


class Exercise(Model):
    prompt: str = Field(min_length=1, max_length=6000)
    starter_code: str = Field(max_length=12000)
    solution_code: str = Field(max_length=12000)


class Segment(Model):
    id: str
    title: str = Field(min_length=1, max_length=200)
    concepts: list[str] = Field(default_factory=list, max_length=12)
    source_excerpt: str
    source_locator: str
    script: list[str] = Field(min_length=1, max_length=60)
    learning_objectives: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(default_factory=list, max_length=12)
    prerequisites: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(default_factory=list, max_length=12)
    check_questions: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(default_factory=list, max_length=12)
    supplemental_notes: list[Annotated[str, Field(min_length=1, max_length=1200)]] = Field(default_factory=list, max_length=12)
    exercise: Exercise | None = None

    @field_validator("script")
    @classmethod
    def valid_sentences(cls, value: list[str]) -> list[str]:
        if any(not sentence.strip() or len(sentence) > 1200 for sentence in value):
            raise ValueError("每句话需为 1–1200 字。")
        return [sentence.strip() for sentence in value]


class Course(Model):
    id: str
    title: str
    source_name: str
    created_at: str
    provider: str
    segments: list[Segment] = Field(min_length=1, max_length=80)


class CourseSummary(Model):
    id: str
    title: str
    source_name: str
    created_at: str
    provider: str
    segment_count: int


class QuestionAnswer(Model):
    id: str
    question: str
    answer: str
    segment_index: int
    sentence_index: int
    created_at: str
    source_locator: str


class Session(Model):
    course_id: str
    segment_index: int = 0
    sentence_index: int = 0
    status: Literal["paused", "playing", "answering", "completed"] = "paused"
    revision: int = 0
    qa: list[QuestionAnswer] = Field(default_factory=list)


class CreateCourse(Model):
    title: str = Field(min_length=1, max_length=160)
    source_text: str = Field(min_length=1, max_length=60000)

    @field_validator("title", "source_text")
    @classmethod
    def strip_nonempty(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError("内容不能为空。")
        return value.strip() if info.field_name == "title" else value


class UpdateSession(Model):
    segment_index: int = Field(ge=0)
    sentence_index: int = Field(ge=0)
    status: Literal["paused", "playing", "completed"]
    revision: int = Field(ge=0)


class AskQuestion(Model):
    question: str = Field(min_length=1, max_length=2000)
    segment_index: int = Field(ge=0)
    sentence_index: int = Field(ge=0)
    revision: int = Field(ge=0)

    @field_validator("question")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入问题。")
        return value.strip()
