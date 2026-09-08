"""Получение долгоживущего токена Threads — одной командой.

    python get_token.py

Threads принимает только HTTPS-редиректы, поэтому localhost не подойдёт.
Проще всего указать редиректом собственный сайт (например https://kleiwell.ru/):
после подтверждения браузер уедет туда, а код авторизации будет в адресной
строке. Поднимать сервер не нужно — код копируется руками.

Шаги:
    1. Скрипт печатает ссылку авторизации.
    2. Открываешь её, входишь в аккаунт Threads, подтверждаешь доступ.
    3. Копируешь адрес из адресной строки целиком и вставляешь сюда.
    4. Скрипт меняет код на короткий токен, короткий — на 60-дневный,
       и печатает готовые строки для .env.
"""

import argparse
import os
import sys
import urllib.parse

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://threads.net/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
EXCHANGE_URL = "https://graph.threads.net/access_token"

# По умолчанию просим только два скоупа, которые работают сразу.
#
# threads_keyword_search сюда намеренно не входит: со стандартным доступом он
# возвращает только посты самого тестировщика, а чтобы искать чужие (то есть
# посты мебельных фабрик), нужен Advanced Access через App Review. Если скоуп
# не настроен в приложении, запрос авторизации с ним может быть отклонён
# целиком — поэтому он добавляется явным флагом --with-search.
SCOPES = ["threads_basic", "threads_content_publish"]
SEARCH_SCOPE = "threads_keyword_search"


def ask(prompt, env_key=None):
    value = os.getenv(env_key) if env_key else None
    if value:
        print(f"{prompt}: {value}  (из .env)")
        return value
    value = input(f"{prompt}: ").strip()
    if not value:
        sys.exit(f"Пусто — {prompt} обязателен.")
    return value


def extract_code(pasted):
    """Достаёт code из вставленного адреса.

    Threads дописывает к редиректу '#_' — это не часть кода, и если его не
    срезать, обмен вернёт невнятную ошибку. Функция принимает и полный URL,
    и просто код.
    """
    pasted = pasted.strip().strip('"').strip("'")
    if "code=" in pasted:
        query = urllib.parse.urlparse(pasted).query
        code = urllib.parse.parse_qs(query).get("code", [""])[0]
    elif pasted.startswith("http"):
        # Адрес вставили, но кода в нём нет — вернуть сам адрес нельзя,
        # иначе он уедет в Threads как код авторизации.
        code = ""
    else:
        code = pasted
    return code.split("#")[0].strip()


def main():
    parser = argparse.ArgumentParser(description="Получение токена Threads")
    parser.add_argument(
        "--with-search",
        action="store_true",
        help=(
            f"дополнительно запросить {SEARCH_SCOPE}. Только если скоуп уже "
            "настроен в приложении, иначе авторизация может быть отклонена"
        ),
    )
    args = parser.parse_args()
    scopes = SCOPES + ([SEARCH_SCOPE] if args.with_search else [])

    print(__doc__)
    print("-" * 70)
    print(f"Запрашиваемые скоупы: {', '.join(scopes)}")
    if not args.with_search:
        print(
            f"({SEARCH_SCOPE} не запрошен — бот будет брать темы из brand.py.\n"
            " Добавить: python get_token.py --with-search)"
        )

    app_id = ask("App ID (Идентификатор приложения Threads)", "THREADS_APP_ID")
    app_secret = ask("App Secret (Секрет приложения)", "THREADS_APP_SECRET")
    redirect_uri = ask(
        "Redirect URI — точно как в настройках приложения", "THREADS_REDIRECT_URI"
    )

    if not redirect_uri.startswith("https://"):
        sys.exit("Redirect URI должен начинаться с https:// — Meta не принимает http.")

    params = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": ",".join(scopes),
            "response_type": "code",
        }
    )
    print("\n1) Открой эту ссылку и подтверди доступ:\n")
    print(f"   {AUTH_URL}?{params}\n")
    print("2) После подтверждения браузер уедет на твой redirect URI.")
    print("   Страница может показать 404 — это нормально, нужен только адрес.\n")

    code = extract_code(input("3) Вставь адрес из адресной строки целиком: "))
    if not code:
        sys.exit("Код не найден. Нужен адрес вида https://...?code=AQB...")
    print(f"\n   код получен: {code[:12]}...")

    print("\n   меняю код на короткий токен...")
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    payload = resp.json()
    if "access_token" not in payload:
        sys.exit(f"Threads отказал: {payload}")

    short_token, user_id = payload["access_token"], payload.get("user_id")
    print(f"   короткий токен есть, user_id={user_id}")

    print("   меняю короткий токен на 60-дневный...")
    resp = requests.get(
        EXCHANGE_URL,
        params={
            "grant_type": "th_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
        timeout=30,
    )
    payload = resp.json()
    if "access_token" not in payload:
        sys.exit(f"Обмен не удался: {payload}")

    long_token = payload["access_token"]
    days = int(payload.get("expires_in", 0)) // 86400

    print("\n" + "=" * 70)
    print(f"Готово. Токен живёт {days} дней. Впиши в .env:\n")
    print(f"THREADS_ACCESS_TOKEN={long_token}")
    if user_id:
        print(f"THREADS_USER_ID={user_id}")
    print("\n" + "=" * 70)
    print(
        "Продлить до истечения срока:\n"
        "  python -c \"from threads_api import ThreadsClient; \\\n"
        "  print(ThreadsClient('ТОКЕН').refresh_access_token())\""
    )


if __name__ == "__main__":
    main()
