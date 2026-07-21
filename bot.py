# -*- coding: utf-8 -*-
"""
Reaksiya Bot — Telegram kanal/guruh reaksiya boti
aiogram 3.x | Webhook | Render.com uchun tayyor

Konfiguratsiya alohida DB o'rniga maxsus "storage" guruhida saqlanadi:
bot o'sha guruhda admin bo'ladi va har bir o'zgarishda pinned xabarni yangilaydi.

--- BU VERSIYADAGI TUZATISH VA YANGI FUNKSIYALAR ---

Ma'lumot yo'qotilishidan himoya:
1) ConfigStorage.load(): tarmoq xatosida konfiguratsiya HECH QACHON yo'qotilmaydi —
   3 marta qayta uriniladi, xotiradagi eski ma'lumot har doim ustuvor.
2) Pinned xabar "begonalashtirilsa" — JSON tuzilishi orqali aniqlanadi va bot
   o'z konfiguratsiyasini avtomatik qayta pin qiladi (self-healing).
3) 4096 belgi chegarasi oldindan tekshiriladi — chegaradan oshsa saqlash BEKOR
   qilinadi (eski ma'lumot buzilmaydi) va SUPERADMIN ogohlantiriladi.
4) Broadcast paytida botni bloklagan foydalanuvchilar avtomatik tozalanadi.

Bug tuzatish:
5) MAJBURIY KANAL/GURUH endi to'g'ri ishlaydi: forward qilingan xabar, @username,
   to'liq havola (https://t.me/...) va raqamli ID — barchasi qo'llab-quvvatlanadi.
   Guruhlar ham (nafaqat kanallar) endi to'liq qo'llab-quvvatlanadi.
6) is_subscribed() doimiy xato bersa (masalan bot admin emas) — SUPERADMIN
   bir martalik ogohlantirish oladi (spam qilmaydi).
7) FSM holati server qayta ishga tushganda tozalanib qolsa, admin adashib
   qolmaydi — bot avtomatik asosiy menyuga qaytaradi.

Yangi funksiyalar:
8) Foydalanuvchi botga yozgan har qanday xabari adminga yetkaziladi; admin shu
   xabarga Reply qilib to'g'ridan-to'g'ri javob qaytara oladi.
9) Admin panelida "Foydalanuvchilar" tugmasi — ro'yxatni .txt fayl sifatida oladi.
10) Kanalni ro'yxatdan butunlay o'chirish imkoniyati.
11) UX: barcha kiritish so'rovlarida aniq misollar va doimiy "Orqaga" tugmasi.
"""

import asyncio
import json
import logging
import os
import random
from copy import deepcopy

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ----------------------------------------------------------------------------
# SOZLAMALAR (ENV o'zgaruvchilar)
# ----------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPERADMIN_ID = int(os.environ["SUPERADMIN_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
STORAGE_CHAT_ID = int(os.environ["STORAGE_CHAT_ID"])  # bot admin bo'lgan maxfiy guruh
WEBHOOK_HOST = os.environ["WEBHOOK_HOST"].rstrip("/")  # masalan https://sizning-app.onrender.com

if not WEBHOOK_HOST.startswith("https://") and not WEBHOOK_HOST.startswith("http://"):
    WEBHOOK_HOST = f"https://{WEBHOOK_HOST}"

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.environ.get("PORT", 8080))

ADMIN_IDS = {SUPERADMIN_ID, ADMIN_ID}

# Telegram xabar chegarasi 4096 belgi — xavfsizlik zahirasi bilan shu chegarani qo'yamiz
CONFIG_SIZE_LIMIT = 4000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reaction_bot")

# ----------------------------------------------------------------------------
# TELEGRAMNING STANDART REAKSIYA EMOJILARI
# ----------------------------------------------------------------------------
ALL_REACTIONS = [
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂",
    "🤷", "🤷‍♀", "😡",
]

DEFAULT_CONFIG = {
    "channels": {},  # "chat_id" -> {"title": str, "general_enabled": bool, "custom_reactions": list|None}
    "general_reactions": ["👍", "❤", "🔥", "🎉", "🥰"],
    "boss_reaction": "❤",
    "start_message_user": "Assalomu alaykum! Botimizga xush kelibsiz 👋",
    "mandatory_channel": None,        # int (chat ID) yoki eski formatda "@username" (str)
    "mandatory_channel_title": None,
    "mandatory_channel_url": None,    # a'zo bo'lish tugmasi uchun havola
    "ad_text": None,
    "ad_url": None,
    "ad_button_text": "📢 Reklama",
    "pinned_message_id": None,
    "users": [],  # /start bosgan barcha foydalanuvchi id'lari (broadcast uchun)
}


def _looks_like_our_config(d) -> bool:
    """Pinned xabar chindan ham bizning konfiguratsiyamiz ekanligini
    tekshiradi (struktura orqali) — begona xabarlarni chalkashtirmaslik uchun."""
    return isinstance(d, dict) and "channels" in d and "general_reactions" in d


async def notify_superadmin(bot: Bot, text: str):
    """Kritik xatoliklar haqida SUPERADMIN'ga to'g'ridan-to'g'ri xabar beradi."""
    try:
        await bot.send_message(SUPERADMIN_ID, text)
    except Exception as e:
        logger.error(f"SUPERADMIN'ga ogohlantirish yuborib bo'lmadi: {e}")


# ----------------------------------------------------------------------------
# KONFIGURATSIYA — Telegram guruhidagi pinned xabarda saqlanadi
# ----------------------------------------------------------------------------
class ConfigStorage:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.data: dict | None = None
        self._lock = asyncio.Lock()

    async def load(self, bot: Bot) -> dict:
        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                chat = await bot.get_chat(self.chat_id)
                pinned = chat.pinned_message

                if pinned and pinned.text:
                    try:
                        loaded = json.loads(pinned.text)
                    except json.JSONDecodeError:
                        loaded = None

                    if _looks_like_our_config(loaded):
                        merged = deepcopy(DEFAULT_CONFIG)
                        merged.update(loaded)
                        self.data = merged
                        logger.info("Konfiguratsiya pinned xabardan yuklandi.")
                        return self.data

                    logger.warning(
                        "Storage guruhdagi pinned xabar bizning konfiguratsiya "
                        "emasga o'xshaydi (begona xabar yoki buzilgan JSON)."
                    )
                    if self.data is not None:
                        logger.warning(
                            "Xotiradagi oxirgi ma'lumot ISHLATILMOQDA (yo'qotilmadi), "
                            "o'z konfiguratsiyamiz qayta pin qilinmoqda."
                        )
                        self.data["pinned_message_id"] = None
                        await self.save(bot)
                        return self.data
                    self.data = deepcopy(DEFAULT_CONFIG)
                    logger.info("Standart konfiguratsiya bilan (birinchi marta) ishga tushirildi.")
                    return self.data

                if self.data is not None:
                    logger.warning(
                        "Pinned xabar topilmadi, lekin xotirada eski ma'lumot bor — "
                        "u ishlatilmoqda va qayta pin qilinmoqda."
                    )
                    self.data["pinned_message_id"] = None
                    await self.save(bot)
                    return self.data
                self.data = deepcopy(DEFAULT_CONFIG)
                logger.info("Standart konfiguratsiya bilan (birinchi marta) ishga tushirildi.")
                return self.data

            except Exception as e:
                last_error = e
                logger.warning(f"Konfiguratsiyani yuklashda xatolik (urinish {attempt}/3): {e}")
                await asyncio.sleep(2 * attempt)

        if self.data is not None:
            logger.error(
                f"Konfiguratsiyani qayta yuklab bo'lmadi ({last_error}), "
                "lekin xotiradagi oxirgi ma'lumot ishlatilmoqda — hech narsa yo'qolmadi."
            )
            return self.data

        logger.critical(
            f"Konfiguratsiyani birinchi marta yuklab bo'lmadi ({last_error}). "
            "Standart holat bilan ishga tushirilmoqda."
        )
        self.data = deepcopy(DEFAULT_CONFIG)
        return self.data

    async def get(self) -> dict:
        if self.data is None:
            raise RuntimeError("Konfiguratsiya hali yuklanmagan")
        return self.data

    async def save(self, bot: Bot):
        async with self._lock:
            text = json.dumps(self.data, ensure_ascii=False)

            if len(text) > CONFIG_SIZE_LIMIT:
                logger.error(
                    f"Konfiguratsiya hajmi chegaradan katta ({len(text)}/{CONFIG_SIZE_LIMIT} belgi). "
                    "Saqlash BEKOR qilindi — eski ma'lumot buzilmasligi uchun."
                )
                await notify_superadmin(
                    bot,
                    "⚠️ <b>DIQQAT: Konfiguratsiya hajmi chegaraga yaqinlashdi!</b>\n"
                    f"Hozirgi hajm: {len(text)} / ~4096 belgi.\n"
                    "So'nggi o'zgarish SAQLANMADI (eski sozlamalar hali ham amalda).\n"
                    "Iltimos, kanallar yoki foydalanuvchilar sonini tekshiring.",
                )
                raise RuntimeError("Konfiguratsiya hajmi chegaradan oshdi, saqlanmadi")

            pid = self.data.get("pinned_message_id")
            pin_ok = False

            if pid:
                try:
                    await bot.edit_message_text(chat_id=self.chat_id, message_id=pid, text=text)
                    try:
                        chat = await bot.get_chat(self.chat_id)
                        if chat.pinned_message and chat.pinned_message.message_id == pid:
                            pin_ok = True
                        else:
                            logger.warning(
                                "Xabar tahrirlandi, lekin endi pin qilinmagan — qayta pin qilinmoqda."
                            )
                            await bot.pin_chat_message(self.chat_id, pid, disable_notification=True)
                            pin_ok = True
                    except Exception as e:
                        logger.warning(f"Pin holatini tekshirishda xatolik: {e}")
                        pin_ok = True
                except Exception as e:
                    logger.warning(f"Pinned xabarni tahrirlab bo'lmadi, yangisi yaratiladi: {e}")
                    pid = None

            if not pin_ok:
                msg = await bot.send_message(self.chat_id, text)
                try:
                    await bot.pin_chat_message(self.chat_id, msg.message_id, disable_notification=True)
                except Exception as e:
                    logger.warning(f"Xabarni pin qilishda xatolik: {e}")
                    await notify_superadmin(
                        bot, f"⚠️ Konfiguratsiya xabarini pin qilib bo'lmadi: {e}"
                    )
                self.data["pinned_message_id"] = msg.message_id
                await bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=msg.message_id,
                    text=json.dumps(self.data, ensure_ascii=False),
                )


storage = ConfigStorage(STORAGE_CHAT_ID)
router = Router()

_mandatory_check_error_notified = False  # SUPERADMIN'ni spam qilmaslik uchun bayroq


# ----------------------------------------------------------------------------
# FSM HOLATLARI
# ----------------------------------------------------------------------------
class AdminStates(StatesGroup):
    editing_general = State()
    editing_custom = State()
    editing_boss = State()
    wait_start_message = State()
    wait_mandatory_channel = State()
    wait_ad_text = State()
    wait_ad_url = State()
    wait_broadcast = State()
    wait_direct_message = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ----------------------------------------------------------------------------
# MANZILLARNI (KANAL/GURUH) ANIQLASH UCHUN YORDAMCHI FUNKSIYALAR
# ----------------------------------------------------------------------------
def parse_chat_reference(text: str):
    """Turli formatlardagi kanal/guruh manzilini ajratib oladi:
    @username, https://t.me/username, t.me/username, -100123456789 (ID).
    Qaytaradi: str (@username) | int (ID) | "INVITE_LINK" | None."""
    text = (text or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            lowered = text.lower()
            break
    text = text.strip("/ ")
    if not text:
        return None
    if text.startswith("+") or lowered.startswith("joinchat/"):
        return "INVITE_LINK"
    if text.startswith("@"):
        return text
    stripped = text[1:] if text.startswith("-") else text
    if stripped.isdigit():
        return int(text)
    return f"@{text}"


def extract_forwarded_chat(message: Message):
    """Forward qilingan xabardan manba chatni oladi (aiogram versiyalariga mos)."""
    fc = getattr(message, "forward_from_chat", None)
    if fc:
        return fc
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        if chat:
            return chat
    return None


# ----------------------------------------------------------------------------
# KLAVIATURALAR
# ----------------------------------------------------------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🌐 Umumiy kanal reaksiyasi", callback_data="menu:general")],
        [InlineKeyboardButton(text="🎯 Maxsus kanal reaksiyasi", callback_data="menu:custom")],
        [InlineKeyboardButton(text="👑 BOSS reaksiyasi (guruhda)", callback_data="menu:boss")],
        [InlineKeyboardButton(text="💬 Start xabari (foydalanuvchilar)", callback_data="menu:startmsg")],
        [InlineKeyboardButton(text="🔒 Majburiy kanal/guruh", callback_data="menu:mandatory")],
        [InlineKeyboardButton(text="📢 Reklama", callback_data="menu:ad")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="menu:users")],
        [InlineKeyboardButton(text="📣 Xabar yuborish (hammaga)", callback_data="menu:broadcast")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb(target: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"back:{target}")]]
    )


def reactions_grid_kb(selected: list[str], prefix: str, per_row: int = 6, extra_rows=None) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, emoji in enumerate(ALL_REACTIONS, 1):
        label = f"✅{emoji}" if emoji in selected else emoji
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{ALL_REACTIONS.index(emoji)}"))
        if i % per_row == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channels_list_kb(cfg: dict, prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for cid, info in cfg["channels"].items():
        title = info.get("title") or cid
        mark = "🎯" if info.get("custom_reactions") else ("🌐" if info.get("general_enabled", True) else "🚫")
        rows.append([InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"{prefix}:{cid}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="— Kanallar topilmadi —", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def yesno_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yoqish", callback_data=f"{prefix}:on"),
                InlineKeyboardButton(text="🚫 O'chirish", callback_data=f"{prefix}:off"),
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:custom")],
        ]
    )


# ----------------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    cfg = await storage.get()
    if message.from_user.id not in cfg.setdefault("users", []):
        cfg["users"].append(message.from_user.id)
        await storage.save(bot)

    if is_admin(message.from_user.id):
        await message.answer(
            "Assalomu alaykum, <b>BOSS</b>! 👑\nNima qilmoqchisiz?",
            reply_markup=main_menu_kb(),
        )
        return

    if cfg.get("mandatory_channel"):
        if not await is_subscribed(bot, message.from_user.id, cfg["mandatory_channel"]):
            await send_subscribe_prompt(message, cfg)
            return

    await send_user_start(message, cfg)


async def is_subscribed(bot: Bot, user_id: int, channel) -> bool:
    global _mandatory_check_error_notified
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        _mandatory_check_error_notified = False
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Obuna tekshirishda xatolik: {e}")
        if not _mandatory_check_error_notified:
            _mandatory_check_error_notified = True
            await notify_superadmin(
                bot,
                "⚠️ Majburiy kanal/guruh a'zoligini tekshirishda doimiy xatolik bor "
                "(masalan, bot u yerda admin emas yoki chat o'chirilgan).\n"
                f"Texnik xato: <code>{e}</code>\n\n"
                "Bu davrda foydalanuvchilar botdan foydalana olmasligi mumkin!",
            )
        return False


async def send_subscribe_prompt(message: Message, cfg: dict):
    title = cfg.get("mandatory_channel_title") or "kanal"
    url = cfg.get("mandatory_channel_url")
    if not url:
        legacy = cfg.get("mandatory_channel")
        if isinstance(legacy, str) and legacy.startswith("@"):
            url = f"https://t.me/{legacy.lstrip('@')}"
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text=f"➡️ {title}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    text = f"Botdan foydalanish uchun avval quyidagiga a'zo bo'ling:\n\n📌 <b>{title}</b>"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def user_contact_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✉️ Admin bilan bog'lanish", callback_data="contact_admin")]]
    )


async def send_user_start(message: Message, cfg: dict):
    await message.answer(
        cfg.get("start_message_user") or DEFAULT_CONFIG["start_message_user"],
        reply_markup=user_contact_kb(),
    )
    if cfg.get("ad_text"):
        rows = []
        if cfg.get("ad_url"):
            rows.append([InlineKeyboardButton(text=cfg.get("ad_button_text", "📢 Reklama"), url=cfg["ad_url"])])
        kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        await message.answer(cfg["ad_text"], reply_markup=kb)


@router.callback_query(F.data == "contact_admin")
async def cb_contact_admin(call: CallbackQuery):
    await call.message.answer(
        "✍️ Xabaringizni shu yerga yozing (matn, rasm, video — hammasi mumkin), "
        "men uni adminga yetkazaman."
    )
    await call.answer()


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot):
    cfg = await storage.get()
    channel = cfg.get("mandatory_channel")
    if channel and not await is_subscribed(bot, call.from_user.id, channel):
        await call.answer("Hali obuna bo'lmadingiz ❌", show_alert=True)
        return
    await call.message.delete()
    await send_user_start(call.message, cfg)
    await call.answer()


# ----------------------------------------------------------------------------
# FOYDALANUVCHI <-> ADMIN XABARLASHUV (Reply orqali javob berish)
# ----------------------------------------------------------------------------
feedback_map: dict[int, int] = {}  # admin chatidagi xabar ID -> foydalanuvchi ID (faqat xotirada)


class IsFeedbackReply(Filter):
    """Faqat: admin, private chatda, va aynan feedback sifatida forward qilingan
    xabarga Reply qilayotgan bo'lsa True qaytaradi."""

    async def __call__(self, message: Message) -> bool:
        if not message.from_user or message.from_user.id not in ADMIN_IDS:
            return False
        if not message.reply_to_message:
            return False
        return message.reply_to_message.message_id in feedback_map


async def forward_to_admins(message: Message, bot: Bot):
    user = message.from_user
    header = (
        "✉️ <b>Yangi xabar</b>\n"
        f"👤 {user.full_name}" + (f" (@{user.username})" if user.username else "") + "\n"
        f"🆔 <code>{user.id}</code>\n\n"
        "Javob berish uchun ushbu xabarga <b>Reply</b> qiling."
    )
    delivered = False
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, header)
            fwd = await bot.forward_message(admin_id, message.chat.id, message.message_id)
            feedback_map[fwd.message_id] = user.id
            delivered = True
        except Exception as e:
            logger.warning(f"Adminga forward qilishda xatolik ({admin_id}): {e}")
    if delivered:
        await message.answer("✅ Xabaringiz adminga yuborildi. Tez orada javob beriladi.")
    else:
        await message.answer("❌ Afsuski xabaringizni yuborib bo'lmadi. Birozdan keyin qayta urinib ko'ring.")


@router.message(F.chat.type == ChatType.PRIVATE, IsFeedbackReply())
async def on_admin_reply(message: Message, bot: Bot):
    target_user_id = feedback_map[message.reply_to_message.message_id]
    try:
        await bot.copy_message(chat_id=target_user_id, from_chat_id=message.chat.id, message_id=message.message_id)
        await message.reply("✅ Foydalanuvchiga yuborildi.")
    except Exception as e:
        await message.reply(f"❌ Yuborib bo'lmadi: {e}")


# ----------------------------------------------------------------------------
# ASOSIY MENYU NAVIGATSIYASI (faqat admin, faqat shaxsiy chat)
# ----------------------------------------------------------------------------
@router.callback_query(F.data == "back:main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ---- 1) Umumiy kanal reaksiyasi -------------------------------------------
@router.callback_query(F.data == "menu:general")
async def cb_menu_general(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    cfg = await storage.get()
    await state.set_state(AdminStates.editing_general)
    await state.update_data(selection=list(cfg["general_reactions"]))
    extra = [[InlineKeyboardButton(text="💾 Saqlash", callback_data="general:save")],
             [InlineKeyboardButton(text="📋 Kanallar (yoqish/o'chirish)", callback_data="general:channels")],
             [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")]]
    await call.message.edit_text(
        "🌐 <b>Umumiy kanal reaksiyasi</b>\n"
        "Bu reaksiyalar maxsus sozlanmagan BARCHA kanallarda qo'llanadi.\n"
        "Kerakli emojilarni tanlang:",
        reply_markup=reactions_grid_kb(cfg["general_reactions"], "gen", extra_rows=extra),
    )
    await call.answer()


@router.callback_query(AdminStates.editing_general, F.data.startswith("gen:"))
async def cb_toggle_general(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    emoji = ALL_REACTIONS[idx]
    data = await state.get_data()
    sel = data.get("selection", [])
    if emoji in sel:
        sel.remove(emoji)
    else:
        sel.append(emoji)
    await state.update_data(selection=sel)
    extra = [[InlineKeyboardButton(text="💾 Saqlash", callback_data="general:save")],
             [InlineKeyboardButton(text="📋 Kanallar (yoqish/o'chirish)", callback_data="general:channels")],
             [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")]]
    await call.message.edit_reply_markup(reply_markup=reactions_grid_kb(sel, "gen", extra_rows=extra))
    await call.answer()


@router.callback_query(AdminStates.editing_general, F.data == "general:save")
async def cb_save_general(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    sel = data.get("selection", [])
    cfg = await storage.get()
    cfg["general_reactions"] = sel
    await storage.save(bot)
    await state.clear()
    await call.answer("Saqlandi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


@router.callback_query(F.data == "general:channels")
async def cb_general_channels(call: CallbackQuery, state: FSMContext):
    cfg = await storage.get()
    await state.clear()
    await call.message.edit_text(
        "🌐 = umumiy reaksiya yoqilgan | 🎯 = maxsus reaksiya bor | 🚫 = umumiy reaksiya o'chirilgan\n"
        "Kanalni tanlang:",
        reply_markup=channels_list_kb(cfg, "genchan"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("genchan:"))
async def cb_general_channel_toggle_menu(call: CallbackQuery):
    cid = call.data.split(":", 1)[1]
    cfg = await storage.get()
    info = cfg["channels"].get(cid, {})
    status = "yoqilgan ✅" if info.get("general_enabled", True) else "o'chirilgan 🚫"
    kb = yesno_kb(f"gensetch:{cid}")
    kb.inline_keyboard.insert(-1, [InlineKeyboardButton(
        text="🗑 Kanalni ro'yxatdan o'chirish", callback_data=f"delchan_ask:{cid}:genchan"
    )])
    await call.message.edit_text(
        f"<b>{info.get('title', cid)}</b>\nUmumiy reaksiya holati: {status}",
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("gensetch:"))
async def cb_general_channel_toggle_set(call: CallbackQuery, bot: Bot):
    _, cid, val = call.data.split(":")
    cfg = await storage.get()
    cfg["channels"].setdefault(cid, {"title": cid, "general_enabled": True, "custom_reactions": None})
    cfg["channels"][cid]["general_enabled"] = (val == "on")
    await storage.save(bot)
    await call.answer("Saqlandi ✅")
    await call.message.edit_text(
        "🌐 = umumiy yoqilgan | 🎯 = maxsus reaksiya bor | 🚫 = umumiy o'chirilgan\nKanalni tanlang:",
        reply_markup=channels_list_kb(cfg, "genchan"),
    )


# ---- Kanalni ro'yxatdan o'chirish (genchan va cchan ikkovi uchun umumiy) ----
@router.callback_query(F.data.startswith("delchan_ask:"))
async def cb_delchan_ask(call: CallbackQuery):
    _, cid, ret = call.data.split(":", 2)
    cfg = await storage.get()
    title = cfg["channels"].get(cid, {}).get("title", cid)
    rows = [[
        InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"delchan_do:{cid}:{ret}"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"{ret}:{cid}"),
    ]]
    await call.message.edit_text(
        f"<b>{title}</b> sozlamalar ro'yxatidan butunlay o'chirilsinmi?\n\n"
        "<i>Eslatma: bot kanalda admin bo'lib qolaveradi, faqat reaksiya sozlamalari "
        "o'chadi — keyingi postda kanal avtomatik qayta ro'yxatga olinadi.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("delchan_do:"))
async def cb_delchan_do(call: CallbackQuery, bot: Bot):
    _, cid, ret = call.data.split(":", 2)
    cfg = await storage.get()
    cfg["channels"].pop(cid, None)
    await storage.save(bot)
    await call.answer("O'chirildi ✅", show_alert=True)
    if ret == "cchan":
        await call.message.edit_text("🎯 Kanalni tanlang:", reply_markup=channels_list_kb(cfg, "cchan"))
    else:
        await call.message.edit_text(
            "🌐 = umumiy yoqilgan | 🎯 = maxsus reaksiya bor | 🚫 = umumiy o'chirilgan\nKanalni tanlang:",
            reply_markup=channels_list_kb(cfg, "genchan"),
        )


@router.callback_query(F.data == "back:custom")
async def cb_back_custom(call: CallbackQuery):
    cfg = await storage.get()
    await call.message.edit_text("🎯 Kanalni tanlang:", reply_markup=channels_list_kb(cfg, "cchan"))
    await call.answer()


# ---- 2) Maxsus kanal reaksiyasi --------------------------------------------
@router.callback_query(F.data == "menu:custom")
async def cb_menu_custom(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    await state.clear()
    cfg = await storage.get()
    await call.message.edit_text(
        "🎯 <b>Maxsus kanal reaksiyasi</b>\nKanalni tanlang:",
        reply_markup=channels_list_kb(cfg, "cchan"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("cchan:"))
async def cb_custom_channel_pick(call: CallbackQuery, state: FSMContext):
    cid = call.data.split(":", 1)[1]
    cfg = await storage.get()
    info = cfg["channels"].get(cid, {})
    current = info.get("custom_reactions") or []
    await state.set_state(AdminStates.editing_custom)
    await state.update_data(selection=list(current), chan_id=cid)
    extra = [
        [InlineKeyboardButton(text="💾 Saqlash", callback_data="custom:save")],
        [InlineKeyboardButton(text="♻️ Tozalash (umumiyga qaytarish)", callback_data="custom:clear")],
        [InlineKeyboardButton(text="🗑 Kanalni butunlay o'chirish", callback_data=f"delchan_ask:{cid}:cchan")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:custom")],
    ]
    await call.message.edit_text(
        f"<b>{info.get('title', cid)}</b> uchun maxsus reaksiyalarni tanlang:",
        reply_markup=reactions_grid_kb(current, "cus", extra_rows=extra),
    )
    await call.answer()


@router.callback_query(AdminStates.editing_custom, F.data.startswith("cus:"))
async def cb_toggle_custom(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    emoji = ALL_REACTIONS[idx]
    data = await state.get_data()
    sel = data.get("selection", [])
    if emoji in sel:
        sel.remove(emoji)
    else:
        sel.append(emoji)
    await state.update_data(selection=sel)
    data = await state.get_data()
    cid = data["chan_id"]
    extra = [
        [InlineKeyboardButton(text="💾 Saqlash", callback_data="custom:save")],
        [InlineKeyboardButton(text="♻️ Tozalash (umumiyga qaytarish)", callback_data="custom:clear")],
        [InlineKeyboardButton(text="🗑 Kanalni butunlay o'chirish", callback_data=f"delchan_ask:{cid}:cchan")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:custom")],
    ]
    await call.message.edit_reply_markup(reply_markup=reactions_grid_kb(sel, "cus", extra_rows=extra))
    await call.answer()


@router.callback_query(AdminStates.editing_custom, F.data == "custom:save")
async def cb_save_custom(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    cid = data["chan_id"]
    sel = data.get("selection", [])
    cfg = await storage.get()
    cfg["channels"].setdefault(cid, {"title": cid, "general_enabled": True})
    cfg["channels"][cid]["custom_reactions"] = sel if sel else None
    await storage.save(bot)
    await state.clear()
    await call.answer("Saqlandi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


@router.callback_query(AdminStates.editing_custom, F.data == "custom:clear")
async def cb_clear_custom(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    cid = data["chan_id"]
    cfg = await storage.get()
    if cid in cfg["channels"]:
        cfg["channels"][cid]["custom_reactions"] = None
        await storage.save(bot)
    await state.clear()
    await call.answer("Tozalandi, endi umumiy reaksiya ishlaydi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


# ---- 3) BOSS reaksiyasi (guruhda) -----------------------------------------
@router.callback_query(F.data == "menu:boss")
async def cb_menu_boss(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    cfg = await storage.get()
    await state.set_state(AdminStates.editing_boss)
    current = cfg.get("boss_reaction")
    extra = [[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")]]
    await call.message.edit_text(
        "👑 <b>BOSS reaksiyasi</b>\n"
        f"Hozirgi: {current or '—'}\n"
        "Siz guruhga yozganingizda bot xabaringizga shu reaksiyani qo'yadi. "
        "Yangi emojini tanlang:",
        reply_markup=reactions_grid_kb([current] if current else [], "boss", extra_rows=extra),
    )
    await call.answer()


@router.callback_query(AdminStates.editing_boss, F.data.startswith("boss:"))
async def cb_set_boss(call: CallbackQuery, state: FSMContext, bot: Bot):
    idx = int(call.data.split(":")[1])
    emoji = ALL_REACTIONS[idx]
    cfg = await storage.get()
    cfg["boss_reaction"] = emoji
    await storage.save(bot)
    await state.clear()
    await call.answer(f"Saqlandi: {emoji} ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


# ---- 4) Start xabari (oddiy foydalanuvchilar) ------------------------------
@router.callback_query(F.data == "menu:startmsg")
async def cb_menu_startmsg(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    cfg = await storage.get()
    await state.set_state(AdminStates.wait_start_message)
    await call.message.edit_text(
        f"💬 Hozirgi start xabari:\n\n<i>{cfg.get('start_message_user')}</i>\n\n"
        "Yangi matnni yuboring (bekor qilish uchun /cancel):",
        reply_markup=back_kb("main"),
    )
    await call.answer()


@router.message(AdminStates.wait_start_message, F.chat.type == ChatType.PRIVATE)
async def on_new_start_message(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    if not message.text:
        await message.answer("❌ Iltimos, matn ko'rinishida yuboring.", reply_markup=back_kb("main"))
        return
    cfg = await storage.get()
    cfg["start_message_user"] = message.text
    await storage.save(bot)
    await state.clear()
    await message.answer("Saqlandi ✅", reply_markup=main_menu_kb())


# ---- 5) Majburiy kanal/guruh -------------------------------------------------
def _mandatory_prompt_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🚫 O'chirish", callback_data="mandatory:off")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:mandatory")
async def cb_menu_mandatory(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    cfg = await storage.get()
    current = cfg.get("mandatory_channel_title") or "o'rnatilmagan"
    await state.set_state(AdminStates.wait_mandatory_channel)
    await call.message.edit_text(
        "🔒 <b>Majburiy kanal/guruh</b>\n\n"
        f"Hozirgi: <b>{current}</b>\n\n"
        "Botni kerakli kanal yoki guruhga <b>admin</b> qilib qo'shing "
        "(a'zolarni ko'rish huquqi bilan), so'ng quyidagilardan birini bajaring:\n\n"
        "1️⃣ O'sha kanal/guruhdan istalgan xabarni shu yerga <b>forward</b> qiling\n"
        "2️⃣ Yoki yuboring: <code>@username</code> yoki <code>https://t.me/username</code>\n"
        "3️⃣ Yoki ID yuboring: <code>-1001234567890</code>\n\n"
        "<i>Eslatma: taklif havolalari (masalan t.me/+AbCdEf ko'rinishidagi) ishlamaydi — "
        "forward yoki ID usulidan foydalaning.</i>",
        reply_markup=_mandatory_prompt_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "mandatory:off")
async def cb_mandatory_off(call: CallbackQuery, state: FSMContext, bot: Bot):
    cfg = await storage.get()
    cfg["mandatory_channel"] = None
    cfg["mandatory_channel_title"] = None
    cfg["mandatory_channel_url"] = None
    await storage.save(bot)
    await state.clear()
    await call.answer("O'chirildi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


@router.message(AdminStates.wait_mandatory_channel, F.chat.type == ChatType.PRIVATE)
async def on_new_mandatory_channel(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())

    kb = _mandatory_prompt_kb()

    fwd_chat = extract_forwarded_chat(message)
    target = fwd_chat.id if fwd_chat else parse_chat_reference(message.text or "")

    if target is None:
        await message.answer(
            "❌ Bu formatni tushunmadim.\n\n"
            "Iltimos: kanal/guruhdan xabar <b>forward</b> qiling, <code>@username</code> yuboring, "
            "yoki raqamli ID yuboring (masalan <code>-1001234567890</code>).",
            reply_markup=kb,
        )
        return
    if target == "INVITE_LINK":
        await message.answer(
            "❌ Taklif havolasi (t.me/+...) orqali kanal/guruhni aniqlab bo'lmaydi.\n\n"
            "Iltimos, o'sha kanal/guruhdan istalgan xabarni shu yerga <b>forward</b> qiling, "
            "yoki <code>@username</code> / raqamli ID yuboring.",
            reply_markup=kb,
        )
        return

    try:
        chat = await bot.get_chat(target)
    except Exception as e:
        await message.answer(
            "❌ Bu kanal/guruh topilmadi.\n\n"
            "Sabab: username xato yozilgan bo'lishi mumkin, yoki bot u yerga umuman a'zo emas.\n"
            f"Texnik xato: <code>{e}</code>",
            reply_markup=kb,
        )
        return

    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception:
        await message.answer(
            f"❌ Bot <b>{chat.title}</b>ga umuman a'zo emas.\n"
            "Avval botni o'sha kanal/guruhga qo'shing, so'ng qayta yuboring.",
            reply_markup=kb,
        )
        return

    if member.status not in ("administrator", "creator"):
        await message.answer(
            f"❌ Bot <b>{chat.title}</b>da admin emas.\n"
            "Iltimos, botni admin qilib tayinlang (a'zolarni ko'rish huquqi bilan), "
            "so'ng qayta yuboring.",
            reply_markup=kb,
        )
        return

    invite_url = None
    username = getattr(chat, "username", None)
    if username:
        invite_url = f"https://t.me/{username}"
    else:
        try:
            invite_url = getattr(chat, "invite_link", None) or await bot.export_chat_invite_link(chat.id)
        except Exception as e:
            logger.warning(f"Taklif havolasini olib bo'lmadi: {e}")

    cfg = await storage.get()
    cfg["mandatory_channel"] = chat.id
    cfg["mandatory_channel_title"] = chat.title
    cfg["mandatory_channel_url"] = invite_url
    await storage.save(bot)
    await state.clear()
    note = "" if invite_url else "\n⚠️ Ochiq havola topilmadi — foydalanuvchilarga faqat nom ko'rsatiladi."
    await message.answer(f"Saqlandi ✅ ({chat.title}){note}", reply_markup=main_menu_kb())


# ---- 6) Reklama --------------------------------------------------------------
@router.callback_query(F.data == "menu:ad")
async def cb_menu_ad(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    cfg = await storage.get()
    await state.clear()
    text = cfg.get("ad_text") or "—"
    url = cfg.get("ad_url") or "—"
    rows = [
        [InlineKeyboardButton(text="✏️ Matnni o'zgartirish", callback_data="ad:text")],
        [InlineKeyboardButton(text="🔗 Linkni o'zgartirish", callback_data="ad:url")],
        [InlineKeyboardButton(text="🚫 O'chirish", callback_data="ad:clear")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")],
    ]
    await call.message.edit_text(
        f"📢 <b>Reklama</b> (foydalanuvchi start bosganda ko'radi)\n\n"
        f"Matn: {text}\nLink: {url}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.data == "ad:text")
async def cb_ad_text(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.wait_ad_text)
    await call.message.edit_text("Yangi reklama matnini yuboring (/cancel — bekor qilish):", reply_markup=back_kb("main"))
    await call.answer()


@router.message(AdminStates.wait_ad_text, F.chat.type == ChatType.PRIVATE)
async def on_ad_text(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    cfg = await storage.get()
    cfg["ad_text"] = message.text
    await storage.save(bot)
    await state.clear()
    await message.answer("Saqlandi ✅", reply_markup=main_menu_kb())


@router.callback_query(F.data == "ad:url")
async def cb_ad_url(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.wait_ad_url)
    await call.message.edit_text("Yangi reklama linkini yuboring (https://...):", reply_markup=back_kb("main"))
    await call.answer()


@router.message(AdminStates.wait_ad_url, F.chat.type == ChatType.PRIVATE)
async def on_ad_url(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    cfg = await storage.get()
    cfg["ad_url"] = message.text.strip()
    await storage.save(bot)
    await state.clear()
    await message.answer("Saqlandi ✅", reply_markup=main_menu_kb())


@router.callback_query(F.data == "ad:clear")
async def cb_ad_clear(call: CallbackQuery, bot: Bot):
    cfg = await storage.get()
    cfg["ad_text"] = None
    cfg["ad_url"] = None
    await storage.save(bot)
    await call.answer("O'chirildi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


# ---- 7) Foydalanuvchilar ro'yxati (tugmalar + to'g'ridan-to'g'ri xabar) ----
USERS_PAGE_SIZE = 8


def users_page_kb(users: list[int], page: int, names: dict[int, str]) -> InlineKeyboardMarkup:
    start = page * USERS_PAGE_SIZE
    chunk = users[start:start + USERS_PAGE_SIZE]
    rows = []
    for uid in chunk:
        rows.append([InlineKeyboardButton(text=f"🧑 {names.get(uid, str(uid))}", callback_data=f"userpick:{uid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"userspage:{page - 1}"))
    if start + USERS_PAGE_SIZE < len(users):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"userspage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="📄 .txt yuklab olish", callback_data="users_export")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_users_page(call: CallbackQuery, bot: Bot, page: int):
    cfg = await storage.get()
    users = cfg.get("users", [])
    if not users:
        await call.message.edit_text(
            "👥 Hozircha /start bosgan foydalanuvchi yo'q.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")]]),
        )
        return
    start = page * USERS_PAGE_SIZE
    chunk = users[start:start + USERS_PAGE_SIZE]
    names: dict[int, str] = {}
    for uid in chunk:
        try:
            chat = await bot.get_chat(uid)
            name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None) or str(uid)
            if getattr(chat, "username", None):
                name += f" (@{chat.username})"
            names[uid] = name
        except Exception:
            names[uid] = f"{uid} (nom olinmadi)"
    total_pages = (len(users) - 1) // USERS_PAGE_SIZE + 1
    await call.message.edit_text(
        f"👥 Jami: <b>{len(users)}</b> ta foydalanuvchi (sahifa {page + 1}/{total_pages})\n"
        "Xabar yubormoqchi bo'lgan foydalanuvchini tanlang:",
        reply_markup=users_page_kb(users, page, names),
    )


@router.callback_query(F.data == "menu:users")
async def cb_menu_users(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer()
    await show_users_page(call, bot, 0)
    await call.answer()


@router.callback_query(F.data.startswith("userspage:"))
async def cb_users_page(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer()
    page = int(call.data.split(":")[1])
    await show_users_page(call, bot, page)
    await call.answer()


@router.callback_query(F.data == "users_export")
async def cb_users_export(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer()
    cfg = await storage.get()
    users = cfg.get("users", [])
    await call.answer()
    text_content = "\n".join(str(u) for u in users)
    file = BufferedInputFile(text_content.encode("utf-8"), filename="foydalanuvchilar.txt")
    await call.message.answer_document(file, caption=f"👥 Jami {len(users)} ta foydalanuvchi (Telegram ID).")


@router.callback_query(F.data.startswith("userpick:"))
async def cb_user_pick(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    uid = int(call.data.split(":")[1])
    await state.set_state(AdminStates.wait_direct_message)
    await state.update_data(direct_target=uid)
    await call.message.edit_text(
        f"✍️ <code>{uid}</code> ga yubormoqchi bo'lgan xabaringizni yozing "
        "(matn, rasm, video, fayl — hammasi mumkin):\n\n/cancel — bekor qilish",
        reply_markup=back_kb("main"),
    )
    await call.answer()


@router.message(AdminStates.wait_direct_message, F.chat.type == ChatType.PRIVATE)
async def on_direct_message(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    data = await state.get_data()
    uid = data.get("direct_target")
    await state.clear()
    if not uid:
        return await message.answer("Xatolik: foydalanuvchi topilmadi.", reply_markup=main_menu_kb())
    try:
        await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
        await message.answer("✅ Yuborildi.", reply_markup=main_menu_kb())
    except TelegramForbiddenError:
        await message.answer("❌ Foydalanuvchi botni bloklagan yoki hisobini o'chirgan, yuborib bo'lmadi.", reply_markup=main_menu_kb())
    except Exception as e:
        await message.answer(f"❌ Yuborib bo'lmadi: {e}", reply_markup=main_menu_kb())


# ---- 8) Broadcast — barcha /start bosgan foydalanuvchilarga to'g'ridan-to'g'ri xabar ----
@router.callback_query(F.data == "menu:broadcast")
async def cb_menu_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminStates.wait_broadcast)
    await call.message.edit_text(
        "📣 Hammaga yubormoqchi bo'lgan xabaringizni yuboring.\n"
        "Matn, rasm, video, fayl, audio, gif yoki stiker — hammasi mumkin.\n\n"
        "/cancel — bekor qilish",
        reply_markup=back_kb("main"),
    )
    await call.answer()


@router.message(AdminStates.wait_broadcast, F.chat.type == ChatType.PRIVATE)
async def on_broadcast_content(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    await state.update_data(bc_chat_id=message.chat.id, bc_message_id=message.message_id)
    cfg = await storage.get()
    count = len(cfg.get("users", []))
    rows = [[
        InlineKeyboardButton(text="✅ Ha, yubor", callback_data="bc:send"),
        InlineKeyboardButton(text="❌ Yo'q", callback_data="bc:cancel"),
    ]]
    await message.answer(
        f"Yuqoridagi xabar {count} ta foydalanuvchiga yuborilsinmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "bc:cancel")
async def cb_broadcast_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Bekor qilindi.")
    await call.answer()


@router.callback_query(F.data == "bc:send")
async def cb_broadcast_send(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer()
    data = await state.get_data()
    src_chat_id = data.get("bc_chat_id")
    src_message_id = data.get("bc_message_id")
    await state.clear()
    if not src_chat_id or not src_message_id:
        return await call.message.edit_text("Xatolik: yuboriladigan xabar topilmadi, qaytadan urinib ko'ring.")

    cfg = await storage.get()
    user_ids = list(cfg.get("users", []))
    await call.message.edit_text(f"Yuborilmoqda... (0/{len(user_ids)})")
    await call.answer()

    sent, failed = 0, 0
    dead_users = []
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
            sent += 1
        except TelegramForbiddenError:
            failed += 1
            dead_users.append(uid)
        except Exception as e:
            failed += 1
            logger.warning(f"Broadcast xatolik ({uid}): {e}")
        await asyncio.sleep(0.05)

    extra_note = ""
    if dead_users:
        cfg["users"] = [u for u in cfg.get("users", []) if u not in dead_users]
        try:
            await storage.save(bot)
            extra_note = f"\n🗑 {len(dead_users)} ta faol bo'lmagan foydalanuvchi ro'yxatdan tozalandi."
        except Exception as e:
            logger.warning(f"Faol bo'lmagan foydalanuvchilarni tozalab bo'lmadi: {e}")

    await call.message.edit_text(f"✅ Yuborildi: {sent} ta\n❌ Yetkazilmadi: {failed} ta{extra_note}")


# ----------------------------------------------------------------------------
# KANAL POSTLARIGA AVTOMATIK REAKSIYA
# ----------------------------------------------------------------------------
@router.channel_post()
async def on_channel_post(message: Message, bot: Bot):
    cfg = await storage.get()
    cid = str(message.chat.id)
    info = cfg["channels"].get(cid)
    if info is None:
        info = {"title": message.chat.title, "general_enabled": True, "custom_reactions": None}
        cfg["channels"][cid] = info
        await storage.save(bot)
    else:
        if info.get("title") != message.chat.title:
            info["title"] = message.chat.title

    if info.get("custom_reactions"):
        pool = info["custom_reactions"]
    elif info.get("general_enabled", True):
        pool = cfg["general_reactions"]
    else:
        return

    if not pool:
        return

    emoji = random.choice(pool)
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        logger.warning(f"Kanal reaksiyasida xatolik ({message.chat.id}): {e}")


# ----------------------------------------------------------------------------
# GURUHDA — ADMIN (BOSS) XABARLARIGA REAKSIYA
# ----------------------------------------------------------------------------
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_group_message(message: Message, bot: Bot):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    cfg = await storage.get()
    emoji = cfg.get("boss_reaction")
    if not emoji:
        return
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        logger.warning(f"BOSS reaksiyasida xatolik ({message.chat.id}): {e}")


# ----------------------------------------------------------------------------
# Bot yangi kanalga admin qilib qo'shilganda avtomatik ro'yxatga olish
# ----------------------------------------------------------------------------
@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, bot: Bot):
    if update.chat.type != ChatType.CHANNEL:
        return
    if update.new_chat_member.status != "administrator":
        return
    cfg = await storage.get()
    cid = str(update.chat.id)
    if cid not in cfg["channels"]:
        cfg["channels"][cid] = {
            "title": update.chat.title,
            "general_enabled": True,
            "custom_reactions": None,
        }
        await storage.save(bot)
        logger.info(f"Yangi kanal ro'yxatga olindi: {update.chat.title} ({cid})")


# ----------------------------------------------------------------------------
# ZAXIRA HANDLER (Fallback) — private chatda hech qanday holatga to'g'ri
# kelmagan har qanday xabarni ushlaydi. HAR DOIM ENG OXIRIDA registratsiya
# qilinishi kerak, chunki filtri eng keng qamrovli.
# ----------------------------------------------------------------------------
@router.message(F.chat.type == ChatType.PRIVATE)
async def on_private_message_fallback(message: Message, bot: Bot):
    if is_admin(message.from_user.id):
        # Bu holatga admin tushib qolishi odatda FSM holati kutilmaganda
        # tozalanib qolganida yuz beradi (masalan server qayta ishga tushganda).
        await message.answer(
            "🤔 Bu buyruqni tushunmadim, yoki jarayon vaqtincha uzilib qolgan bo'lishi mumkin.\n\n"
            "Asosiy menyu:",
            reply_markup=main_menu_kb(),
        )
        return

    cfg = await storage.get()
    if message.from_user.id not in cfg.setdefault("users", []):
        cfg["users"].append(message.from_user.id)
        try:
            await storage.save(bot)
        except Exception:
            pass

    if cfg.get("mandatory_channel"):
        if not await is_subscribed(bot, message.from_user.id, cfg["mandatory_channel"]):
            await send_subscribe_prompt(message, cfg)
            return

    await forward_to_admins(message, bot)


# ----------------------------------------------------------------------------
# WEBHOOK / RENDER ISHGA TUSHIRISH
# ----------------------------------------------------------------------------
async def on_startup(bot: Bot):
    await storage.load(bot)

    webhook_set = False
    for attempt in range(1, 6):
        try:
            ok = await bot.set_webhook(
                WEBHOOK_URL,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=True,
            )
            info = await bot.get_webhook_info()
            logger.info(
                f"Webhook o'rnatildi (attempt {attempt}): ok={ok}, "
                f"url={info.url!r}, last_error={info.last_error_message!r}"
            )
            if info.url:
                webhook_set = True
                break
        except Exception as e:
            logger.error(f"Webhook o'rnatishda xatolik (attempt {attempt}/5): {e}")
        await asyncio.sleep(3)

    if not webhook_set:
        logger.error("Webhookni 5 urinishdan keyin ham o'rnatib bo'lmadi! Server baribir ishga tushadi.")
        await notify_superadmin(
            bot,
            "🚨 <b>KRITIK:</b> Webhook 5 urinishdan keyin ham o'rnatilmadi! "
            "Bot xabarlar qabul qilmasligi mumkin. Render loglarini tekshiring.",
        )


async def on_shutdown(bot: Bot):
    logger.info("Bot to'xtatilmoqda (webhook saqlab qolindi).")


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


async def health(request: web.Request):
    return web.Response(text="OK")


def main():
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
