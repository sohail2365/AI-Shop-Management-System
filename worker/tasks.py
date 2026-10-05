# worker/tasks.py
"""
Task processor — pending agent_tasks ko pick karke sahi handler ko bhejta hai.
Yeh actual agent orchestration ka entry point hai (Phase 1 mein agents fill honge).
"""
import time
from typing import Callable
from worker.db_worker import (
    claim_pending_tasks, mark_task_done, mark_task_failed,
    write_audit, get_shop_agent_settings,
)
from logger import get_logger

log = get_logger("dukaan.worker.tasks")


# =========================================================
# TASK HANDLERS — Phase 1 mein implement honge
# =========================================================

def handle_reminder(task: dict) -> dict:
    """
    Reminder Agent chalao.
    """
    payload = task["payload"]
    shop_id = task["shop_id"]
    
    from agent.agents.reminder import handle_reminder_task
    result = handle_reminder_task(shop_id, payload)
    
    log.info(f"[reminder] shop={shop_id} result={result.get('status')}")
    return result


def handle_upsell(task: dict) -> dict:
    payload = task["payload"]
    log.info(f"[upsell] shop={task['shop_id']} customer={payload.get('customer_name')}")
    # TODO Phase 2
    return {"status": "stub"}


def handle_followup(task: dict) -> dict:
    log.info(f"[followup] shop={task['shop_id']}")
    # TODO Phase 2
    return {"status": "stub"}


def handle_payment_nudge(task: dict) -> dict:
    log.info(f"[payment_nudge] shop={task['shop_id']}")
    # TODO Phase 1
    return {"status": "stub"}


def handle_cold_call(task: dict) -> dict:
    """
    Phase 3 — voice outbound call. Guardrails zaroori.
    """
    payload = task["payload"]
    shop_id = task["shop_id"]
    settings = get_shop_agent_settings(shop_id)

    if not settings.get("cold_call_enabled"):
        log.info(f"[cold_call] disabled for shop={shop_id}")
        return {"status": "skipped", "reason": "cold_call_disabled"}

    log.info(f"[cold_call] shop={shop_id} customer={payload.get('customer_name')}")
    # TODO Phase 3
    return {"status": "stub"}


def handle_cleanup(task: dict) -> dict:
    """Purane data cleanup — conversations archive, audit prune."""
    from worker.scheduler import cleanup_old_data
    result = cleanup_old_data()
    return {"status": "ok", "cleanup": result}


# Registry — task_type -> handler
TASK_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "reminder": handle_reminder,
    "upsell": handle_upsell,
    "followup": handle_followup,
    "payment_nudge": handle_payment_nudge,
    "cold_call": handle_cold_call,
    "cleanup": handle_cleanup,
}


# =========================================================
# MAIN PROCESSOR
# =========================================================

def process_pending_tasks(batch_size: int = 10):
    """
    Scheduler isko har 30 second call karta hai.
    Tasks claim karo, handlers chalao, result mark karo.
    """
    tasks = claim_pending_tasks(limit=batch_size)
    if not tasks:
        return

    log.info(f"Processing {len(tasks)} pending tasks")

    for task in tasks:
        task_id = task["id"]
        task_type = task["task_type"]
        shop_id = task["shop_id"]
        started = time.time()

        handler = TASK_HANDLERS.get(task_type)
        if not handler:
            log.warning(f"No handler for task_type={task_type}")
            mark_task_failed(task_id, f"no handler for {task_type}", retry=False)
            continue

        try:
            result = handler(task)
            latency_ms = int((time.time() - started) * 1000)
            mark_task_done(task_id)
            write_audit(
                shop_id=shop_id,
                agent_name=f"task_handler.{task_type}",
                action="task_completed",
                input_data={"task_id": task_id, "payload": task["payload"]},
                output_data=result,
                latency_ms=latency_ms,
                success=True,
            )
            log.info(f"Task {task_id} ({task_type}) done in {latency_ms}ms")
        except Exception as e:
            latency_ms = int((time.time() - started) * 1000)
            log.error(f"Task {task_id} ({task_type}) raised: {e}")
            mark_task_failed(task_id, str(e), retry=True)
            write_audit(
                shop_id=shop_id,
                agent_name=f"task_handler.{task_type}",
                action="task_failed",
                input_data={"task_id": task_id, "payload": task["payload"]},
                latency_ms=latency_ms,
                success=False,
                error=str(e)[:2000],
            )