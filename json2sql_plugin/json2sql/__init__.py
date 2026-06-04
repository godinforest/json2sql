# json2sql/__init__.py
#Expose the main migration function for the package
from .converter import migrate_data

__all__ = ["migrate_data"]
