import asyncio
import logging
import os
import json
from datetime import datetime
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from aiogram.fsm.storage.memory import MemoryStorage

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.environ.get("BOT_TOKEN") 
WEBSITE_URL = "https://stories-wall-app.vercel.app/"  # Ссылка на твой сайт
ADMIN_IDS = [5155608716] # ID администраторов

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

DATA_FILE = Path("storieswallbot/user_data.json")
STATS_FILE = Path("storieswallbot/stats.json")

Path("storieswallbot").mkdir(exist_ok=True)

# --- УПРОЩЕННАЯ БАЗА ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ ---
class UserData:
    def __init__(self):
        self.stats = self.load_stats()
    
    def load_stats(self) -> dict:
        if STATS_FILE.exists():
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"total_users": 0, "known_users": []}
    
    def save_stats(self):
        STATS_FILE.parent.mkdir(exist_ok=True)
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, indent=2, ensure_ascii=False)
    
    def register_user(self, user_id: int):
        """Регистрирует нового пользователя, если его еще нет."""
        if user_id not in self.stats["known_users"]:
            self.stats["total_users"] += 1
            self.stats["known_users"].append(user_id)
            self.save_stats()
            logger.info(f"New user registered: {user_id}. Total: {self.stats['total_users']}")

    def is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS

user_db = UserData()


# --- КЛАВИАТУРЫ ---

def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Главная клавиатура со ссылкой на сайт."""
    keyboard = [
        [InlineKeyboardButton(
            text="✨ Создать стенку на сайте",
            url=WEBSITE_URL
        )],
        [
            InlineKeyboardButton(text="📖 Инструкция", callback_data="help"),
            InlineKeyboardButton(text="💎 Примеры", callback_data="examples")
        ]
    ]
    
    if user_db.is_admin(user_id):
        keyboard.append([
            InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
    ])


# --- ОБРАБОТЧИКИ КОМАНД И КОЛЛБЭКОВ ---

@router.message(CommandStart())
@router.callback_query(F.data == "back_to_main")
async def show_start_menu(event: Message | CallbackQuery):
    """Отображает главное меню."""
    user_db.register_user(event.from_user.id)
    
    text = (
        "👋 <b>Привет! Я бот-помощник для StoriesWall.</b>\n\n"
        "Мы перенесли весь функционал на наш сайт, чтобы сделать процесс создания стенки "
        "быстрым, бесплатным и безлимитным для всех!\n\n"
        "На сайте ты сможешь:\n"
        "✅ Загрузить любое изображение\n"
        "✅ Увидеть предпросмотр в реальном времени\n"
        "✅ Мгновенно скачать готовые части\n\n"
        "Нажми на кнопку ниже, чтобы начать! 👇"
    )

    reply_markup = get_main_keyboard(event.from_user.id)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    help_text = (
        "📖 <b>Инструкция по созданию стенки на сайте</b>\n\n"
        "1️⃣ Перейди на наш сайт по кнопке в главном меню.\n"
        "2️⃣ Нажми на область загрузки и выбери свою картинку.\n"
        "3️⃣ Настрой количество частей и режим обрезки.\n"
        "4️⃣ Увидишь готовый предпросмотр своей стенки.\n"
        "5️⃣ Нажми кнопку «Скачать» — ты получишь ZIP-архив.\n"
        "6️⃣ Распакуй архив и публикуй сторис в профиль <b>В ОБРАТНОМ ПОРЯДКЕ!</b>\n\n"
        "✨ <b>ВАЖНО:</b> Начинай загрузку с последней картинки (например, `story_09.png`), "
        "чтобы в профиле они выстроились правильно."
    )
    
    await callback.message.edit_text(
        help_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "examples")
async def show_examples(callback: CallbackQuery):
    examples_text = (
        "💎 <b>Примеры готовых стенок:</b>\n\n"
        "Ты можешь посмотреть, как выглядят крутые стенки в профилях у этих ребят:\n\n"
        "• @AlliSighs\n"
        "• @awlxa\n"
        "• @monaki\n"
        "• @detochka\n\n"
        "Вдохновляйся и создавай свою на нашем сайте! 🎨"
    )
    
    await callback.message.edit_text(
        examples_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()

# --- АДМИН-ПАНЕЛЬ ---
@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not user_db.is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    
    await callback.message.edit_text(
        "⚙️ <b>Админ-панель</b>\n\nДобро пожаловать!",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "admin_stats")
async def show_admin_stats(callback: CallbackQuery):
    if not user_db.is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    
    stats = user_db.stats
    stats_text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего уникальных пользователей: <b>{stats['total_users']}</b>\n\n"
        f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    
    await callback.message.edit_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

# --- ОБРАБОТКА ЛЮБОГО ДРУГОГО КОНТЕНТА ---
@router.message()
async def handle_other_messages(message: Message):
    """Перенаправляет пользователя в главное меню при отправке чего угодно."""
    await message.answer(
        "Пожалуйста, используй кнопки для навигации. "
        "Чтобы создать стенку, переходи на наш сайт 👇",
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

# --- ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---
async def health_check(request):
    """Эндпоинт для проверки, что бот жив."""
    return web.Response(text="Bot is running!")

async def start_web_server():
    """Запускает простой веб-сервер для health-чеков хостинга."""
    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_get('/', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Web server started on port {port}")

async def main():
    dp.include_router(router)
    
    # Запускаем веб-сервер для хостинга
    await start_web_server()
    
    logger.info("🚀 StoriesWall Bot (Gateway mode) started!")
    logger.info(f"📊 Initial users: {user_db.stats['total_users']}")
    logger.info(f"⚡️ Admins: {ADMIN_IDS}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
