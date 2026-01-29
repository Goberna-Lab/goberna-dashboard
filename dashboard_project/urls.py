from django.urls import path
from core.views import home_dashboard

urlpatterns = [
    path('', home_dashboard, name='home'),
]
