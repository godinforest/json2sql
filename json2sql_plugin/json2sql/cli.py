# json2sql/cli.py
import argparse
import os
from .models import db, User, Chat, UserProfile, Pet, FeedingRecord, NotificationState
from .converter import migrate_data

def main():
    parser = argparse.ArgumentParser(description="ETL Plugin: Convert JSON feeding records to SQLite")
    parser.add_argument("--data-dir", type=str, required=True, help="Path to the directory with JSON files")
    parser.add_argument("--db-path", type=str, required=True, help="Path to the output SQLite database file")
    
    args = parser.parse_args()

    if not os.path.exists(args.data_dir):
        print(f"Error: Data directory '{args.data_dir}' does not exist.")
        return

    # English comments: Dynamically initialize the database and create tables if they don't exist
    db.init(args.db_path, pragmas={'foreign_keys': 1})
    db.connect()
    db.create_tables([User, Chat, UserProfile, Pet, FeedingRecord, NotificationState], safe=True)

    print(f"Starting conversion from {args.data_dir} to {args.db_path}...")
    
    migrate_data(data_dir=args.data_dir)
    
    db.close()
    print("Zero-downtime ETL sync completed successfully.")

if __name__ == "__main__":
    main()