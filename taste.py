# ======================================
# === TasteBalance v4.0 (Полная версия) === 
# ======================================

import os
import re
import json
import sqlite3
import asyncio
import logging
import atexit
import base64
import aiohttp
import ssl, certifi
from datetime import date, datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import google.generativeai as genai
load_dotenv()

# ==========
# intentionally empty: we don't want bot commands visible, но main() вызывает эту функцию — чтобы не падало
async def set_commands(bot):
    return
# ==========

# ======================================
# 🔧 Настройки
# ======================================

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GOOGLE_GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.workflow_data = {}

genai.configure(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)

# ========== Stripe & aiohttp для webhook ==========
import stripe
from aiohttp import web

# Stripe config — подгружаются из .env
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")  # webhook signing secret
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")  # optional: if present, create subscription
DOMAIN = os.getenv("DOMAIN", "")  # required for success/cancel URLs in Stripe
CURRENCY = os.getenv("CURRENCY", "usd")

# инициализация stripe (если ключ задан)
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# ======================================
# 🗄️ База данных
# ======================================

conn = sqlite3.connect("tastebalance.db", check_same_thread=False)
cursor = conn.cursor()


cursor.execute("""
CREATE TABLE IF NOT EXISTS meals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    description TEXT,
    calories REAL,
    protein REAL,
    fat REAL,
    carbs REAL,
    date TEXT,
    time TEXT
)
""")


cursor.execute("""
CREATE TABLE IF NOT EXISTS cache(
    hash TEXT PRIMARY KEY,
    result TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    is_premium INTEGER DEFAULT 0,
    last_date TEXT,
    photos_today INTEGER DEFAULT 0,
    premium_until TEXT
)
""")

conn.commit()
atexit.register(conn.close)


# ======================================
# ⚙️ Вспомогательные функции
# ======================================

def cache_get(key: str):
    """Получить значение из кэша."""
    cursor.execute("SELECT result FROM cache WHERE hash=?", (key,))
    row = cursor.fetchone()
    return row[0] if row else None


def cache_set(key: str, value: str):
    """Сохранить результат в кэше."""
    cursor.execute("INSERT OR REPLACE INTO cache (hash, result) VALUES (?, ?)", (key, value))
    conn.commit()


def save_meal(user_id, desc, kcal, p, f, c):
    now = datetime.now()
    cursor.execute(
        """
        INSERT INTO meals (user_id, description, calories, protein, fat, carbs, date, time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            desc,
            kcal,
            p,
            f,
            c,
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M")
        )
    )
    conn.commit()


def get_stats(user_id):
    """Получить статистику за текущий день."""
    cursor.execute(
        "SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM meals WHERE user_id=? AND date=?",
        (user_id, date.today().isoformat())
    )
    return cursor.fetchone() or (0, 0, 0, 0)


def get_user(user_id):
    """Получить данные пользователя или создать нового."""
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.execute(
            "INSERT INTO users (user_id, is_premium, last_date, photos_today, premium_until) VALUES (?, 0, ?, 0, NULL)",
            (user_id, date.today().isoformat())
        )
        conn.commit()
        return (user_id, 0, date.today().isoformat(), 0, None)
    return user


def update_user(user_id, **fields):
    """Обновить данные пользователя."""
    set_clause = ", ".join([f"{k}=?" for k in fields.keys()])
    cursor.execute(f"UPDATE users SET {set_clause} WHERE user_id=?", (*fields.values(), user_id))
    conn.commit()


def is_premium_active(user_id):
    """Проверить, активен ли Premium."""
    user = get_user(user_id)
    is_premium, premium_until = user[1], user[4]
    if is_premium:
        if not premium_until:
            return True
        try:
            return datetime.fromisoformat(premium_until) >= datetime.now()
        except Exception:
            return False
    return False


def increment_photo(user_id):
    """Увеличить счётчик фото за день."""
    user_id, is_premium, last_date, photos_today, premium_until = get_user(user_id)
    today = date.today().isoformat()
    if last_date != today:
        update_user(user_id, last_date=today, photos_today=0)
        photos_today = 0
    photos_today += 1
    update_user(user_id, photos_today=photos_today)
    return photos_today


def can_analyze_photo(user_id):
    """Проверить, может ли пользователь отправить фото (лимит)."""
    user_id, is_premium, last_date, photos_today, premium_until = get_user(user_id)
    if is_premium_active(user_id):
        return True, None
    today = date.today().isoformat()
    if last_date != today:
        update_user(user_id, last_date=today, photos_today=0)
        return True, None
    if photos_today >= 2:
        return False, (
            "📸 Сегодня лимит 2 фото.\n\n"
            "💎 *TasteBalance Premium* — без ограничений и с точным анализом.\n"
            "Нажми «Получить Premium» ниже 👇"
        )
    return True, None


# ======================================
# 🔮 Промпт для анализа изображения
# ======================================

ANALYSIS_PROMPT = """
Ты — эксперт по питанию и анализу изображений еды.
Проанализируй фото и верни JSON строго по формату.

⚙️ Правила:
- Определи все видимые ингредиенты и блюда (по отдельности).
- Не добавляй несуществующие продукты.
- Для каждого ингредиента оцени примерный вес (целое число).
- Рассчитай КБЖУ (калории, белки, жиры, углеводы) максимально реалистично.
- Не пиши лишний текст, комментарии и описания.

📋 Формат ответа строго:
{
  "items": [
    {"name": "курица", "weight_g": 150, "cal": 230, "protein": 32, "fat": 5, "carbs": 0},
    {"name": "рис", "weight_g": 200, "cal": 260, "protein": 6, "fat": 2, "carbs": 56}
  ],
  "total": {"cal": 490, "protein": 38, "fat": 7, "carbs": 56}
}
"""

# ======================================
# 📋 Главное меню и команды
# ======================================

def main_menu():
    keyboard = [
        [types.KeyboardButton(text="👋 Главное меню")],
        [types.KeyboardButton(text="📊 Статистика"), types.KeyboardButton(text="🕒 История")],
        [types.KeyboardButton(text="✍️ Ввести вручную")],
        [types.KeyboardButton(text="ℹ️ Помощь"), types.KeyboardButton(text="💎 Premium")],
        [types.KeyboardButton(text="💌 Отправить отзыв / сотрудничество")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# ======================================
# 👋 /start
# ======================================

@dp.message(Command("start"))
@dp.message(F.text == "👋 Главное меню")
async def start_cmd(message: types.Message):
    user = get_user(message.from_user.id)
    is_premium = is_premium_active(message.from_user.id)

    greeting = (
        f"👋 Привет, {message.from_user.first_name or 'друг'}!\n\n"
        f"Я — *TasteBalance*, твой AI-ассистент по питанию 🍽️\n\n"
        f"💎 *Статус:* {'Premium активен ✅' if is_premium else 'Бесплатный аккаунт (2 фото в день)'}\n\n"
        "📸 Просто отправь фото еды или напиши, что ты ел — я определю состав и КБЖУ.\n\n"
        "Или выбери действие из меню: 👇 "
    )

    await message.answer(greeting, parse_mode="Markdown", reply_markup=main_menu())


# ======================================
# 📊 /stats — статистика за день
# ======================================

@dp.message(Command("stats"))
@dp.message(F.text == "📊 Статистика")
async def stats_cmd(message: types.Message):
    kcal, p, f, c = get_stats(message.from_user.id)
    if kcal and kcal > 0:
        text = (
            f"📈 *Сегодняшний результат:*\n"
            f"🔥 Калории: {round(kcal)} ккал\n"
            f"🍗 Белки: {round(p)} г\n"
            f"🥑 Жиры: {round(f)} г\n"
            f"🍞 Углеводы: {round(c)} г"
        )
    else:
        text = "🫙 Сегодня ещё ничего не добавлено."
    await message.answer(text, parse_mode="Markdown")


# ======================================
# 🕒 /history — история за неделю
# ======================================

@dp.message(Command("history"))
@dp.message(F.text == "🕒 История")
async def history_cmd(message: types.Message):
    """Показать историю за последние 7 дней с временем и ингредиентами."""
    cursor.execute(
        "SELECT date, time, description, calories, protein, fat, carbs "
        "FROM meals WHERE user_id=? AND date>=? "
        "ORDER BY date DESC, time DESC",
        (message.from_user.id, (date.today() - timedelta(days=7)).isoformat())
    )
    rows = cursor.fetchall()

    if not rows:
        await message.answer("📭 История пуста за последние 7 дней.")
        return

    text = "🕒 *История за 7 дней:*\n\n"
    for d, t, desc, kcal, p, f, c in rows:
        date_part = f"📅 {d}"
        time_part = f"🕐 {t}" if t else ""
        ingredients = desc.replace("Фото еды", "📷 Фото блюда")

        text += (
            f"{date_part}  {time_part}\n"
            f"🍽️ {ingredients}\n"
            f"🔥 {round(kcal)} ккал — "
            f"Б: {round(p)} Ж: {round(f)} У: {round(c)}\n\n"
        )

    await message.answer(text.strip(), parse_mode="Markdown")

# ======================================
# ℹ️ /help — справка
# ======================================

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(message: types.Message):
    text = (
        "ℹ️ *TasteBalance — твой AI-ассистент по питанию!*\n\n"
        "📸 Просто отправь фото еды или напиши блюдо — я определю состав и КБЖУ.\n\n"
        "🆓 *Бесплатно:* 2 фото в день\n"
        "💎 *Premium:* безлимит, улучшенная точность и автоотчёты\n\n"
        "📋 *Команды:*\n"
        "/start — главное меню\n"
        "/stats — статистика за день\n"
        "/history — история за неделю\n"
        "/premium — Premium-возможности\n"
        "/help — справка"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())

# ======================================
# ✍️ Ввести вручную
# ======================================

@dp.message(F.text == "✍️ Ввести вручную")
async def manual_input(message: types.Message):
    """Начинает ручной ввод блюда."""
    user_id = str(message.from_user.id)
    dp.workflow_data[user_id] = {"mode": "manual_input"}

    await message.answer(
        "📝 Введи блюдо текстом, например:\n\n"
        "_овсянка с молоком 100г и бананом 50г_\n"
        "_или же просто напиши:_\n"
        "_курица с рисом и овощами_\n\n"
        "✨ Я рассчитаю состав и КБЖУ максимально точно.",
        parse_mode="Markdown"
    )

# ======================================
# 💬 Отзывы и сотрудничество
# ======================================

FEEDBACK_TARGET_ID = 408204060  # <-- замени на свой Telegram ID

@dp.message(Command("feedback"))
@dp.message(F.text == "💌 Отправить отзыв / сотрудничество")
async def feedback_entry(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="💭 Оставить отзыв", callback_data="feedback")
    builder.button(text="🤝 Предложить сотрудничество", callback_data="cooperation")
    builder.adjust(1)

    await message.answer("💬 Выберите, что хотите отправить 👇", reply_markup=builder.as_markup())


@dp.callback_query(F.data.in_(["feedback", "cooperation"]))
async def feedback_choose(callback: types.CallbackQuery):
    user_key = str(callback.from_user.id)
    dp.workflow_data[user_key] = {"mode": callback.data}
    await callback.message.answer("✍️ Напиши сообщение, я передам его напрямую разработчику 👇")
    await callback.answer()


# ======================================
# 💎 Premium — меню, функции и оплата
# ======================================

@dp.message(Command("premium"))
@dp.message(F.text == "💎 Premium")
async def premium_info(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Получить Premium", callback_data="buy_premium")
    builder.button(text="📋 Что входит в Premium", callback_data="premium_features")
    builder.button(text="ℹ️ Проверить статус", callback_data="check_premium")
    builder.adjust(1)

    text = (
        "💎 *TasteBalance Premium*\n\n"
        "✅ Безлимит фото и анализов\n"
        "⚡ Улучшенная точность расчёта\n"
        "🍽️ Возможность редактировать блюда и ингредиенты\n"
        "📊 Автоотчёты за день и неделю\n"
        "🚀 Приоритетная скорость анализа\n\n"
        "💰 Всего $7.99 в месяц\n\n"
        "Нажми ниже, чтобы оформить 👇"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "premium_features")
async def premium_features(callback: types.CallbackQuery):
    text = (
        "💎 *Что входит в Premium:*\n\n"
        "1. Безлимит фото и текстов\n"
        "2. Повышенная точность анализа\n"
        "3. Возможность редактировать ингредиенты\n"
        "4. Автоотчёты за день и неделю\n"
        "5. Быстрая очередь обработки ⚡"
    )
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "check_premium")
async def check_premium(callback: types.CallbackQuery):
    if is_premium_active(callback.from_user.id):
        await callback.message.answer("✅ Premium активен! Наслаждайтесь полным функционалом 💪")
    else:
        await callback.message.answer("⚠️ Premium не активирован. Нажми /premium, чтобы оформить 💎")
    await callback.answer()


@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: types.CallbackQuery):
    """
    Создаём Stripe Checkout и отправляем пользователю одно сообщение
    с кнопкой "💳 Оплатить (Stripe)" — сразу открывает checkout.
    Убираем промежуточное сообщение «Создаю платёжную сессию…».
    """
    await callback.answer()  # быстро закрываем «spinner» у Telegram (без текста)
    user_id = callback.from_user.id

    try:
        # создаём сессию в фоновом потоке (если STRIPE_SECRET_KEY не задан — выбросится)
        url = await asyncio.to_thread(create_checkout_session_sync, user_id)

        # кнопка с URL (Откроет Checkout)
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Оплатить (Stripe)", url=url)
        builder.adjust(1)

        text = (
            "🔒 Оплата проходит через Stripe.\n\n"
            "Нажмите кнопку ниже — вас перенесёт на безопасную страницу оплаты."
        )

        await callback.message.answer(text, reply_markup=builder.as_markup())

    except Exception as e:
        logging.exception("Failed to create stripe session: %s", e)
        # более дружелюбный текст ошибки для пользователя
        await callback.message.answer(
            "⚠️ Не удалось создать платёжную сессию. Проверьте настройки Stripe (STRIPE_SECRET_KEY / PRICE / DOMAIN)."
        )


#@dp.callback_query(F.data == "activate_premium")
#async def activate_premium(callback: types.CallbackQuery):
#   """Временная ручная активация Premium."""
#    until_date = (datetime.now() + timedelta(days=30)).isoformat()
#    update_user(callback.from_user.id, is_premium=1, premium_until=until_date)
#    await callback.message.answer("✅ Premium активирован на 30 дней! 💎")
#    await callback.answer()

# ======================================
# 📦 Безопасная загрузка файла с Telegram
# ======================================

ssl_context = ssl.create_default_context(cafile=certifi.where())

async def safe_download(bot, file_path, retries=3, timeout=30):
    """Безопасно загружает файл с Telegram CDN с несколькими попытками."""
    file_url = f"https://api.telegram.org/file/bot{bot.token}/{file_path}"

    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(file_url, ssl=ssl_context) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        logging.warning(f"⚠️ Ошибка {resp.status} при загрузке файла с Telegram CDN.")
        except (aiohttp.ClientError, asyncio.TimeoutError, ssl.SSLError) as e:
            if attempt < retries - 1:
                logging.warning(f"⏳ Попытка {attempt+2}/{retries} после ошибки: {e}")
                await asyncio.sleep(2)
            else:
                raise

# =================== Stripe helpers ===================

def _make_success_cancel_urls():
    """Вспомогательная функция, возвращает success и cancel URL для Checkout."""
    success_url = f"https://{DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"https://{DOMAIN}/cancel"
    return success_url, cancel_url

def create_checkout_session_sync(user_id: int):
    """
    Создаёт Stripe Checkout Session (синхронно).
    Возвращает session.url
    """
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("Stripe not configured (STRIPE_SECRET_KEY missing)")

    success_url, cancel_url = _make_success_cancel_urls()
    metadata = {"user_id": str(user_id)}

    if STRIPE_PRICE_ID:
        # Подписка
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            # ВАЖНО: кладём user_id в метадату подписки
            subscription_data={
                "metadata": {
                    "user_id": str(user_id)
                }
            },
        )
    else:
        # Разовый платёж $7.99
        unit_amount = 799  # cents
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": CURRENCY,
                        "product_data": {"name": "TasteBalance Premium"},
                        "unit_amount": unit_amount,
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
        )

    return session.url


# Webhook handler — aiohttp
async def stripe_webhook(request: web.Request):
    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature", "")

    # Проверяем подпись (если есть вебхук-секрет)
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=sig_header,
                secret=STRIPE_WEBHOOK_SECRET,
            )
        except (ValueError, stripe.error.SignatureVerificationError):
            logging.warning("Stripe webhook signature/parse error")
            return web.Response(status=400)
    else:
        try:
            event = json.loads(payload)
        except Exception:
            logging.warning("Stripe webhook parse error (no secret)")
            return web.Response(status=400)

    etype = event.get("type") if isinstance(event, dict) else event["type"]
    obj = event.get("data", {}).get("object", {}) if isinstance(event, dict) else event.data["object"]

    try:
        # 1) Первая успешная оплата через Checkout
        if etype == "checkout.session.completed":
            session = obj
            sub_id = session.get("subscription")
            metadata = session.get("metadata") or {}
            user_id = metadata.get("user_id")

            if sub_id and user_id:
                sub = stripe.Subscription.retrieve(sub_id)
                period_end_ts = sub.get("current_period_end")
                if period_end_ts:
                    until = datetime.fromtimestamp(int(period_end_ts))

                    # если уже есть более дальняя дата — не укорачиваем
                    old = get_user(int(user_id))[4]
                    if old:
                        try:
                            old_dt = datetime.fromisoformat(old)
                            if old_dt > until:
                                until = old_dt
                        except Exception:
                            pass

                    update_user(int(user_id), is_premium=1, premium_until=until.isoformat())
                    logging.info(f"Activated premium for user {user_id} until {until}")

        # 2) Продление подписки (каждый успешный платеж)
        elif etype == "invoice.payment_succeeded":
            invoice = obj
            sub_id = invoice.get("subscription")
            if sub_id:
                sub = stripe.Subscription.retrieve(sub_id)
                period_end_ts = sub.get("current_period_end")

                # user_id ищем в metadata подписки или инвойса
                user_id = None
                if sub.get("metadata", {}).get("user_id"):
                    user_id = sub["metadata"]["user_id"]
                elif invoice.get("metadata", {}).get("user_id"):
                    user_id = invoice["metadata"]["user_id"]

                if user_id and period_end_ts:
                    until = datetime.fromtimestamp(int(period_end_ts))

                    old = get_user(int(user_id))[4]
                    if old:
                        try:
                            old_dt = datetime.fromisoformat(old)
                            if old_dt > until:
                                until = old_dt
                        except Exception:
                            pass

                    update_user(int(user_id), is_premium=1, premium_until=until.isoformat())
                    logging.info(f"Renewed premium for user {user_id} until {until}")

        # 3) Отмена / изменение подписки
        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            sub = obj
            sub_full = stripe.Subscription.retrieve(sub.get("id"))

            user_id = sub_full.get("metadata", {}).get("user_id")
            status = sub_full.get("status")
            cancel_at_period_end = sub_full.get("cancel_at_period_end")
            period_end_ts = sub_full.get("current_period_end")

            if not user_id:
                return web.Response(status=200)

            # отменили сразу (без «действует до конца периода»)
            if status == "canceled" and not cancel_at_period_end:
                update_user(int(user_id), is_premium=0, premium_until=None)
                logging.info(f"Premium revoked immediately for user {user_id}")
            else:
                # отмена в конце периода — держим до current_period_end
                if period_end_ts:
                    until = datetime.fromtimestamp(int(period_end_ts)).isoformat()
                    update_user(int(user_id), is_premium=1, premium_until=until)
                    logging.info(f"Premium for user {user_id} active until period end {until}")

    except Exception:
        logging.exception("Error handling Stripe event")
        return web.Response(status=500)

    return web.Response(status=200)


async def start_stripe_webserver(host="0.0.0.0", port=8080):
    """Запускает aiohttp webserver с endpoint /stripe/webhook"""
    app = web.Application()
    app.router.add_post("/stripe/webhook", stripe_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logging.info(f"Stripe webhook server running on {host}:{port}")

# ======================================
# 💬 Универсальный обработчик текста (ввод блюда, редактирование, отзывы)
# ======================================

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_any_text(message: types.Message):
    user_key = str(message.from_user.id)

    # ----- Admin secret premium -----
    secret = os.getenv("ADMIN_PREMIUM_CODE", "")
    if secret and message.text.strip() == secret:
        until = (datetime.now() + timedelta(days=30)).isoformat()
        update_user(message.from_user.id, is_premium=1, premium_until=until)
        await message.answer("✅ Админ-Premium активирован на 30 дней.")
        return
    # --------------------------------

    wf = dp.workflow_data.get(user_key)

    # Если пользователь сейчас пишет отзыв / сотрудничество
    if wf and wf.get("mode") in ["feedback", "cooperation"]:
        try:
            mode = "📝 Отзыв" if wf["mode"] == "feedback" else "🤝 Сотрудничество"
            await bot.send_message(
                FEEDBACK_TARGET_ID,
                f"{mode} от @{message.from_user.username or message.from_user.id}:\n\n{message.text}"
            )
            await message.answer("✅ Спасибо! Сообщение отправлено разработчику 🙌\n\n"
                                 "Ты можешь вернуться в главное меню — просто введи /start 💬")
        except Exception as e:
            logging.error(f"Ошибка при отправке отзыва: {e}")
            await message.answer("⚠️ Не удалось отправить сообщение. Попробуй позже.")
        finally:
            dp.workflow_data.pop(user_key, None)
        return

        # --- изменение названия ингредиента с пересчётом ---
    if wf.get("stage") == "await_name":
        new_name = message.text.strip()
        idx = wf.get("editing_index")

        if idx is None or idx >= len(wf["meal"]["items"]):
            await message.answer("⚠️ Ошибка: ингредиент не найден.")
            wf["stage"] = None
            return

        await message.answer(f"🔄 Пересчитываю КБЖУ для *{new_name}*...", parse_mode="Markdown")

        try:
            # ✨ Пересчитываем только один ингредиент с помощью Gemini
            model = "gemini-2.5-flash" if is_premium_active(message.from_user.id) else "gemini-2.5-flash-lite"
            gen_model = genai.GenerativeModel(model)

            prompt = f"""
            Ты — эксперт по питанию. Определи КБЖУ для продукта "{new_name}" в количестве {wf["meal"]["items"][idx]["weight_g"]} г.
            Ответ строго в JSON формате:
            {{
              "cal": число,
              "protein": число,
              "fat": число,
              "carbs": число
            }}
            """

            response = await asyncio.to_thread(gen_model.generate_content, [prompt])

            # ✅ Безопасно извлекаем результат из Gemini
            if hasattr(response, "text") and response.text:
                result = response.text.strip()
            elif hasattr(response, "candidates"):
                try:
                    result = response.candidates[0].content.parts[0].text.strip()
                except Exception:
                    result = ""
            else:
                result = str(response).strip()

            cleaned = result.replace("```json", "").replace("```", "").strip()
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            cleaned = match.group(0) if match else "{}"

            try:
                data = json.loads(cleaned)
            except Exception:
                data = {}

            cal = data.get("cal", 0)
            p = data.get("protein", 0)
            f = data.get("fat", 0)
            c = data.get("carbs", 0)

            wf["meal"]["items"][idx].update({
                "name": new_name,
                "cal": cal,
                "protein": p,
                "fat": f,
                "carbs": c
            })

            # 🔄 Пересчёт общего КБЖУ
            total = {"cal": 0, "protein": 0, "fat": 0, "carbs": 0}
            for i in wf["meal"]["items"]:
                total["cal"] += i.get("cal", 0)
                total["protein"] += i.get("protein", 0)
                total["fat"] += i.get("fat", 0)
                total["carbs"] += i.get("carbs", 0)
            wf["meal"]["total"] = {k: round(v, 2) for k, v in total.items()}

            wf["stage"] = None

            await message.answer(
                f"✅ Название обновлено, КБЖУ пересчитано!\n\n"
                f"🔥 *Итого:* {round(total['cal'])} ккал\n"
                f"Б: {round(total['protein'])} г  Ж: {round(total['fat'])} г  У: {round(total['carbs'])} г",
                parse_mode="Markdown"
            )
            await show_updated_meal(message.from_user.id)

        except Exception as e:
            logging.error(f"Ошибка пересчёта КБЖУ для нового ингредиента: {e}")
            await message.answer("⚠️ Не удалось пересчитать КБЖУ. Название обновлено, но значения остались прежними.")
            wf["meal"]["items"][idx]["name"] = new_name
            wf["stage"] = None
            await show_updated_meal(message.from_user.id)
        return


    # --- изменение веса ---
    if wf.get("stage") == "await_weight":
        try:
            new_weight = float(message.text.strip())
            idx = wf.get("editing_index")

            if idx is None or idx >= len(wf["meal"]["items"]):
                await message.answer("⚠️ Ошибка: ингредиент не найден.")
                wf["stage"] = None
                return

            item = wf["meal"]["items"][idx]
            old_weight = item.get("weight_g", 1)

            if new_weight <= 0:
                await message.answer("⚠️ Вес должен быть положительным числом.")
                return

            # 🔥 Пересчёт пропорционально новому весу
            factor = new_weight / old_weight
            for key in ["cal", "protein", "fat", "carbs"]:
                item[key] = round(item.get(key, 0) * factor, 2)
            item["weight_g"] = new_weight

            # 🔄 Пересчёт общего КБЖУ
            total = {"cal": 0, "protein": 0, "fat": 0, "carbs": 0}
            for i in wf["meal"]["items"]:
                total["cal"] += i.get("cal", 0)
                total["protein"] += i.get("protein", 0)
                total["fat"] += i.get("fat", 0)
                total["carbs"] += i.get("carbs", 0)
            # 🔢 Округляем значения для стабильного отображения
            total = {k: round(v, 2) for k, v in total.items()}
            wf["meal"]["total"] = total

            wf["stage"] = None

            # ✅ Показываем обновлённое блюдо
            await message.answer(
                f"✅ Вес обновлён и КБЖУ пересчитано!\n\n"
                f"🔥 *Итого:* {round(total['cal'])} ккал\n"
                f"Б: {round(total['protein'])} г  Ж: {round(total['fat'])} г  У: {round(total['carbs'])} г",
                parse_mode="Markdown"
            )
            await show_updated_meal(message.from_user.id)

        except ValueError:
            await message.answer("⚠️ Введите корректное число (в граммах).")
        return
    
        # Если идёт ручной ввод блюда
    if wf and wf.get("mode") == "manual_input":
        dp.workflow_data[user_key]["mode"] = None
        user_text = message.text.strip()
        await message.answer("🍽️ Анализирую блюдо...")

        try:
            model = "gemini-2.5-flash" if is_premium_active(message.from_user.id) else "gemini-2.5-flash-lite"
            gen_model = genai.GenerativeModel(model)

            # 🧠 Промпт для Gemini
            prompt = f"""
            Ты — эксперт по питанию. Пользователь описал блюдо:
            "{user_text}"

            Определи ингредиенты, примерный вес и рассчитай КБЖУ.
            Ответ строго в JSON формате, как в примере:

            {{
            "items": [
                {{"name": "курица", "weight_g": 150, "cal": 230, "protein": 32, "fat": 5, "carbs": 0}},
                {{"name": "рис", "weight_g": 200, "cal": 260, "protein": 6, "fat": 2, "carbs": 56}}
            ],
            "total": {{"cal": 490, "protein": 38, "fat": 7, "carbs": 56}}
            }}
            """

            # --- Отправляем запрос в Gemini ---
            response = await asyncio.to_thread(gen_model.generate_content, [prompt])

            # ✅ Универсальный способ извлечь текст из ответа Gemini
            if hasattr(response, "text") and response.text:
                result = response.text.strip()
            elif hasattr(response, "candidates"):
                try:
                    result = response.candidates[0].content.parts[0].text.strip()
                except Exception:
                    result = ""
            else:
                result = str(response).strip()

            # 🧹 Очистка и попытка вытащить JSON
            cleaned = result.replace("```json", "").replace("```", "").strip()

            if not cleaned.startswith("{"):
                match = re.search(r"\{.*\}", cleaned, re.DOTALL)
                cleaned = match.group(0) if match else "{}"

            try:
                data = json.loads(cleaned)
            except Exception as e:
                logging.warning(f"⚠️ Ошибка парсинга JSON Gemini: {e}\nОтвет: {result}")
                data = {"items": [], "total": {}}

            items, total = data.get("items", []), data.get("total", {})

            # Если ничего не найдено
            if not items:
                await message.answer("⚠️ Не удалось определить блюдо. Попробуй уточнить или переформулировать.")
                return

            kcal = total.get("cal", 0)
            p, f, c = total.get("protein", 0), total.get("fat", 0), total.get("carbs", 0)

            text = "🍽️ *Анализ блюда:*\n" + "\n".join(
                [f"- {i['name']} ({i['weight_g']} г)" for i in items]
            )
            text += f"\n\n🔥 *Итого:* {round(kcal)} ккал\nБ: {round(p)} г  Ж: {round(f)} г  У: {round(c)} г"

            builder = InlineKeyboardBuilder()
            builder.button(text="✏️ Изменить ингредиент", callback_data="edit_meal")
            builder.button(text="✅ Добавить в статистику", callback_data="save_meal_to_stats")
            if not is_premium_active(message.from_user.id):
                builder.button(text="💎 Получить Premium", callback_data="buy_premium")
            builder.adjust(2)

            dp.workflow_data[user_key] = {"meal": {"items": items, "total": total}}
            await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())

        except Exception as e:
            logging.error(f"Ошибка анализа текста: {e}")
            await message.answer("⚠️ Ошибка анализа текста. Попробуй снова.")
            return

    # Если ни один режим не активен
    await message.answer("⚙️ Пожалуйста, выбери действие из меню 👇", reply_markup=main_menu())


# ======================================
# 🍝 Обработка фото и анализ Gemini
# ======================================

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка фото еды и анализ через Gemini."""
    ok, reason = can_analyze_photo(message.from_user.id)
    if not ok:
        await message.answer(reason, parse_mode="Markdown")
        return

    await message.answer("🧠 Анализирую блюдо…")
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)

    # --- безопасная загрузка файла ---
    try:
        image_bytes = await safe_download(bot, file.file_path)
    except Exception as e:
        logging.error(f"⚠️ Ошибка загрузки файла: {e}")
        await message.answer("⚠️ Не удалось загрузить фото. Проверь соединение и попробуй снова.")
        return

    # лимит фото
    try:
        increment_photo(message.from_user.id)
    except Exception:
        logging.exception("Ошибка increment_photo")

    try:
        model = "gemini-2.5-flash" if is_premium_active(message.from_user.id) else "gemini-2.5-flash-lite"
        gen_model = genai.GenerativeModel(model)

        response = await asyncio.to_thread(gen_model.generate_content, [ANALYSIS_PROMPT, {"mime_type": "image/jpeg", "data": image_bytes}])

        # ✅ Проверяем разные варианты, как Gemini возвращает ответ
        if hasattr(response, "text") and response.text:
            result = response.text.strip()
        elif hasattr(response, "candidates"):
            try:
                result = response.candidates[0].content.parts[0].text.strip()
            except Exception:
                result = ""
        else:
            result = str(response).strip()
        if result.startswith("```"):
            result = result.replace("```json", "").replace("```", "").strip()

        # 🧠 Безопасно обрабатываем ответ Gemini
        if not result or not isinstance(result, str):
            await message.answer("⚠️ Gemini не смог распознать фото. Попробуй другое изображение или более чёткое фото.")
            return

        # 🧹 Если Gemini вернул Markdown — чистим от ```json
        cleaned = result.replace("```json", "").replace("```", "").strip()

        # ⚙️ Если ответ не похож на JSON — пытаемся вытащить JSON из текста
        if not cleaned.startswith("{"):
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            cleaned = match.group(0) if match else "{}"

        try:
            data = json.loads(cleaned)
        except Exception as e:
            logging.error(f"⚠️ Ошибка парсинга JSON Gemini: {e}\nОтвет: {cleaned}")
            await message.answer("⚠️ Не удалось обработать ответ Gemini. Попробуй другое фото.")
            return

        # ✅ ВОТ ЭТИ 2 СТРОКИ НУЖНО ДОБАВИТЬ
        items = data.get("items", [])
        total = data.get("total", {})

        if not items:
            await message.answer("⚠️ Не удалось определить ингредиенты. Попробуй другое фото.")
            return

        kcal = total.get("cal", 0)
        p = total.get("protein", 0)
        f = total.get("fat", 0)
        c = total.get("carbs", 0)

        text = "🍽️ *Обнаружено:*\n"
        for i in items:
            text += f"- {i.get('name', '—')} ({i.get('weight_g', 0)} г)\n"
        text += f"\n🔥 *Итого:* {round(kcal)} ккал\nБ: {round(p)} г  Ж: {round(f)} г  У: {round(c)} г"

        builder = InlineKeyboardBuilder()
        builder.button(text="✏️ Изменить ингредиент", callback_data="edit_meal")
        builder.button(text="✅ Добавить в статистику", callback_data="save_meal_to_stats")
        if not is_premium_active(message.from_user.id):
            builder.button(text="💎 Получить Premium", callback_data="buy_premium")
        builder.adjust(2)


        await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())

        dp.workflow_data[str(message.from_user.id)] = {"meal": {"items": items, "total": total}}

    except Exception as e:
        logging.error(f"Ошибка анализа Gemini: {e}")
        await message.answer("⚠️ Ошибка анализа фото. Попробуй снова.")

# ======================================
# 💎 Premium-заглушки и обработка кнопок
# ======================================

@dp.callback_query(F.data.in_({"edit_meal", "delete_meal", "save_meal_to_stats"}))
async def handle_meal_actions(callback: types.CallbackQuery):
    """Обработка кнопок изменения, удаления и добавления блюда."""

    user_id = callback.from_user.id

    # Проверяем Premium
    if not is_premium_active(user_id):
        promo_text = (
            "💎 *Функции редактирования и управления доступны только в TasteBalance Premium!*\n\n"
            "🚀 Что ты получишь:\n"
            "• Изменение и удаление ингредиентов\n"
            "• Добавление блюд в статистику\n"
            "• Безлимит фото и текстов\n"
            "• Более точный анализ состава\n\n"
            "✨ Активируй Premium и управляй питанием как профи 👇"
        )

        builder = InlineKeyboardBuilder()
        if not is_premium_active(user_id):
            builder.button(text="💎 Получить Premium", callback_data="buy_premium")
        builder.adjust(1)

        await callback.message.answer(promo_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        await callback.answer()
        return  # 👈 добавлен return, чтобы не выполнялся код ниже

    # Если Premium активен — обрабатываем кнопки дальше
    if callback.data == "edit_meal":
        await edit_meal(callback)
    elif callback.data == "delete_meal":
        await delete_item(callback)
    elif callback.data == "save_meal_to_stats":
        await save_meal_to_stats(callback)
    else:
        await callback.answer()

# ======================================
# ✏️ Редактирование ингредиентов
# ======================================

@dp.callback_query(F.data == "edit_meal")
async def edit_meal(callback: types.CallbackQuery):
    """Показать список ингредиентов для редактирования."""
    if not is_premium_active(callback.from_user.id):
        await callback.message.answer("💎 Редактирование доступно только в Premium.")
        await callback.answer()
        return

    wf = dp.workflow_data.get(str(callback.from_user.id))
    if not wf or "meal" not in wf:
        await callback.message.answer("⚠️ Нет данных для редактирования. Сначала проанализируй фото.")
        await callback.answer()
        return

    items = wf["meal"]["items"]
    builder = InlineKeyboardBuilder()
    for i, item in enumerate(items):
        builder.button(text=f"{item['name']} ({item['weight_g']} г)", callback_data=f"edit_item:{i}")
    builder.adjust(2)

    await callback.message.answer("🔍 Выберите ингредиент для изменения:", reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_item:"))
async def edit_item(callback: types.CallbackQuery):
    """Выбор действия для конкретного ингредиента."""
    idx = int(callback.data.split(":")[1])
    wf = dp.workflow_data.get(str(callback.from_user.id))
    if not wf:
        await callback.message.answer("⚠️ Ошибка редактирования.")
        await callback.answer()
        return

    wf["editing_index"] = idx
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить название", callback_data="edit_name")
    builder.button(text="📏 Изменить вес", callback_data="edit_weight")
    builder.button(text="🗑 Удалить", callback_data="delete_item")
    builder.adjust(1)

    item = wf["meal"]["items"][idx]
    await callback.message.answer(
        f"🔧 *Ингредиент:* {item['name']} ({item['weight_g']} г)\nЧто хотите изменить?",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data == "edit_name")
async def edit_name(callback: types.CallbackQuery):
    wf = dp.workflow_data.get(str(callback.from_user.id))
    wf["stage"] = "await_name"
    await callback.message.answer("✏️ Введите новое название ингредиента:")
    await callback.answer()


@dp.callback_query(F.data == "edit_weight")
async def edit_weight(callback: types.CallbackQuery):
    wf = dp.workflow_data.get(str(callback.from_user.id))
    wf["stage"] = "await_weight"
    await callback.message.answer("📏 Введите новый вес (в граммах):")
    await callback.answer()


@dp.callback_query(F.data == "delete_item")
async def delete_item(callback: types.CallbackQuery):
    wf = dp.workflow_data.get(str(callback.from_user.id))
    idx = wf.get("editing_index")
    if idx is None:
        await callback.message.answer("⚠️ Ошибка: ингредиент не найден.")
        await callback.answer()
        return

    item = wf["meal"]["items"].pop(idx)
    await callback.message.answer(f"🗑 Удалено: *{item['name']}*", parse_mode="Markdown")
    await show_updated_meal(callback.from_user.id)
    await callback.answer()

# ======================================
# 🧮 Пересчёт и обновление блюда
# ======================================

async def show_updated_meal(user_id):
    """Показать пересчитанное блюдо после изменений и добавить кнопку для сохранения."""
    wf = dp.workflow_data.get(str(user_id))
    if not wf or "meal" not in wf:
        return

    items = wf["meal"]["items"]
    total = {"cal": 0, "protein": 0, "fat": 0, "carbs": 0}

    for i in items:
        total["cal"] += i.get("cal", 0)
        total["protein"] += i.get("protein", 0)
        total["fat"] += i.get("fat", 0)
        total["carbs"] += i.get("carbs", 0)

    # 🔢 Округляем значения для стабильного отображения
    total = {k: round(v, 2) for k, v in total.items()}
    wf["meal"]["total"] = total


    text = "🍽️ *Обновлённое блюдо:*\n"
    for i in items:
        text += f"- {i['name']} ({i['weight_g']} г)\n"
    text += (
        f"\n🔥 *Итого:* {round(total['cal'])} ккал\n"
        f"Б: {round(total['protein'])} г  "
        f"Ж: {round(total['fat'])} г  "
        f"У: {round(total['carbs'])} г"
    )

    builder = InlineKeyboardBuilder()
    for i, item in enumerate(items):
        builder.button(text=f"{item['name']} ({item['weight_g']} г)", callback_data=f"edit_item:{i}")
    builder.button(text="✅ Добавить в статистику", callback_data="save_meal_to_stats")
    builder.adjust(2)

    await bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=builder.as_markup())

# ======================================
# 💾 Сохранение обновлённого блюда в статистику
# ======================================

@dp.callback_query(F.data == "save_meal_to_stats")
async def save_meal_to_stats(callback: types.CallbackQuery):
    """Добавление обновлённого блюда в статистику."""
    wf = dp.workflow_data.get(str(callback.from_user.id))
    if not wf or "meal" not in wf:
        await callback.message.answer("⚠️ Нет данных для сохранения. Попробуйте снова.")
        await callback.answer()
        return

    total = wf["meal"]["total"]
    kcal = total.get("cal", 0)
    p = total.get("protein", 0)
    f = total.get("fat", 0)
    c = total.get("carbs", 0)

    desc = ", ".join([i["name"] for i in wf["meal"]["items"]])
    save_meal(callback.from_user.id, desc, kcal, p, f, c)

    await callback.message.answer("✅ Блюдо успешно добавлено в статистику за сегодня!")
    await callback.answer()

# ======================================
# 🕒 Автоматические отчёты для Premium
# ======================================

async def send_summaries():
    """Автоотчёты для Premium-пользователей в 21:00."""
    while True:
        now = datetime.now()
        if now.hour == 21 and now.minute < 10:
            cursor.execute("SELECT user_id FROM users WHERE is_premium=1")
            for (uid,) in cursor.fetchall():
                kcal, p, f, c = get_stats(uid)
                if kcal > 0:
                    await bot.send_message(
                        uid,
                        f"📊 *Отчёт за сегодня:*\n"
                        f"Ккал: {round(kcal)}\n"
                        f"Б: {round(p)} г  Ж: {round(f)} г  У: {round(c)} г",
                        parse_mode="Markdown"
                    )
        await asyncio.sleep(600)

# ======================================
# ▶️ Запуск TasteBalance
# ======================================

async def main():
    await set_commands(bot)

    # Запускаем Stripe webhook server, если настроен или для теста
    try:
        asyncio.create_task(start_stripe_webserver(host="0.0.0.0", port=8080))
    except Exception as e:
        logging.exception("Failed to start stripe webserver: %s", e)

    asyncio.create_task(send_summaries())
    logging.info("🚀 TasteBalance запущен и готов к приёму сообщений.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
