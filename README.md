# Pet Feeding Tracker & ETL Plugin

This repository contains a Telegram Bot for tracking pet feedings and a standalone ETL plugin (`json2sql_plugin`). The plugin is designed to asynchronously convert the "raw" NoSQL data (JSON) collected by the bot into a strict relational SQLite database without interrupting the bot's operation (Zero-downtime ETL).

## 1. Plugin Structure & File Descriptions

The plugin is isolated in the `json2sql_plugin` directory and is designed as an independent Python package.

* **`setup.py`**: Package configuration file. It handles the packaging of the code, installation of dependencies (e.g., `peewee`), and registers the global console command `feeding-sync` in your operating system.
* **`json2sql/__init__.py`**: Package initializer. It treats the `json2sql` directory as a proper Python module and exports the main migration function for external use.
* **`json2sql/cli.py`**: Command Line Interface. It processes the terminal flags, dynamically initializes the database connection using the provided paths, and triggers the data migration process.
* **`json2sql/converter.py`**: The core ETL logic. This script reads the JSON files, strictly validates them using regular expressions, extracts the data, and loads it into the SQLite database while preserving all relational constraints (Foreign Keys).
* **`json2sql/models.py`**: Database schema definition. Contains the Peewee ORM classes (`User`, `Chat`, `UserProfile`, `Pet`, `FeedingRecord`, `NotificationState`) that map directly to SQLite tables. The database connection is initialized with `None` to support dynamic binding via the CLI.

## 2. Installation

To ensure your operating system recognizes the `feeding-sync` command, you must install the plugin into your Python environment.

1. Open your terminal.
2. Activate your project's virtual environment (if you are using one).
3. Navigate to the plugin directory:
```bash
cd /path/to/your/project/json2sql_plugin

```



```
4. Install the package in developer (editable) mode:
   ```bash
   pip install -e .

```

*Note: The `-e` flag means any future changes to the plugin's code will apply immediately without needing reinstallation.*

Once installed, the `feeding-sync` command becomes available globally from any directory on your machine.

## 3. CLI Usage (Import & Export)

Data synchronization is executed via a single terminal command. You must provide two arguments: the source of the data (import) and the destination for the database (export).

**Syntax:**

```bash
feeding-sync --data-dir <JSON_FOLDER_PATH> --db-path <SQLITE_FILE_PATH>

```

**Parameter Details:**

* **`--data-dir` (Import Source):** The path to the `data` directory where the Telegram bot stores its `*_profile.json`, `*_feedings.json`, and `notify_state.json` files. You must specify the folder path.
*Example: `/Users/username/projects/tg_bot/data*`
* **`--db-path` (Export Destination):** The path where the final SQLite database will be saved. You must provide the **full path including the desired filename and the `.db` extension**.
* If the file does not exist, the plugin will create it automatically.
* If it already exists, the plugin will safely update it without duplicating records (utilizing `get_or_create` logic).
*Example: `/Users/username/projects/sqldata/pet_feeding.db*`



**Example Command:**

```bash
feeding-sync --data-dir /path/to/your/bot/data --db-path /path/to/your/database/pet_feeding.db

```

## 4. Working with the Data

After running the synchronization command, the `pet_feeding.db` file will be created or updated at your specified location.

You can now use this relational data for advanced analytics:

* Open the file in database management tools like **DBeaver**, **DB Browser for SQLite**, or **DataGrip** to write raw SQL queries.
* Connect to it using Python data science libraries like **pandas** to build dashboards or generate reports.
* The Telegram bot continues to operate entirely independently on its JSON storage, meaning you can safely query the SQLite database without causing locks or downtime for your users.
