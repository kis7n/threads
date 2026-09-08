"""Проверка постов перед публикацией.

Три вещи, ради которых это существует:
  * лимит Threads в 500 символов — иначе контейнер просто не создастся;
  * запрещённые заявления из brand.BANNED_PHRASES — модель может увлечься
    и пообещать то, за что потом отвечать;
  * повторы — бот работает годами, а тем конечное число.
"""

import logging
import re
from difflib import SequenceMatcher

import brand

log = logging.getLogger(__name__)

# Выше этого порога считаем пост пересказом уже опубликованного.
SIMILARITY_THRESHOLD = 0.72
MIN_POST_CHARS = 60


class Rejected(Exception):
    """Пост не прошёл модерацию."""


def normalize(text):
    """Схлопывает пробелы, но сохраняет разбивку на абзацы."""
    text = (text or "").replace("\r\n", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _fingerprint(text):
    """Только буквы и цифры в нижнем регистре — для сравнения на похожесть."""
    return re.sub(r"[^\w\s]", "", (text or "").lower())


def find_banned(text):
    lowered = (text or "").lower()
    return [phrase for phrase in brand.BANNED_PHRASES if phrase in lowered]


def similarity(a, b):
    return SequenceMatcher(None, _fingerprint(a), _fingerprint(b)).ratio()


def most_similar(text, previous_texts):
    """Ближайший похожий пост: (ratio, text) или (0.0, None)."""
    best_ratio, best_text = 0.0, None
    for old in previous_texts:
        ratio = similarity(text, old)
        if ratio > best_ratio:
            best_ratio, best_text = ratio, old
    return best_ratio, best_text


def check(text, previous_texts=(), threshold=SIMILARITY_THRESHOLD):
    """Возвращает нормализованный текст или поднимает Rejected."""
    text = normalize(text)

    if not text:
        raise Rejected("пустой текст")

    if len(text) < MIN_POST_CHARS:
        raise Rejected(f"слишком коротко: {len(text)} символов")

    if len(text) > brand.MAX_POST_CHARS:
        raise Rejected(
            f"{len(text)} символов, лимит Threads — {brand.MAX_POST_CHARS}"
        )

    banned = find_banned(text)
    if banned:
        raise Rejected("запрещённые формулировки: " + ", ".join(banned))

    ratio, twin = most_similar(text, previous_texts)
    if ratio >= threshold:
        raise Rejected(
            f"повтор уже опубликованного (совпадение {ratio:.0%}): "
            f"{twin[:80]}..."
        )

    return text
