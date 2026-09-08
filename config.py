"""Конфигурация из окружения.

Все переменные читаются один раз при импорте. validate() собирает *все*
отсутствующие ключи сразу, а не падает на первом — так меньше циклов
"запустил → упало → дописал одну строчку в .env".
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: str, cast):
    raw = os.getenv(name, default).strip()
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} — ожидалось число") from None


# ---------- Claude ----------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")

# ---------- Telegram ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------- Threads ----------
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "me")

# ---------- Поведение ----------
AUTO_PUBLISH = _flag("AUTO_PUBLISH")
POST_INTERVAL_HOURS = _num("POST_INTERVAL_HOURS", "8", float)
POSTS_PER_RUN = _num("POSTS_PER_RUN", "3", int)
MIN_QUOTA_LEFT = _num("MIN_QUOTA_LEFT", "5", int)

DATA_DIR = os.getenv("DATA_DIR", "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
MARKET_CACHE_PATH = os.path.join(DATA_DIR, "market_cache.json")

# Кеш анализа рынка. Threads даёт всего 500 keyword_search на 7 дней,
# поэтому дёргаем поиск раз в сутки, а не перед каждой генерацией.
MARKET_CACHE_TTL_HOURS = _num("MARKET_CACHE_TTL_HOURS", "24", float)

_REQUIRED = (
    ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ("THREADS_ACCESS_TOKEN", THREADS_ACCESS_TOKEN),
)


def validate() -> None:
    """Падает с перечнем всех недостающих переменных."""
    missing = [name for name, value in _REQUIRED if not value]
    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
            + ". Скопируй .env.example в .env и заполни."
        )
    if POSTS_PER_RUN < 1:
        raise ValueError("POSTS_PER_RUN должен быть >= 1")
    if POST_INTERVAL_HOURS <= 0:
        raise ValueError("POST_INTERVAL_HOURS должен быть > 0")
