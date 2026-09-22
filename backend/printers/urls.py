from django.urls import path

from .views import PrinterListView, PrintHistoryView

app_name = "printers"

urlpatterns = [
    path("", PrinterListView.as_view(), name="list"),
    path("history/", PrintHistoryView.as_view(), name="history"),
]
