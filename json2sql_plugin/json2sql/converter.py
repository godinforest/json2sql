# json2sql/converter.py
import os
import json
import glob
import re 
from .models import User, Chat, UserProfile, Pet, FeedingRecord, NotificationState

def migrate_data(data_dir: str):
    """
    Parses JSON files from data_dir and loads them into SQLite.
    Assumes DB connection is already initialized externally.
    """
    
    # 1. Process profiles and pets
    profile_files = glob.glob(os.path.join(data_dir, "*_*_profile.json"))
    for file_path in profile_files:
        filename = os.path.basename(file_path)
        parts = filename.replace("_profile.json", "").split("_")
        if len(parts) >= 2:
            try:
                chat_id = int(parts[0])
                user_id = int(parts[1])
            except ValueError:
                continue  # Skip files that don't match the numerical pattern
        else:
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        chat, _ = Chat.get_or_create(chat_id=chat_id, defaults={'chat_type': 'unknown'})
        user, _ = User.get_or_create(user_id=user_id, defaults={'first_name': 'Unknown'})

        profile, _ = UserProfile.get_or_create(
            chat=chat,
            user=user,
            defaults={'language': data.get('language', 'en')}
        )

        pets_data = data.get("pets")
        if pets_data and isinstance(pets_data, list):
            for idx, pd in enumerate(pets_data, start=1):
                Pet.get_or_create(
                    profile=profile,
                    pet_id_local=int(pd.get("pet_id", idx)),
                    defaults={
                        'pet_type': pd.get("pet_type", "unknown"),
                        'pet_name': pd.get("pet_name", "unknown"),
                        'feedings_per_day': int(pd.get("feedings_per_day", 0)),
                        'portion_grams': int(pd.get("portion_grams", 0)),
                        'interval_hours': int(pd.get("interval_hours", 0)),
                        'first_feed_time_hhmm': pd.get("first_feed_time_hhmm")
                    }
                )
        else:
            Pet.get_or_create(
                profile=profile,
                pet_id_local=1,
                defaults={
                    'pet_type': data.get("pet_type", "unknown"),
                    'pet_name': data.get("pet_name", "unknown"),
                    'feedings_per_day': int(data.get("feedings_per_day", 0)),
                    'portion_grams': int(data.get("portion_grams", 0)),
                    'interval_hours': int(data.get("interval_hours", 0)),
                    'first_feed_time_hhmm': data.get("first_feed_time_hhmm")
                }
            )

    # 2. Process feedings
    feeding_files = glob.glob(os.path.join(data_dir, "*_feedings.json"))
    for file_path in feeding_files:
        filename = os.path.basename(file_path)
        chat_id_str = filename.replace("_feedings.json", "")
        
        # English comments: Strict verification that filename starts with a valid integer ID
        if not re.match(r'^-?\d+$', chat_id_str):
            continue  # Ignore temporary or chart cache files like '2026-06-04_feedings.json'

        chat_id = int(chat_id_str)
        chat, _ = Chat.get_or_create(chat_id=chat_id, defaults={'chat_type': 'unknown'})

        with open(file_path, "r", encoding="utf-8") as f:
            records = json.load(f)
            for rec in records:
                FeedingRecord.create(
                    chat=chat,
                    user_id=rec.get('user_id'), 
                    day_iso=rec.get('day_iso', '1970-01-01'),
                    time_hhmm=rec.get('time_hhmm', '00:00'),
                    fed=rec.get('fed', False),
                    who=rec.get('who', ''),
                    portion_grams=int(rec.get('portion_grams', 0)),
                    meal_name=str(rec.get('meal_name', ''))
                )

    # 3. Process notification states
    notify_file = os.path.join(data_dir, "notify_state.json")
    if os.path.exists(notify_file):
        with open(notify_file, "r", encoding="utf-8") as f:
            notify_data = json.load(f)
            
            if isinstance(notify_data, dict):
                for chat_id_str, states in notify_data.items():
                    if not re.match(r'^-?\d+$', chat_id_str):
                        continue
                    chat_id = int(chat_id_str)
                    chat, _ = Chat.get_or_create(chat_id=chat_id, defaults={'chat_type': 'unknown'})
                    for state in states:
                        NotificationState.create(
                            chat=chat,
                            day_iso=state.get('day_iso', '1970-01-01'),
                            meal_index=int(state.get('meal_index', 0)),
                            is_sent=state.get('is_sent', False)
                        )
            elif isinstance(notify_data, list):
                for state in notify_data:
                    chat_id = state.get('chat_id')
                    if chat_id:
                        chat, _ = Chat.get_or_create(chat_id=chat_id, defaults={'chat_type': 'unknown'})
                        NotificationState.create(
                            chat=chat,
                            day_iso=state.get('day_iso', '1970-01-01'),
                            meal_index=int(state.get('meal_index', 0)),
                            is_sent=state.get('is_sent', False)
                        )