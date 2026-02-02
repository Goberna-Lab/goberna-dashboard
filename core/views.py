import csv
import json
from decimal import Decimal
from io import StringIO, BytesIO
import datetime

from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Sum, F, ExpressionWrapper, DecimalField, Value, Case, When
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

@login_required
def home_dashboard(request):
    """Dashboard de ventas por usuario actual con opción de exportar reportes."""
    is_admin = request.user.groups.filter(id=2).exists()
    
    # Base querysets
    if is_admin:
        ventas_base = Venta.objects.all()
        cuotas_base = Cuota.objects.all()
    else:
        ventas_base = Venta.objects.filter(usuario=request.user)
        cuotas_base = Cuota.objects.filter(venta__usuario=request.user)

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
    # v4: fuerza a recalcular para que el ranking de libros ignore los filtros del dashboard
    cache_key = f"dash_stats_v4_books_{request.user.id}_{is_admin}_{params_hash}"
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
            year=ExtractYear('fecha_venta'),
            month=ExtractMonth('fecha_venta')
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
        ventas_estado_qs = ventas_qs.values('estado').annotate(total=Count('id'))
        ventas_estado_chart_data = [0,0,0,0]
        for v in ventas_estado_qs:
            idx = v['estado'] - 1
            if 0 <= idx < 4:
                ventas_estado_chart_data[idx] = v['total']
        
        ventas_por_estado = [] 

        cuotas_estado_qs = cuotas_qs.values('estado').annotate(total=Count('id'))
        cuotas_estado_chart_data = [0,0,0]
        for c in cuotas_estado_qs:
            idx = c['estado'] - 1
            if 0 <= idx < 3:
                cuotas_estado_chart_data[idx] = c['total']
        cuotas_por_estado = []

        # Categorías
        detalles_qs = DetalleVenta.objects.filter(venta__in=ventas_qs).select_related(
            'venta', 'venta__moneda', 'producto__codigo_categoria'
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
        from django.db.models import Q
        libros_filter = Q(producto__codigo_categoria__in=[1, 15]) | Q(
            producto__codigo_categoria__nombre_categoria__icontains="fisico"
        ) | Q(producto__codigo_categoria__nombre_categoria__icontains="físico") | Q(
            producto__codigo_categoria__nombre_categoria__icontains="preventa"
        )
        top_books_qs = (
            DetalleVenta.objects.filter(libros_filter)
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
        
        # Ranking Vendedores (Por cantidad de ventas)
        # Necesitamos usuario__username o usuario__first_name
        # Como Venta es managed=False y usuario es FK a auth_user (que si existe en default db o misma db),
        # el join deberia funcionar si estamos en la misma DB. Sino, podria fallar.
        # Asumimos misma DB o configuracion correcta de router.
        try:
            vendors_qs = ventas_qs.values(
                'usuario__username', 'usuario__first_name', 'usuario__last_name'
            ).annotate(
                total_ventas=Count('id'),
                total_monto=Sum('monto_usd')
            ).order_by('-total_ventas')[:20]
            
            vendors_data = []
            for v in vendors_qs:
                name = f"{v['usuario__first_name']} {v['usuario__last_name']}".strip() or v['usuario__username']
                vendors_data.append({
                    "name": name,
                    "count": v['total_ventas'],
                    "amount": float(v['total_monto'] or 0)
                })
        except Exception:
            vendors_data = []

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
        }, 300)

    # API Carga Asíncrona
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        list_cache_key = f"dash_list_v2_{request.user.id}_{is_admin}"
        cached_lists = cache.get(list_cache_key)
        
        if cached_lists:
            return JsonResponse(cached_lists, encoder=DjangoJSONEncoder)

        # Para Vendedores dinámico en JS, necesitamos saber quien vendió cada venta.
        # Ya tenemos ventas_detalle, pero falta el nombre del vendedor.
        # Agregamos username a ventas_detalle query arriba? 
        # Mejor re-hacemos la query de ventas_detalle para incluir usuario info.
        
        ventas_detalle = list(
            ventas_qs.values(
                "id", "folio_venta", "monto_usd", "estado", "fecha_venta",
                "usuario__username", "usuario__first_name", "usuario__last_name"
            )
        )
        for v in ventas_detalle:
            v["monto_total"] = v.pop("monto_usd")
            v["vendedor"] = f"{v['usuario__first_name']} {v['usuario__last_name']}".strip() or v['usuario__username']

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
            'venta', 'venta__moneda', 'producto__codigo_categoria'
        ).annotate(
            tasa_cambio=Coalesce('venta__radio_multiplicador_usado', 'venta__moneda__radioMultiplicador', 1, output_field=DecimalField())
        ).annotate(
            tasa_final=Case(When(tasa_cambio=0, then=Value(1)), default=F('tasa_cambio'), output_field=DecimalField())
        ).annotate(
            monto_usd=ExpressionWrapper(F('precio_total') / F('tasa_final'), output_field=DecimalField(max_digits=12, decimal_places=2))
        ).values(
            "venta_id", 
            "producto__codigo_categoria__nombre_categoria", 
            "producto__codigo_categoria__nombre_categoria", 
            "monto_usd",
            "producto__codigo_categoria", # Para filtrar ID en JS
            "producto__nombre_producto",
            "cantidad"
        )

        response_data = {
            "ventas_detalle": ventas_detalle,
            "cuotas_detalle": cuotas_detalle,
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

    if fmt in ("xls", "xlsx") and openpyxl:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Ventas"
        ws.append(["Folio", "Monto (USD)", "Estado", "Fecha"])
        for v in queryset:
            ws.append([
                v.folio_venta, float(v.monto_usd or 0), v.estado,
                _naive(v.fecha_venta) if hasattr(v, "fecha_venta") else "",
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
        writer.writerow([v.folio_venta, v.monto_usd, v.estado, v.fecha_venta])
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
