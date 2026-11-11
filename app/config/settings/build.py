"""
Image build only: ``collectstatic`` at ``docker build`` time, with no secrets
and no database. Never used to serve requests — the image runs with
config.settings.production or config.settings.staging.
"""

from .base import *  # noqa: F401,F403
from .base import PRODUCTION_STORAGES

ENVIRONMENT = "build"
STORAGES = PRODUCTION_STORAGES
