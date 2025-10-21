import asyncio
import logging
from aiohttp import web
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
import tempfile
import shutil
import zipfile
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, WebAppInfo, LabeledPrice,
    PreCheckoutQuery, FSInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from PIL import Image
from typing import Optional, List, Dict
import json

TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = "https://stories-wall-app.vercel.app/webapp.html"
ADMIN_IDS = [5155608716]

PRICE_21_PARTS = 15
# ### ИЗМЕНЕНИЕ: Добавлены цены для новых опций ###
PRICE_29_PARTS = 20 
PRICE_42_PARTS = 30
PRICE_AFTER_2_FREE = 10
PRICE_LARGE_FILE = 10
FILE_SIZE_LIMIT_MB = 4

MAX_CONCURRENT_WORKERS = 1

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

job_queue = asyncio.Queue()

DATA_FILE = Path("storieswallbot/user_data.json")
STATS_FILE = Path("storieswallbot/stats.json")
TEMP_DIR = Path("storieswallbot/temp_processing")

Path("storieswallbot").mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)


class UserData:
    def __init__(self):
        self.data = self.load_data()
        self.stats = self.load_stats()
    
    def load_data(self) -> dict:
        if DATA_FILE.exists():
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    
    def save_data(self):
        DATA_FILE.parent.mkdir(exist_ok=True)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
    
    def load_stats(self) -> dict:
        if STATS_FILE.exists():
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        # ### ИЗМЕНЕНИЕ: Добавлены ключи для статистики по 29 и 42 частям ###
        return {
            "total_users": 0,
            "total_creations": 0,
            "total_paid": 0,
            "total_stars_earned": 0,
            "by_parts": {
                "3": 0, "6": 0, "9": 0, "12": 0, "15": 0, "18": 0, "21": 0,
                "29": 0, "42": 0
            }
        }
    
    def save_stats(self):
        STATS_FILE.parent.mkdir(exist_ok=True)
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, indent=2, ensure_ascii=False)
    
    def get_user(self, user_id: int) -> dict:
        uid = str(user_id)
        if uid not in self.data:
            self.data[uid] = {
                "created_count": 0,
                "first_seen": datetime.now().isoformat(),
                "last_creation": None,
                "total_paid": 0
            }
            self.stats["total_users"] += 1
            self.save_data()
            self.save_stats()
        return self.data[uid]
    
    def increment_creations(self, user_id: int, parts: int):
        user = self.get_user(user_id)
        user["created_count"] += 1
        user["last_creation"] = datetime.now().isoformat()
        
        self.stats["total_creations"] += 1
        parts_key = str(parts)
        # Улучшенная логика: не вызовет ошибку, если ключа нет
        self.stats["by_parts"][parts_key] = self.stats["by_parts"].get(parts_key, 0) + 1
        
        self.save_data()
        self.save_stats()
    
    def increment_paid(self, user_id: int, amount: int):
        user = self.get_user(user_id)
        user["total_paid"] += amount
        
        self.stats["total_paid"] += 1
        self.stats["total_stars_earned"] += amount
        
        self.save_data()
        self.save_stats()
    
    def is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS


user_db = UserData()


class PendingCreation:
    def __init__(self):
        self.pending = {}
    
    def add(self, user_id: int, data: dict):
        self.pending[user_id] = data
        logger.info(f"Добавлено создание для пользователя {user_id}: {data}")
    
    def get(self, user_id: int) -> Optional[dict]:
        return self.pending.get(user_id)
    
    def remove(self, user_id: int, cleanup_files: bool = True):
        if user_id in self.pending:
            if cleanup_files:
                creation_data = self.pending[user_id]
                temp_dir_path = creation_data.get("temp_dir_path")
                if temp_dir_path and Path(temp_dir_path).exists():
                    shutil.rmtree(temp_dir_path, ignore_errors=True)
                    logger.info(f"Очищена временная папка: {temp_dir_path}")
            
            del self.pending[user_id]
            logger.info(f"Удалено создание для пользователя {user_id}")
    
    def has_pending(self, user_id: int) -> bool:
        return user_id in self.pending


pending_creations = PendingCreation()


class CreateStates(StatesGroup):
    waiting_image = State()
    waiting_parts_selection = State()
    waiting_fit_mode = State()
    waiting_final_confirmation = State()
    waiting_payment = State()


def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(
            text="🎨 Создать стенку",
            callback_data="start_creation"
        )],
        [InlineKeyboardButton(
            text="✨ Попробовать превью", 
            web_app=WebAppInfo(url=WEBAPP_URL)
        )],
        [
            InlineKeyboardButton(text="📖 Инструкция", callback_data="help"),
            InlineKeyboardButton(text="📊 Моя статистика", callback_data="stats")
        ],
        [InlineKeyboardButton(text="💎 Примеры", callback_data="examples")]
    ]
    
    if user_db.is_admin(user_id):
        keyboard.append([
            InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_parts_keyboard(user_id: int) -> InlineKeyboardMarkup:
    is_admin = user_db.is_admin(user_id)
    user = user_db.get_user(user_id)
    needs_payment = user["created_count"] >= 2 and not is_admin
    
    base_keyboard = [
        [
            InlineKeyboardButton(text="3 части", callback_data="parts_3"),
            InlineKeyboardButton(text="6 частей", callback_data="parts_6"),
            InlineKeyboardButton(text="9 частей", callback_data="parts_9")
        ],
        [
            InlineKeyboardButton(text="12 частей", callback_data="parts_12"),
            InlineKeyboardButton(text="15 частей", callback_data="parts_15"),
            InlineKeyboardButton(text="18 частей", callback_data="parts_18")
        ]
    ]
    
    # ### ИЗМЕНЕНИЕ: Добавлены кнопки для 21, 29 и 42 частей ###
    premium_text_21 = "21 часть"
    premium_text_29 = "29 частей"
    premium_text_42 = "42 части"
    
    if is_admin:
        pass # Admin sees simple text
    elif needs_payment:
        premium_text_21 += " 🔒"
        premium_text_29 += " 🔒"
        premium_text_42 += " 🔒"
    else: # Has free creations left, but premium is still paid
        premium_text_21 += " 🔒 (платно)"
        premium_text_29 += " 🔒 (платно)"
        premium_text_42 += " 🔒 (платно)"

    premium_keyboard = [
        [InlineKeyboardButton(text=premium_text_21, callback_data="parts_21")],
        [
            InlineKeyboardButton(text=premium_text_29, callback_data="parts_29"),
            InlineKeyboardButton(text=premium_text_42, callback_data="parts_42")
        ]
    ]

    cancel_button = [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")]]
    
    return InlineKeyboardMarkup(inline_keyboard=base_keyboard + premium_keyboard + cancel_button)


def get_fit_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✂️ Обрезать (cover)", callback_data="fit_cover")],
        [InlineKeyboardButton(text="🖼 Вписать (contain)", callback_data="fit_contain")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")]
    ])
    
def get_final_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Посмотреть превью", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="✅ Создать", callback_data="create_now")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")]
    ])


def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Полная статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
    ])


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_db.get_user(message.from_user.id)
    user = user_db.get_user(message.from_user.id)
    is_admin = user_db.is_admin(message.from_user.id)
    
    welcome_text = "👋 <b>Привет! Я StoriesWall</b>\n\n"
    
    if is_admin:
        welcome_text += "⚡️ <b>Режим администратора</b>\n"
        welcome_text += "🎁 У тебя безлимитные бесплатные создания!\n\n"
    else:
        free_left = max(0, 2 - user["created_count"])
        
        if free_left > 0:
            welcome_text += f"🎁 <b>У тебя осталось {free_left} бесплатных {'создание' if free_left == 1 else 'создания'}!</b>\n\n"
        else:
            # ### ИЗМЕНЕНИЕ: Обновлен текст с ценами ###
            welcome_text += (
                "💫 <b>Цены:</b>\n"
                f"• Стандартная стенка: {PRICE_AFTER_2_FREE} ⭐️\n"
                f"• Расширенная (21): +{PRICE_21_PARTS} ⭐️\n"
                f"• Большая (29): +{PRICE_29_PARTS} ⭐️\n"
                f"• Гигантская (42): +{PRICE_42_PARTS} ⭐️\n\n"
            )
    
    welcome_text += "Я помогу создать крутую стенку из сторис!\n\n"
    welcome_text += "Нажми кнопку ниже, чтобы начать! 👇"
    
    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "start_creation")
async def start_creation(callback: CallbackQuery, state: FSMContext):
    if pending_creations.has_pending(callback.from_user.id):
        await callback.message.edit_text(
            "⚠️ <b>У тебя уже есть незавершенное создание!</b>\n\n"
            "Хочешь отменить предыдущее и начать новое?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, начать новое", callback_data="cancel_and_start")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "📤 <b>Загрузи изображение</b>\n\n"
        "Отправь мне картинку, которую хочешь превратить в стенку.\n\n"
        "💡 <b>Совет:</b> Загружай фото без сжатия (как файл) для лучшего качества!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(CreateStates.waiting_image)
    await callback.answer()


@router.callback_query(F.data == "cancel_and_start")
async def cancel_and_start(callback: CallbackQuery, state: FSMContext):
    pending_creations.remove(callback.from_user.id)
    await state.clear()
    await start_creation(callback, state)


@router.callback_query(F.data == "cancel_creation")
async def cancel_creation(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    pending_creations.remove(callback.from_user.id)
    await back_to_main(callback)

@router.message(CreateStates.waiting_image, F.photo | F.document)
async def handle_image(message: Message, state: FSMContext):
    try:
        if message.photo:
            file_obj = message.photo[-1]
        elif message.document and message.document.mime_type.startswith('image/'):
            file_obj = message.document
        else:
            await message.answer("❌ Это не изображение. Попробуй снова.")
            return

        file = await bot.get_file(file_obj.file_id)
        file_size_mb = file.file_size / (1024 * 1024)
        logger.info(f"File size: {file_size_mb:.2f} MB for user {message.from_user.id}")
        
        temp_user_dir = TEMP_DIR / f"user_{message.from_user.id}_{uuid.uuid4()}"
        temp_user_dir.mkdir(exist_ok=True)
        image_path = temp_user_dir / "source_image.jpg"

        await bot.download_file(file.file_path, destination=image_path)
        
        try:
            with Image.open(image_path) as image:
                image_size = image.size
            logger.info(f"Изображение сохранено: {image_path}, размер {image_size}")
        except Exception as e:
            logger.error(f"Ошибка открытия сохраненного изображения: {e}")
            await message.answer("❌ Не удалось обработать изображение. Попробуй другое.")
            shutil.rmtree(temp_user_dir, ignore_errors=True)
            return

        is_large_file = file_size_mb > FILE_SIZE_LIMIT_MB
        is_admin = user_db.is_admin(message.from_user.id)
        
        pending_creations.add(message.from_user.id, {
            "temp_dir_path": str(temp_user_dir),
            "image_path": str(image_path),
            "image_size": image_size,
            "file_size_mb": file_size_mb,
            "is_large_file": is_large_file and not is_admin
        })
        
        user = user_db.get_user(message.from_user.id)
        info_text = "✅ <b>Изображение получено!</b>\n\n"
        info_text += f"📐 Размер: {image_size[0]}x{image_size[1]}\n"
        info_text += f"📦 Размер файла: {file_size_mb:.2f} МБ\n\n"
        
        if is_large_file and not is_admin:
            info_text += f"⚠️ <b>Файл больше {FILE_SIZE_LIMIT_MB} МБ</b>\n"
            info_text += f"Дополнительная плата: {PRICE_LARGE_FILE} ⭐️\n\n"
        
        if is_admin:
            info_text += "⚡️ Все варианты доступны бесплатно\n\n"
        else:
            free_left = max(0, 2 - user["created_count"])
            if free_left > 0:
                info_text += f"🎁 Бесплатных созданий: {free_left}\n"
                if is_large_file:
                    info_text += f"💰 С большим файлом: {PRICE_LARGE_FILE} ⭐️\n"
                info_text += "\n"
            else:
                total_base = PRICE_AFTER_2_FREE + (PRICE_LARGE_FILE if is_large_file else 0)
                info_text += f"💰 Создание: {total_base} ⭐️\n"
                info_text += f"💎 21 часть: +{PRICE_21_PARTS} ⭐️\n"
                info_text += f"💎 29 частей: +{PRICE_29_PARTS} ⭐️\n"
                info_text += f"💎 42 части: +{PRICE_42_PARTS} ⭐️\n\n"

        info_text += "Выбери количество частей:"
        
        await message.answer(
            info_text,
            reply_markup=get_parts_keyboard(message.from_user.id),
            parse_mode="HTML"
        )
        await state.set_state(CreateStates.waiting_parts_selection)
    
    except Exception as e:
        logger.error(f"Ошибка обработки изображения: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуй снова.")


@router.callback_query(CreateStates.waiting_parts_selection, F.data.startswith("parts_"))
async def handle_parts_selection(callback: CallbackQuery, state: FSMContext):
    parts = int(callback.data.split("_")[1])
    
    creation_data = pending_creations.get(callback.from_user.id)
    if not creation_data:
        await callback.answer("❌ Данные не найдены. Начни заново.", show_alert=True)
        await state.clear()
        return
    
    creation_data["parts"] = parts
    pending_creations.add(callback.from_user.id, creation_data)
    
    await callback.message.edit_text(
        "⚙️ <b>Выбери режим вписывания</b>\n\n"
        "✂️ <b>Обрезать (cover)</b> — изображение заполнит всё пространство, лишнее обрежется.\n"
        "<i>Идеально для большинства фото.</i>\n\n"
        "🖼 <b>Вписать (contain)</b> — изображение будет видно целиком, по бокам могут быть чёрные поля.\n"
        "<i>Идеально для вертикальных фото.</i>",
        reply_markup=get_fit_mode_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(CreateStates.waiting_fit_mode)
    await callback.answer()


@router.callback_query(CreateStates.waiting_fit_mode, F.data.startswith("fit_"))
async def handle_fit_mode_selection(callback: CallbackQuery, state: FSMContext):
    fit_mode = callback.data.split("_")[1]
    
    creation_data = pending_creations.get(callback.from_user.id)
    if not creation_data:
        await callback.answer("❌ Данные не найдены. Начни заново.", show_alert=True)
        await state.clear()
        return
    
    creation_data["fit_mode"] = fit_mode
    pending_creations.add(callback.from_user.id, creation_data)
    
    parts = creation_data['parts']
    mode_text = "Обрезать" if fit_mode == "cover" else "Вписать"
    
    await callback.message.edit_text(
        f"👍 <b>Отлично!</b>\n\n"
        f"<b>Твой выбор:</b>\n"
        f"• {parts} частей\n"
        f"• Режим: «{mode_text}»\n\n"
        f"💡 <b>Совет:</b> перед созданием можно посмотреть, как стенка будет выглядеть в профиле. "
        f"Для этого нажми «Посмотреть превью» и загрузи ту же картинку.",
        reply_markup=get_final_confirmation_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(CreateStates.waiting_final_confirmation)
    await callback.answer()

async def add_to_queue(user_id: int, state: FSMContext, is_paid: bool):
    creation_data = pending_creations.get(user_id)
    if not creation_data:
        await bot.send_message(user_id, "❌ Данные для создания не найдены. Начните заново.")
        return

    queue_pos = job_queue.qsize() + 1
    
    progress_msg = await bot.send_message(
        user_id,
        f"✅ <b>Заказ принят!</b>\n\n"
        f"Вы в очереди на <b>{queue_pos}-м</b> месте.\n\n"
        "Я пришлю результат, как только он будет готов. "
        "Это может занять несколько минут в часы пик.",
        parse_mode="HTML"
    )

    job = {
        "user_id": user_id,
        "creation_data": creation_data,
        "progress_msg_id": progress_msg.message_id,
        "is_paid": is_paid
    }
    await job_queue.put(job)
    
    pending_creations.remove(user_id, cleanup_files=False)
    
    await state.clear()

@router.callback_query(CreateStates.waiting_final_confirmation, F.data == "create_now")
async def handle_create_now(callback: CallbackQuery, state: FSMContext):
    creation_data = pending_creations.get(callback.from_user.id)
    if not creation_data:
        await callback.answer("❌ Данные не найдены. Начни заново.", show_alert=True)
        await state.clear()
        return

    parts = creation_data['parts']
    is_large_file = creation_data.get('is_large_file', False)
    user = user_db.get_user(callback.from_user.id)
    is_admin = user_db.is_admin(callback.from_user.id)
    
    needs_payment = user["created_count"] >= 2 and not is_admin
    needs_large_file_payment = is_large_file and not is_admin
    
    # ### ИЗМЕНЕНИЕ: Добавлена логика для 21, 29 и 42 частей ###
    needs_premium_payment = parts in [21, 29, 42] and not is_admin
    
    total_price = 0
    if needs_payment:
        total_price += PRICE_AFTER_2_FREE
    if needs_large_file_payment:
        total_price += PRICE_LARGE_FILE
    
    premium_price = 0
    if needs_premium_payment:
        if parts == 21:
            premium_price = PRICE_21_PARTS
        elif parts == 29:
            premium_price = PRICE_29_PARTS
        elif parts == 42:
            premium_price = PRICE_42_PARTS
    total_price += premium_price

    if total_price > 0:
        price_text = f"💫 <b>Стенка из {parts} частей</b>\n\n"
        price_text += "<b>Стоимость:</b>\n"
        if needs_payment:
            price_text += f"• Создание: {PRICE_AFTER_2_FREE} ⭐️\n"
        if premium_price > 0:
            price_text += f"• Расширенная ({parts}ч): {premium_price} ⭐️\n"
        if needs_large_file_payment:
            price_text += f"• Большой файл (>{FILE_SIZE_LIMIT_MB}МБ): {PRICE_LARGE_FILE} ⭐️\n"
        price_text += f"\n<b>Итого: {total_price} ⭐️</b>"
        
        await callback.message.edit_text(
            price_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"💳 Оплатить {total_price} ⭐️",
                    callback_data=f"pay_{total_price}"
                )],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")]
            ])
        )
        await state.set_state(CreateStates.waiting_payment)
    else:
        await callback.message.delete()
        await add_to_queue(callback.from_user.id, state, is_paid=False)

    await callback.answer()

@router.callback_query(F.data.startswith("pay_"))
async def process_payment_request(callback: CallbackQuery):
    try:
        price = int(callback.data.split("_")[1])
        creation_data = pending_creations.get(callback.from_user.id)
        if not creation_data:
            await callback.answer("❌ Данные не найдены", show_alert=True)
            return
        
        parts = creation_data.get("parts", 9)
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"Стенка из {parts} частей",
            description="Создание стенки из сторис для профиля",
            payload=f"storieswall_{callback.from_user.id}_{price}_{parts}",
            provider_token="", # УКАЖИ ТОКЕН ПРОВАЙДЕРА!
            currency="XTR",
            prices=[LabeledPrice(label="Создание стенки", amount=price)]
        )
        await callback.message.edit_text(
            "💫 Счёт отправлен!\n\nПосле оплаты стенка будет создана автоматически.",
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Payment error: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при создании счёта", show_alert=True)

@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    try:
        amount = message.successful_payment.total_amount
        logger.info(f"Успешная оплата от {message.from_user.id}: {amount} звёзд")
        user_db.increment_paid(message.from_user.id, amount)
        
        await add_to_queue(message.from_user.id, state, is_paid=True)
    except Exception as e:
        logger.error(f"Error after payment: {e}", exc_info=True)
        await message.answer("❌ Ошибка после оплаты. Свяжись с поддержкой: @AlliSighs")


async def update_progress(user_id: int, message_id: int, parts: int, current: int):
    try:
        percent = int((current / parts) * 100)
        filled = int(percent / 20)
        bar = "🟦" * filled + "⬜️" * (5 - filled)
        text = (
            f"⏳ Ваша стенка из {parts} частей в работе...\n\n"
            f"{bar} {percent}%\n"
            f"Обработано: {current}/{parts}"
        )
        await bot.edit_message_text(text, chat_id=user_id, message_id=message_id, parse_mode="HTML")
    except Exception:
        pass

def heavy_processing_task(image_path: str, parts: int, fit_mode: str, output_dir: Path) -> List[Path]:
    """Синхронная, ресурсоемкая задача по нарезке изображения."""
    PIECE_WIDTH, PIECE_HEIGHT = 1080, 1342
    TARGET_WIDTH, TARGET_HEIGHT = 1080, 1920
    GRID_COLS = 3
    # ### ВАЖНОЕ ИСПРАВЛЕНИЕ: Правильный расчет кол-ва рядов для чисел, не кратных 3 ###
    GRID_ROWS = (parts + GRID_COLS - 1) // GRID_COLS # Это эквивалент math.ceil(parts / GRID_COLS)
    
    total_content_width = PIECE_WIDTH * GRID_COLS
    total_content_height = PIECE_HEIGHT * GRID_ROWS
    
    with Image.open(image_path) as image:
        source_canvas = Image.new('RGB', (total_content_width, total_content_height), (0, 0, 0))
        img_aspect = image.width / image.height
        content_aspect = total_content_width / total_content_height

        if fit_mode == 'cover':
            new_height = total_content_height if img_aspect <= content_aspect else int(total_content_width / img_aspect)
            new_width = total_content_width if img_aspect > content_aspect else int(total_content_height * img_aspect)
        else: # contain
            new_height = total_content_height if img_aspect > content_aspect else int(total_content_width / img_aspect)
            new_width = total_content_width if img_aspect <= content_aspect else int(total_content_height * img_aspect)

        resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        offset_x = (total_content_width - new_width) // 2
        offset_y = (total_content_height - new_height) // 2
        source_canvas.paste(resized, (offset_x, offset_y))
    
    output_files = []
    # Цикл по фактическому числу частей, а не по рядам/колонкам
    for i in range(parts):
        row, col = i // GRID_COLS, i % GRID_COLS
        sx, sy = col * PIECE_WIDTH, row * PIECE_HEIGHT
        piece = source_canvas.crop((sx, sy, sx + PIECE_WIDTH, sy + PIECE_HEIGHT))
        
        output = Image.new('RGB', (TARGET_WIDTH, TARGET_HEIGHT), (0, 0, 0))
        dx, dy = (TARGET_WIDTH - PIECE_WIDTH) // 2, (TARGET_HEIGHT - PIECE_HEIGHT) // 2
        output.paste(piece, (dx, dy))
        
        part_path = output_dir / f"story_{i + 1:02d}.png"
        output.save(part_path, format='PNG', optimize=True)
        output_files.append(part_path)
    
    return output_files


async def processing_worker(queue: asyncio.Queue):
    while True:
        job = await queue.get()
        user_id = job["user_id"]
        progress_msg_id = job["progress_msg_id"]
        creation_data = job["creation_data"]
        temp_dir_path = Path(creation_data["temp_dir_path"])
        
        try:
            logger.info(f"Начинаю обработку для {user_id}. Задач в очереди: {queue.qsize()}")
            
            parts = creation_data["parts"]
            fit_mode = creation_data["fit_mode"]
            image_path = creation_data["image_path"]
            
            output_dir = temp_dir_path / "output"
            output_dir.mkdir()
            
            await update_progress(user_id, progress_msg_id, parts, 0)
            
            file_paths = await asyncio.to_thread(
                heavy_processing_task, image_path, parts, fit_mode, output_dir
            )
            
            await update_progress(user_id, progress_msg_id, parts, parts)
            await asyncio.sleep(0.5)
            
            await bot.edit_message_text("📦 Упаковка файлов...", chat_id=user_id, message_id=progress_msg_id)
            zip_path = temp_dir_path / f"storieswall_{parts}parts.zip"
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in file_paths:
                    zipf.write(file_path, arcname=file_path.name)
            
            await bot.edit_message_text("📤 Отправка файла...", chat_id=user_id, message_id=progress_msg_id)
            zip_file = FSInputFile(zip_path)
            await bot.send_document(
                user_id,
                zip_file,
                caption=(
                    f"✅ <b>Готово!</b>\n\n"
                    f"Твоя стенка из {parts} частей готова! 🎨\n\n"
                    f"📌 <b>Важно:</b> Публикуй сторис <b>в обратном порядке</b> "
                    f"(загружай в профиль начиная с последней картинки)!\n\n"
                    f"Удачи! 🚀"
                ),
                parse_mode="HTML"
            )
            
            user_db.increment_creations(user_id, parts)
            
            await bot.delete_message(user_id, progress_msg_id)
            await bot.send_message(
                user_id,
                "Создать ещё одну стенку? 😊",
                reply_markup=get_main_keyboard(user_id)
            )

        except Exception as e:
            logger.error(f"Ошибка в воркере для {user_id}: {e}", exc_info=True)
            await bot.send_message(
                user_id,
                "❌ Произошла ошибка при создании.\nПопробуй снова или свяжись с @AlliSighs"
            )
        finally:
            shutil.rmtree(temp_dir_path, ignore_errors=True)
            logger.info(f"Очищена папка {temp_dir_path} для {user_id}")
            queue.task_done()

# ... (остальной код остается без изменений) ...

@router.callback_query(F.data == "stats")
async def show_stats_callback(callback: CallbackQuery):
    user = user_db.get_user(callback.from_user.id)
    stats = user_db.stats
    
    user_text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"🎨 Создано стенок: <b>{user['created_count']}</b>\n"
        f"💰 Потрачено звёзд: <b>{user['total_paid']}</b> ⭐️\n"
    )
    
    if user['last_creation']:
        last_date = datetime.fromisoformat(user['last_creation'])
        user_text += f"📅 Последнее: {last_date.strftime('%d.%m.%Y %H:%M')}\n"
    
    user_text += (
        f"\n<b>Общая статистика:</b>\n"
        f"👥 Пользователей: {stats['total_users']}\n"
        f"🎨 Всего создано: {stats['total_creations']}\n"
    )
    
    await callback.message.edit_text(
        user_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    help_text = (
        "📖 <b>Как создать стенку сторис</b>\n\n"
        "1️⃣ Нажми «Создать стенку»\n"
        "2️⃣ Отправь картинку боту (лучше файлом!)\n"
        "3️⃣ Выбери количество частей (3-42)\n"
        "4️⃣ Выбери режим обрезки\n"
        "5️⃣ Оплати, если нужно\n"
        "6️⃣ Получи архив с частями\n"
        "7️⃣ Публикуй в профиль <b>В ОБРАТНОМ ПОРЯДКЕ</b> ⬆️\n\n"
        "✨ <b>ВАЖНО:</b> Загружай картинки в профиль начиная с последней!\n"
        "Например, если у тебя 9 частей, начни с story_09.png, потом story_08.png и так далее.\n\n"
        "💡 <b>Совет:</b> Отправляй фото без сжатия (как файл) для максимального качества!"
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
        "💎 <b>Крутые примеры стенок:</b>\n\n"
        "✨ @AlliSighs — developer\n"
        "✨ @awlxa\n"
        "✨ @monaki\n"
        "✨ @detochka\n"
        "✨ @mcduck\n"
        "✨ @alexzackerman\n\n"
        "Вдохновляйся и создавай свою! 🎨"
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


@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    user = user_db.get_user(callback.from_user.id)
    is_admin = user_db.is_admin(callback.from_user.id)
    
    welcome_text = "👋 <b>StoriesWall</b>\n\n"
    
    if is_admin:
        welcome_text += "⚡️ <b>Режим администратора</b>\n"
        welcome_text += "🎁 Безлимитные бесплатные создания!\n\n"
    else:
        free_left = max(0, 2 - user["created_count"])
        
        if free_left > 0:
            welcome_text += f"🎁 Бесплатных созданий: {free_left}\n\n"
        else:
            welcome_text += (
                "💫 Цены:\n"
                f"• Стандартная: {PRICE_AFTER_2_FREE} ⭐️\n"
                f"• Расширенная (21): +{PRICE_21_PARTS} ⭐️\n"
                f"• Большая (29): +{PRICE_29_PARTS} ⭐️\n"
                f"• Гигантская (42): +{PRICE_42_PARTS} ⭐️\n\n"
            )
    
    welcome_text += "Создавай крутые стенки! 🎨"
    
    await callback.message.edit_text(
        welcome_text,
        reply_markup=get_main_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not user_db.is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    
    await callback.message.edit_text(
        "⚙️ <b>Админ-панель</b>\n\nВыбери действие:",
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
    
    parts_stats = "\n".join([
        f"  • {parts} частей: {count} раз"
        for parts, count in sorted(stats["by_parts"].items(), key=lambda x: int(x[0]))
        if count > 0
    ])
    
    stats_text = (
        "📊 <b>Полная статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"🎨 Всего создано: <b>{stats['total_creations']}</b>\n"
        f"💰 Платных операций: <b>{stats['total_paid']}</b>\n"
        f"⭐️ Заработано звёзд: <b>{stats['total_stars_earned']}</b>\n\n"
        f"<b>Популярность размеров:</b>\n{parts_stats}\n\n"
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


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>Как создать стенку сторис</b>\n\n"
        "1️⃣ Нажми «Создать стенку»\n"
        "2️⃣ Отправь картинку боту (лучше файлом!)\n"
        "3️⃣ Выбери количество частей (3-42)\n"
        "4️⃣ Оплати, если нужно\n"
        "5️⃣ Получи архив с частями\n"
        "6️⃣ Публикуй в профиль <b>В ОБРАТНОМ ПОРЯДКЕ</b> ⬆️\n\n"
        "✨ <b>ВАЖНО:</b> Загружай картинки в профиль начиная с последней!\n\n"
        "💡 <b>Совет:</b> Отправляй фото без сжатия для лучшего качества!"
    )
    
    await message.answer(help_text, parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user = user_db.get_user(message.from_user.id)
    stats = user_db.stats
    
    user_text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"🎨 Создано стенок: <b>{user['created_count']}</b>\n"
        f"💰 Потрачено звёзд: <b>{user['total_paid']}</b> ⭐️\n"
    )
    
    if user['last_creation']:
        last_date = datetime.fromisoformat(user['last_creation'])
        user_text += f"📅 Последнее: {last_date.strftime('%d.%m.%Y %H:%M')}\n"
    
    user_text += (
        f"\n<b>Общая статистика:</b>\n"
        f"👥 Пользователей: {stats['total_users']}\n"
        f"🎨 Всего создано: {stats['total_creations']}\n"
    )
    
    await message.answer(user_text, parse_mode="HTML")

async def main():
    dp.include_router(router)
    
    await start_web_server()
    
    workers = [
        asyncio.create_task(processing_worker(job_queue))
        for _ in range(MAX_CONCURRENT_WORKERS)
    ]
    
    logger.info(f"🚀 StoriesWall Bot started with {MAX_CONCURRENT_WORKERS} worker(s)!")
    logger.info(f"📊 Users: {user_db.stats['total_users']}")
    logger.info(f"🎨 Creations: {user_db.stats['total_creations']}")
    logger.info(f"⚡️ Admins: {ADMIN_IDS}")
    
    await dp.start_polling(bot)
    
    await job_queue.join()
    for worker in workers:
        worker.cancel()



async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_get('/', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Web server started on port {port}")


if __name__ == "__main__":
    if TEMP_DIR.exists():
        for item in TEMP_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
    
    asyncio.run(main())
