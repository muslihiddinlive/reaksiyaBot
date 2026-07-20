# Reaksiya Bot

Telegram kanal/guruh reaksiya boti. aiogram 3, webhook, Render.com uchun tayyor.

## Xususiyatlar

- **Umumiy kanal reaksiyasi** — maxsus sozlanmagan barcha kanallarda ishlaydigan reaksiyalar (admin har bir kanal uchun alohida yoqib/o'chirib qo'ya oladi).
- **Maxsus kanal reaksiyasi** — muayyan kanal uchun boshqacha reaksiya to'plami (belgilansa, umumiy reaksiyadan ustun turadi).
- **BOSS reaksiyasi (guruhda)** — admin (BOSS) biror guruhga yozganda, bot uning xabariga avtomatik shu reaksiyani qo'yadi.
- **Start xabari** — oddiy foydalanuvchi /start bosganda ko'radigan matn, admin panel orqali tahrirlanadi.
- **Majburiy kanal** — botdan foydalanish uchun avval obuna bo'lish shart bo'lgan kanal.
- **Reklama** — foydalanuvchi start bosganda ko'rsatiladigan qo'shimcha matn + tugma (link).
- **2 admin**: SUPERADMIN va ADMIN — ikkalasi ham bir xil huquqqa ega, ikkalasiga ham botda "BOSS" deb murojaat qilinadi. Botda "adminlar ro'yxati" kabi funksiya yo'q, shuning uchun ADMIN superadminning borligini bilmaydi.
- **Konfiguratsiya alohida DB talab qilmaydi** — maxfiy Telegram guruhida (bot admin bo'lgan) pinned xabar orqali saqlanadi, har bir o'zgarishda avtomatik yangilanadi.

## Kerakli reaksiyalar haqida eslatma

Telegram Bot API faqat quyidagi standart emoji ro'yxatidagi reaksiyalarni qo'yishga ruxsat beradi (`bot.py` ichidagi `ALL_REACTIONS`) — bular "barcha default reaksiyalar" hisoblanadi va kanallarda to'liq ishlaydi.

## O'rnatish

### 1. Bot yaratish
@BotFather orqali bot yarating, `BOT_TOKEN` oling.
`/setjoingroups`, kanal/guruhlarga admin sifatida qo'shish huquqini yoqing.

### 2. Storage guruh
Yangi **maxfiy Telegram guruh** yarating (masalan "Bot Storage"), botni o'sha yerga qo'shing va **admin** qiling (kamida "Xabarlarni pin qilish" huquqi bilan). Guruh ID'sini oling (masalan `@userinfobot` yoki `@RawDataBot` yordamida) — bu `STORAGE_CHAT_ID` (odatda `-100...` bilan boshlanadi).

### 3. Admin ID'lar
O'zingizning va ikkinchi adminning Telegram user ID'sini oling (`@userinfobot`).

### 4. Kanallar / guruhlar
Botni reaksiya qo'yishi kerak bo'lgan barcha kanallarga **admin** qilib qo'shing (kamida "Xabarlarga reaksiya qo'yish" huquqi bilan). Bot avtomatik ravishda ularni ro'yxatga oladi.

### 5. Render'ga deploy qilish
1. Ushbu papkani GitHub repo'siga yuklang.
2. Render.com'da **New → Web Service** → repo'ni tanlang.
3. Environment variables qo'shing:
   - `BOT_TOKEN` — BotFather'dan olingan token
   - `SUPERADMIN_ID` — superadmin Telegram ID
   - `ADMIN_ID` — oddiy admin Telegram ID
   - `STORAGE_CHAT_ID` — storage guruh ID (masalan `-1001234567890`)
   - `WEBHOOK_HOST` — Render bergan URL, masalan `https://reaction-bot-xxxx.onrender.com` (deploydan keyin to'ldiring, keyin qayta deploy qiling)
4. Build command: `pip install -r requirements.txt`
5. Start command: `python bot.py`
6. Deploy qiling. Birinchi ishga tushishda bot avtomatik webhook o'rnatadi.

> Render Free plan uyquga ketishi mumkin — [UptimeRobot](https://uptimerobot.com) bilan `https://.../` manzilini har 5 daqiqada ping qilib turing (health-check endpoint tayyor).

## Foydalanish

- Admin (BOSS) botga `/start` bossa — inline menyu chiqadi (6 ta bo'lim).
- Oddiy foydalanuvchi `/start` bossa — majburiy kanal tekshiriladi, so'ng start xabari + (bo'lsa) reklama ko'rsatiladi.
