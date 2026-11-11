from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("mfa/setup", views.mfa_setup, name="mfa-setup"),
    path("mfa/verify", views.mfa_verify, name="mfa-verify"),
]
