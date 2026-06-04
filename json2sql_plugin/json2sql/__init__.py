# json2sql/__init__.py
# English comments: Expose the main migration function for the package
from .converter import migrate_data

__all__ = ["migrate_data"]