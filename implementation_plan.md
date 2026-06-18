# 📋 План рефакторингу: Виправлення Monobank API 404 та забезпечення відмовостійкості бота

Ми підготували детальний план рефакторингу для вирішення виявлених логічних збоїв, проблем еквайрингу та стабільності роботи вебхуків Telegram.

---

## 🛠️ Запропоновані зміни

### 1. Виправлення кінцевої точки Monobank API
Ми оновимо інтеграцію Monobank, щоб запити на створення рахунків надсилалися на коректну адресу згідно з офіційним API.

#### [MODIFY] [payments.py](file:///Users/valentin/Secret_Cava/app/integrations/payments.py)
* **Зміна:**
  - Змінити шлях у методі `create_invoice` з `/personal/merchant/invoice/create` на `/api/merchant/invoice/create`.
* **Фрагмент коду:**
  ```python
  async def create_invoice(self, amount: float, order_id: str, client_name: str) -> tuple[str, str]:
      """Creates a Monobank Invoice returning a payment checkout page and invoice ID."""
      url = f"{self.base_url}/api/merchant/invoice/create"
      ...
  ```

---

### 2. Забезпечення стабільності Telegram Webhook Gateway
Ми захистимо головний обробник вебхуків від необроблених винятків усередині aiogram. Це запобіжить падінню ASGI-додатка та нескінченному дублюванню повідомлень з боку Telegram.

#### [MODIFY] [main.py](file:///Users/valentin/Secret_Cava/app/main.py)
* **Зміна:**
  - Обгорнути `await dp.feed_update(bot, telegram_update)` в блок `try-except Exception`.
  - У разі виникнення будь-якої помилки логувати її за допомогою `logger.exception("telegram_update_processing_failed")`, але повертати успішний статус `Response(status_code=status.HTTP_200_OK)`.
* **Фрагмент коду:**
  ```python
  @app.post("/webhooks/telegram", tags=["Bot Webhooks Gateway"])
  async def process_telegram_webhook(
      request: Request,
      x_telegram_bot_api_secret_token: str | None = Header(None, alias="X-Telegram-Bot-Api-Secret-Token")
  ) -> Response:
      ...
      # Process updates asynchronously using aiogram safely
      update_data = await request.json()
      telegram_update = types.Update(**update_data)
      try:
          await dp.feed_update(bot, telegram_update)
      except Exception as e:
          logger.exception("telegram_webhook_handler_error", error=str(e))
          
      return Response(status_code=status.HTTP_200_OK)
  ```

---

### 3. Надійна обробка помилок зовнішніх інтеграцій у бізнес-сервісі
Ми додамо додаткову ізоляцію помилок (fault isolation) при роботі із зовнішніми API (Google Sheets, Google Calendar, APScheduler) під час підтвердження транзакцій. Якщо сервіси Google тимчасово недоступні або токени прострочені, оплата все одно має фіксуватися в базі даних.

#### [MODIFY] [booking.py (сервіс)](file:///Users/valentin/Secret_Cava/app/services/booking.py)
* **Зміна:**
  - Додати блоки `try-except` навколо викликів `self.gcal.create_booking_event(...)` та `self.sheets.append_row(...)` в методі `confirm_payment_and_booking`.
* **Фрагмент коду:**
  ```python
  # 1. Sync to Google Calendar safely
  if self.gcal and booking.psychologist.google_calendar_id:
      try:
          gcal_id = await self.gcal.create_booking_event(...)
          booking.google_event_id = gcal_id
      except Exception as ex:
          logger.error("google_calendar_sync_failed_during_settlement", error=str(ex))
  ...
  # 4. Sync to Google Sheets safely
  if self.sheets:
      try:
          await self.sheets.append_row(...)
      except Exception as ex:
          logger.error("google_sheets_sync_failed_during_settlement", error=str(ex))
  ```

---

## 🧪 План тестування та верифікації

### Автоматичні тести
1. **Тестування API Monobank**:
   - Запустити інтеграційний скрипт `test_payment.py` з налаштованим токеном Monobank та перевірити повернення коду `200 OK` та валідного посилання на оплату.
2. **Компіляція проекту**:
   - Імпортувати змінені файли для перевірки синтаксичної коректності.
3. **Симуляція помилок**:
   - Провести тест з невалідними параметрами вебхуку, щоб переконатися, що додаток не падає в 500, а повертає 200 Telegram.

---

## 📋 Команди для ручного керування сервісами в терміналі

### 1. 🗄️ База даних PostgreSQL 17 (Локальний кластер)
* **Запуск бази даних**:
  ```bash
  LC_ALL=C /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /Users/valentin/Secret_Cava/pg_data -l /Users/valentin/Secret_Cava/pg_data/postgres.log start
  ```
* **Зупинка бази даних**:
  ```bash
  /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /Users/valentin/Secret_Cava/pg_data stop
  ```
* **Перевірка статусу бази даних**:
  ```bash
  /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /Users/valentin/Secret_Cava/pg_data status
  ```

### 2. 🤖 Бот & FastAPI Веб-сервер
* **Очищення порту 8000 від можливих завислих процесів**:
  ```bash
  lsof -t -i :8000 | xargs kill -9
  ```
* **Запуск бота (FastAPI через Uvicorn)**:
  ```bash
  .venv/bin/uvicorn app.main:app --port 8000 --host 0.0.0.0
  ```

### 3. 🧪 Тестові скрипти платіжних ланцюгів
* **Запуск тесту оплат консультацій**:
  ```bash
  .venv/bin/python test_payment.py
  ```
* **Запуск тесту оплат заходів**:
  ```bash
  .venv/bin/python test_event_payment.py
  ```
