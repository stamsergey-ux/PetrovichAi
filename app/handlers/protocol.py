"""Protocol upload and AI analysis handlers."""
from __future__ import annotations

import json
import io
import logging
import traceback
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.database import async_session, Meeting, Task, Member
from app.ai_service import analyze_transcript
from app.rag import store_meeting_chunks
from app.utils import is_chairman

logger = logging.getLogger(__name__)
router = Router()

# Storage for pending reviews: {chairman_telegram_id: [list of reviews]}
_pending_reviews: dict[int, list] = {}


def _escape_md(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for ch in special:
        text = text.replace(ch, f'\\{ch}')
    return text


async def _get_members_list() -> str:
    """Get comma-separated list of known member names."""
    async with async_session() as session:
        result = await session.execute(select(Member))
        members = result.scalars().all()
    if not members:
        return "No members registered yet"
    names = []
    for m in members:
        parts = [m.display_name, m.first_name, m.username]
        name = next((p for p in parts if p), f"ID:{m.telegram_id}")
        names.append(name)
    return ", ".join(names)


async def _extract_text_from_file(message: Message, bot: Bot) -> str | None:
    """Extract text from uploaded file (txt or pdf)."""
    doc = message.document
    if not doc:
        return None

    file = await bot.download(doc)
    content = file.read()

    if doc.file_name and doc.file_name.endswith(".pdf"):
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(content))
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text
        except Exception as e:
            logger.error(f"PDF parse error: {e}")
            return None
    else:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="ignore")


def _review_keyboard(review_index: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_protocol:{review_index}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_protocol:{review_index}"),
        ]
    ])


@router.message(F.document)
async def handle_document(message: Message, bot: Bot):
    """Handle uploaded document (transcript from Plaud)."""
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Загрузка протоколов доступна только администраторам.")
        return

    filename = message.document.file_name or "файл"
    await message.answer(f"📎 Получил файл \"{filename}\", обрабатываю...")

    try:
        text = await _extract_text_from_file(message, bot)
        if not text or len(text.strip()) < 50:
            await message.answer(
                f"⚠️ Не удалось извлечь текст из \"{filename}\" "
                f"({len(text) if text else 0} символов). "
                f"Попробуй отправить как текстовое сообщение."
            )
            return

        await _process_transcript(message, text, filename)
    except Exception as e:
        logger.error(f"Error processing document: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка при обработке файла: {e}")


@router.message(F.text & F.text.func(lambda t: len(t) > 500))
async def handle_long_text(message: Message):
    """Handle long text messages as potential transcripts (only from chairman)."""
    if not is_chairman(message.from_user.username):
        return  # Will fall through to AI chat handler
    text_lower = message.text.lower()
    transcript_markers = ["совещание", "встреча", "протокол", "участники", "тема:", "следующие шаги"]
    if any(marker in text_lower for marker in transcript_markers):
        await message.answer("📝 Похоже на протокол совещания. Обрабатываю...")
        await _process_transcript(message, message.text, "текстовое сообщение")


async def _process_transcript(message: Message, transcript: str, source_name: str):
    """Process transcript through AI and present for review."""
    members_list = await _get_members_list()

    await message.answer("🤖 Анализирую транскрипт через AI... Это может занять до минуты.")

    try:
        analysis = await analyze_transcript(transcript, members_list)
    except Exception as e:
        logger.error(f"AI analysis error: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка AI-анализа: {e}")
        return

    if "error" in analysis:
        await message.answer(f"⚠️ Ошибка анализа: {analysis['error']}")
        return

    # Store for review
    user_id = message.from_user.id
    if user_id not in _pending_reviews:
        _pending_reviews[user_id] = []

    review_index = len(_pending_reviews[user_id])
    _pending_reviews[user_id].append({
        "transcript": transcript,
        "analysis": analysis,
        "source": source_name,
    })

    # Format review message (plain text — more reliable for long AI output)
    tasks = analysis.get("tasks", [])
    decisions = analysis.get("decisions", [])
    open_q = analysis.get("open_questions", [])
    agenda = analysis.get("agenda_next", [])

    title = analysis.get("title", "Без названия")
    date = analysis.get("date", "дата не определена")
    summary = analysis.get("summary", "")

    review_text = f"📝 ПРОТОКОЛ НА ПРОВЕРКУ\n\n"
    review_text += f"📌 {title}\n"
    review_text += f"📅 {date}  ·  📎 {source_name}\n\n"
    review_text += f"💡 Краткое содержание:\n{summary}\n"

    if tasks:
        review_text += f"\n{'─' * 30}\n"
        review_text += f"✅ ЗАДАЧИ ({len(tasks)})\n\n"
        for i, t in enumerate(tasks, 1):
            assignee = t.get("assignee_name", "не определён")
            deadline = t.get("deadline", "без срока")
            priority = t.get("priority", "medium")
            p_icon = {"high": "🔥", "medium": "▫️", "low": "💤"}.get(priority, "▫️")
            review_text += f"  {p_icon} {i}. {t['title']}\n"
            review_text += f"       👤 {assignee}  📅 {deadline}\n\n"

    if decisions:
        review_text += f"{'─' * 30}\n"
        review_text += f"⚖️ РЕШЕНИЯ ({len(decisions)})\n\n"
        for d in decisions:
            review_text += f"  • {d['text']}\n"
        review_text += "\n"

    if open_q:
        review_text += f"{'─' * 30}\n"
        review_text += f"❓ ОТКРЫТЫЕ ВОПРОСЫ ({len(open_q)})\n\n"
        for q in open_q:
            review_text += f"  • {q['text']}\n"
        review_text += "\n"

    if agenda:
        review_text += f"{'─' * 30}\n"
        review_text += f"📌 НА СЛЕДУЮЩЕЕ СОВЕЩАНИЕ ({len(agenda)})\n\n"
        for a in agenda:
            presenter = a.get("presenter", "?")
            mins = a.get("estimated_minutes", "?")
            review_text += f"  • [{mins} мин] {presenter}: {a['topic']}\n"

    # Telegram message limit is 4096 chars
    if len(review_text) > 4000:
        chunks = [review_text[i:i+4000] for i in range(0, len(review_text), 4000)]
        for chunk in chunks[:-1]:
            await message.answer(chunk)
        await message.answer(chunks[-1], reply_markup=_review_keyboard(review_index))
    else:
        await message.answer(review_text, reply_markup=_review_keyboard(review_index))


@router.callback_query(F.data.startswith("confirm_protocol:"))
async def confirm_protocol(callback: CallbackQuery):
    """Chairman confirms the analyzed protocol."""
    user_id = callback.from_user.id
    review_index = int(callback.data.split(":")[1])

    reviews = _pending_reviews.get(user_id, [])
    if review_index >= len(reviews) or reviews[review_index] is None:
        await callback.answer("Этот протокол уже обработан.")
        return

    review = reviews[review_index]
    reviews[review_index] = None  # Mark as processed

    analysis = review["analysis"]
    transcript = review["transcript"]

    try:
        async with async_session() as session:
            # Save meeting
            meeting_date_str = analysis.get("date")
            meeting_date = datetime.now()
            if meeting_date_str:
                try:
                    meeting_date = datetime.fromisoformat(meeting_date_str)
                except ValueError:
                    pass

            meeting = Meeting(
                date=meeting_date,
                title=analysis.get("title", ""),
                raw_transcript=transcript,
                summary=analysis.get("summary", ""),
                participants=", ".join(analysis.get("participants", [])),
                decisions=json.dumps(analysis.get("decisions", []), ensure_ascii=False),
                open_questions=json.dumps(analysis.get("open_questions", []), ensure_ascii=False),
                agenda_items_next=json.dumps(analysis.get("agenda_next", []), ensure_ascii=False),
                is_confirmed=True,
            )
            session.add(meeting)
            await session.flush()

            # Save tasks
            tasks_created = 0
            tasks_unassigned = 0
            for t in analysis.get("tasks", []):
                assignee = None
                assignee_name = t.get("assignee_name") or "не определён"

                if assignee_name and assignee_name != "не определён":
                    result = await session.execute(select(Member))
                    all_members = result.scalars().all()
                    assignee = _fuzzy_match_member(assignee_name, all_members)

                if not assignee:
                    tasks_unassigned += 1

                deadline = None
                if t.get("deadline"):
                    try:
                        deadline = datetime.fromisoformat(t["deadline"])
                    except ValueError:
                        pass

                task = Task(
                    meeting_id=meeting.id,
                    assignee_id=assignee.id if assignee else None,
                    title=t["title"],
                    description=f"Ответственный (из транскрипта): {assignee_name}",
                    context_quote=t.get("context_quote"),
                    priority=t.get("priority", "medium"),
                    status="new",
                    deadline=deadline,
                )
                session.add(task)
                tasks_created += 1

            # Apply task status updates from transcript
            status_updates = analysis.get("task_status_updates", [])
            tasks_completed = 0
            tasks_updated = 0
            if status_updates:
                open_tasks_result = await session.execute(
                    select(Task).where(Task.status.in_(["new", "in_progress", "overdue"]))
                )
                open_tasks = open_tasks_result.scalars().all()

                for upd in status_updates:
                    hint = (upd.get("task_title_hint") or "").lower().strip()
                    new_status = upd.get("new_status")
                    if not hint or new_status not in ("done", "in_progress"):
                        continue

                    # Fuzzy-match hint against open task titles
                    best: Task | None = None
                    best_score = 0
                    for t in open_tasks:
                        title_l = t.title.lower()
                        # Score: substring match or word overlap
                        if hint in title_l or title_l in hint:
                            score = 2
                        else:
                            hint_words = set(hint.split())
                            title_words = set(title_l.split())
                            score = len(hint_words & title_words)
                        if score > best_score:
                            best_score = score
                            best = t

                    if best and best_score > 0:
                        best.status = new_status
                        if new_status == "done":
                            best.completed_at = datetime.utcnow()
                            best.progress_percent = 100
                            tasks_completed += 1
                        else:
                            tasks_updated += 1

            await session.commit()

            # Store chunks for RAG
            full_text = f"Совещание: {analysis.get('title', '')}\nДата: {analysis.get('date', '')}\n\n{transcript}"
            if analysis.get("summary"):
                full_text += f"\n\nКраткое содержание:\n{analysis['summary']}"
            await store_meeting_chunks(meeting.id, full_text)

        result_text = f"✅ Протокол сохранён!\n\n"
        result_text += f"📝 {analysis.get('title', '')}\n"
        result_text += f"📋 Задач создано: {tasks_created}\n"
        if tasks_completed:
            result_text += f"✅ Задач закрыто по итогам встречи: {tasks_completed}\n"
        if tasks_updated:
            result_text += f"🔄 Обновлено статусов: {tasks_updated}\n"
        if tasks_unassigned:
            result_text += f"⚠️ Без ответственного: {tasks_unassigned}\n"
        result_text += f"\n🔔 Уведомления отправлены участникам."

        await callback.message.answer(result_text)
        await callback.answer("Подтверждено!")

        # Notify assignees
        await _notify_assignees(callback.bot, meeting.id)

    except Exception as e:
        logger.error(f"Error confirming protocol: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка при сохранении: {e}")
        await callback.answer("Ошибка!")


def _fuzzy_match_member(name: str, members: list) -> Member | None:
    """Try to match a name from transcript to a registered member."""
    name_lower = name.lower().strip()

    for m in members:
        for field in [m.display_name, m.first_name, m.last_name, m.username]:
            if field and field.lower() == name_lower:
                return m

    for m in members:
        for field in [m.display_name, m.first_name, m.username]:
            if not field:
                continue
            if field.lower() in name_lower or name_lower in field.lower():
                return m

    first_word = name_lower.split()[0] if name_lower.split() else ""
    if first_word and len(first_word) > 2:
        for m in members:
            for field in [m.display_name, m.first_name]:
                if field and field.lower().startswith(first_word):
                    return m

    return None


@router.callback_query(F.data.startswith("reject_protocol:"))
async def reject_protocol(callback: CallbackQuery):
    """Chairman rejects the analyzed protocol."""
    user_id = callback.from_user.id
    review_index = int(callback.data.split(":")[1])

    reviews = _pending_reviews.get(user_id, [])
    if review_index < len(reviews):
        reviews[review_index] = None

    await callback.message.answer("❌ Протокол отклонён. Можешь отправить файл заново.")
    await callback.answer("Отклонено")


async def _notify_assignees(bot: Bot, meeting_id: int):
    """Send personal task notifications to each assignee."""
    async with async_session() as session:
        meeting = await session.get(Meeting, meeting_id)
        result = await session.execute(
            select(Task, Member)
            .join(Member, Task.assignee_id == Member.id)
            .where(Task.meeting_id == meeting_id)
        )
        rows = result.all()

    if not rows:
        return

    # Group tasks by assignee
    by_assignee: dict[int, dict] = {}
    for task, member in rows:
        if member.telegram_id not in by_assignee:
            by_assignee[member.telegram_id] = {"name": member.name, "tasks": []}
        by_assignee[member.telegram_id]["tasks"].append(task)

    for tg_id, data in by_assignee.items():
        text = f"🔔 Новые задачи\n\n"
        text += f"С совещания \"{meeting.title}\":\n\n"

        for task in data["tasks"]:
            deadline_str = task.deadline.strftime("%d.%m.%Y") if task.deadline else "без срока"
            p_icon = {"high": "🔥 ", "medium": "", "low": ""}.get(task.priority, "")
            text += f"  {p_icon}#{task.id} {task.title}\n"
            text += f"       📅 {deadline_str}\n\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks")]
        ])

        try:
            await bot.send_message(tg_id, text, reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Failed to notify {tg_id}: {e}")
