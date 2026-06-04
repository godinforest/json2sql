import json
import os
import logging
from typing import Dict, Tuple

from telegram import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    Application as PTBApp,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from auth import (
    AuthorizationManager,
    CHOOSE_LANG,
    PET_COUNT,
    PET_TYPE,
    PET_NAME,
    FEEDS_PER_DAY,
    PORTION,
    INTERVAL,
    FIRST_TIME,
)
from feeding_core import MainMenuBot
from locales import LOCALES
from models import UserProfile
from stats import StatsService

logger = logging.getLogger(__name__)

class FeedingApp:
    """Wire-up of Authorization, Main menu, Stats and Telegram handlers."""

    def __init__(self, token: str, data_dir: str = "data") -> None:
        self._data_dir = data_dir
        os.makedirs(self._data_dir, exist_ok=True)
        self._profiles: Dict[Tuple[int, int], UserProfile] = {}

        def _on_profile_saved(profile: UserProfile) -> None:
            self._profiles[(profile.chat_id, profile.user_id)] = profile

        self._auth = AuthorizationManager(self._data_dir, on_profile_saved=_on_profile_saved)
        self._stats = StatsService(self._data_dir)

        # Preload existing profiles
        for fn in os.listdir(self._data_dir):
            if not fn.endswith("_profile.json"):
                continue
            path = os.path.join(self._data_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                d.setdefault("chat_id", d.get("chat_id", 0))
                d.setdefault("user_id", d.get("user_id", 0))
                prof = UserProfile.from_legacy_dict(d)
                self._profiles[(prof.chat_id, prof.user_id)] = prof
            except Exception:
                continue

        self._bot = MainMenuBot(self._profiles, self._stats, self._auth)
        self.app: PTBApp = Application.builder().token(token).build()
        self._register_handlers()

    def _chat_has_profile(self, chat_id: int) -> bool:
        for (c_id, _u), _p in self._profiles.items():
            if c_id == chat_id:
                return True
        return False

    def _chat_profile_language(self, chat_id: int) -> str:
        for (c_id, _u), p in self._profiles.items():
            if c_id == chat_id:
                return p.language
        return "en"

    async def _setup_entry(self, update, context):
        chat_id = update.effective_chat.id
        if self._chat_has_profile(chat_id):
            lang = self._chat_profile_language(chat_id)
            msg_map = {
                "ru": "Аккаунт уже существует в этом чате.\nОбновить данные? /updateprofile\nСтереть? /fullreset",
                "en": "An account already exists in this chat.\nUpdate? /updateprofile\nErase? /fullreset",
                "es": "Ya existe una cuenta en este chat.\n¿Actualizar? /updateprofile\n¿Borrar? /fullreset",
            }
            await update.message.reply_text(msg_map.get(lang, msg_map["en"]))
            return ConversationHandler.END
        return await self._auth.start_onboarding(update, context)

    async def _setup_entry_from_settings(self, update, context):
        query = update.callback_query
        await query.answer()
        class _U:
            def __init__(self, q):
                self.message = q.message
                self.effective_chat = q.message.chat
                self.effective_user = q.from_user
        return await self._setup_entry(_U(query), context)

    async def _updateprofile_entry_from_settings(self, update, context):
        query = update.callback_query
        await query.answer()
        class _U:
            def __init__(self, q):
                self.message = q.message
                self.effective_chat = q.message.chat
                self.effective_user = q.from_user
        return await self._auth.start_update_profile(_U(query), context)

    async def _fullreset_prompt(self, update, context):
        chat_id = update.effective_chat.id
        lang = self._chat_profile_language(chat_id)
        msg = {
            "ru": "Уверены, что хотите сбросить все данные этого чата?",
            "en": "Are you sure you want to reset all data for this chat?",
            "es": "¿Seguro que desea restablecer todos los datos de este chat?",
        }[lang]
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(LOCALES[lang]["confirm_yes"], callback_data="fr_yes"),
                InlineKeyboardButton(LOCALES[lang]["confirm_no"], callback_data="fr_no"),
            ]]
        )
        await update.message.reply_text(msg, reply_markup=kb)

    async def _fullreset_cb(self, update, context):
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat_id
        lang = self._chat_profile_language(chat_id)

        if query.data == "fr_no":
            await query.message.reply_text(LOCALES[lang].get("cancelled", "Cancelled."))
            return

        done_map = {
            "ru": "Все данные чата удалены. Профили, кормления и статистика очищены. \n/setup",
            "en": "All chat data removed. Profiles, feedings and statistics were wiped. \n/setup",
            "es": "Todos los datos del chat fueron eliminados. Perfiles, comidas y estadísticas borrados. \n/setup",
        }

        try:
            self._bot.full_reset_state(chat_id)
        except Exception:
            pass

        try:
            to_del = [(c, u) for (c, u) in list(self._profiles.keys()) if c == chat_id]
            for key in to_del:
                self._profiles.pop(key, None)

            for fn in os.listdir(self._data_dir):
                if not fn.endswith("_profile.json"):
                    continue
                path = os.path.join(self._data_dir, fn)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                if isinstance(d, dict) and int(d.get("chat_id", -1)) == chat_id:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            self._stats.purge_chat(chat_id)
        except Exception:
            pass

        await query.message.reply_text(done_map.get(lang, done_map["en"]))

    def _register_handlers(self) -> None:
        conv = ConversationHandler(
            entry_points=[
                CommandHandler("setup", self._setup_entry),
                CallbackQueryHandler(self._setup_entry_from_settings, pattern=r"^settings_setup_enter$"),
            ],
            states={
                CHOOSE_LANG: [CallbackQueryHandler(self._auth.choose_language, pattern=r"^lang_")],
                PET_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._auth.set_pet_count)],
                PET_TYPE: [CallbackQueryHandler(self._auth.pick_pet_type, pattern=r"^pet_")],
                PET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._auth.set_pet_name)],
                FEEDS_PER_DAY: [CallbackQueryHandler(self._auth.set_feedings_per_day, pattern=r"^fpd_")],
                PORTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._auth.set_portion)],
                INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._auth.set_interval)],
                FIRST_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._auth.set_first_time)],
            },
            fallbacks=[],
            allow_reentry=True,
        )
        self.app.add_handler(conv)

        self.app.add_handler(CommandHandler("menu", self._bot.handle_start))
        self.app.add_handler(CommandHandler("stats", self._bot.handle_stats))
        self.app.add_handler(CommandHandler("updateprofile", self._auth.start_update_profile))
        self.app.add_handler(CommandHandler("fullreset", self._fullreset_prompt))
        self.app.add_handler(CallbackQueryHandler(self._updateprofile_entry_from_settings, pattern=r"^settings_updateprofile_enter$"))
        self.app.add_handler(CallbackQueryHandler(self._fullreset_cb, pattern=r"^fr_(yes|no)$"))
        
        # Legacy update callbacks
        self.app.add_handler(CallbackQueryHandler(self._auth.update_profile_callback, pattern=r"^(upd_|setlang_|setpet_|setfpd_)"))
        
        # Main callbacks & Text input
        self.app.add_handler(CallbackQueryHandler(self._bot.handle_callbacks))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._bot.handle_replan_input))

        async def on_error(update, context):
            # English comments: Ignore typical long-polling timeout spam
            if isinstance(context.error, (TimedOut, NetworkError)):
                return 
            logger.error("Error occurred: %s", context.error)

        self.app.add_error_handler(on_error)

    def run(self) -> None:
        async def post_init(app: PTBApp):
            await app.bot.set_my_commands([
                BotCommand("menu", "Open main menu"),
                BotCommand("stats", "Download XLS stats"),
                BotCommand("updateprofile", "Update profile via wizard"),
                BotCommand("setup", "Setup your profile"),
                BotCommand("fullreset", "Delete profile and stats completely"),
            ])

        self.app.post_init = post_init
        # English comments: Drop old updates and set a timeout to prevent hanging
        self.app.run_polling(drop_pending_updates=True, timeout=30)