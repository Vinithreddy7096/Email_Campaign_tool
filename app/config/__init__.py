"""Django project package.

Importing the Celery app here guarantees ``@shared_task`` resolves against it
whenever Django starts.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
