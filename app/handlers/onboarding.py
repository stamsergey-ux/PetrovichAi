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

Привет, <b>{name}</b>! Я веду протоколы, отслеживаю задачи и готовлю совещания.

📋 <b>Протоколы совещаний</b>
Все совещания сохранены и доступны:
· <i>«Что обсуждали на последнем совещании?»</i>
· <i>«Что решили по бюджету в феврале?»</i>
· <i>«Последний протокол»</i> — полный текст

✅ <b>Мои задачи</b>
· <i>«Какие у меня задачи?»</i>
· <i>«Что у меня просрочено?»</i>
· Кнопки <b>✅ Выполнено</b> и <b>🔄 В работе</b> — в карточке задачи
· <b>💬 Комментировать</b> — добавить комментарий

🔔 <b>Автонапоминания</b>
· ⏳ За 2 дня до дедлайна
· ⚡ В день дедлайна
· 🚨 При просрочке

🎙 <b>Голосовые сообщения</b>
Отправь войс — распознаю речь и выполню команду.
Можно диктовать задачи, вопросы, отчёты.

📅 <b>Совещания</b>
· За 48ч — бот собирает статусы по твоим задачам
· За 24ч — получишь повестку следующего совещания
· <i>«Добавь в адженду: обсудить бюджет»</i>

💬 <b>Свободный чат</b>
Не нужно запоминать команды — пиши как человеку."""

CHAIRMAN_EXTRA = """

🔑 <b>Управление</b>
· Загрузи файл из Plaud (.txt/.pdf) — разберу протокол и создам задачи
· <i>«Создай задачу для Екатерины: подготовить отчёт до 15 марта»</i>
· <i>«Все задачи»</i> — статус по всем участникам

📊 <b>Аналитика</b>
· <i>«Дашборд»</i> — общая картина выполнения
· <i>«Аналитика»</i> — статистика, просрочки, динамика
· <i>«Гант»</i> — PDF-диаграмма всех задач

📅 <b>Планирование совещаний</b>
· <i>«Назначь совещание 15.03.2026 Итоги Q1»</i>
· <i>«Подготовь адженду»</i> — AI-повестка на основе задач
· Авто-сбор статусов за 48ч до совещания
· Авто-рассылка повестки всем участникам за 24ч"""

STAKEHOLDER_INTRO = """💎 <b>AI-секретарь — Акционер</b>

Привет, <b>{name}</b>! Я ваш AI-секретарь Совета Директоров.

💎 <b>Поставить задачу</b>
Нажми кнопку и напиши или продиктуй:
что нужно сделать, кто ответственный, срок.
Покажу карточку для подтверждения перед созданием.

💎 <b>Мои поручения</b>
Все поставленные вами задачи и их текущий статус.

📋 <b>Протоколы и задачи</b>
· Все протоколы совещаний доступны
· Статус по любым задачам
· Спрашивайте в свободной форме:
  <i>«Что решили на последнем совещании?»</i>

💬 <b>Свободный чат</b>
Пишите любые вопросы — отвечу на основе данных Совета."""



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
