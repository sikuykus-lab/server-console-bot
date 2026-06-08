# Диаграммы

Три вида схемы — как в [dataroom-cms](https://github.com/sikuykus-lab/dataroom-cms):
**данные**, **взаимодействие пользователя**, **процессы администратора**.

Рендер: скопировать блок в [mermaid.live](https://mermaid.live).

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

## Процесс пользователя

```mermaid
flowchart LR
  A["Открыл бота"] --> B["Кнопка job"]
  B --> C["«Выполняется…»"]
  C --> D{"exit 0?"}
  D -->|да| E["Готово + хвост лога"]
  D -->|нет| F["Код ошибки →\nадмину в лог"]
```

## Процессы администратора

```mermaid
flowchart TD
  R1["Новый сотрудник"] --> R2["Конструктор прав"]
  R2 --> R3["Галочки f11/f17/refresh"]
  R3 --> R4["Клавиатура\nобновилась"]
```
