"""SQLite persistence and revision-based state transitions."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .schemas import Course, CourseSummary, QuestionAnswer, Session


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    def __init__(self, session: Session):
        self.session = session


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "learning.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS courses (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    course_id TEXT PRIMARY KEY REFERENCES courses(id),
                    segment_index INTEGER NOT NULL DEFAULT 0,
                    sentence_index INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'paused',
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS qa (
                    id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
                    body TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id TEXT NOT NULL REFERENCES courses(id), kind TEXT NOT NULL,
                    body TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            db.execute("UPDATE sessions SET status='paused', revision=revision+1 WHERE status='answering'")

    @contextmanager
    def connection(self, write: bool = False):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, course_id: str, kind: str, body: dict) -> None:
        db.execute("INSERT INTO events(course_id,kind,body,created_at) VALUES (?,?,?,?)",
                   (course_id, kind, json.dumps(body, ensure_ascii=False), now()))

    def add_course(self, course: Course) -> Course:
        with self.connection(write=True) as db:
            db.execute("INSERT INTO courses VALUES (?,?)", (course.id, course.model_dump_json()))
            db.execute("INSERT INTO sessions(course_id) VALUES (?)", (course.id,))
            self._event(db, course.id, "course_created", {"provider": course.provider})
        return course

    def get_course(self, course_id: str) -> Course:
        with self.connection() as db:
            row = db.execute("SELECT body FROM courses WHERE id=?", (course_id,)).fetchone()
        if row is None:
            raise NotFoundError(course_id)
        return Course.model_validate_json(row["body"])

    def list_courses(self) -> list[CourseSummary]:
        with self.connection() as db:
            rows = db.execute("SELECT body FROM courses ORDER BY rowid DESC").fetchall()
        result = []
        for row in rows:
            course = Course.model_validate_json(row["body"])
            result.append(CourseSummary(**course.model_dump(exclude={"segments"}), segment_count=len(course.segments)))
        return result

    def _session(self, db, course_id: str) -> Session:
        row = db.execute("SELECT * FROM sessions WHERE course_id=?", (course_id,)).fetchone()
        if row is None:
            raise NotFoundError(course_id)
        qa = [QuestionAnswer.model_validate_json(item["body"]) for item in db.execute(
            "SELECT body FROM qa WHERE course_id=? ORDER BY rowid", (course_id,)).fetchall()]
        return Session(**dict(row), qa=qa)

    def get_session(self, course_id: str) -> Session:
        with self.connection() as db:
            return self._session(db, course_id)

    def update_session(self, course_id: str, segment_index: int, sentence_index: int,
                       status: str, revision: int, kind: str = "cursor_saved") -> Session:
        with self.connection(write=True) as db:
            current = self._session(db, course_id)
            if current.revision != revision:
                raise ConflictError(current)
            db.execute("UPDATE sessions SET segment_index=?, sentence_index=?, status=?, revision=revision+1 WHERE course_id=?",
                       (segment_index, sentence_index, status, course_id))
            # Legacy text/browser-TTS controls share the same course cursor.
            # An explicit legacy write also revokes an older voice transport;
            # its late acknowledgements must not pull the cursor backwards.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_states'").fetchone():
                row = db.execute("SELECT body FROM learning_states WHERE course_id=?", (course_id,)).fetchone()
                if row:
                    learning = json.loads(row["body"])
                    learning.update(segment_index=segment_index, sentence_index=sentence_index,
                                    completed=status == "completed", activity="completed" if status == "completed" else "paused",
                                    connection_id=None, utterance=None, resume_anchor=None,
                                    revision=learning["revision"] + 1)
                    db.execute("UPDATE learning_states SET body=? WHERE course_id=?",
                               (json.dumps(learning, ensure_ascii=False), course_id))
            self._event(db, course_id, kind, {"segment_index": segment_index, "sentence_index": sentence_index,
                                             "status": status, "revision": revision + 1})
            return self._session(db, course_id)

    def finish_question(self, course_id: str, question: str, answer: str,
                        segment_index: int, sentence_index: int, source_locator: str,
                        owned_revision: int) -> Session:
        qa = QuestionAnswer(id=uuid4().hex, question=question, answer=answer,
                            segment_index=segment_index, sentence_index=sentence_index,
                            source_locator=source_locator, created_at=now())
        with self.connection(write=True) as db:
            db.execute("INSERT INTO qa VALUES (?,?,?,?)", (qa.id, course_id, qa.model_dump_json(), qa.created_at))
            db.execute("UPDATE sessions SET status='paused', revision=revision+1 WHERE course_id=? AND revision=?",
                       (course_id, owned_revision))
            self._event(db, course_id, "question_answered", {"qa_id": qa.id, "checkpoint_revision": owned_revision})
            return self._session(db, course_id)

    def fail_question(self, course_id: str, owned_revision: int) -> None:
        with self.connection(write=True) as db:
            db.execute("UPDATE sessions SET status='paused', revision=revision+1 WHERE course_id=? AND revision=?",
                       (course_id, owned_revision))
            self._event(db, course_id, "question_failed", {"checkpoint_revision": owned_revision})
