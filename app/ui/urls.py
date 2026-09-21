from django.urls import path

from ui import views

app_name = "ui"

urlpatterns = [
    path("", views.home, name="home"),
]
