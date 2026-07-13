# NDA Sanitizer Frontend

**Статус**: запланировано  
**Дата**: 2026-07-10

## Задача

Локальный веб-интерфейс для NDA-санитайзера — обертка над Python-бэкендом `Sanitizer`, чтобы работать через браузер вместо CLI.

## Функции

### Sanitize
- Поле ввода текста
- Вывод: очищенный текст + vault_id
- Кнопка «Копировать» для clean_text
- Кнопка «Копировать vault_id»

### Restore
- Поле ввода текста (с плейсхолдерами) + поле vault_id
- Вывод: восстановленный текст
- Кнопка «Копировать»

### Check (dry-run)
- Поле ввода текста
- Вывод: категории и счётчики найденных сущностей (без vault)
- Список будущих плейсхолдеров (без исходных значений)

### Vault Manager
- Список всех vault-сессий: vault_id + дата создания
- Кнопка удаления vault'а
- **Содержимое vault'ов не показывается в UI**

## Нефункциональные требования

- Всё на `127.0.0.1`, никаких внешних вызовов
- Vault'ы не покидают диск — в UI рендерятся только vault_id и счётчики
- **Backend**: FastAPI (или Flask) в том же `.venv`, порт 8090
- **Frontend**: ванильный HTML + CSS + JS, без node_modules, без фреймворков
- **Никаких CDN** — все ресурсы локальные или встроенные (inline)
- Автостарт: systemd user-unit, после `llama-sanitizer.service`
- Отдельный лог-файл для API-запросов (без чувствительных данных)

## Архитектура

```
Браузер (127.0.0.1:8090)
  → POST /api/sanitize  { text } → { clean_text, vault_id }
  → POST /api/restore   { text, vault_id } → { restored_text }
  → POST /api/check     { text } → { blocked, categories, placeholders }
  → GET  /api/vaults               → [{ vault_id, created_at }]
  → DELETE /api/vaults/{vault_id}  → { deleted }
  → GET  /                         → index.html (фронтенд)

Backend (FastAPI, порт 8090)
  → src/sanitizer.py (Sanitizer)
  → vaults/ (0600, только чтение vault_id и дат)
```

## systemd-юнит

```ini
[Unit]
Description=NDA Sanitizer web UI
After=llama-sanitizer.service
Requires=llama-sanitizer.service

[Service]
Type=simple
WorkingDirectory=/home/dima/nda-sanitizer
ExecStart=/home/dima/nda-sanitizer/.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8090
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
```

## Компоненты

| Файл | Назначение |
|------|-----------|
| `server.py` | FastAPI-роуты, вызов Sanitizer |
| `static/index.html` | Фронтенд (single-page, всё inline) |
| `systemd/nda-sanitizer-web.service` | systemd-юнит |

## Приёмка

- [ ] `http://127.0.0.1:8090` открывается в браузере
- [ ] Sanitize → clean_text восстанавливается через Restore
- [ ] Check → категории и счётчики без vault
- [ ] Vault Manager → список vault_id, удаление
- [ ] Vault-файлы не читаются через API (только id и дата)
- [ ] systemd: `systemctl --user enable --now nda-sanitizer-web.service`
