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

Привет, <b>{name}</b>!

📋 <b>Протоколы</b>
Все совещания сохранены. Просто спроси:
<i>«Что обсуждали на последнем совещании?»</i>

✅ <b>Задачи</b>
Твои задачи и дедлайны — всегда под рукой.
Напиши <i>«Какие у меня задачи?»</i>
Напоминаю за 2 дня, в день дедлайна и при просрочке.

🎙 <b>Голосовые</b>
Отправь войс — распознаю и выполню команду.

💬 <b>Свободный чат</b>
Не нужно запоминать команды — пиши как человеку."""

CHAIRMAN_EXTRA = """

🔑 <b>Управление (председатель)</b>
· Загрузи файл из Plaud — разберу протокол
· <i>«Создай задачу для Екатерины до 15 марта»</i>
· <i>«Подготовь адженду»</i>
· <i>«Гант»</i> — PDF-диаграмма задач
· <i>«Дашборд»</i> / <i>«Аналитика»</i>
· <i>«Назначь совещание 15.03.2026 Итоги Q1»</i>"""

STAKEHOLDER_INTRO = """💎 <b>AI-секретарь — Акционер</b>

Привет, <b>{name}</b>!

Используй кнопки ниже:
· <b>💎 Поставить задачу</b> — создать поручение
· <b>💎 Мои поручения</b> — статус задач

Или просто напиши вопрос в свободной форме."""



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
        buttons.append(
            [KeyboardButton(text="⚙️ Расширенные функции")]
        )
    buttons.append(
        [KeyboardButton(text="🔄 Перезапустить бот"), KeyboardButton(text="❓ Помощь")]
    )
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)




def _stakeholder_keyboard() -> ReplyKeyboardMarkup:
    """Persistent keyboard for stakeholder/shareholder."""
    buttons = [
        [KeyboardButton(text="💎 Поставить задачу"), KeyboardButton(text="💎 Мои поручения")],
        [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📝 Протокол")],
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
        existing = await session.execute(
            select(Member).where(Member.telegram_id == user.id)
        )
        member = existing.scalar_one_or_none()

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
            await session.commit()
        elif stakeholder and not member.is_stakeholder:
            member.is_stakeholder = True
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
