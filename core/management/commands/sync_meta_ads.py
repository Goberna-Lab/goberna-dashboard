"""
Management command: sync_meta_ads
Pulls campaign-level insights (country breakdown, monthly) from the Meta
Marketing API and writes them into tb_meta_ads with source='api'.

Usage:
    python manage.py sync_meta_ads [--months N] [--accounts act_X,act_Y] [--dry-run] [--replace-excel-window]

Options:
    --months N                Window size in calendar months (default 1 = current month).
                              N=3 → since the 1st of (current month - 2) until today.
    --accounts a,b            Comma-separated subset of ad account ids (with or without act_ prefix).
    --dry-run                 Full fetch + mapping + summary. ZERO writes (no DDL, no DELETE, no INSERT).
    --replace-excel-window    ALSO delete source='excel' rows with report_start >= window start.
                              Default OFF. Printed loudly. Guards:
                              - Incompatible with --accounts (the excel delete is NOT
                                account-scoped — excel account_ids are float-corrupted —
                                so a subset run would delete excel rows it never replaces).
                              - Skipped automatically if any account failed mid-run
                                (otherwise the failed accounts' excel rows would be
                                deleted with no api replacement).

Write strategy (idempotent re-runs):
    transaction.atomic:
        DELETE FROM tb_meta_ads
         WHERE source='api' AND report_start >= window_start
           AND account_id IN (accounts successfully fetched this run)
        [+ optional excel-window delete]
        bulk_create(new rows)

Notes:
- API version pinned in META_API_VERSION (one constant).
- DDL (additive columns + idx_source) is emitted by this command, never by the
  web app at runtime. MySQL has no ADD COLUMN IF NOT EXISTS → we check
  information_schema.COLUMNS first.
- 'results' / 'cost_per_result' arrive as arrays like
  [{"indicator": "...", "values": [{"value": "123"}]}] — first element wins.
- Budgets arrive as strings in minor units ("40000" = 400.00) → /100.
  CAVEAT: zero-decimal currencies (CLP, COP, PYG) may use offset 1 in Meta;
  verified accounts (BOB/USD) use offset 100. Revisit if CLP budgets look 100x off.
- amount_usd is only filled when account currency is USD (FX table pending — MAS-6).
- product/category looked up from EXISTING tb_meta_ads rows by exact
  campaign_name (excel campaign_ids are corrupted; the name is the reliable key).
- Accounts are processed SEQUENTIALLY with a small sleep between calls;
  HTTP 429 / error codes 4, 17, 32, 613, 80004 → backoff using
  'estimated_time_to_regain_access' from X-Business-Use-Case-Usage when present.
- Uses urllib (requests is not in requirements.txt — no new deps).
"""

import datetime
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from core.models import MetaAds, MetaAccount, MetaCampaignMap

from ._meta_ads_schema import (
    ensure_full_schema,
    source_column_exists,
    ensure_accounts_schema,
    ensure_campaign_map_schema,
)

# ---------------------------------------------------------------------------
# API constants — version pinned in ONE place (MAS-11)
# ---------------------------------------------------------------------------
META_API_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{META_API_VERSION}"
SLEEP_BETWEEN_CALLS = 0.4  # seconds — be gentle, per-account rate limits
MAX_BACKOFF_SECONDS = 300
RETRYABLE_ERROR_CODES = {4, 17, 32, 613, 80004}

INSIGHTS_FIELDS = (
    "campaign_id,campaign_name,account_id,reach,impressions,"
    "inline_link_clicks,spend,results,cost_per_result,date_start,date_stop"
)
CAMPAIGN_FIELDS = (
    "id,name,effective_status,start_time,stop_time,"
    "daily_budget,lifetime_budget,budget_remaining"
)

# Schema DDL lives in _meta_ads_schema (shared with import_meta_ads so either
# command can bootstrap the full schema — no API token required for DDL).

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------
MONTHS_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# ISO-2 → Spanish full name (matches the excel convention in tb_meta_ads)
COUNTRY_ES = {
    "unknown": "Desconocido",   # Meta literal → Spanish (item A4)
    "AR": "Argentina",
    "BO": "Bolivia",
    "BR": "Brasil",
    "BZ": "Belice",
    "CA": "Canadá",
    "CL": "Chile",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "DE": "Alemania",
    "DO": "República Dominicana",
    "EC": "Ecuador",
    "ES": "España",
    "FR": "Francia",
    "GB": "Reino Unido",
    "GT": "Guatemala",
    "HN": "Honduras",
    "IT": "Italia",
    "MX": "México",
    "NI": "Nicaragua",
    "PA": "Panamá",
    "PE": "Perú",
    "PR": "Puerto Rico",
    "PT": "Portugal",
    "PY": "Paraguay",
    "SV": "El Salvador",
    "US": "Estados Unidos",
    "UY": "Uruguay",
    "VE": "Venezuela",
}

# Account-name heuristic → paid_country (substring on accent-stripped upper name,
# token match for short codes). Order matters: first hit wins.
PAID_COUNTRY_WORDS = [
    ("BOLIVIA",    "Bolivia"),
    ("PERU",       "Perú"),
    ("MEXICO",     "México"),
    ("ECUADOR",    "Ecuador"),
    ("CHILE",      "Chile"),
    ("COLOMBIA",   "Colombia"),
    ("ARGENTINA",  "Argentina"),
    ("GUATEMALA",  "Guatemala"),
    ("HONDURAS",   "Honduras"),
    ("PANAMA",     "Panamá"),
    ("PARAGUAY",   "Paraguay"),
    ("URUGUAY",    "Uruguay"),
    ("DOMINICANA", "República Dominicana"),
    ("SALVADOR",   "El Salvador"),
    ("NICARAGUA",  "Nicaragua"),
    ("VENEZUELA",  "Venezuela"),
    ("BRASIL",     "Brasil"),
    ("ESPANA",     "España"),
]
PAID_COUNTRY_TOKENS = {
    "PE": "Perú",
    "MX": "México",
    "BO": "Bolivia",
    "EC": "Ecuador",
    "CL": "Chile",
    "CO": "Colombia",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", s)
        if unicodedata.category(ch) != "Mn"
    )


def paid_country_from_account_name(name) -> str | None:
    """'Goberna Bolivia' → 'Bolivia'; 'GOBERNA MX' → 'México'; 'GOBERNA' → None."""
    if not name:
        return None
    n = _strip_accents(str(name)).upper()
    for word, country in PAID_COUNTRY_WORDS:
        if word in n:
            return country
    tokens = set(re.split(r"[^A-Z0-9]+", n))
    for token, country in PAID_COUNTRY_TOKENS.items():
        if token in tokens:
            return country
    return None


def _to_int(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(str(v)))
    except (ValueError, TypeError):
        return None


def _to_decimal(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _minor_units_to_decimal(v) -> Decimal | None:
    """Budgets come as strings in minor units: '40000' → 400.00."""
    if v in (None, ""):
        return None
    try:
        return (Decimal(str(v).strip()) / Decimal(100)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _iso_date(v) -> datetime.date | None:
    """'2025-04-21T10:00:00-0500' → date(2025, 4, 21)."""
    if not v:
        return None
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _extract_indicator_value(arr) -> tuple[str | None, str | None]:
    """
    'results' / 'cost_per_result' arrive as:
        [{"indicator": "reach", "values": [{"value": "443984"}]}]
    Returns (indicator, raw_value) from the FIRST element; (None, None) if absent.
    """
    if not arr or not isinstance(arr, list):
        return None, None
    first = arr[0] or {}
    indicator = first.get("indicator")
    values = first.get("values") or []
    value = values[0].get("value") if values and isinstance(values[0], dict) else None
    return indicator, value


def _window_start(months: int, today: datetime.date) -> datetime.date:
    """months=1 → 1st of current month; months=3 → 1st of (current - 2)."""
    year, month = today.year, today.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return datetime.date(year, month, 1)


def _strip_act(account_id: str) -> str:
    return account_id[4:] if account_id.startswith("act_") else account_id


# Max calendar months per insights call. Large accounts reject the full
# window in one request (HTTP 400 code=100 — result set too large with
# country breakdown), so long windows are split into month-aligned chunks.
INSIGHTS_CHUNK_MONTHS = 6


def _month_chunks(start: datetime.date, end: datetime.date, chunk_months: int):
    """Yield (since, until) pairs covering [start, end], month-aligned,
    at most chunk_months calendar months each. Boundaries never overlap, so
    time_increment=monthly rows are identical to a single full-window call."""
    cur = start
    while cur <= end:
        year = cur.year + (cur.month - 1 + chunk_months) // 12
        month = (cur.month - 1 + chunk_months) % 12 + 1
        next_start = datetime.date(year, month, 1)
        until = min(end, next_start - datetime.timedelta(days=1))
        yield cur, until
        cur = next_start


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        "Sync campaign insights (country breakdown, monthly) from the Meta "
        f"Marketing API ({META_API_VERSION}) into tb_meta_ads with source='api'."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--months", type=int, default=1,
            help="Window in calendar months (default 1 = current month).",
        )
        parser.add_argument(
            "--accounts", type=str, default=None,
            help="Comma-separated subset of ad account ids (act_X or bare).",
        )
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Fetch + map + summary, zero writes (no DDL/DELETE/INSERT).",
        )
        parser.add_argument(
            "--replace-excel-window", action="store_true", default=False,
            help="ALSO delete source='excel' rows with report_start >= window start.",
        )

    # ------------------------------------------------------------------
    # Graph API client (urllib, no extra deps)
    # ------------------------------------------------------------------

    def _graph_get(self, path_or_url: str, params: dict | None = None) -> dict:
        """GET a Graph endpoint (or a full paging.next URL). Retries throttling."""
        if params is not None:
            q = dict(params)
            q["access_token"] = self.token
            url = f"{GRAPH_BASE}/{path_or_url}?{urllib.parse.urlencode(q)}"
        else:
            url = path_or_url  # paging.next already carries the token

        last_err = None
        for attempt in range(4):
            self.api_calls += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "goberna-sync-meta-ads/1.0"})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                try:
                    err = json.loads(body).get("error", {}) or {}
                except (ValueError, AttributeError):
                    err = {}
                code = err.get("code")
                last_err = f"HTTP {e.code} code={code} {err.get('message', body[:200])}"
                if e.code == 429 or code in RETRYABLE_ERROR_CODES:
                    wait = self._throttle_wait_seconds(e.headers) or 30 * (attempt + 1)
                    wait = min(wait, MAX_BACKOFF_SECONDS)
                    self.stdout.write(self.style.WARNING(
                        f"    rate limit (code={code}); backoff {wait}s..."
                    ))
                    time.sleep(wait)
                    continue
                raise RuntimeError(last_err)
            except urllib.error.URLError as e:
                last_err = f"network error: {e.reason}"
                time.sleep(5 * (attempt + 1))
                continue
        raise RuntimeError(f"agotados los reintentos: {last_err}")

    @staticmethod
    def _throttle_wait_seconds(headers) -> int | None:
        """Parse estimated_time_to_regain_access (minutes) from BUC/throttle headers."""
        for header in ("X-Business-Use-Case-Usage", "X-FB-Ads-Insights-Throttle", "X-Ad-Account-Usage"):
            raw = headers.get(header) if headers else None
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            entries = []
            if isinstance(data, dict):
                for v in data.values():
                    entries.extend(v if isinstance(v, list) else [v])
            minutes = max(
                (e.get("estimated_time_to_regain_access", 0) for e in entries if isinstance(e, dict)),
                default=0,
            )
            if minutes:
                return int(minutes) * 60
        return None

    def _graph_get_all(self, path: str, params: dict) -> list[dict]:
        """GET with paging.next follow-up; returns concatenated data[]."""
        out: list[dict] = []
        resp = self._graph_get(path, params)
        while True:
            out.extend(resp.get("data", []))
            next_url = (resp.get("paging") or {}).get("next")
            if not next_url:
                return out
            time.sleep(SLEEP_BETWEEN_CALLS)
            resp = self._graph_get(next_url)

    # ------------------------------------------------------------------
    # Schema (additive, idempotent — MySQL lacks ADD COLUMN IF NOT EXISTS)
    # ------------------------------------------------------------------

    def _ensure_schema(self):
        self.stdout.write("Verificando schema de tb_meta_ads...")
        added = ensure_full_schema(connection)
        if added:
            self.stdout.write(self.style.SUCCESS(f"  schema actualizado: {', '.join(added)}"))
        else:
            self.stdout.write(self.style.SUCCESS("  schema OK (sin cambios)"))
        self.stdout.write("Verificando schema de tb_meta_accounts...")
        added_accts = ensure_accounts_schema(connection)
        if added_accts:
            self.stdout.write(self.style.SUCCESS(f"  tb_meta_accounts actualizado: {', '.join(added_accts)}"))
        else:
            self.stdout.write(self.style.SUCCESS("  tb_meta_accounts OK (sin cambios)"))
        self.stdout.write("Verificando schema de tb_meta_campaign_map...")
        ensure_campaign_map_schema(connection)
        self.stdout.write(self.style.SUCCESS("  tb_meta_campaign_map OK"))

    @staticmethod
    def _source_column_exists() -> bool:
        return source_column_exists(connection)

    # ------------------------------------------------------------------
    # Account upsert (tb_meta_accounts)
    # ------------------------------------------------------------------

    def _upsert_accounts(self, accounts: list[dict], dry_run: bool) -> tuple[int, int]:
        """
        Upsert the visible ad accounts into tb_meta_accounts.
        - INSERT new rows (set first_seen = now, last_seen = now)
        - UPDATE existing rows (name, currency, account_status, last_seen)
        Returns (inserted, updated).
        Skipped entirely in dry_run — prints what would change instead.
        """
        now = timezone.now()
        inserted = updated = 0

        existing_ids = set(MetaAccount.objects.values_list("account_id", flat=True))

        for acct in accounts:
            act_id = acct["id"]
            acct_num = _strip_act(act_id)
            name = acct.get("name") or act_id
            currency = acct.get("currency")
            status = acct.get("account_status")

            if acct_num not in existing_ids:
                if dry_run:
                    self.stdout.write(f"  [dry-run] INSERT tb_meta_accounts: {acct_num} ({name})")
                else:
                    MetaAccount.objects.create(
                        account_id=acct_num,
                        name=name,
                        currency=currency,
                        account_status=status,
                        first_seen=now,
                        last_seen=now,
                    )
                inserted += 1
            else:
                if dry_run:
                    self.stdout.write(f"  [dry-run] UPDATE tb_meta_accounts: {acct_num} ({name})")
                else:
                    MetaAccount.objects.filter(account_id=acct_num).update(
                        name=name,
                        currency=currency,
                        account_status=status,
                        last_seen=now,
                    )
                updated += 1

        return inserted, updated

    # ------------------------------------------------------------------
    # SKU regex (campaign names embed product SKUs like [DIPCPOL016])
    # ------------------------------------------------------------------
    _SKU_PATTERN = re.compile(r'\[([A-Z]{2,10}\d{1,5})\]')

    # Negocio name normalization: strip accents + lowercase for comparison.
    # The ads table stores 'Consultoría' but tb_negocio has 'Consultoria'.
    # We keep the ads-side canonical form for the category cache.
    _NEGOCIO_TO_ADS_CATEGORY: dict[str, str] = {
        "consultoria": "Consultoría",
        "escuela":     "Escuela",
        "editorial":   "Editorial",
        "lifestyle":   "LifeStyle",
    }

    @staticmethod
    def _normalize_negocio(name: str) -> str:
        """Strip accents + lowercase for negocio name comparison."""
        import unicodedata
        return "".join(
            ch for ch in unicodedata.normalize("NFD", name or "")
            if unicodedata.category(ch) != "Mn"
        ).lower()

    # ------------------------------------------------------------------
    # Product/category maps — built ONCE per run (not per row)
    # ------------------------------------------------------------------

    def _build_product_maps(self) -> tuple[
        dict[str, tuple[str, str | None, int]],  # sku -> (nombre, category, codigo)
        dict[str, tuple[str | None, str | None]],  # campaign_name -> (product, category) [excel]
        dict[str, tuple[str | None, str | None, str | None, int | None]],  # campaign_id -> (product, category, linked_by, codigo)
    ]:
        """
        Build three maps used during classification:
          1. sku_map:   SKU → (nombre_producto, category, codigo_producto)
          2. excel_map: campaign_name → (product, category)  from existing excel rows
          3. id_map:    campaign_id  → (product, category, linked_by, codigo_producto) from tb_meta_campaign_map
        """
        from core.models import Producto, Negocio

        # --- 1. SKU map from Producto + Negocio ---
        sku_map: dict[str, tuple[str, str | None, int]] = {}
        negocio_cache: dict[int, str] = {}
        for neg in Negocio.objects.all():
            key = self._normalize_negocio(neg.nombre_negocio)
            ads_cat = self._NEGOCIO_TO_ADS_CATEGORY.get(key, neg.nombre_negocio)
            negocio_cache[neg.codigo_negocio] = ads_cat

        for prod in Producto.objects.select_related("codigo_negocio").all():
            sku = (prod.sku_producto or "").strip().upper()
            if not sku:
                continue
            cat = negocio_cache.get(prod.codigo_negocio_id)
            sku_map[sku] = (prod.nombre_producto, cat, prod.codigo_producto)

        # --- 2. Excel inheritance map (existing campaign_name → product/category) ---
        excel_map: dict[str, tuple[str | None, str | None]] = {}
        counters: dict[str, Counter] = defaultdict(Counter)
        qs = (
            MetaAds.objects
            .exclude(campaign_name__isnull=True)
            .filter(source="excel")
            .values_list("campaign_name", "product", "category")
        )
        for name, product, category in qs.iterator():
            if product is None and category is None:
                continue
            counters[str(name).strip()][(product, category)] += 1
        excel_map = {name: c.most_common(1)[0][0] for name, c in counters.items()}

        # --- 3. Campaign map from tb_meta_campaign_map ---
        id_map: dict[str, tuple[str | None, str | None, str | None, int | None]] = {}
        for entry in MetaCampaignMap.objects.all():
            id_map[entry.campaign_id] = (
                entry.product_name,
                entry.category,
                entry.linked_by,
                entry.codigo_producto,
            )

        self.stdout.write(
            f"  SKUs en catálogo: {len(sku_map)} | "
            f"campañas en mapa: {len(id_map)} | "
            f"herencia excel: {len(excel_map)}"
        )
        return sku_map, excel_map, id_map

    def _classify_campaign(
        self,
        campaign_id: str | None,
        campaign_name: str | None,
        sku_map: dict,
        excel_map: dict,
        id_map: dict,
    ) -> tuple[str | None, str | None, str | None, int | None]:
        """
        Returns (product_name, category, linked_by, codigo_producto).
        Priority order:
          a. MetaCampaignMap by campaign_id
          b. SKU regex in campaign_name
          c. Excel name inheritance
          d. None (sin clasificar)
        """
        # a. Map by campaign_id
        if campaign_id and campaign_id in id_map:
            product_name, category, linked_by, codigo = id_map[campaign_id]
            return product_name, category, linked_by or "manual", codigo

        # b. SKU in campaign name
        if campaign_name:
            m = self._SKU_PATTERN.search(campaign_name)
            if m:
                sku = m.group(1).upper()
                if sku in sku_map:
                    nombre, cat, codigo = sku_map[sku]
                    return nombre, cat, "sku", codigo

        # c. Excel name inheritance
        if campaign_name and campaign_name in excel_map:
            product, category = excel_map[campaign_name]
            return product, category, "excel", None

        return None, None, None, None

    # ------------------------------------------------------------------
    # Campaign map upsert (persist new entries found during sync)
    # ------------------------------------------------------------------

    def _upsert_campaign_map_entries(
        self,
        new_entries: dict[str, tuple[str | None, str | None, str, int | None]],
        dry_run: bool,
    ) -> int:
        """
        Upsert entries into tb_meta_campaign_map for campaigns newly resolved
        by SKU or Excel inheritance during this sync run.
        Skips campaigns already present in the map (those came from _build_product_maps).
        Returns count of rows upserted.
        """
        from django.utils import timezone as tz

        if not new_entries or dry_run:
            return 0

        now = tz.now()
        count = 0
        for cid, (product_name, category, linked_by, codigo_producto) in new_entries.items():
            MetaCampaignMap.objects.update_or_create(
                campaign_id=cid,
                defaults={
                    "product_name": product_name,
                    "category": category,
                    "linked_by": linked_by,
                    "codigo_producto": codigo_producto,
                    "linked_at": now,
                },
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Main
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        months = options["months"]
        dry_run = options["dry_run"]
        replace_excel_window = options["replace_excel_window"]
        accounts_arg = options["accounts"]

        if months < 1:
            raise CommandError("--months debe ser >= 1")

        if replace_excel_window and accounts_arg:
            raise CommandError(
                "--replace-excel-window no se puede combinar con --accounts: "
                "el DELETE de filas excel NO está scopeado por cuenta (los account_id "
                "del excel están corruptos por floats), así que borraría filas excel "
                "de TODAS las cuentas pero solo insertaría api del subset. "
                "Correr --replace-excel-window sin --accounts."
            )

        self.token = os.getenv("META_ACCESS_TOKEN")
        if not self.token:
            raise CommandError("META_ACCESS_TOKEN no está definido en el entorno (.env).")

        self.api_calls = 0
        today = datetime.date.today()
        window_start = _window_start(months, today)
        since, until = window_start.isoformat(), today.isoformat()

        self.stdout.write(f"Meta Marketing API {META_API_VERSION}")
        self.stdout.write(f"Ventana: {since} → {until}  (--months {months})")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY-RUN: cero escrituras (sin DDL, DELETE ni INSERT)."))

        # 1. Schema (skipped in dry-run: zero writes)
        if not dry_run:
            self._ensure_schema()

        # 2. Classification maps (built ONCE per run — not per row)
        self.stdout.write("Construyendo mapas de clasificación producto/categoría...")
        sku_map, excel_map, id_map = self._build_product_maps()

        # 3. Ad accounts
        self.stdout.write("Listando ad accounts...")
        accounts = self._graph_get_all(
            "me/adaccounts",
            {"fields": "id,name,account_status,currency", "limit": "100"},
        )
        self.stdout.write(f"  {len(accounts)} cuentas visibles para el system user")

        # 3b. Upsert accounts into tb_meta_accounts (ALL visible accounts, before --accounts filter)
        if not dry_run:
            self.stdout.write("Actualizando tb_meta_accounts...")
            ins, upd = self._upsert_accounts(accounts, dry_run=False)
            self.stdout.write(self.style.SUCCESS(
                f"  tb_meta_accounts: {ins} nuevas, {upd} actualizadas"
            ))
        else:
            self.stdout.write("Simulando upsert de tb_meta_accounts (dry-run):")
            ins, upd = self._upsert_accounts(accounts, dry_run=True)
            self.stdout.write(self.style.WARNING(
                f"  [dry-run] se insertarían {ins}, actualizarían {upd} cuentas"
            ))

        if accounts_arg:
            wanted = {
                aid if aid.startswith("act_") else f"act_{aid}"
                for aid in (a.strip() for a in accounts_arg.split(",")) if aid
            }
            found_ids = {a["id"] for a in accounts}
            for missing in sorted(wanted - found_ids):
                self.stdout.write(self.style.WARNING(f"  cuenta no encontrada en el listado: {missing}"))
            accounts = [a for a in accounts if a["id"] in wanted]
            self.stdout.write(f"  subset --accounts: {len(accounts)} cuentas a procesar")

        # 4. Per-account fetch (SEQUENTIAL — per-account rate limits)
        objects: list[MetaAds] = []
        rows_fetched = 0
        unmapped_countries: Counter = Counter()
        unclassified: set[str] = set()
        per_account_spend: dict[str, tuple[Decimal, str]] = {}
        ok_account_nums: list[str] = []
        failed_accounts: list[tuple[str, str, str]] = []  # (id, name, error)
        synced_at = timezone.now()

        # New-in-this-run campaign map entries to persist after the fetch loop
        # campaign_id → (product_name, category, linked_by, codigo_producto)
        new_map_entries: dict[str, tuple[str | None, str | None, str, int | None]] = {}

        # Classification stats counters
        class_stats: Counter = Counter()

        for acct in accounts:
            act_id = acct["id"]                      # "act_123..."
            acct_num = _strip_act(act_id)
            acct_name = acct.get("name") or act_id
            currency = acct.get("currency")
            paid_country = paid_country_from_account_name(acct_name)

            self.stdout.write(f"  → {acct_name} ({act_id}, {currency}, status={acct.get('account_status')})")
            try:
                time.sleep(SLEEP_BETWEEN_CALLS)
                campaigns = self._graph_get_all(
                    f"{act_id}/campaigns",
                    {
                        "fields": CAMPAIGN_FIELDS,
                        "limit": "200",
                        # MAS-5: the API hides ARCHIVED/DELETED by default
                        "filtering": json.dumps([{
                            "field": "effective_status",
                            "operator": "IN",
                            "value": ["ACTIVE", "PAUSED", "ARCHIVED", "DELETED",
                                      "IN_PROCESS", "WITH_ISSUES"],
                        }]),
                    },
                )
                camp_map = {c["id"]: c for c in campaigns}

                insights = []
                for c_since, c_until in _month_chunks(
                    window_start, today, INSIGHTS_CHUNK_MONTHS
                ):
                    time.sleep(SLEEP_BETWEEN_CALLS)
                    insights.extend(self._graph_get_all(
                        f"{act_id}/insights",
                        {
                            "level": "campaign",
                            "breakdowns": "country",
                            "fields": INSIGHTS_FIELDS,
                            "time_increment": "monthly",
                            "time_range": json.dumps({
                                "since": c_since.isoformat(),
                                "until": c_until.isoformat(),
                            }),
                            "limit": "500",
                            # MAS-5: include spend from archived/deleted campaigns
                            "filtering": json.dumps([{
                                "field": "campaign.effective_status",
                                "operator": "IN",
                                "value": ["ACTIVE", "PAUSED", "ARCHIVED", "DELETED",
                                          "IN_PROCESS", "WITH_ISSUES"],
                            }]),
                        },
                    ))
            except (RuntimeError, urllib.error.URLError, OSError) as exc:
                failed_accounts.append((act_id, acct_name, str(exc)))
                self.stdout.write(self.style.ERROR(f"    ERROR — cuenta saltada: {exc}"))
                continue

            acct_spend = Decimal("0")
            for row in insights:
                rows_fetched += 1
                campaign_id = row.get("campaign_id")
                campaign_name = (row.get("campaign_name") or "").strip() or None
                meta = camp_map.get(campaign_id) or {}

                # country: ISO-2 → Spanish name; fallback keeps the code + counts it
                iso = row.get("country")
                country = COUNTRY_ES.get(iso)
                if country is None and iso:
                    unmapped_countries[iso] += 1
                    country = iso

                # results / cost_per_result arrays
                result_indicator, results_raw = _extract_indicator_value(row.get("results"))
                _, cpr_raw = _extract_indicator_value(row.get("cost_per_result"))

                # product/category — multi-source classifier (priority: map > sku > excel > none)
                product, category, linked_by, codigo_producto = self._classify_campaign(
                    campaign_id, campaign_name, sku_map, excel_map, id_map
                )
                if linked_by is None:
                    if campaign_name:
                        unclassified.add(campaign_name)
                    class_stats["none"] += 1
                else:
                    class_stats[linked_by] += 1
                    # Persist new SKU/excel entries discovered this run
                    if (
                        campaign_id
                        and linked_by in ("sku", "excel")
                        and campaign_id not in id_map
                        and campaign_id not in new_map_entries
                    ):
                        new_map_entries[campaign_id] = (product, category, linked_by, codigo_producto)

                report_start = _iso_date(row.get("date_start"))
                report_end = _iso_date(row.get("date_stop"))
                month = MONTHS_ES[report_start.month - 1] if report_start else None

                spend = _to_decimal(row.get("spend"))
                if spend is not None:
                    acct_spend += spend

                effective_status = meta.get("effective_status")
                objects.append(MetaAds(
                    campaign_name=campaign_name,
                    campaign_id=campaign_id,
                    account_id=row.get("account_id") or acct_num,
                    product=product,
                    category=category,
                    month=month,
                    paid_country=paid_country,
                    country=country,
                    delivery=effective_status.lower() if effective_status else None,
                    results=_to_int(results_raw),
                    result_indicator=result_indicator,
                    reach=_to_int(row.get("reach")),
                    impressions=_to_int(row.get("impressions")),
                    link_clicks=_to_int(row.get("inline_link_clicks")),
                    cost_per_result=_to_decimal(cpr_raw),
                    spend=spend,
                    amount_usd=spend if currency == "USD" else None,  # FX pendiente (MAS-6)
                    start_date=_iso_date(meta.get("start_time")),
                    end_date=_iso_date(meta.get("stop_time")),
                    report_start=report_start,
                    report_end=report_end,
                    source="api",
                    synced_at=synced_at,
                    account_currency=currency,
                    effective_status=effective_status,
                    lifetime_budget=_minor_units_to_decimal(meta.get("lifetime_budget")),
                    budget_remaining=_minor_units_to_decimal(meta.get("budget_remaining")),
                    daily_budget=_minor_units_to_decimal(meta.get("daily_budget")),
                ))

            ok_account_nums.append(acct_num)
            if acct_spend > 0:
                per_account_spend[acct_name] = (acct_spend, currency or "?")

        # 5. Deletes (compute counts; execute only on real run)
        deleted_api = deleted_excel = 0
        would_delete_api = would_delete_excel = None
        if self._source_column_exists():
            would_delete_api = MetaAds.objects.filter(
                source="api", report_start__gte=window_start,
                account_id__in=ok_account_nums,
            ).count()
            if replace_excel_window:
                would_delete_excel = MetaAds.objects.filter(
                    source="excel", report_start__gte=window_start,
                ).count()

        # 6. Summary (always printed)
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write(f"  Cuentas procesadas    : {len(ok_account_nums)}")
        self.stdout.write(f"  Cuentas fallidas      : {len(failed_accounts)}")
        self.stdout.write(f"  Llamadas API          : {self.api_calls}")
        self.stdout.write(f"  Filas insights        : {rows_fetched}")
        self.stdout.write(f"  Filas a insertar      : {len(objects)}")
        if would_delete_api is not None:
            self.stdout.write(f"  Filas api a borrar    : {would_delete_api} (report_start >= {since})")
        else:
            self.stdout.write("  Filas api a borrar    : columna 'source' aún no existe (se crea en run real)")
        if replace_excel_window:
            if failed_accounts:
                self.stdout.write(self.style.WARNING(
                    f"  *** --replace-excel-window: SE OMITE el borrado de filas EXCEL "
                    f"porque {len(failed_accounts)} cuentas fallaron — sus filas excel "
                    f"no tendrían reemplazo api. Re-correr cuando todas las cuentas pasen. ***"
                ))
            else:
                n = would_delete_excel if would_delete_excel is not None else "?"
                self.stdout.write(self.style.WARNING(
                    f"  *** --replace-excel-window ACTIVO: {n} filas EXCEL con "
                    f"report_start >= {since} serán ELIMINADAS ***"
                ))
        if unmapped_countries:
            det = ", ".join(f"{c}×{n}" for c, n in unmapped_countries.most_common())
            self.stdout.write(self.style.WARNING(f"  Países sin mapear     : {det} (se guardó el código ISO)"))
        else:
            self.stdout.write("  Países sin mapear     : 0")
        # Classification stats
        total_classified = sum(class_stats.values())
        self.stdout.write(f"  Clasificación de filas (source=api):")
        self.stdout.write(f"    por mapa/manual : {class_stats.get('manual', 0) + class_stats.get('sku', 0) + class_stats.get('excel', 0) - class_stats.get('excel', 0)}")
        self.stdout.write(f"    por SKU         : {class_stats.get('sku', 0)}")
        self.stdout.write(f"    por excel       : {class_stats.get('excel', 0)}")
        self.stdout.write(f"    por manual/mapa : {class_stats.get('manual', 0)}")
        self.stdout.write(f"    sin clasificar  : {class_stats.get('none', 0)}")
        self.stdout.write(f"    entradas mapa a persistir: {len(new_map_entries)}")
        self.stdout.write(f"  Campañas sin clasificar (nombres): {len(unclassified)}")
        for name in sorted(unclassified)[:20]:
            self.stdout.write(f"    - {name}")
        if len(unclassified) > 20:
            self.stdout.write(f"    ... y {len(unclassified) - 20} más")
        if per_account_spend:
            self.stdout.write("  Gasto por cuenta (moneda nativa):")
            for name, (total, cur) in sorted(per_account_spend.items(), key=lambda kv: -kv[1][0]):
                self.stdout.write(f"    {name:<40} {total:>14,.2f} {cur}")
        if failed_accounts:
            self.stdout.write(self.style.ERROR("  Cuentas FALLIDAS (no afectan las demás):"))
            for act_id, name, err in failed_accounts:
                self.stdout.write(self.style.ERROR(f"    {act_id} {name}: {err[:160]}"))
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY-RUN activo — ningún dato fue escrito a la DB."))
            self.stdout.write(f"[dry-run] tb_meta_campaign_map: {len(new_map_entries)} entradas se crearían")
            return

        # 7. Write (idempotent: delete window for synced accounts, then insert)
        self.stdout.write(f"Escribiendo {len(objects)} filas en tb_meta_ads...")
        try:
            with transaction.atomic():
                deleted_api = MetaAds.objects.filter(
                    source="api", report_start__gte=window_start,
                    account_id__in=ok_account_nums,
                ).delete()[0]
                if replace_excel_window:
                    if failed_accounts:
                        # Guard: deleting the excel window while accounts failed would
                        # destroy excel rows that get NO api replacement this run.
                        self.stdout.write(self.style.WARNING(
                            f"  *** --replace-excel-window OMITIDO: {len(failed_accounts)} "
                            f"cuentas fallaron; 0 filas excel borradas ***"
                        ))
                    else:
                        deleted_excel = MetaAds.objects.filter(
                            source="excel", report_start__gte=window_start,
                        ).delete()[0]
                        self.stdout.write(self.style.WARNING(
                            f"  *** {deleted_excel} filas EXCEL eliminadas (report_start >= {since}) ***"
                        ))
                MetaAds.objects.bulk_create(objects, batch_size=500)
        except Exception as exc:
            raise CommandError(f"Error durante la escritura en la DB (rollback completo): {exc}")

        # 8. Persist new campaign map entries (outside the main transaction — additive/idempotent)
        map_upserted = self._upsert_campaign_map_entries(new_map_entries, dry_run=False)
        if map_upserted:
            self.stdout.write(self.style.SUCCESS(
                f"  tb_meta_campaign_map: {map_upserted} entradas nuevas/actualizadas"
            ))

        self.stdout.write(self.style.SUCCESS(
            f"  Sync completado: {len(objects)} filas insertadas | "
            f"borradas api={deleted_api}, excel={deleted_excel}"
        ))
        if failed_accounts:
            self.stdout.write(self.style.WARNING(
                f"  ATENCIÓN: {len(failed_accounts)} cuentas fallaron — re-correr con "
                f"--accounts {','.join(a for a, _, _ in failed_accounts)}"
            ))
