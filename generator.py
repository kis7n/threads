"""Генерация постов через Claude.

Заменяет прежний вызов OpenAI. Отличия от исходной версии:
  * структурированный вывод вместо разбора текста по разделителю ###POST###
    — модель отдаёт валидный JSON, парсить руками нечего;
  * в промпт уходит анализ реальных постов мебельщиков и список уже
    опубликованного, чтобы бот не ходил по кругу;
  * включён server-side fallback: если запрос упрётся в классификатор,
    ответ придёт от резервной модели, а не превратится в исключение.
"""

import logging
import random

import anthropic
from pydantic import BaseModel, Field

import analyzer
import brand
import config

log = logging.getLogger(__name__)

# Резервная маршрутизация при отказе классификатора.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class GeneratedPost(BaseModel):
    text: str = Field(
        description=(
            f"Готовый пост для Threads на русском, не длиннее "
            f"{brand.TARGET_POST_CHARS} символов, без хэштегов"
        )
    )
    angle: str = Field(description="Заход, использованный в посте")
    hook: str = Field(description="Первая строка поста — крючок")
    why_viral: str = Field(
        description="Одно предложение: почему это зацепит мебельщика"
    )


class PostBatch(BaseModel):
    posts: list[GeneratedPost]


def _system_prompt():
    advantages = "\n".join(f"- {item}" for item in brand.ADVANTAGES)
    rules = "\n".join(f"- {item}" for item in brand.CONTENT_RULES)
    applications = "\n".join(f"- {item}" for item in brand.APPLICATIONS)
    specs = "\n".join(f"- {key}: {value}" for key, value in brand.SPECS.items())
    skus = "\n".join(
        f"- {p['sku']} — {p['tagline']}; под {p['best_for']}; "
        f"схватывается за {p['set_time']}" + (" (хит продаж)" if p["hit"] else "")
        for p in brand.PRODUCTS
    )

    return f"""Ты пишешь вирусные посты для Threads от лица бренда {brand.BRAND} \
({brand.SITE}) — это {brand.PRODUCT['category']}. Производитель — \
{brand.LEGAL_ENTITY}.

ЧТО ПРОДАЁМ
{brand.PRODUCT['what']}

ДВЕ МАРКИ
{skus}
Выбор между ними — сам по себе хорошая тема: под тонкий поролон и ткань берут
215, на каркас и блоки — 225.

ХАРАКТЕРИСТИКИ
{specs}

ЦЕНЫ (других цифр по деньгам не существует)
- {brand.PRICING['base']}, {brand.PRICING['tiers']}
- минимальный заказ — {brand.PRICING['min_order']}

ГДЕ ПРИМЕНЯЕТСЯ
{applications}

ПРЕИМУЩЕСТВА (единственный источник фактов о продукте)
{advantages}

КОМУ ПИШЕМ
{brand.AUDIENCE}. Это люди с производства: они сами клеят, сами считают \
себестоимость и сами разбирают рекламации. Они мгновенно чувствуют \
рекламный текст, написанный маркетологом, который не был в цеху.

КАК ПИСАТЬ
- смешно и узнаваемо: цеховой юмор, а не корпоративные шутки
- короткие абзацы, живой язык, без воды
- сильная первая строка, которая работает без контекста
- конкретика цеха: поролон, каркас, обивка, распылитель, рекламация, смена
- продукт появляется как решение боли, а не как баннер
- упоминай {brand.BRAND} мягко: пост должен быть смешным даже без бренда
- максимум одно упоминание бренда на пост, {brand.SITE} — не в каждом посте
- если называешь марку — называй конкретную (225 или 215), а не «наш клей»
- без хэштегов, без эмодзи-спама (0-1 эмодзи максимум)
- жёсткий лимит: {brand.TARGET_POST_CHARS} символов на пост

ЧЕГО НЕ ДЕЛАТЬ
{rules}

Каждый пост в выдаче — про другую боль и в другом заходе. Три одинаковых \
поста разными словами считаются провалом задачи."""


def _user_prompt(market_signal, previous_texts, count):
    angles = random.sample(brand.ANGLES, k=min(count, len(brand.ANGLES)))
    triggers = random.sample(brand.TRIGGERS, k=min(count, len(brand.TRIGGERS)))
    pains = random.sample(brand.AUDIENCE_PAINS, k=min(count + 2, len(brand.AUDIENCE_PAINS)))

    source = market_signal.get("source")
    if source == analyzer.SOURCE_THREADS:
        market_header = (
            "О ЧЁМ СЕЙЧАС ПИШУТ В THREADS МЕБЕЛЬЩИКИ (свежая выдача поиска).\n"
            "Используй их язык, формулировки и болевые точки. Не цитируй дословно "
            "и не упоминай авторов."
        )
    else:
        market_header = (
            "ПОИСК ПО THREADS НЕДОСТУПЕН, работаем по известным болям аудитории."
        )

    market_block = analyzer.format_for_prompt(market_signal)

    if previous_texts:
        published = "\n".join(
            f"- {' '.join(text.split())[:140]}" for text in previous_texts[:15]
        )
        history_block = (
            "УЖЕ ОПУБЛИКОВАНО — не повторяй эти мысли, заходы и формулировки:\n"
            f"{published}"
        )
    else:
        history_block = "Опубликованных постов пока нет — это первый заход."

    return f"""{market_header}

{market_block}

{history_block}

ЗАДАНИЕ
Напиши {count} разных поста.

Распредели по ним:
- заходы: {', '.join(angles)}
- триггеры: {', '.join(triggers)}
- боли на выбор: {'; '.join(pains)}

Каждый пост — самостоятельный, работает без остальных двух."""


def _client():
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def generate_posts(market_signal, previous_texts=(), count=None):
    """Возвращает список GeneratedPost. Модерацию делает вызывающий код."""
    count = count or config.POSTS_PER_RUN

    response = _client().beta.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=8000,
        system=_system_prompt(),
        messages=[{"role": "user", "content": _user_prompt(market_signal, previous_texts, count)}],
        output_format=PostBatch,
        betas=[_FALLBACK_BETA],
        fallbacks="default",
    )

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise RuntimeError(
            "Claude отклонил запрос на генерацию"
            + (f" ({details.category})" if details else "")
        )

    batch = response.parsed_output
    if batch is None:
        raise RuntimeError("Claude вернул ответ без структурированной части")

    posts = batch.posts[:count]
    log.info("сгенерировано постов: %d", len(posts))
    return posts
