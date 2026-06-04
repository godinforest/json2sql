from dataclasses import dataclass, asdict
from datetime import date
from typing import List, Dict, Any, Optional


@dataclass
class PetProfile:
    """
    Профиль одного питомца внутри UserProfile.
    """
    pet_id: int               # локальный идентификатор питомца (1..N для данного пользователя)
    pet_type: str             # тип животного (кошка, собака и т.п.)
    pet_name: str             # имя питомца
    feedings_per_day: int     # сколько кормлений в день
    portion_grams: int        # порция в граммах
    interval_hours: int       # интервал между кормлениями в часах
    first_feed_time_hhmm: Optional[str] = None  # базовое время первого кормления, например '08:30'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UserProfile:
    """Persistent user profile linked to Telegram user_id."""
    user_id: int
    chat_id: int
    language: str  # 'ru' | 'en' | 'es'
    pets: List[PetProfile]

    def to_dict(self) -> Dict[str, Any]:
        """
        Новый формат хранения профиля:
        {
            "user_id": ...,
            "chat_id": ...,
            "language": "...",
            "pets": [
                {
                    "pet_id": 1,
                    "pet_type": "...",
                    "pet_name": "...",
                    "feedings_per_day": ...,
                    "portion_grams": ...,
                    "interval_hours": ...,
                    "first_feed_time_hhmm": "HH:MM" | null
                },
                ...
            ]
        }
        """
        return {
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "language": self.language,
            "pets": [p.to_dict() for p in self.pets],
        }

    @staticmethod
    def from_legacy_dict(d: Dict[str, Any]) -> "UserProfile":
        """
        Обратная совместимость.

        Старый формат профиля был плоским и описывал только одного питомца:
        {
            "user_id": ...,
            "chat_id": ...,
            "language": "...",
            "pet_type": "...",
            "pet_name": "...",
            "feedings_per_day": ...,
            "portion_grams": ...,
            "interval_hours": ...,
            "first_feed_time_hhmm": "HH:MM" | null
        }

        Новый формат хранит список питомцев в поле "pets".
        Эта функция принимает как старый, так и новый формат и всегда
        возвращает UserProfile с заполненным списком pets.
        """
        # Если нового поля "pets" нет — считаем, что это старый формат
        if "pets" not in d:
            pet = PetProfile(
                pet_id=1,
                pet_type=d.get("pet_type", "pet"),
                pet_name=d.get("pet_name", "pet"),
                feedings_per_day=int(d.get("feedings_per_day", 3)),
                portion_grams=int(d.get("portion_grams", 0)),
                interval_hours=int(d.get("interval_hours", 6)),
                first_feed_time_hhmm=d.get("first_feed_time_hhmm"),
            )
            return UserProfile(
                user_id=int(d["user_id"]),
                chat_id=int(d["chat_id"]),
                language=d.get("language", "en"),
                pets=[pet],
            )

        # Новый формат: есть массив pets
        pets_raw = d.get("pets") or []
        pets: List[PetProfile] = []
        for i, pd in enumerate(pets_raw, start=1):
            pets.append(
                PetProfile(
                    pet_id=int(pd.get("pet_id", i)),
                    pet_type=pd.get("pet_type", "pet"),
                    pet_name=pd.get("pet_name", f"pet{i}"),
                    feedings_per_day=int(pd.get("feedings_per_day", 3)),
                    portion_grams=int(pd.get("portion_grams", 0)),
                    interval_hours=int(pd.get("interval_hours", 6)),
                    first_feed_time_hhmm=pd.get("first_feed_time_hhmm"),
                )
            )

        return UserProfile(
            user_id=int(d["user_id"]),
            chat_id=int(d["chat_id"]),
            language=d.get("language", "en"),
            pets=pets,
        )

    def get_pet(self, pet_id: int) -> PetProfile:
        """
        Удобный helper: вернуть питомца по pet_id.
        Если такого нет, вернёт первого питомца в списке.
        """
        for p in self.pets:
            if p.pet_id == pet_id:
                return p
        # Fallback — хотя бы один питомец в профиле должен быть всегда
        return self.pets[0]


@dataclass
class FeedingRecord:
    """Single feeding event entry."""
    day_iso: str     # YYYY-MM-DD
    time_hhmm: str   # HH:MM
    fed: bool
    who: str
    portion_grams: int
    meal_name: str   # localized label

    def to_row(self, headers: List[str]) -> List[str]:
        # Order: Day, Time, Fed, Who, Portion (g), Meal
        return [
            self.day_iso,
            self.time_hhmm,
            "1" if self.fed else "0",
            self.who,
            str(self.portion_grams),
            self.meal_name,
        ]
