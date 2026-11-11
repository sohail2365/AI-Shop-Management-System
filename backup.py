# backup.py — Dukaan AI backup utility
#
# Usage:
#   python backup.py                    # sab shops, JSON
#   python backup.py --shop-id 1        # specific shop
#   python backup.py --format csv       # CSV format
#
# Cron (daily):
#   0 3 * * * cd /path/to/dukaan-ai && python backup.py --output /var/backups/dukaan

import os
import sys
import json
import argparse
from datetime import datetime, date
from pathlib import Path
from sqlalchemy import text

from dotenv import load_dotenv
load_dotenv()

from database import get_connection
from logger import get_logger

log = get_logger("dukaan.backup")


def _serialize(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return str(obj)


def dump_shop(shop_id: int, output_dir: Path, fmt: str = "json") -> dict:
    """Ek shop ki saari data dump karo. Returns summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")
    summary = {"shop_id": shop_id, "date": ts, "tables": {}}

    with get_connection() as conn:
        shop = conn.execute(
            text("SELECT shop_name, username FROM shops WHERE shop_id = :sid"),
            {"sid": shop_id}
        ).fetchone()
        if not shop:
            log.error(f"Shop {shop_id} nahi mila")
            return summary
        summary["shop_name"] = shop[0]

        # Order matters for restore: parents before children
        tables = {
            "shops": """SELECT shop_id, username, password, shop_name,
                               created_at, agent_settings
                        FROM shops WHERE shop_id = :sid""",
            "customers": "SELECT * FROM customers WHERE shop_id = :sid ORDER BY customer_id",
            "khaata": """SELECT k.* FROM khaata k
                         JOIN customers c ON c.customer_id = k.customer_id
                         WHERE c.shop_id = :sid ORDER BY k.id""",
            "inventory": "SELECT * FROM inventory WHERE shop_id = :sid ORDER BY id",
            "purchases": "SELECT * FROM purchases WHERE shop_id = :sid ORDER BY id",
            "expenses": "SELECT * FROM expenses WHERE shop_id = :sid ORDER BY id",
            "supplier_payments": "SELECT * FROM supplier_payments WHERE shop_id = :sid ORDER BY id",
        }
        data = {}
        for tname, q in tables.items():
            rows = conn.execute(text(q), {"sid": shop_id}).mappings().all()
            data[tname] = [{k: _serialize(v) for k, v in r.items()} for r in rows]
            summary["tables"][tname] = len(rows)

    safe_username = (shop[1] or "shop").replace("/", "_").replace("\\", "_")
    filename = f"backup_{ts}_shop{shop_id}_{safe_username}.{fmt}"
    filepath = output_dir / filename

    if fmt == "json":
        payload = {
            "backup_date": ts,
            "shop_id": shop_id,
            "shop_name": shop[0],
            "username": shop[1],
            "data": data,
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=_serialize)
    elif fmt == "csv":
        import csv
        for tname, rows in data.items():
            if not rows:
                continue
            csv_path = output_dir / f"backup_{ts}_shop{shop_id}_{tname}.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
        filepath = output_dir / f"backup_{ts}_shop{shop_id}_*.csv"

    log.info(f"Backup done: shop={shop_id} ({shop[0]}) tables={summary['tables']} -> {filepath}")
    summary["file"] = str(filepath)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Dukaan AI backup utility")
    parser.add_argument("--shop-id", type=int, help="Specific shop ID (default: all)")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--output", default="backups", help="Output directory")
    args = parser.parse_args()

    output_dir = Path(args.output)

    with get_connection() as conn:
        if args.shop_id:
            shop_ids = [args.shop_id]
        else:
            shop_ids = [r[0] for r in conn.execute(
                text("SELECT shop_id FROM shops ORDER BY shop_id")
            ).fetchall()]

    if not shop_ids:
        log.warning("Koi shop nahi mila")
        return

    log.info(f"Backing up {len(shop_ids)} shop(s) to {output_dir}/")
    for sid in shop_ids:
        dump_shop(sid, output_dir, args.format)
    log.info("Sab backups mukammal ho gaye")


if __name__ == "__main__":
    main()