"""FastAPI web application — Board of Directors AI Secretary."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, func, delete as sql_delete

# Reuse existing DB models
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import (
    async_session, init_db,
    Member, Meeting, Task, TaskComment, ScheduledMeeting, AgendaRequest, UserActivity,
)
from webapp.auth import verify_credentials, get_current_user, is_chairman_email


async def _notify_assignee_tg(task_id: int, task_title: str, assignee: Member, deadline_str: str, creator_email: str = ""):
    """Send Telegram notification to assignee when task is created via web."""
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token or not assignee or not assignee.telegram_id or assignee.telegram_id < 0:
        return
    try:
        from aiogram import Bot
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from html import escape
        bot = Bot(token=bot_token)
        creator_note = f"\n👤 Поставил: {escape(creator_email.split('@')[0])}" if creator_email else ""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks")]
        ])
        await bot.send_message(
            assignee.telegram_id,
            f"🔔 <b>Новая задача назначена</b>\n\n"
            f"<b>#{task_id}</b> {escape(task_title)}\n"
            f"📅 Срок: {deadline_str}{creator_note}\n\n"
            f"<i>Задача добавлена в твой список.</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await bot.session.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"TG notify failed: {e}")

async def _notify_chairman_tg(task_id: int, task_title: str, new_status: str, changer_email: str):
    """Notify chairmen when a task status changes via the web panel."""
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        return
    status_phrases = {
        "in_progress": "взял(а) в работу",
        "done": "отметил(а) выполненным",
        "pending_done": "просит подтвердить выполнение",
    }
    phrase = status_phrases.get(new_status, f"изменил(а) статус → {new_status}")
    try:
        from aiogram import Bot
        from html import escape
        bot = Bot(token=bot_token)
        name = escape(changer_email.split("@")[0])
        msg = (
            f"🔔 <b>Обновление задачи #{task_id}</b>\n\n"
            f"{escape(task_title)}\n\n"
            f"👤 <b>{name}</b> {phrase}"
        )
        async with async_session() as session:
            chairmen = (await session.execute(
                select(Member).where(Member.is_chairman == True)
            )).scalars().all()
        for ch in chairmen:
            if ch.telegram_id and ch.telegram_id > 0:
                try:
                    await bot.send_message(ch.telegram_id, msg, parse_mode="HTML")
                except Exception:
                    pass
        await bot.session.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Chairman TG notify failed: {e}")


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


@app.get("/api/env-check")
async def env_check(user: str = Depends(get_current_user)):
    """Debug: show which env vars are set (values hidden)."""
    keys = ["CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BOT_TOKEN", "PORT"]
    return {k: "set" if os.getenv(k) else "MISSING" for k in keys}


@app.get("/api/me")
async def get_me(user: str = Depends(get_current_user)):
    return {"email": user, "is_chairman": is_chairman_email(user)}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def dashboard(user: str = Depends(get_current_user)):
    from datetime import timedelta
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    async with async_session() as session:
        all_tasks = (await session.execute(select(Task))).scalars().all()

        total_tasks = len(all_tasks)
        done_tasks = sum(1 for t in all_tasks if t.status == "done")
        overdue_tasks = sum(1 for t in all_tasks if t.status == "overdue")
        in_progress_tasks = sum(1 for t in all_tasks if t.status == "in_progress")
        new_tasks = sum(1 for t in all_tasks if t.status == "new")
        pending_tasks = sum(1 for t in all_tasks if t.status == "pending_done")

        # Protocol stats
        all_meetings = (await session.execute(
            select(Meeting).where(Meeting.is_confirmed == True).order_by(Meeting.date.desc())
        )).scalars().all()
        total_meetings = len(all_meetings)
        last_meeting = all_meetings[0] if all_meetings else None
        tasks_from_meetings = sum(1 for t in all_tasks if t.meeting_id is not None)
        done_from_meetings = sum(1 for t in all_tasks if t.meeting_id is not None and t.status == "done")

        # Next scheduled meeting
        next_meeting = (await session.execute(
            select(ScheduledMeeting)
            .where(ScheduledMeeting.is_completed == False)
            .order_by(ScheduledMeeting.scheduled_date.asc())
            .limit(1)
        )).scalar_one_or_none()

        agenda_count = 0
        if next_meeting:
            agenda_count = (await session.execute(
                select(func.count(AgendaRequest.id))
                .where(AgendaRequest.scheduled_meeting_id == next_meeting.id)
                .where(AgendaRequest.is_included == False)
            )).scalar() or 0

        # Overdue by person
        overdue_rows = (await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status == "overdue")
        )).all()
        overdue_by_person = {}
        for t, m in overdue_rows:
            name = (m.display_name or m.first_name or "?") if m else "?"
            overdue_by_person[name] = overdue_by_person.get(name, 0) + 1

        # Tasks due this week
        week_end = now + timedelta(days=7)
        upcoming_count = (await session.execute(
            select(func.count(Task.id)).where(
                Task.status.in_(["new", "in_progress"]),
                Task.deadline != None,
                Task.deadline <= week_end,
                Task.deadline > now,
            )
        )).scalar() or 0

        # Engagement
        all_members = (await session.execute(
            select(Member).where(Member.is_active == True)
        )).scalars().all()

        activity_rows = (await session.execute(
            select(
                UserActivity.telegram_id,
                func.count(UserActivity.id).label("cnt"),
            )
            .where(UserActivity.created_at >= week_ago)
            .group_by(UserActivity.telegram_id)
        )).all()
        activity_map = {row[0]: row[1] for row in activity_rows}

        engagement = []
        for m in all_members:
            if m.telegram_id <= 0:
                status = "not_connected"
                actions = 0
            else:
                actions = activity_map.get(m.telegram_id, 0)
                status = "active" if actions > 5 else ("low" if actions > 0 else "inactive")
            engagement.append({
                "name": m.display_name or m.first_name or m.username or "?",
                "username": m.username,
                "status": status,
                "actions": actions,
            })
        engagement.sort(key=lambda x: -x["actions"] if x["status"] != "not_connected" else -999)

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

    return {
        "stats": {
            "total_tasks": total_tasks,
            "done_tasks": done_tasks,
            "overdue_tasks": overdue_tasks,
            "in_progress_tasks": in_progress_tasks,
            "new_tasks": new_tasks,
            "pending_tasks": pending_tasks,
            "total_meetings": total_meetings,
            "tasks_from_meetings": tasks_from_meetings,
            "done_from_meetings": done_from_meetings,
            "upcoming_this_week": upcoming_count,
        },
        "last_meeting": {
            "date": last_meeting.date.isoformat(),
            "title": last_meeting.title,
            "tasks_count": sum(1 for t in all_tasks if t.meeting_id == last_meeting.id),
        } if last_meeting else None,
        "next_meeting": {
            "date": next_meeting.scheduled_date.isoformat(),
            "title": next_meeting.title,
            "agenda_requests": agenda_count,
        } if next_meeting else None,
        "overdue_by_person": [
            {"name": name, "count": cnt}
            for name, cnt in sorted(overdue_by_person.items(), key=lambda x: -x[1])
        ],
        "engagement": engagement,
        "recent_tasks": recent_tasks,
    }


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.get("/api/tasks")
async def get_tasks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assignee_id: Optional[int] = None,
    source: Optional[str] = None,
    user: str = Depends(get_current_user),
):
    async with async_session() as session:
        q = (
            select(Task, Member, Meeting)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .outerjoin(Meeting, Task.meeting_id == Meeting.id)
        )
        if status:
            q = q.where(Task.status == status)
        if priority:
            q = q.where(Task.priority == priority)
        if assignee_id:
            q = q.where(Task.assignee_id == assignee_id)
        if source:
            q = q.where(Task.source == source)
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
                "meeting_id": t.meeting_id,
                "meeting_title": mtg.title if mtg else None,
                "source": t.source,
                "progress_percent": t.progress_percent,
                "created_at": t.created_at.isoformat(),
            }
            for t, m, mtg in result.all()
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

    old_status = None
    task_title = None
    async with async_session() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id)
        )).scalar_one_or_none()
        if not task:
            raise HTTPException(404, "Задача не найдена")

        old_status = task.status
        task_title = task.title
        for k, v in updates.items():
            if k == "deadline" and v:
                v = datetime.fromisoformat(v)
            if k == "status" and v == "done":
                task.completed_at = datetime.utcnow()
                task.progress_percent = 100
            setattr(task, k, v)
        await session.commit()

    new_status = updates.get("status")
    if new_status and new_status != old_status:
        import asyncio
        asyncio.create_task(_notify_chairman_tg(task_id, task_title, new_status, user))

    return {"ok": True}


class BulkTaskBody(BaseModel):
    ids: list[int]
    action: str  # "delete" | "done"


@app.post("/api/tasks/bulk")
async def bulk_tasks(body: BulkTaskBody, user: str = Depends(get_current_user)):
    if body.action not in ("delete", "done"):
        raise HTTPException(400, "Неверное действие")
    if not body.ids:
        raise HTTPException(400, "Список задач пустой")
    async with async_session() as session:
        if body.action == "delete":
            for tid in body.ids:
                await session.execute(sql_delete(TaskComment).where(TaskComment.task_id == tid))
            await session.execute(sql_delete(Task).where(Task.id.in_(body.ids)))
        else:
            tasks = (await session.execute(
                select(Task).where(Task.id.in_(body.ids))
            )).scalars().all()
            for t in tasks:
                t.status = "done"
                t.progress_percent = 100
                t.completed_at = datetime.utcnow()
        await session.commit()
    return {"ok": True, "count": len(body.ids)}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, user: str = Depends(get_current_user)):
    async with async_session() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id)
        )).scalar_one_or_none()
        if not task:
            raise HTTPException(404, "Задача не найдена")
        # Use raw SQL to avoid async ORM lazy-loading issues with relationships
        await session.execute(sql_delete(TaskComment).where(TaskComment.task_id == task_id))
        await session.execute(sql_delete(Task).where(Task.id == task_id))
        await session.commit()
    return {"ok": True}


# ── Task comments ─────────────────────────────────────────────────────────────

@app.get("/api/tasks/{task_id}/comments")
async def get_task_comments(task_id: int, user: str = Depends(get_current_user)):
    async with async_session() as session:
        result = await session.execute(
            select(TaskComment, Member)
            .outerjoin(Member, TaskComment.author_id == Member.id)
            .where(TaskComment.task_id == task_id)
            .order_by(TaskComment.created_at.asc())
        )
        comments = [
            {
                "id": c.id,
                "text": c.text,
                "comment_type": c.comment_type or "comment",
                "author": (m.name if m else None) or c.author_email or "Пользователь",
                "created_at": c.created_at.isoformat(),
            }
            for c, m in result.all()
        ]
    return {"comments": comments}


class AddCommentBody(BaseModel):
    text: str


@app.post("/api/tasks/{task_id}/comments")
async def add_task_comment(
    task_id: int,
    body: AddCommentBody,
    user: str = Depends(get_current_user),
):
    if not body.text.strip():
        raise HTTPException(400, "Пустой комментарий")
    async with async_session() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id)
        )).scalar_one_or_none()
        if not task:
            raise HTTPException(404, "Задача не найдена")
        comment = TaskComment(
            task_id=task_id,
            author_id=None,
            author_email=user,
            text=body.text.strip(),
        )
        session.add(comment)
        await session.commit()
        await session.refresh(comment)
    return {"id": comment.id, "ok": True}


# ── Create task (web) ─────────────────────────────────────────────────────────

class CreateTaskBody(BaseModel):
    title: str
    description: Optional[str] = None
    assignee_id: Optional[int] = None
    priority: str = "medium"
    deadline: Optional[str] = None
    source: Optional[str] = None  # "manual" | "voice"

@app.post("/api/tasks")
async def create_task_web(body: CreateTaskBody, user: str = Depends(get_current_user)):
    async with async_session() as session:
        deadline_dt = datetime.fromisoformat(body.deadline) if body.deadline else None
        task = Task(
            title=body.title,
            description=body.description or None,
            assignee_id=body.assignee_id or None,
            priority=body.priority,
            deadline=deadline_dt,
            status="new",
            source=body.source if hasattr(body, 'source') and body.source else "manual",
            is_verified=True,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None
    deadline_str = deadline_dt.strftime("%d.%m.%Y") if deadline_dt else "без срока"
    import asyncio
    asyncio.create_task(_notify_assignee_tg(task.id, task.title, assignee, deadline_str, user))
    return {"id": task.id, "ok": True}


# ── Voice transcription ────────────────────────────────────────────────────────

@app.post("/api/transcribe")
async def transcribe_audio(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    import openai, tempfile, asyncio
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY не настроен")
    ct = file.content_type or ""
    ext = ".mp4" if ("mp4" in ct or "mpeg" in ct or "m4a" in ct) else ".webm"
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        def _transcribe():
            client = openai.OpenAI(api_key=api_key)
            with open(tmp_path, "rb") as f:
                return client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
        result = await asyncio.to_thread(_transcribe)
        return {"text": result.text}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        try: os.unlink(tmp_path)
        except: pass


# ── Parse voice task ──────────────────────────────────────────────────────────

class VoiceParseBody(BaseModel):
    text: str

@app.post("/api/voice/parse")
async def parse_voice_task(body: VoiceParseBody, user: str = Depends(get_current_user)):
    import anthropic as _anthropic
    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "CLAUDE_API_KEY не настроен в Railway")
    client = _anthropic.AsyncAnthropic(api_key=api_key)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    prompt = f"""Из голосового поручения извлеки данные задачи. Верни ТОЛЬКО JSON, без объяснений.

Сегодня: {today}

Поручение: «{body.text}»

Верни JSON:
{{
  "title": "краткое название задачи (до 80 символов)",
  "description": "подробное описание что нужно сделать (если есть детали)",
  "assignee_name": "имя ответственного или null",
  "priority": "high|medium|low",
  "deadline": "YYYY-MM-DD или null"
}}

Правила:
- title — конкретное действие, без воды
- description — только если есть детали сверх title, иначе null
- priority high если есть слова: срочно, немедленно, ASAP, сегодня, важно
- deadline — вычисли дату если сказано "до пятницы", "через неделю" и т.п.
- assignee_name — только имя/фамилия если явно указан ответственный"""
    msg = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"): raw = raw[4:].lstrip()
    try:
        parsed = json.loads(raw)
    except Exception:
        raise HTTPException(500, "Не удалось разобрать ответ AI")
    return {"task": parsed, "ok": True}


# ── Analyze meeting transcript ─────────────────────────────────────────────────

class AnalyzeMeetingBody(BaseModel):
    title: Optional[str] = None
    date: Optional[str] = None
    transcript: str

@app.post("/api/meetings/analyze")
async def analyze_meeting_web(body: AnalyzeMeetingBody, user: str = Depends(get_current_user)):
    import anthropic as _anthropic
    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "CLAUDE_API_KEY не настроен в Railway")
    client = _anthropic.AsyncAnthropic(api_key=api_key)
    prompt = f"""Проанализируй транскрипт совещания и верни ТОЛЬКО JSON без лишнего текста.

Транскрипт:
{body.transcript[:8000]}

JSON структура:
{{
  "title": "краткое название совещания",
  "summary": "резюме 2-3 предложения",
  "participants": "список участников через запятую",
  "decisions": ["решение 1", "решение 2"],
  "open_questions": ["вопрос 1"],
  "tasks": [
    {{"title": "задача", "description": "детали", "assignee_name": "имя или null", "deadline": "YYYY-MM-DD или null", "priority": "high|medium|low"}}
  ]
}}"""
    msg = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"): raw = raw[4:].lstrip()
    try:
        analysis = json.loads(raw)
    except Exception:
        raise HTTPException(500, "Не удалось разобрать ответ AI")
    return {"analysis": analysis, "ok": True}


@app.post("/api/meetings/save")
async def save_meeting_web(body: dict, user: str = Depends(get_current_user)):
    analysis = body.get("analysis", {})
    transcript = body.get("transcript", "")
    date_str = body.get("date") or datetime.utcnow().isoformat()
    async with async_session() as session:
        meeting = Meeting(
            date=datetime.fromisoformat(date_str[:10]),
            title=analysis.get("title") or body.get("title") or "Совещание",
            raw_transcript=transcript,
            summary=analysis.get("summary"),
            participants=analysis.get("participants"),
            decisions=json.dumps(analysis.get("decisions", []), ensure_ascii=False),
            open_questions=json.dumps(analysis.get("open_questions", []), ensure_ascii=False),
            is_confirmed=True,
        )
        session.add(meeting)
        await session.flush()
        tasks_count = 0
        tasks_to_notify = []
        for t in analysis.get("tasks", []):
            assignee_id = None
            aname = t.get("assignee_name")
            if aname and aname != "null":
                r = await session.execute(select(Member).where(Member.display_name.ilike(f"%{aname}%")))
                m = r.scalar_one_or_none()
                if not m:
                    r = await session.execute(select(Member).where(Member.first_name.ilike(f"%{aname}%")))
                    m = r.scalar_one_or_none()
                if m: assignee_id = m.id
            dl = None
            if t.get("deadline") and t["deadline"] not in (None, "null"):
                try: dl = datetime.fromisoformat(t["deadline"])
                except: pass
            new_task = Task(
                meeting_id=meeting.id, title=t["title"],
                description=t.get("description"), assignee_id=assignee_id,
                priority=t.get("priority", "medium"), deadline=dl,
                status="new", source="meeting", is_verified=True,
            )
            session.add(new_task)
            tasks_to_notify.append((new_task, assignee_id, dl))
            tasks_count += 1
        await session.commit()
        # Collect assignee objects for notifications
        notify_list = []
        for new_task, aid, dl in tasks_to_notify:
            await session.refresh(new_task)
            assignee_obj = await session.get(Member, aid) if aid else None
            notify_list.append((new_task.id, new_task.title, assignee_obj, dl))
    import asyncio
    for tid, ttitle, assignee_obj, dl in notify_list:
        dl_str = dl.strftime("%d.%m.%Y") if dl else "без срока"
        asyncio.create_task(_notify_assignee_tg(tid, ttitle, assignee_obj, dl_str, user))
    return {"meeting_id": meeting.id, "tasks_created": tasks_count, "ok": True}


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


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: int, user: str = Depends(get_current_user)):
    async with async_session() as session:
        meeting = (await session.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )).scalar_one_or_none()
        if not meeting:
            raise HTTPException(404, "Совещание не найдено")
        task_ids = (await session.execute(
            select(Task.id).where(Task.meeting_id == meeting_id)
        )).scalars().all()
        for tid in task_ids:
            await session.execute(sql_delete(TaskComment).where(TaskComment.task_id == tid))
        await session.execute(sql_delete(Task).where(Task.meeting_id == meeting_id))
        await session.execute(sql_delete(Meeting).where(Meeting.id == meeting_id))
        await session.commit()
    return {"ok": True}


@app.patch("/api/meetings/{meeting_id}")
async def update_meeting(meeting_id: int, body: dict, user: str = Depends(get_current_user)):
    allowed_fields = {"date", "title"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        raise HTTPException(400, "Нет допустимых полей")
    async with async_session() as session:
        meeting = (await session.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )).scalar_one_or_none()
        if not meeting:
            raise HTTPException(404, "Совещание не найдено")
        for k, v in updates.items():
            if k == "date" and v:
                from datetime import date as date_cls
                try:
                    v = datetime.fromisoformat(v).date()
                except Exception:
                    v = date_cls.fromisoformat(v)
            setattr(meeting, k, v)
        await session.commit()
    return {"ok": True}


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
                "duration_minutes": r.duration_minutes,
                "member": m.name,
                "is_included": r.is_included,
                "created_at": r.created_at.isoformat(),
            }
            for r, m in result.all()
        ]
    return {"requests": items}


# ── Workload ──────────────────────────────────────────────────────────────────

@app.get("/api/workload")
async def get_workload(user: str = Depends(get_current_user)):
    async with async_session() as session:
        members = (await session.execute(
            select(Member).where(Member.is_active == True)
        )).scalars().all()

        open_rows = (await session.execute(
            select(Task).where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
        )).scalars().all()

        done_tasks = (await session.execute(
            select(Task).where(Task.status == "done")
        )).scalars().all()

    open_by_member: dict[int, list] = {}
    for t in open_rows:
        if t.assignee_id:
            open_by_member.setdefault(t.assignee_id, []).append(t)

    done_by_member: dict[int, int] = {}
    for t in done_tasks:
        if t.assignee_id:
            done_by_member[t.assignee_id] = done_by_member.get(t.assignee_id, 0) + 1

    workload = []
    for m in members:
        open_tasks = open_by_member.get(m.id, [])
        sorted_tasks = sorted(
            open_tasks,
            key=lambda t: (t.status != "overdue", t.deadline or datetime(9999, 1, 1))
        )
        workload.append({
            "member_id": m.id,
            "name": m.name,
            "username": m.username,
            "is_chairman": m.is_chairman,
            "is_stakeholder": m.is_stakeholder,
            "open": len(open_tasks),
            "done_total": done_by_member.get(m.id, 0),
            "by_status": {
                "new": sum(1 for t in open_tasks if t.status == "new"),
                "in_progress": sum(1 for t in open_tasks if t.status == "in_progress"),
                "overdue": sum(1 for t in open_tasks if t.status == "overdue"),
                "pending_done": sum(1 for t in open_tasks if t.status == "pending_done"),
            },
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "priority": t.priority,
                    "deadline": t.deadline.isoformat() if t.deadline else None,
                }
                for t in sorted_tasks
            ],
        })

    workload.sort(key=lambda x: (-x["open"], x["name"]))
    return {"workload": workload}


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/{path:path}", response_class=HTMLResponse)
async def serve_spa(path: str = ""):
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)
