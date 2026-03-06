"""Pre-configured board members mapping: transcript names -> telegram usernames."""
from __future__ import annotations

# Maps all known name variations from transcripts to a canonical member record.
# transcript_aliases: list of names as they appear in Plaud transcripts
# username: Telegram username (without @)
# display_name: how the bot addresses this person
# is_chairman: whether this person is the chairman

BOARD_MEMBERS = [
    {
        "display_name": "Сергей С",
        "username": "Sergstam",
        "is_chairman": True,
        "transcript_aliases": ["stamsergey", "Sergstam", "Сергей Стамбровский", "Сергей С", "Сергей С."],
    },
    {
        "display_name": "Ренат Ш",
        "username": "Chess2707",
        "is_chairman": False,
        "transcript_aliases": ["Ренат Ш", "Ренат", "Ренат Ш."],
    },
    {
        "display_name": "Данила О",
        "username": "DO009",
        "is_chairman": False,
        "transcript_aliases": ["Данила О", "Данила", "Данила О."],
    },
    {
        "display_name": "Виктория М",
        "username": "vikamikhno",
        "is_chairman": False,
        "transcript_aliases": ["Виктория М", "Виктория", "Вика", "Виктория М."],
    },
    {
        "display_name": "Надежда П",
        "username": "nadezhda_hr",
        "is_chairman": False,
        "transcript_aliases": ["Надежда П", "Надежда", "Надежда П."],
    },
    {
        "display_name": "Катя Б",
        "username": "katerina_bokova",
        "is_chairman": False,
        "transcript_aliases": ["Катя Б", "Катя Б.", "Екатерина Б"],
    },
    {
        "display_name": "Сергей И",
        "username": "s5069561",
        "is_chairman": False,
        "transcript_aliases": ["Сергей И", "Сергей И."],
    },
    {
        "display_name": "Дмитрий Е",
        "username": "Dmitry_Egorov",
        "is_chairman": False,
        "transcript_aliases": ["Дмитрий Е", "Дмитрий Е.", "Дмитрий"],
    },
    {
        "display_name": "Егор",
        "username": "egorv",
        "is_chairman": False,
        "transcript_aliases": ["Егор"],
    },
    {
        "display_name": "Дарья Ю",
        "username": None,  # TBD
        "is_chairman": False,
        "transcript_aliases": ["Дарья Ю", "Дарья", "Дарья Ю."],
    },
    {
        "display_name": "Мария С",
        "username": None,  # TBD
        "is_chairman": False,
        "transcript_aliases": ["Мария С", "Мария", "Мария С.", "Мария Смирнова"],
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
