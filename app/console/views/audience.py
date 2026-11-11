"""Contacts and consent: lists, file imports and segments."""

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect

from common.models import Stream
from consent.evaluation import marketing_consent_bulk
from console.forms import ContactImportForm, SegmentForm
from console.views.base import console_view, page, run
from contacts import imports
from contacts import services as contact_services
from contacts.models import Contact, ContactImport, ContactList, Segment

CONSENT_COUNT_LIMIT = 20_000  # above this, counting consent per page view is too slow


def _consented(ids: list[Any], lst: ContactList) -> int | None:
    if lst.stream != Stream.MARKETING or len(ids) > CONSENT_COUNT_LIMIT:
        return None
    return sum(v.granted for v in marketing_consent_bulk(ids, lst).values())


@console_view
def contacts(request: HttpRequest) -> HttpResponse:
    lists = []
    for lst in ContactList.objects.annotate(
        members=Count("memberships", filter=Q(memberships__contact__deleted_at__isnull=True))
    ).order_by("archived_at", "name"):
        consented = None
        if lst.stream == Stream.MARKETING and lst.members <= CONSENT_COUNT_LIMIT:
            ids = list(Contact.objects.live().filter(memberships__contact_list=lst)
                       .values_list("pk", flat=True))  # fmt: skip
            consented = _consented(ids, lst)
        lists.append({"list": lst, "consented": consented})
    totals = {
        "contacts": Contact.objects.live().count(),
        "erased": Contact.objects.filter(deleted_at__isnull=False).count(),
        "lists": len(lists),
    }
    segments = []
    for seg in Segment.objects.select_related("contact_list").order_by("contact_list__name", "name"):
        try:
            count: int | None = seg.members().count()
        except ValidationError:
            count = None  # invalid rules; the segment page says why
        segments.append({"segment": seg, "members": count})
    recent = ContactImport.objects.select_related("contact_list", "attested_by").order_by(
        "-created_at"
    )[:20]
    return page(request, "console/contacts.html", "audience", lists=lists, totals=totals,
                segments=segments, imports=recent)  # fmt: skip


# ─── File import ─────────────────────────────────────────────────────────


@console_view
def contact_import_new(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm("contacts.add_contactimport"):
        raise PermissionDenied("Importing contacts needs the Campaign Manager role.")
    form = ContactImportForm(request.POST or None, request.FILES or None,
                             initial={"contact_list": request.GET.get("list")})  # fmt: skip
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        if upload.size > settings.CONTACT_IMPORT_MAX_BYTES:
            form.add_error("file", f"Larger than {settings.CONTACT_IMPORT_MAX_BYTES:,} bytes.")
        else:
            imp = run(request, lambda: imports.start(
                request.user,
                contact_list=form.cleaned_data["contact_list"],
                filename=upload.name,
                content=upload.read(),
                consent_source=form.cleaned_data["consent_source"],
                attestation_text=form.cleaned_data["attestation_text"],
            ), "File received. It is checked row by row before anything is added.")  # fmt: skip
            if imp:
                return redirect("console:contact_import", pk=imp.pk)
    return page(request, "console/contact_import_form.html", "audience", form=form,
                max_bytes=settings.CONTACT_IMPORT_MAX_BYTES,
                max_rows=settings.CONTACT_IMPORT_MAX_ROWS)  # fmt: skip


@console_view
def contact_import_detail(request: HttpRequest, pk: str) -> HttpResponse:
    imp = get_object_or_404(
        ContactImport.objects.select_related("contact_list", "attested_by", "file"), pk=pk
    )
    skipped = [
        ("Invalid", imp.skipped_invalid, "bad address, bad consent_date, wrong column count"),
        ("Duplicate", imp.skipped_duplicate, "twice in the file, or already on the list"),
        ("Suppressed", imp.skipped_suppressed, "suppressed for this list's stream"),
        ("Opted out", imp.skipped_opted_out, "withdrew consent earlier, or erased"),
    ]
    return page(request, "console/contact_import.html", "audience", imp=imp, skipped=skipped)


# ─── Segments ────────────────────────────────────────────────────────────


@console_view
def segment_new(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm("contacts.add_segment"):
        raise PermissionDenied("Creating segments needs the Campaign Manager role.")
    lst = get_object_or_404(ContactList, pk=request.GET.get("list") or request.POST.get("list"))
    form = SegmentForm(request.POST or None, initial={"definition": "{}"})
    if request.method == "POST" and form.is_valid():
        seg = run(request, lambda: contact_services.create_segment(
            request.user, contact_list=lst, **form.cleaned_data), "Segment saved.")  # fmt: skip
        if seg:
            return redirect("console:segment", pk=seg.pk)
    return page(request, "console/segment.html", "audience", form=form, lst=lst, segment=None)


@console_view
def segment_detail(request: HttpRequest, pk: str) -> HttpResponse:
    seg = get_object_or_404(Segment.objects.select_related("contact_list"), pk=pk)
    form = SegmentForm(request.POST or None, initial={
        "name": seg.name, "definition": json.dumps(seg.definition, indent=2)})  # fmt: skip
    if request.method == "POST":
        if not request.user.has_perm("contacts.change_segment"):
            raise PermissionDenied("Editing segments needs the Campaign Manager role.")
        if form.is_valid() and run(request, lambda: contact_services.update_segment(
                request.user, seg, **form.cleaned_data), "Segment saved."):  # fmt: skip
            return redirect("console:segment", pk=seg.pk)
    stats: dict[str, Any] = {"error": None}
    try:
        ids = list(seg.members().values_list("pk", flat=True))
    except ValidationError as exc:
        stats["error"] = "; ".join(exc.messages)
    else:
        stats.update(
            members=len(ids),
            list_members=Contact.objects.live().filter(memberships__contact_list=seg.contact_list)
            .count(),
            consented=_consented(ids, seg.contact_list),
        )  # fmt: skip
    campaigns = seg.campaigns.order_by("-created_at")[:20]
    return page(request, "console/segment.html", "audience", form=form, lst=seg.contact_list,
                segment=seg, stats=stats, campaigns=campaigns)  # fmt: skip
