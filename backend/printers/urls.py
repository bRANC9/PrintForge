from django.urls import path

from .views import PrintHistoryView

app_name = "printers"

urlpatterns = [
    path("history/", PrintHistoryView.as_view(), name="history"),
]
