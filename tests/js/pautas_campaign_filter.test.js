// Regression test para el filtro de Campaña de "Pautas y Ventas"
// (core/templates/pautas_cursos.html).
//
// Bug real visto en producción (2026-07-21): filtrar por una sola campaña
// (ej. "[JUL] PACK 360 | WSP") seguía mostrando OTRAS campañas en la tabla.
// Causa: un producto vinculado a un pack Y a su propia campaña dedicada
// trae AMBAS campañas en `row.campanas`; el filtro solo decidía qué FILAS
// sobrevivían (con un .some()) pero no recortaba ese array, así que
// buildCampaignGroupedRows() volvía a abrir un grupo por cada campaña que
// la fila traía, incluyendo las no seleccionadas.
//
// Este test extrae las funciones reales del template (no una copia a
// mano, para no perder cobertura si el código cambia sin actualizar el
// test) y verifica, con datos sintéticos que reproducen el caso real, que
// filtrar por una campaña NO deja pasar grupos de otras campañas.
//
// Correr con: node tests/js/pautas_campaign_filter.test.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const TEMPLATE_PATH = path.join(
  __dirname, "..", "..", "core", "templates", "pautas_cursos.html"
);

function extractFunction(source, name) {
  const startMarker = "function " + name + "(";
  const start = source.indexOf(startMarker);
  if (start === -1) {
    throw new Error("No se encontro la funcion '" + name + "' en pautas_cursos.html — ¿se renombro?");
  }
  let i = source.indexOf("{", start);
  let depth = 0;
  for (; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) { i++; break; }
    }
  }
  return source.slice(start, i);
}

const html = fs.readFileSync(TEMPLATE_PATH, "utf8");

const sandbox = {
  SIN_CAMPANA_GROUP: "Sin campaña con gasto en el rango",
  round2: function (v) { return Math.round((v || 0) * 100) / 100; },
};
vm.createContext(sandbox);

["filterRowCampanas", "applyCampaignFilterToRows", "buildCampaignGroupedRows"].forEach(function (name) {
  const src = extractFunction(html, name);
  vm.runInContext(src + "\nthis." + name + " = " + name + ";", sandbox);
});

// ── Dataset sintético: reproduce el caso real PACK 360 ──────────────────
// Perú tiene 2 productos vinculados: uno solo al pack, otro AL PACK Y a su
// propia campaña dedicada (el caso que gatillaba el bug).
const allLinkedRows = [
  {
    pais: "Perú",
    producto: "Consultor Político 360",
    codigo_producto: 1,
    ventas_usd: 200,
    ventas_count: 1,
    campanas: [
      { campaign_id: "PACK_PE", nombre: "[JUL] PACK 360 | WSP", gasto_usd: 54 },
    ],
  },
  {
    pais: "Perú",
    producto: "Contrainteligencia 360",
    codigo_producto: 2,
    ventas_usd: 178,
    ventas_count: 1,
    campanas: [
      { campaign_id: "PACK_PE", nombre: "[JUL] PACK 360 | WSP", gasto_usd: 54 },
      { campaign_id: "PKCONTR_PE", nombre: "[ENE] [PKCONTR001] CONTRAINTELIGENCIA 360", gasto_usd: 0 },
    ],
  },
];

// Los arrays que devuelven las funciones evaluadas en el vm.Context son de
// un realm distinto al de este proceso node — se copian a un array "plano"
// de este realm (via Array.prototype.slice.call) para que assert compare
// por contenido y no tropiece con la identidad de constructor entre realms.
function groupNames(rows) {
  var groups = sandbox.buildCampaignGroupedRows(rows);
  var out = [];
  for (var i = 0; i < groups.length; i++) out.push(String(groups[i].campana_nombre));
  return out;
}

// Sin filtro: deben aparecer las 2 campañas.
const sinFiltro = groupNames(sandbox.applyCampaignFilterToRows(allLinkedRows, []));
assert.strictEqual(
  JSON.stringify(sinFiltro.slice().sort()),
  JSON.stringify(["[ENE] [PKCONTR001] CONTRAINTELIGENCIA 360", "[JUL] PACK 360 | WSP"].sort()),
  "Sin filtro de campaña deberian verse ambas campanas"
);

// Filtrando a "[JUL] PACK 360 | WSP": NO debe aparecer PKCONTR001.
const conFiltro = groupNames(
  sandbox.applyCampaignFilterToRows(allLinkedRows, ["[JUL] PACK 360 | WSP"])
);
assert.strictEqual(
  JSON.stringify(conFiltro),
  JSON.stringify(["[JUL] PACK 360 | WSP"]),
  "Filtrar por '[JUL] PACK 360 | WSP' no debe mostrar '[ENE] [PKCONTR001] CONTRAINTELIGENCIA 360' " +
  "(bug real de produccion: el filtro dejaba pasar la fila completa con todas sus campanas)"
);

// Filtrando a la campaña dedicada: no debe aparecer el pack.
const soloDedicada = groupNames(
  sandbox.applyCampaignFilterToRows(allLinkedRows, ["[ENE] [PKCONTR001] CONTRAINTELIGENCIA 360"])
);
assert.strictEqual(
  JSON.stringify(soloDedicada),
  JSON.stringify(["[ENE] [PKCONTR001] CONTRAINTELIGENCIA 360"]),
  "Filtrar por la campana dedicada no debe mostrar el pack"
);

console.log("OK - pautas_campaign_filter.test.js: 3/3 aserciones pasaron");
