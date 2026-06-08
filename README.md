# Remote Job Bot — пульт скриптов на VPS

Короче: **Telegram-кнопки → subprocess на сервере** — без SSH и без выдачи root всем.

Задача: f11, f17, refresh CRM тяжёлые — cron есть, но иногда нужен прогон «сейчас». Не каждый должен лезть в терминал; права — по человеку и по скрипту.

---

## Что сделано

- **Reply-клавиатура** — только разрешённые job для user_id.
- **scripts.json** — реестр: argv, timeout, подсказка cron.
- **access.db** — роли, галочки по скриптам, история run_id.
- **Хвост stdout в чат** — код выхода без полного лога в группу.
- **Конструктор прав** — владелец включает кнопки без правки кода.

---

## Фишки и удобство

| Фишка | Зачем |
|-------|-------|
| Cooldown | Не спамят повторным запуском |
| timeout_sec 7200 | Длинные парсеры не обрываются |
| «Расписание» в меню | Подсказка cron без crontab -e |
| SQLite ACL | Не хардкодить ALLOWED в .py |
| Отдельно от cron | Ручной слой, не замена расписанию |

---

## Схема данных

```mermaid
flowchart TB
  subgraph tg ["Telegram"]
    U["Пользователь"]
    O["Владелец"]
  end

  subgraph bot ["Remote Job Bot"]
    B["bot.py"]
    ACL["access.db"]
    SJ["scripts.json"]
  end

  subgraph jobs ["VPS /opt или РабочиеСкрипты"]
    F11["parser A"]
    F17["parser B"]
    REF["CRM refresh"]
  end

  U --> B
  O --> ACL
  B --> ACL
  ACL --> SJ
  SJ --> F11
  SJ --> F17
  SJ --> REF
  B --> U
```

---

## Процесс пользователя

```mermaid
flowchart LR
  A["Открыл бота"] --> B["Кнопка job"]
  B --> C["«Выполняется…»"]
  C --> D{"exit 0?"}
  D -->|да| E["Готово + хвост лога"]
  D -->|нет| F["Код ошибки →\nадмину в лог"]
```

**Владелец:**

```mermaid
flowchart TD
  R1["Новый сотрудник"] --> R2["Конструктор прав"]
  R2 --> R3["Галочки f11/f17/refresh"]
  R3 --> R4["Клавиатура\nобновилась"]
```

---

## Стек

| Слой | Технология |
|------|------------|
| Бот | python-telegram-bot / aiogram |
| Права | SQLite |
| Запуск | subprocess |
| Конфиг | scripts.json, .env |
| Демон | systemd |

---

## Структура репозитория

```
README.md
LICENSE
.gitignore
bot/bot.py
deploy/server-console-bot.service
docs/                     — DIAGRAMS.md (3× mermaid)
examples/                 — scripts.example.json, .env.example
requirements.txt
```

---

## Быстрый старт

```bash
cp .env.example .env   # BOT_TOKEN, OWNER_ID
cp scripts.json.example scripts.json
systemctl enable --now server-console-bot
```

Добавить job: новая запись в `scripts.json` + права в ACL.
