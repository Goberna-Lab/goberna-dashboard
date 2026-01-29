from django.db import models
from django.conf import settings

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

class Venta(models.Model):
    id = models.AutoField(primary_key=True, db_column='codigo_venta')
    
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
    monto_total = models.DecimalField(max_digits=12, decimal_places=2, db_column='monto_total', default=0)
    
    ESTADO_CHOICES = [
        (1, 'Pagado'),
        (2, 'Pendiente'),
        (3, 'No Validado'),
        (4, 'Anulado'),
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

    fecha_venta = models.DateTimeField(db_column='fecha_venta')

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
    ]
    estado = models.IntegerField(choices=ESTADO_CHOICES, default=2, db_column='estado')
    
    fecha_registro = models.DateField(db_column='fecha_registro')
    fecha_vencimiento = models.DateField(db_column='fecha_vencimiento')

    class Meta:
        managed = False
        db_table = 'tb_cuotas'
