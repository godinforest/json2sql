# stats.py
import io
import json
import os
import re #parcear numeros de char
from typing import List, Dict, Tuple
from collections import Counter, defaultdict

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import xlsxwriter

from models import FeedingRecord


class StatsService:
    """Charts & exports for feedings. Storage is per chat (chat-local)."""

    def __init__(self, data_dir: str = "data") -> None:
        self._data_dir = data_dir
        os.makedirs(self._data_dir, exist_ok=True)

    # ---------- Helper to extract meal number ----------
    def _extract_meal_number(self, meal_name: str) -> str:
        """
        Вытаскивает только цифру из названия приёма пищи (например, 'Karamelka #1 (manual)' -> '1').
        Если цифра не найдена, возвращает исходную строку.
        """
        if not meal_name:
            return "1"
        match = re.search(r'#(\d+)', meal_name)  # Ищет паттерн #1, #2 и т.д.
        if match:
            return match.group(1)
        
        # Резервный поиск первой попавшейся цифры, если нет знака #
        match_any_digit = re.search(r'\d+', meal_name)
        if match_any_digit:
            return match_any_digit.group(0)
            
        return meal_name

    # ---------- File paths are per chat ----------
    def _feedings_path(self, chat_id: int) -> str:
        return os.path.join(self._data_dir, f"{chat_id}_feedings.json")

    def _ensure_feedings_file(self, chat_id: int) -> None:
        path = self._feedings_path(chat_id)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)

    # ---------- Persistence ----------
    def append_record(self, chat_id: int, record: FeedingRecord) -> None:
        self._ensure_feedings_file(chat_id)
        path = self._feedings_path(chat_id)
        with open(path, "r", encoding="utf-8") as f:
            arr: List[Dict] = json.load(f)
        arr.append(record.__dict__)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)

    def load_records(self, chat_id: int) -> List[FeedingRecord]:
        self._ensure_feedings_file(chat_id)
        path = self._feedings_path(chat_id)
        with open(path, "r", encoding="utf-8") as f:
            arr: List[Dict] = json.load(f)
        return [FeedingRecord(**x) for x in arr]

    # ---------- Charts ----------
    def build_who_counts_png(self, chat_id: int, lang_code: str) -> io.BytesIO:
        recs = self.load_records(chat_id)
        counts: Dict[str, int] = {}
        for r in recs:
            if r.fed:
                counts[r.who] = counts.get(r.who, 0) + 1

        items = sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))
        labels = [k for (k, _) in items] or ["—"]
        values = np.array([v for (_, v) in items] or [0], dtype=np.int32)

        x_label = {"ru": "Кто кормил", "en": "Who", "es": "Quién"}[lang_code]
        y_label = {"ru": "Количество", "en": "Count", "es": "Cantidad"}[lang_code]
        title   = {
            "ru": "Кормления по людям (всё время)",
            "en": "Feedings by person (all time)",
            "es": "Alimentaciones por persona (todo el tiempo)",
        }[lang_code]

        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar(labels, values)
        ax.set_title(title)
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.set_ylim(0, max(1, int(values.max()) if values.size else 1) * 1.15)

        for tick in ax.get_xticklabels():
            tick.set_rotation(20)
            tick.set_ha("right")

        bio = io.BytesIO()
        fig.tight_layout()
        fig.savefig(bio, format="png", dpi=160)
        plt.close(fig)
        bio.seek(0)
        return bio

    def build_meal_time_modes_png(self, chat_id: int, lang_code: str) -> io.BytesIO:
        """
        Мода времени (HH:MM) по каждому приёму.
        Ось X: № приёма (1,2,3...), ось Y: время 05:00–22:00. На графике точки + подпись HH:MM.
        """
        x_label = {"ru": "Приём", "en": "Meal", "es": "Comida"}.get(lang_code, "Meal")
        y_label = {"ru": "Время", "en": "Time", "es": "Hora"}.get(lang_code, "Time")
        title   = {
            "ru": "Типичное время по приёмам (мода)",
            "en": "Typical time by meal (mode)",
            "es": "Horario típico por comida (moda)",
        }.get(lang_code, "Meal time modes")

        recs = [r for r in self.load_records(chat_id) if getattr(r, "fed", True)]

        per_meal = defaultdict(Counter)
        for r in recs:
            # Очищаем имя до чистого ID приёма (числа) для группировки графиков
            meal_key = self._extract_meal_number(str(r.meal_name))
            t = str(r.time_hhmm)
            if t and t != "––":
                per_meal[meal_key][t] += 1

        if not per_meal:
            fig, ax = plt.subplots(figsize=(6, 3))
            ax.text(0.5, 0.5, {"ru": "Нет данных", "en": "No data", "es": "Sin datos"}.get(lang_code, "No data"),
                    ha="center", va="center", fontsize=12)
            ax.axis("off")
            bio = io.BytesIO()
            fig.tight_layout()
            fig.savefig(bio, format="png", dpi=160)
            plt.close(fig)
            bio.seek(0)
            return bio

        def meal_sort_key(k: str):
            try:
                return (0, int(k))
            except ValueError:
                return (1, k)

        def hhmm_to_minutes(hhmm: str) -> int:
            try:
                h, m = hhmm.split(":")
                return int(h) * 60 + int(m)
            except Exception:
                return 0

        meals_sorted = sorted(per_meal.keys(), key=meal_sort_key)

        x_pos = []
        x_labels = []
        y_minutes = []
        ann_labels = []

        for meal in meals_sorted:
            common = per_meal[meal].most_common()
            if not common:
                continue
            max_freq = common[0][1]
            candidates = sorted([t for t, c in common if c == max_freq], key=hhmm_to_minutes)
            mode_t = candidates[0]

            try:
                x_val = int(meal)
            except ValueError:
                x_val = len(x_pos) + 1

            x_pos.append(x_val)
            x_labels.append(str(x_val))
            y_minutes.append(hhmm_to_minutes(mode_t))
            ann_labels.append(mode_t)

        y_min = 5 * 60
        y_max = 22 * 60
        y_minutes = [min(max(v, y_min), y_max) for v in y_minutes]

        yticks = list(range(y_min, y_max + 1, 60))
        yticklabels = [f"{h:02d}:00" for h in range(5, 23)]

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.scatter(x_pos, y_minutes, s=35)
        ax.set_title(title)
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.set_ylim(y_min, y_max)
        ax.set_yticks(yticks)
        ax.set_yticklabels(yticklabels)
        ax.set_xticks(x_pos)
        ax.set_xticklabels(x_labels)
        ax.grid(axis="y", linestyle=":", alpha=0.4)

        for x, y, txt in zip(x_pos, y_minutes, ann_labels):
            ax.annotate(txt, (x, y), textcoords="offset points", xytext=(6, 6),
                        ha="left", va="bottom", fontsize=9)

        bio = io.BytesIO()
        fig.tight_layout()
        fig.savefig(bio, format="png", dpi=160)
        plt.close(fig)
        bio.seek(0)
        return bio

    # ---------- Purge ----------
    def purge_chat(self, chat_id: int) -> None:
        path = self._feedings_path(chat_id)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

        try:
            for fn in os.listdir(self._data_dir):
                full = os.path.join(self._data_dir, fn)
                if not os.path.isfile(full):
                    continue
                if str(chat_id) in fn and any(fn.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".xlsx", ".csv", ".json"]):
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------- xlsx export ----------
    def export_detailed_xlsx(self, chat_id: int, lang_code: str, pet_type: str, pet_name: str) -> io.BytesIO:
        """
        XLSX всех кормлений для чата.
        Колонки (локализованные): Who, Day, Time, Pet type, Pet name, Portion (g), Meal.
        """
        headers_map = {
            "ru": ["Кто", "День", "Время", "Тип животного", "Имя животного", "Порция (г)", "Приём"],
            "en": ["Who", "Day", "Time", "Pet type", "Pet name", "Portion (g)", "Meal"],
            "es": ["Quién", "Día", "Hora", "Tipo de mascota", "Nombre de mascota", "Porción (g)", "Comida"],
        }
        headers = headers_map.get(lang_code, headers_map["en"])

        recs = self.load_records(chat_id)
        
        # Сортируем корректно с учетом вычищенного ID приёма пищи
        recs.sort(key=lambda r: (r.day_iso, r.time_hhmm, self._extract_meal_number(str(r.meal_name))))

        bio = io.BytesIO()
        wb = xlsxwriter.Workbook(bio, {'in_memory': True})
        ws = wb.add_worksheet("stats")

        # Заголовки
        for col, h in enumerate(headers):
            ws.write(0, col, h)

        # Данные
        for row, r in enumerate(recs, start=1):
            ws.write(row, 0, r.who)
            ws.write(row, 1, r.day_iso)
            ws.write(row, 2, r.time_hhmm)
            ws.write(row, 3, pet_type)
            ws.write(row, 4, pet_name)
            ws.write(row, 5, r.portion_grams)
            
            # В Excel пишем только ЧИСТОЕ ЧИСЛО (int), чтобы аналитика и графики в Excel работали
            cleaned_meal = self._extract_meal_number(str(r.meal_name))
            try:
                ws.write(row, 6, int(cleaned_meal))
            except ValueError:
                ws.write(row, 6, cleaned_meal)

        wb.close()
        bio.seek(0)
        return bio

