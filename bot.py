# -*- coding: utf-8 -*-
"""
Reaksiya Bot — Telegram kanal/guruh reaksiya boti
aiogram 3.x | Webhook | Render.com uchun tayyor

Konfiguratsiya alohida DB o'rniga maxsus "storage" guruhida saqlanadi:
bot o'sha guruhda admin bo'ladi va har bir o'zgarishda pinned FAYLNI (config.json)
yangilaydi. Matn emas, FAYL — chunki Telegram matn xabari 4096 belgi bilan
cheklangan, fayl esa ~20MB gacha bo'lishi mumkin (yuz minglab user uchun yetarli).

--- v2 o'zgarishlari (bug-fix + xavfsizlik + scaling) ---
1. Config endi pinned "document" (JSON fayl) sifatida saqlanadi — 4096-belgi
   limitidan chiqib ketish endi mumkin emas (avvalgi "majburiy kanal saqlanmayapti"
   degan muammoning asosiy sababi shu edi).
2. Webhook manzilida endi xom BOT_TOKEN ishlatilmaydi (hash qilinadi) va
   Telegram secret_token bilan tasdiqlanadi — token loglarga tushib qolsa ham,
   soxta update yuborib botni boshqarib bo'lmaydi.
3. Webhook har startup'da majburan qayta o'rnatiladi + har 10 daqiqada
   o'z-o'zini tekshiruvchi background task ishlaydi (self-heal).
   Shutdown'da webhook ATAYIN o'chirilmaydi — Render rolling-deploy paytida
   buni o'chirish aksincha botni "o'chirib qo'yishi" mumkin edi.
4. Barcha foydalanuvchi matn kiritadigan joylarda None-matn (rasm/sticker/forward)
   yuborilsa ham bot yiqilmaydi.
5. Majburiy kanal input parser: @kanal, -100..., https://t.me/kanal — hammasini tushunadi.
6. ad_url validatsiya qilinadi (http/https bilan boshlanishi shart) va
   reklama yuborish try/except bilan o'ralgan — noto'g'ri link butun /start'ni
   (hamma foydalanuvchi uchun) buzib qo'ymaydi.
7. Broadcast endi FON REJIMIDA ishlaydi (webhook javobini bloklamaydi),
   429 (flood) xatolarida avtomatik kutadi, botni bloklagan userlarni
   ro'yxatdan avtomatik tozalaydi.
8. /start bosilganda user ro'yxatiga qo'shish endi "debounced" saqlanadi
   (har safar emas, 5 sekundda bir marta) — ko'p user bir vaqtda /start
   bossa ham API'ga zarba kam tushadi.
9. Har bir muhim saqlash joyida xatolik bo'lsa, admin buni "hech narsa
   bo'lmagandek" jim qolish o'rniga ANIQ xabar ko'radi.
10. Kanal nomi o'zgarganda endi saqlanadi (avval faqat xotirada qolib,
    restart'da yo'qolib ketardi).
"""

import asyncio
import hashlib
import json
import logging
import os
import random
from copy import deepcopy

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
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

if not WEBHOOK_HOST.startswith(("https://", "http://")):
    # Render ba'zan domenni sxemasiz beradi — shu yerda avtomatik tuzatamiz
    WEBHOOK_HOST = f"https://{WEBHOOK_HOST}"

# XAVFSIZLIK: webhook manzilida xom BOT_TOKEN ishlatmaymiz (u Render'ning HTTP
# access loglariga har update'da yozilib boradi). Buning o'rniga tokendan
# deterministik hash chiqaramiz — qayta deploy qilinsa ham manzil o'zgarmaydi,
# lekin token o'zi hech qayerda ko'rinmaydi.
_path_hash = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
WEBHOOK_PATH = f"/webhook/{_path_hash}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# Telegram har webhook so'rovida shu tokenni header orqali yuboradi — biz uni
# tekshirib, soxta (bizniki bo'lmagan) so'rovlarni rad etamiz.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(
    (BOT_TOKEN + "::reaksiyabot-secret").encode()
).hexdigest()[:50]

PORT = int(os.environ.get("PORT", 8080))

# ADMIN_IDS -> ikkalasi ham bir xil huquqga ega, lekin ADMIN superadminni bilmaydi
# (chunki hech qanday "adminlar ro'yxati" funksiyasi yo'q, ikkalasi ham shunchaki "BOSS")
ADMIN_IDS = {SUPERADMIN_ID, ADMIN_ID}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reaction_bot")

# ----------------------------------------------------------------------------
# TELEGRAMNING STANDART REAKSIYA EMOJILARI (barcha default reaksiyalar)
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
    "mandatory_channel": None,  # "@username" yoki -100... (int yoki str)
    "mandatory_channel_title": None,
    "ad_text": None,
    "ad_url": None,
    "ad_button_text": "📢 Reklama",
    "pinned_message_id": None,
    "users": [],  # /start bosgan barcha foydalanuvchi id'lari (broadcast uchun)
}

# ----------------------------------------------------------------------------
# KONFIGURATSIYA — Telegram guruhidagi pinned FAYLDA (document) saqlanadi
# ----------------------------------------------------------------------------
class ConfigStorage:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.data: dict | None = None
        self._lock = asyncio.Lock()
        self._dirty = False

    async def load(self, bot: Bot) -> dict:
        try:
            chat = await bot.get_chat(self.chat_id)
            pinned = chat.pinned_message
            loaded = None
            if pinned and pinned.document:
                buf = await bot.download(pinned.document.file_id)
                loaded = json.loads(buf.read().decode("utf-8"))
            elif pinned and pinned.text:
                # Eski (matn asosidagi, v1) konfiguratsiyadan avtomatik migratsiya
                loaded = json.loads(pinned.text)
                logger.info("Eski matn-formatdagi konfiguratsiya topildi, fayl formatiga o'tkaziladi.")
            if loaded is not None:
                merged = deepcopy(DEFAULT_CONFIG)
                merged.update(loaded)
                self.data = merged
                logger.info(f"Konfiguratsiya yuklandi ({len(merged.get('users', []))} user, "
                            f"{len(merged.get('channels', {}))} kanal).")
                return self.data
        except Exception as e:
            logger.warning(f"Konfiguratsiyani yuklashda xatolik: {e}")

        self.data = deepcopy(DEFAULT_CONFIG)
        logger.info("Standart konfiguratsiya bilan ishga tushirildi.")
        return self.data

    async def get(self) -> dict:
        if self.data is None:
            raise RuntimeError("Konfiguratsiya hali yuklanmagan")
        return self.data

    def mark_dirty(self):
        """Darhol saqlamasdan, 'o'zgarish bor' deb belgilaydi — flush_loop uni yuboradi."""
        self._dirty = True

    async def flush_loop(self, bot: Bot, interval: float = 5.0):
        """Debounced saqlash: ko'p /start bir vaqtda kelsa ham, API'ga kam zarba tushadi."""
        while True:
            await asyncio.sleep(interval)
            if self._dirty:
                self._dirty = False
                try:
                    await self.save(bot)
                except Exception as e:
                    logger.error(f"Debounced saqlashda xatolik: {e}")

    async def save(self, bot: Bot):
        async with self._lock:
            payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            pid = self.data.get("pinned_message_id")

            if pid:
                try:
                    await bot.edit_message_media(
                        chat_id=self.chat_id,
                        message_id=pid,
                        media=InputMediaDocument(
                            media=BufferedInputFile(payload, filename="config.json"),
                            caption="reaksiyaBot config (avtomatik saqlanadi, qo'lda o'zgartirmang)",
                        ),
                    )
                    return
                except Exception as e:
                    logger.warning(f"Pinned faylni tahrirlab bo'lmadi, yangisi yaratiladi: {e}")
                    pid = None

            # Pinned fayl yo'q yoki tahrirlab bo'lmadi -> yangisini yuboramiz
            msg = await bot.send_document(
                self.chat_id,
                document=BufferedInputFile(payload, filename="config.json"),
                caption="reaksiyaBot config (avtomatik saqlanadi, qo'lda o'zgartirmang)",
            )
            try:
                await bot.pin_chat_message(self.chat_id, msg.message_id, disable_notification=True)
            except Exception as e:
                logger.warning(f"Xabarni pin qilishda xatolik: {e}")

            self.data["pinned_message_id"] = msg.message_id
            # ID ni ham fayl ichiga yozib qo'yamiz (keyingi safar shu xabar tahrirlanadi)
            payload2 = json.dumps(self.data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            try:
                await bot.edit_message_media(
                    chat_id=self.chat_id,
                    message_id=msg.message_id,
                    media=InputMediaDocument(
                        media=BufferedInputFile(payload2, filename="config.json"),
                        caption="reaksiyaBot config (avtomatik saqlanadi, qo'lda o'zgartirmang)",
                    ),
                )
            except Exception as e:
                logger.warning(f"pinned_message_id yozishda xatolik: {e}")


storage = ConfigStorage(STORAGE_CHAT_ID)
router = Router()


async def try_save(bot: Bot) -> tuple[bool, str]:
    """storage.save() ni xavfsiz chaqiradi — xatolik bo'lsa jim qolmaydi, xabarni qaytaradi."""
    try:
        await storage.save(bot)
        return True, ""
    except Exception as e:
        logger.error(f"Konfiguratsiya saqlashda xatolik: {e}")
        return False, str(e)


def admin_text_or_none(message: Message) -> str | None:
    """Admin matn o'rniga rasm/sticker/forward yuborsa ham bot yiqilmasligi uchun."""
    return message.text.strip() if message.text else None


def parse_channel_ref(raw: str):
    """@kanal, -100123..., https://t.me/kanal, t.me/kanal — hammasini tushunadi."""
    raw = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip("/ ")
    if raw.lstrip("-").isdigit():
        return int(raw)
    return f"@{raw}"

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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ----------------------------------------------------------------------------
# KLAVIATURALAR
# ----------------------------------------------------------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🌐 Umumiy kanal reaksiyasi", callback_data="menu:general")],
        [InlineKeyboardButton(text="🎯 Maxsus kanal reaksiyasi", callback_data="menu:custom")],
        [InlineKeyboardButton(text="👑 BOSS reaksiyasi (guruhda)", callback_data="menu:boss")],
        [InlineKeyboardButton(text="💬 Start xabari (foydalanuvchilar)", callback_data="menu:startmsg")],
        [InlineKeyboardButton(text="🔒 Majburiy kanal", callback_data="menu:mandatory")],
        [InlineKeyboardButton(text="📢 Reklama", callback_data="menu:ad")],
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
        # Darhol saqlamaymiz — debounced flush_loop buni bir necha soniyada
        # bir marta saqlaydi. Ko'p user bir vaqtda /start bossa ham xavfsiz.
        storage.mark_dirty()

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
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Obuna tekshirishda xatolik: {e}")
        return False


async def send_subscribe_prompt(message: Message, cfg: dict):
    channel = cfg["mandatory_channel"]
    link = channel if str(channel).startswith("@") else cfg.get("mandatory_channel_title") or "kanal"
    url = f"https://t.me/{str(channel).lstrip('@')}" if str(channel).startswith("@") else None
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text="➡️ Kanalga o'tish", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    await message.answer(
        f"Botdan foydalanish uchun avval kanalga obuna bo'ling: {link}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def send_user_start(message: Message, cfg: dict):
    await message.answer(cfg.get("start_message_user") or DEFAULT_CONFIG["start_message_user"])
    if cfg.get("ad_text"):
        try:
            rows = []
            if cfg.get("ad_url"):
                rows.append(
                    [InlineKeyboardButton(text=cfg.get("ad_button_text", "📢 Reklama"), url=cfg["ad_url"])]
                )
            kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
            await message.answer(cfg["ad_text"], reply_markup=kb)
        except Exception as e:
            # Noto'g'ri/eski reklama linki BUTUN /start oqimini buzmasligi uchun.
            logger.warning(f"Reklama xabarini yuborishda xatolik: {e}")


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
    extra = [
        [InlineKeyboardButton(text="💾 Saqlash", callback_data="general:save")],
        [InlineKeyboardButton(text="📋 Kanallar (yoqish/o'chirish)", callback_data="general:channels")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")],
    ]
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
    extra = [
        [InlineKeyboardButton(text="💾 Saqlash", callback_data="general:save")],
        [InlineKeyboardButton(text="📋 Kanallar (yoqish/o'chirish)", callback_data="general:channels")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")],
    ]
    await call.message.edit_reply_markup(reply_markup=reactions_grid_kb(sel, "gen", extra_rows=extra))
    await call.answer()


@router.callback_query(AdminStates.editing_general, F.data == "general:save")
async def cb_save_general(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    sel = data.get("selection", [])
    cfg = await storage.get()
    cfg["general_reactions"] = sel
    ok, err = await try_save(bot)
    await state.clear()
    if not ok:
        return await call.answer(f"❌ Saqlashda xatolik: {err}", show_alert=True)
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
    await call.message.edit_text(
        f"<b>{info.get('title', cid)}</b>\nUmumiy reaksiya holati: {status}",
        reply_markup=yesno_kb(f"gensetch:{cid}"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("gensetch:"))
async def cb_general_channel_toggle_set(call: CallbackQuery, bot: Bot):
    _, cid, val = call.data.split(":")
    cfg = await storage.get()
    cfg["channels"].setdefault(cid, {"title": cid, "general_enabled": True, "custom_reactions": None})
    cfg["channels"][cid]["general_enabled"] = (val == "on")
    ok, err = await try_save(bot)
    if not ok:
        await call.answer(f"❌ Saqlashda xatolik: {err}", show_alert=True)
        return
    await call.answer("Saqlandi ✅")
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
    extra = [
        [InlineKeyboardButton(text="💾 Saqlash", callback_data="custom:save")],
        [InlineKeyboardButton(text="♻️ Tozalash (umumiyga qaytarish)", callback_data="custom:clear")],
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
    ok, err = await try_save(bot)
    await state.clear()
    if not ok:
        return await call.answer(f"❌ Saqlashda xatolik: {err}", show_alert=True)
    await call.answer("Saqlandi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


@router.callback_query(AdminStates.editing_custom, F.data == "custom:clear")
async def cb_clear_custom(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    cid = data["chan_id"]
    cfg = await storage.get()
    if cid in cfg["channels"]:
        cfg["channels"][cid]["custom_reactions"] = None
        ok, err = await try_save(bot)
        if not ok:
            await state.clear()
            return await call.answer(f"❌ Saqlashda xatolik: {err}", show_alert=True)
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
    ok, err = await try_save(bot)
    await state.clear()
    if not ok:
        return await call.answer(f"❌ Saqlashda xatolik: {err}", show_alert=True)
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
    text = admin_text_or_none(message)
    if text is None:
        return await message.answer("❌ Iltimos, yangi start xabarini matn ko'rinishida yuboring.")
    if text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    cfg = await storage.get()
    cfg["start_message_user"] = message.text
    ok, err = await try_save(bot)
    await state.clear()
    if not ok:
        return await message.answer(f"❌ Saqlashda xatolik: {err}")
    await message.answer("Saqlandi ✅", reply_markup=main_menu_kb())


# ---- 5) Majburiy kanal ------------------------------------------------------
@router.callback_query(F.data == "menu:mandatory")
async def cb_menu_mandatory(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    cfg = await storage.get()
    current = cfg.get("mandatory_channel") or "o'rnatilmagan"
    await state.set_state(AdminStates.wait_mandatory_channel)
    rows = [
        [InlineKeyboardButton(text="🚫 O'chirish", callback_data="mandatory:off")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:main")],
    ]
    await call.message.edit_text(
        f"🔒 Hozirgi majburiy kanal: <b>{current}</b>\n\n"
        "Botni o'sha kanalga ADMIN qilib qo'shing, so'ng kanal username'ini "
        "(@kanal), ID'sini (-100...) yoki https://t.me/kanal linkini yuboring:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.data == "mandatory:off")
async def cb_mandatory_off(call: CallbackQuery, state: FSMContext, bot: Bot):
    cfg = await storage.get()
    cfg["mandatory_channel"] = None
    cfg["mandatory_channel_title"] = None
    ok, err = await try_save(bot)
    await state.clear()
    if not ok:
        return await call.answer(f"❌ Saqlashda xatolik: {err}", show_alert=True)
    await call.answer("O'chirildi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


@router.message(AdminStates.wait_mandatory_channel, F.chat.type == ChatType.PRIVATE)
async def on_new_mandatory_channel(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    text = admin_text_or_none(message)
    if text is None:
        return await message.answer(
            "❌ Iltimos, kanal username (@kanal), ID (-100...) yoki https://t.me/kanal "
            "ko'rinishida matn yuboring."
        )
    if text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())

    channel = parse_channel_ref(text)
    try:
        chat = await bot.get_chat(channel)
        me = await bot.me()
        member = await bot.get_chat_member(channel, me.id)
        if member.status not in ("administrator", "creator"):
            await message.answer("❌ Bot bu kanalda admin emas. Avval admin qilib qo'shing, so'ng qaytadan yuboring.")
            return
    except Exception as e:
        await message.answer(
            f"❌ Kanal topilmadi yoki xatolik:\n<code>{e}</code>\n\n"
            "To'g'ri format: @kanal_username, -100... ID, yoki https://t.me/kanal. "
            "Botni kanalga admin qilib qo'shganingizni tekshiring."
        )
        return

    cfg = await storage.get()
    cfg["mandatory_channel"] = channel
    cfg["mandatory_channel_title"] = chat.title
    ok, err = await try_save(bot)
    if not ok:
        return await message.answer(f"❌ Saqlashda xatolik: {err}\nQaytadan urinib ko'ring.")
    await state.clear()
    await message.answer(f"Saqlandi ✅ ({chat.title})", reply_markup=main_menu_kb())


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
    text = admin_text_or_none(message)
    if text is None:
        return await message.answer("❌ Reklama matnini matn ko'rinishida yuboring.")
    if text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    cfg = await storage.get()
    cfg["ad_text"] = message.text
    ok, err = await try_save(bot)
    await state.clear()
    if not ok:
        return await message.answer(f"❌ Saqlashda xatolik: {err}")
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
    text = admin_text_or_none(message)
    if text is None:
        return await message.answer("❌ Linkni matn ko'rinishida yuboring.")
    if text == "/cancel":
        await state.clear()
        return await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
    url = text
    if not (url.startswith("https://") or url.startswith("http://")):
        return await message.answer(
            "❌ Link http:// yoki https:// bilan boshlanishi kerak (masalan: https://t.me/kanal). "
            "Qaytadan yuboring:"
        )
    cfg = await storage.get()
    cfg["ad_url"] = url
    ok, err = await try_save(bot)
    await state.clear()
    if not ok:
        return await message.answer(f"❌ Saqlashda xatolik: {err}")
    await message.answer("Saqlandi ✅", reply_markup=main_menu_kb())


@router.callback_query(F.data == "ad:clear")
async def cb_ad_clear(call: CallbackQuery, bot: Bot):
    cfg = await storage.get()
    cfg["ad_text"] = None
    cfg["ad_url"] = None
    ok, err = await try_save(bot)
    if not ok:
        return await call.answer(f"❌ Saqlashda xatolik: {err}", show_alert=True)
    await call.answer("O'chirildi ✅", show_alert=True)
    await call.message.edit_text("BOSS, nima qilmoqchisiz?", reply_markup=main_menu_kb())


# ---- 7) Broadcast — barcha /start bosgan foydalanuvchilarga to'g'ridan-to'g'ri xabar ----
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

    # MUHIM: bu yerda broadcast'ni to'g'ridan-to'g'ri KUTIB turmaymiz — aks holda
    # webhook javobi minutlab kutib qoladi, Telegram/Render buni "timeout" deb
    # hisoblab, YANA bir marta xuddi shu update'ni yuborishi mumkin (natijada
    # broadcast IKKI MARTA ishga tushadi). Fon vazifasi (background task) qilib
    # ishga tushiramiz, webhook esa darhol javob qaytaradi.
    await call.message.edit_text(
        f"📣 Fon rejimida yuborilmoqda: {len(user_ids)} ta foydalanuvchi.\n"
        "Jarayon holatini shu yerga yozib boraman."
    )
    await call.answer()
    asyncio.create_task(run_broadcast(bot, call.message.chat.id, src_chat_id, src_message_id, user_ids))


async def run_broadcast(bot: Bot, admin_chat_id: int, src_chat_id: int, src_message_id: int, user_ids: list[int]):
    sent = failed = 0
    blocked: list[int] = []
    total = len(user_ids)

    for i, uid in enumerate(user_ids, 1):
        for _retry in range(3):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
                sent += 1
                break
            except TelegramRetryAfter as e:
                # Telegram flood-limit signali — aynan shuncha kutish shart
                await asyncio.sleep(e.retry_after + 0.5)
            except TelegramForbiddenError:
                # User botni bloklagan/o'chirgan — ro'yxatdan olib tashlaymiz
                failed += 1
                blocked.append(uid)
                break
            except Exception as e:
                failed += 1
                logger.warning(f"Broadcast xatolik ({uid}): {e}")
                break
        await asyncio.sleep(0.04)  # ~25 xabar/soniya — Telegram limitidan xavfsiz zaxira bilan
        if i % 500 == 0 or i == total:
            try:
                await bot.send_message(admin_chat_id, f"⏳ {i}/{total} | ✅ {sent} | ❌ {failed}")
            except Exception:
                pass

    if blocked:
        cfg = await storage.get()
        blocked_set = set(blocked)
        cfg["users"] = [u for u in cfg.get("users", []) if u not in blocked_set]
        ok, err = await try_save(bot)
        if not ok:
            logger.error(f"Bloklagan userlarni tozalashda xatolik: {err}")

    try:
        await bot.send_message(
            admin_chat_id,
            f"✅ Broadcast tugadi!\nYuborildi: {sent}\n"
            f"Yetkazilmadi: {failed} (shundan {len(blocked)} ta botni bloklagan, ro'yxatdan olib tashlandi)",
        )
    except Exception:
        pass

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
        ok, err = await try_save(bot)
        if not ok:
            logger.error(f"Yangi kanal ro'yxatga olinmadi ({cid}): {err}")
    else:
        if info.get("title") != message.chat.title:
            info["title"] = message.chat.title  # nomini yangilaymiz VA saqlaymiz
            ok, err = await try_save(bot)
            if not ok:
                logger.error(f"Kanal nomini yangilashda xatolik ({cid}): {err}")

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
        ok, err = await try_save(bot)
        if not ok:
            logger.error(f"Yangi kanal ro'yxatga olinmadi ({cid}): {err}")
        else:
            logger.info(f"Yangi kanal ro'yxatga olindi: {update.chat.title} ({cid})")

# ----------------------------------------------------------------------------
# WEBHOOK / RENDER ISHGA TUSHIRISH
# ----------------------------------------------------------------------------
async def on_startup(bot: Bot):
    await storage.load(bot)

    # set_webhook muvaffaqiyatsiz bo'lsa ham dastur qulamasligi kerak —
    # aks holda Render uni qayta-qayta restart qiladi va webhook doim bo'sh qoladi.
    for attempt in range(1, 6):
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            ok = await bot.set_webhook(
                WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=True,
            )
            info = await bot.get_webhook_info()
            # DIQQAT: info.url ni logga chiqarmaymiz — u BOT_TOKEN o'rniga endi
            # hash bo'lsa ham, umuman token/manzilni loglarga yozmaslik yaxshiroq odat.
            logger.info(
                f"Webhook o'rnatildi (attempt {attempt}): ok={ok}, url_set={bool(info.url)}, "
                f"last_error={info.last_error_message!r}"
            )
            if info.url:
                break
        except Exception as e:
            logger.error(f"Webhook o'rnatishda xatolik (attempt {attempt}/5): {e}")
            await asyncio.sleep(3)
    else:
        logger.error("Webhookni 5 urinishdan keyin ham o'rnatib bo'lmadi! Server baribir ishga tushadi.")

    # Fon vazifalari: webhook o'z-o'zini davolashi + user ro'yxati debounced saqlanishi
    asyncio.create_task(webhook_self_heal(bot))
    asyncio.create_task(storage.flush_loop(bot))


async def webhook_self_heal(bot: Bot):
    """Har 10 daqiqada webhook holatini tekshiradi va kerak bo'lsa qayta o'rnatadi.
    Bu Render qayta ishga tushishi / vaqtinchalik tarmoq xatolari tufayli
    webhook 'chalkash' bo'lib qolib, bot javob bermay qo'yishining oldini oladi."""
    while True:
        await asyncio.sleep(600)
        try:
            info = await bot.get_webhook_info()
            if info.url != WEBHOOK_URL:
                logger.warning("Webhook manzili mos emas, qayta o'rnatilmoqda...")
                await bot.set_webhook(
                    WEBHOOK_URL,
                    secret_token=WEBHOOK_SECRET,
                    allowed_updates=dp.resolve_used_update_types(),
                )
        except Exception as e:
            logger.error(f"Webhook self-heal xatolik: {e}")


async def on_shutdown(bot: Bot):
    # Webhookni QASDDAN o'chirmaymiz: Render odatda "rolling deploy" qiladi —
    # yangi instance to'liq ishga tushib bo'lgandan KEYIN eski instance to'xtaydi.
    # Agar shu yerda webhookni o'chirsak, yangi instance allaqachon o'rnatgan
    # to'g'ri webhookni ham o'chirib qo'yamiz — bu aynan botni "javob bermay
    # qolish" holatiga olib keladi. Shuning uchun bu YERDA hech narsa qilinmaydi;
    # to'g'ri manzil har doim on_startup + webhook_self_heal orqali kafolatlanadi.
    logger.info("Bot to'xtatilmoqda (webhook saqlab qolindi).")


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


async def health(request: web.Request):
    # UptimeRobot shu yerga (asosiy "/" manzilga) GET yoki HEAD so'rov yuboradi.
    return web.Response(text="OK")


def main():
    app = web.Application()
    app.router.add_get("/", health)  # allow_head=True standart bo'lgani uchun HEAD ham ishlaydi (UptimeRobot uchun)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
