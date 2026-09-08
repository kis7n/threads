"""Анализ постов мебельных фабрик — сырьё для генератора.

Threads даёт 500 keyword_search на скользящие 7 дней (~70 в день), а тем у нас
шесть. Поэтому результат кешируется на MARKET_CACHE_TTL_HOURS: при интервале
постинга 8 часов это ~6 запросов в сутки вместо 18.

Если скоуп threads_keyword_search не выдан, анализ не падает, а переключается
на фолбэк — список болей аудитории из brand.py. Генерация продолжает работать,
просто без свежих формулировок из ленты.
"""

import json
import logging
import os
import time

import brand
import config
from threads_api import ThreadsError, ThreadsPermissionError, ThreadsRateLimitError

log = logging.getLogger(__name__)

SOURCE_THREADS = "threads"
SOURCE_FALLBACK = "fallback"

# Посты короче этого — реакции вроде "огонь", смысла в них нет.
_MIN_TEXT_LEN = 40
# Сколько наблюдений отдаём генератору.
_MAX_OBSERVATIONS = 24


def _load_cache():
    try:
        with open(config.MARKET_CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_cache(payload):
    os.makedirs(os.path.dirname(config.MARKET_CACHE_PATH) or ".", exist_ok=True)
    tmp = f"{config.MARKET_CACHE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config.MARKET_CACHE_PATH)


def _is_fresh(cache):
    if not cache:
        return False
    age_hours = (time.time() - cache.get("fetched_at", 0)) / 3600
    return age_hours < config.MARKET_CACHE_TTL_HOURS


def _fallback_signal(reason):
    return {
        "source": SOURCE_FALLBACK,
        "reason": reason,
        "fetched_at": time.time(),
        "observations": [
            {"text": pain, "username": None} for pain in brand.AUDIENCE_PAINS
        ],
    }


def _own_username(client):
    """Свой username, чтобы не анализировать собственные посты.

    Со стандартным доступом keyword_search возвращает только посты самого
    тестировщика — то есть наши же. Без этого фильтра бот скармливал бы себе
    собственную выдачу и ходил по кругу.
    """
    try:
        return (client.me().get("username") or "").lower()
    except ThreadsError as exc:
        log.warning("не удалось узнать свой username: %s", exc)
        return ""


def _collect(client):
    """Обходит SEARCH_QUERIES и собирает уникальные посты."""
    seen_ids = set()
    observations = []
    own = _own_username(client)

    for query in brand.SEARCH_QUERIES:
        try:
            results = client.keyword_search(query, search_type="TOP")
        except ThreadsPermissionError:
            raise
        except ThreadsRateLimitError as exc:
            log.warning("keyword_search уперся в лимит на %r: %s", query, exc)
            break
        except ThreadsError as exc:
            log.warning("keyword_search упал на %r: %s", query, exc)
            continue

        for item in results:
            post_id = item.get("id")
            text = (item.get("text") or "").strip()
            if not text or post_id in seen_ids or len(text) < _MIN_TEXT_LEN:
                continue
            if own and (item.get("username") or "").lower() == own:
                continue
            seen_ids.add(post_id)
            observations.append(
                {
                    "text": text,
                    "username": item.get("username"),
                    "query": query,
                    "permalink": item.get("permalink"),
                }
            )

    return observations


def gather_market_signal(client, force=False):
    """Что сейчас обсуждают мебельщики.

    Возвращает dict с ключами source / observations / fetched_at.
    Никогда не поднимает исключение — при любой проблеме отдаёт фолбэк.
    """
    cache = _load_cache()
    if not force and _is_fresh(cache):
        log.info(
            "анализ рынка из кеша (%s, %d наблюдений)",
            cache.get("source"),
            len(cache.get("observations", [])),
        )
        return cache

    if client is None:
        return _fallback_signal("клиент Threads не сконфигурирован")

    try:
        observations = _collect(client)
    except ThreadsPermissionError as exc:
        log.warning(
            "нет скоупа threads_keyword_search (%s) — работаем на болях из brand.py",
            exc,
        )
        signal = _fallback_signal(f"нет скоупа threads_keyword_search: {exc}")
        _save_cache(signal)
        return signal

    if not observations:
        log.warning("keyword_search ничего не вернул — фолбэк на brand.AUDIENCE_PAINS")
        signal = _fallback_signal("поиск не дал результатов")
        _save_cache(signal)
        return signal

    signal = {
        "source": SOURCE_THREADS,
        "reason": None,
        "fetched_at": time.time(),
        "observations": observations[:_MAX_OBSERVATIONS],
    }
    _save_cache(signal)
    log.info("анализ рынка: %d постов из Threads", len(signal["observations"]))
    return signal


def format_for_prompt(signal, limit=_MAX_OBSERVATIONS):
    """Наблюдения в виде текстового блока для промпта."""
    observations = signal.get("observations", [])[:limit]
    if not observations:
        return "(данных нет)"

    lines = []
    for item in observations:
        text = " ".join(item["text"].split())
        if len(text) > 300:
            text = text[:297] + "..."
        author = item.get("username")
        lines.append(f"- {'@' + author + ': ' if author else ''}{text}")
    return "\n".join(lines)
