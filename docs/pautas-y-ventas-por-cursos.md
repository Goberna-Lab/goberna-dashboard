# Pautas y Ventas — Cursos / Países (rama `andree-dashboard`)

Documento de seguimiento para no perder el contexto de esta iniciativa.
Objetivo de negocio: comparar la inversión en Meta Ads (USD) contra las
ventas pagadas (USD) por curso (producto) y por país, mostrando cantidad
de ventas, importe gastado USD, ventas USD, utilidad y ROAS.

## Estado actual (hecho ✅)

1. **Fix BOB→USD en `_roas_por_producto`** (commit `a17fc72`)
   - Antes se excluían (`amount_usd__isnull=False`) las filas de Meta Ads
     de cuentas que facturan en bolivianos (BOB), subestimando la
     inversión 2026 en ~$1,402.83 USD.
   - Ahora se convierte `spend / 9.07` on-the-fly (tasa de `tb_moneda`,
     codigo_moneda=3), sin persistir nada en `tb_meta_ads`.
   - Validado: inversión total 2026 pasó de $29,246.22 → $30,649.05
     (+$1,402.83, exacto).

2. **Nueva función `_roas_por_producto_pais(date_from, date_to)`**
   (`core/views.py` ~líneas 2599-2748)
   - Agrupa por `(producto, país)`.
   - Reutiliza la misma conversión BOB→USD y la cadena de resolución de
     producto: `MetaCampaignMap.codigo_producto` → fallback nombre exacto
     (`Producto.nombre_producto == MetaAds.product.strip()`) →
     `"Sin producto vinculado"` (codigo=None, NO se descarta, queda
     desglosado por país).
   - País Ads: `paid_country` → fallback `country` → `"Sin país"`.
   - País Ventas: `Venta.pais.nombre` → fallback
     `Venta.cliente.pais.nombre` → `"Sin país"`.
   - Incluye filas con `inversion_usd == 0` y `ventas_usd > 0` (ventas sin
     pauta atribuida en ese país/producto) — a diferencia de
     `_roas_por_producto`, que solo muestra productos con gasto.
   - Estructura de cada fila:
     ```python
     {
       "codigo_producto": int | None,
       "producto": str,
       "pais": str,
       "ventas_count": int,
       "inversion_usd": float,
       "ventas_usd": float,
       "utilidad_usd": float,
       "roas": float | None,  # None si inversion_usd == 0
     }
     ```
   - No expuesta en `ads.html` (eso queda solo para `_roas_por_producto`).

3. **Página nueva "Pautas y Ventas por Cursos"**
   - Vista `pautas_ventas_cursos` en `core/views.py` (~líneas 2751-2789),
     con `@ads_admin_required`.
     - GET normal → renderiza `pautas_cursos.html`.
     - GET AJAX (`X-Requested-With` o `?data=1`) → JSON
       `{date_from, date_to, rows: [...]}` desde `_roas_por_producto_pais`.
     - `date_from`/`date_to` por defecto = año actual (1 enero → hoy).
   - URL: `path('ads/cursos/', pautas_ventas_cursos, name='pautas_cursos')`
     en `dashboard_project/urls.py`.
   - Template nuevo `core/templates/pautas_cursos.html` (independiente de
     `ads.html`): mismo header/sidebar/design tokens, selector de fechas
     flatpickr, tabla Tabulator agrupada por producto (`groupBy:
     "producto"`) con columnas País, Cant. ventas, Importe gastado USD,
     Ventas USD, Utilidad, ROAS.
   - Link nuevo en `core/templates/menu/sidebard.html` ("Pautas y Ventas
     (Cursos)", ícono `fa-graduation-cap`, junto a "Ads / Pauta").

4. **Limpieza**: `core/__pycache__/models.cpython-314.pyc` dejó de estar
   trackeado en git (era un binario que no debía estar versionado).

## Decisiones de diseño confirmadas (no reabrir)

- Conversión BOB→USD: **siempre on-the-fly**, nunca persistida en
  `tb_meta_ads`. No se modifica `sync_meta_ads.py`, `import_meta_ads.py`
  ni `_meta_ads_schema.py`.
- `account_currency` solo tiene BOB y USD en datos relevantes (2026+).
- `paid_country` de la cuenta "Goberna Bolivia" es siempre "Bolivia" (igual
  patrón que la cuenta "Perú") — el 100% del gasto BOB se agrupa bajo
  país="Bolivia", aunque parte ($~49.5) se mostró en otros países. Esto es
  una limitación conocida y documentada, no un bug nuevo.
- `Venta.pais` nunca es NULL en ventas válidas (`estado in (1,2)`,
  `medio='pagado'`) — el fallback a `Cliente.pais` es solo defensivo.
- Convención ROAS: `inversion_usd == 0` → `roas = None` ("—", sin pauta);
  `inversion_usd > 0` y `ventas_usd == 0` → `roas = 0.0` (gasto sin venta
  atribuida, se muestra en rojo).

## Pendiente (por hacer)

- [ ] **Commit** de todo lo implementado en esta rama (`_roas_por_producto_pais`,
      `pautas_ventas_cursos`, template, ruta, sidebar) — aún sin commitear.
- [ ] **Probar en el navegador** `http://127.0.0.1:8000/ads/cursos/` con
      distintos rangos de fechas (incluye casos: solo pauta, solo venta,
      "Sin producto vinculado", país "Bolivia" con conversión BOB).
- [ ] **"Pautas y Ventas por Países"**: nueva función `_pautas_ventas_por_pais`
      (agrupa solo por país, sin producto) — es casi un subconjunto de
      `_roas_por_producto_pais` (sumar por país, ignorando producto).
      Decidir si va en la misma página `pautas_cursos.html` (otra pestaña/
      tabla) o en una página separada.
- [ ] **Vincular campañas pendientes**: 137/237 campañas activas 2026 sin
      `codigo_producto` en `MetaCampaignMap` (~$122,233.99 USD), de las
      cuales $15,526.21 caen hoy en "Sin producto vinculado" en el panel de
      Cursos. Vincularlas reduciría ese bucket y movería gasto real a sus
      cursos.
- [ ] (Opcional) Aplicar el mismo fix BOB→USD a `_roas_por_producto` original
      ya está hecho (commit `a17fc72`); revisar si algún otro cálculo de
      inversión Meta en `core/views.py` tiene el mismo problema de
      `amount_usd__isnull=False`.
- [ ] Decidir si `.claude/` (agentes + memoria) se sube al repo o se deja
      fuera (`.gitignore`) — pendiente desde antes, sin decidir.

## Memoria de agentes relacionada (contexto adicional)
- `.claude/agent-memory/kpi-metrics-validator/roas_por_producto_pais_design.md`
- `.claude/agent-memory/goberna-dashboard-orchestrator/decision_bob_usd_conversion.md`
