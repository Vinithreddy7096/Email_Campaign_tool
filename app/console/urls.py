from django.urls import path

from console.views import audience, campaigns, deliverability, governance, overview, templates

app_name = "console"

urlpatterns = [
    path("", overview.today, name="today"),
    path("login/", governance.LoginView.as_view(), name="login"),
    path("logout/", governance.LogoutView.as_view(), name="logout"),
    # Sending
    path("campaigns/", campaigns.campaign_list, name="campaigns"),
    path("campaigns/new/", campaigns.campaign_new, name="campaign_new"),
    path("campaigns/<uuid:pk>/", campaigns.campaign_detail, name="campaign"),
    path("campaigns/<uuid:pk>/edit/", campaigns.campaign_edit, name="campaign_edit"),
    path("templates/", templates.template_list, name="templates"),
    path("templates/new/", templates.template_new, name="template_new"),
    path("templates/versions/<int:pk>/", templates.template_version, name="template_version"),
    path("templates/versions/<int:pk>/review/", templates.template_review, name="template_review"),
    path("approvals/", templates.approvals, name="approvals"),
    path("contacts/", audience.contacts, name="audience"),
    path("contacts/import/", audience.contact_import_new, name="contact_import_new"),
    path("contacts/imports/<uuid:pk>/", audience.contact_import_detail, name="contact_import"),
    path("contacts/segments/new/", audience.segment_new, name="segment_new"),
    path("contacts/segments/<uuid:pk>/", audience.segment_detail, name="segment"),
    # Deliverability
    path("domains/", deliverability.domains, name="domains"),
    path("events/", deliverability.events, name="events"),
    path("suppressions/", deliverability.suppressions, name="suppressions"),
    path("controls/", deliverability.controls, name="controls"),
    # Governance
    path("audit/", governance.audit, name="audit"),
    path("unsubscribe-page/", governance.unsubscribe_preview, name="unsubscribe"),
]
