import logging
import os
from dotenv import load_dotenv
from feeding_app import FeedingApp
from notifications import NotificationScheduler

# English comments: Load environment variables from .env file at the very beginning
load_dotenv()

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

def main():
    # English comments: Retrieve configuration with safe defaults where applicable
    token = os.environ.get("TELEGRAM_TOKEN")
    db_name = os.environ.get("DATABASE_NAME", "pet_feeding.db")
    data_dir = os.environ.get("DATA_DIR", "data")
    
    # English comments: Safely convert numeric values from environment strings
    tz_offset = int(os.environ.get("TZ_OFFSET_MINUTES", 180))
    poll_period = int(os.environ.get("POLL_PERIOD_SEC", 1))

    if not token:
        logger.error("TELEGRAM_TOKEN is missing in .env file. Execution stopped.")
        return

    # 1) Запускаем уведомления (в отдельном потоке)
    scheduler = NotificationScheduler(
        token=token, 
        data_dir=data_dir, 
        tz_offset_minutes=tz_offset, 
        poll_period_sec=poll_period
    )
    scheduler.start()

    # 2) Запускаем самого бота
    app = FeedingApp(token)
    app.run()

if __name__ == "__main__":
    main()