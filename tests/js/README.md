# Tests JS (client-side, sin framework)

El proyecto no tiene un test runner de JS instalado (`package.json` solo trae
dependencias de Vercel analytics). Estos tests son scripts Node planos que se
corren directo, sin dependencias nuevas:

```bash
node tests/js/pautas_campaign_filter.test.js
```

Cada test extrae las funciones reales del `<script>` del template
correspondiente (no una copia a mano) y las corre en un `vm.Context` con
stubs mínimos, para no perder cobertura si el código cambia sin actualizar
el test.
