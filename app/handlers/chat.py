"""AI chat handler — free-form conversation about meetings and tasks."""
from __future__ import annotations

import io
import json
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.database import async_session, Task, Member, Meeting
from app.ai_service import chat_with_context, generate_agenda
from app.rag import search_relevant_chunks
from app.gantt import generate_gantt_pdf
from app.utils import is_chairman

router = Router()


class BroadcastState(StatesGroup):
    waiting_changelog = State()


# ── Changelog — update this with every deploy ──────────────────────────────────
CURRENT_CHANGELOG = """✅ Что нового в боте:

• <b>Комментировать задачи</b> — кнопка 💬 теперь работает. Можно оставлять голосовые и текстовые комментарии. Вторая сторона получает уведомление.

• <b>Уточняющие вопросы</b> — исполнитель может нажать ❓ в карточке задачи, задать вопрос голосом или текстом. Председатель отвечает — ответ приходит исполнителю.

• <b>Отслеживание задачи после постановки</b> — сразу после создания задачи появляются кнопки "Открыть задачу" и "Все задачи".

• <b>ИИ помнит контекст</b> — если открыл карточку задачи и задал вопрос боту, он отвечает именно про эту задачу, не переспрашивая."""


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
            .where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
            .where(Task.is_verified == True)
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
    import matplotlib

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    # Use DejaVuSans bundled with matplotlib — guaranteed Cyrillic support
    font_name = "Helvetica"
    font_candidates = [
        os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for font_path in font_candidates:
        if os.path.exists(font_path):
            try:
                pdfmetrics.registerFont(TTFont("DejaVu", font_path))
                font_name = "DejaVu"
                break
            except Exception:
                continue

    # Also register bold variant for headings
    bold_font = font_name
    if font_name == "DejaVu":
        bold_path = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf")
        if os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont("DejaVuBold", bold_path))
                bold_font = "DejaVuBold"
            except Exception:
                pass

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'AgendaTitle', parent=styles['Title'],
        fontName=bold_font, fontSize=16, spaceAfter=12,
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
async def handle_text_message(message: Message, state: FSMContext):
    """Handle all text messages as AI chat (catch-all handler, must be registered last)."""
    await _dispatch_text(message, message.text, state)


async def _dispatch_text(message: Message, raw_text: str, state: FSMContext | None = None):
    """Dispatch a text (from keyboard or transcribed voice) to the right handler."""
    text = raw_text.strip().lower()

    # Quick command detection (including persistent keyboard button texts)
    if text in ("мои задачи", "мои задачи?", "какие у меня задачи", "какие у меня задачи?",
                "📋 мои задачи", "подробное описание", "дай подробное описание",
                "подробнее о задачах", "покажи задачи", "мои задачи подробно"):
        return await _show_my_tasks(message)

    if text in ("протокол", "последний протокол", "📝 протокол"):
        return await _show_last_protocol(message)

    if text in ("гант", "ганта", "гант-таблица", "экспорт задач", "диаграмма ганта", "📊 гант"):
        return await _send_gantt(message)

    if text in ("адженда", "повестка", "подготовь адженду", "подготовь повестку"):
        return await _send_agenda(message)

    if text in ("дашборд", "dashboard", "статус", "📊 дашборд"):
        return await _send_dashboard(message)

    if text in ("все задачи", "👥 все задачи"):
        return await _show_all_tasks(message)

    if text in ("помощь", "❓ помощь", "help"):
        return await _show_help(message)

    if text in ("⚙️ расширенные функции", "расширенные функции", "⚙️ управление", "управление"):
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

    if text in ("✅ верифицировать задачи", "верифицировать задачи", "верификация задач"):
        from app.handlers.task_verify import start_verification
        return await start_verification(message)

    if text in ("📎 материалы", "материалы", "материалы совещаний", "презентации"):
        from app.handlers.materials import show_materials
        return await show_materials(message)

    if text in ("📋 мои заметки", "мои заметки", "мои напоминалки", "заметки"):
        from app.handlers.personal import show_personal_tasks
        return await show_personal_tasks(message)

    if text.startswith(("напомни ", "запиши ", "заметка ")):
        # Quick capture: skip FSM, save directly
        from app.handlers.personal import _save_personal_task_direct
        return await _save_personal_task_direct(message, raw_text)

    # For everything else — AI chat with RAG
    await _ai_chat(message, override_text=raw_text, state=state)


async def _show_my_tasks(message: Message):
    from html import escape
    from app.handlers.tasks import _task_buttons, _task_list_keyboard

    user_id = message.from_user.id
    admin = is_chairman(message.from_user.username)

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
                Task.status.in_(["new", "in_progress", "overdue", "pending_done"]),
            ).order_by(Task.deadline.asc())
        )
        tasks = result.scalars().all()

    if not tasks:
        await message.answer(
            "🎉 <b>Нет открытых задач!</b>\n\nВсе задачи выполнены или ещё не назначены.",
            parse_mode="HTML",
            reply_markup=_task_list_keyboard(admin),
        )
        return

    name = escape(member.name or member.first_name or "")
    overdue_cnt = sum(1 for t in tasks if t.status == "overdue")
    pending_cnt = sum(1 for t in tasks if t.status == "pending_done")
    badges = []
    if overdue_cnt:
        badges.append(f"🚨 {overdue_cnt} просрочено")
    if pending_cnt:
        badges.append(f"🟡 {pending_cnt} ждут подтверждения")
    badge_str = ("\n" + " · ".join(badges)) if badges else ""

    text = f"📋 <b>Задачи: {name}</b>\n{len(tasks)} открытых{badge_str}"

    task_rows = _task_buttons(tasks)
    admin_kb = _task_list_keyboard(admin)
    admin_rows = admin_kb.inline_keyboard if admin_kb else []
    keyboard = InlineKeyboardMarkup(inline_keyboard=task_rows + admin_rows) if (task_rows or admin_rows) else None

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


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
    from app.handlers.tasks import _send_dashboard_to
    await _send_dashboard_to(message)


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
    """Show all open tasks — redirect to grouped view in tasks.py."""
    from app.handlers.tasks import cb_all_tasks

    # Create a fake callback-like flow: just show grouped by meeting
    async with async_session() as session:
        from app.database import Meeting
        result = await session.execute(
            select(Task, Meeting)
            .outerjoin(Meeting, Task.meeting_id == Meeting.id)
            .where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
        )
        rows = result.all()

    if not rows:
        await message.answer("🎉 <b>Нет открытых задач!</b>", parse_mode="HTML")
        return

    # Group by meeting
    groups: dict[int, list] = {}
    for row in rows:
        task, meeting = row[0], row[1]
        mid = task.meeting_id or 0
        if mid not in groups:
            groups[mid] = [meeting, 0, 0]
        groups[mid][1] += 1
        if task.status == "overdue":
            groups[mid][2] += 1

    def sort_key(item):
        mid, (meeting, *_) = item
        if mid == 0:
            return (0,)
        return (1, -(meeting.date.timestamp() if meeting and meeting.date else 0))

    sorted_groups = sorted(groups.items(), key=sort_key)

    total = len(rows)
    overdue_total = sum(1 for row in rows if row[0].status == "overdue")

    text = f"👥 <b>Все открытые задачи — {total}</b>"
    if overdue_total:
        text += f"\n🚨 {overdue_total} просрочено"
    text += "\n\nВыбери протокол:"

    btn_rows = []
    for mid, (meeting, cnt, ov) in sorted_groups:
        if mid == 0:
            label = "📌 Без протокола"
        else:
            date_str = meeting.date.strftime("%d.%m.%Y") if meeting and meeting.date else "—"
            title = (meeting.title or "Совещание")[:30]
            label = f"📋 {date_str} — {title}"
        badge = f"  {cnt} зад."
        if ov:
            badge += f" 🚨{ov}"
        btn_text = label + badge
        if len(btn_text) > 60:
            btn_text = btn_text[:59] + "…"
        btn_rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"tasks_by_meeting:{mid}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=btn_rows)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


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
    """Show advanced admin menu with inline buttons grouped by section."""
    from app.utils import is_stakeholder
    if is_stakeholder(message.from_user.username):
        # Stakeholder gets a focused menu
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Поставить задачу", callback_data="stk_start_task")],
            [InlineKeyboardButton(text="💎 Мои поручения", callback_data="stk_my_tasks")],
            [InlineKeyboardButton(text="📝 Протоколы", callback_data="last_protocol")],
        ])
        await message.answer(
            "💎 <b>Меню акционера</b>\n\nВыбери действие:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Управление доступно администраторам.")
        return

    text = "⚙️ <b>УПРАВЛЕНИЕ</b>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        # Section: Tasks
        [InlineKeyboardButton(text="── ЗАДАЧИ ──", callback_data="noop")],
        [
            InlineKeyboardButton(text="✅ Верифицировать", callback_data="adv_verify"),
            InlineKeyboardButton(text="💎 Задачи акционера", callback_data="stk_all_tasks"),
        ],
        [
            InlineKeyboardButton(text="✅ Закрытые задачи всех", callback_data="all_closed_tasks"),
        ],
        # Section: Meetings
        [InlineKeyboardButton(text="── СОВЕЩАНИЯ ──", callback_data="noop")],
        [
            InlineKeyboardButton(text="📌 Адженда", callback_data="adv_agenda"),
            InlineKeyboardButton(text="📅 Назначить совещание", callback_data="adv_schedule"),
        ],
        [
            InlineKeyboardButton(text="🗑 Протоколы", callback_data="manage_protocols"),
            InlineKeyboardButton(text="📎 Материалы", callback_data="adv_materials"),
        ],
        # Section: Analytics
        [InlineKeyboardButton(text="── АНАЛИТИКА ──", callback_data="noop")],
        [
            InlineKeyboardButton(text="📊 Аналитика", callback_data="adv_analytics"),
            InlineKeyboardButton(text="📈 Гант (PDF)", callback_data="adv_gantt"),
        ],
        # Section: System
        [InlineKeyboardButton(text="── СИСТЕМА ──", callback_data="noop")],
        [
            InlineKeyboardButton(text="🔄 Обновить клавиатуры", callback_data="adv_refresh_keyboards"),
        ],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    """No-op handler for section header buttons."""
    await callback.answer()


@router.callback_query(F.data == "adv_materials")
async def cb_adv_materials(callback: CallbackQuery):
    """Show meeting materials."""
    await callback.answer()
    from app.handlers.materials import show_materials
    await show_materials(callback.message)


@router.callback_query(F.data == "adv_verify")
async def cb_adv_verify(callback: CallbackQuery):
    """Start task verification flow."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Доступно администраторам", show_alert=True)
        return
    await callback.answer()
    from app.handlers.task_verify import start_verification
    await start_verification(callback.message, user=callback.from_user)


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


@router.callback_query(F.data == "adv_refresh_keyboards")
async def cb_adv_refresh_keyboards(callback: CallbackQuery, state: FSMContext):
    """Ask chairman for a changelog note before broadcasting keyboards."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Доступно администраторам", show_alert=True)
        return

    await state.set_state(BroadcastState.waiting_changelog)
    await callback.answer()
    await callback.message.answer(
        f"📣 <b>Рассылка обновления</b>\n\n"
        f"Подготовлен текст для рассылки:\n\n"
        f"<blockquote>{CURRENT_CHANGELOG}</blockquote>\n\n"
        f"Отправь этот текст или напиши свой:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Отправить этот текст", callback_data="broadcast_use_default")],
            [InlineKeyboardButton(text="✏️ Написать свой", callback_data="broadcast_custom")],
            [InlineKeyboardButton(text="🔇 Без комментария", callback_data="broadcast_no_note")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
        ]),
    )


@router.callback_query(F.data == "broadcast_cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Отменено.")
    await callback.answer()


@router.callback_query(F.data == "broadcast_use_default")
async def cb_broadcast_use_default(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await _do_broadcast(callback.message, callback.bot, note=CURRENT_CHANGELOG)


@router.callback_query(F.data == "broadcast_custom")
async def cb_broadcast_custom(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("✏️ Напиши свой текст обновления:")


@router.callback_query(F.data == "broadcast_no_note")
async def cb_broadcast_no_note(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await _do_broadcast(callback.message, callback.bot, note=None)


@router.message(BroadcastState.waiting_changelog, F.text)
async def process_changelog(message: Message, state: FSMContext):
    await state.clear()
    await _do_broadcast(message, message.bot, note=message.text.strip())


async def _do_broadcast(trigger_message, bot, note: str | None):
    """Send updated keyboards (+ optional changelog note) to all members."""
    from app.utils import is_stakeholder
    from app.handlers.onboarding import _persistent_keyboard, _stakeholder_keyboard

    async with async_session() as session:
        members = (await session.execute(
            select(Member).where(Member.telegram_id > 0)
        )).scalars().all()

    if note:
        broadcast_text = (
            f"🔄 <b>Обновление бота</b>\n\n"
            f"{note}"
        )
    else:
        broadcast_text = "🔄 <b>Меню обновлено</b>"

    sent = 0
    failed = 0
    for member in members:
        try:
            if is_chairman(member.username):
                kb = _persistent_keyboard(is_admin=True)
            elif member.is_stakeholder:
                kb = _stakeholder_keyboard()
            else:
                kb = _persistent_keyboard(is_admin=False)

            await bot.send_message(
                chat_id=member.telegram_id,
                text=broadcast_text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            sent += 1
        except Exception:
            failed += 1

    status = f"✅ Разослано: {sent} пользователей"
    if failed:
        status += f"\n⚠️ Не доставлено: {failed}"
    await trigger_message.answer(status, parse_mode="HTML")


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


async def _get_my_tasks_summary(telegram_id: int) -> str | None:
    """Get tasks assigned to the specific user."""
    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == telegram_id)
        )).scalar_one_or_none()
        if not member:
            return None
        result = await session.execute(
            select(Task)
            .where(Task.assignee_id == member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
            .order_by(Task.deadline.asc())
        )
        tasks = result.scalars().all()
    if not tasks:
        return "No open tasks assigned."
    lines = []
    for t in tasks:
        deadline = t.deadline.strftime("%d.%m.%Y") if t.deadline else "no deadline"
        lines.append(f"#{t.id} [{t.status}] {t.title}, deadline: {deadline}")
    return "\n".join(lines)


async def _get_task_context(task_id: int) -> str | None:
    """Fetch full task details for AI context."""
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            return None
        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None
        meeting = await session.get(Meeting, task.meeting_id) if task.meeting_id else None
    assignee_name = assignee.name if assignee else "не назначен"
    deadline = task.deadline.strftime("%d.%m.%Y") if task.deadline else "без срока"
    meeting_str = (meeting.title or f"Совещание {meeting.date.strftime('%d.%m.%Y')}") if meeting else "Поручения председателя"
    lines = [
        f"#{task.id} [{task.status}] {task.title}",
        f"Исполнитель: {assignee_name}, Дедлайн: {deadline}",
        f"Протокол: {meeting_str}",
    ]
    if task.description:
        lines.append(f"Описание: {task.description}")
    if task.context_quote:
        lines.append(f"Контекст из протокола: {task.context_quote}")
    return "\n".join(lines)


async def _ai_chat(message: Message, override_text: str | None = None, state: FSMContext | None = None):
    """Handle free-form AI chat with RAG context."""
    from app.utils import is_stakeholder
    user = message.from_user
    user_name = user.first_name or user.username or "Пользователь"
    user_text = override_text or message.text

    if is_chairman(user.username):
        user_role = "Председатель совета директоров"
    elif is_stakeholder(user.username):
        user_role = "Акционер"
    else:
        user_role = "Член совета директоров"

    # Check if user was looking at a specific task (context from task_detail)
    task_context = None
    if state:
        data = await state.get_data()
        last_task_id = data.get("last_task_id")
        if last_task_id:
            task_context = await _get_task_context(last_task_id)

    # Search relevant meeting chunks
    chunks = await search_relevant_chunks(user_text, limit=5)
    tasks_summary = await _get_tasks_summary()
    my_tasks = await _get_my_tasks_summary(user.id)

    await message.answer("🤖 Думаю...")

    response = await chat_with_context(
        user_message=user_text,
        user_name=user_name,
        context_chunks=chunks,
        tasks_summary=tasks_summary,
        user_role=user_role,
        my_tasks_summary=my_tasks,
        task_context=task_context,
    )

    # Split long responses
    if len(response) > 4000:
        parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
        for part in parts:
            await message.answer(part)
    else:
        await message.answer(response)
