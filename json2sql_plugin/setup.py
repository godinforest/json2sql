# setup.py
from setuptools import setup, find_packages

setup(
    name="json2sql-feeding-etl",
    version="1.0.0",
    description="An ETL plugin to sync JSON NoSQL data to a relational SQLite database.",
    packages=find_packages(),
    install_requires=[
        "peewee>=3.17.0"
    ],
    entry_points={
        "console_scripts": [
            "feeding-sync=json2sql.cli:main", 
        ]
    }
)