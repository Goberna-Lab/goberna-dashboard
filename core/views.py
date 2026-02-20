# sales/views.py
# =========================================================
# VIEWS — Ventas / Pagos / Tesorería / Facturación
# (Código consolidado y con imports ordenados, sin cambiar tu lógica)
# ✅ Incluye tus modificaciones:
#   - Helpers _pick_cliente_telefono / _pick_cliente_correo
#   - api_facturacion_data devuelve teléfono/correo
#   - Export certificados SAN MARCOS alineado al LISTADO (usa CertificadoCurso)
#   - _filtrar_certificados_sm_common corrige filtros de nombre/apellido (cubre ambos esquemas)
# =========================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, time as dtime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import openpyxl
from openpyxl.drawing.image import Image as ExcelImage  # compat (no insertas imagen ahora)
from openpyxl.styles import Alignment, Font, PatternFill

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import (
    Exists,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from notifications.models import Notification
from products.export_utils import export_as_response
from products.models import Categoria, Division, Negocio, Producto
from users.forms import ClienteForm, CorreoFormSet, TelefonoFormSet
from users.models import Cliente, Correo, Pais, Telefono
from movimientos.models import TbLocal, TbStockLocal, TbUbicacion

from .forms import PagoForm, VentaForm
from .models import (
    CertificadoCurso,
    Cuota,
    DetalleVenta,
    Facturacion,
    LibroEnPack,
    MetodoPago,
    Moneda,
    Pago,
    Venta,
    VoucherPurgeConfig,
)
from sales.voucher_maintenance import next_purge_date, parse_date_str, run_due_actions


# =========================================================
# CONSTANTES
# =========================================================
User = get_user_model()

VENDOR_GROUP_ID = 3
ADMIN_GROUP_ID = 2
ASSISTANT_ADMIN_GROUP_ID = 6
ADMIN_GROUP_IDS = (ADMIN_GROUP_ID, ASSISTANT_ADMIN_GROUP_ID)
TREASURY_GROUP_ID = 5

SALES_GLOBAL_USER_IDS = {7, 8, 35}
SALES_GLOBAL_SCOPE_GROUP_NAMES = ("Scope - Ventas Global",)
PAYPAL_LINK_METODO_ID = 57
HIDDEN_TREASURY_OBSERVATIONS = ("importación histórica", "importacion historica")

VENTA_ESTADO_PAGADO = 1
VENTA_ESTADO_PENDIENTE = 2
VENTA_ESTADO_NO_VALIDADO = 3
VENTA_ESTADO_ANULADO = 4
VENTA_ESTADO_COTIZACION = 5
VENTA_ESTADO_PREVENTA = 6
VENTA_ESTADO_RETIRADO = 7
VENTA_ESTADO_REEMBOLSADO = 8

CUOTA_ESTADO_PAGADO = 1
CUOTA_ESTADO_PENDIENTE = 2
CUOTA_ESTADO_VENCIDA = 3
CUOTA_ESTADO_REINTENTO = 4
CUOTA_ESTADO_RETIRADO = 5
CUOTA_ESTADO_RETIRADO_INCONFORMIDAD = 6
CUOTA_ESTADO_RETIRADO_INSATISFECHO = 7
CUOTA_ESTADO_ANULADO = 8
CUOTA_ESTADO_REEMBOLSO = 9

CUOTA_ESTADOS_RETIRADOS = {5, 6, 7}
CUOTA_ESTADOS_BLOQUEADOS = {8, 9}  # anulado/reembolso


COBRANZA_USER_IDS = {15}

# Certificados (hardcodeados por nombre)
CERTIFICADO_NOMBRES = {"certificado", "certificado san marcos"}


def _normalizar_nombre(nombre: str) -> str:
    return " ".join((nombre or "").strip().lower().split())


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes", "si"}


def _is_cobranza_user(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return int(getattr(user, "id", 0) or 0) in COBRANZA_USER_IDS


def _mensaje_bloqueo_pago_venta(venta: Venta) -> str | None:
    if not venta:
        return "No se pudo identificar la venta de la cuota."

    estado = int(getattr(venta, "estado", 0) or 0)
    if estado == VENTA_ESTADO_COTIZACION:
        return "Primero convierte la cotización en venta para registrar pagos."
    if estado == VENTA_ESTADO_PREVENTA:
        return "La venta está en preventa. Convierte la preventa a venta normal para habilitar pagos."
    if estado == VENTA_ESTADO_RETIRADO:
        return "La venta está retirada por cobranzas y no admite pagos."
    if estado == VENTA_ESTADO_ANULADO:
        return "La venta está anulada y no admite pagos."
    if estado == VENTA_ESTADO_REEMBOLSADO:
        return "La venta está reembolsada y no admite pagos."
    return None


def _has_sales_global_access(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False

    try:
        user_id = int(getattr(user, "id", 0) or 0)
    except (TypeError, ValueError):
        user_id = 0

    # Fallback: algunos equipos usan el id de tb_perfil_usuario al referirse al "id de usuario".
    # Si existe perfil, también lo consideramos para acceso global.
    perfil_id = 0
    try:
        perfil = getattr(user, "perfil", None)
        perfil_id = int(getattr(perfil, "id", 0) or 0)
    except Exception:
        perfil_id = 0

    return bool(
        user_id in SALES_GLOBAL_USER_IDS
        or perfil_id in SALES_GLOBAL_USER_IDS
        or user.is_superuser
        or user.groups.filter(id__in=ADMIN_GROUP_IDS).exists()
        or user.groups.filter(name__iexact="Administrador").exists()
        or user.groups.filter(name__in=SALES_GLOBAL_SCOPE_GROUP_NAMES).exists()
    )


def _is_admin_user(user) -> bool:
    return _has_sales_global_access(user)


def _tipo_certificado_por_nombre(nombre: str) -> Optional[str]:
    """
    Retorna el tipo de certificado si el nombre coincide con los hardcodeados.
    """
    normalizado = _normalizar_nombre(nombre)
    if normalizado == "certificado san marcos":
        return "SAN_MARCOS"
    if normalizado == "certificado":
        return "CERTIFICADO"
    return None


# =========================================================
# HELPERS: STOCK / NOTIFICACIONES
# =========================================================
def _producto_requiere_stock(producto):
    """
    True si el producto requiere stock local.
    - Requiere: categorías 1 y 11
    - No requiere: todas las demás categorías
    """
    # Certificados hardcodeados: no requieren stock aunque sean físicos.
    if _tipo_certificado_por_nombre(getattr(producto, "nombre_producto", "")):
        return False
    cat_id = getattr(producto, "codigo_categoria_id", None)
    if cat_id is None:
        cat_id = getattr(producto, "codigo_categoria", None)
    return cat_id in {1, 11}


def _metodos_pago_para_modal():
    """
    Métodos para el modal de registro de pago.
    Incluye los disponibles y fuerza la presencia de Link PayPal (id=57).
    """
    return (
        MetodoPago.objects.filter(Q(estado=1) | Q(id=PAYPAL_LINK_METODO_ID))
        .select_related("moneda")
        .distinct()
        .order_by("tipo_pago", "id")
    )


def _notify_user_and_vendors(
    usuario,
    *,
    titulo: str,
    mensaje: str,
    tipo: str = "info",
    url: str | None = None,
):
    """Envía la notificación SOLO al usuario específico (vendedor/responsable)."""
    if usuario and getattr(usuario, "is_active", False):
        Notification.objects.create(usuario=usuario, titulo=titulo, mensaje=mensaje, tipo=tipo, url=url)


def _notify_admins(*, titulo: str, mensaje: str, tipo: str = "info", url: str | None = None):
    """Envía la notificación a todos los admins (grupos 2 y 6)."""
    admins = User.objects.filter(groups__id__in=ADMIN_GROUP_IDS, is_active=True).distinct()
    Notification.objects.bulk_create(
        [Notification(usuario=admin, titulo=titulo, mensaje=mensaje, tipo=tipo, url=url) for admin in admins]
    )


def _notify_campus_users(*, titulo: str, mensaje: str, tipo: str = "info", url: str | None = None):
    """Envía la notificación a todos los usuarios activos del grupo Campus."""
    campus_users = User.objects.filter(groups__name="Campus", is_active=True).distinct()
    if not campus_users.exists():
        return
    Notification.objects.bulk_create(
        [Notification(usuario=user, titulo=titulo, mensaje=mensaje, tipo=tipo, url=url) for user in campus_users]
    )


def _get_stock_row(producto_id, ubicacion: TbUbicacion):
    if not ubicacion:
        raise ValidationError("Debe seleccionar una ubicación para gestionar stock.")
    stock, _ = TbStockLocal.objects.select_for_update().get_or_create(
        producto_id=producto_id,
        ubicacion=ubicacion,
        defaults={"stock_actual": 0, "stock_reservado": 0},
    )
    return stock


def _stock_disponible(stock: TbStockLocal) -> int:
    return max((stock.stock_actual or 0) - (stock.stock_reservado or 0), 0)


def _reservar_stock(producto, ubicacion: TbUbicacion, cantidad: int):
    stock = _get_stock_row(producto.codigo_producto, ubicacion)
    disponible = _stock_disponible(stock)
    if disponible < cantidad:
        raise ValidationError(
            f"Stock insuficiente en {ubicacion.nombre_ubicacion} ({ubicacion.local.nombre_local}). "
            f"Disponible: {disponible}"
        )
    # CORRECCIÓN: No debitar stock_actual al reservar, solo aumentar reserva.
    # El débito real ocurre al despachar.
    stock.stock_reservado = stock.stock_reservado + cantidad
    stock.save(update_fields=["stock_reservado", "fecha_actualizacion"])


def _liberar_reserva(producto, ubicacion: TbUbicacion, cantidad: int):
    stock = _get_stock_row(producto.codigo_producto, ubicacion)
    # CORRECCIÓN: Al liberar, solo reducimos la reserva.
    # No sumamos al stock_actual porque nunca se restó de ahí en la reserva.
    stock.stock_reservado = max(stock.stock_reservado - cantidad, 0)
    stock.save(update_fields=["stock_reservado", "fecha_actualizacion"])


def _resolve_ubicacion_para_detalle(venta, prod_data) -> Optional[TbUbicacion]:
    """
    Devuelve la ubicación específica para el detalle. Prioriza el id en prod_data,
    luego la ubicación predeterminada de la venta.
    """
    ubicacion_id = prod_data.get("ubicacion_id") or prod_data.get("ubicacion")
    if ubicacion_id:
        ubicacion_obj = TbUbicacion.objects.select_related("local").filter(pk=ubicacion_id, activa=True).first()
        if not ubicacion_obj:
            raise ValidationError("La ubicación seleccionada no es válida.")
        if venta.local_id and ubicacion_obj.local_id != venta.local_id:
            raise ValidationError("La ubicación debe pertenecer al local de la venta.")
        if (
            venta.pais_id
            and ubicacion_obj.local
            and ubicacion_obj.local.pais_id
            and ubicacion_obj.local.pais_id != venta.pais_id
        ):
            raise ValidationError("La ubicación debe pertenecer al país de la venta.")
        return ubicacion_obj
    return venta.ubicacion


def _build_detalles_payload(detalles):
    payload = []
    for detalle in detalles:
        nombre = getattr(detalle.producto, "nombre_producto", "") if detalle.producto_id else ""
        cantidad = int(detalle.cantidad or 0)
        precio_unitario = detalle.precio_venta if detalle.precio_venta is not None else detalle.precio_regular
        if precio_unitario is None:
            precio_unitario = Decimal("0.00")
        total = detalle.precio_total if detalle.precio_total is not None else (precio_unitario * cantidad)
        payload.append(
            {
                "nombre": nombre,
                "cantidad": cantidad,
                "precio_unitario": float(precio_unitario),
                "total": float(total),
            }
        )
    return payload


def _distribuir_monto_en_cuotas(monto_total: Decimal, cantidad_cuotas: int) -> list[Decimal]:
    """
    Distribuye un monto total en N cuotas con precisión de 2 decimales.
    Reparte centavos sobrantes en las primeras cuotas.
    """
    cantidad = max(int(cantidad_cuotas or 0), 0)
    if cantidad == 0:
        return []

    total_cents = int((Decimal(str(monto_total or 0)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    total_cents = max(total_cents, 0)
    base = total_cents // cantidad
    resto = total_cents - (base * cantidad)

    montos = []
    for idx in range(cantidad):
        cents = base + (1 if idx < resto else 0)
        montos.append((Decimal(cents) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return montos


# =========================================================
# VENTA: CREAR
# =========================================================
def crear_venta(request):
    form = VentaForm(request.POST or None)
    productos_data = request.POST.get("productos_json", "[]")
    cuotas_data = request.POST.get("cuotas_json", "[]")
    save_mode = (request.POST.get("save_mode") or "venta").strip().lower()
    if save_mode not in {"venta", "cotizacion"}:
        save_mode = "venta"
    is_preventa = _is_truthy(request.POST.get("is_preventa"))
    is_preventa_mode = bool(is_preventa and save_mode != "cotizacion")

    # Formularios de cliente
    cliente_form = ClienteForm()
    correo_formset = CorreoFormSet(queryset=Correo.objects.none(), prefix="correo")
    telefono_formset = TelefonoFormSet(queryset=Telefono.objects.none(), prefix="telefono")
    paises = Pais.objects.all()

    # Convertir JSON recibido
    try:
        productos = json.loads(productos_data or "[]")
        cuotas = json.loads(cuotas_data or "[]")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Datos mal formateados"}, status=400)

    if request.method == "POST":
        if not productos:
            return JsonResponse({"success": False, "message": "Debe agregar al menos un producto"}, status=400)

        if not cuotas:
            return JsonResponse({"success": False, "message": "Debe agregar al menos una cuota"}, status=400)

        for c in cuotas:
            if not c.get("fecha_vencimiento"):
                return JsonResponse(
                    {"success": False, "message": "Debe ingresar la fecha de vencimiento en todas las cuotas"}, status=400
                )

        if form.is_valid():
            try:
                with transaction.atomic():
                    usuario_actual = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
                    if not usuario_actual:
                        # Fallback: primer superusuario o staff disponible
                        usuario_actual = User.objects.filter(is_superuser=True).first() or User.objects.filter(is_staff=True).first()

                    if not usuario_actual:
                        return JsonResponse(
                            {"success": False, "message": "No se pudo identificar un usuario para registrar la venta."},
                            status=401,
                        )

                    venta = form.save(commit=False)
                    venta.usuario = usuario_actual
                    if save_mode == "cotizacion":
                        venta.estado = VENTA_ESTADO_COTIZACION
                    elif is_preventa_mode:
                        venta.estado = VENTA_ESTADO_PREVENTA
                    else:
                        venta.estado = VENTA_ESTADO_PENDIENTE

                    # =========================================================
                    # ✅ FIX CONCURRENCIA FOLIO: reintento si choca UNIQUE(folio_venta)
                    # - Forzamos folio vacío para que el model.save() lo regenere
                    # - Si hay choque por concurrencia, reintentamos unas veces
                    # =========================================================
                    for _ in range(10):
                        try:
                            venta.folio_venta = ""  # fuerza regeneración en Venta.save()
                            venta.save()
                            break
                        except IntegrityError:
                            time.sleep(0.05)
                    else:
                        return JsonResponse(
                            {"success": False, "message": "No se pudo generar un folio único (concurrencia). Intenta nuevamente."},
                            status=409,
                        )

                    for prod in productos:
                        producto = Producto.objects.select_related("codigo_categoria").filter(pk=prod["producto_id"]).first()
                        if not producto:
                            continue

                        tipo_certificado = _tipo_certificado_por_nombre(producto.nombre_producto)
                        curso_obj = None
                        if tipo_certificado:
                            curso_id = prod.get("certificado_curso_id")
                            if not curso_id:
                                return JsonResponse(
                                    {"success": False, "message": f"Debes seleccionar el curso para el certificado '{producto.nombre_producto}'."},
                                    status=400,
                                )
                            curso_obj = Producto.objects.filter(pk=curso_id).first()
                            if not curso_obj or curso_obj.codigo_negocio_id != 2 or curso_obj.estado != 1:
                                return JsonResponse(
                                    {"success": False, "message": "El curso seleccionado no pertenece al negocio Escuela o no está disponible."},
                                    status=400,
                                )

                        if producto.codigo_categoria == 4 and not prod.get("libros_en_pack"):
                            return JsonResponse(
                                {"success": False, "message": f"El producto '{producto.nombre_producto}' es un pack y debe tener libros."},
                                status=400,
                            )

                        # En preventa no se exige local/ubicación ni validación de stock.
                        det_ubicacion = None if is_preventa_mode else _resolve_ubicacion_para_detalle(venta, prod)

                        # Si el producto requiere stock, debe tener ubicación asignada
                        if not is_preventa_mode and _producto_requiere_stock(producto) and not det_ubicacion:
                            raise ValidationError("Debes elegir una ubicación para los productos que requieren stock.")

                        # Reservar stock solo si se requiere y hay ubicación definida
                        if not is_preventa_mode and _producto_requiere_stock(producto) and det_ubicacion:
                            _reservar_stock(producto, det_ubicacion, int(prod["cantidad"]))

                        detalle = DetalleVenta.objects.create(
                            venta=venta,
                            producto=producto,
                            cantidad=prod["cantidad"],
                            precio_regular=prod.get("precio_regular", 0),
                            precio_venta=prod.get("precio_venta", 0),
                            precio_total=prod.get("precio_total", 0),
                            ubicacion=det_ubicacion,
                        )

                        if tipo_certificado and curso_obj:
                            CertificadoCurso.objects.create(
                                detalle_venta=detalle,
                                curso=curso_obj,
                                tipo_certificado=tipo_certificado,
                                cantidad=int(prod.get("cantidad") or 1),
                            )

                        if det_ubicacion and not venta.ubicacion_id:
                            venta.ubicacion = det_ubicacion
                            venta.save(update_fields=["ubicacion"])

                        # PACKS
                        if prod.get("es_pack") and prod.get("libros_en_pack"):
                            libros = prod["libros_en_pack"]
                            cantidad_pack = int(prod.get("cantidad", 1))
                            precio_total_pack = float(prod.get("precio_venta", 0)) * cantidad_pack

                            ids_unicos = list(set([l["id"] for l in libros]))

                            if len(ids_unicos) == 1:
                                libro = libros[0]
                                cantidad_final = int(libro["cantidad"]) * cantidad_pack
                                LibroEnPack.objects.create(
                                    detalle_venta_pack=detalle,
                                    libro_id=libro["id"],
                                    cantidad=cantidad_final,
                                    precio_unitario=precio_total_pack,  # se lleva todo el valor
                                )
                            else:
                                # Distribución proporcional según la cantidad de cada libro
                                total_unidades = sum(int(l["cantidad"]) for l in libros)
                                for libro in libros:
                                    cantidad_por_pack = int(libro["cantidad"])
                                    cantidad_final = cantidad_por_pack * cantidad_pack
                                    proporcion = cantidad_por_pack / total_unidades
                                    precio_unitario = round(proporcion * float(prod.get("precio_venta", 0)), 2)

                                    LibroEnPack.objects.create(
                                        detalle_venta_pack=detalle,
                                        libro_id=libro["id"],
                                        cantidad=cantidad_final,
                                        precio_unitario=precio_unitario,
                                    )

                            # Reservar stock de los libros del pack si hay ubicación
                            if det_ubicacion and not is_preventa_mode:
                                for l in libros:
                                    libro_obj = Producto.objects.filter(pk=l["id"]).first()
                                    if libro_obj and _producto_requiere_stock(libro_obj):
                                        _reservar_stock(libro_obj, det_ubicacion, int(l["cantidad"]) * cantidad_pack)

                    # El monto de cada cuota es inmutable en creación:
                    # siempre se recalcula en backend según el monto total de la venta.
                    montos_cuotas = _distribuir_monto_en_cuotas(Decimal(str(venta.monto_total or 0)), len(cuotas))
                    for idx, c in enumerate(cuotas):
                        fecha_registro = parse_date(str(c.get("fecha_registro") or "")) or timezone.localdate()
                        fecha_vencimiento = parse_date(str(c.get("fecha_vencimiento") or ""))
                        if not fecha_vencimiento:
                            return JsonResponse(
                                {"success": False, "message": "Debe ingresar la fecha de vencimiento en todas las cuotas"},
                                status=400,
                            )

                        Cuota.objects.create(
                            venta=venta,
                            # Evita manipulación de correlativo desde cliente.
                            numero_cuota=idx + 1,
                            monto_total=montos_cuotas[idx] if idx < len(montos_cuotas) else Decimal("0.00"),
                            fecha_registro=fecha_registro,
                            fecha_vencimiento=fecha_vencimiento,
                            estado=2,
                        )

                    # Notification trigger
                    notif_user = request.user if getattr(request.user, "is_authenticated", False) else None
                    if not notif_user:
                        notif_user = User.objects.filter(is_superuser=True).first() or User.objects.filter(is_staff=True).first()

                    if notif_user:
                        notif_titulo = "Cotización registrada" if save_mode == "cotizacion" else "Venta registrada"
                        notif_texto = (
                            f"Nueva cotización {venta.folio_venta} registrada para "
                            if save_mode == "cotizacion"
                            else f"Nueva venta {venta.folio_venta} registrada para "
                        )
                        _notify_user_and_vendors(
                            notif_user,
                            titulo=notif_titulo,
                            mensaje=(
                                notif_texto
                                + " "
                                f"{venta.cliente.nombre} {venta.cliente.apellido}. "
                                f"Monto: {venta.moneda.nombre} {venta.monto_total}"
                            ),
                            tipo="success",
                            url=reverse("detalle_venta", args=[venta.id]),
                        )

                return JsonResponse({"success": True, "redirect_url": reverse("lista_ventas")})

            except ValidationError as ve:
                return JsonResponse({"success": False, "message": str(ve)}, status=400)
            except Exception as e:
                return JsonResponse({"success": False, "message": f"Error interno: {str(e)}"}, status=500)

        return JsonResponse({"success": False, "message": "Formulario inválido"}, status=400)

    # GET
    return render(
        request,
        "sales/crear_venta.html",
        {
            "form": form,
            "productos_json": productos_data,
            "cuotas_json": cuotas_data,
            "timestamp": now().timestamp(),
            "negocios": Negocio.objects.all(),
            "categorias": Categoria.objects.all(),
            "divisiones": Division.objects.all(),
            "cliente_form": cliente_form,
            "correo_formset": correo_formset,
            "telefono_formset": telefono_formset,
            "paises": paises,
        },
    )


@require_GET
def api_productos_search(request):
    """
    Autocomplete: retorna productos por nombre (dropdown con buscador).
    GET:
      - q: texto
      - limit: default 30 (max 100)
    Response:
      { "results": [ {"id": 1, "text": "Producto X"}, ... ] }
    """
    q = (request.GET.get("q") or "").strip()
    try:
        limit = int(request.GET.get("limit", 30))
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 100))

    qs = Producto.objects.all()

    # si tu Producto tiene "estado", filtra activos
    if hasattr(Producto, "estado"):
        qs = qs.filter(estado=1)

    if q:
        qs = qs.filter(Q(nombre_producto__icontains=q))

    qs = qs.order_by("nombre_producto")[:limit]
    data = [{"id": p.codigo_producto, "text": p.nombre_producto} for p in qs]
    return JsonResponse({"results": data})


@require_GET
def api_productos_by_ids(request):
    """
    Retorna productos por IDs para pre-cargar selects multiselección.
    GET:
      - ids: "1,2,3"
    Response:
      { "results": [ {"id": 1, "text": "Producto X"}, ... ] }
    """
    ids_raw = (request.GET.get("ids") or "").strip()
    if not ids_raw:
        return JsonResponse({"results": []})

    ids = []
    vistos = set()
    for part in ids_raw.split(","):
        part = (part or "").strip()
        if not part.isdigit():
            continue
        pid = int(part)
        if pid <= 0 or pid in vistos:
            continue
        vistos.add(pid)
        ids.append(pid)

    if not ids:
        return JsonResponse({"results": []})

    productos_map = {
        p.codigo_producto: p
        for p in Producto.objects.filter(codigo_producto__in=ids).only("codigo_producto", "nombre_producto")
    }
    data = [
        {"id": pid, "text": productos_map[pid].nombre_producto}
        for pid in ids
        if pid in productos_map
    ]
    return JsonResponse({"results": data})


# ---------------------------------------------------------
# Helpers fechas / estado venta
# ---------------------------------------------------------
def _today_range():
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    start = timezone.make_aware(datetime.combine(today, dtime.min), tz)
    end = timezone.make_aware(datetime.combine(today, dtime.max), tz)
    return start, end


def _sync_estado_venta(venta: Venta) -> int:
    """
    Sincroniza el estado de la venta según el estado real de sus cuotas.
    - Retirado (7): existe al menos una cuota retirada.
    - Pagado (1): no existen cuotas abiertas (pagadas/reintento/retiradas/bloqueadas no bloquean).
    - Pendiente (2): existe al menos una cuota no pagada.
    - No aplica para Anulado, Reembolsado, Cotización, Preventa y Retirado.
    """
    if venta.estado in (
        VENTA_ESTADO_ANULADO,
        VENTA_ESTADO_REEMBOLSADO,
        VENTA_ESTADO_COTIZACION,
        VENTA_ESTADO_PREVENTA,
        VENTA_ESTADO_RETIRADO,
    ):
        return venta.estado

    if venta.cuotas.filter(estado__in=CUOTA_ESTADOS_RETIRADOS).exists():
        nuevo_estado = VENTA_ESTADO_RETIRADO
    else:
        estados_cerrados = (
            {CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO}
            | set(CUOTA_ESTADOS_RETIRADOS)
            | set(CUOTA_ESTADOS_BLOQUEADOS)
        )
        nuevo_estado = (
            VENTA_ESTADO_PENDIENTE
            if venta.cuotas.exclude(estado__in=estados_cerrados).exists()
            else VENTA_ESTADO_PAGADO
        )
    if venta.estado != nuevo_estado:
        venta.estado = nuevo_estado
        venta.save(update_fields=["estado"])
    return nuevo_estado


# ---------------------------------------------------------
# LISTA VENTAS (con export y filtro producto)
# ---------------------------------------------------------
@login_required
def lista_ventas(request):
    detalles_qs = DetalleVenta.objects.select_related("producto")

    ventas = (
        Venta.objects.select_related("cliente", "moneda", "usuario", "pais")
        .prefetch_related(Prefetch("detalles", queryset=detalles_qs), "cuotas__pagos")
        .order_by("-fecha_venta")
    )

    # Admin (grupos 2/6), superuser o IDs globales ven todo
    is_admin = _has_sales_global_access(request.user)

    # Vendedores: solo sus ventas
    if not is_admin:
        ventas = ventas.filter(usuario=request.user)

    # ✅ TAB + "No validado" real (según Pago.estado 3/4 en cuotas aún abiertas)
    cuota_estados_cerrados_novalidado = (
        {CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO}
        | set(CUOTA_ESTADOS_RETIRADOS)
        | set(CUOTA_ESTADOS_BLOQUEADOS)
    )
    pagos_malos_sq = (
        Pago.objects.filter(
            cuota__venta_id=OuterRef("pk"),
            estado__in=[3, 4],  # 3=No Validado, 4=Denegado
        )
        .exclude(cuota__estado__in=cuota_estados_cerrados_novalidado)
    )
    ventas = ventas.annotate(has_pagos_malos=Exists(pagos_malos_sq))

    default_tab = "todo" if is_admin else "hoy"
    tab = (request.GET.get("tab") or default_tab).strip().lower()
    valid_tabs = {
        "pagadas",
        "pendientes",
        "novalidado",
        "cotizaciones",
        "preventas",
        "hoy",
        "todo",
        "anuladas",
        "retiradas",
        "reembolsadas",
    }
    if tab not in valid_tabs:
        tab = "pagadas"

    ventas_activo = ventas.exclude(
        estado__in=[VENTA_ESTADO_ANULADO, VENTA_ESTADO_REEMBOLSADO]
    )  # no anuladas/reembolsadas
    today_start, today_end = _today_range()

    counts = {
        "pagadas": ventas_activo.filter(estado=VENTA_ESTADO_PAGADO).count(),
        "pendientes": ventas_activo.filter(estado=VENTA_ESTADO_PENDIENTE).count(),
        "novalidado": (
            ventas_activo
            .filter(Q(estado=VENTA_ESTADO_NO_VALIDADO) | Q(has_pagos_malos=True))
            .exclude(estado=VENTA_ESTADO_PAGADO)
            .count()
        ),
        "cotizaciones": ventas_activo.filter(estado=VENTA_ESTADO_COTIZACION).count(),
        "preventas": ventas_activo.filter(estado=VENTA_ESTADO_PREVENTA).count(),
        "hoy": ventas_activo.filter(fecha_venta__range=[today_start, today_end]).count(),
        "todo": ventas_activo.count(),
        "anuladas": ventas.filter(estado=VENTA_ESTADO_ANULADO).count(),
        "reembolsadas": ventas.filter(estado=VENTA_ESTADO_REEMBOLSADO).count(),
        "retiradas": ventas.filter(estado=VENTA_ESTADO_RETIRADO).count(),
    }

    # Aplicar tab
    if tab == "pagadas":
        ventas = ventas_activo.filter(estado=VENTA_ESTADO_PAGADO)
    elif tab == "pendientes":
        ventas = ventas_activo.filter(estado=VENTA_ESTADO_PENDIENTE)
    elif tab == "novalidado":
        ventas = (
            ventas_activo
            .filter(Q(estado=VENTA_ESTADO_NO_VALIDADO) | Q(has_pagos_malos=True))
            .exclude(estado=VENTA_ESTADO_PAGADO)
        )
    elif tab == "cotizaciones":
        ventas = ventas_activo.filter(estado=VENTA_ESTADO_COTIZACION)
    elif tab == "preventas":
        ventas = ventas_activo.filter(estado=VENTA_ESTADO_PREVENTA)
    elif tab == "hoy":
        ventas = ventas_activo.filter(fecha_venta__range=[today_start, today_end])
    elif tab == "anuladas":
        ventas = ventas.filter(estado=VENTA_ESTADO_ANULADO)
    elif tab == "reembolsadas":
        ventas = ventas.filter(estado=VENTA_ESTADO_REEMBOLSADO)
    elif tab == "retiradas":
        ventas = ventas.filter(estado=VENTA_ESTADO_RETIRADO)
    else:  # "todo"
        ventas = ventas_activo

    usuarios = (
        User.objects.filter(id__in=ventas.values_list("usuario_id", flat=True))
        .distinct()
        .order_by("username")
    )

    # =========================================================
    # FILTROS
    # =========================================================
    folio = (request.GET.get("folio") or "").strip()
    cliente = (request.GET.get("cliente") or "").strip()
    usuario = (request.GET.get("usuario") or "").strip()
    estado = (request.GET.get("estado") or "").strip()
    cuota_estado = (request.GET.get("cuota_estado") or "").strip()
    fecha_inicio_str = (request.GET.get("fecha_inicio") or "").strip()
    fecha_fin_str = (request.GET.get("fecha_fin") or "").strip()

    # ✅ NUEVO: filtro producto (id) múltiple
    # Soporta:
    #   producto_id=12
    #   producto_id=12,15,18
    producto_id_raw = (request.GET.get("producto_id") or "").strip()
    selected_producto_ids = []
    if producto_id_raw:
        seen_producto_ids = set()
        for part in producto_id_raw.split(","):
            part = (part or "").strip()
            if not part.isdigit():
                continue
            pid = int(part)
            if pid <= 0 or pid in seen_producto_ids:
                continue
            seen_producto_ids.add(pid)
            selected_producto_ids.append(pid)
    producto_id_raw = ",".join(str(pid) for pid in selected_producto_ids)

    page = request.GET.get("page", 1)

    try:
        per_page = int(request.GET.get("per_page", 50))
    except (TypeError, ValueError):
        per_page = 50
    per_page = max(1, min(per_page, 200))

    fecha_inicio = None
    fecha_fin = None
    try:
        if fecha_inicio_str:
            fecha_inicio = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
        if fecha_fin_str:
            fecha_fin = datetime.strptime(fecha_fin_str, "%Y-%m-%d").date()
    except ValueError:
        pass

    if folio:
        ventas = ventas.filter(folio_venta__icontains=folio)

    if cliente:
        ventas = ventas.filter(
            Q(cliente__nombre__icontains=cliente) | Q(cliente__apellido__icontains=cliente)
        )

    if usuario and usuario.isdigit():
        ventas = ventas.filter(usuario_id=int(usuario))
    elif usuario:
        ventas = ventas.filter(
            Q(usuario__username__icontains=usuario)
            | Q(usuario__first_name__icontains=usuario)
            | Q(usuario__last_name__icontains=usuario)
        )

    if estado and estado.isdigit():
        ventas = ventas.filter(estado=int(estado))

    if cuota_estado and cuota_estado.isdigit():
        ventas = ventas.filter(cuotas__estado=int(cuota_estado)).distinct()

    if fecha_inicio and fecha_fin:
        fecha_fin_dt = datetime.combine(fecha_fin, datetime.max.time())
        ventas = ventas.filter(fecha_venta__range=[fecha_inicio, fecha_fin_dt])
    elif fecha_inicio:
        ventas = ventas.filter(fecha_venta__date__gte=fecha_inicio)
    elif fecha_fin:
        fecha_fin_dt = datetime.combine(fecha_fin, datetime.max.time())
        ventas = ventas.filter(fecha_venta__lte=fecha_fin_dt)

    # ✅ FILTRO PRODUCTO (por detalles)
    if selected_producto_ids:
        ventas = ventas.filter(detalles__producto_id__in=selected_producto_ids).distinct()

    productos_filtro_selected = []
    if selected_producto_ids:
        productos_map = {
            p.codigo_producto: p
            for p in Producto.objects.filter(codigo_producto__in=selected_producto_ids).only(
                "codigo_producto",
                "nombre_producto",
            )
        }
        productos_filtro_selected = [
            productos_map[pid]
            for pid in selected_producto_ids
            if pid in productos_map
        ]

    # =========================================================
    # ✅ EXPORT (MISMO FILTRO QUE LA LISTA)
    # GET: ?export=csv o ?export=xlsx
    # =========================================================
    export_fmt = (request.GET.get("export") or "").strip().lower()
    if export_fmt in ("csv", "xlsx"):

        def _fmt_fecha(dt):
            if not dt:
                return ""
            return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M")

        # Prefetch explícito para export y evitar consultas por fila.
        export_ventas = ventas.prefetch_related(
            Prefetch(
                "cliente__correos",
                queryset=Correo.objects.only("id", "cliente_id", "tipo_correo", "nombre_correo").order_by("id"),
                to_attr="correos_prefetch",
            ),
            Prefetch(
                "cliente__telefonos",
                queryset=Telefono.objects.only("id", "cliente_id", "tipo_telefono", "prefijo", "numero").order_by("id"),
                to_attr="telefonos_prefetch",
            ),
        )

        def _pick_correo_prefetch(cliente_obj):
            if not cliente_obj:
                return ""
            correos = list(getattr(cliente_obj, "correos_prefetch", []) or [])
            if not correos:
                return ""

            personal = next(
                (
                    c
                    for c in correos
                    if (getattr(c, "tipo_correo", "") or "").strip().lower() == "personal"
                    and (getattr(c, "nombre_correo", "") or "").strip()
                ),
                None,
            )
            if personal:
                return personal.nombre_correo.strip()

            corporativo = next(
                (
                    c
                    for c in correos
                    if (getattr(c, "tipo_correo", "") or "").strip().lower() == "corporativo"
                    and (getattr(c, "nombre_correo", "") or "").strip()
                ),
                None,
            )
            if corporativo:
                return corporativo.nombre_correo.strip()

            first_valid = next(
                (
                    c
                    for c in correos
                    if (getattr(c, "nombre_correo", "") or "").strip()
                ),
                None,
            )
            return first_valid.nombre_correo.strip() if first_valid else ""

        def _pick_telefono_prefetch(cliente_obj):
            if not cliente_obj:
                return ""
            telefonos = list(getattr(cliente_obj, "telefonos_prefetch", []) or [])
            if not telefonos:
                return ""

            def _fmt_tel(tel):
                pref = (getattr(tel, "prefijo", "") or "").strip()
                num = (getattr(tel, "numero", "") or "").strip()
                return f"{pref} {num}".strip()

            personal = next(
                (
                    t
                    for t in telefonos
                    if (getattr(t, "tipo_telefono", "") or "").strip().lower() == "personal"
                    and _fmt_tel(t)
                ),
                None,
            )
            if personal:
                return _fmt_tel(personal)

            first_valid = next((t for t in telefonos if _fmt_tel(t)), None)
            return _fmt_tel(first_valid) if first_valid else ""

        def _saldo_restante_venta(venta_obj):
            monto_total = Decimal(str(getattr(venta_obj, "monto_total", 0) or 0))
            monto_pagado_confirmado = Decimal("0.00")

            # Se considera pagado solo lo completado por Tesoreria (estado=2).
            for cuota in venta_obj.cuotas.all():
                for pago in cuota.pagos.all():
                    if int(getattr(pago, "estado", 0) or 0) == 2:
                        monto_pagado_confirmado += Decimal(str(getattr(pago, "monto_pagado", 0) or 0))

            saldo = (monto_total - monto_pagado_confirmado).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return saldo if saldo > 0 else Decimal("0.00")

        rows = []

        # ⚠️ Importante: iterar ventas filtradas (sin paginar)
        for v in export_ventas:
            # lista de productos de la venta
            dets = list(v.detalles.all())  # ya está prefetched arriba
            productos_txt = ", ".join(
                [d.producto.nombre_producto for d in dets if d.producto_id and d.producto]
            )
            correo_cliente = _pick_correo_prefetch(v.cliente) if v.cliente_id else ""
            telefono_cliente = _pick_telefono_prefetch(v.cliente) if v.cliente_id else ""
            saldo_restante = _saldo_restante_venta(v)

            rows.append(
                {
                    "Folio": v.folio_venta,
                    "Cliente": f"{v.cliente.nombre} {v.cliente.apellido}".strip() if v.cliente_id else "",
                    "Correo Cliente": correo_cliente,
                    "Teléfono Cliente": telefono_cliente,
                    "País": v.pais.nombre if getattr(v, "pais_id", None) and v.pais else "",
                    "Moneda": v.moneda.nombre if getattr(v, "moneda_id", None) and v.moneda else "",
                    "Monto Total": float(v.monto_total or 0),
                    "Saldo Restante": float(saldo_restante),
                    "Estado": v.get_estado_display() if hasattr(v, "get_estado_display") else v.estado,
                    "Usuario": v.usuario.username if getattr(v, "usuario_id", None) and v.usuario else "",
                    "Fecha Venta": _fmt_fecha(v.fecha_venta),
                    "Productos": productos_txt,
                }
            )

        if not rows:
            rows.append(
                {
                    "Folio": "",
                    "Cliente": "",
                    "Correo Cliente": "",
                    "Teléfono Cliente": "",
                    "País": "",
                    "Moneda": "",
                    "Monto Total": 0,
                    "Saldo Restante": 0,
                    "Estado": "",
                    "Usuario": "",
                    "Fecha Venta": "",
                    "Productos": "",
                }
            )

        return export_as_response(rows, filename="lista_ventas", fmt=export_fmt)

    # =========================================================
    # PAGINACIÓN
    # =========================================================
    paginator = Paginator(ventas, per_page)
    page_obj = paginator.get_page(page)

    query_params = request.GET.copy()
    query_params.pop("page", None)
    querystring = query_params.urlencode()

    # =========================================================
    # LÓGICA "PAGO DISPONIBLE"
    # =========================================================
    ventas_page = list(page_obj.object_list)
    for venta in ventas_page:
        cuotas = list(venta.cuotas.all())
        total_cuotas = len(cuotas)
        primera_pendiente = next(
            (
                c for c in cuotas
                if c.estado not in (
                    CUOTA_ESTADO_PAGADO,
                    CUOTA_ESTADO_REINTENTO,
                    *CUOTA_ESTADOS_RETIRADOS,
                    CUOTA_ESTADO_ANULADO,
                    CUOTA_ESTADO_REEMBOLSO,
                )
            ),
            None,
        )

        venta.detalles_json = json.dumps(_build_detalles_payload(venta.detalles.all()))

        venta.pago_disponible = False
        venta.pago_reintento = False
        venta.pago_motivo = "Sin cuotas pendientes"
        venta.pago_cuota_id = None
        venta.pago_cuota_monto = None
        venta.pago_cuota_label = ""

        bloqueo_pago_msg = _mensaje_bloqueo_pago_venta(venta)
        if bloqueo_pago_msg:
            venta.pago_motivo = bloqueo_pago_msg
            continue

        if primera_pendiente:
            pagos = list(primera_pendiente.pagos.all())
            tiene_pago_pendiente = any(p.estado == 1 for p in pagos)
            tiene_pago_denegado = any(p.estado in (3, 4) for p in pagos)

            venta.pago_cuota_id = primera_pendiente.id
            venta.pago_cuota_monto = primera_pendiente.monto_total
            venta.pago_reintento = tiene_pago_denegado

            if total_cuotas == 1:
                venta.pago_cuota_label = "Unica cuota"
            else:
                venta.pago_cuota_label = f"Cuota {primera_pendiente.numero_cuota} de {total_cuotas}"

            if primera_pendiente.estado != CUOTA_ESTADO_PENDIENTE:
                venta.pago_motivo = "Cuota vencida o no disponible"
            elif tiene_pago_pendiente:
                venta.pago_motivo = "Pago en verificación"
            else:
                venta.pago_disponible = True
                venta.pago_motivo = ""

    return render(
        request,
        "sales/lista_ventas.html",
        {
            "ventas": ventas_page,
            "paginator": paginator,
            "page_obj": page_obj,
            "per_page": per_page,
            "querystring": querystring,
            "folio": folio,
            "cliente": cliente or "",
            "usuario": usuario,
            "usuarios": usuarios,
            "estado": estado or "",
            "cuota_estado": cuota_estado or "",
            "fecha_inicio": fecha_inicio_str or "",
            "fecha_fin": fecha_fin_str or "",

            # ✅ Para repintar tu UI del filtro producto
            "producto_id": producto_id_raw,
            "productos_filtro_selected": productos_filtro_selected,

            "monedas": Moneda.objects.all(),
            "metodos": _metodos_pago_para_modal(),
            "tab": tab,
            "counts": counts,
            "is_cobranza_user": _is_cobranza_user(request.user),
        },
    )


@login_required
def lista_ventas_retiradas_cobranza(request):
    """
    Vista operativa para Cobranzas (user id 15):
    muestra únicamente ventas en estado Retirado.
    """
    if not _is_cobranza_user(request.user):
        return redirect("lista_ventas")

    cuotas_prefetch = Prefetch("cuotas", queryset=Cuota.objects.order_by("numero_cuota"))
    ventas = (
        Venta.objects.select_related("cliente", "moneda", "usuario", "pais")
        .prefetch_related(cuotas_prefetch)
        .filter(estado=VENTA_ESTADO_RETIRADO)
        .order_by("-fecha_venta")
    )

    folio = (request.GET.get("folio") or "").strip()
    cliente = (request.GET.get("cliente") or "").strip()
    fecha_inicio_str = (request.GET.get("fecha_inicio") or "").strip()
    fecha_fin_str = (request.GET.get("fecha_fin") or "").strip()
    page = request.GET.get("page", 1)

    try:
        per_page = int(request.GET.get("per_page", 50))
    except (TypeError, ValueError):
        per_page = 50
    per_page = max(1, min(per_page, 200))

    fecha_inicio = None
    fecha_fin = None
    try:
        if fecha_inicio_str:
            fecha_inicio = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
        if fecha_fin_str:
            fecha_fin = datetime.strptime(fecha_fin_str, "%Y-%m-%d").date()
    except ValueError:
        pass

    if folio:
        ventas = ventas.filter(folio_venta__icontains=folio)
    if cliente:
        ventas = ventas.filter(
            Q(cliente__nombre__icontains=cliente) | Q(cliente__apellido__icontains=cliente)
        )
    if fecha_inicio and fecha_fin:
        fecha_fin_dt = datetime.combine(fecha_fin, datetime.max.time())
        ventas = ventas.filter(fecha_venta__range=[fecha_inicio, fecha_fin_dt])
    elif fecha_inicio:
        ventas = ventas.filter(fecha_venta__date__gte=fecha_inicio)
    elif fecha_fin:
        fecha_fin_dt = datetime.combine(fecha_fin, datetime.max.time())
        ventas = ventas.filter(fecha_venta__lte=fecha_fin_dt)

    paginator = Paginator(ventas, per_page)
    page_obj = paginator.get_page(page)
    ventas_page = list(page_obj.object_list)

    for venta in ventas_page:
        cuotas_retiradas = [c for c in venta.cuotas.all() if c.estado in CUOTA_ESTADOS_RETIRADOS]
        venta.cuotas_retiradas = cuotas_retiradas
        venta.cuotas_retiradas_txt = ", ".join(
            [f"Cuota {c.numero_cuota} ({c.get_estado_display()})" for c in cuotas_retiradas]
        ) or "—"
        venta.observacion_cobranza = next(
            (
                (c.observacion_cobranza or "").strip()
                for c in reversed(cuotas_retiradas)
                if (c.observacion_cobranza or "").strip()
            ),
            "",
        )

    query_params = request.GET.copy()
    query_params.pop("page", None)
    querystring = query_params.urlencode()

    return render(
        request,
        "sales/lista_ventas_retiradas_cobranza.html",
        {
            "ventas": ventas_page,
            "paginator": paginator,
            "page_obj": page_obj,
            "per_page": per_page,
            "querystring": querystring,
            "folio": folio,
            "cliente": cliente,
            "fecha_inicio": fecha_inicio_str,
            "fecha_fin": fecha_fin_str,
        },
    )


# =========================================================
# APIs: moneda / precio
# =========================================================
def api_moneda_multiplicador(request, moneda_id):
    try:
        moneda = Moneda.objects.get(pk=moneda_id)
        return JsonResponse({"nombre": moneda.nombre, "multiplicador": float(moneda.radioMultiplicador or 1)})
    except Moneda.DoesNotExist:
        return JsonResponse({"error": "Moneda no encontrada"}, status=404)


def api_precio_producto(request, producto_id):
    try:
        producto = Producto.objects.get(pk=producto_id)
        return JsonResponse({"precio_usd": float(producto.precio_normal)})
    except Producto.DoesNotExist:
        return JsonResponse({"error": "Producto no encontrado"}, status=404)


# =========================================================
# DETALLE VENTA
# =========================================================
def detalle_venta(request, pk):
    venta = get_object_or_404(Venta, pk=pk)

    detalles = venta.detalles.select_related("producto", "certificado_curso__curso").prefetch_related(
        Prefetch("libros_en_pack", queryset=LibroEnPack.objects.select_related("libro"))
    )
    detalles = list(detalles)
    for d in detalles:
        try:
            d.certificado_curso_safe = d.certificado_curso
        except CertificadoCurso.DoesNotExist:
            d.certificado_curso_safe = None

    cuotas = list(venta.cuotas.prefetch_related("pagos").order_by("numero_cuota"))

    # Identificar cuotas con pagos en proceso/denegados (prefetch)
    for c in cuotas:
        pagos = list(c.pagos.all())
        if c.estado == CUOTA_ESTADO_PAGADO:
            c.tiene_pago_pendiente = False
            c.tiene_pago_denegado = False
            c.ultimo_pago_denegado = None
            continue
        c.tiene_pago_pendiente = any(p.estado == 1 for p in pagos)
        c.tiene_pago_denegado = any(p.estado in (3, 4) for p in pagos)
        c.ultimo_pago_denegado = next((p for p in pagos if p.estado in (3, 4)), None)

    primera_pendiente = next(
        (
            c for c in cuotas
            if c.estado not in (
                CUOTA_ESTADO_PAGADO,
                CUOTA_ESTADO_REINTENTO,
                *CUOTA_ESTADOS_RETIRADOS,
                CUOTA_ESTADO_ANULADO,
                CUOTA_ESTADO_REEMBOLSO,
            )
        ),
        None,
    )

    primera_pendiente_numero = primera_pendiente.numero_cuota if primera_pendiente else None
    for c in cuotas:
        c.es_primera_pendiente = bool(primera_pendiente and c.id == primera_pendiente.id)

    detalles_json = json.dumps(_build_detalles_payload(detalles))

    return render(
        request,
        "sales/ver_venta.html",
        {
            "venta": venta,
            "detalles": detalles,
            "detalles_json": detalles_json,
            "cuotas": cuotas,
            "cuota_estados_retirados": list(CUOTA_ESTADOS_RETIRADOS),
            "primera_pendiente_numero": primera_pendiente_numero,
            "monedas": Moneda.objects.all(),
            "metodos": _metodos_pago_para_modal(),
        },
    )


@login_required
@require_GET
def api_cobranza_cuotas_pendientes(request, venta_id):
    if not _is_cobranza_user(request.user):
        return JsonResponse({"success": False, "message": "No autorizado."}, status=403)

    venta = get_object_or_404(Venta.objects.select_related("cliente"), pk=venta_id)
    if venta.estado == VENTA_ESTADO_PAGADO:
        return JsonResponse(
            {"success": False, "message": "La venta ya está pagada y no se puede retirar."},
            status=400,
        )

    cuotas = (
        venta.cuotas.filter(estado__in=[CUOTA_ESTADO_PENDIENTE, CUOTA_ESTADO_VENCIDA])
        .order_by("numero_cuota")
    )
    cuotas_data = [
        {
            "id": c.id,
            "numero": c.numero_cuota,
            "monto_total": float(c.monto_total or 0),
            "fecha_vencimiento": c.fecha_vencimiento.strftime("%Y-%m-%d") if c.fecha_vencimiento else "",
            "estado": c.estado,
            "estado_label": c.get_estado_display(),
        }
        for c in cuotas
    ]
    return JsonResponse(
        {
            "success": True,
            "venta_id": venta.id,
            "folio_venta": venta.folio_venta,
            "cliente": (
                f"{getattr(getattr(venta, 'cliente', None), 'nombre', '')} "
                f"{getattr(getattr(venta, 'cliente', None), 'apellido', '')}"
            ).strip()
            or "Sin cliente",
            "cuotas": cuotas_data,
        }
    )


@login_required
@require_POST
@transaction.atomic
def marcar_cuota_retirada(request, cuota_id):
    if not _is_cobranza_user(request.user):
        return JsonResponse({"success": False, "message": "No autorizado."}, status=403)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = request.POST or {}

    observacion = (body.get("observacion") or "").strip()
    if len(observacion) < 5:
        return JsonResponse(
            {"success": False, "message": "Debes ingresar una observación mínima de 5 caracteres."},
            status=400,
        )

    # Tipo de retiro obligatorio: 5/6/7
    estado_retiro_raw = body.get("estado_retiro")
    estado_retiro = None
    try:
        if estado_retiro_raw is not None and str(estado_retiro_raw).strip() != "":
            estado_retiro = int(estado_retiro_raw)
    except (TypeError, ValueError):
        estado_retiro = None

    # Compatibilidad hacia atrás por si llega "motivo" desde frontend antiguo.
    if estado_retiro is None:
        motivo = (body.get("motivo") or "").strip().lower()
        if "inconform" in motivo:
            estado_retiro = CUOTA_ESTADO_RETIRADO_INCONFORMIDAD  # 6
        elif "insatis" in motivo:
            estado_retiro = CUOTA_ESTADO_RETIRADO_INSATISFECHO  # 7
        elif "retir" in motivo:
            estado_retiro = CUOTA_ESTADO_RETIRADO  # 5

    if estado_retiro not in CUOTA_ESTADOS_RETIRADOS:
        return JsonResponse(
            {
                "success": False,
                "message": "Debes seleccionar el tipo de retiro: Retirada, Retirada por inconformidad o Retirada por insatisfacción.",
            },
            status=400,
        )

    cuota = get_object_or_404(
        Cuota.objects.select_related("venta__cliente", "venta__usuario"),
        pk=cuota_id,
    )
    venta = cuota.venta

    if venta.estado == VENTA_ESTADO_PAGADO:
        return JsonResponse(
            {"success": False, "message": "La venta ya está pagada y no se puede retirar."},
            status=400,
        )

    if cuota.estado == CUOTA_ESTADO_PAGADO:
        return JsonResponse(
            {"success": False, "message": "No puedes retirar una cuota ya pagada."},
            status=400,
        )

    if cuota.estado in CUOTA_ESTADOS_RETIRADOS:
        return JsonResponse(
            {"success": False, "message": "Esta cuota ya estaba marcada como retirada."},
            status=400,
        )

    if cuota.estado in CUOTA_ESTADOS_BLOQUEADOS:
        return JsonResponse(
            {"success": False, "message": "No puedes retirar una cuota anulada o en reembolso."},
            status=400,
        )

    cuotas_abiertas_qs = Cuota.objects.filter(
        venta=venta,
        estado__in=[
            CUOTA_ESTADO_PENDIENTE,
            CUOTA_ESTADO_VENCIDA,
            CUOTA_ESTADO_REINTENTO,
        ],
    )
    updated_rows = cuotas_abiertas_qs.update(
        estado=estado_retiro,
        observacion_cobranza=observacion,
    )
    if not updated_rows:
        return JsonResponse(
            {"success": False, "message": "No se encontraron cuotas abiertas para marcar como retiradas."},
            status=500,
        )
    cuota.refresh_from_db(fields=["estado", "observacion_cobranza"])

    estado_retiro_label = dict(Cuota.ESTADO_CHOICES).get(int(estado_retiro), "Retirada")
    venta_pasada_a_retirada = False
    if venta.estado != VENTA_ESTADO_RETIRADO:
        venta.estado = VENTA_ESTADO_RETIRADO
        venta.save(update_fields=["estado"])
        venta_pasada_a_retirada = True

    detalle_url = reverse("detalle_venta", args=[venta.id])
    _notify_user_and_vendors(
        venta.usuario,
        titulo="Venta retirada por cobranzas",
        mensaje=(
            f"La cuota #{cuota.numero_cuota} de la venta {venta.folio_venta} "
            f"fue marcada como '{estado_retiro_label}'. Observación: {observacion}"
        ),
        tipo="warning",
        url=detalle_url,
    )

    if venta_pasada_a_retirada:
        cliente_nombre = (
            f"{getattr(venta.cliente, 'nombre', '')} {getattr(venta.cliente, 'apellido', '')}".strip()
            or "Sin cliente"
        )
        _notify_campus_users(
            titulo=f"Venta retirada: {venta.folio_venta}",
            mensaje=(
                f"La venta {venta.folio_venta} ({cliente_nombre}) cambió a Retirado por cobranzas. "
                f"Tipo: {estado_retiro_label}. "
                f"Observación: {observacion}"
            ),
            tipo="warning",
            url=detalle_url,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Cuotas abiertas marcadas como retiradas y venta actualizada a Retirado."
            ),
            "venta_id": venta.id,
            "venta_estado": venta.estado,
            "cuota_id": cuota.id,
            "cuota_estado": cuota.estado,
            "cuotas_actualizadas": updated_rows,
        }
    )



# =========================================================
# REGISTRAR PAGO (Cliente/Vendedor) — estado 1 "Procesando"
# =========================================================
@require_POST
@transaction.atomic
def registrar_pago(request):
    try:
        reintento_cuota_id = (request.POST.get("reintentar_cuota_id") or "").strip()
        form = PagoForm(request.POST, request.FILES)

        if not form.is_valid():
            errores = {field: error[0] for field, error in form.errors.items()}
            return JsonResponse({"success": False, "errors": errores}, status=400)

        pago = form.save(commit=False)
        cuota = pago.cuota
        venta = cuota.venta

        bloqueo_pago_msg = _mensaje_bloqueo_pago_venta(venta)
        if bloqueo_pago_msg:
            return JsonResponse({"success": False, "errors": {"cuota": bloqueo_pago_msg}}, status=400)

        # ✅ BLOQUEO por estados nuevos
        if cuota.estado in CUOTA_ESTADOS_RETIRADOS:
            return JsonResponse(
                {"success": False, "errors": {"cuota": "Esta cuota fue marcada como retirada por cobranzas."}},
                status=400,
            )
        if cuota.estado in CUOTA_ESTADOS_BLOQUEADOS:
            return JsonResponse(
                {"success": False, "errors": {"cuota": "Esta cuota está bloqueada (anulada o en reembolso)."}},
                status=400,
            )

        # Validar voucher
        if not pago.voucher:
            return JsonResponse(
                {"success": False, "errors": {"voucher": "El comprobante de pago es obligatorio para la confirmación de Tesorería."}},
                status=400,
            )

        # Validar que no exista ya un pago confirmado
        if cuota.pagos.filter(estado=2).exists():
            return JsonResponse(
                {"success": False, "errors": {"cuota": "Esta cuota ya tiene un pago confirmado."}},
                status=400,
            )

        cuota_origen = None
        nueva_cuota = None
        estado_original = None

        # =========================================================
        # REINTENTO
        # =========================================================
        if reintento_cuota_id:
            cuota_origen = (
                Cuota.objects.select_related("venta")
                .prefetch_related("pagos")
                .filter(pk=reintento_cuota_id)
                .first()
            )
            if not cuota_origen:
                return JsonResponse({"success": False, "errors": {"cuota": "No se pudo identificar la cuota original para el reintento."}}, status=404)

            if str(cuota_origen.pk) != str(cuota.pk):
                return JsonResponse({"success": False, "errors": {"cuota": "La cuota seleccionada no coincide con la solicitud de reintento."}}, status=400)

            # ✅ ya reemplazada por reintento
            if cuota_origen.estado == CUOTA_ESTADO_REINTENTO:
                return JsonResponse({"success": False, "errors": {"cuota": "Esta cuota ya fue reemplazada por un reintento."}}, status=400)

            if cuota_origen.pagos.filter(estado=1).exists():
                return JsonResponse({"success": False, "errors": {"cuota": "Esta cuota aún tiene un pago en verificación."}}, status=400)

            if cuota_origen.pagos.filter(estado=2).exists():
                return JsonResponse({"success": False, "errors": {"cuota": "Esta cuota ya tiene un pago confirmado."}}, status=400)

            if not cuota_origen.pagos.filter(estado__in=[3, 4]).exists():
                return JsonResponse({"success": False, "errors": {"cuota": "Solo se puede reintentar un pago denegado o no validado."}}, status=400)

            # ✅ cuotas previas: excluye pagado + reintento (y también retirados/bloqueados si quieres que no bloqueen flujo)
            cuotas_previas = (
                venta.cuotas
                .filter(numero_cuota__lt=cuota_origen.numero_cuota)
                .exclude(estado__in=[CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO])
                .exclude(estado__in=CUOTA_ESTADOS_RETIRADOS)
                .exclude(estado__in=CUOTA_ESTADOS_BLOQUEADOS)
            )
            if cuotas_previas.exists():
                primera = cuotas_previas.order_by("numero_cuota").first()
                return JsonResponse({"success": False, "errors": {"cuota": f"Debes pagar primero la cuota #{primera.numero_cuota}."}}, status=400)

            max_num = Cuota.objects.filter(venta=venta).aggregate(Max("numero_cuota"))["numero_cuota__max"] or 0
            nueva_cuota = Cuota.objects.create(
                venta=venta,
                numero_cuota=max_num + 1,
                monto_total=cuota_origen.monto_total,
                fecha_registro=now().date(),
                fecha_vencimiento=cuota_origen.fecha_vencimiento,
                estado=CUOTA_ESTADO_PENDIENTE,
            )

            estado_original = cuota_origen.estado
            cuota_origen.estado = CUOTA_ESTADO_REINTENTO  # 4
            cuota_origen.save(update_fields=["estado"])

            pago.cuota = nueva_cuota
            cuota = nueva_cuota
            venta = cuota.venta

            if pago.observacion:
                pago.observacion = f"[Reintento cuota {cuota_origen.numero_cuota}] {pago.observacion}"
            else:
                pago.observacion = f"Reintento cuota {cuota_origen.numero_cuota}"

        # =========================================================
        # PAGO NORMAL
        # =========================================================
        else:
            cuotas_previas = (
                venta.cuotas
                .filter(numero_cuota__lt=cuota.numero_cuota)
                .exclude(estado__in=[CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO])
                .exclude(estado__in=CUOTA_ESTADOS_RETIRADOS)
                .exclude(estado__in=CUOTA_ESTADOS_BLOQUEADOS)
            )
            if cuotas_previas.exists():
                primera = cuotas_previas.order_by("numero_cuota").first()
                return JsonResponse({"success": False, "errors": {"cuota": f"Debes pagar primero la cuota #{primera.numero_cuota}."}}, status=400)

        monto_pagado = Decimal(pago.monto_pagado)
        monto_cuota_original = cuota.monto_total
        diferencia = monto_pagado - monto_cuota_original

        # ✅ cuotas_restantes: excluye reintento + todos los retirados + bloqueados
        cuotas_restantes = (
            venta.cuotas.filter(numero_cuota__gt=cuota.numero_cuota)
            .exclude(estado=CUOTA_ESTADO_REINTENTO)
            .exclude(estado__in=CUOTA_ESTADOS_RETIRADOS)
            .exclude(estado__in=CUOTA_ESTADOS_BLOQUEADOS)
            .order_by("numero_cuota")
        )
        es_ultima_cuota = not cuotas_restantes.exists()

        # Validar última cuota con tolerancia por redondeo
        if es_ultima_cuota and abs(diferencia) > Decimal("0.02"):
            if nueva_cuota and cuota_origen and estado_original is not None:
                cuota_origen.estado = estado_original
                cuota_origen.save(update_fields=["estado"])
                nueva_cuota.delete()
            return JsonResponse(
                {"success": False, "errors": {"monto_pagado": f"Debes pagar exactamente {monto_cuota_original:.2f} en esta última cuota."}},
                status=400,
            )

        # Guardar el pago en estado "Procesando" (1)
        pago.estado = 1
        pago.save()

        # Ajuste automático solo si no es la última cuota
        if not es_ultima_cuota and diferencia != 0:
            cuotas_pendientes = [
                c for c in cuotas_restantes
                if c.estado not in (CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO)
                and c.estado not in CUOTA_ESTADOS_RETIRADOS
                and c.estado not in CUOTA_ESTADOS_BLOQUEADOS
            ]

            if diferencia > 0:
                restante = diferencia
                for siguiente in cuotas_pendientes:
                    if restante <= 0:
                        break
                    monto_anterior = siguiente.monto_total
                    descuento = min(monto_anterior, restante)
                    siguiente.monto_total = (monto_anterior - descuento).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    siguiente.save()
                    restante -= descuento
            else:
                faltante = abs(diferencia)
                n = len(cuotas_pendientes)
                if n > 0:
                    cuota_extra = (faltante / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    for i, siguiente in enumerate(cuotas_pendientes):
                        if i == n - 1:
                            extra = faltante
                        else:
                            extra = cuota_extra
                            faltante -= cuota_extra
                        siguiente.monto_total = (siguiente.monto_total + extra).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        siguiente.save()

        _sync_estado_venta(venta)
        return JsonResponse({"success": True, "redirect_url": reverse("detalle_venta", args=[venta.pk])})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "message": f"Error interno: {str(e)}"}, status=500)


# =========================================================
# EDITAR VENTA
# =========================================================
def editar_venta(request, pk):
    venta = get_object_or_404(Venta, pk=pk)

    if request.method == "POST":
        venia_de_cotizacion = venta.estado == VENTA_ESTADO_COTIZACION
        venia_de_preventa = venta.estado == VENTA_ESTADO_PREVENTA

        form = VentaForm(request.POST, instance=venta)
        productos_data = request.POST.get("productos_json", "[]")
        cuotas_data = request.POST.get("cuotas_json", "[]")

        save_mode = (request.POST.get("save_mode") or "venta").strip().lower()
        if save_mode not in {"venta", "cotizacion"}:
            save_mode = "venta"

        is_preventa = _is_truthy(request.POST.get("is_preventa"))
        is_preventa_mode = bool(is_preventa and save_mode != "cotizacion")

        try:
            productos = json.loads(productos_data)
            cuotas = json.loads(cuotas_data)
        except json.JSONDecodeError:
            return JsonResponse({"success": False, "message": "Datos mal formateados"}, status=400)

        if not productos:
            return JsonResponse({"success": False, "message": "Debe agregar al menos un producto"}, status=400)

        if not cuotas:
            return JsonResponse({"success": False, "message": "Debe agregar al menos una cuota"}, status=400)

        for c in cuotas:
            if not c.get("fecha_vencimiento"):
                return JsonResponse(
                    {"success": False, "message": "Debe ingresar la fecha de vencimiento en todas las cuotas"},
                    status=400,
                )

        if form.is_valid():
            try:
                with transaction.atomic():
                    venta = form.save()

                    if save_mode == "cotizacion":
                        target_estado = VENTA_ESTADO_COTIZACION
                    elif is_preventa_mode:
                        target_estado = VENTA_ESTADO_PREVENTA
                    else:
                        target_estado = VENTA_ESTADO_PENDIENTE

                    # =========================================================
                    # ✅ Revertir reservas previas de los detalles antiguos
                    # =========================================================
                    for det_antiguo in venta.detalles.select_related("producto").prefetch_related("libros_en_pack"):
                        if _producto_requiere_stock(det_antiguo.producto) and det_antiguo.ubicacion:
                            _liberar_reserva(det_antiguo.producto, det_antiguo.ubicacion, int(det_antiguo.cantidad or 0))

                        if det_antiguo.ubicacion:
                            for l in det_antiguo.libros_en_pack.all():
                                if _producto_requiere_stock(l.libro):
                                    _liberar_reserva(l.libro, det_antiguo.ubicacion, int(l.cantidad or 0))

                    # =========================================================
                    # ✅ NO BORRAR cuotas "históricas" / cerradas
                    # - Pagadas (1)
                    # - Reintento (4) (cuota reemplazada)
                    # - Retiradas (5/6/7)
                    # - Bloqueadas (8/9) anulado/reembolso
                    # =========================================================
                    PRESERVAR_ESTADOS = (
                        {CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO}
                        | set(CUOTA_ESTADOS_RETIRADOS)
                        | set(CUOTA_ESTADOS_BLOQUEADOS)
                    )
                    numeros_preservados = set(
                        venta.cuotas.filter(estado__in=PRESERVAR_ESTADOS).values_list("numero_cuota", flat=True)
                    )
                    venta.cuotas.exclude(estado__in=PRESERVAR_ESTADOS).delete()

                    # =========================================================
                    # BORRAR DETALLES
                    # =========================================================
                    venta.detalles.all().delete()

                    # =========================================================
                    # GUARDAR PRODUCTOS EDITADOS
                    # =========================================================
                    for prod in productos:
                        producto = Producto.objects.filter(pk=prod["producto_id"]).first()
                        if not producto:
                            continue

                        tipo_certificado = _tipo_certificado_por_nombre(producto.nombre_producto)
                        curso_obj = None
                        if tipo_certificado:
                            curso_id = prod.get("certificado_curso_id")
                            if not curso_id:
                                return JsonResponse(
                                    {"success": False, "message": f"Debes seleccionar el curso para el certificado '{producto.nombre_producto}'."},
                                    status=400,
                                )
                            curso_obj = Producto.objects.filter(pk=curso_id).first()
                            if not curso_obj or curso_obj.codigo_negocio_id != 2 or curso_obj.estado != 1:
                                return JsonResponse(
                                    {"success": False, "message": "El curso seleccionado no pertenece al negocio Escuela o no está disponible."},
                                    status=400,
                                )

                        det_ubicacion = None if is_preventa_mode else _resolve_ubicacion_para_detalle(venta, prod)

                        if not is_preventa_mode and _producto_requiere_stock(producto) and not det_ubicacion:
                            raise ValidationError("Debes elegir una ubicación para los productos que requieren stock.")

                        if not is_preventa_mode and _producto_requiere_stock(producto) and det_ubicacion:
                            _reservar_stock(producto, det_ubicacion, int(prod["cantidad"]))

                        detalle = DetalleVenta.objects.create(
                            venta=venta,
                            producto=producto,
                            cantidad=prod["cantidad"],
                            precio_regular=prod.get("precio_regular", 0),
                            precio_venta=prod.get("precio_venta", 0),
                            precio_total=prod.get("precio_total", 0),
                            ubicacion=det_ubicacion,
                        )

                        if det_ubicacion and not venta.ubicacion_id:
                            venta.ubicacion = det_ubicacion
                            venta.save(update_fields=["ubicacion"])

                        if tipo_certificado and curso_obj:
                            CertificadoCurso.objects.create(
                                detalle_venta=detalle,
                                curso=curso_obj,
                                tipo_certificado=tipo_certificado,
                                cantidad=int(prod.get("cantidad") or 1),
                            )

                        # PACKS
                        if producto.codigo_categoria_id == 4 and prod.get("libros_en_pack"):
                            libros = prod["libros_en_pack"]
                            cantidad_pack = int(prod.get("cantidad", 1))
                            precio_total_pack = float(prod.get("precio_venta", 0)) * cantidad_pack

                            ids_unicos = list(set([l["id"] for l in libros]))

                            if len(ids_unicos) == 1:
                                libro = libros[0]
                                cantidad_final = int(libro["cantidad"]) * cantidad_pack
                                LibroEnPack.objects.create(
                                    detalle_venta_pack=detalle,
                                    libro_id=libro["id"],
                                    cantidad=cantidad_final,
                                    precio_unitario=precio_total_pack,
                                )
                            else:
                                total_unidades = sum(int(l["cantidad"]) for l in libros)
                                for libro in libros:
                                    cantidad_por_pack = int(libro["cantidad"])
                                    cantidad_final = cantidad_por_pack * cantidad_pack
                                    proporcion = cantidad_por_pack / total_unidades
                                    precio_unitario = round(proporcion * float(prod.get("precio_venta", 0)), 2)

                                    LibroEnPack.objects.create(
                                        detalle_venta_pack=detalle,
                                        libro_id=libro["id"],
                                        cantidad=cantidad_final,
                                        precio_unitario=precio_unitario,
                                    )

                            if det_ubicacion and not is_preventa_mode:
                                for l in libros:
                                    libro_obj = Producto.objects.filter(pk=l["id"]).first()
                                    if libro_obj and _producto_requiere_stock(libro_obj):
                                        _reservar_stock(libro_obj, det_ubicacion, int(l["cantidad"]) * cantidad_pack)

                    # =========================================================
                    # Volver a guardar solo cuotas NO preservadas
                    # =========================================================
                    numeros_ocupados = set(venta.cuotas.values_list("numero_cuota", flat=True))
                    numero_max = max(numeros_ocupados) if numeros_ocupados else 0

                    for c in cuotas:
                        numero_cuota = int(c.get("numero") or 0)

                        # Blindaje: si la cuota ya existe en estados preservados
                        # (pagada/reintento/retirada/bloqueada), nunca recrearla como pendiente.
                        if numero_cuota in numeros_preservados:
                            continue

                        estado_cuota = int(c.get("estado", CUOTA_ESTADO_PENDIENTE))

                        # Si viene de cotización, toda cuota editada debe quedar pendiente.
                        if venia_de_cotizacion:
                            estado_cuota = CUOTA_ESTADO_PENDIENTE

                        # ✅ No recrear cuotas que ya deben preservarse/histórico
                        if estado_cuota in PRESERVAR_ESTADOS:
                            continue

                        if numero_cuota <= 0 or numero_cuota in numeros_ocupados:
                            numero_max += 1
                            numero_cuota = numero_max
                        else:
                            numero_max = max(numero_max, numero_cuota)

                        fecha_registro = parse_date(str(c.get("fecha_registro") or "")) or timezone.localdate()
                        fecha_vencimiento = parse_date(str(c.get("fecha_vencimiento") or ""))

                        if not fecha_vencimiento:
                            raise ValidationError("Debe ingresar la fecha de vencimiento en todas las cuotas.")

                        Cuota.objects.create(
                            venta=venta,
                            numero_cuota=numero_cuota,
                            monto_total=Decimal(str(c.get("monto", 0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                            fecha_registro=fecha_registro,
                            fecha_vencimiento=fecha_vencimiento,
                            estado=estado_cuota,
                        )
                        numeros_ocupados.add(numero_cuota)

                    # =========================================================
                    # Ajuste: si el total de cuotas queda por debajo del monto_total
                    # se crea una cuota extra pendiente
                    # =========================================================
                    cuotas_actuales = list(venta.cuotas.all().order_by("numero_cuota"))
                    total_cuotas = sum((Decimal(str(c.monto_total or 0)) for c in cuotas_actuales), Decimal("0.00"))

                    monto_total_venta = Decimal(str(venta.monto_total or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    diferencia = (monto_total_venta - total_cuotas).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                    if diferencia < Decimal("0.00"):
                        raise ValidationError("La suma de cuotas supera el monto total de la venta. Ajusta las cuotas antes de guardar.")

                    if diferencia > Decimal("0.00"):
                        numero_extra = (max([c.numero_cuota for c in cuotas_actuales], default=0)) + 1
                        fecha_hoy = timezone.localdate()
                        fecha_venc_extra = max([c.fecha_vencimiento for c in cuotas_actuales], default=fecha_hoy)

                        Cuota.objects.create(
                            venta=venta,
                            numero_cuota=numero_extra,
                            monto_total=diferencia,
                            fecha_registro=fecha_hoy,
                            fecha_vencimiento=fecha_venc_extra,
                            estado=CUOTA_ESTADO_PENDIENTE,
                        )

                    # =========================================================
                    # Estado venta
                    # =========================================================
                    if save_mode == "cotizacion":
                        if venta.estado != VENTA_ESTADO_COTIZACION:
                            venta.estado = VENTA_ESTADO_COTIZACION
                            venta.save(update_fields=["estado"])
                    else:
                        if is_preventa_mode:
                            if venta.estado != VENTA_ESTADO_PREVENTA:
                                venta.estado = VENTA_ESTADO_PREVENTA
                                venta.save(update_fields=["estado"])
                        elif venta.estado in (VENTA_ESTADO_COTIZACION, VENTA_ESTADO_PREVENTA) or venia_de_preventa:
                            venta.estado = target_estado
                            venta.save(update_fields=["estado"])

                        _sync_estado_venta(venta)

                return JsonResponse({"success": True, "redirect_url": reverse("lista_ventas")})

            except ValidationError as ve:
                return JsonResponse({"success": False, "message": str(ve)}, status=400)
            except Exception as e:
                return JsonResponse({"success": False, "message": f"Error interno: {str(e)}"}, status=500)

        return JsonResponse({"success": False, "message": "Formulario inválido"}, status=400)

    # =========================================================
    # GET
    # =========================================================
    form = VentaForm(instance=venta)

    productos = []
    for detalle in venta.detalles.select_related("producto", "certificado_curso__curso").prefetch_related(
        Prefetch("libros_en_pack", queryset=LibroEnPack.objects.select_related("libro"))
    ):
        item = {
            "producto_id": detalle.producto_id,
            "nombre": detalle.producto.nombre_producto,
            "precio_base_usd": float(detalle.producto.precio_normal),
            "precio_regular": float(detalle.precio_regular),
            "precio_venta": float(detalle.precio_venta),
            "cantidad": detalle.cantidad,
            "precio_total": float(detalle.precio_total),
            "es_pack": detalle.producto.codigo_categoria_id == 4,
            "ubicacion_id": detalle.ubicacion_id,
            "ubicacion_nombre": detalle.ubicacion.nombre_ubicacion if detalle.ubicacion else "",
            "libros_en_pack": [],
        }

        try:
            cert_rel = detalle.certificado_curso
        except CertificadoCurso.DoesNotExist:
            cert_rel = None

        if cert_rel:
            item["certificado_curso_id"] = cert_rel.curso_id
            item["certificado_curso_nombre"] = cert_rel.curso.nombre_producto if cert_rel.curso_id else ""
            item["certificado_tipo"] = cert_rel.tipo_certificado

        if item["es_pack"]:
            for l in detalle.libros_en_pack.all():
                item["libros_en_pack"].append(
                    {
                        "id": l.libro_id,
                        "nombre": l.libro.nombre_producto,
                        "cantidad": l.cantidad,
                        "imagen": l.libro.imagen_producto.url if l.libro.imagen_producto else "",
                    }
                )

        productos.append(item)

    cuotas = [
        {
            "numero": c.numero_cuota,
            "monto": float(c.monto_total),
            "estado": c.estado,
            "estado_label": c.get_estado_display(),
            "fecha_registro": c.fecha_registro.strftime("%Y-%m-%d") if c.fecha_registro else "",
            "fecha_vencimiento": c.fecha_vencimiento.strftime("%Y-%m-%d") if c.fecha_vencimiento else "",
        }
        for c in venta.cuotas.all().order_by("numero_cuota")
    ]

    return render(
        request,
        "sales/editar_venta.html",
        {
            "form": form,
            "venta": venta,
            "productos_json": json.dumps(productos),
            "cuotas_json": json.dumps(cuotas),
            "cuotas_json_original": json.dumps(cuotas),
            "negocios": Negocio.objects.all(),
            "categorias": Categoria.objects.all(),
            "divisiones": Division.objects.all(),
            "timestamp": now().timestamp(),
        },
    )



# =========================================================
# ELIMINAR VENTA (con liberación de reservas)
# =========================================================
@csrf_protect
@transaction.atomic
def eliminar_venta(request, pk):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Método no permitido"}, status=405)

    venta = get_object_or_404(Venta, pk=pk)

    try:
        # Liberar reservas antes de eliminar detalles/libros en pack.
        for detalle in venta.detalles.select_related("producto").prefetch_related("libros_en_pack"):
            if _producto_requiere_stock(detalle.producto) and detalle.ubicacion:
                _liberar_reserva(detalle.producto, detalle.ubicacion, int(detalle.cantidad or 0))
            if detalle.ubicacion:
                for l in detalle.libros_en_pack.all():
                    if _producto_requiere_stock(l.libro):
                        _liberar_reserva(l.libro, detalle.ubicacion, int(l.cantidad or 0))

        for cuota in venta.cuotas.all():
            cuota.pagos.all().delete()

        venta.detalles.all().delete()
        venta.cuotas.all().delete()
        venta.delete()

        return JsonResponse({"success": True, "message": "Venta eliminada correctamente con todos sus datos relacionados."})

    except Exception as e:
        return JsonResponse({"success": False, "message": f"Error al eliminar la venta: {str(e)}"}, status=500)


# =========================================================
# APIs: locales / ubicaciones
# =========================================================
@require_GET
def api_locales_por_pais(request, pais_id):
    locales = TbLocal.objects.filter(pais_id=pais_id, activo=True).order_by("nombre_local")
    data = [{"id": l.codigo_local, "nombre": l.nombre_local} for l in locales]
    return JsonResponse({"success": True, "locales": data})


@require_GET
def api_ubicaciones_por_local(request, local_id):
    ubicaciones = TbUbicacion.objects.filter(local_id=local_id, activa=True).order_by("nombre_ubicacion")
    data = [{"id": u.codigo_ubicacion, "nombre": u.nombre_ubicacion} for u in ubicaciones]
    return JsonResponse({"success": True, "ubicaciones": data})


# =========================================================
# TESORERÍA - LISTA / CONFIRMAR / DENEGAR
# =========================================================
@login_required
def lista_pagos_pendientes(request):
    """Vista para que Tesorería vea pagos pendientes de confirmación"""
    if not (
        request.user.groups.filter(id=TREASURY_GROUP_ID).exists()
        or request.user.groups.filter(id__in=ADMIN_GROUP_IDS).exists()
    ):
        return redirect("home")

    voucher_purge_errors = []
    voucher_purge_saved = request.GET.get("voucher_purge_saved") == "1"
    voucher_purge_autorun_message = None

    # Guardar configuración purge desde UI
    if request.method == "POST" and request.POST.get("action") == "update_voucher_purge":
        enabled = request.POST.get("enabled") == "on"
        start_date_raw = (request.POST.get("start_date") or "").strip()
        every_months_raw = (request.POST.get("every_months") or "").strip()
        notify_days_raw = (request.POST.get("notify_days") or "").strip()

        start_date = None
        if not start_date_raw:
            voucher_purge_errors.append("La fecha de inicio es obligatoria.")
        else:
            try:
                start_date = parse_date_str(start_date_raw)
            except ValueError as exc:
                voucher_purge_errors.append(str(exc))

        if not every_months_raw.isdigit() or int(every_months_raw) < 1:
            voucher_purge_errors.append("La frecuencia debe ser un número de meses (mínimo 1).")
        if not notify_days_raw.isdigit() or int(notify_days_raw) < 0:
            voucher_purge_errors.append("El aviso debe ser un número de días (0 o más).")

        if not voucher_purge_errors:
            cfg, _ = VoucherPurgeConfig.objects.get_or_create(pk=1)
            cfg.enabled = enabled
            cfg.start_date = start_date
            cfg.every_months = int(every_months_raw)
            cfg.notify_days = int(notify_days_raw)
            cfg.updated_by = request.user
            cfg.save()

            params = request.GET.copy()
            params["voucher_purge_saved"] = "1"
            query = params.urlencode()
            return redirect(f"{request.path}?{query}" if query else request.path)

    # Autorun (GET)
    if request.method == "GET":
        try:
            autorun_result = run_due_actions()
            if autorun_result.get("did_notify") and autorun_result.get("did_purge"):
                voucher_purge_autorun_message = "Hoy se envió el aviso y se ejecutó la limpieza de vouchers."
            elif autorun_result.get("did_notify"):
                voucher_purge_autorun_message = "Hoy se envió el aviso de limpieza de vouchers."
            elif autorun_result.get("did_purge"):
                voucher_purge_autorun_message = "Hoy se ejecutó la limpieza de vouchers."
        except ValueError as exc:
            voucher_purge_errors.append(str(exc))

    # Filtros
    estado_filtro = request.GET.get("estado", "pendientes")
    fecha_desde = request.GET.get("fecha_desde", "")
    fecha_hasta = request.GET.get("fecha_hasta", "")
    folio = (request.GET.get("folio") or "").strip()
    metodo = (request.GET.get("metodo") or "").strip()

    queryset = (
        Pago.objects.select_related(
            "cuota__venta__cliente",
            "cuota__venta__usuario",
            "cuota__venta__moneda",
            "moneda",
            "metodo",
            "metodo__moneda",
        )
        .order_by("-fecha_pago")
    )
    # Las cotizaciones no entran al flujo de tesorería.
    queryset = queryset.exclude(
        cuota__venta__estado__in=[
            VENTA_ESTADO_COTIZACION,
            VENTA_ESTADO_PREVENTA,
            VENTA_ESTADO_RETIRADO,
            VENTA_ESTADO_ANULADO,
            VENTA_ESTADO_REEMBOLSADO,
        ]
    )


    if estado_filtro == "pendientes":
        queryset = queryset.filter(estado=1).exclude(cuota__estado=CUOTA_ESTADO_PAGADO)
    elif estado_filtro == "pagados":
        queryset = queryset.filter(estado=2)
    elif estado_filtro == "denegados":
        queryset = queryset.filter(estado=4)

    if fecha_desde:
        queryset = queryset.filter(fecha_pago__gte=fecha_desde)
    if fecha_hasta:
        queryset = queryset.filter(fecha_pago__lte=fecha_hasta)
    if folio:
        queryset = queryset.filter(cuota__venta__folio_venta__icontains=folio)

    # Métodos disponibles según el resto de filtros (estado/fecha/folio)
    metodos_filtro = (
        MetodoPago.objects.filter(pagos__in=queryset)
        .select_related("moneda", "pais")
        .distinct()
        .order_by("tipo_pago", "id")
    )

    # Filtro por método de pago (selector por id)
    if metodo and metodo.isdigit():
        queryset = queryset.filter(metodo_id=int(metodo))
    elif metodo:
        queryset = queryset.filter(metodo__tipo_pago__icontains=metodo)

    # Config purge (defaults desde settings)
    default_start_date_value = getattr(settings, "VOUCHERS_PURGE_START_DATE", "2024-01-01")
    try:
        default_start_date = parse_date_str(str(default_start_date_value))
    except ValueError:
        default_start_date = timezone.localdate()

    default_every_months = int(getattr(settings, "VOUCHERS_PURGE_EVERY_MONTHS", 3))
    default_notify_days = int(getattr(settings, "VOUCHERS_PURGE_NOTIFY_DAYS", 1))
    default_enabled = bool(getattr(settings, "VOUCHERS_PURGE_ENABLED", True))

    cfg = VoucherPurgeConfig.objects.first()
    voucher_purge = {
        "enabled": default_enabled,
        "start_date": default_start_date,
        "every_months": default_every_months,
        "notify_days": default_notify_days,
        "updated_at": None,
        "updated_by": None,
    }
    if cfg:
        voucher_purge["enabled"] = cfg.enabled
        voucher_purge["start_date"] = cfg.start_date or default_start_date
        voucher_purge["every_months"] = cfg.every_months or default_every_months
        voucher_purge["notify_days"] = cfg.notify_days if cfg.notify_days is not None else default_notify_days
        voucher_purge["updated_at"] = cfg.updated_at
        voucher_purge["updated_by"] = cfg.updated_by

    today = timezone.localdate()
    next_purge = next_purge_date(today, voucher_purge["start_date"], voucher_purge["every_months"])
    notify_date = next_purge - timedelta(days=voucher_purge["notify_days"])

    # Paginación
    page_number = request.GET.get("page")
    paginator = Paginator(queryset, 20)
    pagos_page = paginator.get_page(page_number)
    query_params = request.GET.copy()
    query_params.pop("page", None)

    return render(
        request,
        "treasury/lista_pagos_pendientes.html",
        {
            "pagos": pagos_page,
            "estado_actual": estado_filtro,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
            "folio": folio,
            "metodo": metodo,
            "metodos_filtro": metodos_filtro,
            "query_params": query_params.urlencode(),
            "voucher_purge": voucher_purge,
            "voucher_purge_next_date": next_purge,
            "voucher_purge_notify_date": notify_date,
            "voucher_purge_saved": voucher_purge_saved,
            "voucher_purge_errors": voucher_purge_errors,
            "voucher_purge_autorun_message": voucher_purge_autorun_message,
        },
    )


@login_required
@require_POST
@transaction.atomic
def confirmar_pago(request, pago_id):
    if not (
        request.user.groups.filter(id=TREASURY_GROUP_ID).exists()
        or request.user.groups.filter(id__in=ADMIN_GROUP_IDS).exists()
    ):
        return JsonResponse({"success": False, "message": "No autorizado"}, status=403)

    pago = get_object_or_404(Pago, id=pago_id)
    bloqueo_pago_msg = _mensaje_bloqueo_pago_venta(pago.cuota.venta)
    if bloqueo_pago_msg:
        return JsonResponse({"success": False, "message": bloqueo_pago_msg}, status=400)

    if pago.estado != 1:
        return JsonResponse({"success": False, "message": "Este pago ya fue procesado anteriormente"}, status=400)

    if pago.cuota.estado == CUOTA_ESTADO_PAGADO:
        return JsonResponse(
            {"success": False, "message": "La cuota ya está pagada y no requiere confirmación de Tesorería."},
            status=400,
        )

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = {}

    observacion = (body.get("observacion") or "").strip()
    if not observacion or len(observacion) < 5:
        return JsonResponse({"success": False, "message": "La observación es obligatoria."}, status=400)

    try:
        pago.estado = 2  # Completado
        pago.confirmado_por = request.user
        pago.fecha_confirmacion = timezone.now()
        observacion_tesoreria = f"[Confirmado por Tesorería] {observacion}"
        if (pago.observacion or "").strip():
            pago.observacion = f"{pago.observacion}\n{observacion_tesoreria}"
        else:
            pago.observacion = observacion_tesoreria
        pago.save()

        cuota = pago.cuota
        cuota.estado = 1  # Pagado
        cuota.save()

        venta = cuota.venta
        estado_anterior = venta.estado
        nuevo_estado = _sync_estado_venta(venta)

        if estado_anterior != 1 and nuevo_estado == 1:

            _notify_user_and_vendors(
                venta.usuario,
                titulo="Pago confirmado por Tesorería",
                mensaje=f"La venta {venta.folio_venta} ha sido marcada como PAGADA por Tesorería",
                tipo="success",
                url=reverse("detalle_venta", args=[venta.id]),
            )

        return JsonResponse({"success": True, "message": "Pago confirmado exitosamente"})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "message": f"Error al confirmar pago: {str(e)}"}, status=500)


@login_required
@require_POST
@transaction.atomic
def denegar_pago(request, pago_id):
    """Marca un pago como No Validado, guardando la observación ingresada."""
    if not (
        request.user.groups.filter(id=TREASURY_GROUP_ID).exists()
        or request.user.groups.filter(id__in=ADMIN_GROUP_IDS).exists()
    ):
        return JsonResponse({"success": False, "message": "No autorizado"}, status=403)

    pago = get_object_or_404(Pago, id=pago_id)
    bloqueo_pago_msg = _mensaje_bloqueo_pago_venta(pago.cuota.venta)
    if bloqueo_pago_msg:
        return JsonResponse({"success": False, "message": bloqueo_pago_msg}, status=400)

    if pago.estado != 1:
        return JsonResponse({"success": False, "message": "Este pago ya fue procesado anteriormente"}, status=400)

    if pago.cuota.estado == CUOTA_ESTADO_PAGADO:
        return JsonResponse(
            {"success": False, "message": "La cuota ya está pagada y no requiere validación adicional."},
            status=400,
        )

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = {}

    observacion = (body.get("observacion") or "").strip()
    if not observacion or len(observacion) < 5:
        return JsonResponse({"success": False, "message": "La observación es obligatoria."}, status=400)

    try:
        pago.estado = 4  # Denegado
        pago.observacion = observacion
        pago.confirmado_por = request.user
        pago.fecha_confirmacion = timezone.now()
        pago.save()

        cuota = pago.cuota
        cuota.estado = 2  # Mantener en pendiente
        cuota.save()

        venta = cuota.venta
        _sync_estado_venta(venta)
        vendedor = venta.usuario
        detalle_url = reverse("detalle_venta", args=[venta.id])

        _notify_user_and_vendors(
            vendedor,
            titulo="Pago denegado por Tesorería",
            mensaje=f"El pago de la venta {venta.folio_venta} fue denegado. Motivo: {observacion}",
            tipo="error",
            url=detalle_url,
        )
        _notify_admins(
            titulo="Pago denegado por Tesorería",
            mensaje=f"La venta {venta.folio_venta} tiene un pago denegado. Motivo: {observacion}",
            tipo="error",
            url=detalle_url,
        )

        return JsonResponse({"success": True, "message": "Pago marcado como No Validado."})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "message": f"Error al denegar pago: {str(e)}"}, status=500)


@login_required
@require_POST
@transaction.atomic
def devolver_venta_pagada(request, pago_id):
    if not (
        request.user.groups.filter(id=TREASURY_GROUP_ID).exists()
        or request.user.groups.filter(id__in=ADMIN_GROUP_IDS).exists()
    ):
        return JsonResponse({"success": False, "message": "No autorizado"}, status=403)

    pago = get_object_or_404(
        Pago.objects.select_related("cuota__venta__usuario", "cuota__venta__cliente"),
        id=pago_id,
    )
    venta = pago.cuota.venta

    if pago.estado != 2:
        return JsonResponse({"success": False, "message": "Solo se pueden devolver pagos completados."}, status=400)

    if venta.estado == VENTA_ESTADO_ANULADO:
        return JsonResponse({"success": False, "message": "La venta ya está anulada."}, status=400)
    if venta.estado == VENTA_ESTADO_REEMBOLSADO:
        return JsonResponse({"success": False, "message": "La venta ya está reembolsada."}, status=400)

    venta.estado = VENTA_ESTADO_REEMBOLSADO
    venta.save(update_fields=["estado"])

    detalle_url = reverse("detalle_venta", args=[venta.id])
    _notify_user_and_vendors(
        venta.usuario,
        titulo="Venta reembolsada por Tesorería",
        mensaje=f"La venta {venta.folio_venta} fue marcada como reembolsada por devolución.",
        tipo="warning",
        url=detalle_url,
    )

    return JsonResponse(
        {
            "success": True,
            "message": f"Devolución aplicada. La venta {venta.folio_venta} quedó en estado Reembolsado.",
            "venta_id": venta.id,
            "venta_estado": venta.estado,
        }
    )


# =========================================================
# EXPORTACIÓN EXCEL TESORERÍA (Backend Nativo)
# =========================================================
def export_pagos_excel(request):
    estado_filtro = request.GET.get("estado", "pendientes")
    fecha_desde = request.GET.get("fecha_desde", "")
    fecha_hasta = request.GET.get("fecha_hasta", "")
    folio = (request.GET.get("folio") or "").strip()
    metodo = (request.GET.get("metodo") or "").strip()

    queryset = (
        Pago.objects.exclude(voucher__isnull=True)
        .exclude(voucher="")
        .select_related("cuota__venta__cliente", "cuota__venta__moneda", "moneda", "metodo")
        .order_by("-fecha_pago")
    )
    queryset = queryset.exclude(
        cuota__venta__estado__in=[
            VENTA_ESTADO_COTIZACION,
            VENTA_ESTADO_PREVENTA,
            VENTA_ESTADO_RETIRADO,
            VENTA_ESTADO_ANULADO,
            VENTA_ESTADO_REEMBOLSADO,
        ]
    )

    if estado_filtro == "pendientes":
        queryset = queryset.filter(estado=1).exclude(cuota__estado=CUOTA_ESTADO_PAGADO)
    elif estado_filtro == "pagados":
        queryset = queryset.filter(estado=2)
    elif estado_filtro == "denegados":
        queryset = queryset.filter(estado=4)

    if fecha_desde:
        queryset = queryset.filter(fecha_pago__gte=fecha_desde)
    if fecha_hasta:
        queryset = queryset.filter(fecha_pago__lte=fecha_hasta)
    if folio:
        queryset = queryset.filter(cuota__venta__folio_venta__icontains=folio)
    if metodo and metodo.isdigit():
        queryset = queryset.filter(metodo_id=int(metodo))
    elif metodo:
        queryset = queryset.filter(metodo__tipo_pago__icontains=metodo)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    filename = f"Reporte_Pagos_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pagos Tesorería"

    headers = ["Folio Venta", "Cliente", "Fecha Pago", "Método", "Monto Cuota", "Moneda", "Monto Pago", "Estado", "Voucher"]
    ws.append(headers)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="193B59", end_color="193B59", fill_type="solid")
    center_align = Alignment(horizontal="center", vertical="center")

    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align

    for pago in queryset:
        estado_label = dict(Pago.ESTADO_CHOICES).get(pago.estado, "Desconocido")

        voucher_url = ""
        if pago.voucher:
            voucher_url = request.build_absolute_uri(pago.voucher.url)

        row_data = [
            pago.cuota.venta.folio_venta,
            f"{pago.cuota.venta.cliente.nombre} {pago.cuota.venta.cliente.apellido}",
            pago.fecha_pago,
            pago.metodo.tipo_pago if pago.metodo else "N/A",
            pago.cuota.monto_total,
            pago.moneda.nombre,
            pago.monto_pagado,
            estado_label,
            voucher_url,
        ]
        ws.append(row_data)

        if voucher_url:
            cell = ws.cell(row=ws.max_row, column=9)
            cell.value = str(voucher_url)
            cell.hyperlink = voucher_url
            cell.font = Font(color="0563C1", underline="single")

        ws.row_dimensions[ws.max_row].height = 20

    ws.column_dimensions["A"].width = 15
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 15
    ws.column_dimensions["D"].width = 15
    ws.column_dimensions["E"].width = 15
    ws.column_dimensions["F"].width = 10
    ws.column_dimensions["G"].width = 15
    ws.column_dimensions["H"].width = 15
    ws.column_dimensions["I"].width = 20

    wb.save(response)
    return response


# ---------------------------------------------------------
# Helper: Lee payload soportando JSON y form-data
# ---------------------------------------------------------
def _get_payload(request):
    """
    Soporta:
    - application/json (tu fetch actual)
    - form-encoded / multipart (por si luego envías desde <form>)
    """
    ct = (request.content_type or "").lower()
    if "application/json" in ct:
        try:
            return json.loads((request.body or b"{}").decode("utf-8"))
        except Exception:
            return None
    return request.POST


# ---------------------------------------------------------
# Helpers: Cliente Teléfono/Correo preferidos
# ---------------------------------------------------------
def _pick_cliente_telefono(cliente_id: int) -> str | None:
    # 1) Personal
    t = Telefono.objects.filter(cliente_id=cliente_id, tipo_telefono="Personal").order_by("id").first()
    if not t:
        # 2) Cualquiera
        t = Telefono.objects.filter(cliente_id=cliente_id).order_by("id").first()

    if not t:
        return None

    pref = (t.prefijo or "").strip()
    num = (t.numero or "").strip()
    full = f"{pref} {num}".strip()
    return full or None


def _pick_cliente_correo(cliente_id: int) -> str | None:
    # 1) personal
    e = Correo.objects.filter(cliente_id=cliente_id, tipo_correo="personal").order_by("id").first()
    if not e:
        # 2) corporativo
        e = Correo.objects.filter(cliente_id=cliente_id, tipo_correo="corporativo").order_by("id").first()
    if not e:
        # 3) cualquiera
        e = Correo.objects.filter(cliente_id=cliente_id).order_by("id").first()

    if not e:
        return None

    return (e.nombre_correo or "").strip() or None


# ---------------------------------------------------------
# Helpers: Scope de facturación por país del perfil
# ---------------------------------------------------------
def _pais_facturacion_usuario_id(user) -> int | None:
    perfil = getattr(user, "perfil", None)
    pais_id = getattr(perfil, "pais_facturacion_id", None)
    return int(pais_id) if pais_id else None


def _base_qs_facturacion():
    facturada_sq = Facturacion.objects.filter(venta_id=OuterRef("pk"))

    # ✅ Estados que NO deben bloquear (venta “cerrada”)
    CUOTA_ESTADOS_NO_BLOQUEAN = (
        {CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO}
        | set(CUOTA_ESTADOS_RETIRADOS)
        | set(CUOTA_ESTADOS_BLOQUEADOS)
    )

    cuotas_abiertas_sq = (
        Cuota.objects
        .filter(venta_id=OuterRef("pk"))
        .exclude(estado__in=CUOTA_ESTADOS_NO_BLOQUEAN)
    )

    pagos_no_verificados_sq = (
        Pago.objects
        .filter(cuota__venta_id=OuterRef("pk"))
        .filter(~Q(estado=2) | Q(confirmado_por__isnull=True))
    )

    last_pago_qs = (
        Pago.objects
        .filter(cuota__venta_id=OuterRef("pk"), estado=2, confirmado_por__isnull=False)
        .order_by("-fecha_pago", "-id")
    )

    last_fact_qs = (
        Facturacion.objects
        .filter(venta_id=OuterRef("pk"))
        .order_by("-fecha_emision", "-id")
    )

    return (
        Venta.objects
        .annotate(
            is_facturada=Exists(facturada_sq),
            has_cuotas_abiertas=Exists(cuotas_abiertas_sq),
            has_pagos_no_verificados=Exists(pagos_no_verificados_sq),
            metodo_pago_nombre=Subquery(last_pago_qs.values("metodo__tipo_pago")[:1]),
            metodo_pago_id=Subquery(last_pago_qs.values("metodo_id")[:1]),
            pais_metodo_nombre=Subquery(last_pago_qs.values("metodo__pais__nombre")[:1]),
            pais_metodo_id=Subquery(last_pago_qs.values("metodo__pais_id")[:1]),
            fact_tipo=Subquery(last_fact_qs.values("tipo_comprobante")[:1]),
            fact_num=Subquery(last_fact_qs.values("numero_comprobante")[:1]),
            fact_fecha=Subquery(last_fact_qs.values("fecha_emision")[:1]),
        )
        .filter(
            estado=VENTA_ESTADO_PAGADO,
            has_cuotas_abiertas=False,
            has_pagos_no_verificados=False,
        )
        .select_related(
            "cliente", "moneda", "usuario", "pais",
            "facturacion", "facturacion__registrado_por",
        )
        .order_by("-fecha_venta")
    )



def _aplicar_scope_facturacion_por_pais(queryset, user):
    pais_id = _pais_facturacion_usuario_id(user)
    if pais_id:
        return queryset.filter(pais_metodo_id=pais_id)
    return queryset


# ---------------------------------------------------------
# FACTURACIÓN: LISTA (COMPLETO)
# ---------------------------------------------------------
@login_required
def lista_ventas_facturacion(request):
    user = request.user

    # =========================
    # GET params
    # =========================
    tab = (request.GET.get("tab") or "pendientes").strip()  # pendientes|facturadas|todas
    cliente_q = (request.GET.get("cliente") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip().upper()  # BOLETA|FACTURA|"" (solo aplica en facturadas/todas)
    per_page = (request.GET.get("per_page") or "10").strip()
    page_number = request.GET.get("page") or 1

    try:
        per_page_int = int(per_page)
        if per_page_int not in (10, 25, 50):
            per_page_int = 10
    except ValueError:
        per_page_int = 10

    # =========================
    # Base + scope por país de facturación del perfil
    # - Si perfil.pais_facturacion es NULL: ve todo
    # - Si tiene país: solo ese país por método de pago
    # =========================
    base_qs = _aplicar_scope_facturacion_por_pais(_base_qs_facturacion(), user)

    # =========================
    # Filtro por cliente / folio
    # =========================
    if cliente_q:
        base_qs = base_qs.filter(
            Q(folio_venta__icontains=cliente_q) |
            Q(cliente__nombre__icontains=cliente_q) |
            Q(cliente__apellido__icontains=cliente_q)
        )

    # =========================
    # Tabs
    # =========================
    if tab == "facturadas":
        qs = base_qs.filter(is_facturada=True)
    elif tab == "todas":
        qs = base_qs
    else:
        tab = "pendientes"
        qs = base_qs.filter(is_facturada=False)

    # =========================
    # Filtro tipo comprobante
    # - Solo aplica en facturadas/todas
    # =========================
    if tipo in ("BOLETA", "FACTURA") and tab in ("facturadas", "todas"):
        qs = qs.filter(fact_tipo=tipo)
    else:
        # en pendientes no aplica o si vino basura
        if tab == "pendientes":
            tipo = ""
        elif tipo not in ("BOLETA", "FACTURA"):
            tipo = ""

    # =========================
    # Conteos para chips (sobre base_qs)
    # =========================
    count_pendientes = base_qs.filter(is_facturada=False).count()
    count_facturadas = base_qs.filter(is_facturada=True).count()
    count_todas = base_qs.count()

    hoy = timezone.localdate()
    count_hoy = base_qs.filter(fecha_venta__date=hoy).count()

    # =========================
    # Paginación
    # =========================
    paginator = Paginator(qs, per_page_int)
    page_obj = paginator.get_page(page_number)
    ventas = page_obj.object_list

    # Querystring sin page
    qd = request.GET.copy()
    qd.pop("page", None)
    querystring = qd.urlencode()

    return render(
        request,
        "sales/lista_ventas_facturacion.html",
        {
            "ventas": ventas,
            "page_obj": page_obj,
            "per_page": per_page_int,
            "cliente": cliente_q,
            "tipo": tipo if tipo in ("BOLETA", "FACTURA") else "",
            "tab": tab,
            "querystring": querystring,

            # chips
            "count_pendientes": count_pendientes,
            "count_facturadas": count_facturadas,
            "count_hoy": count_hoy,
            "count_todas": count_todas,
        },
    )


# ---------------------------------------------------------
# DATA FACTURACIÓN (modal)  -> SOLO PENDIENTES (CREAR)
# ---------------------------------------------------------
@login_required
@require_GET
def api_facturacion_data(request, venta_id):
    qs = _aplicar_scope_facturacion_por_pais(
        _base_qs_facturacion().filter(is_facturada=False),
        request.user,
    )

    venta = qs.filter(pk=venta_id).first()
    if not venta:
        return JsonResponse({"ok": False, "error": "Venta no disponible para facturación."}, status=404)

    cliente = venta.cliente
    nombre, apellido = _get_cliente_nombre_apellido(cliente)
    cliente_nombre = f"{nombre} {apellido}".strip() or getattr(cliente, "codigo_cliente", "") or "-"

    dni = (getattr(cliente, "dni", "") or "").strip() or None
    telefono = _pick_cliente_telefono(cliente.id) if cliente else None
    correo = _pick_cliente_correo(cliente.id) if cliente else None

    monto_decimal = Decimal(venta.monto_total or "0").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    monto_txt = f"{monto_decimal:.2f}"

    return JsonResponse(
        {
            "ok": True,
            "fecha_emision_default": timezone.localdate().isoformat(),
            "venta": {
                "id": venta.id,
                "folio": venta.folio_venta,
                "cliente": cliente_nombre,
                "dni": dni,
                "telefono": telefono,
                "correo": correo,
                "monto_total": monto_txt,
                "moneda": venta.moneda.nombre if venta.moneda_id else None,
            },
            # Compat: shape plana usada por versiones anteriores
            "folio": venta.folio_venta,
            "cliente": cliente_nombre,
            "documento": dni,
            "telefono": telefono,
            "correo": correo,
            "monto": monto_txt,
        }
    )


# ---------------------------------------------------------
# DETALLE FACTURACIÓN (modal) -> SOLO FACTURADAS (VER)
# ---------------------------------------------------------
@login_required
@require_GET
def api_facturacion_detalle(request, venta_id):
    qs = _aplicar_scope_facturacion_por_pais(
        _base_qs_facturacion(),  # aquí sí permitimos ver facturadas
        request.user,
    )

    venta = qs.filter(pk=venta_id, is_facturada=True).first()
    if not venta:
        return JsonResponse({"ok": False, "error": "Facturación no encontrada o no autorizada."}, status=404)

    # OneToOne: debería estar si is_facturada=True
    fact = getattr(venta, "facturacion", None)
    if not fact:
        return JsonResponse({"ok": False, "error": "La venta no tiene facturación registrada."}, status=404)

    cliente = venta.cliente
    nombre, apellido = _get_cliente_nombre_apellido(cliente)
    cliente_nombre = f"{nombre} {apellido}".strip() or getattr(cliente, "codigo_cliente", "") or "-"

    dni = (getattr(cliente, "dni", "") or "").strip() or None
    telefono = _pick_cliente_telefono(cliente.id) if cliente else None
    correo = _pick_cliente_correo(cliente.id) if cliente else None

    monto_decimal = Decimal(venta.monto_total or "0").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    monto_txt = f"{monto_decimal:.2f}"

    return JsonResponse(
        {
            "ok": True,
            "venta": {
                "id": venta.id,
                "folio": venta.folio_venta,
                "cliente": cliente_nombre,
                "dni": dni,
                "telefono": telefono,
                "correo": correo,
                "monto_total": monto_txt,
                "moneda": venta.moneda.nombre if venta.moneda_id else None,
                "fecha_venta": timezone.localtime(venta.fecha_venta).strftime("%Y-%m-%d %H:%M") if venta.fecha_venta else None,
            },
            "facturacion": {
                "tipo": fact.tipo_comprobante,
                "numero": fact.numero_comprobante,
                "fecha_emision": timezone.localtime(fact.fecha_emision).strftime("%Y-%m-%d %H:%M") if fact.fecha_emision else None,
                "registrado_por": getattr(fact.registrado_por, "username", None),
            },
        }
    )


# ---------------------------------------------------------
# CREAR FACTURACIÓN (FIX: JSON + numero libre + fecha flexible)
# ---------------------------------------------------------
@login_required
@require_POST
@transaction.atomic
def crear_facturacion(request, venta_id):
    user = request.user
    qs = _aplicar_scope_facturacion_por_pais(
        _base_qs_facturacion().filter(is_facturada=False).select_for_update(),
        user,
    )

    venta = qs.filter(pk=venta_id).first()
    if not venta:
        return JsonResponse({"ok": False, "error": "Venta no disponible para facturación."}, status=404)

    data = _get_payload(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "JSON inválido."}, status=400)

    tipo = (data.get("tipo_comprobante") or "").strip().upper()
    if tipo not in dict(Facturacion.TIPO_COMPROBANTE_CHOICES):
        return JsonResponse({"ok": False, "error": "Tipo de comprobante inválido."}, status=400)

    # ✅ libre: letras/números/guiones/etc (solo requerido)
    numero_comprobante = (data.get("numero_comprobante") or "").strip()
    if not numero_comprobante:
        return JsonResponse({"ok": False, "error": "El número de comprobante es obligatorio."}, status=400)

    # ✅ fecha flexible: "YYYY-MM-DD" o "YYYY-MM-DDTHH:MM"
    fecha_emision_str = (data.get("fecha_emision") or "").strip()
    fecha_emision = None
    if fecha_emision_str:
        try:
            if "T" in fecha_emision_str:
                dt = timezone.datetime.strptime(fecha_emision_str, "%Y-%m-%dT%H:%M")
            else:
                dt = timezone.datetime.strptime(fecha_emision_str, "%Y-%m-%d")
            fecha_emision = timezone.make_aware(dt, timezone.get_current_timezone())
        except ValueError:
            return JsonResponse({"ok": False, "error": "Fecha de emisión inválida."}, status=400)

    try:
        fact = Facturacion.objects.create(
            venta=venta,
            tipo_comprobante=tipo,
            numero_comprobante=numero_comprobante,
            fecha_emision=fecha_emision or timezone.now(),
            registrado_por=user,
        )
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "Esta venta ya fue facturada."}, status=404)

    return JsonResponse({
        "ok": True,
        "message": "Facturación registrada correctamente.",
        "facturacion": {
            "id": fact.id,
            "tipo": fact.tipo_comprobante,
            "numero_comprobante": fact.numero_comprobante,
            "fecha_emision": timezone.localtime(fact.fecha_emision).strftime("%Y-%m-%d %H:%M"),
        },
    })



# =========================================================
# HELPERS: Cliente fields
# =========================================================
def _cliente_has_field(field_name: str) -> bool:
    """
    Retorna True si el modelo Cliente tiene ese campo.
    Evita FieldError cuando intentas filtrar por campos que no existen.
    """
    try:
        Cliente._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False


def _get_cliente_nombre_apellido(cliente) -> tuple[str, str]:
    """
    Devuelve (nombre, apellido) soportando ambos esquemas:
    - nombre/apellido
    - nombre_cliente/apellido_cliente
    """
    if not cliente:
        return "", ""

    nombre = (getattr(cliente, "nombre", None) or getattr(cliente, "nombre_cliente", None) or "").strip()
    apellido = (getattr(cliente, "apellido", None) or getattr(cliente, "apellido_cliente", None) or "").strip()
    return nombre, apellido


# =========================================================
# ✅ NUEVO HELPER: Solo ventas pagadas + confirmadas por Tesorería
# =========================================================
def _solo_ventas_pagadas_y_confirmadas_por_tesoreria(qs):
    """
    Solo certificados cuyo detalle_venta pertenece a una venta:
    - Venta.estado = 1 (Pagado)
    - No existan cuotas "abiertas" (pendiente/vencida/etc.)
      (se permiten pagado, reintento, retirados, anulada, reembolso)
    - No existan pagos NO confirmados por Tesorería:
        pago.estado != 2 OR pago.confirmado_por IS NULL
    """

    # ✅ Estados de cuota que NO deben bloquear (se consideran "cerrados")
    CUOTA_ESTADOS_NO_BLOQUEAN = (
        {CUOTA_ESTADO_PAGADO, CUOTA_ESTADO_REINTENTO}
        | set(CUOTA_ESTADOS_RETIRADOS)
        | set(CUOTA_ESTADOS_BLOQUEADOS)
    )

    cuotas_abiertas_sq = (
        Cuota.objects
        .filter(venta_id=OuterRef("detalle_venta__venta_id"))
        .exclude(estado__in=CUOTA_ESTADOS_NO_BLOQUEAN)
    )

    pagos_no_confirmados_sq = (
        Pago.objects
        .filter(cuota__venta_id=OuterRef("detalle_venta__venta_id"))
        .filter(~Q(estado=2) | Q(confirmado_por__isnull=True))
    )

    return (
        qs.annotate(
            has_cuotas_abiertas=Exists(cuotas_abiertas_sq),
            has_pagos_no_confirmados=Exists(pagos_no_confirmados_sq),
        )
        .filter(
            detalle_venta__venta__estado=VENTA_ESTADO_PAGADO,  # 1
            has_cuotas_abiertas=False,
            has_pagos_no_confirmados=False,
        )
    )



# =========================================================
# LISTA CERTIFICADOS SAN MARCOS
# =========================================================
@login_required
def lista_certificados_san_marcos(request):
    """
    Lista SOLO certificados tipo SAN_MARCOS con:
    - Cliente (y opcional correo/teléfono preferidos)
    - Venta
    - Curso al que pertenece el certificado (CertificadoCurso.curso)
    - Producto comprado (DetalleVenta.producto)

    ✅ SOLO ventas pagadas y confirmadas por Tesorería
    """
    qs = (
        CertificadoCurso.objects
        .filter(tipo_certificado="SAN_MARCOS")
        .select_related(
            "curso",
            "detalle_venta__producto",
            "detalle_venta__venta__cliente",
            "detalle_venta__venta__usuario",
            "detalle_venta__venta__moneda",
            "detalle_venta__venta__pais",
            "detalle_venta__venta__local",
            "detalle_venta__venta__ubicacion",
        )
        .order_by("-detalle_venta__venta__fecha_venta")
    )

    # ✅ Aplicar filtro PAGADO + CONFIRMADO (antes de filtros UI)
    qs = _solo_ventas_pagadas_y_confirmadas_por_tesoreria(qs)

    q = (request.GET.get("q") or "").strip()
    folio = (request.GET.get("folio") or "").strip()
    # ⚠️ Estado ya no aplica realmente, porque ya filtramos a pagado/confirmado
    # Lo dejamos por compat, pero lo forzamos a "" para que no te “mate” resultados.
    estado = ""  # (request.GET.get("estado") or "").strip()
    fecha_ini = parse_date(request.GET.get("fecha_ini") or "")
    fecha_fin = parse_date(request.GET.get("fecha_fin") or "")

    # ✅ Filtro robusto: solo agrega campos existentes
    if q:
        q_obj = (
            Q(detalle_venta__venta__cliente__codigo_cliente__icontains=q)
            | Q(detalle_venta__venta__cliente__dni__icontains=q)
        )

        if _cliente_has_field("nombre"):
            q_obj |= Q(detalle_venta__venta__cliente__nombre__icontains=q)
        if _cliente_has_field("apellido"):
            q_obj |= Q(detalle_venta__venta__cliente__apellido__icontains=q)
        if _cliente_has_field("nombre_cliente"):
            q_obj |= Q(detalle_venta__venta__cliente__nombre_cliente__icontains=q)
        if _cliente_has_field("apellido_cliente"):
            q_obj |= Q(detalle_venta__venta__cliente__apellido_cliente__icontains=q)

        qs = qs.filter(q_obj)

    if folio:
        qs = qs.filter(detalle_venta__venta__folio_venta__icontains=folio)

    # (estado eliminado a propósito)

    if fecha_ini:
        qs = qs.filter(detalle_venta__venta__fecha_venta__date__gte=fecha_ini)
    if fecha_fin:
        qs = qs.filter(detalle_venta__venta__fecha_venta__date__lte=fecha_fin)

    try:
        per_page = int(request.GET.get("per_page", 50))
    except (TypeError, ValueError):
        per_page = 50
    per_page = max(1, min(per_page, 200))

    page = request.GET.get("page", 1)
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(page)

    items = list(page_obj.object_list)
    for c in items:
        venta = c.detalle_venta.venta
        cliente = venta.cliente if venta else None

        # ✅ Inyecta campos seguros para template
        nombre, apellido = _get_cliente_nombre_apellido(cliente)
        c.cliente_nombre = nombre
        c.cliente_apellido = apellido

        c.cliente_telefono = _pick_cliente_telefono(cliente.id) if cliente else None
        c.cliente_correo = _pick_cliente_correo(cliente.id) if cliente else None

    query_params = request.GET.copy()
    query_params.pop("page", None)
    # Como "estado" ya no se usa, lo removemos si venía
    query_params.pop("estado", None)

    return render(
        request,
        "sales/certificados_san_marcos.html",
        {
            "certificados": items,
            "page_obj": page_obj,
            "paginator": paginator,
            "querystring": query_params.urlencode(),
            "filtros": {
                "q": q,
                "folio": folio,
                "estado": "",  # fijo
                "fecha_ini": request.GET.get("fecha_ini", ""),
                "fecha_fin": request.GET.get("fecha_fin", ""),
                "per_page": per_page,
            },
        },
    )


# =========================================================
# FILTROS COMUNES (LISTA + EXPORT)
# =========================================================
def _filtrar_certificados_sm_common(qs, params):
    """
    Helper para centralizar filtros: listado y export usan lo mismo.
    ✅ Evita reventar si Cliente no tiene nombre_cliente/apellido_cliente.
    ✅ OJO: NO filtramos por estado, porque aquí solo entran pagadas/confirmadas.
    """
    q = (params.get("q") or "").strip()
    folio = (params.get("folio") or "").strip()
    fecha_ini = parse_date((params.get("fecha_ini") or "").strip())
    fecha_fin = parse_date((params.get("fecha_fin") or "").strip())

    if q:
        q_obj = (
            Q(detalle_venta__venta__cliente__codigo_cliente__icontains=q)
            | Q(detalle_venta__venta__cliente__dni__icontains=q)
        )

        if _cliente_has_field("nombre"):
            q_obj |= Q(detalle_venta__venta__cliente__nombre__icontains=q)
        if _cliente_has_field("apellido"):
            q_obj |= Q(detalle_venta__venta__cliente__apellido__icontains=q)
        if _cliente_has_field("nombre_cliente"):
            q_obj |= Q(detalle_venta__venta__cliente__nombre_cliente__icontains=q)
        if _cliente_has_field("apellido_cliente"):
            q_obj |= Q(detalle_venta__venta__cliente__apellido_cliente__icontains=q)

        qs = qs.filter(q_obj)

    if folio:
        qs = qs.filter(detalle_venta__venta__folio_venta__icontains=folio)

    if fecha_ini:
        qs = qs.filter(detalle_venta__venta__fecha_venta__date__gte=fecha_ini)
    if fecha_fin:
        qs = qs.filter(detalle_venta__venta__fecha_venta__date__lte=fecha_fin)

    return qs


# =========================================================
# EXPORT CERTIFICADOS SAN MARCOS
# =========================================================
@login_required
def export_certificados_san_marcos(request):
    """
    ✅ Export alineado al LISTADO:
    - Usa CertificadoCurso (tipo_certificado='SAN_MARCOS')
    - Calcula Correo/Teléfono con helpers
    ✅ SOLO ventas pagadas y confirmadas por Tesorería
    """
    fmt = request.GET.get("format", "xlsx")  # format=xlsx|csv

    qs = (
        CertificadoCurso.objects
        .filter(tipo_certificado="SAN_MARCOS")
        .select_related(
            "curso",
            "detalle_venta",
            "detalle_venta__producto",
            "detalle_venta__venta",
            "detalle_venta__venta__cliente",
            "detalle_venta__venta__moneda",
        )
        .order_by("-detalle_venta__venta__fecha_venta")
    )

    # ✅ Aplicar filtro PAGADO + CONFIRMADO
    qs = _solo_ventas_pagadas_y_confirmadas_por_tesoreria(qs)

    # ✅ filtros UI comunes
    qs = _filtrar_certificados_sm_common(qs, request.GET)

    rows = []
    for c in qs:
        dv = c.detalle_venta
        v = dv.venta if dv else None
        cl = v.cliente if v else None

        nombre, apellido = _get_cliente_nombre_apellido(cl)

        correo = _pick_cliente_correo(cl.id) if cl else None
        telefono = _pick_cliente_telefono(cl.id) if cl else None

        rows.append(
            {
                "Folio": getattr(v, "folio_venta", "") if v else "",
                "Cliente": f"{nombre} {apellido}".strip(),
                "Codigo Cliente": getattr(cl, "codigo_cliente", "") if cl else "",
                "DNI": getattr(cl, "dni", "") if cl else "",
                "Correo": correo or "",
                "Telefono": telefono or "",
                "Curso (certificado)": getattr(c.curso, "nombre_producto", "") if c.curso_id else "",
                "Producto comprado": getattr(dv.producto, "nombre_producto", "") if dv and dv.producto_id else "",
                "Cant.": getattr(dv, "cantidad", "") if dv else "",
                "Moneda": getattr(v.moneda, "nombre", "") if v and getattr(v, "moneda_id", None) else "",
                "Monto Venta": float(getattr(v, "monto_total", 0) or 0) if v else 0,
                "Estado": v.get_estado_display() if v and hasattr(v, "get_estado_display") else (getattr(v, "estado", "") if v else ""),
                "Fecha Venta": v.fecha_venta.strftime("%d/%m/%Y %H:%M") if v and v.fecha_venta else "",
            }
        )

    if not rows:
        rows.append(
            {
                "Folio": "",
                "Cliente": "",
                "Codigo Cliente": "",
                "DNI": "",
                "Correo": "",
                "Telefono": "",
                "Curso (certificado)": "",
                "Producto comprado": "",
                "Cant.": "",
                "Moneda": "",
                "Monto Venta": 0,
                "Estado": "",
                "Fecha Venta": "",
            }
        )

    return export_as_response(rows, filename="certificados_san_marcos", fmt=fmt)
