"""Onboarding and /start, /help handlers."""

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardRemove, BotCommand,
)
from aiogram.types.bot_command_scope_chat import BotCommandScopeChat
from sqlalchemy import select

from app.database import async_session, Member
from app.utils import is_chairman, is_stakeholder

router = Router()

MEMBER_INTRO = """🤖 <b>AI-секретарь Совета Директоров</b>

Привет, <b>{name}</b>! Я веду протоколы, отслеживаю задачи и помогаю готовиться к совещаниям.

✅ <b>Мои задачи</b>
· «Мои задачи» — список с дедлайнами и статусами
· Кнопки «Выполнено» и «В работе» прямо в карточке задачи
· «Комментировать» — добавить обновление по задаче

🔔 <b>Автонапоминания</b>
· За 2 дня до дедлайна
· В день дедлайна
· При просрочке — отдельное уведомление

📋 <b>Протоколы совещаний</b>
· «Протокол» — список всех совещаний
· «Что решили по бюджету в феврале?» — поиск по истории
· «Что обсуждали на последнем совещании?»

📎 <b>Материалы совещаний</b>
· Отправь PDF или PPTX боту — сохраню в архив
· «Материалы» — посмотреть все загруженные файлы

📅 <b>До следующего совещания</b>
· За 48ч бот запросит статус по твоим задачам
· За 24ч придёт повестка следующего совещания
· «Добавь в адженду: обсудить бюджет Q2»

🎙 <b>Голосовые сообщения</b>
Отправь войс — распознаю речь и выполню команду.

💬 <b>Свободный чат</b>
Не нужно запоминать команды — пиши как человеку."""

CHAIRMAN_EXTRA = """

🔑 <b>Председатель — расширенный доступ</b>

📝 <b>Поставить задачу</b>
· Нажми кнопку и опиши голосом или текстом:
  кому, что сделать, срок и приоритет
· Исполнитель получит уведомление с кнопкой «Принял задачу»
· Ты получишь отбивку о подтверждении получения

📂 <b>Загрузка протоколов</b>
· Отправь .txt или .pdf без подписи — разберу и создам задачи
· После анализа — кнопки «Подтвердить» / «Отклонить»
· PPTX, DOCX, PDF с подписью — автоматически в архив материалов

✅ <b>Верификация задач</b>
· «Верифицировать задачи» — назначить точного исполнителя
  и срок по каждой задаче из протокола
· Только верифицированные задачи видны участникам

📊 <b>Аналитика и контроль</b>
· «Дашборд» — прогресс, просрочки, нагрузка по участникам
· «Аналитика» — статистика и динамика выполнения
· «Гант» — PDF-диаграмма всех задач по исполнителям
· «Все задачи» — полный список по всем участникам

☑ <b>Групповые операции</b>
· «Все задачи» → выбери протокол → «☑ Выбрать несколько»
· Отмечай задачи чекбоксами, затем:
  — «✅ Принять (N)» — закрыть сразу несколько как выполненные
  — «🗑 Удалить (N)» — удалить выбранные задачи
· «☑ Все» / «☐ Снять» — выбрать или снять все сразу

📅 <b>Планирование совещаний</b>
· «Подготовь адженду» — AI-повестка на основе задач и протоколов
· «Назначь совещание 15.03.2026 Итоги Q1»
· Авто-рассылка повестки и сбор статусов"""

STAKEHOLDER_INTRO = """💎 <b>AI-секретарь — Акционер</b>

Привет, <b>{name}</b>!

💎 <b>Поставить задачу</b>
Нажми кнопку и опиши голосом или текстом:
что сделать, кто ответственный, срок.
Исполнитель и руководство получат уведомление.

💎 <b>Мои поручения</b>
Все поставленные тобой задачи, статус, просрочки.
Когда исполнитель задаёт уточняющий вопрос — ты получишь уведомление и сможешь ответить.

📋 <b>Протоколы и повестки</b>
Протоколы совещаний и повестки встреч.

🎙 <b>Голосовые сообщения</b>
Отправь войс — распознаю и выполню команду."""



def _main_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks"),
            InlineKeyboardButton(text="📝 Протокол", callback_data="last_protocol"),
        ],
    ]
    if is_admin:
        buttons.append([
            InlineKeyboardButton(text="📊 Дашборд", callback_data="dashboard_cb"),
            InlineKeyboardButton(text="👥 Все задачи", callback_data="all_tasks"),
        ])
    buttons.append([
        InlineKeyboardButton(text="❓ Что умеет бот?", callback_data="help"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


PERSONAL_NOTES_USERS = {"vikamikhno"}  # Pilot: personal notes only for Виктория


async def _set_user_commands(bot: Bot, chat_id: int, role: str, username: str | None = None):
    """Set role-specific Menu commands for a user."""
    has_notes = username and username.lower() in PERSONAL_NOTES_USERS

    if role == "chairman":
        commands = [
            BotCommand(command="tasks", description="📋 Мои задачи"),
            BotCommand(command="newtask", description="📝 Поставить задачу"),
            BotCommand(command="alltasks", description="👥 Все задачи"),
            BotCommand(command="dashboard", description="📊 Сводка"),
            BotCommand(command="protocol", description="📝 Протоколы"),
            BotCommand(command="agenda", description="📌 Адженда"),
            BotCommand(command="verify", description="✅ Верифицировать"),
            BotCommand(command="help", description="❓ Помощь"),
        ]
        if has_notes:
            commands.insert(5, BotCommand(command="note", description="📝 Записать заметку"))
            commands.insert(6, BotCommand(command="notes", description="📋 Мои заметки"))
    elif role == "stakeholder":
        commands = [
            BotCommand(command="newtask", description="💎 Поставить задачу"),
            BotCommand(command="assignments", description="💎 Мои поручения"),
            BotCommand(command="protocol", description="📝 Протоколы"),
            BotCommand(command="help", description="❓ Помощь"),
        ]
    else:
        commands = [
            BotCommand(command="tasks", description="📋 Мои задачи"),
            BotCommand(command="protocol", description="📝 Протоколы"),
            BotCommand(command="agenda_add", description="📌 Добавить в адженду"),
            BotCommand(command="help", description="❓ Помощь"),
        ]
        if has_notes:
            commands.insert(2, BotCommand(command="note", description="📝 Записать заметку"))
            commands.insert(3, BotCommand(command="notes", description="📋 Мои заметки"))
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception:
        pass
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start — register user and show onboarding."""
    user = message.from_user
    chairman = is_chairman(user.username)
    stakeholder = is_stakeholder(user.username)

    async with async_session() as session:
        # First check by telegram_id (already connected)
        result = await session.execute(
            select(Member).where(Member.telegram_id == user.id)
        )
        member = result.scalar_one_or_none()

        if not member and user.username:
            # Check if pre-seeded by username (placeholder telegram_id < 0)
            # Use case-insensitive match — Telegram usernames are case-insensitive
            result2 = await session.execute(
                select(Member).where(Member.username.ilike(user.username))
            )
            member = result2.scalar_one_or_none()

        # Remove any placeholder duplicate with the same username
        if member and user.username:
            await session.execute(
                Member.__table__.delete().where(
                    Member.username.ilike(user.username),
                    Member.id != member.id,
                )
            )
            await session.flush()

        if not member:
            member = Member(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_chairman=chairman,
                is_stakeholder=stakeholder,
            )
            session.add(member)
        else:
            # Update real telegram_id if it was a placeholder
            if member.telegram_id != user.id:
                member.telegram_id = user.id
            if not member.first_name:
                member.first_name = user.first_name
            if not member.last_name:
                member.last_name = user.last_name
            if user.username:
                member.username = user.username
            if stakeholder and not member.is_stakeholder:
                member.is_stakeholder = True
            if chairman and not member.is_chairman:
                member.is_chairman = True

        await session.commit()

    name = user.first_name or user.username or "коллега"

    if chairman:
        text = MEMBER_INTRO.format(name=name) + CHAIRMAN_EXTRA
        role = "chairman"
    elif stakeholder:
        text = STAKEHOLDER_INTRO.format(name=name)
        role = "stakeholder"
    else:
        text = MEMBER_INTRO.format(name=name)
        role = "member"

    # Set role-specific Menu commands and remove reply keyboard
    await _set_user_commands(message.bot, user.id, role, user.username)
    await message.answer(text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())


@router.message(Command("help"))
async def cmd_help(message: Message):
    user = message.from_user
    name = user.first_name or user.username or "коллега"
    chairman = is_chairman(user.username)
    text = MEMBER_INTRO.format(name=name)
    if chairman:
        text += CHAIRMAN_EXTRA
    await message.answer(text, parse_mode="HTML", reply_markup=_main_keyboard(chairman))


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    user = callback.from_user
    name = user.first_name or user.username or "коллега"
    chairman = is_chairman(user.username)
    text = MEMBER_INTRO.format(name=name)
    if chairman:
        text += CHAIRMAN_EXTRA
    await callback.message.answer(text, parse_mode="HTML", reply_markup=_main_keyboard(chairman))
    await callback.answer()


# ── Menu command handlers ─────────────────────────────────────────────────────

@router.message(Command("tasks"))
async def cmd_tasks(message: Message):
    from app.handlers.chat import _show_my_tasks
    await _show_my_tasks(message)


@router.message(Command("newtask"))
async def cmd_newtask(message: Message, state):
    if is_stakeholder(message.from_user.username):
        from app.handlers.stakeholder import start_task_creation
        await start_task_creation(message, state)
    elif is_chairman(message.from_user.username):
        from app.handlers.chairman_tasks import start_chairman_task
        await start_chairman_task(message, state)
    else:
        await message.answer("⛔ Постановка задач доступна председателям и акционерам.")


@router.message(Command("alltasks"))
async def cmd_alltasks(message: Message):
    from app.handlers.chat import _show_all_tasks
    await _show_all_tasks(message)


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Дашборд доступен администраторам.")
        return
    from app.handlers.tasks import _send_dashboard_to
    await _send_dashboard_to(message)


@router.message(Command("note"))
async def cmd_note(message: Message, state):
    from app.handlers.personal import start_personal_task
    await start_personal_task(message, state)


@router.message(Command("notes"))
async def cmd_notes(message: Message):
    from app.handlers.personal import show_personal_tasks
    await show_personal_tasks(message)


@router.message(Command("protocol"))
async def cmd_protocol(message: Message):
    from app.handlers.chat import _show_last_protocol
    await _show_last_protocol(message)


@router.message(Command("agenda"))
async def cmd_agenda(message: Message):
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Генерация адженды доступна администраторам.")
        return
    from app.handlers.chat import _send_agenda
    await _send_agenda(message)


@router.message(Command("verify"))
async def cmd_verify(message: Message):
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Верификация доступна администраторам.")
        return
    from app.handlers.task_verify import start_verification
    await start_verification(message, user=message.from_user)


@router.message(Command("manage"))
async def cmd_manage(message: Message):
    from app.handlers.chat import _show_advanced_menu
    await _show_advanced_menu(message)


@router.message(Command("assignments"))
async def cmd_assignments(message: Message):
    if not is_stakeholder(message.from_user.username):
        await message.answer("⛔ Доступно акционерам.")
        return
    from app.handlers.stakeholder import _render_my_assignments
    await _render_my_assignments(message)


@router.message(Command("agenda_add"))
async def cmd_agenda_add(message: Message):
    await message.answer(
        "📌 Чтобы добавить пункт в адженду, напиши:\n\n"
        "<code>добавь в адженду: тема, 15 мин</code>",
        parse_mode="HTML",
    )
