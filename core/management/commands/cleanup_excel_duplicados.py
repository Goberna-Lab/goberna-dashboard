"""
Borra las filas source='excel' de tb_meta_ads que la API ya repuso:
por cuenta, solo desde el mes donde existe data source='api' de esa cuenta.
El histórico legacy (cuentas sin data API, ej. 2024 / enero 2025) NO se toca.

Uso:
    python manage.py cleanup_excel_duplicados            # muestra qué borraría
    python manage.py cleanup_excel_duplicados --confirmar  # borra de verdad
"""

from django.core.management.base import BaseCommand
from django.db.models import Min, Sum, Count

from core.models import MetaAds


class Command(BaseCommand):
    help = "Elimina filas excel duplicadas ya repuestas por la API (por cuenta, desde su primer mes API)."

    def add_arguments(self, parser):
        parser.add_argument("--confirmar", action="store_true",
                            help="Ejecuta el borrado. Sin este flag solo muestra el plan.")

    def handle(self, *args, **opts):
        api_min = {
            r["account_id"]: r["m"]
            for r in MetaAds.objects.filter(source="api")
            .values("account_id").annotate(m=Min("report_start"))
        }
        total_n = 0
        total_g = 0.0
        plan = []
        for acc, mn in sorted(api_min.items(), key=lambda x: x[1]):
            qs = MetaAds.objects.filter(
                source="excel", account_id__startswith=acc[:13], report_start__gte=mn
            )
            agg = qs.aggregate(n=Count("id"), g=Sum("spend"))
            if agg["n"]:
                plan.append((acc, mn, agg["n"], float(agg["g"] or 0), qs))
                total_n += agg["n"]
                total_g += float(agg["g"] or 0)

        for acc, mn, n, g, _ in plan:
            self.stdout.write(f"  {acc} desde {mn}: {n} filas (gasto {g:,.0f})")
        self.stdout.write(f"TOTAL: {total_n} filas (~{total_g:,.0f} USD ya repuestos por la API)")

        if not opts["confirmar"]:
            self.stdout.write(self.style.WARNING(
                "Modo vista previa — nada borrado. Corré con --confirmar para ejecutar."))
            return

        deleted = 0
        for _, _, _, _, qs in plan:
            deleted += qs.delete()[0]
        self.stdout.write(self.style.SUCCESS(f"Borradas {deleted} filas excel duplicadas."))
        self.stdout.write(
            f"Tabla final: total={MetaAds.objects.count()} | "
            f"excel(legacy)={MetaAds.objects.filter(source='excel').count()} | "
            f"api={MetaAds.objects.filter(source='api').count()}"
        )
