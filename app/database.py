import hashlib
import os
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text,
    create_engine
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Member(Base):
    __tablename__ = "members"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String(100), unique=True, nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    display_name = Column(String(200), nullable=True)  # how they appear in transcripts
    is_chairman = Column(Boolean, default=False)
    is_stakeholder = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    tasks = relationship("Task", back_populates="assignee", foreign_keys="[Task.assignee_id]")
    comments = relationship("TaskComment", back_populates="author")

    @property
    def name(self):
        return self.display_name or self.first_name or self.username or f"User {self.telegram_id}"


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False)
    title = Column(String(500), nullable=True)
    raw_transcript = Column(Text, nullable=False)  # original text from Plaud
    summary = Column(Text, nullable=True)  # AI-generated structured summary
    participants = Column(Text, nullable=True)  # comma-separated names
    decisions = Column(Text, nullable=True)  # AI-extracted decisions (JSON)
    open_questions = Column(Text, nullable=True)  # AI-extracted open questions (JSON)
    agenda_items_next = Column(Text, nullable=True)  # items for next meeting agenda (JSON)
    is_confirmed = Column(Boolean, default=False)
    analysis_json = Column(Text, nullable=True)  # raw AI analysis, cleared after confirm
    transcript_hash = Column(String(16), nullable=True, unique=False)  # for dedup detection
    created_at = Column(DateTime, default=datetime.utcnow)

    tasks = relationship("Task", back_populates="meeting")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=True)
    assignee_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    context_quote = Column(Text, nullable=True)  # quote from transcript
    priority = Column(String(20), default="medium")  # high, medium, low
    status = Column(String(20), default="new")  # new, in_progress, done, overdue
    deadline = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    progress_percent = Column(Integer, default=0)  # 0-100
    goal_id = Column(Integer, ForeignKey("strategic_goals.id"), nullable=True)
    source = Column(String(20), default="manual")  # manual, meeting, stakeholder
    created_by_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    is_verified = Column(Boolean, default=True)  # False for meeting tasks until chairman verifies
    completion_comment = Column(Text, nullable=True)  # how the task was completed
    last_notified_at = Column(DateTime, nullable=True)  # last deadline reminder sent
    created_at = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="tasks")
    assignee = relationship("Member", back_populates="tasks", foreign_keys="Task.assignee_id")
    creator = relationship("Member", foreign_keys="Task.created_by_id")
    comments = relationship("TaskComment", back_populates="task")


class TaskComment(Base):
    __tablename__ = "task_comments"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    author_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    author_email = Column(Text, nullable=True)  # for web-posted comments
    text = Column(Text, nullable=False)
    comment_type = Column(String(20), default="comment")  # comment, question, answer
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="comments")
    author = relationship("Member", back_populates="comments")


class ScheduledMeeting(Base):
    """Scheduled upcoming meetings for agenda distribution and status collection."""
    __tablename__ = "scheduled_meetings"

    id = Column(Integer, primary_key=True)
    scheduled_date = Column(DateTime, nullable=False)
    title = Column(String(500), nullable=True)
    agenda_text = Column(Text, nullable=True)  # generated agenda
    agenda_sent = Column(Boolean, default=False)  # was agenda sent to participants
    status_collection_sent = Column(Boolean, default=False)  # were status requests sent
    is_completed = Column(Boolean, default=False)  # meeting took place
    linked_meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=True)  # after completion
    created_at = Column(DateTime, default=datetime.utcnow)


class AgendaRequest(Base):
    """Agenda items requested by board members for upcoming meetings."""
    __tablename__ = "agenda_requests"

    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    topic = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    scheduled_meeting_id = Column(Integer, ForeignKey("scheduled_meetings.id"), nullable=True)
    is_included = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class StatusReport(Base):
    """Pre-meeting status reports from members about their tasks."""
    __tablename__ = "status_reports"

    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    scheduled_meeting_id = Column(Integer, ForeignKey("scheduled_meetings.id"), nullable=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    status_text = Column(Text, nullable=False)  # free-form status update
    progress_percent = Column(Integer, nullable=True)  # 0-100
    created_at = Column(DateTime, default=datetime.utcnow)


class StrategicGoal(Base):
    """High-level strategic goals that tasks can be linked to."""
    __tablename__ = "strategic_goals"

    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    status = Column(String(20), default="active")  # active, completed, paused
    target_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MeetingMaterial(Base):
    """Presentation materials uploaded by board members after meetings."""
    __tablename__ = "meeting_materials"

    id = Column(Integer, primary_key=True)
    uploader_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=True)  # optional link
    file_id = Column(String(500), nullable=False)  # Telegram file_id for retrieval
    file_name = Column(String(500), nullable=True)
    file_type = Column(String(20), nullable=True)  # pdf, pptx, other
    description = Column(Text, nullable=True)  # from caption or user input
    created_at = Column(DateTime, default=datetime.utcnow)


class MeetingEmbedding(Base):
    """Stores text chunks and their embeddings for RAG search."""
    __tablename__ = "meeting_embeddings"

    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    chunk_text = Column(Text, nullable=False)
    chunk_index = Column(Integer, nullable=False)
    # Store embedding as JSON string for SQLite v1
    embedding = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


async def seed_members_from_config():
    """Pre-populate members table from BOARD_MEMBERS config.
    Members who haven't /start yet get a negative placeholder telegram_id.
    When they /start, onboarding will update their real telegram_id.
    """
    from app.members_config import BOARD_MEMBERS
    from sqlalchemy import select as _select

    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        for idx, cfg in enumerate(BOARD_MEMBERS, start=1):
            username = cfg.get("username")
            display_name = cfg["display_name"]

            # Try to find by username first, then by display_name
            existing = None
            if username:
                result = await session.execute(
                    _select(Member).where(Member.username == username)
                )
                existing = result.scalar_one_or_none()

            if not existing:
                result = await session.execute(
                    _select(Member).where(Member.display_name == display_name)
                )
                existing = result.scalar_one_or_none()

            if existing:
                # Update display_name if it changed in config
                if existing.display_name != display_name:
                    existing.display_name = display_name
                continue

            # Create placeholder record — negative id won't collide with real Telegram IDs
            placeholder_id = -(idx * 1000)
            # Make sure placeholder doesn't collide with an existing one
            while True:
                check = await session.execute(
                    _select(Member).where(Member.telegram_id == placeholder_id)
                )
                if not check.scalar_one_or_none():
                    break
                placeholder_id -= 1

            member = Member(
                telegram_id=placeholder_id,
                username=username,
                display_name=display_name,
                is_chairman=cfg.get("is_chairman", False),
                is_active=True,
            )
            session.add(member)

        await session.commit()


def compute_transcript_hash(text: str) -> str:
    """Normalized hash of transcript text for deduplication."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_db()


async def _migrate_db():
    """Add new columns to existing tables (safe, idempotent)."""
    import aiosqlite
    db_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "")
    if not db_path.startswith("/"):
        db_path = db_path  # relative path
    try:
        async with aiosqlite.connect(db_path) as db:
            for sql in [
                "ALTER TABLE members ADD COLUMN is_stakeholder BOOLEAN DEFAULT FALSE",
                "ALTER TABLE tasks ADD COLUMN source VARCHAR(20) DEFAULT 'manual'",
                "ALTER TABLE tasks ADD COLUMN created_by_id INTEGER REFERENCES members(id)",
                "ALTER TABLE meetings ADD COLUMN analysis_json TEXT",
                "ALTER TABLE tasks ADD COLUMN is_verified BOOLEAN DEFAULT TRUE",
                "ALTER TABLE meetings ADD COLUMN transcript_hash VARCHAR(16)",
                "ALTER TABLE tasks ADD COLUMN completion_comment TEXT",
                "ALTER TABLE tasks ADD COLUMN last_notified_at DATETIME",
                "ALTER TABLE task_comments ADD COLUMN comment_type VARCHAR(20) DEFAULT 'comment'",
                "CREATE TABLE IF NOT EXISTS meeting_materials (id INTEGER PRIMARY KEY, uploader_id INTEGER REFERENCES members(id), meeting_id INTEGER REFERENCES meetings(id), file_id VARCHAR(500) NOT NULL, file_name VARCHAR(500), file_type VARCHAR(20), description TEXT, created_at DATETIME)",
            ]:
                try:
                    await db.execute(sql)
                except Exception:
                    pass  # column already exists
            await db.commit()

        # Migrate task_comments: make author_id nullable, add author_email
        try:
            cols = await db.execute("PRAGMA table_info(task_comments)")
            col_names = {row[1] for row in await cols.fetchall()}
            if col_names and 'author_email' not in col_names:
                await db.execute("""
                    CREATE TABLE task_comments_new (
                        id INTEGER PRIMARY KEY,
                        task_id INTEGER NOT NULL REFERENCES tasks(id),
                        author_id INTEGER REFERENCES members(id),
                        author_email TEXT,
                        text TEXT NOT NULL,
                        created_at DATETIME
                    )
                """)
                await db.execute("""
                    INSERT INTO task_comments_new (id, task_id, author_id, text, created_at)
                    SELECT id, task_id, author_id, text, created_at FROM task_comments
                """)
                await db.execute("DROP TABLE task_comments")
                await db.execute("ALTER TABLE task_comments_new RENAME TO task_comments")
                await db.commit()
        except Exception:
            pass
    except Exception:
        pass  # DB might not exist yet (first run)
