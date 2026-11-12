"""Backend drivers. Importing this package registers the built-ins.

Each module here implements freehold.core.driver.StorageDriver and nothing
else. The core never imports any of them by name.
"""
from . import github, local, s3  # noqa: F401

__all__ = ["local", "github", "s3"]
