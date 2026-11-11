from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission

AUDIT_EVENTS = []


def record_audit(**kwargs):
    AUDIT_EVENTS.append(kwargs)


GOOD_HTML = (
    '<html lang="en"><body><p>Hello {{ first_name }},</p>'
    '<p><a href="https://example.org/offer">See the offer</a></p>'
    '<p><a href="{{ unsubscribe_url }}">Unsubscribe</a></p></body></html>'
)
GOOD_TEXT = (
    "Hello {{ first_name }},\nSee https://example.org/offer\nUnsubscribe: {{ unsubscribe_url }}\n"
)
GOOD_VARIABLES = {"first_name": {"required": True}}
GOOD_SAMPLE = {"first_name": "Ada"}


def make_user(username, *perms):
    user = get_user_model().objects.create_user(username, password="pw")
    for codename in perms:
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label="email_templates", codename=codename)
        )
    return get_user_model().objects.get(pk=user.pk)  # reset permission cache


AUTHOR_PERMS = (
    "add_emailtemplate",
    "add_templateversion",
    "change_templateversion",
    "submit_templateversion",
)
