# 🤖 VK ↔ Telegram · Tmux/Claude Control Suite

Два бота в одном репозитории с общим ядром:

1. **VK-бот** — превращает диалог с сообществом ВК в **полноценный клиент
   Telegram** (прокси) + пульт управления сервером (**tmux + Claude Code**).
   Нужен, когда Telegram недоступен напрямую, а ВК работает.
2. **Telegram-бот** — управление сервером и **Claude Code прямо из Telegram**:
   сессии, стриминг вывода моноширинно, пульт клавиш, планировщик. Замена
   Node-решений с расширенным функционалом.

Оба настраиваются и запускаются **независимо** (см. секции `vk` и `tgbot`
в конфиге). Общее ядро: tmux, планировщик, детект простоя, рендер вывода.

---

## ✨ Возможности

### ✈️ Telegram-прокси (как настоящий мессенджер)
- 📋 Чаты (личные → группы → каналы), пагинация, **поиск** (рус/лат транслит)
- 💬 Открыл чат → **просто пишешь**, уходит собеседнику; ответы приходят **живой лентой**
- 🖼 **Медиа в обе стороны** — фото и файлы (видео — только метка)
- 📁 **Папки** Telegram · 🌳 **форумы** открываются по топикам
- ⭐ Избранное · 🔕 Исключение чатов из непрочитанных
- 🔔 **Уведомления по таймеру** (30с/1/5/10/15 мин) — приходит, кто написал
- 🔀 **Несколько аккаунтов** на один VK ID (рабочий/личный) с переключением
- ♻️ Персистентность: после перезагрузки бот вернёт вас в диалог

### 🖥 Управление сервером (только для админа)
- Список tmux-сессий (тап = подключиться + живой вывод)
- 🤖 **Claude Code** и 🧠 **DeepClaude** одной кнопкой
- 💤 **Детект простоя**: если сессия молчит N минут (напр. Claude закончил
  задачу/петлю) — приходит уведомление с последними строками
- ⏰ Планировщик задач с пайплайнами: `/in 5m sess | cmd1 | cmd2 | 30s`

### 👑 Роли и админка
- **Админ** (первый VK ID) — полный доступ: tmux, Claude, планировщик, админка
- **Остальные** — только Telegram-меню
- Админ добавляет пользователей и выдаёт доступ **прямо из бота**

---

## 🚀 Установка

### Требования
- Linux, Python 3.9+
- `tmux` (для серверных функций): `sudo apt install tmux`

### Шаги
```bash
git clone https://github.com/<ваш-логин>/vk-tmux-tg-proxy.git
cd vk-tmux-tg-proxy

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Конфиг
mkdir -p ~/.vk-tmux-bot
cp config.example.yaml ~/.vk-tmux-bot/config.yaml
nano ~/.vk-tmux-bot/config.yaml     # заполнить (см. ниже)

# Запуск
.venv/bin/python run.py start
```

---

## ⚙️ Что заполнить в конфиге

Файл `~/.vk-tmux-bot/config.yaml` (пример — `config.example.yaml`):

### 1. Сообщество ВК → `vk.group_token`, `vk.group_id`
1. Создайте сообщество ВК (закрытое — чтобы никто не видел переписку).
2. **Управление → Настройки → Работа с API → Создать ключ**, права:
   «Сообщения сообщества».
3. **Long Poll API**: включить, тип событий — «Входящие сообщения», версия 5.199.
4. **Сообщения**: включить, «Возможности ботов» → ВКЛ.
5. `group_id` — число из адреса `vk.com/club<ID>`.

### 2. Ваш VK ID → `vk.admin_ids`, `vk.allowed_user_ids`
Узнать ID: [regvk.com/id](https://regvk.com/id/). Первый в `admin_ids` — админ.

### 3. Telegram → `telegram.api_id`, `telegram.api_hash`
С [my.telegram.org](https://my.telegram.org) → **API development tools**.
Если сайт недоступен — можно взять публичные значения Telegram Desktop
(для личного использования): `api_id: 17349`,
`api_hash: 344583e45741c457fe1862106095a5eb`.

Авторизация в Telegram — уже **из бота**: `/tg login` → номер → код → (2FA).

---

## 📱 Использование

Напишите сообществу в ВК `/help`. Основное:

| Команда | Что делает |
|---------|-----------|
| `/tg` | чаты Telegram (дальше всё на кнопках) |
| `/tg unread` | непрочитанные · `/tg find <имя>` — поиск |
| `/tg watch` | 🔔 уведомления по таймеру |
| `/tg folders` | 📁 папки · `/tg accounts` — 🔀 аккаунты |
| `/tg login` / `/tg logout` | вход/выход из Telegram |
| `/ls` | tmux-сессии (тап = подключиться + вывод) |
| `/claude` · `/dcc` | Claude Code · DeepClaude |
| `/in 5m sess cmd` | запланировать команду |
| `/admin` | 👑 админка (добавить юзера, выдать доступ) |

**Нативный флоу:** в открытом TG-чате просто печатаешь → уходит человеку;
вне чата текст идёт в активную tmux-сессию.

---

## 👑 Админка (добавить жену/друга/коллегу)
```
/adduser <vk_id> [имя]   — дать доступ к Telegram-прокси
/grant   <vk_id>         — дать доступ к серверу (tmux/Claude)
/revoke  <vk_id>         — забрать доступ к серверу
/admin                   — список пользователей и кнопки
```
Новый пользователь пишет сообществу, делает `/tg login` со **своим** номером —
у него свой отдельный Telegram.

---

## ✈️ Telegram-бот (управление сервером/Claude из Telegram)

Отдельный бот на **Telegram Bot API** — управление tmux и Claude Code без VK.

**Фичи:** список сессий тапом (подключение + стрим), 🤖 Claude / 🧠 DeepClaude,
**моноширинный стриминг** вывода (TUI Claude ровный — благодаря `<pre>` и узкой
ширине терминала), 🎮 пульт клавиш (стрелки/Enter/Esc/Tab/Shift+Tab/Ctrl+C),
планировщик с пайплайнами, детект простоя, `//model` — слэш-команды в Claude,
персистентность (сессии переживают перезапуск).

**Настройка:** секция `tgbot` в конфиге — `enabled: true`, `bot_token` от
[@BotFather](https://t.me/botfather), ваш Telegram ID в `admin_ids`
(узнать: [@userinfobot](https://t.me/userinfobot)).

```bash
python3 run_tgbot.py start            # запуск
# как сервис:
sudo cp tgbot.service /etc/systemd/system/
sudo systemctl enable --now tgbot
sudo journalctl -u tgbot -f
```

**Команды:** `/ls` `/claude` `/dcc` `/new` `/attach` `/kill` `/detach` ·
`/in 5m sess cmd` `/at 14:30 sess cmd` `/tasks` · пульт кнопками · `//model`.

---

## 🛠 VK-бот как сервис (автозапуск, переживает перезагрузку)
```bash
sudo cp vk-tmux-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vk-tmux-bot
sudo journalctl -u vk-tmux-bot -f      # логи
```
> В `.service`-файлах поправьте `User=` и пути под себя.

---

## 🗂 Структура

```
vkbot/
  bot.py            — VK-бот (команды, роли, живые ленты, прокси)
  vk_api.py         — VK API + клавиатуры + загрузка медиа
  tg_client.py      — Telethon-обёртка (чаты, папки, топики, медиа)
  tmux_handler.py   — tmux + рендер вывода (общее ядро)
  scheduler.py      — планировщик (общее ядро)
  state_manager.py  — сохранение состояния
  config.py         — конфигурация (YAML)
tgbot/
  api.py            — Telegram Bot API (inline-кнопки, <pre>, стрим)
  bot.py            — Telegram-бот (переиспользует ядро vkbot)
run.py              — CLI VK-бота (init / start / status)
run_tgbot.py        — CLI Telegram-бота (start)
tests.py            — 59 тестов
config.example.yaml — пример конфига (секции vk и tgbot)
```

Данные (НЕ в репозитории, см. `.gitignore`): `~/.vk-tmux-bot/` —
`config.yaml`, `*.session`, `users.json`, избранное и т.д.

---

## 🧪 Тесты
```bash
.venv/bin/python tests.py      # 58 тестов
```

---

## 🔒 Безопасность
- Токены и сессии лежат в `~/.vk-tmux-bot/` (права 600) и **не коммитятся**.
- Доступ — по белому списку VK ID; управление сервером — только у админа.
- Локальный запуск, без облака.

## 📄 Лицензия
MIT
