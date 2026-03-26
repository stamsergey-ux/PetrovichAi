"""Pre-configured board members mapping: transcript names -> telegram usernames."""
from __future__ import annotations

# Maps all known name variations from transcripts to a canonical member record.
# transcript_aliases: list of names as they appear in Plaud transcripts
# username: Telegram username (without @)
# display_name: how the bot addresses this person
# is_chairman: whether this person is the chairman

BOARD_MEMBERS = [
    {
        "display_name": "Сергей Стамболцян",
        "username": "Sergstam",
        "is_chairman": True,
        "transcript_aliases": ["stamsergey", "Sergstam", "Сергей Стамболцян", "Сергей С", "Сергей С."],
    },
    {
        "display_name": "Ренат Шаяхметов",
        "username": "Chess2707",
        "is_chairman": False,
        "transcript_aliases": ["Ренат Шаяхметов", "Ренат Ш", "Ренат", "Ренат Ш."],
    },
    {
        "display_name": "Данила Овчаров",
        "username": "DO009",
        "is_chairman": False,
        "transcript_aliases": ["Данила Овчаров", "Данила О", "Данила", "Данила О."],
    },
    {
        "display_name": "Виктория Михно",
        "username": "vikamikhno",
        "is_chairman": True,
        "transcript_aliases": ["Виктория Михно", "Виктория М", "Виктория", "Вика", "Виктория М."],
    },
    {
        "display_name": "Надежда Петрушенко",
        "username": "nadezhda_hr",
        "is_chairman": False,
        "transcript_aliases": ["Надежда Петрушенко", "Надежда П", "Надежда", "Надежда П."],
    },
    {
        "display_name": "Екатерина Бокова",
        "username": "katerina_bokova",
        "is_chairman": False,
        "transcript_aliases": ["Екатерина Бокова", "Катя Бокова", "Катя Б", "Катя Б.", "Екатерина Б"],
    },
    {
        "display_name": "Сергей Иванов",
        "username": "s5069561",
        "is_chairman": False,
        "transcript_aliases": ["Сергей Иванов", "Сергей И", "Сергей И."],
    },
    {
        "display_name": "Дмитрий Егоров",
        "username": "Dmitry_Egorov",
        "is_chairman": False,
        "transcript_aliases": ["Дмитрий Егоров", "Дмитрий Е", "Дмитрий Е.", "Дмитрий"],
    },
    {
        "display_name": "Егор Великогло",
        "username": "egorv",
        "is_chairman": False,
        "transcript_aliases": ["Егор Великогло", "Егор В", "Егор"],
    },
    {
        "display_name": "Лилия Мансурская",
        "username": "Lily_mans",
        "is_chairman": False,
        "transcript_aliases": ["Лилия Мансурская", "Лилия М", "Лилия", "Лилия М."],
    },
    {
        "display_name": "Евгений Ильчук",
        "username": "Evilchuk",
        "is_chairman": False,
        "transcript_aliases": ["Евгений Ильчук", "Евгений И", "Евгений", "Женя", "Евгений И."],
    },
    {
        "display_name": "Дарья Юрасова",
        "username": "DaryaYurasova",
        "is_chairman": False,
        "transcript_aliases": ["Дарья Юрасова", "Дарья Ю", "Дарья", "Дарья Ю."],
    },
    {
        "display_name": "Сергей Косинский",
        "username": "sergeykosinskiy",
        "is_chairman": False,
        "is_stakeholder": True,
        "transcript_aliases": ["Сергей Косинский", "Косинский", "Сергей К", "Сергей К."],
    },
    {
        "display_name": "Давид Кочаров",
        "username": "ProDavid",
        "is_chairman": False,
        "transcript_aliases": ["Давид Кочаров", "Давид К", "Давид", "Давид К."],
    },
    {
        "display_name": "Мария Смирнова",
        "username": "divo_divnoe_by_masha",
        "is_chairman": False,
        "transcript_aliases": ["Мария Смирнова", "Мария С", "Мария", "Мария С."],
    },
]


def find_member_by_transcript_name(name: str) -> dict | None:
    """Find a board member config by a name from transcript."""
    name_lower = name.lower().strip()
    for member in BOARD_MEMBERS:
        for alias in member["transcript_aliases"]:
            if alias.lower() == name_lower:
                return member
    # Partial match by first word
    first_word = name_lower.split()[0] if name_lower.split() else ""
    if first_word and len(first_word) > 2:
        for member in BOARD_MEMBERS:
            for alias in member["transcript_aliases"]:
                if alias.lower().startswith(first_word) or first_word in alias.lower():
                    return member
    return None
