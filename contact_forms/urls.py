from django.urls import path

from . import views

app_name = "contact_forms"

urlpatterns = [
    path("<slug:slug>/submit/", views.submit, name="submit"),
]
