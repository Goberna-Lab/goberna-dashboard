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

from .models import Venta, Cuota, Moneda, PerfilUsuario, DetalleVenta, Division, Negocio, Pais

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
    cache_key = f"dash_stats_v3_1_{request.user.id}_{is_admin}"
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
        cat_labels = cached_stats["cat_labels"]
        cat_data = cached_stats["cat_data"]
        # V3 Data (safe get)
        stats_v3 = {
            "div_labels": cached_stats.get("div_labels", []),
            "div_data": cached_stats.get("div_data", []),
            "neg_labels": cached_stats.get("neg_labels", []),
            "neg_data": cached_stats.get("neg_data", []),
            "medio_labels": cached_stats.get("medio_labels", []),
            "medio_data": cached_stats.get("medio_data", []),
            "pais_list": cached_stats.get("pais_list", []),
            "sales_list": cached_stats.get("sales_list", []),
            "prod_list": cached_stats.get("prod_list", []),
        }

    else:
        # === CÁLCULOS ===
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

        # === AGREGACIONES V3 ===

        # 1. Ventas por División
        div_data = detalles_qs.values('producto__codigo_division__nombre_division').annotate(
            total=Sum('monto_usd')
        ).order_by('-total')
        
        # 2. Ventas por Negocio
        neg_data = detalles_qs.values('producto__codigo_negocio__nombre_negocio').annotate(
            total=Sum('monto_usd')
        ).order_by('-total')
        
        # 3. Ventas por Medio
        ventas_annotated = ventas_qs.annotate(
            tasa_cambio=Coalesce('radio_multiplicador_usado', 'moneda__radioMultiplicador', 1, output_field=DecimalField())
        ).annotate(
            tasa_final=Case(When(tasa_cambio=0, then=Value(1)), default=F('tasa_cambio'), output_field=DecimalField())
        ).annotate(
            monto_usd_v=ExpressionWrapper(F('monto_total') / F('tasa_final'), output_field=DecimalField(max_digits=12, decimal_places=2))
        )

        medio_data = ventas_annotated.values('medio').annotate(total=Sum('monto_usd_v')).order_by('-total')
        
        # 4. Ventas por Pais
        pais_data = ventas_annotated.values('pais__nombre').annotate(
            total=Sum('monto_usd_v'), 
            count=Count('id')
        ).order_by('-total')

        # 5. Top Vendedores
        sales_data = ventas_annotated.values('usuario__first_name', 'usuario__last_name').annotate(
            total=Sum('monto_usd_v'),
            count=Count('id') 
        ).order_by('-total')[:10]
        
        # 6. Top Productos
        prod_data = detalles_qs.values('producto__nombre_producto').annotate(
            total=Sum('monto_usd'),
            count=Sum('cantidad')
        ).order_by('-total')[:10]

        # Serialización
        stats_v3 = {
            "div_labels": [d['producto__codigo_division__nombre_division'] or 'Sin División' for d in div_data],
            "div_data": [float(d['total']) for d in div_data],
            "neg_labels": [d['producto__codigo_negocio__nombre_negocio'] or 'Sin Negocio' for d in neg_data],
            "neg_data": [float(d['total']) for d in neg_data],
            "medio_labels": [d['medio'] or 'Desconocido' for d in medio_data],
            "medio_data": [float(d['total']) for d in medio_data],
            "pais_list": list(pais_data),
            "sales_list": list(sales_data),
            "prod_list": list(prod_data),
        }

        # === KPIs Reales ===
        from django.utils import timezone
        now = timezone.now()
        today = now.date()
        start_week = today - datetime.timedelta(days=today.weekday())
        
        # 1. Venta del Día
        venta_dia_qs = ventas_annotated.filter(fecha_venta__date=today)
        venta_dia_res = venta_dia_qs.aggregate(
            total=Count('id'), 
            monto=Sum('monto_usd_v')
        )
        stats_v3["kpi_dia_count"] = venta_dia_res['total'] or 0
        stats_v3["kpi_dia_monto"] = float(venta_dia_res['monto'] or 0)

        # 2. Avance Semanal
        venta_sem_qs = ventas_annotated.filter(fecha_venta__date__gte=start_week)
        venta_sem_res = venta_sem_qs.aggregate(
            total=Count('id'), 
            monto=Sum('monto_usd_v')
        )
        stats_v3["kpi_sem_monto"] = float(venta_sem_res['monto'] or 0)
        # Comparativa con semana anterior (simple estimación o 0 por ahora)
        
        # 3. Monto Pagado (Estado=1)
        pagado_res = ventas_annotated.filter(estado=1).aggregate(monto=Sum('monto_usd_v'))
        stats_v3["kpi_pagado"] = float(pagado_res['monto'] or 0)

        # 4. Deuda Total (Cuotas pendientes/vencidas)
        # Necesitamos convertir cuotas a USD tambien si la venta fue en otra moneda?
        # Por simplicidad asumiremos la misma logica de 'ventas_annotated' pero sobre cuotas es complejo sin un annotate previo extenso.
        # Vamos a usar una aproximacion con las cuotas_qs ya anotadas si existen o hacerlo aqui rapido.
        # Reusamos la logica de 'cuotas_qs' definida arriba en lines 60-78?
        # cuotas_qs ya tiene 'monto_usd'.
        deuda_qs = cuotas_qs.filter(estado__in=[2, 3]) # Pendiente(2), Vencida(3)
        deuda_res = deuda_qs.aggregate(total=Sum('monto_usd'))
        stats_v3["kpi_deuda"] = float(deuda_res['total'] or 0)

        # 5. Deuda del Día (Vence hoy)
        deuda_hoy_res = deuda_qs.filter(fecha_vencimiento=today).aggregate(total=Sum('monto_usd'))
        stats_v3["kpi_deuda_hoy"] = float(deuda_hoy_res['total'] or 0)

        ventas_chart_labels = [m["month"] for m in monthly]
        ventas_chart_data = [float(m["total"]) for m in monthly]
        
        if not ventas_chart_data and ventas_resumen.get("monto"):
            ventas_chart_labels = ["Actual"]
            ventas_chart_data = [float(ventas_resumen.get("monto") or 0)]
        
        # GUARDAR EN CACHÉ (Fusionamos con lo viejo)
        cache_payload = {
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
            **stats_v3 # V3 data
        }
        cache.set(cache_key, cache_payload, 300)

        # Para uso inmediato (no cache retrieval above fallback)
        cached_stats = cache_payload # Hack para usar abajo

    # API Carga Asíncrona RESPONSE
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        list_cache_key = f"dash_list_v3_1_{request.user.id}_{is_admin}"
        cached_lists = cache.get(list_cache_key)
        
        if cached_lists:
            return JsonResponse(cached_lists, encoder=DjangoJSONEncoder)

        # Necesitamos recalcular listas detalladas para los graficos interactivos en JS?
        # En la V3, el usuario filtrará y querrá ver los graficos actualizados.
        # Enviar TODO el detalle de ventas (con pais, medio, etc) permitiría filtrar en cliente super rapido.
        # Vamos a expandir "ventas_detalle" para incluir los campos necesarios.
        
        ventas_export = ventas_qs.select_related('moneda', 'usuario', 'pais').annotate(
             tasa_cambio=Coalesce('radio_multiplicador_usado', 'moneda__radioMultiplicador', 1, output_field=DecimalField())
        ).annotate(
            tasa_final=Case(When(tasa_cambio=0, then=Value(1)), default=F('tasa_cambio'), output_field=DecimalField())
        ).annotate(
            monto_usd=ExpressionWrapper(F('monto_total') / F('tasa_final'), output_field=DecimalField(max_digits=12, decimal_places=2))
        ).values(
            "id", "folio_venta", "monto_total", "monto_usd", "fecha_venta", "estado", 
            "usuario__first_name", "usuario__last_name",  # Vendedor
            "pais__nombre", # Pais
            "medio", # Medio
            "origen" # Origen
        )

        # Detalles (Productos) expandidos con Division/Negocio
        detalles_export = DetalleVenta.objects.filter(venta__in=ventas_qs).select_related(
            'venta', 'venta__moneda', 'producto__codigo_categoria', 'producto__codigo_division', 'producto__codigo_negocio'
        ).annotate(
            tasa_cambio=Coalesce('venta__radio_multiplicador_usado', 'venta__moneda__radioMultiplicador', 1, output_field=DecimalField())
        ).annotate(
            tasa_final=Case(When(tasa_cambio=0, then=Value(1)), default=F('tasa_cambio'), output_field=DecimalField())
        ).annotate(
            monto_usd=ExpressionWrapper(F('precio_total') / F('tasa_final'), output_field=DecimalField(max_digits=12, decimal_places=2))
        ).values(
            "venta_id", 
            "producto__codigo_categoria__nombre_categoria",
            "producto__codigo_division__nombre_division",
            "producto__codigo_negocio__nombre_negocio",
            "producto__nombre_producto",
            "monto_usd",
            "cantidad"
        )
        
        cuotas_detalle = list(cuotas_qs.values("venta__folio_venta", "numero_cuota", "monto_total", "estado", "fecha_vencimiento"))

        response_data = {
            "ventas_detalle": list(ventas_export),
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
        "chart_cat_data": json.dumps(cat_data, cls=DjangoJSONEncoder),
        
        # V3 Initial Data
        "chart_div_labels": json.dumps(cached_stats.get("div_labels", []), cls=DjangoJSONEncoder),
        "chart_div_data": json.dumps(cached_stats.get("div_data", []), cls=DjangoJSONEncoder),
        "chart_neg_labels": json.dumps(cached_stats.get("neg_labels", []), cls=DjangoJSONEncoder),
        "chart_neg_data": json.dumps(cached_stats.get("neg_data", []), cls=DjangoJSONEncoder),
        "chart_medio_labels": json.dumps(cached_stats.get("medio_labels", []), cls=DjangoJSONEncoder),
        "chart_medio_data": json.dumps(cached_stats.get("medio_data", []), cls=DjangoJSONEncoder),
        "chart_neg_data": json.dumps(cached_stats.get("neg_data", []), cls=DjangoJSONEncoder),
        "chart_medio_labels": json.dumps(cached_stats.get("medio_labels", []), cls=DjangoJSONEncoder),
        "chart_medio_data": json.dumps(cached_stats.get("medio_data", []), cls=DjangoJSONEncoder),
        "top_pais_list": cached_stats.get("pais_list", []),
        "top_sales_list": cached_stats.get("sales_list", []),
        "top_prod_list": cached_stats.get("prod_list", []),
        
        # KPIs V3
        "kpi_dia_count": cached_stats.get("kpi_dia_count", 0),
        "kpi_dia_monto": cached_stats.get("kpi_dia_monto", 0),
        "kpi_sem_monto": cached_stats.get("kpi_sem_monto", 0),
        "kpi_pagado": cached_stats.get("kpi_pagado", 0),
        "kpi_deuda": cached_stats.get("kpi_deuda", 0),
        "kpi_deuda_hoy": cached_stats.get("kpi_deuda_hoy", 0),

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
