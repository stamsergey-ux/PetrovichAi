"""FastAPI web application — Board of Directors AI Secretary."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, func

# Reuse existing DB models
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import (
    async_session, init_db,
    Member, Meeting, Task, TaskComment, ScheduledMeeting, AgendaRequest,
)
from webapp.auth import verify_credentials, get_current_user

app = FastAPI(title="AI Secretary — Web", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"


# ── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    await init_db()


# ── Auth ─────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/login")
async def login(body: LoginRequest):
    token = verify_credentials(body.email, body.password)
    return {"token": token, "email": body.email.lower()}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def dashboard(user: str = Depends(get_current_user)):
    async with async_session() as session:
        total_tasks = (await session.execute(
            select(func.count(Task.id))
        )).scalar()

        done_tasks = (await session.execute(
            select(func.count(Task.id)).where(Task.status == "done")
        )).scalar()

        overdue_tasks = (await session.execute(
            select(func.count(Task.id)).where(Task.status == "overdue")
        )).scalar()

        in_progress_tasks = (await session.execute(
            select(func.count(Task.id)).where(Task.status == "in_progress")
        )).scalar()

        total_meetings = (await session.execute(
            select(func.count(Meeting.id))
        )).scalar()

        # Recent tasks (last 5)
        recent_tasks_result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .order_by(Task.created_at.desc())
            .limit(5)
        )
        recent_tasks = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "deadline": t.deadline.isoformat() if t.deadline else None,
                "assignee": m.name if m else None,
            }
            for t, m in recent_tasks_result.all()
        ]

        # Recent meetings (last 3)
        recent_meetings_result = await session.execute(
            select(Meeting).order_by(Meeting.date.desc()).limit(3)
        )
        recent_meetings = [
            {
                "id": m.id,
                "title": m.title,
                "date": m.date.isoformat(),
            }
            for m in recent_meetings_result.scalars().all()
        ]

    return {
        "stats": {
            "total_tasks": total_tasks,
            "done_tasks": done_tasks,
            "overdue_tasks": overdue_tasks,
            "in_progress_tasks": in_progress_tasks,
            "total_meetings": total_meetings,
        },
        "recent_tasks": recent_tasks,
        "recent_meetings": recent_meetings,
    }


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.get("/api/tasks")
async def get_tasks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assignee_id: Optional[int] = None,
    user: str = Depends(get_current_user),
):
    async with async_session() as session:
        q = select(Task, Member).outerjoin(Member, Task.assignee_id == Member.id)
        if status:
            q = q.where(Task.status == status)
        if priority:
            q = q.where(Task.priority == priority)
        if assignee_id:
            q = q.where(Task.assignee_id == assignee_id)
        q = q.order_by(Task.created_at.desc())
        result = await session.execute(q)
        tasks = [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": t.status,
                "priority": t.priority,
                "deadline": t.deadline.isoformat() if t.deadline else None,
                "assignee": m.name if m else None,
                "assignee_id": t.assignee_id,
                "source": t.source,
                "progress_percent": t.progress_percent,
                "created_at": t.created_at.isoformat(),
            }
            for t, m in result.all()
        ]
    return {"tasks": tasks}


@app.patch("/api/tasks/{task_id}")
async def update_task(
    task_id: int,
    body: dict,
    user: str = Depends(get_current_user),
):
    allowed_fields = {"status", "progress_percent", "priority", "deadline"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        raise HTTPException(400, "Нет допустимых полей для обновления")

    async with async_session() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id)
        )).scalar_one_or_none()
        if not task:
            raise HTTPException(404, "Задача не найдена")

        for k, v in updates.items():
            if k == "deadline" and v:
                v = datetime.fromisoformat(v)
            if k == "status" and v == "done":
                task.completed_at = datetime.utcnow()
                task.progress_percent = 100
            setattr(task, k, v)
        await session.commit()

    return {"ok": True}


# ── Members ───────────────────────────────────────────────────────────────────

@app.get("/api/members")
async def get_members(user: str = Depends(get_current_user)):
    async with async_session() as session:
        result = await session.execute(
            select(Member).where(Member.is_active == True).order_by(Member.first_name)
        )
        members = [
            {
                "id": m.id,
                "name": m.name,
                "username": m.username,
                "is_chairman": m.is_chairman,
                "is_stakeholder": m.is_stakeholder,
            }
            for m in result.scalars().all()
        ]
    return {"members": members}


# ── Meetings / Protocols ───────────────────────────────────────────────────────

@app.get("/api/meetings")
async def get_meetings(user: str = Depends(get_current_user)):
    async with async_session() as session:
        result = await session.execute(
            select(Meeting).order_by(Meeting.date.desc())
        )
        meetings = [
            {
                "id": m.id,
                "title": m.title,
                "date": m.date.isoformat(),
                "participants": m.participants,
                "is_confirmed": m.is_confirmed,
            }
            for m in result.scalars().all()
        ]
    return {"meetings": meetings}


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: int, user: str = Depends(get_current_user)):
    async with async_session() as session:
        meeting = (await session.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )).scalar_one_or_none()
        if not meeting:
            raise HTTPException(404, "Совещание не найдено")

        tasks_result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.meeting_id == meeting_id)
        )

        tasks = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "assignee": m.name if m else None,
                "deadline": t.deadline.isoformat() if t.deadline else None,
            }
            for t, m in tasks_result.all()
        ]

        decisions = []
        open_questions = []
        try:
            if meeting.decisions:
                decisions = json.loads(meeting.decisions)
        except Exception:
            pass
        try:
            if meeting.open_questions:
                open_questions = json.loads(meeting.open_questions)
        except Exception:
            pass

    return {
        "id": meeting.id,
        "title": meeting.title,
        "date": meeting.date.isoformat(),
        "participants": meeting.participants,
        "summary": meeting.summary,
        "decisions": decisions,
        "open_questions": open_questions,
        "tasks": tasks,
        "is_confirmed": meeting.is_confirmed,
    }


# ── Agenda / Scheduled meetings ───────────────────────────────────────────────

@app.get("/api/scheduled")
async def get_scheduled(user: str = Depends(get_current_user)):
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledMeeting)
            .where(ScheduledMeeting.is_completed == False)
            .order_by(ScheduledMeeting.scheduled_date)
        )
        meetings = [
            {
                "id": m.id,
                "title": m.title,
                "scheduled_date": m.scheduled_date.isoformat(),
                "agenda_text": m.agenda_text,
                "agenda_sent": m.agenda_sent,
            }
            for m in result.scalars().all()
        ]
    return {"scheduled": meetings}


@app.get("/api/agenda-requests")
async def get_agenda_requests(user: str = Depends(get_current_user)):
    async with async_session() as session:
        result = await session.execute(
            select(AgendaRequest, Member)
            .join(Member, AgendaRequest.member_id == Member.id)
            .order_by(AgendaRequest.created_at.desc())
            .limit(20)
        )
        items = [
            {
                "id": r.id,
                "topic": r.topic,
                "reason": r.reason,
                "member": m.name,
                "is_included": r.is_included,
                "created_at": r.created_at.isoformat(),
            }
            for r, m in result.all()
        ]
    return {"requests": items}


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/{path:path}", response_class=HTMLResponse)
async def serve_spa(path: str = ""):
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)
