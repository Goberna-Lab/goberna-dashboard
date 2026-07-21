# Vinculación de campañas Meta Ads a varios productos (pack)

Rama: `feature/meta-campaign-multi-product` (no mergeada, no pusheada — nada de esto está en producción todavía).

## Por qué se hizo esto

El sistema original (`tb_meta_campaign_map`) fue diseñado asumiendo **una campaña de Meta → un producto**. Eso cubre el ~99% de los casos, pero se rompía con campañas tipo "pack" que promocionan **más de un producto al mismo tiempo** en el mismo anuncio — el caso real que lo disparó fue `[JUL] PACK 360 | WSP` (3 campañas, una por país: Ecuador, México, Perú), que vende **Consultor Político 360** y **Contrainteligencia 360** juntos.

Con el sistema viejo, vincular esa campaña obligaba a elegir un solo producto, dejando al otro sin ningún gasto de Meta asociado — su ROAS se veía artificialmente perfecto (sin costo de adquisición) mientras el otro producto cargaba con el gasto de ambos.

## Qué se construyó

### 1. Tabla nueva: `tb_meta_campaign_product_weight`

Satélite **opcional**, no reemplaza nada:

```sql
CREATE TABLE tb_meta_campaign_product_weight (
  id INT AUTO_INCREMENT PRIMARY KEY,
  campaign_id VARCHAR(32) NOT NULL,
  codigo_producto INT NOT NULL,
  weight_pct DECIMAL(5,2) NOT NULL DEFAULT 100.00,
  linked_by VARCHAR(10) NOT NULL DEFAULT 'manual',
  linked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_campaign_producto (campaign_id, codigo_producto)
);
```

- Una campaña con **un solo producto NUNCA tiene fila acá** — sigue resolviendo por `tb_meta_campaign_map.codigo_producto` exactamente como antes. Cero cambio de comportamiento para las ~800 campañas ya vinculadas.
- Solo las campañas vinculadas explícitamente a 2+ productos (vía el flujo nuevo) tienen filas acá, una por producto.
- `weight_pct` es la **atribución de cada producto sobre el gasto de la campaña, de forma independiente** — no es un pool que deba sumar 100.

### 2. Decisión de negocio clave: NO se reparte el gasto

Primer diseño: repartir 50/50 (o por peso custom) entre los productos de un pack. **El usuario lo rechazó** después de verlo funcionando: quería que cada producto muestre el **gasto completo** de la campaña, no una porción — el costo de armar y correr el pack cuenta íntegro para cada producto que vende, no se divide entre ellos.

Resultado: `weight_pct = 100` para cada producto vinculado a un pack (no `100/N`). Efecto: si sumás el gasto de todos los productos de un negocio, el total puede superar el gasto real de Meta para campañas pack (se cuenta completo más de una vez). Es un trade-off aceptado explícitamente por el usuario.

### 3. Cálculo de ROAS (`core/views.py`)

`_roas_por_producto` y `_roas_por_producto_pais`:
- Si una campaña tiene filas en `tb_meta_campaign_product_weight`, el gasto de cada fila de insights se multiplica por `weight_pct/100` **por cada producto vinculado**, sumando al mismo `spend_by_codigo[codigo]` que usan las campañas 1:1 normales — así el gasto de un producto en campañas propias + su parte de un pack cae en el mismo balde, sin doble contabilización accidental.
- Si no hay filas (caso normal), el comportamiento es exactamente el de antes (`cmap.get(campaign_id)` → fallback por nombre exacto → "Sin producto vinculado").
- Las **ventas nunca se dividen** — cada producto cuenta sus ventas reales completas, siempre.

### 4. UI — decisión de diseño (2 iteraciones)

**Intento 1 (descartado):** un botón/panel nuevo "¿Es un pack de varios productos?" por grupo, con filas de producto+peso para completar a mano. El usuario lo rechazó: "no quiero más botones, quiero usar el mismo dropdown".

**Diseño final:** se reutiliza el combo existente "Elegí un producto..." (`core/templates/vincular.html`), que ya mostraba un checkbox junto a cada opción pero solo permitía elegir uno. Se convirtió en multi-select real:
- 0 o 1 producto tildado → comportamiento **idéntico** al de siempre.
- 2+ tildados → el mismo botón "Vincular"/"Vincular las N" que ya está en pantalla dispara el endpoint nuevo (`multi=1`) en vez del de un solo producto, con `weight_pct=100` para cada uno.
- Feedback visual agregado (después de que el usuario reportó que la interacción no se entendía): chips "Tildados (N)" dentro del panel, borde dorado en el combo cerrado ("modo pack"), texto fijo junto al botón explicando qué va a pasar.

### 5. Endpoint (`/ads/vincular/`, `core/views.py::ads_vincular`)

Rama nueva activada por `request.POST.get("multi") == "1"` — **no toca** los flujos `bulk` (grupo → 1 producto) ni `single` (fila individual → 1 producto) que ya existían. Valida que cada peso esté en `(0, 100]`, que los productos existan en catálogo, que las campañas existan en `tb_meta_ads`. Escribe en transacción atómica: reemplaza las filas de `tb_meta_campaign_product_weight` para esas campañas, upsert de `tb_meta_campaign_map` (producto de mayor peso como representativo, para que el resto del sistema que lee un solo `codigo_producto` siga funcionando), y actualiza `tb_meta_ads.product/category` con el nombre combinado ("Producto A / Producto B") para que se vea bien en la tabla de campañas.

`/ads/desvincular/` también se actualizó para borrar las filas de peso al desvincular (si no, una campaña "desvinculada" seguiría repartiendo gasto según pesos viejos).

### 6. Trazabilidad en "Pautas y Ventas" (`core/templates/pautas_cursos.html`)

- El modal "Ver campañas" (por fila país+producto) ahora muestra, cuando aplica, **"gasto completo (pack compartido)"** debajo del monto — para que quede claro que ese importe también está atribuido a otro producto, no es un gasto separado.
- Se probó (y se revirtió después a pedido del usuario, ver commit `961868b`) un badge "Pack junto con: X" en el encabezado de cada grupo de producto — el usuario pidió sacarlo mientras se resolvía otra prioridad (traer datos reales a la DB local); queda como mejora pendiente si se quiere retomar.

## Entorno de prueba local

- Servidor Django local: `http://127.0.0.1:8001` (venv en `.venv/`, `DEBUG=True` en `.env` local — bypassea login para requests desde localhost).
- DB: MariaDB en Docker `cerberus-local-db`, puerto host `3309`, base `goberna_app_local`.
- Todo se probó ahí antes de cualquier commit — nunca se escribió en la base de producción durante el desarrollo de esta funcionalidad.
- Se importó un espejo real de producción a esta DB local (tablas de Meta Ads completas + catálogo de productos + ventas/clientes/cuotas/pagos, sin contraseñas de staff) vía el cliente `mysql` interactivo por SSH restringido (no hay `mysqldump` ni `scp` habilitados en el servidor) — export en formato JSON por bloques de ~1000 filas para evitar que las tablas ASCII de la terminal corten texto largo.

## Qué falta para llevar esto a producción

1. Correr `ensure_campaign_weight_schema()` contra la DB de producción (aditivo, `CREATE TABLE IF NOT EXISTS`, no toca nada existente) — requiere confirmación explícita antes de ejecutar.
2. Deploy del código (`core/models.py`, `core/views.py`, `core/management/commands/sync_meta_ads.py`, `core/templates/vincular.html`, `core/templates/pautas_cursos.html`).
3. Vincular en producción las 3 campañas reales `[JUL] PACK 360 | WSP` a Consultor Político 360 + Contrainteligencia 360, y verificar que el ROAS se vea razonable antes de anunciarlo al equipo.
4. (Opcional, pendiente) revisar si se quiere reintroducir el badge "Pack junto con" en el encabezado de grupo.
