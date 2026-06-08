# scripts.json

| Поле | Назначение |
|------|------------|
| scripts_dir | каталог скриптов на VPS |
| scripts[].id | короткий id для кнопки |
| scripts[].argv | команда запуска |
| scripts[].timeout_sec | обрыв зависшего процесса |

Доступ к боту — whitelist Telegram user id в `.env`.
