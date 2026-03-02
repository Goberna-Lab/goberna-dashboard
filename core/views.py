import csv
import json
from decimal import Decimal
from io import StringIO, BytesIO
import datetime

from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import (
    Case,
    Count,
    DateTimeField,
    DecimalField,
    ExpressionWrapper,
    F,
    Q as DJANGO_Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, ExtractYear, ExtractMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.conf import settings
from django.core.cache import cache
import hashlib

from .models import Venta, Cuota, Moneda, PerfilUsuario, DetalleVenta

try:
    import openpyxl
except ImportError:
    openpyxl = None


DASHBOARD_GLOBAL_USER_IDS = {7, 8, 35}
ADMIN_GROUP_IDS = (2,)
DASHBOARD_SCOPE_GROUP_NAMES = ("Scope - Dashboard Satelite Global",)
MEDIO_LABELS = {
    "organico": "Orgánico",
    "pagado": "Pagado",
    "referente": "Referente",
    "remarketing": "Remarketing",
    "postventa": "Postventa",
}


def _is_admin_user(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(
        user.is_superuser
        or user.id in DASHBOARD_GLOBAL_USER_IDS
        or user.groups.filter(id__in=ADMIN_GROUP_IDS).exists()
        or user.groups.filter(name__in=DASHBOARD_SCOPE_GROUP_NAMES).exists()
    )


def _medio_label(value: str) -> str:
    key = (value or "").strip().lower()
    if not key:
        return "Sin medio"
    return MEDIO_LABELS.get(key, key.replace("_", " ").title())

@login_required
def home_dashboard(request):
    """Dashboard de ventas por usuario actual con opción de exportar reportes."""
    is_admin = _is_admin_user(request.user)

    # Base scope por usuario
    if is_admin:
        ventas_scope = Venta.objects.all()
    else:
        ventas_scope = Venta.objects.filter(usuario=request.user)
    cuotas_scope = Cuota.objects.filter(venta__in=ventas_scope)

    # Universo de ventas del dashboard:
    # - Pagadas (1) y Pendientes (2)..
    # - Sin exigir confirmación de Tesorería para estado=1.
    ventas_base = ventas_scope.filter(estado__in=[1, 2])
    cuotas_base = Cuota.objects.filter(venta__in=ventas_base)

    # CÁLCULO EN DÓLARES (USD)
    
    # Anotar Ventas con monto_usd
    ventas_qs = ventas_base.annotate(
        tasa_cambio=Coalesce(
            'radio_multiplicador_usado', 
            'moneda__radioMultiplicador', 
            1,
            output_field=DecimalField()
        )
    ).annotate(
        tasa_final=Case(
            When(tasa_cambio=0, then=Value(1)),
            default=F('tasa_cambio'),
            output_field=DecimalField()
        )
    ).annotate(
        monto_usd=ExpressionWrapper(
            F('monto_total') / F('tasa_final'),
            output_field=DecimalField(max_digits=12, decimal_places=2)
        )
    ).annotate(
        # Regla de fecha del dashboard:
        # - Siempre usar fecha_venta.
        # - Fallback a fecha_registro solo si fecha_venta viene nula.
        fecha_evento=Coalesce("fecha_venta", "fecha_registro", output_field=DateTimeField())
    )

    # Anotar Cuotas con monto_usd
    cuotas_qs = cuotas_base.select_related('venta', 'venta__moneda').annotate(
        tasa_cambio=Coalesce(
            'venta__radio_multiplicador_usado', 
            'venta__moneda__radioMultiplicador', 
            1,
            output_field=DecimalField()
        )
    ).annotate(
        tasa_final=Case(
            When(tasa_cambio=0, then=Value(1)),
            default=F('tasa_cambio'),
            output_field=DecimalField()
        )
    ).annotate(
        monto_usd=ExpressionWrapper(
            F('monto_total') / F('tasa_final'),
            output_field=DecimalField(max_digits=12, decimal_places=2)
        )
    )

    # Exportaciones
    reporte = request.GET.get("reporte")
    fmt = request.GET.get("fmt", "csv").lower()
    if reporte == "ventas":
        return _export_ventas(ventas_qs, fmt)
    if reporte == "cuotas":
        return _export_cuotas(cuotas_qs, fmt)

    # CACHÉ DE ESTADÍSTICAS
    params_sorted = sorted(request.GET.items())
    params_hash = hashlib.md5(str(params_sorted).encode()).hexdigest()
    # v11: ranking de vendedores usa deuda/pagado desde cuotas (USD)
    cache_key = f"dash_stats_v11_pending_paid_{request.user.id}_{is_admin}_{params_hash}"
    cached_stats = cache.get(cache_key)

    if cached_stats:
        ventas_resumen = cached_stats["ventas_resumen"]
        ventas_por_estado = cached_stats["ventas_por_estado"]
        monthly = cached_stats["monthly"]
        cuotas_por_estado = cached_stats["cuotas_por_estado"]
        ventas_chart_labels = cached_stats["ventas_chart_labels"]
        ventas_chart_data = cached_stats["ventas_chart_data"]
        ventas_estado_chart_data = cached_stats["ventas_estado_chart_data"]
        cuotas_estado_chart_data = cached_stats["cuotas_estado_chart_data"]
        cat_labels = cached_stats.get("cat_labels", [])
        cat_data = cached_stats.get("cat_data", [])
        books_labels = cached_stats.get("books_labels", [])
        books_data = cached_stats.get("books_data", [])
        courses_labels = cached_stats.get("courses_labels", [])
        courses_data = cached_stats.get("courses_data", [])
        vendors_data = cached_stats.get("vendors_data", [])
        country_labels = cached_stats.get("country_labels", [])
        country_data = cached_stats.get("country_data", [])
        medium_labels = cached_stats.get("medium_labels", [])
        medium_data = cached_stats.get("medium_data", [])
    else:
        # CALCULAR
        try:
            ventas_resumen = ventas_qs.aggregate(
                total=Count("id"),
                monto=Sum(ExpressionWrapper(
                    F('monto_total') / Case(
                        When(moneda__radioMultiplicador__isnull=True, then=Value(1)),
                        When(moneda__radioMultiplicador=0, then=Value(1)),
                        default=F('moneda__radioMultiplicador'),
                        output_field=DecimalField()
                    ),
                    output_field=DecimalField()
                ))
            )
        except Exception:
            ventas_resumen = {"total": 0, "monto": 0}

        # Gráficos (Mensual)
        monthly_qs = ventas_qs.annotate(
            year=ExtractYear('fecha_evento'),
            month=ExtractMonth('fecha_evento')
        ).values('year', 'month').annotate(
            total_usd=Sum(ExpressionWrapper(
                F('monto_total') / Case(
                    When(moneda__radioMultiplicador__isnull=True, then=Value(1)),
                    When(moneda__radioMultiplicador=0, then=Value(1)),
                    default=F('moneda__radioMultiplicador'),
                    output_field=DecimalField()
                ),
                output_field=DecimalField()
            )),
            count=Count('id')
        ).order_by('year', 'month')

        monthly = []
        for m in monthly_qs:
            try:
                y = m['year'] or 2024
                mo = m['month'] or 1
                date_obj = datetime.date(y, mo, 1)
                m_iso = date_obj.isoformat()
            except:
                m_iso = "Unknown"
            monthly.append({
                "month": m_iso,
                "total": m['total_usd'] or 0,
                "count": m['count']
            })

        # Estados
        ventas_estado_qs = ventas_scope.values('estado').annotate(total=Count('id'))
        ventas_estado_keys = [1, 2, 3, 4, 5, 6, 7]
        ventas_estado_map = {int(v["estado"]): int(v["total"] or 0) for v in ventas_estado_qs}
        ventas_estado_chart_data = [ventas_estado_map.get(k, 0) for k in ventas_estado_keys]
        
        ventas_por_estado = [] 

        cuotas_estado_qs = cuotas_scope.values('estado').annotate(total=Count('id'))
        cuotas_estado_keys = [1, 2, 3, 4, 5]
        cuotas_estado_map = {int(c["estado"]): int(c["total"] or 0) for c in cuotas_estado_qs}
        cuotas_estado_chart_data = [cuotas_estado_map.get(k, 0) for k in cuotas_estado_keys]
        cuotas_por_estado = []

        # Categorías
        detalles_qs = DetalleVenta.objects.filter(venta__in=ventas_qs).select_related(
            'venta', 'venta__moneda', 'producto__codigo_categoria', 'producto__codigo_negocio'
        ).annotate(
            tasa_cambio=Coalesce(
                'venta__radio_multiplicador_usado', 
                'venta__moneda__radioMultiplicador', 
                1,
                output_field=DecimalField()
            )
        ).annotate(
            tasa_final=Case(
                When(tasa_cambio=0, then=Value(1)),
                default=F('tasa_cambio'),
                output_field=DecimalField()
            )
        ).annotate(
            monto_usd=ExpressionWrapper(
                F('precio_total') / F('tasa_final'),
                output_field=DecimalField(max_digits=12, decimal_places=2)
            )
        )
        
        cats = detalles_qs.values('producto__codigo_categoria__nombre_categoria').annotate(
            total=Sum('monto_usd')
        ).order_by('-total')

        cat_labels = [c['producto__codigo_categoria__nombre_categoria'] for c in cats]
        cat_data = [float(c['total']) for c in cats]

        # Top Ranking Libros (Físico / Preventa) - considerar IDs y nombre de categoría
        libros_filter = DJANGO_Q(producto__codigo_categoria__in=[1, 15]) | DJANGO_Q(
            producto__codigo_categoria__nombre_categoria__icontains="fisico"
        ) | DJANGO_Q(producto__codigo_categoria__nombre_categoria__icontains="físico") | DJANGO_Q(
            producto__codigo_categoria__nombre_categoria__icontains="preventa"
        )
        top_books_qs = (
            DetalleVenta.objects.filter(
                libros_filter,
                venta__in=ventas_qs,
                producto__codigo_negocio__nombre_negocio__icontains="editorial"
            )
            .values("producto__nombre_producto")
            .annotate(total_qty=Sum("cantidad"))
            .order_by("-total_qty")[:10]
        )
        
        books_labels = [b['producto__nombre_producto'] for b in top_books_qs]
        books_data = [int(b['total_qty']) for b in top_books_qs]
        
        # Top Ranking Cursos (5=Online, 6=Virtual)
        top_courses_qs = DetalleVenta.objects.filter(
            venta__in=ventas_qs,
            producto__codigo_categoria__in=[5, 6]
        ).values('producto__nombre_producto').annotate(
            total_qty=Sum('cantidad')
        ).order_by('-total_qty')[:10]
        
        courses_labels = [b['producto__nombre_producto'] for b in top_courses_qs]
        courses_data = [int(b['total_qty']) for b in top_courses_qs]
        
        # Ranking Vendedores (cantidad de ventas + montos de cuotas en USD)
        try:
            vendors_qs = ventas_qs.values(
                'usuario_id', 'usuario__username', 'usuario__first_name', 'usuario__last_name'
            ).annotate(
                total_ventas=Count('id'),
                total_monto=Sum('monto_usd')
            ).order_by('-total_ventas', '-total_monto')[:20]

            # Deuda/pagado por vendedor desde cuotas (mismo universo del dashboard).
            # Deuda: cuotas estado=2 (Pendiente)
            # Pagado: cuotas estado=1 (Pagado)
            vendor_cuotas_qs = cuotas_qs.values('venta__usuario_id').annotate(
                deuda_usd=Sum(
                    Case(
                        When(estado=2, then=F("monto_usd")),
                        default=Value(Decimal("0.00")),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                ),
                monto_pagado_usd=Sum(
                    Case(
                        When(estado=1, then=F("monto_usd")),
                        default=Value(Decimal("0.00")),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                ),
            )
            vendor_cuotas_map = {
                int(row["venta__usuario_id"]): {
                    "deuda_usd": float(row.get("deuda_usd") or 0),
                    "monto_pagado_usd": float(row.get("monto_pagado_usd") or 0),
                }
                for row in vendor_cuotas_qs
                if row.get("venta__usuario_id") is not None
            }

            vendors_data = []
            for v in vendors_qs:
                name = f"{v['usuario__first_name']} {v['usuario__last_name']}".strip() or v['usuario__username']
                cuota_stats = vendor_cuotas_map.get(int(v['usuario_id']), {"deuda_usd": 0.0, "monto_pagado_usd": 0.0})
                vendors_data.append({
                    "user_id": v['usuario_id'],
                    "username": v['usuario__username'],
                    "name": name,
                    "count": v['total_ventas'],
                    "deuda_usd": cuota_stats["deuda_usd"],
                    "monto_pagado_usd": cuota_stats["monto_pagado_usd"],
                    "monto_total_usd": float(v['total_monto'] or 0),
                    # Compatibilidad con frontend previo.
                    "amount": float(v['total_monto'] or 0)
                })
        except Exception:
            vendors_data = []

        # Ventas por país (basado en país del cliente; fallback a venta.pais)
        try:
            country_qs = ventas_qs.values(
                "cliente__pais__nombre", "pais__nombre"
            ).annotate(
                total_ventas=Count("id"),
                total_monto=Sum("monto_usd")
            )

            country_bucket = {}
            for row in country_qs:
                country_name = (row.get("cliente__pais__nombre") or row.get("pais__nombre") or "").strip() or "Sin país"
                bucket = country_bucket.setdefault(country_name, {"count": 0, "amount": 0.0})
                bucket["count"] += int(row.get("total_ventas") or 0)
                bucket["amount"] += float(row.get("total_monto") or 0)

            top_country = sorted(
                country_bucket.items(),
                key=lambda x: (-x[1]["count"], -x[1]["amount"], x[0])
            )[:12]

            country_labels = [k for k, _ in top_country]
            country_data = [v["count"] for _, v in top_country]
        except Exception:
            country_labels = []
            country_data = []

        # Ventas por medio
        try:
            medium_qs = ventas_qs.values("medio").annotate(
                total_ventas=Count("id")
            )

            medium_bucket = {}
            for row in medium_qs:
                label = _medio_label(row.get("medio"))
                medium_bucket[label] = medium_bucket.get(label, 0) + int(row.get("total_ventas") or 0)

            top_medium = sorted(
                medium_bucket.items(),
                key=lambda x: (-x[1], x[0])
            )[:10]

            medium_labels = [k for k, _ in top_medium]
            medium_data = [v for _, v in top_medium]
        except Exception:
            medium_labels = []
            medium_data = []

        ventas_chart_labels = [m["month"] for m in monthly]
        ventas_chart_data = [float(m["total"]) for m in monthly]
        
        if not ventas_chart_data and ventas_resumen.get("monto"):
            ventas_chart_labels = ["Actual"]
            ventas_chart_data = [float(ventas_resumen.get("monto") or 0)]
        
        cache.set(cache_key, {
            "ventas_resumen": ventas_resumen,
            "ventas_por_estado": ventas_por_estado,
            "monthly": monthly,
            "cuotas_por_estado": cuotas_por_estado,
            "ventas_chart_labels": ventas_chart_labels,
            "ventas_chart_data": ventas_chart_data,
            "ventas_estado_chart_data": ventas_estado_chart_data,
            "cuotas_estado_chart_data": cuotas_estado_chart_data,
            "cat_labels": cat_labels,
            "cat_data": cat_data,
            "books_labels": books_labels,
            "books_data": books_data,
            "courses_labels": courses_labels,
            "courses_data": courses_data,
            "vendors_data": vendors_data,
            "country_labels": country_labels,
            "country_data": country_data,
            "medium_labels": medium_labels,
            "medium_data": medium_data,
        }, 300)

    # API Carga Asíncrona
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        list_cache_key = f"dash_list_v8_pending_paid_{request.user.id}_{is_admin}"
        cached_lists = cache.get(list_cache_key)
        
        if cached_lists:
            return JsonResponse(cached_lists, encoder=DjangoJSONEncoder)

        # Para Vendedores dinámico en JS, necesitamos saber quien vendió cada venta.
        # Ya tenemos ventas_detalle, pero falta el nombre del vendedor.
        # Agregamos username a ventas_detalle query arriba? 
        # Mejor re-hacemos la query de ventas_detalle para incluir usuario info.
        
        ventas_detalle = list(
            ventas_qs.values(
                "id", "folio_venta", "monto_usd", "estado", "fecha_evento",
                "usuario_id", "usuario__username", "usuario__first_name", "usuario__last_name",
                "cliente__pais__nombre", "pais__nombre", "medio"
            )
        )
        for v in ventas_detalle:
            v["monto_total"] = v.pop("monto_usd")
            # Mantener compatibilidad con el frontend actual (usa v.fecha_venta)
            v["fecha_venta"] = v.pop("fecha_evento", None)
            v["vendedor"] = f"{v['usuario__first_name']} {v['usuario__last_name']}".strip() or v['usuario__username']
            v["pais_cliente"] = (v.pop("cliente__pais__nombre", None) or v.pop("pais__nombre", None) or "").strip() or "Sin país"
            v["medio_label"] = _medio_label(v.get("medio"))

        cuotas_detalle = list(
            cuotas_qs.values(
                "id", "venta__folio_venta", "monto_usd", "estado", "fecha_vencimiento",
            )
        )
        for c in cuotas_detalle:
            c["monto_total"] = c.pop("monto_usd")
        
        # Detalles con categoría para filtrar en JS
        # (Reutilizamos la query de detalles_qs pero sin agrupar, filtrando por ID venta)
        # Nota: detalles_qs ya depende de ventas_qs, asi que es consistente.
        # Pero ventas_qs es QuerySet, asi que podemos reconstruir detalles query rapido:
        
        detalles_export = DetalleVenta.objects.filter(venta__in=ventas_qs).select_related(
            'venta', 'venta__moneda', 'producto__codigo_categoria', 'producto__codigo_negocio'
        ).annotate(
            tasa_cambio=Coalesce('venta__radio_multiplicador_usado', 'venta__moneda__radioMultiplicador', 1, output_field=DecimalField())
        ).annotate(
            tasa_final=Case(When(tasa_cambio=0, then=Value(1)), default=F('tasa_cambio'), output_field=DecimalField())
        ).annotate(
            monto_usd=ExpressionWrapper(F('precio_total') / F('tasa_final'), output_field=DecimalField(max_digits=12, decimal_places=2))
        ).values(
            "venta_id", 
            "producto__codigo_producto",
            "producto__codigo_categoria__nombre_categoria", 
            "producto__codigo_categoria__nombre_categoria", 
            "monto_usd",
            "producto__codigo_categoria", # Para filtrar ID en JS
            "producto__codigo_negocio",
            "producto__codigo_negocio__nombre_negocio",
            "producto__nombre_producto",
            "cantidad"
        )

        response_data = {
            "ventas_detalle": ventas_detalle,
            "cuotas_detalle": cuotas_detalle,
            "ventas_estado_detalle": list(ventas_scope.values("estado")),
            "cuotas_estado_detalle": list(cuotas_scope.values("estado")),
            "detalles_categoria": list(detalles_export), 
        }
        cache.set(list_cache_key, response_data, 300)
        return JsonResponse(response_data, encoder=DjangoJSONEncoder)
    
    # Perfil
    perfil = None
    try:
        # PerfilUsuario usa OneToOne con user, pero en managed=False puede fallar si no hay user instance real
        # Si estamos debugueando, omitimos perfil o lo buscamos manual
        if request.user.is_authenticated:
            perfil = PerfilUsuario.objects.get(user=request.user)
        else:
            # Fake perfil lookup if needed, or pass None
            pass
    except PerfilUsuario.DoesNotExist:
        pass

    username_str = "Visitante (Debug)"
    if request.user.is_authenticated:
        username_str = request.user.get_full_name() or request.user.username
    else:
        # Intentar simular nombre si quisieramos, pero 'Visitante' basta
        pass

    context = {
        "username": username_str,
        "perfil": perfil,
        "ventas_resumen": {
            "total": ventas_resumen.get("total") or 0,
            "monto": ventas_resumen.get("monto") or Decimal("0.00"),
        },
        "is_admin": is_admin,
        "ventas_por_estado": list(ventas_por_estado),
        "monthly": list(monthly),
        "cuotas_por_estado": list(cuotas_por_estado),
        "ventas_detalle_json": "[]", 
        "cuotas_detalle_json": "[]",
        "chart_ventas_labels": json.dumps(ventas_chart_labels, cls=DjangoJSONEncoder),
        "chart_ventas_data": json.dumps(ventas_chart_data, cls=DjangoJSONEncoder),
        "chart_ventas_estado": json.dumps(ventas_estado_chart_data, cls=DjangoJSONEncoder),
        "chart_ventas_estado": json.dumps(ventas_estado_chart_data, cls=DjangoJSONEncoder),
        "chart_cuotas_estado": json.dumps(cuotas_estado_chart_data, cls=DjangoJSONEncoder),
        "chart_cat_labels": json.dumps(cat_labels, cls=DjangoJSONEncoder),
        "chart_cat_labels": json.dumps(cat_labels, cls=DjangoJSONEncoder),
        "chart_cat_data": json.dumps(cat_data, cls=DjangoJSONEncoder),
        "chart_books_labels": json.dumps(books_labels, cls=DjangoJSONEncoder),
        "chart_books_data": json.dumps(books_data, cls=DjangoJSONEncoder),
        "chart_courses_labels": json.dumps(courses_labels, cls=DjangoJSONEncoder),
        "chart_courses_data": json.dumps(courses_data, cls=DjangoJSONEncoder),
        "chart_country_labels": json.dumps(country_labels, cls=DjangoJSONEncoder),
        "chart_country_data": json.dumps(country_data, cls=DjangoJSONEncoder),
        "chart_medium_labels": json.dumps(medium_labels, cls=DjangoJSONEncoder),
        "chart_medium_data": json.dumps(medium_data, cls=DjangoJSONEncoder),
        "vendors_data": json.dumps(vendors_data, cls=DjangoJSONEncoder),
        # Variables extra para el template satélite
        "MAIN_APP_URL": getattr(settings, 'MAIN_APP_URL', ''),
    }
    return render(request, "home.html", context)


def _export_ventas(queryset, fmt: str = "csv"):
    # (Misma implementación de exportación)
    def _naive(dt):
        if not dt: return ""
        if hasattr(dt, "tzinfo") and dt.tzinfo: return dt.replace(tzinfo=None)
        return dt

    def _fecha_export(v):
        return getattr(v, "fecha_evento", None) or getattr(v, "fecha_venta", None) or getattr(v, "fecha_registro", None)

    if fmt in ("xls", "xlsx") and openpyxl:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Ventas"
        ws.append(["Folio", "Monto (USD)", "Estado", "Fecha"])
        for v in queryset:
            ws.append([
                v.folio_venta, float(v.monto_usd or 0), v.estado,
                _naive(_fecha_export(v)),
            ])
        buffer = BytesIO()
        wb.save(buffer)
        resp = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = "attachment; filename=ventas.xlsx"
        return resp
        
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Folio", "Monto (USD)", "Estado", "Fecha"])
    for v in queryset:
        writer.writerow([v.folio_venta, v.monto_usd, v.estado, _fecha_export(v)])
    resp = HttpResponse(buffer.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = "attachment; filename=ventas.csv"
    return resp


def _export_cuotas(queryset, fmt: str = "csv"):
    def _naive(dt):
        if not dt: return ""
        if hasattr(dt, "tzinfo") and dt.tzinfo: return dt.replace(tzinfo=None)
        return dt

    if fmt in ("xls", "xlsx") and openpyxl:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Cuotas"
        ws.append(["Venta", "Cuota", "Monto (USD)", "Estado", "Vence"])
        for c in queryset:
            # c.venta ya se trajo con select_related
            ws.append([
                c.venta.folio_venta, c.numero_cuota, float(c.monto_usd or 0), c.estado,
                _naive(c.fecha_vencimiento) if hasattr(c, "fecha_vencimiento") else "",
            ])
        buffer = BytesIO()
        wb.save(buffer)
        resp = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = "attachment; filename=cuotas.xlsx"
        return resp

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Venta", "Cuota", "Monto (USD)", "Estado", "Vence"])
    for c in queryset:
        writer.writerow([c.venta.folio_venta, c.numero_cuota, c.monto_usd, c.estado, c.fecha_vencimiento])
    resp = HttpResponse(buffer.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = "attachment; filename=cuotas.csv"
    return resp
