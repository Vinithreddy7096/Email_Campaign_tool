"""The only tags and filters email templates may use (BUILD.md §10.5.2).

Nothing here reads the clock, randomness, the database or the request, and
nothing marks output safe, so rendering stays deterministic and escaped.
"""

from django.template import Library, defaultfilters, defaulttags

ALLOWED_TAGS = ("if", "for", "with", "comment", "firstof", "cycle", "spaceless")

ALLOWED_FILTERS = (
    "add",
    "capfirst",
    "center",
    "cut",
    "default",
    "default_if_none",
    "divisibleby",
    "escape",
    "first",
    "floatformat",
    "join",
    "last",
    "length",
    "linebreaks",
    "linebreaksbr",
    "ljust",
    "lower",
    "pluralize",
    "rjust",
    "slice",
    "stringformat",
    "striptags",
    "title",
    "truncatechars",
    "truncatewords",
    "upper",
    "urlencode",
    "wordwrap",
    "yesno",
)

register = Library()
for _name in ALLOWED_TAGS:
    register.tags[_name] = defaulttags.register.tags[_name]
for _name in ALLOWED_FILTERS:
    register.filters[_name] = defaultfilters.register.filters[_name]
