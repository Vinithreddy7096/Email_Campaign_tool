"""Queue consumers and scheduled tasks.

Split deliberately (BUILD.md §3): ``workers.tasks`` holds thin Celery task
wrappers, ``workers.services`` holds the thick business logic with no Celery
or Django-request dependency so it stays unit-testable.
"""
