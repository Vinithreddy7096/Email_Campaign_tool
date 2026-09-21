"""
Regression tests for the defaults that keep mail off the internet.

These exist because each one is a single-character edit away from being wrong,
and each wrong value is an incident rather than a bug.
"""

from django.conf import settings

from config.celery import app as celery_app


def test_mock_mode_is_on_in_the_test_environment():
    assert settings.MOCK_MODE is True


def test_global_send_switch_defaults_off():
    """§4.2: GLOBAL_SEND_ENABLED is false even in production. Enabling it is an
    audited human action through the admin UI, never a deploy."""
    assert settings.GLOBAL_SEND_ENABLED is False


def test_tracking_is_off_by_default():
    """Privacy default per §4.1 and spec §10.2."""
    assert settings.TRACKING_OPENS_ENABLED is False
    assert settings.TRACKING_CLICKS_ENABLED is False


def test_retry_backoff_matches_the_documented_policy():
    """§9.4: 5m, 15m, 1h, 3h, 6h, 12h."""
    assert settings.RETRY_BACKOFF_SECONDS == [300, 900, 3600, 10800, 21600, 43200]
    assert len(settings.RETRY_BACKOFF_SECONDS) == settings.MAX_DELIVERY_ATTEMPTS


def test_templating_app_is_not_named_templates():
    """§3 naming trap: an app called `templates` collides with Django template
    discovery."""
    assert "templating" in settings.INSTALLED_APPS
    assert "templates" not in settings.INSTALLED_APPS


class TestCeleryLossPrevention:
    """§9.2 — the settings that stop messages being silently lost."""

    def test_broker_is_actually_wired_to_rabbitmq(self):
        """Regression: CELERY_BROKER_URL was missing, so broker_url resolved to
        None and Celery fell back to amqp://guest@localhost. Workers reported
        themselves 'running' while retrying a refused connection forever, and no
        static check caught it."""
        assert celery_app.conf.broker_url == settings.RABBITMQ_URL
        assert celery_app.conf.broker_url is not None

    def test_acks_are_late(self):
        assert celery_app.conf.task_acks_late is True

    def test_tasks_requeue_when_a_worker_is_killed(self):
        assert celery_app.conf.task_reject_on_worker_lost is True

    def test_prefetch_is_one(self):
        assert celery_app.conf.worker_prefetch_multiplier == 1

    def test_publishes_are_confirmed(self):
        assert celery_app.conf.broker_transport_options["confirm_publish"] is True

    def test_nothing_lands_on_deliver_by_accident(self):
        assert celery_app.conf.task_default_queue == "maintenance"

    def test_every_queue_is_quorum_backed_and_dead_letters(self):
        queues = {q.name: q for q in celery_app.conf.task_queues}
        assert set(queues) == {"expand", "render", "deliver", "events", "maintenance"}
        for name, queue in queues.items():
            args = queue.queue_arguments
            assert args["x-queue-type"] == "quorum", name
            assert args["x-dead-letter-exchange"] == "dlx", name
            assert args["x-dead-letter-routing-key"] == f"dlq.{name}", name
