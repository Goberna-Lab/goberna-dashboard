from django.conf import settings
from django.db import models
from urllib.parse import urljoin

class PerfilUsuario(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="perfil_satelite")
    foto = models.ImageField(upload_to="profile_photos/", null=True, blank=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "tb_perfil_usuario"

class Moneda(models.Model):
    id = models.AutoField(primary_key=True, db_column='codigo_moneda')
    nombre = models.CharField(max_length=100, db_column='nombre_moneda')
    radioDivisor = models.DecimalField(
        max_digits=12, decimal_places=6,
        db_column='radio_divisor', null=True, blank=True
    )
    radioMultiplicador = models.DecimalField(
        max_digits=12, decimal_places=6,
        db_column='radio_multiplicador', null=True, blank=True
    )

    class Meta:
        managed = False
        db_table = 'tb_moneda'


class Pais(models.Model):
    id = models.AutoField(primary_key=True, db_column='id_pais')
    nombre = models.CharField(max_length=100, db_column='nombre_pais', unique=True)

    class Meta:
        managed = False
        db_table = 'tb_pais'


class Cliente(models.Model):
    id = models.AutoField(primary_key=True, db_column='id_cliente')
    pais = models.ForeignKey(
        Pais, on_delete=models.DO_NOTHING,
        db_column='id_pais', related_name='clientes_satelite'
    )

    class Meta:
        managed = False
        db_table = 'tb_cliente'

class Venta(models.Model):
    id = models.AutoField(primary_key=True, db_column='codigo_venta')
    cliente = models.ForeignKey(
        Cliente, on_delete=models.DO_NOTHING,
        db_column='codigo_cliente', related_name='ventas_satelite',
        null=True, blank=True
    )
    
    # Relación con User (auth_user existe en la db remota)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING,
        db_column='codigo_usuario', related_name='ventas_satelite'
    )
    
    moneda = models.ForeignKey(
        Moneda, on_delete=models.DO_NOTHING,
        db_column='codigo_moneda', related_name='ventas'
    )

    folio_venta = models.CharField(max_length=20, db_column='folio_venta', unique=True)
    medio = models.CharField(max_length=25, db_column='medio_venta', null=True, blank=True)
    monto_total = models.DecimalField(max_digits=12, decimal_places=2, db_column='monto_total', default=0)
    
    ESTADO_CHOICES = [
        (1, 'Pagado'),
        (2, 'Pendiente'),
        (3, 'No Validado'),
        (4, 'Anulado'),
        (5, 'Cotización'),
        (6, 'Preventa'),
        (7, 'Retirado'),
    ]
    estado = models.IntegerField(choices=ESTADO_CHOICES, default=2, db_column='estado')

    radio_divisor_usado = models.DecimalField(
        max_digits=12, decimal_places=6,
        null=True, blank=True, db_column='radio_divisor_usado'
    )
    radio_multiplicador_usado = models.DecimalField(
        max_digits=12, decimal_places=6,
        null=True, blank=True, db_column='radio_multiplicador_usado'
    )

    pais = models.ForeignKey(
        Pais, on_delete=models.DO_NOTHING,
        db_column='codigo_pais', related_name='ventas_satelite',
        null=True, blank=True
    )

    fecha_venta = models.DateTimeField(db_column='fecha_venta')
    fecha_registro = models.DateTimeField(db_column='fecha_registro', null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'tb_venta'

class Cuota(models.Model):
    id = models.AutoField(primary_key=True, db_column='codigo_cuota')
    venta = models.ForeignKey(
        Venta, on_delete=models.DO_NOTHING,
        db_column='codigo_venta', related_name='cuotas'
    )
    numero_cuota = models.PositiveIntegerField(db_column='numero_cuotas')
    monto_total = models.DecimalField(max_digits=12, decimal_places=2, db_column='monto_total')
    
    ESTADO_CHOICES = [
        (1, 'Pagado'),
        (2, 'Pendiente'),
        (3, 'Vencida'),
        (4, 'Reintento solicitado'),
        (5, 'Retirada'),
    ]
    estado = models.IntegerField(choices=ESTADO_CHOICES, default=2, db_column='estado')
    
    fecha_registro = models.DateField(db_column='fecha_registro')
    fecha_vencimiento = models.DateField(db_column='fecha_vencimiento')

    class Meta:
        managed = False
        db_table = 'tb_cuotas'


class Pago(models.Model):
    id = models.AutoField(primary_key=True, db_column='codigo_pago')
    cuota = models.ForeignKey(
        Cuota, on_delete=models.DO_NOTHING,
        db_column='codigo_cuota', related_name='pagos_satelite'
    )
    estado = models.IntegerField(db_column='estado')
    confirmado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_column='usuario_confirmacion',
        null=True,
        blank=True,
        related_name='pagos_confirmados_satelite',
    )
    fecha_confirmacion = models.DateTimeField(
        db_column='fecha_confirmacion',
        null=True,
        blank=True,
    )

    class Meta:
        managed = False
        db_table = 'tb_pago'

class Categoria(models.Model):
    codigo_categoria = models.AutoField(primary_key=True)
    nombre_categoria = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = 'tb_categoria'

class Negocio(models.Model):
    codigo_negocio = models.AutoField(primary_key=True)
    nombre_negocio = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = 'tb_negocio'

class Division(models.Model):
    codigo_division = models.AutoField(primary_key=True)
    nombre_division = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = 'tb_division'

class Producto(models.Model):
    ESTADO_CHOICES = (
        (1, "Disponible"),
        (2, "No Disponible"),
    )

    codigo_producto = models.AutoField(primary_key=True)
    sku_producto = models.CharField(max_length=50, unique=True)
    nombre_producto = models.CharField(max_length=200)
    precio_normal = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    precio_promocion = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # En la BD principal esta columna guarda la ruta, por ejemplo: productos/default.png
    imagen_producto = models.CharField(max_length=255, null=True, blank=True, db_column='imagen_producto')
    estado = models.IntegerField(choices=ESTADO_CHOICES, default=1, db_column='estado')
    
    codigo_categoria = models.ForeignKey(
        Categoria, on_delete=models.DO_NOTHING,
        db_column='codigo_categoria', related_name='productos'
    )
    codigo_negocio = models.ForeignKey(
        Negocio, on_delete=models.DO_NOTHING,
        db_column='codigo_negocio', related_name='productos'
    )
    codigo_division = models.ForeignKey(
        Division, on_delete=models.DO_NOTHING,
        db_column='codigo_division', related_name='productos',
        null=True, blank=True
    )

    fecha_registro = models.DateField(null=True, blank=True, db_column='fecha_registro')
    fecha_edicion = models.DateTimeField(null=True, blank=True, db_column='fecha_edicion')

    def __str__(self):
        return f"{self.sku_producto} - {self.nombre_producto}"

    @property
    def imagen_producto_url(self):
        ruta = (self.imagen_producto or "").strip()
        if not ruta:
            ruta = "productos/default.png"

        if ruta.startswith(("http://", "https://")):
            return ruta

        main_app_url = getattr(settings, "MAIN_APP_URL", "https://app.goberna.pe").rstrip("/") + "/"
        media_url = getattr(settings, "MEDIA_URL", "/media/")

        # Si viene como /media/... se respeta esa ruta en el dominio principal
        if ruta.startswith("/"):
            return urljoin(main_app_url, ruta.lstrip("/"))

        if isinstance(media_url, str) and media_url.startswith(("http://", "https://")):
            media_base = media_url if media_url.endswith("/") else f"{media_url}/"
        else:
            media_path = media_url or "/media/"
            if not media_path.startswith("/"):
                media_path = f"/{media_path}"
            media_base = f"{main_app_url.rstrip('/')}{media_path}"
            if not media_base.endswith("/"):
                media_base = f"{media_base}/"

        return urljoin(media_base, ruta.lstrip("/"))

    class Meta:
        managed = False
        db_table = 'tb_producto'

class DetalleVenta(models.Model):
    id = models.AutoField(primary_key=True, db_column='codigo_detalle')
    
    venta = models.ForeignKey(
        Venta, on_delete=models.DO_NOTHING,
        db_column='codigo_venta', related_name='detalles'
    )
    
    producto = models.ForeignKey(
        Producto, on_delete=models.DO_NOTHING,
        db_column='codigo_producto', related_name='detalles_venta'
    )
    
    cantidad = models.PositiveIntegerField(db_column='cantidad', default=1)
    precio_total = models.DecimalField(max_digits=12, decimal_places=2, db_column='precio_total')

    class Meta:
        managed = False
        db_table = 'tb_detalleVenta'


class LibroEnPack(models.Model):
    id = models.AutoField(primary_key=True, db_column='codigo_libro_en_pack')

    detalle_venta = models.ForeignKey(
        DetalleVenta,
        on_delete=models.DO_NOTHING,
        db_column='codigo_detalle_venta',
        related_name='libros_en_pack',
    )

    libro = models.ForeignKey(
        Producto,
        on_delete=models.DO_NOTHING,
        db_column='codigo_libro',
        related_name='pack_items_satelite',
    )

    cantidad = models.PositiveIntegerField(db_column='cantidad', default=1)
    precio_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        db_column='precio_unitario',
        default=0,
    )

    class Meta:
        managed = False
        db_table = 'tb_libros_en_pack'
