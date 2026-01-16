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
    premium_until TEXT,
    language TEXT DEFAULT 'ru',
    rmr REAL,
    protein_goal INTEGER,
    carbs_min INTEGER,
    carbs_max INTEGER,
    fat_limit INTEGER,
    fibre_goal INTEGER,
    activity_calories INTEGER
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

def delete_full_meal(user_id, meal_id: int) -> bool:
    """Удалить одно блюдо пользователя по id. Возвращает True, если что-то удалили."""
    cursor.execute(
        "DELETE FROM meals WHERE id=? AND user_id=?",
        (meal_id, user_id)
    )
    conn.commit()
    return cursor.rowcount > 0


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
            "INSERT INTO users (user_id, is_premium, last_date, photos_today, premium_until, language) VALUES (?, 0, ?, 0, NULL, 'ru')",
            (user_id, date.today().isoformat())
        )
        conn.commit()
        return (user_id, 0, date.today().isoformat(), 0, None, 'ru')
    return user


def get_user_lang(user_id):
    """Получить язык пользователя."""
    user = get_user(user_id)
    return user[5] if len(user) > 5 else 'ru'


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
- Если пользователь/контекст указывают количество в штуках (1 шт., 2 яйца, 3 банана и т.п.), переводи в граммы по типичным средним весам:
  * 1 яичный белок ≈ 33 г
  * 1 целое яйцо (среднее) ≈ 55 г
  * 1 большое яйцо ≈ 65 г
  * 1 банан (средний) ≈ 120 г
  * 1 ломтик хлеба ≈ 30 г
  * 1 кусочек твёрдого сыра ≈ 25 г
- Если размер не указан (просто «яйцо», «банан»), используй средний вариант.
- Всегда указывай итоговый вес ингредиентов в граммах в поле "weight_g".

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
# 🌐 Языковая поддержка
# ======================================

LANG = {
    "ru": {
        "main_menu_btn": "👋 Главное меню",
        "nutrition_report_btn": "📊 Отчёт по целям",
        "greeting": "👋 Привет, {first_name}!\n\nЯ — *TasteBalance*, твой AI-ассистент по питанию 🍽️\n\n💎 *Статус:* {status}\n\n📸 Просто отправь фото еды или напиши, что ты ел — я определю состав и КБЖУ.\n\nИли выбери действие из меню: 👇",
        "stats_today": "📈 *Сегодняшний результат:*\n🔥 Калории: {kcal} ккал\n🍗 Белки: {p} г\n🥑 Жиры: {f} г\n🍞 Углеводы: {c} г",
        "stats_empty": "🫙 Сегодня ещё ничего не добавлено.",
        "history_empty": "📭 История пуста за последние 7 дней.",
        "history_title": "🕒 *История за 7 дней:*\n\n",
        "history_item": "{date_part}\n🍽️ {ingredients}\n🔥 {kcal} ккал — Б: {p} Ж: {f} У: {c}\n\n",
        "delete_prompt": "Чтобы удалить запись, нажми на кнопку 🗑 ниже ⬇️",
        "help_text": "ℹ️ *TasteBalance — твой AI-ассистент по питанию!*\n\n📸 Просто отправь фото еды или напиши блюдо — я определю состав и КБЖУ.\n\n💡 Для максимальной точности пиши вес в граммах. Формат \"2 яйца, 1 банан\" я тоже понимаю — это будет средний размер.\n\n🆓 *Бесплатно:* 2 фото в день\n💎 *Premium:* безлимит, улучшенная точность и автоотчёты\n\n📋 *Команды:*\n/start — главное меню\n/stats — статистика за день\n/history — история за неделю\n/premium — Premium-возможности\n/help — справка",
        "manual_input_prompt": "📝 Введи блюдо текстом, например:\n\n_овсянка с молоком 100г и бананом 50г_\n_или же просто напиши:_\n_курица с рисом и овощами_\n\n✨ Для максимальной точности указывай вес в граммах.\nФормат вроде _\"3 яйца\", \"2 банана\"_ я тоже понимаю — я переведу их в средний вес.",
        "feedback_choose": "💬 Выберите, что хотите отправить 👇",
        "premium_info": "💎 *TasteBalance Premium*\n\n✅ Безлимит фото и анализов\n⚡ Улучшенная точность расчёта\n🍽️ Возможность редактировать блюда и ингредиенты\n📊 Автоотчёты за день и неделю\n🚀 Приоритетная скорость анализа\n\n💰 Всего $7.99 в месяц\n\nНажми ниже, чтобы оформить 👇",
        "premium_features": "💎 *Что входит в Premium:*\n\n1. Безлимит фото и текстов\n2. Повышенная точность анализа\n3. Возможность редактировать ингредиенты\n4. Автоотчёты за день и неделю\n5. Быстрая очередь обработки ⚡",
        "premium_active": "✅ Premium активен! Наслаждайтесь полным функционалом 💪",
        "premium_inactive": "⚠️ Premium не активирован. Нажми /premium, чтобы оформить 💎",
        "premium_already_active": "💎 У тебя уже активен Premium.",
        "premium_until": "\n\nОн действует до: *{until}*",
        "premium_no_need": "\n\nОплачивать ещё раз сейчас не нужно 🚫.",
        "payment_processing": "🔒 Оплата проходит через Stripe.\n\nНажмите кнопку ниже — вас перенесёт на безопасную страницу оплаты.",
        "payment_error": "⚠️ Не удалось создать платёжную сессию. Обратитесь к Автору через кнопку 'Отправить Отзыв или Сотрудничество'",
        "analyzing_photo": "🧠 Анализирую блюдо…",
        "analyzing_text": "🍽️ Анализирую блюдо...",
        "analysis_failed": "⚠️ Не удалось определить блюдо. Попробуй уточнить или переформулировать.",
        "meal_analysis": "🍽️ *Анализ блюда:*\n{lines}\n\n🔥 *Итого:* {kcal} ккал\nБ: {p} г  Ж: {f} г  У: {c} г",
        "updated_meal": "🍽️ *Обновлённое блюдо:*\n{lines}\n🔥 *Итого:* {kcal} ккал\nБ: {p} г  Ж: {f} г  У: {c} г",
        "meal_saved": "✅ Блюдо успешно добавлено в статистику за сегодня!\n\nМожешь продолжить — выбери действие из меню 👇",
        "photo_limit": "📸 Сегодня лимит 2 фото.\n\n💎 *TasteBalance Premium* — без ограничений и с точным анализом.\nНажми «Получить Premium» ниже 👇",
        "download_error": "⚠️ Не удалось загрузить фото. Проверь соединение и попробуй снова.",
        "analysis_error": "⚠️ Ошибка анализа фото. Попробуй снова.",
        "text_analysis_error": "⚠️ Ошибка анализа текста. Попробуй снова.",
        "no_data_edit": "⚠️ Нет данных для редактирования. Сначала проанализируй фото.",
        "edit_ingredient": "🔍 Выберите ингредиент для изменения:",
        "edit_actions": "🔧 *Ингредиент:* {name} ({weight} г)\nЧто хотите изменить?",
        "enter_new_name": "✏️ Введите новое название ингредиента:",
        "enter_new_weight": "📏 Введите новый вес (в граммах):",
        "weight_invalid": "⚠️ Вес должен быть положительным числом.",
        "name_updated": "✅ Название обновлено, КБЖУ пересчитано!\n\n🔥 *Итого:* {kcal} ккал\nБ: {p} г  Ж: {f} г  У: {c} г",
        "weight_updated": "✅ Вес обновлён и КБЖУ пересчитано!\n\n🔥 *Итого:* {kcal} ккал\nБ: {p} г  Ж: {f} г  У: {c} г",
        "item_deleted": "🗑 Удалено: *{name}*",
        "no_data_save": "⚠️ Нет данных для сохранения. Попробуйте снова.",
        "feedback_sent": "✅ Спасибо! Сообщение отправлено разработчику 🙌\n\nТы можешь вернуться в главное меню — просто введи /start 💬",
        "feedback_error": "⚠️ Не удалось отправить сообщение. Попробуй позже.",
        "choose_action": "⚙️ Пожалуйста, выбери действие из меню 👇",
        "premium_required": "💎 *Функции редактирования и управления доступны только в TasteBalance Premium!*\n\n🚀 Что ты получишь:\n• Изменение и удаление ингредиентов\n• Добавление блюд в статистику\n• Безлимит фото и текстов\n• Более точный анализ состава\n\n✨ Активируй Premium и управляй питанием как профи 👇",
        "delete_success": "🗑 Запись удалена из статистики и истории.",
        "delete_error": "⚠️ Не удалось удалить запись (возможно, она уже удалена).",
        "admin_premium": "✅ Админ-Premium активирован на 30 дней.",
        "language_prompt": "🌐 Выберите язык / Choose language:",
        "rus_button": "🇷🇺 Русский",
        "eng_button": "🇬🇧 English",
        "add_ingredient_button": "➕ Добавить ингредиент",
        "enter_new_ingredient": "📝 Введите новый ингредиент (например, 'банан 100г' или просто 'курица'):",
        "setup_rmr": "📝 Шаг 1/6: Введите ваш RMR (базовый метаболизм, ккал/день) или 'calculate' для автоматического расчёта:",
        "setup_protein": "📝 Шаг 2/6: Цель по белку (г/день). По умолчанию 220:",
        "setup_carbs_min": "📝 Шаг 3/6: Минимум углеводов (г/день). По умолчанию 330:",
        "setup_carbs_max": "📝 Шаг 4/6: Максимум углеводов (г/день). По умолчанию 360:",
        "setup_fat": "📝 Шаг 5/6: Лимит жиров (г/день). По умолчанию 70:",
        "setup_fibre": "📝 Шаг 6/6: Цель по клетчатке (г/день). По умолчанию 40:",
        "setup_done": "✅ *Настройки сохранены!*\n\n📊 Ваши цели:\n🔥 RMR: {rmr} ккал\n🍗 Белок: {protein} г\n🍞 Углеводы: {carbs_min}-{carbs_max} г\n🥑 Жиры: до {fat} г\n🌾 Клетчатка: {fibre} г",
        "setup_premium_only": "💎 Настройка целей питания доступна только в Premium.",
        "setup_invalid_number": "⚠️ Введите корректное число.",
        "stats_btn": "📊 Статистика",
        "history_btn": "🕒 История",
        "manual_input_btn": "✍️ Ввести вручную",
        "premium_btn": "💎 Premium",
        "language_btn": "🌐 Язык",
        "feedback_btn": "💌 Отправить отзыв / сотрудничество"
    },
    "en": {
        "main_menu_btn": "👋 Main Menu",
        "nutrition_report_btn": "📊 Nutrition Report",
        "greeting": "👋 Hi, {first_name}!\n\nI'm *TasteBalance*, your AI nutrition assistant 🍽️\n\n💎 *Status:* {'Premium active ✅' if is_premium else 'Free account (2 photos per day)'}\n\n📸 Just send a photo of food or write what you ate — I'll determine the composition and macros.\n\nOr choose an action from the menu: 👇",
        "stats_today": "📈 *Today's result:*\n🔥 Calories: {kcal} kcal\n🍗 Protein: {p} g\n🥑 Fat: {f} g\n🍞 Carbs: {c} g",
        "stats_empty": "🫙 Nothing added today yet.",
        "history_empty": "📭 History is empty for the last 7 days.",
        "history_title": "🕒 *History for 7 days:*\n\n",
        "history_item": "{date_part}\n🍽️ {ingredients}\n🔥 {kcal} kcal — P: {p} F: {f} C: {c}\n\n",
        "delete_prompt": "To delete a record, click the 🗑 button below ⬇️",
        "help_text": "ℹ️ *TasteBalance — your AI nutrition assistant!*\n\n📸 Just send a photo of food or write a dish — I'll determine the composition and macros.\n\n💡 For maximum accuracy, write weight in grams. Format like \"2 eggs, 1 banana\" I also understand — it will be average size.\n\n🆓 *Free:* 2 photos per day\n💎 *Premium:* unlimited, improved accuracy and auto-reports\n\n📋 *Commands:*\n/start — main menu\n/stats — daily stats\n/history — history for a week\n/premium — Premium features\n/help — help",
        "manual_input_prompt": "📝 Enter the dish in text, for example:\n\n_oatmeal with milk 100g and banana 50g_\n_or just write:_\n_chicken with rice and vegetables_\n\n✨ For maximum accuracy, specify weight in grams.\nFormat like _\"3 eggs\", \"2 bananas\"_ I also understand — I'll convert them to average weight.",
        "feedback_choose": "💬 Choose what you want to send 👇",
        "premium_info": "💎 *TasteBalance Premium*\n\n✅ Unlimited photos and analyses\n⚡ Improved calculation accuracy\n🍽️ Ability to edit dishes and ingredients\n📊 Auto-reports for day and week\n🚀 Priority processing speed\n\n💰 Only $7.99 per month\n\nClick below to subscribe 👇",
        "premium_features": "💎 *What's included in Premium:*\n\n1. Unlimited photos and texts\n2. Increased analysis accuracy\n3. Ability to edit ingredients\n4. Auto-reports for day and week\n5. Fast processing queue ⚡",
        "premium_active": "✅ Premium is active! Enjoy full functionality 💪",
        "premium_inactive": "⚠️ Premium is not activated. Click /premium to subscribe 💎",
        "premium_already_active": "💎 You already have active Premium.",
        "premium_until": "\n\nIt is active until: *{until}*",
        "premium_no_need": "\n\nNo need to pay again right now 🚫.",
        "payment_processing": "🔒 Payment is processed through Stripe.\n\nClick the button below — you'll be taken to the secure payment page.",
        "payment_error": "⚠️ Failed to create payment session. Contact the Author via 'Send Feedback or Cooperation' button",
        "analyzing_photo": "🧠 Analyzing the dish…",
        "analyzing_text": "🍽️ Analyzing the dish...",
        "analysis_failed": "⚠️ Could not determine the dish. Try to clarify or rephrase.",
        "meal_analysis": "🍽️ *Dish analysis:*\n{lines}\n\n🔥 *Total:* {kcal} kcal\nP: {p} g  F: {f} g  C: {c} g",
        "updated_meal": "🍽️ *Updated dish:*\n{lines}\n🔥 *Total:* {kcal} kcal\nP: {p} g  F: {f} g  C: {c} g",
        "meal_saved": "✅ Dish successfully added to today's stats!\n\nYou can continue — choose an action from the menu 👇",
        "photo_limit": "📸 Today's limit is 2 photos.\n\n💎 *TasteBalance Premium* — no limits and accurate analysis.\nClick 'Get Premium' below 👇",
        "download_error": "⚠️ Failed to download photo. Check connection and try again.",
        "analysis_error": "⚠️ Photo analysis error. Try again.",
        "text_analysis_error": "⚠️ Text analysis error. Try again.",
        "no_data_edit": "⚠️ No data to edit. Analyze a photo first.",
        "edit_ingredient": "🔍 Select an ingredient to change:",
        "edit_actions": "🔧 *Ingredient:* {name} ({weight} g)\nWhat do you want to change?",
        "enter_new_name": "✏️ Enter new ingredient name:",
        "enter_new_weight": "📏 Enter new weight (in grams):",
        "weight_invalid": "⚠️ Weight must be a positive number.",
        "name_updated": "✅ Name updated, macros recalculated!\n\n🔥 *Total:* {kcal} kcal\nP: {p} g  F: {f} g  C: {c} g",
        "weight_updated": "✅ Weight updated and macros recalculated!\n\n🔥 *Total:* {kcal} kcal\nP: {p} g  F: {f} g  C: {c} g",
        "item_deleted": "🗑 Deleted: *{name}*",
        "no_data_save": "⚠️ No data to save. Try again.",
        "feedback_sent": "✅ Thanks! Message sent to the developer 🙌\n\nYou can return to the main menu — just type /start 💬",
        "feedback_error": "⚠️ Failed to send message. Try later.",
        "choose_action": "⚙️ Please choose an action from the menu 👇",
        "premium_required": "💎 *Editing and management features are available only in TasteBalance Premium!*\n\n🚀 What you'll get:\n• Edit and delete ingredients\n• Add dishes to stats\n• Unlimited photos and texts\n• More accurate composition analysis\n\n✨ Activate Premium and manage nutrition like a pro 👇",
        "delete_success": "🗑 Record deleted from stats and history.",
        "delete_error": "⚠️ Failed to delete record (maybe it's already deleted).",
        "admin_premium": "✅ Admin-Premium activated for 30 days.",
        "language_prompt": "🌐 Выберите язык / Choose language:",
        "rus_button": "🇷🇺 Русский",
        "eng_button": "🇬🇧 English",
        "add_ingredient_button": "➕ Add Ingredient",
        "enter_new_ingredient": "📝 Enter new ingredient (e.g., 'banana 100g' or just 'chicken'):",
        "setup_rmr": "📝 Step 1/6: Enter your RMR (basal metabolism, kcal/day) or 'calculate' for automatic calculation:",
        "setup_protein": "📝 Step 2/6: Protein goal (g/day). Default 220:",
        "setup_carbs_min": "📝 Step 3/6: Minimum carbs (g/day). Default 330:",
        "setup_carbs_max": "📝 Step 4/6: Maximum carbs (g/day). Default 360:",
        "setup_fat": "📝 Step 5/6: Fat limit (g/day). Default 70:",
        "setup_fibre": "📝 Step 6/6: Fibre goal (g/day). Default 40:",
        "setup_done": "✅ *Settings saved!*\n\n📊 Your goals:\n🔥 RMR: {rmr} kcal\n🍗 Protein: {protein} g\n🍞 Carbs: {carbs_min}-{carbs_max} g\n🥑 Fat: up to {fat} g\n🌾 Fibre: {fibre} g",
        "setup_premium_only": "💎 Nutrition goals setup is available only in Premium.",
        "setup_invalid_number": "⚠️ Enter a valid number.",
        "stats_btn": "📊 Statistics",
        "history_btn": "🕒 History",
        "manual_input_btn": "✍️ Manual Input",
        "premium_btn": "💎 Premium",
        "language_btn": "🌐 Language",
        "feedback_btn": "💌 Send Feedback / Cooperation"
    }
}

# ======================================
# 📋 Главное меню и команды
# ======================================

def main_menu(lang):
    keyboard = [
        [types.KeyboardButton(text=LANG[lang]["stats_btn"]), types.KeyboardButton(text=LANG[lang]["history_btn"])],
        [types.KeyboardButton(text=LANG[lang]["manual_input_btn"]), types.KeyboardButton(text=LANG[lang]["premium_btn"])],
        [types.KeyboardButton(text=LANG[lang]["nutrition_report_btn"]), types.KeyboardButton(text=LANG[lang]["language_btn"])],
        [types.KeyboardButton(text=LANG[lang]["feedback_btn"]), types.KeyboardButton(text=LANG[lang]["main_menu_btn"])]
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# ======================================
# 👋 /start
# ======================================

@dp.message(Command("start"))
@dp.message(F.text == "👋 Главное меню")
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    lang = get_user_lang(user_id)

    if not lang or lang not in LANG:
        # Language not set, show language selection
        builder = InlineKeyboardBuilder()
        builder.button(text=LANG["ru"]["rus_button"], callback_data="set_lang_ru")
        builder.button(text=LANG["en"]["eng_button"], callback_data="set_lang_en")
        builder.adjust(1)
        await message.answer(LANG["ru"]["language_prompt"], reply_markup=builder.as_markup())
        return

    is_premium = is_premium_active(user_id)
    status = "Premium активен ✅" if is_premium else "Бесплатный аккаунт (2 фото в день)" if lang == "ru" else "Premium active ✅" if is_premium else "Free account (2 photos per day)"
    greeting = LANG[lang]["greeting"].format(first_name=message.from_user.first_name or ('друг' if lang == "ru" else 'friend'), status=status)

    await message.answer(greeting, parse_mode="Markdown", reply_markup=main_menu(lang))


@dp.callback_query(F.data == "set_lang_ru")
async def set_lang_ru(callback: types.CallbackQuery):
    update_user(callback.from_user.id, language="ru")
    await start_cmd(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "set_lang_en")
async def set_lang_en(callback: types.CallbackQuery):
    update_user(callback.from_user.id, language="en")
    await start_cmd(callback.message)
    await callback.answer()


# ======================================
# 📊 /stats — статистика за день
# ======================================

@dp.message(Command("stats"))
@dp.message(F.text == "📊 Статистика")
async def stats_cmd(message: types.Message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    kcal, p, f, c = get_stats(user_id)
    if kcal and kcal > 0:
        text = LANG[lang]["stats_today"].format(kcal=round(kcal), p=round(p), f=round(f), c=round(c))
    else:
        text = LANG[lang]["stats_empty"]
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu())


# ======================================
# 🕒 /history — история за неделю
# ======================================

@dp.message(Command("history"))
@dp.message(F.text == "🕒 История")
async def history_cmd(message: types.Message):
    """Показать историю за последние 7 дней и дать возможность удалить записи."""
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    cursor.execute(
        "SELECT id, date, time, description, calories, protein, fat, carbs "
        "FROM meals WHERE user_id=? AND date>=? "
        "ORDER BY date DESC, time DESC",
        (user_id, (date.today() - timedelta(days=7)).isoformat())
    )
    rows = cursor.fetchall()

    if not rows:
        await message.answer(LANG[lang]["history_empty"], reply_markup=main_menu(lang))
        return

    text = LANG[lang]["history_title"]
    for meal_id, d, t, desc, kcal, p, f, c in rows:
        date_part = f"📅 {d}"
        ingredients = desc.replace("Фото еды", "📷 Фото блюда" if lang == "ru" else "📷 Photo of dish")

        text += LANG[lang]["history_item"].format(date_part=date_part, ingredients=ingredients, kcal=round(kcal), p=round(p), f=round(f), c=round(c))

    # Инлайн-кнопки для удаления (ограничим, скажем, 15 последними записями, чтобы не раздувать клавиатуру)
    builder = InlineKeyboardBuilder()
    for meal_id, d, t, desc, *_ in rows[:15]:
        short = desc.split(",")[0][:20]  # короткое название
        label_time = t or ""
        builder.button(
            text=f"🗑 {d} {label_time} · {short}",
            callback_data=f"delete_full_meal:{meal_id}"
        )
    if rows:
        builder.adjust(1)

    text += LANG[lang]["delete_prompt"]

    await message.answer(
        text.strip(),
        parse_mode="Markdown",
        reply_markup=builder.as_markup() if rows else main_menu()
    )

# ======================================
# ℹ️ /help — справка
# ======================================

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(message: types.Message):
    lang = get_user_lang(message.from_user.id)
    await message.answer(LANG[lang]["help_text"], parse_mode="Markdown", reply_markup=main_menu(lang))


@dp.message(Command("setup"))
async def setup_cmd(message: types.Message):
    if not is_premium_active(message.from_user.id):
        lang = get_user_lang(message.from_user.id)
        await message.answer(LANG[lang]["setup_premium_only"])
        return
    user_id = str(message.from_user.id)
    dp.workflow_data[user_id] = {"mode": "setup", "step": 1, "data": {}}
    lang = get_user_lang(message.from_user.id)
    await message.answer(LANG[lang]["setup_rmr"], parse_mode="Markdown")


@dp.message(F.text.in_({"📊 Отчёт по целям", "📊 Nutrition Report"}))
async def nutrition_report(message: types.Message):
    if not is_premium_active(message.from_user.id):
        lang = get_user_lang(message.from_user.id)
        await message.answer(LANG[lang]["setup_premium_only"])
        return

    user = get_user(message.from_user.id)
    # Проверяем, что RMR задан (минимум одно поле)
    if len(user) < 14 or not user[6]:  # rmr = users[6]
        lang = get_user_lang(message.from_user.id)
        await message.answer("⚠️ Сначала настрой цели через /setup")
        return

    # Извлекаем все цели
    rmr = user[6] or 0
    protein_goal = user[7]
    carbs_min = user[8]
    carbs_max = user[9]
    fat_limit = user[10]
    fibre_goal = user[11]
    activity_calories = user[12] or 0

    kcal, p, f, c = get_stats(message.from_user.id)
    deficit = (rmr + activity_calories) - kcal

    violations = []
    if protein_goal and p < protein_goal:
        violations.append(f"Белки: {round(p)} < {protein_goal}")
    if fat_limit and f > fat_limit:
        violations.append(f"Жиры: {round(f)} > {fat_limit}")
    if carbs_min and c < carbs_min:
        violations.append(f"Углеводы: {round(c)} < {carbs_min}")
    if carbs_max and c > carbs_max:
        violations.append(f"Углеводы: {round(c)} > {carbs_max}")
    if fibre_goal and c < fibre_goal:  # временно считаем клетчатку частью углеводов
        violations.append(f"Клетчатка: {round(c)} < {fibre_goal}")

    lang = get_user_lang(message.from_user.id)
    text = (
        f"📊 *{LANG[lang]['nutrition_report_btn'].replace('📊 ', '')}:*\n"
        f"🔥 Калории: {round(kcal)} / {round(rmr + activity_calories)}\n"
        f"🍗 Белки: {round(p)}" + (f" / {protein_goal}" if protein_goal else "") + "\n"
        f"🥑 Жиры: {round(f)}" + (f" / до {fat_limit}" if fat_limit else "") + "\n"
        f"🍞 Углеводы: {round(c)}" + (f" / {carbs_min}–{carbs_max}" if carbs_min or carbs_max else "") + "\n"
        f"⚖️ Дефицит: {round(deficit)} ккал\n"
    )
    if violations:
        text += "⚠️ Нарушения:\n" + "\n".join(violations)
    else:
        text += "✅ Все цели соблюдены!"

    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu(lang))

# ======================================
# ✍️ Ввести вручную
# ======================================

@dp.message(F.text == "✍️ Ввести вручную")
async def manual_input(message: types.Message):
    """Начинает ручной ввод блюда."""
    user_id = str(message.from_user.id)
    dp.workflow_data[user_id] = {"mode": "manual_input"}
    lang = get_user_lang(message.from_user.id)

    await message.answer(LANG[lang]["manual_input_prompt"], parse_mode="Markdown")
    
# ======================================
# 💬 Отзывы и сотрудничество
# ======================================

FEEDBACK_TARGET_ID = 408204060  # <-- замени на свой Telegram ID

@dp.message(Command("feedback"))
@dp.message(F.text == "💌 Отправить отзыв / сотрудничество")
async def feedback_entry(message: types.Message):
    lang = get_user_lang(message.from_user.id)
    builder = InlineKeyboardBuilder()
    builder.button(text="💭 Оставить отзыв" if lang == "ru" else "💭 Leave feedback", callback_data="feedback")
    builder.button(text="🤝 Предложить сотрудничество" if lang == "ru" else "🤝 Suggest cooperation", callback_data="cooperation")
    builder.adjust(1)

    await message.answer(LANG[lang]["feedback_choose"], reply_markup=builder.as_markup())


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
    lang = get_user_lang(callback.from_user.id)
    await callback.message.answer(LANG[lang]["premium_features"], parse_mode="Markdown")
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

    Защита от двойной подписки:
    - если Premium уже активен (по нашей БД), новую оплату не предлагаем.
    """
    await callback.answer()  # быстро закрываем «spinner» у Telegram (без текста)
    user_id = callback.from_user.id

    # 1) Уже активный Premium — не даём оформить ещё раз
    if is_premium_active(user_id):
        user = get_user(user_id)
        premium_until = user[4]  # premium_until хранится в users[4]

        text = "💎 У тебя уже активен Premium."
        if premium_until:
            try:
                dt = datetime.fromisoformat(premium_until)
                text += f"\n\nОн действует до: *{dt.strftime('%d.%m.%Y %H:%M')}*"
            except Exception:
                pass

        text += "\n\nОплачивать ещё раз сейчас не нужно 🚫."
        await callback.message.answer(text, parse_mode="Markdown")
        return

    # 2) Premium не активен — создаём Stripe Checkout
    try:
        url = await asyncio.to_thread(create_checkout_session_sync, user_id)

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
        await callback.message.answer(
            "⚠️ Не удалось создать платёжную сессию. "
            "Обратитесь к Автору через кнопку 'Отправить Отзыв или Сотрудничество'"
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
    """Запускает aiohttp webserver с endpoint /stripe/webhook + страницы успеха/отмены."""
    app = web.Application()

    # Stripe webhook
    app.router.add_post("/stripe/webhook", stripe_webhook)

    # Страницы после оплаты
    app.router.add_get("/success", success_page)
    app.router.add_get("/cancel", cancel_page)
    app.router.add_get("/", root_page)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logging.info(f"Stripe webserver running on {host}:{port}")


    # --- Страницы после оплаты ---

async def success_page(request: web.Request):
    """
    Страница успешной оплаты.
    Stripe редиректит сюда после checkout.
    """
    session_id = request.query.get("session_id", "")
    # Можно дополнительно дернуть Stripe по session_id, но не обязательно.
    html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <title>TasteBalance – Оплата успешна</title>
      </head>
      <body style="font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; text-align:center; padding:40px;">
        <h1>✅ Оплата прошла успешно</h1>
        <p>Можешь вернуться в Telegram – Premium уже активируется (если ещё не активен, подожди пару секунд).</p>
        <p style="color:#666; font-size:14px;">Session ID: {session_id}</p>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")


async def cancel_page(request: web.Request):
    """
    Страница отмены оплаты.
    """
    html = """
    <html>
      <head>
        <meta charset="utf-8" />
        <title>TasteBalance – Оплата отменена</title>
      </head>
      <body style="font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; text-align:center; padding:40px;">
        <h1>❌ Оплата отменена</h1>
        <p>Если передумаешь — вернись в Telegram и снова нажми «Получить Premium».</p>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")


async def root_page(request: web.Request):
    """
    Корень домена — просто заглушка.
    """
    html = """
    <html>
      <head>
        <meta charset="utf-8" />
        <title>TasteBalance</title>
      </head>
      <body style="font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; text-align:center; padding:40px;">
        <h1>TasteBalance</h1>
        <p>Этот домен используется для оплаты и webhook Stripe. Основная работа идёт в Telegram-боте.</p>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

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
    if wf and wf.get("stage") == "await_name":
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
    if wf and wf.get("stage") == "await_weight":
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

    # --- добавление нового ингредиента ---
    if wf and wf.get("stage") == "await_new_ingredient":
        user_text = message.text.strip()
        await message.answer("🍽️ Анализирую новый ингредиент...")

        # Парсим вес из текста
        weight = 100  # По умолчанию
        name = user_text
        weight_match = re.search(r'(\d+)\s*г', user_text, re.IGNORECASE)
        if weight_match:
            weight = int(weight_match.group(1))
            name = re.sub(r'\d+\s*г', '', user_text, flags=re.IGNORECASE).strip()

        try:
            # ✨ Анализируем только этот ингредиент через Gemini
            model = "gemini-2.5-flash" if is_premium_active(message.from_user.id) else "gemini-2.5-flash-lite"
            gen_model = genai.GenerativeModel(model)

            prompt = f"""
            Ты — эксперт по питанию. Определи КБЖУ для продукта "{name}" в количестве {weight} г.
            Ответ строго в JSON формате:
            {{
              "name": "название продукта",
              "weight_g": {weight},
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

            # Добавляем ингредиент в блюдо
            new_item = {
                "name": data.get("name", name),
                "weight_g": weight,
                "cal": data.get("cal", 0),
                "protein": data.get("protein", 0),
                "fat": data.get("fat", 0),
                "carbs": data.get("carbs", 0)
            }

            wf["meal"]["items"].append(new_item)

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
                f"✅ Ингредиент добавлен: *{new_item['name']}* ({weight} г)\n"
                f"🔥 КБЖУ: {round(new_item['cal'])} ккал, "
                f"Б: {round(new_item['protein'])} г, "
                f"Ж: {round(new_item['fat'])} г, "
                f"У: {round(new_item['carbs'])} г",
                parse_mode="Markdown"
            )
            await show_updated_meal(message.from_user.id)

        except Exception as e:
            logging.error(f"Ошибка анализа нового ингредиента: {e}")
            await message.answer("⚠️ Не удалось проанализировать ингредиент. Попробуй снова.")
            wf["stage"] = None
        return

    # --- настройка целей питания (setup wizard) ---
    if wf and wf.get("mode") == "setup":
        lang = get_user_lang(message.from_user.id)
        step = wf.get("step", 1)
        user_input = message.text.strip()
        data = wf.get("data", {})

    try:
        if step == 1:  # RMR — обязательное
            if user_input.lower() == "calculate":
                await message.answer("⚠️ Автоматический расчёт пока недоступен. Введите RMR вручную.")
                return
            rmr = float(user_input)
            if rmr <= 0:
                raise ValueError
            data["rmr"] = rmr
            wf["step"] = 2
            await message.answer(LANG[lang]["setup_protein"], parse_mode="Markdown")

        elif step == 2:  # Protein goal — опционально
            if user_input.strip() == "":
                data["protein_goal"] = None
            else:
                protein = int(user_input)
                if protein <= 0:
                    raise ValueError
                data["protein_goal"] = protein
            wf["step"] = 3
            await message.answer(LANG[lang]["setup_carbs_min"], parse_mode="Markdown")

        elif step == 3:  # Carbs min — опционально
            if user_input.strip() == "":
                data["carbs_min"] = None
            else:
                carbs_min = int(user_input)
                if carbs_min < 0:
                    raise ValueError
                data["carbs_min"] = carbs_min
            wf["step"] = 4
            await message.answer(LANG[lang]["setup_carbs_max"], parse_mode="Markdown")

        elif step == 4:  # Carbs max — опционально
            if user_input.strip() == "":
                data["carbs_max"] = None
            else:
                carbs_max = int(user_input)
                if carbs_max < 0 or ("carbs_min" in data and data["carbs_min"] and carbs_max < data["carbs_min"]):
                    await message.answer("⚠️ Максимум должен быть ≥ минимума.")
                    return
                data["carbs_max"] = carbs_max
            wf["step"] = 5
            await message.answer(LANG[lang]["setup_fat"], parse_mode="Markdown")

        elif step == 5:  # Fat limit — опционально
            if user_input.strip() == "":
                data["fat_limit"] = None
            else:
                fat = int(user_input)
                if fat < 0:
                    raise ValueError
                data["fat_limit"] = fat
            wf["step"] = 6
            await message.answer(LANG[lang]["setup_fibre"], parse_mode="Markdown")

        elif step == 6:  # Fibre goal — опционально
            if user_input.strip() == "":
                data["fibre_goal"] = None
            else:
                fibre = int(user_input)
                if fibre < 0:
                    raise ValueError
                data["fibre_goal"] = fibre

            # Сохраняем ВСЁ в БД (None — если не задано)
            update_fields = {
                "rmr": data["rmr"],
                "protein_goal": data.get("protein_goal"),
                "carbs_min": data.get("carbs_min"),
                "carbs_max": data.get("carbs_max"),
                "fat_limit": data.get("fat_limit"),
                "fibre_goal": data.get("fibre_goal")
            }
            update_user(message.from_user.id, **update_fields)

            # Формируем сообщение о сохранении
            report_lines = [f"🔥 RMR: {data['rmr']} ккал"]
            if data.get("protein_goal"): report_lines.append(f"🍗 Белок: {data['protein_goal']} г")
            if data.get("carbs_min") or data.get("carbs_max"):
                carbs_range = f"{data.get('carbs_min', '–')}–{data.get('carbs_max', '–')}"
                report_lines.append(f"🍞 Углеводы: {carbs_range} г")
            if data.get("fat_limit"): report_lines.append(f"🥑 Жиры: до {data['fat_limit']} г")
            if data.get("fibre_goal"): report_lines.append(f"🌾 Клетчатка: {data['fibre_goal']} г")

            await message.answer(
                "✅ *Настройки сохранены!*\n📊 Ваши цели:\n" + "\n".join(report_lines),
                parse_mode="Markdown",
                reply_markup=main_menu(lang)
            )
            dp.workflow_data.pop(user_key, None)

        except (ValueError, TypeError):
            await message.answer(LANG[lang]["setup_invalid_number"])
        return

    # Если идёт ручной ввод блюда
    if wf and wf.get("mode") == "manual_input":
        dp.workflow_data[user_key]["mode"] = None
        user_text = message.text.strip()
        await message.answer("🍽️ Анализирую блюдо...")

        try:
            cache_key = f"text:{user_text.strip().lower()}"
            cached = cache_get(cache_key)

            if cached:
                # Берём уже готовый JSON из кэша
                cleaned = cached
            else:
                model = "gemini-2.5-flash" if is_premium_active(message.from_user.id) else "gemini-2.5-flash-lite"
                gen_model = genai.GenerativeModel(model)

                # 🧠 Промпт для Gemini
                prompt = f"""
                Ты — эксперт по питанию. Пользователь описал блюдо:
                "{user_text}"

                Твоя задача — разобрать блюдо на отдельные ингредиенты, оценить их вес и рассчитать КБЖУ.

                Правила:
                - Определи ингредиенты, примерный вес и рассчитай КБЖУ.
                - Не выдумывай продукты, которых явно нет.
                - Если количество указано в штуках (шт., штук, яйца, банана, кусочка и т.п.), переводи в граммы по типичным средним весам:
                  * 1 яичный белок ≈ 33 г
                  * 1 целое яйцо (среднее) ≈ 55 г
                  * 1 большое яйцо ≈ 65 г
                  * 1 банан (средний) ≈ 120 г
                  * 1 ломтик хлеба ≈ 30 г
                  * 1 кусочек твёрдого сыра ≈ 25 г
                - Если размер не указан (просто "яйцо", "банан"), используй средний вариант.
                - Всегда указывай вес каждого ингредиента в граммах в поле "weight_g".
                - Не добавляй комментариев, только JSON.

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

                # Кладём в кэш уже очищенный JSON
                cache_set(cache_key, cleaned)

            try:
                data = json.loads(cleaned)
            except Exception as e:
                logging.warning(f"⚠️ Ошибка парсинга JSON Gemini: {e}\nОтвет: {cleaned}")
                data = {"items": [], "total": {}}

            items, total = data.get("items", []), data.get("total", {})

            # Если ничего не найдено
            if not items:
                await message.answer("⚠️ Не удалось определить блюдо. Попробуй уточнить или переформулировать.")
                return

            kcal = total.get("cal", 0)
            p, f, c = total.get("protein", 0), total.get("fat", 0), total.get("carbs", 0)

            lines = []
            for i in items:
                name = i.get("name", "—")
                w = i.get("weight_g", 0)
                cal_i = i.get("cal", 0)
                pr = i.get("protein", 0)
                fat_i = i.get("fat", 0)
                carb_i = i.get("carbs", 0)
                lines.append(
                    f"- {name} ({round(w)} г) — {round(cal_i)} ккал, "
                    f"Б: {round(pr)} г  Ж: {round(fat_i)} г  У: {round(carb_i)} г"
                )

            text = "🍽️ *Анализ блюда:*\n" + "\n".join(lines)
            text += (
                f"\n\n🔥 *Итого:* {round(kcal)} ккал\n"
                f"Б: {round(p)} г  Ж: {round(f)} г  У: {round(c)} г"
            )

            builder = InlineKeyboardBuilder()
            builder.button(text="✏️ Изменить ингредиент", callback_data="edit_meal")
            builder.button(text="➕ Добавить ингредиент", callback_data="add_ingredient")
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

    # лимит фото (считаем даже если попадём в кэш — чтобы не спамили одно и то же фото)
    try:
        increment_photo(message.from_user.id)
    except Exception:
        logging.exception("Ошибка increment_photo")

    try:
        # ключ для кэша по содержимому картинки
        cache_key = "img:" + base64.b64encode(image_bytes).decode("ascii")
        cached = cache_get(cache_key)

        if cached:
            cleaned = cached
        else:
            model = "gemini-2.5-flash" if is_premium_active(message.from_user.id) else "gemini-2.5-flash-lite"
            gen_model = genai.GenerativeModel(model)

            response = await asyncio.to_thread(
                gen_model.generate_content,
                [ANALYSIS_PROMPT, {"mime_type": "image/jpeg", "data": image_bytes}]
            )

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

            # 🧹 Если Gemini вернул Markdown — чистим от ```json
            cleaned = result.replace("```json", "").replace("```", "").strip()

            # ⚙️ Если ответ не похож на JSON — пытаемся вытащить JSON из текста
            if not cleaned.startswith("{"):
                match = re.search(r"\{.*\}", cleaned, re.DOTALL)
                cleaned = match.group(0) if match else "{}"

            # Кладём в кэш уже очищенный JSON
            cache_set(cache_key, cleaned)

        try:
            data = json.loads(cleaned)
        except Exception as e:
            logging.error(f"⚠️ Ошибка парсинга JSON Gemini: {e}\nОтвет: {cleaned}")
            await message.answer("⚠️ Не удалось обработать ответ Gemini. Попробуй другое фото.")
            return

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
            name = i.get("name", "—")
            w = i.get("weight_g", 0)
            cal_i = i.get("cal", 0)
            pr = i.get("protein", 0)
            fat_i = i.get("fat", 0)
            carb_i = i.get("carbs", 0)
            text += (
                f"- {name} ({round(w)} г) — {round(cal_i)} ккал, "
                f"Б: {round(pr)} г  Ж: {round(fat_i)} г  У: {round(carb_i)} г\n"
            )

        text += (
            f"\n🔥 *Итого:* {round(kcal)} ккал\n"
            f"Б: {round(p)} г  Ж: {round(f)} г  У: {round(c)} г"
        )

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

@dp.callback_query(F.data.startswith("delete_full_meal:"))
async def delete_full_meal_callback(callback: types.CallbackQuery):
        """Удаление записи из истории/статистики по кнопке 🗑 из /history."""
        try:
            meal_id_str = callback.data.split(":", 1)[1]
            meal_id = int(meal_id_str)
        except (IndexError, ValueError):
            await callback.answer("Ошибка удаления.", show_alert=False)
            return

        ok = delete_full_meal(callback.from_user.id, meal_id)
        if ok:
            await callback.message.answer("🗑 Запись удалена из статистики и истории.")
        else:
            await callback.message.answer("⚠️ Не удалось удалить запись (возможно, она уже удалена).")

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
        name = i.get("name", "—")
        w = i.get("weight_g", 0)
        cal_i = i.get("cal", 0)
        pr = i.get("protein", 0)
        fat_i = i.get("fat", 0)
        carb_i = i.get("carbs", 0)
        text += (
            f"- {name} ({round(w)} г) — {round(cal_i)} ккал, "
            f"Б: {round(pr)} г  Ж: {round(fat_i)} г  У: {round(carb_i)} г\n"
        )

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

@dp.callback_query(F.data == "add_ingredient")
async def add_ingredient(callback: types.CallbackQuery):
    wf = dp.workflow_data.get(str(callback.from_user.id))
    if not wf or "meal" not in wf:
        await callback.message.answer("⚠️ Нет данных для добавления ингредиента. Сначала проанализируй фото или введи блюдо.")
        await callback.answer()
        return

    wf["stage"] = "await_new_ingredient"
    lang = get_user_lang(callback.from_user.id)
    await callback.message.answer(LANG[lang]["enter_new_ingredient"])
    await callback.answer()


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

    await callback.message.answer(
        "✅ Блюдо успешно добавлено в статистику за сегодня!\n\n"
        "Можешь продолжить — выбери действие из меню 👇",
        reply_markup=main_menu()
    )
    await callback.answer()

# ======================================
# 🕒 Автоматические отчёты для Premium
# ======================================

# async def send_summaries():
#     """Автоотчёты для Premium-пользователей в 21:00."""
#     while True:
#         now = datetime.now()
#         if now.hour == 21 and now.minute < 10:
#             cursor.execute("SELECT user_id FROM users WHERE is_premium=1")
#             for (uid,) in cursor.fetchall():
#                 kcal, p, f, c = get_stats(uid)
#                 if kcal > 0:
#                     await bot.send_message(
#                         uid,
#                         f"📊 *Отчёт за сегодня:*\n"
#                         f"Ккал: {round(kcal)}\n"
#                         f"Б: {round(p)} г  Ж: {round(f)} г  У: {round(c)} г",
#                         parse_mode="Markdown"
#                     )
#         await asyncio.sleep(600)

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

    logging.info("🚀 TasteBalance запущен и готов к приёму сообщений.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
