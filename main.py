import asyncio
import logging
import os
import psycopg2
from datetime import datetime
from threading import Thread

from flask import Flask # Используем для того, чтобы Render не выключал бота
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, 
    InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
)
from openai import AsyncOpenAI
from dotenv import load_dotenv

# --- ИНИЦИАЛИЗАЦИЯ ---
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
ZVENO_API_KEY = os.getenv("ZVENO_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USERNAME = "@mellfreezy"

client = AsyncOpenAI(base_url="https://api.zveno.ai/v1", api_key=ZVENO_API_KEY)
logging.basicConfig(level=logging.INFO)

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (чтобы не засыпал) ---
app = Flask('')

@app.route('/')
def home():
    return "LolipopAI is running!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- БАЗА ДАННЫХ ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        requests_left INTEGER DEFAULT 50,
        total_sent INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        last_reset_month INTEGER,
        referrer_id BIGINT
    )''')
    conn.commit()
    cur.close(); conn.close()

def get_user(user_id, username=None, referrer_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()
    current_month = datetime.now().month
    if not user:
        cur.execute(
            "INSERT INTO users (user_id, username, requests_left, last_reset_month, referrer_id) VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (user_id, username, 50, current_month, referrer_id)
        )
        conn.commit(); user = cur.fetchone()
    elif user[5] != current_month:
        cur.execute("UPDATE users SET requests_left = 50, last_reset_month = %s WHERE user_id = %s", (current_month, user_id))
        conn.commit(); user = list(user); user[2] = 50
    cur.close(); conn.close()
    return user

# --- ЛОГИКА БОТА ---
class ChatState(StatesGroup):
    waiting_for_prompt = State()

def get_main_kb(user_id, username):
    buttons = [
        [KeyboardButton(text="🍭 Отправить запрос")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💳 Пополнить")]
    ]
    if f"@{username}" == ADMIN_USERNAME:
        buttons.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    ref_id = int(command.args) if (command.args and command.args.isdigit()) else None
    get_user(message.from_user.id, message.from_user.username, ref_id)
    await message.answer("✨ **LolipopAI запущена!**", parse_mode="Markdown", reply_markup=get_main_kb(message.from_user.id, message.from_user.username))

@dp.message(F.text == "👤 Профиль")
async def profile(message: types.Message):
    user = get_user(message.from_user.id, message.from_user.username)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"👤 **Твой профиль**\n\n🍭 Осталось: **{user[2]}**\n📊 Всего отправлено: **{user[3]}**\n\n"
        f"👥 **Рефералка (10% бонус):**\n`{ref_link}`", parse_mode="Markdown"
    )

@dp.message(F.text == "🍭 Отправить запрос")
async def start_chat(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id, message.from_user.username)
    if user[2] <= 0: return await message.answer("❌ Запросы закончились.")
    await message.answer("💬 Введите запрос (или нажми ❌ Отмена):", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True))
    await state.set_state(ChatState.waiting_for_prompt)

@dp.message(ChatState.waiting_for_prompt)
async def handle_ai(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Меню:", reply_markup=get_main_kb(message.from_user.id, message.from_user.username))

    status_msg = await message.answer("🍭 *LolipopAI думает...*", parse_mode="Markdown")
    
    # Fallback моделей
    res = None
    for m in ["nvidia/nemotron-3-super-120b-a12b:free", "qwen/qwen3-next-80b-a3b-instruct:free"]:
        try:
            c = await client.chat.completions.create(
                model=m, messages=[{"role":"system","content":"Ты LolipopAI. Используй Markdown. Код в блоки."}, {"role":"user","content":message.text}], timeout=40
            )
            res = c.choices[0].message.content
            if res: break
        except: continue

    if res:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET requests_left = requests_left - 1, total_sent = total_sent + 1 WHERE user_id = %s", (message.from_user.id,))
        conn.commit(); cur.close(); conn.close()
        await status_msg.delete()
        await message.answer(res, parse_mode="Markdown", reply_markup=get_main_kb(message.from_user.id, message.from_user.username))
        await state.clear()
    else:
        await status_msg.edit_text("❌ Ошибка сервера ИИ.")

@dp.message(F.text == "💳 Пополнить")
async def buy_req(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="50 запр. — 50⭐️", callback_data="buy_50_50")],
        [InlineKeyboardButton(text="150 запр. — 120⭐️", callback_data="buy_150_120")]
    ])
    await message.answer("Выберите пакет:", reply_markup=kb)

@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(cb: types.CallbackQuery):
    _, amt, price = cb.data.split("_")
    await cb.bot.send_invoice(
        chat_id=cb.from_user.id, title="Пополнение", description=f"{amt} запросов",
        payload=f"pay_{amt}", currency="XTR", prices=[LabeledPrice(label="XTR", amount=int(price))],
        provider_token=""
    )

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def success_pay(message: types.Message):
    amount = int(message.successful_payment.invoice_payload.split("_")[1])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET requests_left = requests_left + %s WHERE user_id = %s RETURNING referrer_id", (amount, message.from_user.id))
    ref = cur.fetchone()
    if ref and ref[0]:
        bonus = max(1, int(amount * 0.1))
        cur.execute("UPDATE users SET requests_left = requests_left + %s WHERE user_id = %s", (bonus, ref[0]))
        try: await bot.send_message(ref[0], f"🎁 +{bonus} запросов за покупку реферала!")
        except: pass
    conn.commit(); cur.close(); conn.close()
    await message.answer(f"✅ Добавлено {amount} запросов!")

async def main():
    init_db()
    # Запускаем Flask в отдельном потоке
    Thread(target=run_flask).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
