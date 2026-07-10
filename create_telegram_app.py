#!/usr/bin/env python3
"""Автоматическое создание Telegram приложения через my.telegram.org.

Запуск: python3 create_telegram_app.py +79XXXXXXXXX
"""
import sys
import re
import requests

def create_app(phone, app_name="VK Bridge"):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    })

    # 1. GET login page
    print(">>> Загружаю my.telegram.org...")
    r = session.get("https://my.telegram.org/auth")
    if r.status_code != 200:
        print(f"❌ Ошибка загрузки: {r.status_code}")
        return

    # Extract hash
    hash_match = re.search(r'name="hash"\s+value="([^"]+)"', r.text)
    if not hash_match:
        print("❌ Не нашёл hash на странице.")
        return
    auth_hash = hash_match.group(1)
    print(f"   Hash: {auth_hash[:20]}...")

    # 2. Send phone number
    print(f">>> Отправляю номер: {phone}")
    r = session.post(
        "https://my.telegram.org/auth/send_password",
        data={"phone": phone, "hash": auth_hash},
        allow_redirects=True,
    )
    print(f"   Статус: {r.status_code}")

    if "invalid" in r.text.lower() or "error" in r.text.lower():
        print("❌ Ошибка отправки номера")
        return

    # 3. Get code from user
    print("\n>>> Проверь Telegram — тебе пришёл код подтверждения.")
    code = input(">>> Введи код: ").strip()

    # 4. Login with code
    print(">>> Вхожу...")
    r = session.post(
        "https://my.telegram.org/auth/login",
        data={
            "phone": phone,
            "random_hash": auth_hash,
            "password": code,
            "remember": "1",
        },
        allow_redirects=True,
    )

    # Check if we need 2FA password
    if "password" in r.text.lower() and "two-step" in r.text.lower():
        print("\n⚠️ Нужна двухфакторная аутентификация!")
        password = input(">>> Введи пароль 2FA: ").strip()
        r = session.post(
            "https://my.telegram.org/auth/login",
            data={
                "phone": phone,
                "random_hash": auth_hash,
                "password": password,
                "remember": "1",
            },
            allow_redirects=True,
        )

    # 5. Go to apps page
    print(">>> Открываю страницу приложений...")
    r = session.get("https://my.telegram.org/apps")

    # Check for existing api_id
    existing_id = re.search(r'api_id[^0-9]*([0-9]{4,})', r.text)
    existing_hash = re.search(r'api_hash[^a-f0-9]*([a-f0-9]{32})', r.text)

    if existing_id and existing_hash:
        api_id = existing_id.group(1)
        api_hash = existing_hash.group(1)
        print(f"\n✅ Найдено существующее приложение!")
        print(f"   api_id: {api_id}")
        print(f"   api_hash: {api_hash}")
        return

    # 6. Create new app
    print(f">>> Создаю приложение «{app_name}»...")
    app_hash_match = re.search(r'name="hash"\s+value="([^"]+)"', r.text)
    if not app_hash_match:
        print("❌ Не нашёл hash для создания приложения")
        return

    r = session.post(
        "https://my.telegram.org/apps/create",
        data={
            "hash": app_hash_match.group(1),
            "app_title": app_name,
            "app_shortname": app_name.replace(" ", ""),
            "app_url": "",
            "app_platform": "desktop",
            "app_desc": "Server bridge for VK bot",
        },
        allow_redirects=True,
    )

    # 7. Get the credentials
    r = session.get("https://my.telegram.org/apps")
    new_id = re.search(r'api_id[^0-9]*([0-9]{4,})', r.text)
    new_hash = re.search(r'api_hash[^a-f0-9]*([a-f0-9]{32})', r.text)

    if new_id and new_hash:
        print(f"\n✅ Приложение создано!")
        print(f"   api_id: {new_id.group(1)}")
        print(f"   api_hash: {new_hash.group(1)}")
    else:
        print("❌ Не удалось создать приложение.")
        print(f"   Текст ответа (500 символов): {r.text[:500]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 create_telegram_app.py +79XXXXXXXXX [AppName]")
        sys.exit(1)

    phone = sys.argv[1]
    app_name = sys.argv[2] if len(sys.argv) > 2 else "VK Bridge"
    create_app(phone, app_name)
