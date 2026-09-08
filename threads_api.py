"""Клиент Threads Graph API.

Публикация в Threads двухшаговая и на этом чаще всего спотыкаются:
  1) POST /{user-id}/threads          -> создаёт контейнер, отдаёт creation_id
  2) POST /{user-id}/threads_publish  -> публикует контейнер по creation_id

Между шагами контейнер должен дойти до статуса FINISHED. Контейнер живёт
24 часа, потом протухает (EXPIRED).

Лимиты, под которые тут всё построено:
  * 500 символов на пост;
  * 250 публикаций через API за скользящие 24 часа;
  * 500 запросов keyword_search за скользящие 7 дней.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.threads.net"
API = f"{GRAPH_HOST}/v1.0"

MAX_POST_CHARS = 500

# Статусы контейнера
STATUS_FINISHED = "FINISHED"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_ERROR = "ERROR"
STATUS_EXPIRED = "EXPIRED"
STATUS_PUBLISHED = "PUBLISHED"


class ThreadsError(RuntimeError):
    """Любая ошибка Threads API."""

    def __init__(self, message, code=None, subcode=None, payload=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.payload = payload or {}


class ThreadsPermissionError(ThreadsError):
    """Не выдан нужный скоуп (например, threads_keyword_search)."""


class ThreadsRateLimitError(ThreadsError):
    """Упёрлись в квоту публикаций или поиска."""


# Коды Graph API, означающие нехватку прав.
_PERMISSION_CODES = {10, 200, 803}
# Коды троттлинга.
_RATE_LIMIT_CODES = {4, 17, 32, 613}


class ThreadsClient:
    def __init__(self, access_token, user_id="me", timeout=30, session=None):
        if not access_token:
            raise ValueError("access_token обязателен")
        self.access_token = access_token
        self.user_id = user_id or "me"
        self.timeout = timeout
        self._session = session or requests.Session()

    # ------------------------------------------------------------------ HTTP

    def _request(self, method, url, params=None, data=None):
        params = dict(params or {})
        params["access_token"] = self.access_token

        try:
            resp = self._session.request(
                method, url, params=params, data=data, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ThreadsError(f"сеть недоступна: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError:
            if resp.ok:
                raise ThreadsError(f"не-JSON ответ от Threads: {resp.text[:200]}")
            raise ThreadsError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        if isinstance(payload, dict) and "error" in payload:
            self._raise_for_error(payload["error"], payload)

        if not resp.ok:
            raise ThreadsError(f"HTTP {resp.status_code}: {payload}", payload=payload)

        return payload

    @staticmethod
    def _raise_for_error(error, payload):
        message = error.get("message", "неизвестная ошибка Threads")
        code = error.get("code")
        subcode = error.get("error_subcode")
        text = message.lower()

        if code in _PERMISSION_CODES or "permission" in text or "scope" in text:
            raise ThreadsPermissionError(message, code, subcode, payload)
        if code in _RATE_LIMIT_CODES or "rate limit" in text or "quota" in text:
            raise ThreadsRateLimitError(message, code, subcode, payload)
        raise ThreadsError(message, code, subcode, payload)

    def _get(self, path, **params):
        return self._request("GET", f"{API}/{path.lstrip('/')}", params=params)

    def _post(self, path, **data):
        return self._request("POST", f"{API}/{path.lstrip('/')}", data=data)

    # ------------------------------------------------------------------ квота

    def publishing_limit(self):
        """Сколько публикаций осталось в скользящем окне 24 часа.

        Возвращает {"used": int, "total": int, "left": int}.
        """
        payload = self._get(
            f"{self.user_id}/threads_publishing_limit", fields="quota_usage,config"
        )
        rows = payload.get("data") or [{}]
        row = rows[0]
        used = int(row.get("quota_usage", 0))
        total = int((row.get("config") or {}).get("quota_total", 250))
        return {"used": used, "total": total, "left": max(total - used, 0)}

    # -------------------------------------------------------------- публикация

    def create_text_container(self, text):
        """Шаг 1: контейнер с текстовым постом."""
        text = (text or "").strip()
        if not text:
            raise ValueError("пустой текст поста")
        if len(text) > MAX_POST_CHARS:
            raise ValueError(
                f"пост {len(text)} символов, лимит Threads — {MAX_POST_CHARS}"
            )

        payload = self._post(f"{self.user_id}/threads", media_type="TEXT", text=text)
        creation_id = payload.get("id")
        if not creation_id:
            raise ThreadsError(f"Threads не вернул id контейнера: {payload}")
        return creation_id

    def container_status(self, creation_id):
        payload = self._get(str(creation_id), fields="status,error_message")
        return payload.get("status"), payload.get("error_message")

    def wait_until_finished(self, creation_id, timeout=90, poll_interval=3):
        """Ждём, пока контейнер станет FINISHED.

        Для текстовых постов это почти всегда мгновенно, но статус ERROR/EXPIRED
        надо поймать до publish, иначе publish вернёт невнятную ошибку.
        """
        deadline = time.monotonic() + timeout
        last_status = None

        while True:
            status, error_message = self.container_status(creation_id)
            last_status = status

            if status in (STATUS_FINISHED, STATUS_PUBLISHED):
                return status
            if status == STATUS_ERROR:
                raise ThreadsError(
                    f"контейнер {creation_id} в статусе ERROR: "
                    f"{error_message or 'без описания'}"
                )
            if status == STATUS_EXPIRED:
                raise ThreadsError(f"контейнер {creation_id} протух (EXPIRED)")

            if time.monotonic() >= deadline:
                raise ThreadsError(
                    f"контейнер {creation_id} не дошёл до {STATUS_FINISHED} "
                    f"за {timeout}с, последний статус: {last_status}"
                )
            time.sleep(poll_interval)

    def publish_container(self, creation_id):
        """Шаг 2: публикация готового контейнера."""
        payload = self._post(f"{self.user_id}/threads_publish", creation_id=creation_id)
        post_id = payload.get("id")
        if not post_id:
            raise ThreadsError(f"Threads не вернул id поста: {payload}")
        return post_id

    def post_text(self, text, wait_timeout=90):
        """Полный цикл: контейнер -> ожидание -> публикация. Возвращает id поста."""
        creation_id = self.create_text_container(text)
        log.info("контейнер создан: %s", creation_id)
        self.wait_until_finished(creation_id, timeout=wait_timeout)
        post_id = self.publish_container(creation_id)
        log.info("опубликовано: %s", post_id)
        return post_id

    def permalink(self, post_id):
        try:
            return self._get(str(post_id), fields="permalink").get("permalink")
        except ThreadsError as exc:
            log.warning("не удалось получить permalink для %s: %s", post_id, exc)
            return None

    def me(self):
        """Свой профиль: {'id': ..., 'username': ...}."""
        return self._get(self.user_id, fields="id,username")

    # ------------------------------------------------------------------ поиск

    def keyword_search(self, query, search_type="TOP", limit=25):
        """Публичные посты по ключевому слову.

        Требует скоуп threads_keyword_search (500 запросов на 7 дней).
        Без скоупа поднимает ThreadsPermissionError — вызывающий код должен
        уметь работать без поиска.
        """
        payload = self._get(
            "keyword_search",
            q=query,
            search_type=search_type,
            fields="id,text,username,permalink,timestamp",
            limit=limit,
        )
        return payload.get("data", []) or []

    # ------------------------------------------------------------------ токен

    def refresh_access_token(self):
        """Продлевает долгоживущий токен ещё на 60 дней.

        Токену должно быть больше 24 часов, иначе Threads откажет.
        Эндпоинт живёт в корне хоста, без префикса /v1.0.
        """
        payload = self._request(
            "GET",
            f"{GRAPH_HOST}/refresh_access_token",
            params={"grant_type": "th_refresh_token"},
        )
        token = payload.get("access_token")
        if token:
            self.access_token = token
        return payload
