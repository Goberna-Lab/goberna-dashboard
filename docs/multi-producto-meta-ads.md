# Vinculación de campañas Meta Ads a varios productos (pack)

Rama: `feature/meta-campaign-multi-product` (commiteada localmente, no mergeada ni pusheada a `main` todavía — nada de esto está en producción salvo la tabla de la sección 1).

## Por qué se hizo esto

El sistema original (`tb_meta_campaign_map`) fue diseñado asumiendo **una campaña de Meta → un producto**. Eso cubre el ~99% de los casos, pero se rompía con campañas que promocionan **más de un producto al mismo tiempo** en el mismo anuncio — el caso real que lo disparó fue `[JUL] PACK 360 | WSP` (3 campañas, una por país: Ecuador, México, Perú), que vende **Consultor Político 360** y **Contrainteligencia 360** juntos.

Nota de terminología: evitar la palabra "pack" en cualquier texto visible al usuario (banners, badges, tooltips) — el usuario prefiere describirlo simplemente como "campaña vinculada a varios productos". Ya se limpió del frontend (ver sección 8); quedan nombres internos técnicos (`_resolver_representante_pack`, CSS `.group-pack`, variable `isPack`) que no se ven en pantalla, sin renombrar por ahora (bajo impacto, se puede hacer después si se quiere consistencia total).

## Qué se construyó (resumen de todas las sesiones)

### 1. Tabla nueva: `tb_meta_campaign_product_weight`

Satélite **opcional**, no reemplaza nada. DDL exacto (usado también por `ensure_campaign_weight_schema()`):

```sql
CREATE TABLE IF NOT EXISTS tb_meta_campaign_product_weight (
  id INT AUTO_INCREMENT PRIMARY KEY,
  campaign_id VARCHAR(32) NOT NULL,
  codigo_producto INT NOT NULL,
  weight_pct DECIMAL(5,2) NOT NULL DEFAULT 100.00,
  linked_by VARCHAR(10) NOT NULL DEFAULT 'manual',
  linked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_campaign_producto (campaign_id, codigo_producto),
  KEY idx_cpw_campaign (campaign_id),
  KEY idx_cpw_producto (codigo_producto)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
```

- Una campaña con **un solo producto NUNCA tiene fila acá** — sigue resolviendo por `tb_meta_campaign_map.codigo_producto` exactamente como antes.
- Solo las campañas vinculadas explícitamente a 2+ productos tienen filas acá, una por producto.
- `weight_pct` es la atribución de cada producto sobre el gasto, **de forma independiente** — no es un pool que deba sumar 100.

**✅ Esta tabla ya existe en producción** (`goberna_app`, creada el 2026-07-21 vía SSH — `sudo cerberus-ctl.sh db` contra `75.119.138.200`, mismo DDL de arriba, verificado con `SHOW CREATE TABLE`). Vacía por ahora, esperando el deploy del código y la vinculación real de campañas (ver sección "Qué falta").

### 2. Decisión de negocio clave: NO se reparte el gasto

Cada producto vinculado a un pack recibe `weight_pct = 100` (no `100/N`) — cada uno muestra el **gasto completo** de la campaña. Si sumás el gasto de todos los productos de un negocio, el total puede superar el gasto real de Meta para campañas con 2+ productos (se cuenta completo más de una vez). Es un trade-off aceptado explícitamente por el usuario. **Verificado matemáticamente en esta sesión** (ver sección 9) — funciona exactamente como se diseñó.

### 3. Cálculo de ROAS (`core/views.py`)

`_roas_por_producto` y `_roas_por_producto_pais`: si una campaña tiene filas en `tb_meta_campaign_product_weight`, el gasto de cada fila de insights se multiplica por `weight_pct/100` por cada producto vinculado. Si no hay filas (caso normal), comportamiento idéntico al de antes. **Las ventas nunca se dividen** — cada producto cuenta sus ventas reales completas, siempre (nunca hay doble conteo de ventas, solo de gasto, y solo quand corresponde).

### 4. UI de vinculación (`core/templates/vincular.html`)

Se reutiliza el combo existente "Elegí un producto..." como multi-select real: 0-1 producto tildado → comportamiento idéntico al de siempre; 2+ tildados → dispara el endpoint `multi=1`. Texto de ayuda ("Campaña vinculada a varios productos: cada producto tildado se atribuye el gasto completo...") ya sin la palabra "pack".

### 5. Endpoints (`core/views.py`, dentro de `ads_vincular` salvo el último)

- **`single`** (1 campaña, 1 producto) y **`bulk`** (N campañas, 1 producto): flujos originales. **Fix aplicado esta sesión**: ahora también borran filas viejas de `MetaCampaignProductWeight` al re-vincular — antes, si una campaña pack se re-vinculaba a 1 solo producto por este camino, quedaban filas huérfanas que el cálculo de ROAS seguía leyendo, ignorando el nuevo vínculo en silencio. Bug real, confirmado y corregido.
- **`multi`** (`request.POST.get("multi") == "1"`): N campañas → 2+ productos con peso. Transacción atómica: reemplaza filas de `MetaCampaignProductWeight`, upsert de `MetaCampaignMap` (producto de mayor peso como representante), actualiza `tb_meta_ads.product/category` con nombre combinado.
- **`/ads/desvincular/`** (`ads_desvincular_campana`): desvincula TODO (borra ambas tablas). Todo-o-nada.
- **`/ads/quitar-producto/`** (`ads_quitar_producto_campana`, **nuevo esta sesión**): quita UN producto puntual de un pack sin desvincular el resto. Si quedan 2+, recalcula representante/nombre combinado; si queda 1, colapsa a comportamiento 1:1 normal (borra la última fila de peso). Probado en vivo con el caso real PACK 360 — funciona correctamente, no afecta otras campañas del mismo pack. **Ya conectado a la UI** en `pautas_cursos.html`: cada badge de producto en una fila con 2+ productos vinculados tiene un "×" que llama a este endpoint (una vez por cada `campaign_id` real detrás de la fila), con confirmación previa.

Refactor de deduplicación: `_resolver_representante_pack(pares, productos_by_codigo)` (nueva función en `views.py`, cerca de `_negocio_to_ads_category`) — única fuente de la regla "quién es el representante + cómo se arma el nombre combinado", usada tanto por `multi` como por `quitar-producto`. Antes estaba duplicada en ambos lugares.

### 6. País de la campaña: Origen vs. Destino (descubierto y corregido esta sesión)

`tb_meta_ads` tiene DOS columnas de país independientes:
- **`country`**: país DESTINO real — viene del breakdown geográfico de la Meta Graph API (`sync_meta_ads.py`, `breakdowns: "country"`). A quién le mostró Meta el anuncio. **Nunca viene vacío** en los datos actuales.
- **`paid_country`**: país de ORIGEN — heurística local que adivina el país leyendo el *nombre de la cuenta publicitaria* (`paid_country_from_account_name()`, busca palabras/tokens como "PERU", "DOM", etc.). Frágil: ya causó un hueco de datos real para la cuenta "Rep. Dom." hasta que se parchó (commit `421cb72`).

**Antes**: `_roas_por_producto_pais` priorizaba `paid_country` (origen) sobre `country` (destino real) para agrupar inversión por país — mostraba, ej., "Perú" solo porque la cuenta se llamaba "Goberna Perú", aunque la campaña hubiera invertido/llegado a otros países.

**Ahora**: se invirtió la prioridad — `country` (destino real) primero, `paid_country` como fallback. Verificado que el total de inversión no cambia, solo se redistribuye correctamente (mismo monto exacto antes/después en rango de prueba). El país de origen no se perdió: se agrega como `cuenta_origen` dentro de cada entrada de `campanas[]`, mostrado en el modal "Ver campañas" solo cuando difiere del país destino de esa fila.

En `core/templates/ads.html` (tabla principal de Ads, columnas ya existían desde antes con esta misma distinción): renombradas **"País (pauta)" → "País Origen"** y **"País (cliente)" → "País Destino"** para que el nombre sea autoexplicativo.

### 7. Vista "Pautas y Ventas" (`core/templates/pautas_cursos.html`) — reestructuración completa esta sesión

Cambió de agrupar por **producto → país** a agrupar por **campaña → producto(s) vinculado(s)**, a pedido del usuario ("muy engorroso" ver todo por país sin saber qué campaña genera cada gasto). Iteraciones de diseño, todas aplicadas:

1. Grupos colapsados por default (`groupStartOpen: false`) — el header ya trae los subtotales completos (Inv./Ventas/Utilidad/Margen).
2. `columnCalcs: "table"` — elimina una 3ra repetición automática de subtotales que agregaba Tabulator por cada grupo (bug de configuración, no solo diseño).
3. Filtro nuevo por Campaña (`.multi-check`, mismo patrón visual que el filtro de Producto ya existente), combinable en AND, client-side.
4. KPI nueva: Margen % global (5ta tarjeta).
5. Paginación manual por campaña completa (`GROUP_PAGE_SIZE = 12`, nunca corta un grupo a la mitad — se evitó la paginación nativa de Tabulator por eso). Footer "TOTALES" ahora aclara "(esta página)" cuando hay más de una página; los KPI de arriba siguen mostrando el total general siempre.
6. Más aire visual entre headers de grupo (padding/margin, se sentía "muy apretado").
7. Se probó ocultar la flecha de expandir en grupos de 1 solo país (para evitar una fila de detalle 100% redundante con el header) — **el usuario pidió revertirlo**: todos los grupos muestran la flecha por igual ahora, sin excepción, por consistencia.
8. Badge "Pack N productos" en el header de grupo: **eliminado** (redundante con los badges de producto individuales que ya se muestran, y además usaba la palabra "pack" que el usuario pidió evitar).
9. Tooltip del modal "Ver campañas": "gasto completo (pack compartido)" → "gasto completo (campaña vinculada a varios productos)".

### 8. Terminología "pack" limpiada del frontend

A pedido explícito del usuario: el concepto correcto es "campaña vinculada a varios productos", no "pack". Ya renombrado en todo texto visible (badges, tooltips, hints de `vincular.html`, docstring de `ads_quitar_producto_campana`). Nombres internos técnicos sin renombrar (bajo impacto, no visible).

## Auditoría de casos borde (hecha esta sesión, por agente especializado)

Se revisaron sistemáticamente 8 escenarios del sistema de 2 tablas (`tb_meta_campaign_map` + `tb_meta_campaign_product_weight`):

| Caso | Resultado |
|---|---|
| Re-vincular pack→single/bulk deja filas huérfanas | **Bug real, confirmado y arreglado** (sección 5) |
| Re-vincular single→multi | Verificado seguro, sin cambios necesarios |
| Vincular la misma campaña 2 veces vía multi | Verificado seguro (dedupe + `UniqueConstraint`) |
| Desvincular una campaña sin filas de peso previas | Verificado seguro (`.delete()` sobre queryset vacío es no-op) |
| Concurrencia (dos usuarios tocando la misma campaña) | Riesgo bajo al volumen actual (~800 campañas 1:1 + 3 pack de prueba), no requiere fix ahora |
| Vistas que leen una tabla sin considerar la otra | Verificado consistente en los 2 puntos de cálculo de ROAS |
| Nombre combinado "A / B" quedando pegado tras re-vincular a 1 solo | Verificado que no ocurre (se sobreescribe correctamente) |
| `sync_meta_ads.py` pisando un vínculo manual/pack | Verificado seguro — nunca sobreescribe una entrada ya existente en `MetaCampaignMap` |

## ¿Conviene unificar en una sola tabla (eliminar `tb_meta_campaign_map`)?

Se evaluó explícitamente (a pedido del usuario) y **la recomendación fue NO, por ahora**. Razón principal: 5 de 6 puntos de lectura/escritura de `MetaCampaignMap` son triviales de migrar o hasta mejorarían, pero `sync_meta_ads.py` (el comando que auto-vincula ~800 campañas históricas por SKU/nombre) obligaría a duplicar en un segundo archivo la lógica de "representante de pack" que hoy vive solo en `views.py` — en el comando más sensible del sistema, a cambio de un beneficio modesto. Además, el "modelo mental" no se simplifica tanto: pasa de "1 fila garantizada" (99% de los casos) a "0/1/N filas siempre".

Lo único que se recomendó y ya se hizo: extraer la lógica de representante a `_resolver_representante_pack()` (sección 5) — prerequisito real si algún día se quiere unificar, sin tocar el esquema.

## Verificación matemática completa (esta sesión, caso real PACK 360)

Se verificó número por número, con datos reales de producción espejados en local:

- **Gasto real de las 3 campañas** (directo de `tb_meta_ads`): Ecuador $52,21 + Perú $54,15 + México $45,21 = **$151,57 total**.
- **En la vista**: cada uno de los 2 productos muestra ese gasto completo → suma $303,14 = $151,57 × 2. Exactamente el diseño esperado (atribución completa, no repartida).
- **Ventas**: se trazaron las 13 ventas reales línea por línea (fuente: `tb_venta`/`tb_detalleVenta`, conversión USD con tasa real de cada venta). Total calculado a mano ($2.516,29) coincide **exacto** con lo que devuelve la vista. Cada fila individual (por producto y país) verificada contra la venta real de origen — sin duplicados, sin pérdidas.

## Encoding corrupto en la DB local — arreglado esta sesión (no relacionado a la feature, pero bloqueaba verificar bien los datos)

Al revisar la vista se detectó que varios nombres con tildes se veían como "Per�", "Consultor Pol�tico 360", etc. Causa raíz: durante la importación manual del espejo local (cliente `mysql` interactivo por SSH restringido, sin `mysqldump`/`scp` disponibles — ver más abajo), los bytes de vocales acentuadas se reemplazaron de forma **irreversible** por el carácter Unicode de reemplazo (U+FFFD) en el momento de copiar/pegar por consola. **Confirmado que es un problema exclusivo del espejo local, no de producción** (se verificó contra producción vía SSH read-only: 0 filas corruptas ahí).

Reparado trayendo los valores correctos desde producción por SSH usando `TO_BASE64()` (ASCII puro, no se corrompe en tránsito) y aplicando los `UPDATE` solo en la DB local:

| Tabla.columna | Filas corregidas |
|---|---|
| `tb_pais.nombre_pais` | 7 (Canadá, España, Haití, México, Panamá, Perú, Rep. Dominicana) |
| `tb_producto.nombre_producto` | 204 |
| `tb_cliente.nombre_cliente` | 1247 |
| `tb_cliente.apellido_cliente` | 2087 |
| `tb_cliente.ocupacion_cliente` | 470 |
| `tb_pago.observacion` | 1713 |
| `tb_cliente.dni_cliente`, `tratamiento_cliente`, `tb_correo`, `tb_metodoPago`, `tb_ubicacion`, `tb_venta.observacion_cotizacion` | 13 (varias tablas chicas) |

Total ~4278 filas. Verificado 0 restantes en las 13 tablas/columnas afectadas. Efecto colateral positivo: arreglar `tb_pais` también resolvió un bug real donde Perú/México aparecían como DOS filas separadas por país en "Pautas y Ventas" (el nombre corrupto no hacía match con el limpio al agrupar).

## Entorno de prueba local

- Servidor Django local: `http://127.0.0.1:8001` (venv en `.venv/`, `DEBUG=True` en `.env` local — bypassea login para requests desde localhost). Arrancar con `python manage.py runserver 127.0.0.1:8001`.
- DB: MariaDB en Docker `cerberus-local-db`, puerto host `3309`, base `goberna_app_local`. Puede estar detenido entre sesiones — `docker start cerberus-local-db`, verificar con `mysqladmin ping`.
- Acceso SSH a producción (para consultas read-only o, con confirmación explícita, cambios): guía en `C:\Users\andre\Downloads\guia-conexion-andreecito-cerberus (1).md` — `ssh andreecito@75.119.138.200`, comandos permitidos vía `sudo cerberus-ctl.sh {restart|up|logs|shell|db}`. `sudo cerberus-ctl.sh db` abre cliente mysql interactivo conectado a `goberna_app` (producción). Restringido a IP de origen fija; solo auth por clave.
  - Limitación práctica encontrada: el paste directo al cliente mysql interactivo por SSH se trunca con queries largas (~2.5KB límite de línea del pty) — para IN-lists grandes, trocear en lotes de ~350 ids con `split -l 350`.
  - Otra guía SSH encontrada (`GUIA_ANDRECITO.md`) es de un proyecto DISTINTO ("Certificaciones Goberna", `gobernacertificate`, host `161.132.39.165`) — no confundir, no aplica a esta base de datos.
- Repo hermano `cerberusapp` (comparte la misma DB de producción `goberna_app`): `C:\Users\andre\OneDrive\Desktop\cerberusgobernaus\ceberusapp` — mencionado pero no explorado a fondo esta sesión.

## Qué falta para llevar esto a producción

1. ✅ **Tabla `tb_meta_campaign_product_weight` creada en producción** (2026-07-21, ver sección 1).
2. ✅ **Código commiteado localmente** en `feature/meta-campaign-multi-product` (varios commits lógicos: fix de filas huérfanas, fix país destino/origen, endpoint quitar-producto, reestructuración de Pautas y Ventas, wiring de quitar-producto a la UI, wording sin "pack", docs).
3. ⬜ **Mergear a `main` y pushear** — dispara el deploy automático de `goberna-dashboard`. Todavía no hecho.
4. ⬜ Ya en prod: vincular las 3 campañas reales `[JUL] PACK 360 | WSP` a Consultor Político 360 + Contrainteligencia 360, y verificar que el ROAS se vea razonable antes de anunciarlo al equipo.
5. (Opcional) Decidir si vale la pena re-importar el resto del catálogo/clientes con encoding limpio a producción — NO, espera: la corrupción es SOLO local, producción ya está bien, esto no aplica. (Si se refresca el espejo local en el futuro, usar `HEX()`/`TO_BASE64()` en vez de `SELECT *` por consola para no repetir el problema.)
6. (Opcional, evaluado y descartado por ahora) Unificar `tb_meta_campaign_map` + `tb_meta_campaign_product_weight` en una sola tabla — ver sección correspondiente arriba, no se recomienda todavía.
