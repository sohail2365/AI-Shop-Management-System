# worker/scheduler.py
"""
Scheduled jobs — daily reminders queue, cleanup, etc.
Yeh APScheduler se chalte hain (worker/main.py mein register hote hain).
"""
import json
from datetime import datetime, timedelta
from sqlalchemy import text
from database import get_connection
from worker.db_worker import enqueue_task, get_shop_agent_settings
from logger import get_logger

log = get_logger("dukaan.worker.scheduler")


# =========================================================
# DAILY REMINDERS
# =========================================================

def schedule_daily_reminders():
    """
    Roz chalta hai. Har shop ke liye — jinka baaki >= threshold hai,
    unko aaj ke reminder hour pe WhatsApp reminder queue karo.
    Idempotent — agar already scheduled hai to skip.
    """
    log.info("Running daily reminder scheduler")
    try:
        with get_connection() as conn:
            shops = conn.execute(
                text("""
                    SELECT shop_id, agent_settings
                    FROM shops
                    WHERE COALESCE((agent_settings->>'reminder_enabled')::boolean, TRUE) = TRUE
                """)
            ).fetchall()
    except Exception as e:
        log.error(f"Failed to load shops: {e}")
        return

    total_queued = 0
    for shop_id, raw_settings in shops:
        settings = get_shop_agent_settings(shop_id)
        min_baaki = float(settings.get("reminder_min_baaki", 100))
        reminder_hour = int(settings.get("reminder_hour", 10))
        max_per_month = int(settings.get("max_reminders_per_month", 4))

        # Aaj ka reminder time
        now = datetime.now()
        run_at = now.replace(hour=reminder_hour, minute=0, second=0, microsecond=0)
        if run_at < now:
            run_at = run_at + timedelta(days=1)

        try:
            with get_connection() as conn:
                customers = conn.execute(
                    text("""
                        SELECT c.customer_id, c.name, c.phone,
                               COALESCE(SUM(k.total), 0) as baaki
                        FROM customers c
                        LEFT JOIN khaata k
                            ON k.customer_id = c.customer_id
                            AND k.deleted_at IS NULL
                        WHERE c.shop_id = :shop_id
                          AND COALESCE(c.whatsapp_opted_in, TRUE) = TRUE
                        GROUP BY c.customer_id, c.name, c.phone
                        HAVING COALESCE(SUM(k.total), 0) >= :min_baaki
                    """),
                    {"shop_id": shop_id, "min_baaki": min_baaki}
                ).fetchall()
        except Exception as e:
            log.error(f"shop={shop_id} customer fetch failed: {e}")
            continue

        for cust in customers:
            customer_id, name, phone, baaki = cust
            # Check karo: is mahine kitne reminders gaye?
            try:
                with get_connection() as conn:
                    count_row = conn.execute(
                        text("""
                            SELECT COUNT(*) FROM agent_tasks
                            WHERE shop_id = :shop_id
                              AND task_type = 'reminder'
                              AND payload->>'customer_id' = :cid
                              AND created_at >= date_trunc('month', CURRENT_DATE)
                              AND status IN ('pending', 'running', 'done')
                        """),
                        {"shop_id": shop_id, "cid": str(customer_id)}
                    ).fetchone()
                if count_row and count_row[0] >= max_per_month:
                    continue
            except Exception as e:
                log.warning(f"count check failed: {e}")

            # Enqueue
            enqueue_task(
                shop_id=shop_id,
                task_type="reminder",
                payload={
                    "customer_id": customer_id,
                    "customer_name": name,
                    "phone": phone,
                    "baaki": float(baaki),
                    "channel": "whatsapp",
                    "scheduled_by": "daily_reminder_job",
                },
                run_at=run_at,
            )
            total_queued += 1

    log.info(f"Daily reminder scheduling done — queued {total_queued} tasks")


# =========================================================
# FOLLOW-UP — jo reply na karein unko 3 din baad dobara
# =========================================================

def schedule_no_reply_followups():
    """
    Jin reminders ka 3 din tak koi reply nahi aaya, unko follow-up.
    Phase 2 mein pura implement hoga.
    """
    log.info("Running no-reply followup scheduler (stub)")
    # TODO Phase 2
    return 0


# =========================================================
# CLEANUP — purane data prune
# =========================================================

def cleanup_old_data():
    """
    1. 90+ din purane conversations archive
    2. 180+ din purane agent_audit delete
    3. 30+ din purane done tasks delete
    """
    result = {"conversations_archived": 0, "audit_deleted": 0, "tasks_deleted": 0}
    try:
        with get_connection() as conn:
            r1 = conn.execute(
                text("""
                    UPDATE conversations
                    SET status = 'archived'
                    WHERE status IN ('closed', 'active')
                      AND last_message_at < NOW() - INTERVAL '90 days'
                    RETURNING id
                """)
            ).fetchall()
            result["conversations_archived"] = len(r1)

            r2 = conn.execute(
                text("""
                    DELETE FROM agent_audit
                    WHERE created_at < NOW() - INTERVAL '180 days'
                    RETURNING id
                """)
            ).fetchall()
            result["audit_deleted"] = len(r2)

            r3 = conn.execute(
                text("""
                    DELETE FROM agent_tasks
                    WHERE status IN ('done', 'cancelled', 'failed')
                      AND updated_at < NOW() - INTERVAL '30 days'
                    RETURNING id
                """)
            ).fetchall()
            result["tasks_deleted"] = len(r3)

            conn.commit()
        log.info(f"Cleanup: {result}")
        return result
    except Exception as e:
        log.error(f"cleanup_old_data failed: {e}")
        return result


# =========================================================
# STUCK TASK RECOVERY
# =========================================================

def recover_stuck_tasks():
    """
    Agar koi worker crash ho gaya aur tasks 'running' mein phase gaye —
    5 minute baad unko wapas pending karo.
    """
    try:
        with get_connection() as conn:
            rows = conn.execute(
                text("""
                    UPDATE agent_tasks
                    SET status = 'pending',
                        locked_at = NULL,
                        locked_by = NULL,
                        last_error = COALESCE(last_error, '') || ' [auto-recovered]'
                    WHERE status = 'running'
                      AND locked_at < NOW() - INTERVAL '5 minutes'
                    RETURNING id
                """)
            ).fetchall()
            conn.commit()
        count = len(rows)
        if count > 0:
            log.warning(f"Recovered {count} stuck tasks")
        return count
    except Exception as e:
        log.error(f"recover_stuck_tasks failed: {e}")
        return 0