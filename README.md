# API Proxy

Веб-сервис-прокси для работы с DeepSeek / OpenAI.

- Регистрация, JWT-логин
- Создание API-ключей
- Прокси-эндпоинт `/v1/chat/completions` (совместим с OpenAI SDK)
- Лимиты по токенам на ключ
- Статистика и логирование
- Админ-панель

## Быстрый старт

```bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000
```

Открой `http://127.0.0.1:8000`, зарегистрируйся, создай ключ, тестируй.

## Переменные окружения (.env)

| Переменная | Описание |
| --- | --- |
| `DEEPSEEK_API_KEY` | Ключ DeepSeek (без него — демо-режим) |
| `JWT_SECRET` | Секрет для JWT |
| `ADMIN_USERNAME` | Имя первого админа (по умолчанию admin) |

## Тест через curl

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-ваш-ключ" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Привет!"}]}'
```
