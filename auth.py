import json
import os
import re
from typing import Callable, Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from telegram.ext import ContextTypes, ConversationHandler

from locales import LOCALES, LANG_BUTTON_TO_CODE
from models import UserProfile, PetProfile

# Conversation states
CHOOSE_LANG, PET_COUNT, PET_TYPE, PET_NAME, FEEDS_PER_DAY, PORTION, INTERVAL, FIRST_TIME = range(8)

class AuthorizationManager:
    def __init__(self, data_dir: str = "data", on_profile_saved: Optional[Callable[[UserProfile], None]] = None) -> None:
        self._data_dir = data_dir
        os.makedirs(self._data_dir, exist_ok=True)
        self._on_profile_saved = on_profile_saved

    def _profile_path(self, chat_id: int, user_id: int) -> str:
        return os.path.join(self._data_dir, f"{chat_id}_{user_id}_profile.json")

    def load_profile(self, chat_id: int, user_id: int) -> Optional[UserProfile]:
        path = self._profile_path(chat_id, user_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("chat_id", chat_id)
        d.setdefault("user_id", user_id)
        return UserProfile.from_legacy_dict(d)

    def save_profile(self, profile: UserProfile) -> None:
        path = self._profile_path(profile.chat_id, profile.user_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)

    async def _cleanup_prompt_and_user_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        chat_id = chat.id
        chat_type = getattr(chat, "type", "private")
        user_id = update.effective_user.id

        cleanup = context.chat_data.get("cleanup", {})
        entry = cleanup.pop(user_id, None)
        if entry:
            mid = entry.get("bot_prompt_id")
            if mid:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass

        if chat_type not in ("private",):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception:
                pass

    async def start_onboarding(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        existing = self.load_profile(chat_id, user_id)
        if existing:
            lang = LOCALES.get(existing.language, LOCALES["en"])
            await update.message.reply_text(lang["profile_already_set"])
            return ConversationHandler.END

        ru = LOCALES["ru"]
        keyboard = [[InlineKeyboardButton(b, callback_data=f"lang_{b}")] for b in ru["choose_language_buttons"]]
        await update.message.reply_text(
            ru["choose_language_title"],
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CHOOSE_LANG

    async def choose_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        btn = query.data.split("_", 1)[1]
        lang_code = LANG_BUTTON_TO_CODE.get(btn, "ru")
        context.user_data.clear()
        context.user_data["lang"] = lang_code

        lang = LOCALES[lang_code]
        text = lang.get(
            "ask_pet_count",
            "Сколько животных вы хотите отслеживать? (1–5)" if lang_code == "ru"
            else "How many pets do you want to track? (1–5)" if lang_code == "en"
            else "¿Cuántas mascotas quieres seguir? (1–5)",
        )
        await query.message.edit_text(text)
        return PET_COUNT

    async def set_pet_count(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        lang_code = context.user_data.get("lang", "en")
        lang = LOCALES[lang_code]
        text = (update.message.text or "").strip()

        try:
            n = int(text)
        except ValueError:
            n = 1
        n = max(1, min(5, n))
        context.user_data["pet_count"] = n
        context.user_data["current_pet"] = 1
        context.user_data["pets"] = []

        keyboard = [[InlineKeyboardButton(t, callback_data=f"pet_{t}")] for t in lang["pet_types"]]
        ask_type_text = lang.get("ask_pet_type_one", f"{lang['onboarding_welcome']}\n\n{lang['ask_pet_type']} №1:")
        await update.message.reply_text(
            ask_type_text.format(idx=1),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return PET_TYPE

    async def pick_pet_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        lang = LOCALES[context.user_data["lang"]]
        context.user_data["pet_type"] = query.data.split("_", 1)[1]

        await query.message.reply_text(
            lang["ask_pet_name"],
            reply_markup=ForceReply(selective=True),
        )
        return PET_NAME

    async def set_pet_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        lang = LOCALES[context.user_data["lang"]]
        context.user_data["pet_name"] = (update.message.text or "").strip()

        keyboard = [[InlineKeyboardButton(opt, callback_data=f"fpd_{opt}")] for opt in lang["feedings_per_day_options"]]
        await update.message.reply_text(
            lang["ask_feedings_per_day"],
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return FEEDS_PER_DAY

    async def set_feedings_per_day(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        lang = LOCALES[context.user_data["lang"]]

        opt = query.data.split("_", 1)[1]
        try:
            fpd = int(opt)
        except ValueError:
            fpd = 3
        context.user_data["feedings_per_day"] = max(1, min(24, fpd))

        await query.message.reply_text(
            lang["ask_portion_grams"],
            reply_markup=ForceReply(selective=True),
        )
        return PORTION

    async def set_portion(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        lang = LOCALES[context.user_data["lang"]]
        try:
            grams = int((update.message.text or "0").strip())
        except ValueError:
            grams = 0
        context.user_data["portion_grams"] = max(0, grams)

        await update.message.reply_text(
            lang["ask_interval_hours"],
            reply_markup=ForceReply(selective=True),
        )
        return INTERVAL

    async def set_interval(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        lang = LOCALES[context.user_data["lang"]]
        try:
            hours = int((update.message.text or "0").strip())
        except ValueError:
            hours = 6
        context.user_data["interval_hours"] = max(1, hours)

        await update.message.reply_text(
            lang["ask_first_feed_time"],
            reply_markup=ForceReply(selective=True, input_field_placeholder="HH.MM"),
        )
        return FIRST_TIME

    async def set_first_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        lang_code = context.user_data.get("lang", "en")
        lang = LOCALES[lang_code]
        text = (update.message.text or "").strip()

        m = re.match(r"^\s*(\d{1,2})[:\.](\d{2})\s*$", text)
        if not m:
            msg = {
                "ru": "Неверный формат. Используйте HH.MM или HH:MM (напр. 06:50).",
                "en": "Invalid format. Use HH.MM or HH:MM (e.g., 06:50).",
                "es": "Formato inválido. Usa HH.MM o HH:MM (p. ej., 06:50).",
            }.get(lang_code, "Invalid time format.")
            await update.message.reply_text(msg, reply_markup=ForceReply(selective=True, input_field_placeholder="06:50"))
            return FIRST_TIME

        hh = int(m.group(1))
        mm = int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            msg = {
                "ru": "Часы 00–23, минуты 00–59. Попробуйте ещё раз.",
                "en": "Hours 00–23 and minutes 00–59. Try again.",
                "es": "Horas 00–23 y minutos 00–59. Inténtalo de nuevo.",
            }.get(lang_code, "Invalid time.")
            await update.message.reply_text(msg, reply_markup=ForceReply(selective=True, input_field_placeholder="06:50"))
            return FIRST_TIME

        first_feed_time_hhmm = f"{hh:02d}:{mm:02d}"
        current_idx = context.user_data.get("current_pet", 1)

        pet_dict = {
            "pet_id": current_idx,
            "pet_type": context.user_data.get("pet_type", "pet"),
            "pet_name": context.user_data.get("pet_name", f"pet{current_idx}"),
            "feedings_per_day": context.user_data.get("feedings_per_day", 3),
            "portion_grams": context.user_data.get("portion_grams", 0),
            "interval_hours": context.user_data.get("interval_hours", 6),
            "first_feed_time_hhmm": first_feed_time_hhmm,
        }
        pets = context.user_data.setdefault("pets", [])
        pets.append(pet_dict)

        pet_count = context.user_data.get("pet_count", 1)
        if current_idx < pet_count:
            context.user_data["current_pet"] = current_idx + 1
            for key in ("pet_type", "pet_name", "feedings_per_day", "portion_grams", "interval_hours"):
                context.user_data.pop(key, None)

            keyboard = [[InlineKeyboardButton(t, callback_data=f"pet_{t}")] for t in lang["pet_types"]]
            ask_type_text = lang.get("ask_pet_type_one", f"{lang['onboarding_welcome']}\n\n{lang['ask_pet_type']} №{current_idx + 1}:")
            await update.message.reply_text(
                ask_type_text.format(idx=current_idx + 1),
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return PET_TYPE

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        profile = UserProfile(
            chat_id=chat_id,
            user_id=user_id,
            language=lang_code,
            pets=[PetProfile(**p) for p in pets],
        )
        self.save_profile(profile)
        if self._on_profile_saved:
            try:
                self._on_profile_saved(profile)
            except Exception:
                pass

        await update.message.reply_text(lang["onboarding_done"])
        return ConversationHandler.END

    async def full_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        paths = [
            self._profile_path(chat_id, user_id),
            os.path.join(self._data_dir, f"{chat_id}_feedings.json"),
            os.path.join(self._data_dir, f"{chat_id}_{user_id}_feedings.json"),
            os.path.join(self._data_dir, f"{user_id}_feedings.json"),
        ]
        for p in paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        msg_map = {
            "ru": "✅ Профиль и статистика удалены. Запустите /setup заново.",
            "en": "✅ Profile and statistics cleared. Run /setup again.",
            "es": "✅ Perfil y estadísticas borrados. Ejecuta /setup de nuevo.",
        }
        await update.message.reply_text(msg_map["en"])

    async def start_update_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ru = LOCALES["ru"]
        keyboard = [[InlineKeyboardButton(b, callback_data=f"lang_{b}")] for b in ru["choose_language_buttons"]]
        await update.message.reply_text(
            ru["choose_language_title"],
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CHOOSE_LANG

    async def update_profile_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        await query.message.edit_text(
            "Profile editing via this inline menu is deprecated. "
            "Please use /updateprofile to run the setup wizard again."
        )

    async def update_profile_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        return