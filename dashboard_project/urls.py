from django.contrib import admin
from django.urls import path
from core.views import home_dashboard
from core.debug_view import debug_session

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home_dashboard, name='home'),
    path('debug-session/', debug_session),
]
