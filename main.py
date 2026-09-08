"""Автопостинг Kleiwell в Threads.

Пайплайн одного прогона:
    анализ постов мебельщиков -> генерация через Claude -> модерация
      -> AUTO_PUBLISH=1 : публикуем сразу
      -> AUTO_PUBLISH=0 : шлём в Telegram, ждём кнопку

Запуск: python main.py
"""

import asyncio
import html
import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import analyzer
import brand
import config
import generator
import history as history_module
import moderation
from threads_api import ThreadsClient, ThreadsError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kleiwell")

PENDING_KEY = "pending_posts"
# Telegram режет callback_data до 64 байт, поэтому в кнопку кладём короткий токен.
ACTION_PUBLISH = "pub"
ACTION_SKIP = "skip"


# --------------------------------------------------------------------- клиенты

_threads_client = None


def threads_client():
    global _threads_client
    if _threads_client is None:
        _threads_client = ThreadsClient(
            access_token=config.THREADS_ACCESS_TOKEN,
            user_id=config.THREADS_USER_ID,
        )
    return _threads_client


# ------------------------------------------------------------------ публикация


class QuotaGuard(RuntimeError):
    """Своя квота исчерпана — публиковать не начинаем."""


def _check_quota(client):
    """Предохранитель, а не обязательный шаг.

    Если сам запрос квоты не прошёл (нет скоупа, сеть моргнула) — это не повод
    отменять публикацию: пробуем опубликовать и разбираем уже реальную ошибку.
    А вот исчерпанная квота останавливает нас до создания контейнера.
    """
    try:
        quota = client.publishing_limit()
    except ThreadsError as exc:
        log.warning("не удалось прочитать квоту, публикуем вслепую: %s", exc)
        return

    if quota["left"] < config.MIN_QUOTA_LEFT:
        raise QuotaGuard(
            f"суточная квота почти исчерпана: осталось {quota['left']} "
            f"из {quota['total']}, порог MIN_QUOTA_LEFT={config.MIN_QUOTA_LEFT}"
        )
    log.info("квота Threads: %d из %d свободно", quota["left"], quota["total"])


def _publish_sync(text):
    """Блокирующая публикация с проверкой суточной квоты."""
    client = threads_client()
    _check_quota(client)
    post_id = client.post_text(text)
    return post_id, client.permalink(post_id)


async def publish(text):
    return await asyncio.to_thread(_publish_sync, text)


# -------------------------------------------------------------------- вывод в TG


def _format_card(post, index=None, total=None):
    head = "Черновик" if index is None else f"Черновик {index}/{total}"
    return (
        f"<b>{head}</b> · {html.escape(post.angle)} · {len(post.text)} симв.\n"
        f"<i>{html.escape(post.why_viral)}</i>\n\n"
        f"{html.escape(post.text)}"
    )


def _keyboard(token):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 В Threads", callback_data=f"{ACTION_PUBLISH}:{token}"),
                InlineKeyboardButton("🗑 Пропустить", callback_data=f"{ACTION_SKIP}:{token}"),
            ]
        ]
    )


async def _send_drafts(context, posts):
    pending = context.application.bot_data.setdefault(PENDING_KEY, {})

    for i, post in enumerate(posts, start=1):
        token = uuid.uuid4().hex[:12]
        pending[token] = post
        await context.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=_format_card(post, i, len(posts)),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(token),
        )


async def _notify(context, text):
    await context.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# --------------------------------------------------------------------- пайплайн


def _prepare_sync():
    """Анализ + генерация + модерация. Всё блокирующее — в одном месте."""
    store = history_module.History()
    published_texts = store.published_texts()

    signal = analyzer.gather_market_signal(threads_client())
    raw_posts = generator.generate_posts(signal, published_texts)

    accepted, rejected = [], []
    # Новые посты проверяем и против истории, и против уже принятых в этом прогоне.
    seen = list(published_texts)

    for post in raw_posts:
        try:
            post.text = moderation.check(post.text, seen)
        except moderation.Rejected as exc:
            log.warning("отбраковано (%s): %s", exc, post.text[:70])
            rejected.append((post, str(exc)))
            continue
        seen.append(post.text)
        accepted.append(post)

    return signal, accepted, rejected


async def run_pipeline(context: ContextTypes.DEFAULT_TYPE):
    log.info("=== прогон пайплайна ===")
    try:
        signal, accepted, rejected = await asyncio.to_thread(_prepare_sync)
    except Exception as exc:
        log.exception("пайплайн упал")
        await _notify(context, f"⚠️ Пайплайн упал: <code>{html.escape(str(exc))}</code>")
        return

    source_note = (
        "поиск Threads" if signal["source"] == analyzer.SOURCE_THREADS else "локальные боли"
    )

    if not accepted:
        reasons = "\n".join(f"• {reason}" for _, reason in rejected) or "модель ничего не вернула"
        await _notify(
            context,
            f"⚠️ Ни один пост не прошёл модерацию (данные: {source_note}).\n{reasons}",
        )
        return

    if rejected:
        log.info("отбраковано %d из %d", len(rejected), len(accepted) + len(rejected))

    if config.AUTO_PUBLISH:
        await _auto_publish(context, accepted, source_note)
    else:
        await _notify(
            context,
            f"🧪 Готово {len(accepted)} черновик(ов) (данные: {source_note}). "
            f"Автопостинг выключен — жду кнопку.",
        )
        await _send_drafts(context, accepted)


async def _auto_publish(context, accepted, source_note):
    """Публикует один пост за прогон, остальные показывает как альтернативы."""
    store = history_module.History()
    post = accepted[0]

    try:
        post_id, link = await publish(post.text)
    except Exception as exc:
        store.add(
            post.text,
            angle=post.angle,
            status=history_module.STATUS_FAILED,
            note=str(exc),
        )
        log.exception("автопубликация не удалась")
        await _notify(
            context, f"❌ Автопубликация не удалась: <code>{html.escape(str(exc))}</code>"
        )
        return

    store.add(post.text, angle=post.angle, status=history_module.STATUS_PUBLISHED, post_id=post_id)

    tail = f'\n<a href="{link}">Открыть в Threads</a>' if link else ""
    alternatives = ""
    if len(accepted) > 1:
        alternatives = f"\n\nНе пошли в этот раз: {len(accepted) - 1} шт."

    await _notify(
        context,
        f"✅ Опубликовано автоматически (данные: {source_note})\n"
        f"<i>{html.escape(post.angle)}</i>\n\n"
        f"{html.escape(post.text)}{tail}{alternatives}",
    )


# ------------------------------------------------------------------- обработчики


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, _, token = (query.data or "").partition(":")
    pending = context.application.bot_data.get(PENDING_KEY, {})
    post = pending.pop(token, None)

    if post is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Черновик уже не в памяти — бот перезапускался. Сгенерируй заново: /now"
        )
        return

    store = history_module.History()

    if action == ACTION_SKIP:
        store.add(post.text, angle=post.angle, status=history_module.STATUS_SKIPPED)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🗑 Пропущено.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    status_msg = await query.message.reply_text("⏳ Публикую в Threads...")

    try:
        post_id, link = await publish(post.text)
    except Exception as exc:
        store.add(
            post.text,
            angle=post.angle,
            status=history_module.STATUS_FAILED,
            note=str(exc),
        )
        log.exception("публикация по кнопке не удалась")
        await status_msg.edit_text(f"❌ Не удалось: {exc}")
        return

    store.add(post.text, angle=post.angle, status=history_module.STATUS_PUBLISHED, post_id=post_id)
    await status_msg.edit_text(
        f"✅ Опубликовано.\n{link}" if link else f"✅ Опубликовано. id: {post_id}"
    )


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Генерирую...")
    await run_pipeline(context)


async def cmd_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        quota = await asyncio.to_thread(threads_client().publishing_limit)
    except ThreadsError as exc:
        await update.message.reply_text(f"Не удалось прочитать квоту: {exc}")
        return
    store = history_module.History()
    await update.message.reply_text(
        f"Threads: {quota['left']} из {quota['total']} публикаций свободно "
        f"(за скользящие 24 ч).\n"
        f"Автопостинг: {'включён' if config.AUTO_PUBLISH else 'выключен'}.\n"
        f"В истории бота: {store.published_count()} опубликованных."
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Бот {brand.BRAND} на связи.\n"
        f"/now — сгенерировать сейчас\n"
        f"/limit — квота Threads и статус\n"
        f"Интервал: {config.POST_INTERVAL_HOURS} ч. "
        f"Автопостинг: {'включён' if config.AUTO_PUBLISH else 'выключен'}."
    )


async def post_init(application):
    application.job_queue.run_repeating(
        run_pipeline,
        interval=config.POST_INTERVAL_HOURS * 3600,
        first=10,
        name="kleiwell-pipeline",
    )


def main():
    config.validate()

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("limit", cmd_limit))
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info(
        "старт: интервал %sч, постов за прогон %d, автопостинг %s",
        config.POST_INTERVAL_HOURS,
        config.POSTS_PER_RUN,
        "ВКЛ" if config.AUTO_PUBLISH else "выкл",
    )
    app.run_polling()


if __name__ == "__main__":
    main()
