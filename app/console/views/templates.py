"""Templates: list, new, editor (drafts), review (preview + decision), approvals."""

from __future__ import annotations

from typing import Any

from django.db.models import Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect

from campaigns.models import Campaign
from console.forms import NewTemplateForm, VersionContentForm
from console.views.base import console_view, page, run
from email_templates import preflight, services
from email_templates.models import EmailTemplate, TemplateVersion
from email_templates.rendering import RenderError, render

V = TemplateVersion.Status


@console_view
def template_list(request: HttpRequest) -> HttpResponse:
    category = request.GET.get("c", "")
    query = request.GET.get("q", "").strip()
    templates = EmailTemplate.objects.select_related("owner").prefetch_related(
        Prefetch("versions", queryset=TemplateVersion.objects.order_by("-number"))
    )
    if category in EmailTemplate.Category.values:
        templates = templates.filter(category=category)
    if query:
        templates = templates.filter(Q(name__icontains=query)
                                     | Q(versions__subject__icontains=query)).distinct()  # fmt: skip
    rows = []
    for t in templates.order_by("archived_at", "name"):
        versions = list(t.versions.all())
        live = next((v for v in versions if v.status == V.APPROVED), None)
        newest = versions[0] if versions else None
        in_progress = newest if newest and newest.status not in (V.APPROVED, V.RETIRED) else None
        if live is None and in_progress is None:
            live = next((v for v in versions if v.status == V.RETIRED), None)
        rows.append({"t": t, "live": live, "in_progress": in_progress, "newest": newest})
    return page(request, "console/templates.html", "templates", rows=rows, category=category,
                query=query, categories=EmailTemplate.Category.choices)  # fmt: skip


@console_view
def template_new(request: HttpRequest) -> HttpResponse:
    form = NewTemplateForm(request.POST or None, initial={
        "variables": '{\n "first_name": {"required": true}\n}',
        "sample_data": '{\n "first_name": "Ada"\n}',
    })  # fmt: skip
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        result = run(request, lambda: services.create_template(
            request.user, name=d["name"], category=d["category"], description=d["description"],
            subject=d["subject"], html_body=d["html_body"], text_body=d["text_body"],
            variables=d["variables"], sample_data=d["sample_data"]), "Draft v1 created.")  # fmt: skip
        if result:
            return redirect("console:template_version", pk=result[1].pk)
    return page(request, "console/template_new.html", "templates", form=form)


def _version(pk: int) -> TemplateVersion:
    return get_object_or_404(TemplateVersion.objects.select_related(
        "template", "created_by", "submitted_by", "reviewed_by"), pk=pk)  # fmt: skip


@console_view
def template_version(request: HttpRequest, pk: int) -> HttpResponse:
    """The editor. Non-drafts are frozen, so they open on the review page."""
    version = _version(pk)
    if not version.is_editable:
        return redirect("console:template_review", pk=pk)
    form = VersionContentForm(request.POST or None, initial=VersionContentForm.initial_for(version))
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "submit":
            run(request, lambda: services.submit(request.user, version),
                "Submitted. This version is now frozen and waiting for an approver.")  # fmt: skip
            return redirect("console:template_version", pk=pk)
        if form.is_valid():
            for field, value in form.cleaned_data.items():
                setattr(version, field, value)
            if run(request, lambda: services.save_draft(request.user, version), "Draft saved."):
                return redirect("console:template_version", pk=pk)
    findings = preflight.run(version)
    return page(request, "console/template_editor.html", "templates", v=version, form=form,
                findings=findings, fails=sum(f.level == "FAIL" for f in findings),
                warns=sum(f.level == "WARN" for f in findings),
                allowed_tags=("if", "for", "with", "firstof", "cycle", "spaceless", "comment"))  # fmt: skip


def _preview(version: TemplateVersion) -> dict[str, Any]:
    try:
        rendered = render(version, version.sample_data, preflight.preview_system_data(),
                          require_approved=False)  # fmt: skip
    except RenderError as exc:
        return {"render_error": str(exc)}
    return {"rendered": rendered}


@console_view
def template_review(request: HttpRequest, pk: int) -> HttpResponse:
    version = _version(pk)
    if request.method == "POST":
        action = request.POST.get("action", "")
        note = request.POST.get("note", "")
        user = request.user
        actions = {
            "approve": (lambda: services.approve(user, version, note), f"{version} approved."),
            "request_changes": (lambda: services.request_changes(user, version, note),
                                "Changes requested."),
            "reject": (lambda: services.reject(user, version, note), f"{version} rejected."),
            "retire": (lambda: services.retire(user, version, note),
                       f"{version} retired. Running campaigns keep using it."),
        }  # fmt: skip
        if action == "new_version":
            new = run(
                request, lambda: services.new_version_from(user, version), "New draft created."
            )
            if new:
                return redirect("console:template_version", pk=new.pk)
        elif action in actions:
            func, done = actions[action]
            run(request, func, done)
        return redirect("console:template_review", pk=pk)

    findings = preflight.run(version)
    previous = (TemplateVersion.objects.filter(template=version.template, number__lt=version.number)
                .order_by("-number").first())  # fmt: skip
    can_review = version.status == V.PENDING_APPROVAL and request.user.has_perm(
        "email_templates.approve_templateversion"
    )
    return page(request, "console/template_review.html", "templates", v=version,
                findings=findings, fails=sum(f.level == "FAIL" for f in findings),
                warns=sum(f.level == "WARN" for f in findings), previous=previous,
                changes=_changes(previous, version), can_review=can_review,
                **_preview(version))  # fmt: skip


def _changes(old: TemplateVersion | None, new: TemplateVersion) -> list[tuple[str, str]] | None:
    if old is None:
        return None
    out = []
    for field, label in (("subject", "Subject"), ("html_body", "HTML"), ("text_body", "Text"),
                         ("variables", "Variables"), ("sample_data", "Sample data")):  # fmt: skip
        a, b = getattr(old, field), getattr(new, field)
        if a == b:
            out.append((label, "unchanged"))
        elif isinstance(a, str) and field != "subject":
            old_lines, new_lines = set(a.splitlines()), set(b.splitlines())
            out.append(
                (label, f"+{len(new_lines - old_lines)} / −{len(old_lines - new_lines)} lines")
            )
        else:
            out.append((label, "changed"))
    return out


@console_view
def approvals(request: HttpRequest) -> HttpResponse:
    versions = (TemplateVersion.objects.filter(status=V.PENDING_APPROVAL)
                .select_related("template", "submitted_by").order_by("submitted_at"))  # fmt: skip
    campaigns = (Campaign.objects.filter(status=Campaign.Status.READY_FOR_REVIEW)
                 .select_related("owner", "template_version__template").order_by("submitted_at"))  # fmt: skip
    return page(
        request, "console/approvals.html", "approvals", versions=versions, campaigns=campaigns
    )
