

# notifications.py
import asyncio
import json
import os
import threading
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Any
from collections import Counter

from telegram import Bot

logger = logging.getLogger(__name__)


# ===== Helpers =====
def _tznow(tz_offset_minutes: int) -> datetime:
    """Current local time based on UTC offset (in minutes)."""
    return datetime.utcnow() - timedelta(minutes=tz_offset_minutes)


def _parse_hhmm(s: Optional[str]) -> Optional[time]:
    """Parse 'HH:MM' or 'HH.MM' into time."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    sep = ":" if ":" in s else "."
    try:
        hh, mm = map(int, s.split(sep))
    except Exception:
        return None
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return time(hh, mm)
    return None


def _time_add_hours(t: time, hours: int) -> time:
    base = datetime(2000, 1, 1, t.hour, t.minute)
    return (base + timedelta(hours=hours)).time()


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _parse_meal_name(meal_name: str) -> (Optional[str], Optional[int]):
    """
    Extract (pet_name, meal_index) from meal_name string.

    Supported formats:
      - "Bobik #1"
      - "Bobik #2 (manual)"
      - Legacy: "1", "2", ...
    """
    if not meal_name:
        return None, None
    s = str(meal_name).strip()
    if not s:
        return None, None

    # New format: "<pet_name> #<idx>..."
    m = re.match(r"^(?P<pet>.+?)\s*#\s*(?P<idx>\d+)", s)
    if m:
        pet = m.group("pet").strip()
        try:
            idx = int(m.group("idx"))
        except ValueError:
            return pet, None
        return pet, idx

    # Legacy format: just index "1", "2", ...
    if s.isdigit():
        return None, int(s)

    return None, None


# ===== Storage DTO =====
@dataclass
class ProfileRow:
    chat_id: int
    language: str
    pet_id: int
    pet_name: str
    feedings_per_day: int
    interval_hours: int
    first_feed_time_hhmm: Optional[str]
    notif_message: Optional[str] = None


# ===== Main Scheduler =====
class NotificationScheduler:
    """
    Планировщик уведомлений (для КАЖДОГО питомца отдельно).

    Приоритет:
      1) Если пользователь уже отметил кормление (в том числе через
         «Забыл покормить» с ручным временем), все уведомления ДО этого
         приёма считаются выполненными, а ПОСЛЕ — не выполненными.
      2) Обычный режим – стандартные напоминания.

    Новая логика:
      • Для каждого приёма пищи храним флаг done (массив sent[])
        и время последнего напоминания last_reminders[].
      • Напоминание отправляется, если:
           - приём ещё НЕ done,
           - текущее время >= запланированного,
           - с момента последнего напоминания прошло >= 20 минут.
      • done становится True только из статистики кормлений
        (файл *_feedings.json), а НЕ при отправке уведомления.
    """

    def __init__(
        self,
        token: str,
        data_dir: str = "data",
        tz_offset_minutes: int = 180,  # Буэнос-Айрес = UTC-3
        poll_period_sec: int = 30,
        state_file: str = "notify_state.json",
    ) -> None:
        self._bot = Bot(token=token)
        self._data_dir = data_dir
        os.makedirs(self._data_dir, exist_ok=True)

        self._tz_offset_minutes = tz_offset_minutes
        self._poll_period_sec = max(5, poll_period_sec)

        self._state_path = os.path.join(self._data_dir, state_file)
        # структура:
        # {"YYYY-MM-DD": {
        #     "<chat_id>": {
        #         "<pet_id>": {
        #             "sent": [bool, ...],            # done mask (кормление отмечено)
        #             "base_first": "HH:MM",          # базовое время первого кормления
        #             "last_reminders": [str|None]    # iso-время последнего напоминания
        #         }
        #     }
        # }}
        self._state: Dict[str, Dict[str, Dict[str, Any]]] = self._load_state()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------- Public ----------
    def start(self) -> None:
        """Запускает планировщик в отдельном потоке."""
        if self._thread and self._thread.is_alive():
            logger.info("NotificationScheduler already running.")
            return

        self._stop.clear()

        def _runner():
            asyncio.run(self._loop())

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        logger.info("NotificationScheduler started.")

    def stop(self) -> None:
        """Останавливает планировщик."""
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        logger.info("NotificationScheduler stopped.")

    # ---------- Async loop ----------
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.error("Error in NotificationScheduler tick: %s", e, exc_info=True)
            await asyncio.sleep(self._poll_period_sec)

    # ---------- Core ----------
    async def _tick(self) -> None:
        now = _tznow(self._tz_offset_minutes)
        today_iso = now.date().isoformat()
        now_minutes = now.hour * 60 + now.minute

        self._rollover_state(today_iso)

        day_bucket = self._state.setdefault(today_iso, {})
        if not isinstance(day_bucket, dict):
            day_bucket = {}
            self._state[today_iso] = day_bucket

        profiles_by_chat = self._scan_profiles()
        if not profiles_by_chat:
            return

        for chat_id, pet_rows in profiles_by_chat.items():
            chat_key = str(chat_id)
            chat_bucket = day_bucket.get(chat_key)
            if not isinstance(chat_bucket, dict):
                chat_bucket = {}
                day_bucket[chat_key] = chat_bucket

            # feedings_today: {pet_name_or_all: {meal_idx: time}}
            feedings_today = self._get_today_feedings(chat_id, today_iso)

            for prof in pet_rows:
                pet_key = str(prof.pet_id)

                # 1️⃣ базовое время первого уведомления
                base_first = (
                    _parse_hhmm(prof.first_feed_time_hhmm)
                    or self._mode_time_first_meal(chat_id, prof.pet_name)
                    or time(6, 30)
                )
                base_first_str = f"{base_first.hour:02d}:{base_first.minute:02d}"

                n = max(1, int(prof.feedings_per_day))
                interval_h = max(1, int(prof.interval_hours))

                pet_state: Dict[str, Any] = chat_bucket.setdefault(
                    pet_key,
                    {
                        "sent": [False] * n,
                        "base_first": base_first_str,
                        "last_reminders": [None] * n,
                    },
                )
                done_mask: List[bool] = list(pet_state.get("sent") or [])
                prev_first: Optional[str] = pet_state.get("base_first")
                last_reminders: List[Optional[str]] = list(pet_state.get("last_reminders") or [])

                # если базовое время или количество кормлений изменились — сбрасываем done и last_reminders
                if prev_first != base_first_str or len(done_mask) != n:
                    done_mask = [False] * n
                    last_reminders = [None] * n
                    pet_state["sent"] = done_mask
                    pet_state["base_first"] = base_first_str
                    pet_state["last_reminders"] = last_reminders
                    logger.info(
                        "Reset notification state for chat=%s pet_id=%s (first=%s, n=%d)",
                        chat_key,
                        pet_key,
                        base_first_str,
                        n,
                    )
                else:
                    # подгон длины массивов под n
                    if len(done_mask) < n:
                        done_mask.extend([False] * (n - len(done_mask)))
                    elif len(done_mask) > n:
                        done_mask = done_mask[:n]
                    if len(last_reminders) < n:
                        last_reminders.extend([None] * (n - len(last_reminders)))
                    elif len(last_reminders) > n:
                        last_reminders = last_reminders[:n]
                    pet_state["sent"] = done_mask
                    pet_state["last_reminders"] = last_reminders

                # какой ключ использовать в feedings_today
                pet_feedings_today = (
                    feedings_today.get(prof.pet_name)
                    or feedings_today.get("__all__", {})
                )

                # ===== Приоритет ручного ввода / фактически выполненных кормлений =====
                # Если есть отмеченные кормления, пересобираем.mask: все до max_done -> True, остальные -> False.
                if pet_feedings_today:
                    max_done_idx = max(pet_feedings_today.keys())  # 1-based
                    if max_done_idx >= 1:
                        new_mask = []
                        for i in range(n):
                            new_mask.append(i < max_done_idx)
                        done_mask = new_mask
                        pet_state["sent"] = done_mask
                        logger.info(
                            "Rebuild done mask from feedings (manual priority): "
                            "chat=%s pet=%s max_done=%d mask=%s",
                            chat_key,
                            prof.pet_name,
                            max_done_idx,
                            done_mask,
                        )

                # 2️⃣ проверка и отправка напоминаний (каждые 20 минут, пока done=False)
                for idx in range(n):
                    if done_mask[idx]:
                        # кормление уже отмечено
                        continue

                    # планируемое время уведомления
                    if idx == 0:
                        scheduled_t = base_first
                    else:
                        feeding_index = idx  # уведомление i → кормление i
                        feeding_time = pet_feedings_today.get(feeding_index)
                        if not feeding_time:
                            continue
                        scheduled_t = _time_add_hours(feeding_time, interval_h)

                    scheduled_minutes = _time_to_minutes(scheduled_t)
                    if now_minutes < scheduled_minutes:
                        # ещё рано
                        continue

                    # проверяем, прошло ли 20 минут с последнего напоминания
                    last_str = last_reminders[idx]
                    if last_str:
                        try:
                            last_dt = datetime.fromisoformat(last_str)
                        except Exception:
                            last_dt = None
                    else:
                        last_dt = None

                    if last_dt is not None:
                        diff = now - last_dt
                        if diff.total_seconds() < 20 * 60:
                            # 20 минут ещё не прошло
                            continue

                    # отправляем напоминание
                    try:
                        await self._send_notify(
                            chat_id=chat_id,
                            pet_name=prof.pet_name,
                            lang=prof.language,
                            custom_template=prof.notif_message,
                            morning=(idx == 0),
                            meal_index=idx + 1,
                        )
                        last_reminders[idx] = now.isoformat()
                        pet_state["last_reminders"] = last_reminders
                        logger.info(
                            "Notify sent (reminder): chat=%s pet=%s #%d at %s (done=%s)",
                            chat_key,
                            prof.pet_name,
                            idx + 1,
                            scheduled_t.strftime("%H:%M"),
                            done_mask[idx],
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to send notify to chat=%s pet=%s: %s",
                            chat_key,
                            prof.pet_name,
                            e,
                        )

        self._save_state()

    async def _send_notify(
        self,
        chat_id: int,
        pet_name: str,
        lang: str,
        custom_template: Optional[str],
        morning: bool,
        meal_index: int,
    ) -> None:
        """Отправляет сообщение в чат.

        Если есть кастомный шаблон – используем его.
        Иначе — локализованный дефолт:
          • RU: «Время кормить {pet}!»
          • EN: «Time to feed {pet}!»
          • ES: «¡Es hora de alimentar a {pet}!»
        """
        # если есть кастомный шаблон — используем его
        if custom_template:
            try:
                text = custom_template.format(pet=pet_name, meal=meal_index)
            except Exception:
                text = custom_template
        else:
            lang_norm = (lang or "en").lower()
            if lang_norm.startswith("ru"):
                text = f"Время кормить {pet_name}!"
            elif lang_norm.startswith("es"):
                text = f"¡Es hora de alimentar a {pet_name}!"
            else:
                text = f"Time to feed {pet_name}!"

        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error("Send message error to chat %s: %s", chat_id, e)

    # ---------- Profile & Stats ----------
    def _scan_profiles(self) -> Dict[int, List[ProfileRow]]:
        """
        Сканирует все *_profile.json в data_dir.

        Поддерживаются два формата:
          • старый: один питомец в корне профиля;
          • новый: список pets внутри профиля (1–5 питомцев).
        """
        result: Dict[int, List[ProfileRow]] = {}
        for fn in os.listdir(self._data_dir):
            if not fn.endswith("_profile.json"):
                continue
            path = os.path.join(self._data_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception as e:
                logger.warning("Failed to read profile %s: %s", fn, e)
                continue

            if "chat_id" not in d:
                continue

            chat_id = int(d["chat_id"])
            language = (d.get("language") or d.get("lang") or "en").lower()

            pets = d.get("pets")
            if isinstance(pets, list) and pets:
                for pet in pets:
                    try:
                        pet_id = int(pet.get("pet_id") or len(result.get(chat_id, [])) + 1)
                    except Exception:
                        pet_id = len(result.get(chat_id, [])) + 1
                    row = ProfileRow(
                        chat_id=chat_id,
                        language=language,
                        pet_id=pet_id,
                        pet_name=str(pet.get("pet_name") or pet.get("name") or "pet"),
                        feedings_per_day=int(pet.get("feedings_per_day") or d.get("feedings_per_day", 3)),
                        interval_hours=int(pet.get("interval_hours") or d.get("interval_hours", 6)),
                        first_feed_time_hhmm=pet.get("first_feed_time_hhmm") or d.get("first_feed_time_hhmm"),
                        notif_message=pet.get("notif_message") or d.get("notif_message"),
                    )
                    result.setdefault(chat_id, []).append(row)
            else:
                # Legacy: один питомец в корне профиля
                try:
                    pet_id = int(d.get("pet_id") or 1)
                except Exception:
                    pet_id = 1
                row = ProfileRow(
                    chat_id=chat_id,
                    language=language,
                    pet_id=pet_id,
                    pet_name=str(d.get("pet_name") or "pet"),
                    feedings_per_day=int(d.get("feedings_per_day", 3)),
                    interval_hours=int(d.get("interval_hours", 6)),
                    first_feed_time_hhmm=d.get("first_feed_time_hhmm"),
                    notif_message=d.get("notif_message"),
                )
                result.setdefault(chat_id, []).append(row)

        return result

    def _mode_time_first_meal(self, chat_id: int, pet_name: Optional[str]) -> Optional[time]:
        """
        Возвращает моду по времени первого кормления для КОНКРЕТНОГО питомца
        (или по всем питомцам, если pet_name is None).
        """
        path = os.path.join(self._data_dir, f"{chat_id}_feedings.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                arr = json.load(f)
        except Exception:
            return None

        counter: Counter[str] = Counter()
        for rec in arr or []:
            meal = str(rec.get("meal_name", "")).strip()
            t_str = str(rec.get("time_hhmm", "")).strip()
            pet_from_meal, idx = _parse_meal_name(meal)
            if idx != 1:
                continue
            if pet_name and pet_from_meal and pet_from_meal != pet_name:
                continue
            t = _parse_hhmm(t_str)
            if t:
                key = f"{t.hour:02d}:{t.minute:02d}"
                counter[key] += 1

        if not counter:
            return None

        max_count = max(counter.values())
        candidates = [k for k, v in counter.items() if v == max_count]
        candidates.sort(key=lambda x: _time_to_minutes(_parse_hhmm(x) or time(0, 0)))
        return _parse_hhmm(candidates[0])

    def _get_today_feedings(self, chat_id: int, today_iso: str) -> Dict[str, Dict[int, time]]:
        """
        Читает файл <chat_id>_feedings.json и возвращает:
            {pet_name_or_all: {номер_кормления: time}} только за текущий день.
        """
        path = os.path.join(self._data_dir, f"{chat_id}_feedings.json")
        if not os.path.exists(path):
            return {}

        try:
            with open(path, "r", encoding="utf-8") as f:
                arr = json.load(f)
        except Exception:
            return {}

        result: Dict[str, Dict[int, time]] = {}
        for rec in arr or []:
            if rec.get("day_iso") != today_iso:
                continue
            t = _parse_hhmm(rec.get("time_hhmm"))
            if not t:
                continue
            meal = str(rec.get("meal_name", "")).strip()
            pet_from_meal, idx = _parse_meal_name(meal)
            if idx is None:
                continue
            pet_key = pet_from_meal or "__all__"
            d = result.setdefault(pet_key, {})
            d[idx] = t  # последнее значение для индекса

        return result

    # ---------- State ----------
    def _rollover_state(self, today: str) -> None:
        """Оставляем в state только сегодняшний день."""
        if today in self._state and len(self._state) == 1:
            return
        today_bucket = self._state.get(today, {})
        self._state = {today: today_bucket}

    def _load_state(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        if not os.path.exists(self._state_path):
            return {}
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.error("Load state error: %s", e)
        return {}

    def _save_state(self) -> None:
        try:
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Save state error: %s", e)
