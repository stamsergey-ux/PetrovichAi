"""AI chat handler — free-form conversation about meetings and tasks."""
from __future__ import annotations

import io
import json
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.database import async_session, Task, Member, Meeting
from app.ai_service import chat_with_context, generate_agenda
from app.rag import search_relevant_chunks
from app.gantt import generate_gantt_pdf
from app.utils import is_chairman

router = Router()


def _escape_md(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for ch in special:
        text = text.replace(ch, f'\\{ch}')
    return text


def _progress_bar(done: int, total: int, length: int = 10) -> str:
    if total == 0:
        return "░" * length
    filled = round(done / total * length)
    return "▓" * filled + "░" * (length - filled)


async def _get_tasks_summary(user_id: int | None = None) -> str:
    """Get a summary of current tasks, optionally filtered by user."""
    async with async_session() as session:
        query = (
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue"]))
            .order_by(Task.deadline.asc())
        )
        result = await session.execute(query)
        rows = result.all()

    if not rows:
        return "No open tasks."

    lines = []
    for task, member in rows:
        name = member.name if member else "unassigned"
        deadline = task.deadline.strftime("%d.%m.%Y") if task.deadline else "no deadline"
        lines.append(f"#{task.id} [{task.status}] {task.title} -> {name}, deadline: {deadline}")

    return "\n".join(lines)


async def _get_all_tasks_for_gantt(assignee_filter: str | None = None) -> list[dict]:
    """Get tasks formatted for Gantt chart."""
    async with async_session() as session:
        query = (
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .order_by(Task.deadline.asc())
        )
        result = await session.execute(query)
        rows = result.all()

    tasks = []
    for task, member in rows:
        name = member.name if member else "?"
        if assignee_filter and assignee_filter.lower() not in name.lower():
            continue
        tasks.append({
            "id": task.id,
            "title": task.title,
            "assignee": name,
            "deadline": task.deadline or datetime.now(),
            "created_at": task.created_at or datetime.now(),
            "status": task.status,
        })
    return tasks


async def _build_task_review_block() -> str:
    """Build the first agenda item: structured task status review grouped by assignee."""
    async with async_session() as session:
        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue"]))
            .order_by(Task.deadline.asc())
        )
        rows = result.all()

    if not rows:
        return ""

    # Group by assignee
    by_assignee: dict[str, list] = {}
    for task, member in rows:
        name = (member.display_name or member.first_name or member.username) if member else "Без ответственного"
        by_assignee.setdefault(name, []).append(task)

    total = len(rows)
    estimated_min = total * 2  # 2 min per task

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📋 ПУНКТ 1. ПРОВЕРКА СТАТУСА ЗАДАЧ",
        f"⏱ Расчётное время: ~{estimated_min} мин. ({total} задач × 2 мин.)",
        "",
        "По каждой задаче ответственный докладывает:",
        "  ✅ Выполнено — задача закрывается",
        "  🔄 В процессе — новый срок или комментарий",
        "  ❌ Проблема — обсуждение и решение группой",
        "",
    ]

    for name, tasks in sorted(by_assignee.items()):
        overdue_cnt = sum(1 for t in tasks if t.status == "overdue")
        badge = f"  🚨 {overdue_cnt} просрочено" if overdue_cnt else ""
        lines.append(f"👤 {name} ({len(tasks)}){badge}")

        # Sort: overdue first, then by deadline
        sorted_tasks = sorted(
            tasks,
            key=lambda t: (t.status != "overdue", t.deadline or datetime.max)
        )
        for t in sorted_tasks:
            icon = {"overdue": "🔴", "in_progress": "🔵", "new": "⬜"}.get(t.status, "⬜")
            dl = t.deadline.strftime("%d.%m.%Y") if t.deadline else "без срока"
            if t.status == "overdue" and t.deadline:
                days = (datetime.utcnow() - t.deadline).days
                dl += f" (+{days} дн.)"
            lines.append(f"  {icon} #{t.id} {t.title}")
            lines.append(f"       📅 {dl}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


async def _build_agenda() -> str:
    """Build agenda: task review block first, then AI-generated items."""
    async with async_session() as session:
        result = await session.execute(
            select(Meeting).where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc()).limit(5)
        )
        meetings = result.scalars().all()

        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress"]))
            .order_by(Task.deadline.asc())
        )
        open_rows = result.all()

        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status == "overdue")
        )
        overdue_rows = result.all()

    meetings_ctx = ""
    agenda_items = ""
    for m in meetings:
        meetings_ctx += f"\n[{m.date.strftime('%d.%m.%Y')}] {m.title}\n{m.summary[:500]}\n"
        if m.agenda_items_next:
            try:
                items = json.loads(m.agenda_items_next)
                for item in items:
                    agenda_items += f"- {item.get('topic', '?')} (presenter: {item.get('presenter', '?')})\n"
            except json.JSONDecodeError:
                pass

    open_tasks_text = "\n".join(
        f"#{t.id} {t.title} -> {m.name if m else '?'}, deadline: {t.deadline}" for t, m in open_rows
    ) or "None"

    overdue_text = "\n".join(
        f"#{t.id} {t.title} -> {m.name if m else '?'}, deadline: {t.deadline}" for t, m in overdue_rows
    ) or "None"

    task_review = await _build_task_review_block()
    ai_agenda = await generate_agenda(meetings_ctx, open_tasks_text, overdue_text, agenda_items or "None")

    if task_review:
        return task_review + "\n\n" + ai_agenda
    return ai_agenda


def _generate_agenda_pdf(agenda_text: str) -> io.BytesIO:
    """Generate a PDF document from agenda text."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    # Try to register a font that supports Cyrillic
    font_name = "Helvetica"
    for font_path in [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        if os.path.exists(font_path):
            try:
                pdfmetrics.registerFont(TTFont("CyrFont", font_path))
                font_name = "CyrFont"
                break
            except Exception:
                continue

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'AgendaTitle', parent=styles['Title'],
        fontName=font_name, fontSize=16, spaceAfter=12,
    )
    body_style = ParagraphStyle(
        'AgendaBody', parent=styles['Normal'],
        fontName=font_name, fontSize=11, leading=16, spaceAfter=6,
    )

    story = []
    date_str = datetime.now().strftime("%d.%m.%Y")
    story.append(Paragraph(f"Повестка совещания — {date_str}", title_style))
    story.append(Spacer(1, 0.5*cm))

    for line in agenda_text.split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 0.3*cm))
            continue
        # Escape XML special chars for reportlab
        safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(safe, body_style))

    doc.build(story)
    buf.seek(0)
    return buf


@router.callback_query(F.data.startswith("proto_"))
async def cb_view_protocol(callback: CallbackQuery):
    """Show a specific protocol by ID."""
    from html import escape

    meeting_id = int(callback.data.split("_")[1])

    async with async_session() as session:
        result = await session.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )
        meeting = result.scalar_one_or_none()

    if not meeting:
        await callback.answer("Протокол не найден", show_alert=True)
        return

    date_str = meeting.date.strftime('%d.%m.%Y')
    title = escape(meeting.title or "Без названия")
    participants = escape(meeting.participants or "—")
    summary = escape(meeting.summary or "—")

    text = f"📝 <b>ПРОТОКОЛ</b>\n\n"
    text += f"<b>{title}</b>\n"
    text += f"📅 {date_str}\n"
    text += f"👥 {participants}\n\n"
    text += f"<b>Краткое содержание:</b>\n{summary}\n"

    if meeting.decisions:
        try:
            decisions = json.loads(meeting.decisions)
            if decisions:
                text += f"\n⚖️ <b>РЕШЕНИЯ:</b>\n"
                for d in decisions:
                    text += f"  • {escape(d['text'])}\n"
        except (json.JSONDecodeError, KeyError):
            pass

    if len(text) > 4000:
        text = text[:4000] + "\n\n... <i>протокол обрезан</i>"

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.message(F.text)
async def handle_text_message(message: Message):
    """Handle all text messages as AI chat (catch-all handler, must be registered last)."""
    text = message.text.strip().lower()

    # Quick command detection (including persistent keyboard button texts)
    if text in ("мои задачи", "мои задачи?", "какие у меня задачи", "какие у меня задачи?",
                "📋 мои задачи"):
        return await _show_my_tasks(message)

    if text in ("протокол", "последний протокол", "📝 протокол"):
        return await _show_last_protocol(message)

    if text in ("гант", "ганта", "гант-таблица", "экспорт задач", "диаграмма ганта"):
        return await _send_gantt(message)

    if text in ("адженда", "повестка", "подготовь адженду", "подготовь повестку"):
        return await _send_agenda(message)

    if text in ("дашборд", "dashboard", "статус", "📊 дашборд"):
        return await _send_dashboard(message)

    if text in ("все задачи", "👥 все задачи"):
        return await _show_all_tasks(message)

    if text in ("помощь", "❓ помощь", "help"):
        return await _show_help(message)

    if text in ("⚙️ расширенные функции", "расширенные функции"):
        return await _show_advanced_menu(message)

    if text in ("🔄 перезапустить бот", "перезапустить бот", "старт", "/start"):
        from app.handlers.onboarding import cmd_start
        return await cmd_start(message)

    if text in ("аналитика", "analytics", "📈 аналитика"):
        return await _show_analytics(message)

    if text in ("задачи акционера", "💎 задачи акционера"):
        from app.handlers.stakeholder import _render_stakeholder_tasks
        return await _render_stakeholder_tasks(message)

    if text in ("💎 мои поручения", "мои поручения"):
        from app.handlers.stakeholder import _render_my_assignments
        return await _render_my_assignments(message)

    # For everything else — AI chat with RAG
    await _ai_chat(message)


async def _show_my_tasks(message: Message):
    user_id = message.from_user.id

    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == user_id)
        )).scalar_one_or_none()

        if not member:
            await message.answer("Ты ещё не зарегистрирован. Нажми /start")
            return

        result = await session.execute(
            select(Task).where(
                Task.assignee_id == member.id,
                Task.status.in_(["new", "in_progress", "overdue"])
            ).order_by(Task.deadline.asc())
        )
        tasks = result.scalars().all()

    if not tasks:
        await message.answer("🎉 У тебя нет открытых задач!")
        return

    overdue = [t for t in tasks if t.status == "overdue"]
    in_prog = [t for t in tasks if t.status == "in_progress"]
    new = [t for t in tasks if t.status == "new"]

    text = f"📋 Твои задачи ({len(tasks)})\n\n"

    if overdue:
        text += f"🚨 ПРОСРОЧЕНО ({len(overdue)})\n"
        for t in overdue:
            deadline = t.deadline.strftime("%d.%m.%Y") if t.deadline else "—"
            days = abs((t.deadline - datetime.utcnow()).days) if t.deadline else 0
            text += f"  🔴 #{t.id} {t.title}\n"
            text += f"      📅 {deadline} (⚠️ +{days} дн.)\n\n"

    if in_prog:
        text += f"🔵 В РАБОТЕ ({len(in_prog)})\n"
        for t in in_prog:
            deadline = t.deadline.strftime("%d.%m.%Y") if t.deadline else "без срока"
            text += f"  🔵 #{t.id} {t.title}\n"
            text += f"      📅 {deadline}\n\n"

    if new:
        text += f"⬜ НОВЫЕ ({len(new)})\n"
        for t in new:
            deadline = t.deadline.strftime("%d.%m.%Y") if t.deadline else "без срока"
            text += f"  ⬜ #{t.id} {t.title}\n"
            text += f"      📅 {deadline}\n\n"

    await message.answer(text)


async def _send_gantt(message: Message, assignee_filter: str | None = None):
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Экспорт диаграммы Ганта доступен администраторам.")
        return

    await message.answer("📊 Генерирую диаграмму Ганта...")
    tasks = await _get_all_tasks_for_gantt(assignee_filter)

    if not tasks:
        await message.answer("📭 Нет задач для отображения.")
        return

    pdf_buf = generate_gantt_pdf(tasks)
    doc = BufferedInputFile(
        pdf_buf.read(),
        filename=f"gantt_{datetime.now().strftime('%Y%m%d')}.pdf"
    )
    await message.answer_document(doc, caption="📊 Диаграмма Ганта — Совет Директоров")


async def _send_agenda(message: Message):
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Генерация адженды доступна администраторам.")
        return

    await message.answer("📌 Готовлю адженду следующего совещания...")
    agenda_text = await _build_agenda()

    # Wrap in styled format
    text = f"📌 ПОВЕСТКА СЛЕДУЮЩЕГО СОВЕЩАНИЯ\n\n{agenda_text}"
    await message.answer(text)

    # Send PDF version
    pdf_buf = _generate_agenda_pdf(agenda_text)
    doc = BufferedInputFile(
        pdf_buf.read(),
        filename=f"agenda_{datetime.now().strftime('%Y%m%d')}.pdf",
    )
    await message.answer_document(doc, caption="📎 Адженда в PDF — можно переслать по почте или прикрепить к приглашению")


async def _send_dashboard(message: Message):
    async with async_session() as session:
        all_tasks = (await session.execute(select(Task))).scalars().all()
        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue"]))
        )
        active_rows = result.all()

    total = len(all_tasks)
    new = sum(1 for t in all_tasks if t.status == "new")
    in_progress = sum(1 for t in all_tasks if t.status == "in_progress")
    done = sum(1 for t in all_tasks if t.status == "done")

    now = datetime.utcnow()
    overdue = sum(
        1 for t in all_tasks
        if t.deadline and t.deadline < now and t.status not in ("done",)
    )

    bar = _progress_bar(done, total, 15)

    text = f"📊 ДАШБОРД\n\n"
    text += f"Прогресс: [{bar}] {done}/{total}\n\n"
    text += f"⬜ Новые: {new}\n"
    text += f"🔵 В работе: {in_progress}\n"
    text += f"✅ Выполнено: {done}\n"
    text += f"🔴 Просрочено: {overdue}\n"

    # Workload by person
    if active_rows:
        by_person: dict[str, int] = {}
        for task, member in active_rows:
            name = (member.display_name or member.first_name) if member else "—"
            by_person[name] = by_person.get(name, 0) + 1

        text += f"\n👥 Нагрузка по участникам:\n"
        max_count = max(by_person.values()) if by_person else 1
        for name, count in sorted(by_person.items(), key=lambda x: -x[1]):
            mini_bar = _progress_bar(count, max_count, 8)
            text += f"  {name}: [{mini_bar}] {count}\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks"),
            InlineKeyboardButton(text="👥 Все задачи", callback_data="all_tasks"),
        ]
    ])
    await message.answer(text, reply_markup=keyboard)


async def _show_last_protocol(message: Message):
    """Show list of all protocols with inline buttons to view each one."""
    from html import escape

    async with async_session() as session:
        result = await session.execute(
            select(Meeting)
            .where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc())
        )
        meetings = result.scalars().all()

    if not meetings:
        await message.answer("📭 <b>Пока нет сохранённых протоколов.</b>", parse_mode="HTML")
        return

    text = f"📝 <b>ПРОТОКОЛЫ</b> — {len(meetings)} шт.\n\n"
    buttons = []
    for m in meetings:
        date_str = m.date.strftime('%d.%m.%Y')
        title = escape((m.title or "Без названия")[:50])
        text += f"📅 <b>{date_str}</b> — {title}\n"
        buttons.append([
            InlineKeyboardButton(
                text=f"📅 {date_str} — {(m.title or '?')[:30]}",
                callback_data=f"proto_{m.id}",
            )
        ])

    text += "\n<i>Нажми на кнопку, чтобы открыть протокол:</i>"
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _show_all_tasks(message: Message):
    """Show all open tasks (reply keyboard version)."""
    from html import escape
    async with async_session() as session:
        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue"]))
            .order_by(Task.deadline.asc())
        )
        rows = result.all()

    if not rows:
        await message.answer("🎉 <b>Нет открытых задач!</b>", parse_mode="HTML")
        return

    by_assignee: dict[str, list] = {}
    for task, member in rows:
        name = (member.display_name or member.first_name or member.username) if member else "Не назначено"
        by_assignee.setdefault(name, []).append(task)

    text = f"👥 <b>Все открытые задачи</b> — {len(rows)}\n\n"
    for assignee_name, tasks in sorted(by_assignee.items()):
        overdue_cnt = sum(1 for t in tasks if t.status == "overdue")
        badge = f" 🚨{overdue_cnt}" if overdue_cnt else ""
        text += f"<b>{escape(assignee_name)}</b> ({len(tasks)}){badge}\n"
        for t in tasks:
            status_icon = {"new": "⬜", "in_progress": "🔵", "overdue": "🔴"}.get(t.status, "⬜")
            deadline = t.deadline.strftime("%d.%m") if t.deadline else "—"
            title = t.title[:55] + "..." if len(t.title) > 55 else t.title
            text += f"  {status_icon} #{t.id} {escape(title)} · {deadline}\n"
        text += "\n"

    if len(text) > 4000:
        text = text[:4000] + "\n\n... <i>список обрезан</i>"

    await message.answer(text, parse_mode="HTML")


async def _show_help(message: Message):
    """Show help (reply keyboard version)."""
    from app.handlers.onboarding import MEMBER_INTRO, CHAIRMAN_EXTRA, _persistent_keyboard
    user = message.from_user
    name = user.first_name or user.username or "коллега"
    chairman = is_chairman(user.username)
    text = MEMBER_INTRO.format(name=name)
    if chairman:
        text += CHAIRMAN_EXTRA
    await message.answer(text, parse_mode="HTML", reply_markup=_persistent_keyboard(chairman))


async def _show_analytics(message: Message):
    """Show meeting analytics."""
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Аналитика доступна администраторам.")
        return
    from app.handlers.meetings import get_analytics_text
    text = await get_analytics_text()
    await message.answer(text, parse_mode="HTML")


async def _show_advanced_menu(message: Message):
    """Show advanced admin menu with inline buttons."""
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Расширенные функции доступны администраторам.")
        return

    text = "⚙️ <b>РАСШИРЕННЫЕ ФУНКЦИИ</b>\n\n"
    text += "Выбери действие:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📌 Адженда", callback_data="adv_agenda"),
            InlineKeyboardButton(text="📊 Аналитика", callback_data="adv_analytics"),
        ],
        [
            InlineKeyboardButton(text="📈 Гант (PDF)", callback_data="adv_gantt"),
            InlineKeyboardButton(text="📅 Назначить совещание", callback_data="adv_schedule"),
        ],
        [
            InlineKeyboardButton(text="👥 Все задачи", callback_data="all_tasks"),
            InlineKeyboardButton(text="📊 Дашборд", callback_data="dashboard_cb"),
        ],
        [
            InlineKeyboardButton(text="💎 Задачи акционера", callback_data="stk_all_tasks"),
        ],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "adv_agenda")
async def cb_adv_agenda(callback: CallbackQuery):
    """Generate and show agenda + PDF."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Доступно администраторам", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("📌 Готовлю адженду следующего совещания...")
    agenda_text = await _build_agenda()
    text = f"📌 <b>ПОВЕСТКА СЛЕДУЮЩЕГО СОВЕЩАНИЯ</b>\n\n{agenda_text}"
    if len(text) > 4000:
        text = text[:4000] + "\n\n... <i>обрезано</i>"
    await callback.message.answer(text, parse_mode="HTML")

    # Send PDF version
    pdf_buf = _generate_agenda_pdf(agenda_text)
    doc = BufferedInputFile(
        pdf_buf.read(),
        filename=f"agenda_{datetime.now().strftime('%Y%m%d')}.pdf",
    )
    await callback.message.answer_document(doc, caption="📎 Адженда в PDF — можно переслать по почте или прикрепить к приглашению")


@router.callback_query(F.data == "adv_analytics")
async def cb_adv_analytics(callback: CallbackQuery):
    """Show analytics."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Доступно администраторам", show_alert=True)
        return
    await callback.answer()
    from app.handlers.meetings import get_analytics_text
    text = await get_analytics_text()
    await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "adv_gantt")
async def cb_adv_gantt(callback: CallbackQuery):
    """Generate and send Gantt PDF."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Доступно администраторам", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("📊 Генерирую диаграмму Ганта...")
    tasks = await _get_all_tasks_for_gantt()
    if not tasks:
        await callback.message.answer("📭 Нет задач для отображения.")
        return
    pdf_buf = generate_gantt_pdf(tasks)
    doc = BufferedInputFile(
        pdf_buf.read(),
        filename=f"gantt_{datetime.now().strftime('%Y%m%d')}.pdf"
    )
    await callback.message.answer_document(doc, caption="📊 Диаграмма Ганта — Совет Директоров")


@router.callback_query(F.data == "adv_schedule")
async def cb_adv_schedule(callback: CallbackQuery):
    """Prompt user to schedule a meeting."""
    await callback.answer()
    await callback.message.answer(
        "📅 <b>Назначить совещание</b>\n\n"
        "Напиши в формате:\n"
        "<i>Назначь совещание ДД.ММ.ГГГГ Название</i>\n\n"
        "Пример:\n"
        "<i>Назначь совещание 15.03.2026 Итоги Q1</i>",
        parse_mode="HTML",
    )


async def _ai_chat(message: Message):
    """Handle free-form AI chat with RAG context."""
    user = message.from_user
    user_name = user.first_name or user.username or "Пользователь"

    # Search relevant meeting chunks
    chunks = await search_relevant_chunks(message.text, limit=5)
    tasks_summary = await _get_tasks_summary()

    await message.answer("🤖 Думаю...")

    response = await chat_with_context(
        user_message=message.text,
        user_name=user_name,
        context_chunks=chunks,
        tasks_summary=tasks_summary,
    )

    # Split long responses
    if len(response) > 4000:
        parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
        for part in parts:
            await message.answer(part)
    else:
        await message.answer(response)
