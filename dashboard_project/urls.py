from django.contrib import admin
from django.urls import path
from core.views import home_dashboard, ads_dashboard, ads_accounts, ads_vincular, ads_crear_campana
from core.debug_view import debug_session

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home_dashboard, name='home'),
    path('ads/', ads_dashboard, name='ads'),
    path('ads/cuentas/', ads_accounts, name='ads_accounts'),
    path('ads/vincular/', ads_vincular, name='ads_vincular'),
    path('ads/crear-campana/', ads_crear_campana, name='ads_crear_campana'),
    path('debug-session/', debug_session),
]
