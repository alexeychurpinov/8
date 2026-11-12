# Заявки через Telegram

Сервис принимает заявки от сайта через защищённый API, сохраняет их в SQLite и доставляет уведомления администратору в Telegram. Telegram-бот позволяет просматривать заявки, менять их статус и смотреть историю.

## Требования

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Telegram-бот, созданный через [@BotFather](https://t.me/BotFather)

## Установка и запуск

1. Установите зависимости проекта из lock-файла:

   ```sh
   uv sync --frozen
   ```

2. Создайте `.env` и сгенерируйте отдельный случайный API-ключ длиной не менее 32 символов:

   ```sh
   cp .env.example .env
   python -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

   В `.env` задайте `API_KEY` полученным значением, токен `TELEGRAM_BOT_TOKEN` от BotFather и свой числовой `ADMIN_TELEGRAM_ID`. Остальные параметры по умолчанию:

   ```dotenv
   DATABASE_URL=sqlite+aiosqlite:///./data/leads.db
   RATE_LIMIT_PER_MINUTE=20
   AUTH_RATE_LIMIT_PER_MINUTE=120
   TELEGRAM_ENABLED=true
   ```

   Файл `.env` содержит секреты и не должен попадать в Git. Для получения своего Telegram ID воспользуйтесь ботом, который показывает ID пользователя, и перепроверьте его перед настройкой.

3. Откройте диалог с ботом и отправьте `/start` от имени настроенного администратора. Если у бота ранее был установлен webhook, удалите его вручную перед запуском polling: приложение намеренно не меняет и не удаляет webhook автоматически.

4. Запустите API одним процессом:

   ```sh
   uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
   ```

   Приложение создаст файл базы данных при старте. Проверка доступности и БД: `GET http://127.0.0.1:8000/healthz`.

Не запускайте несколько процессов/реплик: Telegram polling и обработчик очереди уведомлений рассчитаны на один процесс. Это решение для небольшого потока заявок, не для высокой нагрузки.

## API для сайта

`POST /api/leads` принимает JSON и требует заголовок `X-API-Key`. Не встраивайте этот ключ в HTML, JavaScript, мобильное приложение или иной код, доступный посетителю: используйте серверный прокси, как в [примере сайта](examples/index.html) ниже.

Пример серверного запроса:

```sh
curl -X POST http://127.0.0.1:8000/api/leads \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ВАШ_СЕКРЕТНЫЙ_API_KEY' \
  -d '{
    "name": "Анна Иванова",
    "phone": "+79991234567",
    "email": "anna@example.com",
    "service": "Консультация",
    "comment": "Удобно после 18:00",
    "source": "landing-page"
  }'
```

Обязательные поля: `name` (до 120 символов), `phone` (7–15 цифр; допускаются пробелы, скобки, дефисы и начальный `+`), `email` (до 254 символов), `service` (до 160) и `source` (до 200). `comment` необязателен (до 1500 символов). Неизвестные поля отклоняются. Тело запроса ограничено 16 КиБ.

Успешный ответ `201 Created` содержит ID, статус, время создания и состояние уведомления, например:

```json
{"id": 42, "status": "new", "created_at": "2026-01-01T12:00:00Z", "notification_status": "queued"}
```

Коды ошибок: `401` — неверный ключ, `422` — ошибка валидации, `413` — превышен размер запроса, `429` — ограничение частоты (с `Retry-After`), `503` — база данных временно недоступна. Лимиты: `RATE_LIMIT_PER_MINUTE` для заявок по ключу и `AUTH_RATE_LIMIT_PER_MINUTE` для запросов с ошибкой аутентификации по IP; счётчики хранятся в SQLite и переживают перезапуск. Не записывайте персональные данные заявок в логи.

Запись заявки и постановка уведомления в постоянную очередь происходят вместе. Ошибки доставки повторяются; доставка как минимум однократная, поэтому в редком случае сбоя после отправки, но до фиксации результата в БД возможно повторное уведомление.

## Пример HTML-формы и JavaScript

Файл [`examples/index.html`](examples/index.html) содержит полную демонстрационную страницу. Минимальная форма и её JavaScript выглядят так: браузер обращается только к `/submit` на том же сайте, API-ключ остаётся на сервере.

```html
<form id="lead-form">
  <input name="name" maxlength="120" placeholder="Имя" required>
  <input name="phone" type="tel" maxlength="32" placeholder="Телефон" required>
  <input name="email" type="email" maxlength="254" placeholder="Email" required>
  <input name="service" maxlength="160" placeholder="Услуга" required>
  <textarea name="comment" maxlength="1500" placeholder="Комментарий"></textarea>
  <input type="hidden" name="source" value="website-form">
  <button type="submit">Отправить</button>
</form>
<p id="result" role="status" aria-live="polite"></p>
<script>
  const form = document.querySelector("#lead-form");
  const result = document.querySelector("#result");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    try {
      const response = await fetch("/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось отправить заявку.");
      form.reset();
      result.textContent = `Заявка №${data.id} отправлена.`;
    } catch (error) {
      result.textContent = error instanceof Error ? error.message : "Ошибка отправки.";
    }
  });
</script>
```

Для локального демонстрационного запуска используйте скрипт проекта:

```sh
bash scripts/preview.sh
```

Он создаёт случайный ключ только на стороне сервера, отключает Telegram и использует отдельную базу `.hoplite/demo.db`; это demo-режим без приёма уведомлений в Telegram. Он поднимает API на `127.0.0.1:8000` и сайт на `0.0.0.0:3000`. Откройте адрес Preview, указанный проектом. Скрипт сам запускает оба процесса и останавливает их при завершении.

Если запускаете прокси отдельно, используйте тот же `.env`, где задан `API_KEY`:

```sh
uv run uvicorn examples.site:app --host 127.0.0.1 --port 8080 --workers 1 --no-proxy-headers
```

Откройте <http://127.0.0.1:8080/>. Прокси передаёт заявку в API; переменная `LEADS_API_URL` задаёт адрес backend (по умолчанию `http://127.0.0.1:8000/api/leads`). Для удалённого backend требуется HTTPS. В браузерном коде нет API-ключа, а ошибки backend не раскрывают его внутренние ответы.

Это демонстрационный пример, не готовая защита публичной формы от спама. Для production добавьте CAPTCHA или другое противодействие ботам, HTTPS и настройте доверенные прокси корректно. Не доверяйте произвольному `X-Forwarded-For`: пример ограничивает частоту по IP-соединению; по умолчанию он запускается без обработки proxy-заголовков. Backend-лимит заявок общий для всех пользователей, проходящих через один API-ключ сайта. Для нескольких сайтов используйте отдельные ключи и соответствующую конфигурацию.

## Telegram-бот администратора

Доступ к административным функциям есть только у `ADMIN_TELEGRAM_ID` в личном чате с ботом. Команды:

- `/start` — приветствие и справка.
- `/leads` — список заявок; поддерживаются фильтры `all`, `new`, `accepted`, `rejected`, `processed` и постраничный просмотр.
- `/stats` — статистика по статусам.

В карточке заявки доступны просмотр подробностей, принятие, отклонение и отметка обработанной. Переходы статусов: `new` → `accepted` или `rejected`; `accepted` → `processed` или `rejected`. Финальные статусы повторно не открываются. История переходов сохраняется.

## Проверки и резервное копирование

Запуск тестов и линтера:

```sh
uv run pytest
uv run ruff check .
```

Для согласованной копии SQLite используйте штатный SQLite Backup API, например через Python `sqlite3.Connection.backup()` на отдельную базу. Если используете обычное копирование файла, сначала остановите приложение и убедитесь, что нет других писателей; при WAL-режиме копия только основного `.db` во время работы может быть неполной.
