# feeding_core.py
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ForceReply,
)
from telegram.ext import ContextTypes

from locales import LOCALES
from models import FeedingRecord, UserProfile, PetProfile
from stats import StatsService
from auth import AuthorizationManager


# ========= Abstract interface =========

class AbstractBot(ABC):
    @abstractmethod
    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ...

    @abstractmethod
    async def handle_callbacks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ...

    @abstractmethod
    async def handle_replan_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ...

    @abstractmethod
    async def handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ...


# ========= Main implementation =========

class MainMenuBot(AbstractBot):
    """
    State и статистика — на уровне чата (любой пользователь чата может редактировать).

    self._chat_state[chat_id] = {
        "date": date,
        "feedings": { str(pet_id): [ {"time": "HH:MM", "user": "Name"}, ... ] },
        "actions": { str(pet_id): [ {"user_id": int, "kind": str, "index": int, ...}, ... ] },
    }
    """

    def __init__(
        self,
        profiles: Dict[Tuple[int, int], UserProfile],
        stats: StatsService,
        auth: AuthorizationManager,
    ) -> None:
        # in-memory cache from FeedingApp
        self._profiles = profiles
        self._stats = stats
        self._auth = auth
        self._chat_state: Dict[int, Dict[str, Any]] = {}

    # ----- profile helpers -----

    def _get_profile(self, chat_id: int, user_id: int) -> Optional[UserProfile]:
        prof = self._profiles.get((chat_id, user_id))
        if prof:
            return prof
        # try any profile from this chat
        for (c_id, _u), p in self._profiles.items():
            if c_id == chat_id:
                return p
        # load from disk
        prof = self._auth.load_profile(chat_id, user_id)
        if prof:
            self._profiles[(chat_id, user_id)] = prof
        return prof

    def _persist_profile(self, profile: UserProfile) -> None:
        """Soft persistence, using AuthorizationManager if possible."""
        save_fn = getattr(self._auth, "save_profile", None)
        if callable(save_fn):
            try:
                save_fn(profile)
                return
            except Exception:
                pass
        
        # Исправлено: корректное получение _data_dir через getattr
        data_dir = getattr(self._auth, "_data_dir", "data")
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, f"{profile.chat_id}_{profile.user_id}_profile.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)

    # ----- chat state helpers -----

    def _ensure_chat(self, chat_id: int) -> None:
        if chat_id not in self._chat_state:
            self._chat_state[chat_id] = {
                "date": datetime.now().date(),
                "feedings": {},   # pet_id -> [ {time,user}, ... ]
                "actions": {},    # pet_id -> [ {user_id,kind,index,...}, ... ]
            }

    def _rollover_day(self, chat_id: int) -> None:
        today = datetime.now().date()
        self._ensure_chat(chat_id)
        if self._chat_state[chat_id]["date"] != today:
            self._chat_state[chat_id] = {
                "date": today,
                "feedings": {},
                "actions": {},
            }

    def _get_pet_feedings(self, chat_id: int, pet_id: int) -> List[Dict[str, str]]:
        self._ensure_chat(chat_id)
        self._rollover_day(chat_id)
        state = self._chat_state[chat_id]
        feedings_by_pet: Dict[str, List[Dict[str, str]]] = state.setdefault("feedings", {})
        return feedings_by_pet.setdefault(str(pet_id), [])

    def _get_pet_actions(self, chat_id: int, pet_id: int) -> List[Dict[str, Any]]:
        self._ensure_chat(chat_id)
        self._rollover_day(chat_id)
        state = self._chat_state[chat_id]
        actions_by_pet: Dict[str, List[Dict[str, Any]]] = state.setdefault("actions", {})
        return actions_by_pet.setdefault(str(pet_id), [])

    def _push_action(self, chat_id: int, pet_id: int, action: Dict[str, Any]) -> None:
        self._get_pet_actions(chat_id, pet_id).append(action)

    def _pop_last_user_action(self, chat_id: int, pet_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        actions = self._get_pet_actions(chat_id, pet_id)
        for i in range(len(actions) - 1, -1, -1):
            if actions[i].get("user_id") == user_id:
                return actions.pop(i)
        return None

    def _find_pet(self, profile: UserProfile, pet_id: int) -> PetProfile:
        for pet in profile.pets:
            if pet.pet_id == pet_id:
                return pet
        # fallback to first pet if something went wrong
        return profile.pets[0]

    def full_reset_state(self, chat_id: int) -> None:
        """Called from FeedingApp on /fullreset."""
        if chat_id in self._chat_state:
            self._chat_state.pop(chat_id, None)

    # ----- notifications state helpers -----

    def _reset_notify_for_pet(self, chat_id: int, pet_id: int) -> None:
        """
        Обнуляет маску уведомлений для текущего дня и указанного питомца:
            sent -> [False, False, ...]
        + ставит флаг forced_reset, чтобы планировщик НЕ пересобирал маску
          из статистики до конца дня.
        """
        try:
            data_dir = getattr(self._auth, "_data_dir", "data")
            state_path = os.path.join(data_dir, "notify_state.json")
            if not os.path.exists(state_path):
                return
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            if not isinstance(state, dict):
                return

            today = datetime.now().date().isoformat()
            day_bucket = state.get(today)
            if not isinstance(day_bucket, dict):
                return

            chat_key = str(chat_id)
            chat_bucket = day_bucket.get(chat_key)
            if not isinstance(chat_bucket, dict):
                return

            pet_key = str(pet_id)
            pet_state = chat_bucket.get(pet_key)
            if not isinstance(pet_state, dict):
                return

            sent = pet_state.get("sent")
            if isinstance(sent, list) and sent:
                pet_state["sent"] = [False] * len(sent)
                pet_state["forced_reset"] = True
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            # не ломаем бота из-за проблем с файлом уведомлений
            pass

    def _clear_today_stats_for_pet(self, chat_id: int, pet_name: str) -> None:
        """
        Удаляет из <chat_id>_feedings.json ВСЕ записи за сегодня для указанного питомца.
        Это нужно, чтобы статистика за день начиналась с нуля.
        """
        try:
            data_dir = getattr(self._auth, "_data_dir", "data")
            path = os.path.join(data_dir, f"{chat_id}_feedings.json")
            if not os.path.exists(path):
                return

            with open(path, "r", encoding="utf-8") as f:
                arr = json.load(f)

            if not isinstance(arr, list):
                return

            today = datetime.now().date().isoformat()
            prefix = f"{pet_name} #"

            new_arr: List[Dict[str, Any]] = []
            for rec in arr:
                day = rec.get("day_iso")
                meal_name = str(rec.get("meal_name", "")).strip()
                # старый формат, где meal_name = "1", "2", ...
                is_old_single = meal_name.isdigit()
                if day == today and (meal_name.startswith(prefix) or is_old_single):
                    continue
                new_arr.append(rec)

            if len(new_arr) != len(arr):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(new_arr, f, ensure_ascii=False, indent=2)
        except Exception:
            # не хотим ронять бота из-за проблем со статистикой
            pass

    # ----- keyboards -----

    def _main_keyboard(self, lang_code: str) -> InlineKeyboardMarkup:
        lang = LOCALES[lang_code]
        keyboard = [
            [InlineKeyboardButton(lang["menu_feed"], callback_data="feed")],
            [InlineKeyboardButton(lang["menu_when"], callback_data="last_feed")],
            [InlineKeyboardButton(lang["menu_reset"], callback_data="reset_menu")],
            [InlineKeyboardButton(lang["menu_manual"], callback_data="manual_feed")],
            [InlineKeyboardButton(lang.get("menu_settings", "Settings"), callback_data="open_settings")],
        ]
        return InlineKeyboardMarkup(keyboard)

    def _pet_choice_keyboard(self, profile: UserProfile, action: str) -> InlineKeyboardMarkup:
        """
        action: 'feed', 'last_feed', 'reset_menu', 'manual_feed', 'notif_menu'
        callback_data: f'{action}_pet_{pet_id}'
        """
        buttons = []
        for pet in profile.pets:
            cb = f"{action}_pet_{pet.pet_id}"
            buttons.append([InlineKeyboardButton(pet.pet_name, callback_data=cb)])
        return InlineKeyboardMarkup(buttons)

    def _reset_menu_keyboard(self, lang_code: str, pet_id: int) -> InlineKeyboardMarkup:
        lang = LOCALES[lang_code]
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(lang["reset_last"], callback_data=f"reset_last_pet_{pet_id}")],
            [InlineKeyboardButton(lang["reset_day"], callback_data=f"reset_day_pet_{pet_id}")],
            [InlineKeyboardButton(lang["reset_replan"], callback_data=f"reset_replan_pet_{pet_id}")],
            [InlineKeyboardButton(lang["reset_cancel"], callback_data="reset_cancel")],
        ])

    def _replan_keyboard(self, profile: UserProfile, pet: PetProfile, feedings: List[Dict[str, str]]) -> InlineKeyboardMarkup:
        """
        Предлагаем выбрать номер приёма пищи (1..feedings_per_day).
        """
        lang = LOCALES[profile.language]
        buttons = []
        for i in range(1, pet.feedings_per_day + 1):
            idx = i - 1
            label = str(i)
            if idx < len(feedings) and feedings[idx].get("time") and feedings[idx]["time"] != "––":
                label = f"{i} ({feedings[idx]['time']})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"replan_pet_{pet.pet_id}_idx_{i}")])
        # Добавляем "Отмена"
        buttons.append([InlineKeyboardButton(lang["reset_cancel"], callback_data="reset_cancel")])
        return InlineKeyboardMarkup(buttons)

    def _settings_keyboard(self, lang_code: str) -> InlineKeyboardMarkup:
        """
        Главное меню настроек.
        """
        lang = LOCALES[lang_code]
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(lang.get("settings_notifications", "Notifications"), callback_data="settings_notifications")],
            [InlineKeyboardButton(lang.get("settings_stats", "Stats"), callback_data="settings_stats")],
            [InlineKeyboardButton(lang.get("settings_updateprofile", "Update profile"), callback_data="settings_updateprofile_enter")],
            [InlineKeyboardButton(lang.get("settings_setup", "Initial setup"), callback_data="settings_setup_enter")],
            [InlineKeyboardButton(lang.get("settings_fullreset", "Full reset"), callback_data="settings_fullreset")],
            [InlineKeyboardButton(lang.get("settings_back", "Back"), callback_data="settings_back")],
        ])

    def _notif_keyboard(self, lang_code: str, pet_id: int) -> InlineKeyboardMarkup:
        """Подменю настроек уведомлений для конкретного питомца."""
        lang = LOCALES[lang_code]
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(lang.get("notif_set_first", "First feed time"), callback_data=f"notif_set_first_pet_{pet_id}")],
            [InlineKeyboardButton(lang.get("notif_set_interval", "Interval (hours)"), callback_data=f"notif_set_interval_pet_{pet_id}")],
            [InlineKeyboardButton(lang.get("notif_set_message", "Notification text"), callback_data=f"notif_set_message_pet_{pet_id}")],
            [InlineKeyboardButton(lang.get("settings_back", "Back"), callback_data="settings_notifications")],
        ])

    # ----- formatting helpers -----

    @staticmethod
    def _add_hours_to_hhmm(hhmm: str, hours: int) -> str:
        try:
            h, m = map(int, hhmm.split(":"))
        except Exception:
            return "––"
        base = datetime(2000, 1, 1, h, m)
        return (base + timedelta(hours=hours)).strftime("%H:%M")

    def _format_status(self, profile: UserProfile, pet: PetProfile, feedings: List[Dict[str, str]]) -> str:
        lang = LOCALES[profile.language]
        title = lang.get("status_header", "Feeding status for today:")
        lines = [f"{title} {pet.pet_name} ({pet.feedings_per_day})"]

        done_indices = [
            i for i in range(len(feedings))
            if feedings[i].get("time") and feedings[i]["time"] != "––"
        ]
        last_done_idx = done_indices[-1] if done_indices else None

        next_time_hint: Optional[str] = None
        if last_done_idx is not None and (last_done_idx + 1) < pet.feedings_per_day:
            last_time = feedings[last_done_idx]["time"]
            try:
                interval_h = int(round(float(pet.interval_hours)))
            except Exception:
                interval_h = 0
            if interval_h > 0 and last_time and last_time != "––":
                next_time_hint = self._add_hours_to_hhmm(last_time, interval_h)

        for i in range(1, pet.feedings_per_day + 1):
            idx = i - 1
            if idx < len(feedings) and feedings[idx].get("time") and feedings[idx]["time"] != "––":
                f = feedings[idx]
                who = f.get("user", "-")
                lines.append(f"{i}. ✅ {f['time']} ({who})")
            else:
                if last_done_idx is not None and idx == last_done_idx + 1 and next_time_hint:
                    lines.append(f"{i}. {next_time_hint}")
                else:
                    lines.append(f"{i}. ––")

        if last_done_idx is None:
            lines.append("")
            lines.append(lang.get("status_no_feedings", "No feedings recorded yet today."))
        elif last_done_idx + 1 >= pet.feedings_per_day:
            lines.append("")
            lines.append(lang.get("status_completed_all", "All planned feedings are done."))
        elif next_time_hint:
            lines.append("")
            lines.append(
                lang.get("status_upcoming", "Next planned feeding is around {time}.").format(
                    time=next_time_hint
                )
            )
        return "\n".join(lines)

    # ----- common helpers -----

    async def _send_new_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str, lang_code: str) -> None:
        """
        Отправляет новое сообщение с меню, предварительно удаляя старое (если есть).
        Используется для:
          - /start (/menu)
          - после кормления
          - после показа статуса ("Когда кормили?")
        """
        last_id = context.chat_data.get("menu_message_id")
        if last_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=last_id)
            except Exception:
                pass

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=self._main_keyboard(lang_code),
        )
        context.chat_data["menu_message_id"] = msg.message_id

    # ========= public handlers =========

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        profile = self._get_profile(chat_id, user_id)
        if not profile:
            await update.message.reply_text("Run /setup first to configure your profile.")
            return

        lang = LOCALES[profile.language]
        text = lang.get("welcome", "Menu")
        await self._send_new_menu(chat_id, context, text, profile.language)

    async def handle_callbacks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat_id
        user_id = query.from_user.id

        profile = self._get_profile(chat_id, user_id)
        if not profile:
            await query.message.edit_text("Run /setup first.")
            return

        lang = LOCALES[profile.language]
        self._ensure_chat(chat_id)
        self._rollover_day(chat_id)
        data = query.data or ""

        # ----- main menu: choose pet first -----

        if data == "feed":
            await query.message.edit_text(
                lang["choose_pet_for_action"],
                reply_markup=self._pet_choice_keyboard(profile, "feed"),
            )
            return

        if data.startswith("feed_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            feedings = self._get_pet_feedings(chat_id, pet_id)

            if len(feedings) >= pet.feedings_per_day:
                await query.answer(lang.get("already_fed_limit", "All planned feedings are done."), show_alert=True)
                text = self._format_status(profile, pet, feedings)
                await self._send_new_menu(chat_id, context, text, profile.language)
                return

            now = datetime.now()
            feedings.append({
                "time": now.strftime("%H:%M"),
                "user": query.from_user.first_name,
            })
            self._push_action(chat_id, pet_id, {"user_id": user_id, "kind": "feed", "index": len(feedings)})

            # stats record
            self._stats.append_record(chat_id, FeedingRecord(
                day_iso=now.date().isoformat(),
                time_hhmm=now.strftime("%H:%M"),
                fed=True,
                who=query.from_user.first_name,
                portion_grams=pet.portion_grams,
                meal_name=f"{pet.pet_name} #{len(feedings)}",
            ))

            text = self._format_status(profile, pet, feedings)
            await self._send_new_menu(chat_id, context, text, profile.language)
            return

        if data == "last_feed":
            await query.message.edit_text(
                lang["choose_pet_for_action"],
                reply_markup=self._pet_choice_keyboard(profile, "last_feed"),
            )
            return

        if data.startswith("last_feed_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            feedings = self._get_pet_feedings(chat_id, pet_id)
            text = self._format_status(profile, pet, feedings)
            await self._send_new_menu(chat_id, context, text, profile.language)
            return

        # ----- reset menu -----

        if data == "reset_menu":
            await query.message.edit_text(
                lang["reset_menu_title"],
                reply_markup=self._pet_choice_keyboard(profile, "reset_menu"),
            )
            return

        if data.startswith("reset_menu_pet_"):
            pet_id = int(data.split("_")[-1])
            await query.message.edit_text(
                lang["reset_menu_title"],
                reply_markup=self._reset_menu_keyboard(profile.language, pet_id),
            )
            return

        if data.startswith("reset_last_pet_"):
            pet_id = int(data.split("_")[-1])
            # Сразу применяем: отменяем последнее действие
            action = self._pop_last_user_action(chat_id, pet_id, user_id)
            pet = self._find_pet(profile, pet_id)
            feedings = self._get_pet_feedings(chat_id, pet_id)

            if not action:
                msg = {
                    "ru": "Сегодня нечего отменять.",
                    "en": "Nothing to cancel today.",
                    "es": "Nada para cancelar hoy.",
                }[profile.language]
                await query.answer(msg, show_alert=True)
            else:
                kind = action.get("kind")
                idx = action.get("index")
                if kind == "feed" and idx and 0 < idx <= len(feedings):
                    feedings.pop(idx - 1)

            text = self._format_status(profile, pet, feedings)
            await query.message.edit_text(
                text,
                reply_markup=self._reset_menu_keyboard(profile.language, pet_id),
            )
            return

        if data.startswith("reset_day_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            feedings = self._get_pet_feedings(chat_id, pet_id)
            feedings.clear()
            actions = self._get_pet_actions(chat_id, pet_id)
            actions.clear()

            # сбросить уведомления и статистику за сегодня для этого питомца
            self._reset_notify_for_pet(chat_id, pet_id)
            self._clear_today_stats_for_pet(chat_id, pet.pet_name)

            text = self._format_status(profile, pet, feedings)
            await query.message.edit_text(
                text,
                reply_markup=self._reset_menu_keyboard(profile.language, pet_id),
            )
            return

        if data.startswith("reset_replan_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            feedings = self._get_pet_feedings(chat_id, pet_id)
            await query.message.edit_text(
                lang.get("reset_pick_meal", "Choose an entry to replan:"),
                reply_markup=self._replan_keyboard(profile, pet, feedings),
            )
            return

        if data == "reset_cancel":
            # Просто возвращаем основное меню (без нового сообщения)
            text = lang.get("welcome", "Menu")
            await query.message.edit_text(
                text,
                reply_markup=self._main_keyboard(profile.language),
            )
            return

        # ----- manual feed ("Забыл покормить") -----

        if data == "manual_feed":
            await query.message.edit_text(
                lang["choose_pet_for_action"],
                reply_markup=self._pet_choice_keyboard(profile, "manual_feed"),
            )
            return

        if data.startswith("manual_feed_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            feedings = self._get_pet_feedings(chat_id, pet_id)
            await query.message.edit_text(
                lang.get("reset_pick_meal", "Choose an entry to replan:"),
                reply_markup=self._replan_keyboard(profile, pet, feedings),
            )
            # пометим, что это ручное кормление (в отличие от простого перепланирования)
            sessions = context.chat_data.setdefault("replan_sessions", {})
            sessions[user_id] = {"pet_id": pet_id, "mode": "manual"}
            return

        if data.startswith("replan_pet_"):
            # выбор конкретного индекса для manual/reset_replan
            m = re.match(r"replan_pet_(\d+)_idx_(\d+)", data)
            if not m:
                return
            pet_id = int(m.group(1))
            idx = int(m.group(2))

            sessions = context.chat_data.setdefault("replan_sessions", {})
            sess = sessions.get(user_id, {})
            sess.update({"pet_id": pet_id, "index": idx})
            sessions[user_id] = sess

            prompt = {
                "ru": f"Введите время для пункта {idx} в формате HH.MM или HH:MM:",
                "en": f"Enter time for item {idx} as HH.MM or HH:MM:",
                "es": f"Introduce la hora para el ítem {idx} en formato HH.MM o HH:MM:",
            }[profile.language]

            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt,
                reply_markup=ForceReply(selective=True, input_field_placeholder="HH.MM or HH:MM"),
            )
            return

        # ----- settings -----

        if data == "open_settings":
            await query.message.edit_text(
                lang.get("settings_title", "Settings"),
                reply_markup=self._settings_keyboard(profile.language),
            )
            return

        if data == "settings_back":
            text = lang.get("welcome", "Menu")
            await query.message.edit_text(
                text,
                reply_markup=self._main_keyboard(profile.language),
            )
            return

        if data == "settings_notifications":
            await query.message.edit_text(
                lang.get("notif_choose_pet", "Choose a pet to configure notifications:"),
                reply_markup=self._pet_choice_keyboard(profile, "notif_menu"),
            )
            return

        if data.startswith("notif_menu_pet_"):
            pet_id = int(data.split("_")[-1])
            await query.message.edit_text(
                lang.get("notif_menu_title", "Notifications settings"),
                reply_markup=self._notif_keyboard(profile.language, pet_id),
            )
            return

        if data.startswith("notif_set_first_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            prompt = {
                "ru": f"Введите время первого кормления для {pet.pet_name} в формате HH.MM или HH:MM:",
                "en": f"Enter first feeding time for {pet.pet_name} in HH.MM or HH:MM:",
                "es": f"Introduce la hora de la primera comida de {pet.pet_name} en HH.MM o HH:MM:",
            }[profile.language]
            sessions = context.chat_data.setdefault("notif_sessions", {})
            sessions[user_id] = {"mode": "first_time", "pet_id": pet_id}
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt,
                reply_markup=ForceReply(selective=True, input_field_placeholder="06:50"),
            )
            return

        if data.startswith("notif_set_interval_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            prompt = {
                "ru": f"Введите НОВЫЙ интервал (в часах) между кормлениями для {pet.pet_name}:",
                "en": f"Enter NEW interval (in hours) between feedings for {pet.pet_name}:",
                "es": f"Introduce el NUEVO intervalo (en horas) entre comidas para {pet.pet_name}:",
            }[profile.language]
            sessions = context.chat_data.setdefault("notif_sessions", {})
            sessions[user_id] = {"mode": "interval", "pet_id": pet_id}
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt,
                reply_markup=ForceReply(selective=True, input_field_placeholder="6"),
            )
            return

        if data.startswith("notif_set_message_pet_"):
            pet_id = int(data.split("_")[-1])
            pet = self._find_pet(profile, pet_id)
            prompt = {
                "ru": f"Введите текст уведомления для {pet.pet_name} (можно использовать {{pet}}):",
                "en": f"Enter notification text for {pet.pet_name} (you may use {{pet}}):",
                "es": f"Introduce el texto de notificación para {pet.pet_name} (puedes usar {{pet}}):",
            }[profile.language]
            sessions = context.chat_data.setdefault("notif_sessions", {})
            sessions[user_id] = {"mode": "message", "pet_id": pet_id}
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt,
                reply_markup=ForceReply(selective=True, input_field_placeholder=f"Time to feed {pet.pet_name}!"),
            )
            return

        if data == "settings_stats":
            await self._send_stats_all(chat_id, profile, context)
            await query.message.edit_text(
                lang.get("settings_title", "Settings"),
                reply_markup=self._settings_keyboard(profile.language),
            )
            return

        if data == "settings_updateprofile_enter":
            # то же, что /updateprofile
            fake_update = type(
                "FakeUpdate",
                (),
                {"message": query.message, "effective_chat": query.message.chat, "effective_user": query.from_user},
            )
            await self._auth.start_update_profile(fake_update, context)
            return

        if data == "settings_setup_enter":
            # перезапустить первичную настройку /setup
            fake_update = type(
                "FakeUpdate",
                (),
                {"message": query.message, "effective_chat": query.message.chat, "effective_user": query.from_user},
            )
            await self._auth.start_setup(fake_update, context)
            return

        if data == "settings_fullreset":
            confirm_text = {
                "ru": "Уверены, что хотите сбросить все данные этого чата?",
                "en": "Are you sure you want to reset all data for this chat?",
                "es": "¿Seguro que desea restablecer todos los datos de este chat?",
            }[profile.language]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(LOCALES[profile.language]["confirm_yes"], callback_data="fr_yes"),
                InlineKeyboardButton(LOCALES[profile.language]["confirm_no"], callback_data="fr_no"),
            ]])
            await query.message.edit_text(confirm_text, reply_markup=kb)
            return

    # ----- replan / manual + notifications text input -----

    async def handle_replan_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Обработка текстового ввода после:
          - replan_pet_*_idx_* (Сбросить / Забыл покормить),
          - notif_set_*_pet_* (настройка уведомлений).
        """
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        notif_sessions = context.chat_data.get("notif_sessions", {})
        replan_sessions = context.chat_data.get("replan_sessions", {})
        has_notif = user_id in notif_sessions
        has_replan = user_id in replan_sessions

        if not (has_notif or has_replan):
            return

        profile = self._get_profile(chat_id, user_id)
        if not profile:
            await update.message.reply_text("Run /setup first.")
            notif_sessions.pop(user_id, None)
            replan_sessions.pop(user_id, None)
            context.chat_data["notif_sessions"] = notif_sessions
            context.chat_data["replan_sessions"] = replan_sessions
            return

        lang = LOCALES[profile.language]
        text_in = (update.message.text or "").strip()

        # ----- notifications -----
        if has_notif:
            sess = notif_sessions[user_id]
            mode = sess.get("mode")
            pet_id = int(sess.get("pet_id", 1))
            pet = self._find_pet(profile, pet_id)

            if mode == "first_time":
                m = re.match(r"^\s*(\d{1,2})[:\.](\d{2})\s*$", text_in)
                if not m:
                    msg = {
                        "ru": "Неверный формат. Используйте HH.MM или HH:MM (напр. 06:50).",
                        "en": "Invalid format. Use HH.MM or HH:MM (e.g. 06:50).",
                        "es": "Formato inválido. Usa HH.MM o HH:MM (p. ej. 06:50).",
                    }[profile.language]
                    await update.message.reply_text(
                        msg,
                        reply_markup=ForceReply(selective=True, input_field_placeholder="06:50"),
                    )
                    return
                hh = int(m.group(1))
                mm = int(m.group(2))
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    msg = {
                        "ru": "Часы 00–23, минуты 00–59. Попробуйте ещё раз.",
                        "en": "Hours 00–23 and minutes 00–59. Try again.",
                        "es": "Las horas deben ser 00–23 y los minutos 00–59. Inténtalo de nuevo.",
                    }[profile.language]
                    await update.message.reply_text(
                        msg,
                        reply_markup=ForceReply(selective=True, input_field_placeholder="06:50"),
                    )
                    return

                new_time_str = f"{hh:02d}:{mm:02d}"
                setattr(pet, "first_feed_time_hhmm", new_time_str)
                self._persist_profile(profile)

                ok = lang.get("notif_confirm_first_time", "First reminder time for {pet} set to {time}.").format(
                    pet=pet.pet_name,
                    time=new_time_str,
                )
                await update.message.reply_text(ok, reply_markup=self._settings_keyboard(profile.language))
                notif_sessions.pop(user_id, None)
                context.chat_data["notif_sessions"] = notif_sessions
                return

            if mode == "interval":
                try:
                    new_interval = int(text_in)
                except ValueError:
                    msg = {
                        "ru": "Введите целое число часов (например, 6).",
                        "en": "Enter an integer number of hours (e.g. 6).",
                        "es": "Introduce un número entero de horas (p. ej. 6).",
                    }[profile.language]
                    await update.message.reply_text(
                        msg,
                        reply_markup=ForceReply(selective=True, input_field_placeholder="6"),
                    )
                    return

                new_interval = max(1, new_interval)
                setattr(pet, "interval_hours", new_interval)
                self._persist_profile(profile)

                ok = lang.get("notif_confirm_interval", "Reminder interval for {pet}: {hours} h.").format(
                    pet=pet.pet_name,
                    hours=new_interval,
                )
                await update.message.reply_text(ok, reply_markup=self._settings_keyboard(profile.language))
                notif_sessions.pop(user_id, None)
                context.chat_data["notif_sessions"] = notif_sessions
                return

            if mode == "message":
                custom = text_in
                setattr(pet, "notif_message", custom)
                self._persist_profile(profile)

                ok = lang.get("notif_confirm_message", 'Notification text for {pet} saved: "{message}"').format(
                    pet=pet.pet_name,
                    message=custom,
                )
                await update.message.reply_text(ok, reply_markup=self._settings_keyboard(profile.language))
                notif_sessions.pop(user_id, None)
                context.chat_data["notif_sessions"] = notif_sessions
                return

        # ----- replan / manual -----
        if has_replan:
            sess = replan_sessions[user_id]
            pet_id = int(sess.get("pet_id", 1))
            idx = int(sess.get("index", 1))
            mode = sess.get("mode", "replan")

            m = re.match(r"^\s*(\d{1,2})[:\.](\d{2})\s*$", text_in)
            if not m:
                prompt = {
                    "ru": "Неверный формат. Введите время в виде HH.MM или HH:MM (напр. 08.30):",
                    "en": "Wrong format. Please use HH.MM or HH:MM (e.g. 08.30):",
                    "es": "Formato inválido. Usa HH.MM o HH:MM (ej. 08.30):",
                }[profile.language]
                await update.message.reply_text(
                    prompt,
                    reply_markup=ForceReply(selective=True, input_field_placeholder="HH.MM or HH:MM"),
                )
                return

            hh = int(m.group(1))
            mm = int(m.group(2))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                prompt = {
                    "ru": "Часы должны быть 00–23, минуты 00–59. Попробуйте ещё раз.",
                    "en": "Hours must be 00–23 and minutes 00–59. Try again.",
                    "es": "Las horas deben ser 00–23 y los minutos 00–59. Inténtalo de nuevo.",
                }[profile.language]
                await update.message.reply_text(
                    prompt,
                    reply_markup=ForceReply(selective=True, input_field_placeholder="HH.MM or HH:MM"),
                )
                return

            new_time = f"{hh:02d}:{mm:02d}"

            pet = self._find_pet(profile, pet_id)
            feedings = self._get_pet_feedings(chat_id, pet_id)
            while len(feedings) < idx:
                feedings.append({"time": "––", "user": "-"})

            prev = dict(feedings[idx - 1]) if idx - 1 < len(feedings) else {"time": "––", "user": "-"}
            feedings[idx - 1] = {"time": new_time, "user": update.effective_user.first_name}
            self._push_action(chat_id, pet_id, {"user_id": user_id, "kind": "replan", "index": idx, "prev": prev})

            if mode == "manual":
                self._stats.append_record(chat_id, FeedingRecord(
                    day_iso=datetime.now().date().isoformat(),
                    time_hhmm=new_time,
                    fed=True,
                    who=update.effective_user.first_name,
                    portion_grams=pet.portion_grams,
                    meal_name=f"{pet.pet_name} #{idx} (manual)",
                ))

            replan_sessions.pop(user_id, None)
            context.chat_data["replan_sessions"] = replan_sessions

            confirm = {
                "ru": f"Время для пункта {idx} изменено на {new_time}",
                "en": f"Time for item {idx} updated to {new_time}",
                "es": f"La hora del ítem {idx} se actualizó a {new_time}",
            }[profile.language]

            text = confirm + "\n\n" + self._format_status(profile, pet, feedings)
            await update.message.reply_text(
                text,
                reply_markup=self._main_keyboard(profile.language),
            )

    # ----- stats (shared) -----

    async def handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send XLSX + charts."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        profile = self._get_profile(chat_id, user_id)
        if not profile:
            await context.bot.send_message(update.effective_chat.id, "Run /setup first.")
            return
        await self._send_stats_all(chat_id, profile, context)

    async def _send_stats_all(self, chat_id: int, profile: UserProfile, context: ContextTypes.DEFAULT_TYPE) -> None:
        # XLSX
        if not profile.pets:
            return
        main_pet = profile.pets[0]

        try:
            bio_xlsx = self._stats.export_detailed_xlsx(
                chat_id=chat_id,
                lang_code=profile.language,
                pet_type=main_pet.pet_type,
                pet_name=main_pet.pet_name,
            )
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(bio_xlsx, filename="feeding_stats.xlsx"),
                caption=LOCALES[profile.language].get("stats_sent", "Statistics file sent."),
            )
        except Exception as e:
            err = {
                "ru": f"Ошибка экспорта XLSX: {e}",
                "en": f"Error exporting XLSX: {e}",
                "es": f"Error al exportar XLSX: {e}",
            }[profile.language]
            await context.bot.send_message(chat_id, err)

        # bar chart: who feeds more often
        try:
            png = self._stats.build_who_counts_png(chat_id, profile.language)
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(png, filename="who_feeds_more.png"),
            )
        except Exception as e:
            err = {
                "ru": f"Ошибка построения графика по людям: {e}",
                "en": f"Error building people chart: {e}",
                "es": f"Error al generar el gráfico de personas: {e}",
            }[profile.language]
            await context.bot.send_message(chat_id, err)

        # chart: mode of meal times
        try:
            png2 = self._stats.build_meal_time_modes_png(chat_id, profile.language)
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(png2, filename="meal_time_modes.png"),
            )
        except Exception as e:
            err2 = {
                "ru": f"Ошибка построения графика по времени: {e}",
                "en": f"Error building time chart: {e}",
                "es": f"Error al generar el gráfico de horarios: {e}",
            }[profile.language]
            await context.bot.send_message(chat_id, err2)