"""
Shared tb_meta_ads schema helpers for import_meta_ads and sync_meta_ads.

Single source of truth for the table DDL so EITHER command can bootstrap the
full schema (fresh DB) or patch an older table (additive ALTERs) — without
depending on the other command or on a Meta API token.

The leading underscore keeps Django from registering this module as a command.
"""

# Additive API-sync columns (also baked into CREATE_TABLE_SQL below).
# MySQL has no ADD COLUMN IF NOT EXISTS → ensure_api_columns() checks
# information_schema first.
NEW_COLUMNS = [
    ("source",           "VARCHAR(10) NOT NULL DEFAULT 'excel'"),
    ("synced_at",        "DATETIME NULL"),
    ("account_currency", "VARCHAR(3) NULL"),
    ("effective_status", "VARCHAR(20) NULL"),
    ("lifetime_budget",  "DECIMAL(14,2) NULL"),
    ("budget_remaining", "DECIMAL(14,2) NULL"),
    ("daily_budget",     "DECIMAL(14,2) NULL"),
]
SOURCE_INDEX_NAME = "idx_source"

# Generated from NEW_COLUMNS so CREATE TABLE and ALTER TABLE can never drift.
_NEW_COLUMNS_SQL = ",\n  ".join(f"{name} {ddl}" for name, ddl in NEW_COLUMNS)

# Full DDL for tb_meta_ads (emitted by the commands, NOT by Django migrations —
# MetaAds is managed=False).
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS tb_meta_ads (
  id INT AUTO_INCREMENT PRIMARY KEY,
  campaign_name VARCHAR(255) NULL,
  campaign_id VARCHAR(32) NULL,
  account_id VARCHAR(32) NULL,
  product VARCHAR(255) NULL,
  category VARCHAR(100) NULL,
  month VARCHAR(20) NULL,
  paid_country VARCHAR(100) NULL,
  country VARCHAR(100) NULL,
  delivery VARCHAR(50) NULL,
  results INT NULL,
  result_indicator VARCHAR(255) NULL,
  reach INT NULL,
  impressions INT NULL,
  link_clicks INT NULL,
  cost_per_result DECIMAL(12,2) NULL,
  spend DECIMAL(12,2) NULL,
  amount_usd DECIMAL(12,2) NULL,
  start_date DATE NULL,
  end_date DATE NULL,
  report_start DATE NULL,
  report_end DATE NULL,
  {_NEW_COLUMNS_SQL},
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_month (month),
  KEY idx_country (country),
  KEY idx_category (category),
  KEY idx_product (product),
  KEY idx_report (report_start, report_end),
  KEY idx_campaign_id (campaign_id),
  KEY {SOURCE_INDEX_NAME} (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def ensure_full_schema(connection) -> list[str]:
    """
    Idempotent bootstrap: CREATE TABLE IF NOT EXISTS with the full schema,
    then additive ALTERs for tables created by an older DDL.
    Returns the list of columns/indexes added (empty if schema was current).
    """
    with connection.cursor() as cursor:
        cursor.execute(CREATE_TABLE_SQL)
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tb_meta_ads'"
        )
        existing = {row[0].lower() for row in cursor.fetchall()}
        added = []
        for name, ddl in NEW_COLUMNS:
            if name not in existing:
                cursor.execute(f"ALTER TABLE tb_meta_ads ADD COLUMN {name} {ddl}")
                added.append(name)
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tb_meta_ads' "
            "AND INDEX_NAME = %s",
            [SOURCE_INDEX_NAME],
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(f"ALTER TABLE tb_meta_ads ADD INDEX {SOURCE_INDEX_NAME} (source)")
            added.append(SOURCE_INDEX_NAME)
    return added


def source_column_exists(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tb_meta_ads' "
            "AND COLUMN_NAME = 'source'"
        )
        return cursor.fetchone()[0] > 0


# ---------------------------------------------------------------------------
# tb_meta_accounts — one row per visible Meta ad account (from me/adaccounts)
# ---------------------------------------------------------------------------

CREATE_ACCOUNTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tb_meta_accounts (
  account_id   VARCHAR(32)  NOT NULL PRIMARY KEY COMMENT 'Numeric id, no act_ prefix',
  name         VARCHAR(255) NULL,
  currency     VARCHAR(3)   NULL,
  account_status INT        NULL,
  first_seen   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen    DATETIME     NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Columns that may be added if the table was created by an older DDL.
_ACCOUNTS_NEW_COLUMNS: list[tuple[str, str]] = []  # no additive columns yet


def ensure_accounts_schema(connection) -> list[str]:
    """
    Idempotent bootstrap for tb_meta_accounts.
    CREATE TABLE IF NOT EXISTS, then any additive ALTERs.
    Returns list of changes made (empty if schema was current).
    """
    with connection.cursor() as cursor:
        cursor.execute(CREATE_ACCOUNTS_TABLE_SQL)
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tb_meta_accounts'"
        )
        existing = {row[0].lower() for row in cursor.fetchall()}
        added = []
        for name, ddl in _ACCOUNTS_NEW_COLUMNS:
            if name not in existing:
                cursor.execute(f"ALTER TABLE tb_meta_accounts ADD COLUMN {name} {ddl}")
                added.append(name)
    return added


# ---------------------------------------------------------------------------
# tb_meta_campaign_map — product linking for Meta campaigns
# ---------------------------------------------------------------------------

CREATE_CAMPAIGN_MAP_SQL = """
CREATE TABLE IF NOT EXISTS tb_meta_campaign_map (
  campaign_id   VARCHAR(32)  NOT NULL PRIMARY KEY COMMENT 'Meta campaign_id (real API id)',
  codigo_producto INT         NULL COMMENT 'FK to tb_producto.codigo_producto (NULL = not yet resolved)',
  product_name  VARCHAR(200) NULL COMMENT 'Cache of nombre_producto at link time',
  category      VARCHAR(60)  NULL COMMENT 'Cache of nombre_negocio at link time',
  linked_by     VARCHAR(10)  NOT NULL DEFAULT 'sku' COMMENT 'sku | manual | excel',
  linked_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_campaign_map_product (codigo_producto),
  KEY idx_campaign_map_linked_by (linked_by)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def ensure_campaign_map_schema(connection) -> list[str]:
    """
    Idempotent bootstrap for tb_meta_campaign_map.
    CREATE TABLE IF NOT EXISTS, no additive columns yet.
    Returns list of changes made (empty if schema was current).
    """
    with connection.cursor() as cursor:
        cursor.execute(CREATE_CAMPAIGN_MAP_SQL)
    return []
