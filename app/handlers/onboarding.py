"""Onboarding and /start, /help handlers."""

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
)
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
что нужно сделать, кто ответственный, срок.
Покажу карточку — подтвердишь перед созданием.
Ответственный и руководство получат уведомление.

💎 <b>Мои поручения</b>
Все поставленные тобой задачи и их текущий статус.

📋 <b>Протоколы и задачи</b>
· Все протоколы совещаний доступны
· «Что решили на последнем совещании?»
· «Какой статус у задачи по аудиту?»

📎 <b>Материалы совещаний</b>
· Отправь PDF или PPTX — сохраню в архив совещания
· «Материалы» — все загруженные файлы

🎙 <b>Голосовые сообщения</b>
Отправь войс — распознаю и выполню команду.

⚙️ <b>Расширенные функции</b>
Кнопка открывает полный список: задачи, дашборд,
протоколы, материалы совещаний и другие.

💬 <b>Свободный чат</b>
Пиши любые вопросы — отвечу на основе данных Совета."""



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


def _persistent_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Persistent reply keyboard — always visible at the bottom of the chat."""
    buttons = [
        [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📝 Протокол")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="📝 Поставить задачу")])
        buttons.append([KeyboardButton(text="⚙️ Расширенные функции")])
    buttons.append(
        [KeyboardButton(text="🔄 Перезапустить бот"), KeyboardButton(text="❓ Помощь")]
    )
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)




def _stakeholder_keyboard() -> ReplyKeyboardMarkup:
    """Persistent keyboard for stakeholder/shareholder."""
    buttons = [
        [KeyboardButton(text="💎 Поставить задачу"), KeyboardButton(text="💎 Мои поручения")],
        [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📝 Протокол")],
        [KeyboardButton(text="⚙️ Расширенные функции")],
        [KeyboardButton(text="🔄 Перезапустить бот"), KeyboardButton(text="❓ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
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
            result2 = await session.execute(
                select(Member).where(Member.username == user.username)
            )
            member = result2.scalar_one_or_none()

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

    if stakeholder:
        text = STAKEHOLDER_INTRO.format(name=name)
        keyboard = _stakeholder_keyboard()
    else:
        text = MEMBER_INTRO.format(name=name)
        if chairman:
            text += CHAIRMAN_EXTRA
        keyboard = _persistent_keyboard(chairman)

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


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
