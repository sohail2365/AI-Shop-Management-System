# worker/main.py
"""
Dukaan AI Worker Service — Railway / Render / Fly.io pe deploy hota hai.
Vercel FastAPI se alag service hai, kyunki serverless long-running
background jobs aur agent LLM chains support nahi karta.

Run locally:  uvicorn worker.main:app --reload --port 8001
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
import os

from database import get_connection
from logger import get_logger
from worker.db_worker import enqueue_task, WORKER_ID, get_shop_agent_settings
from worker.tasks import process_pending_tasks
from worker.scheduler import (
    schedule_daily_reminders, schedule_no_reply_followups,
    cleanup_old_data, recover_stuck_tasks,
)

log = get_logger("dukaan.worker")

# Simple auth — Vercel se worker ko call karne ke liye
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "")
if not WORKER_API_KEY:
    log.warning("WORKER_API_KEY not set — worker endpoints unsecured hain!")


def verify_worker_key(x_worker_key: Optional[str]):
    if WORKER_API_KEY and x_worker_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid worker key")


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Worker starting — WORKER_ID={WORKER_ID}")

    # ---- Register scheduled jobs ----
    # 1. Pending tasks har 30 sec
    scheduler.add_job(
        process_pending_tasks,
        IntervalTrigger(seconds=30),
        id="task_processor",
        replace_existing=True,
        max_instances=1,
    )
    # 2. Stuck tasks recovery har 2 min
    scheduler.add_job(
        recover_stuck_tasks,
        IntervalTrigger(minutes=2),
        id="stuck_recovery",
        replace_existing=True,
    )
    # 3. Daily reminders roz 10:00 AM (Pakistan time — server TZ adjust karo)
    scheduler.add_job(
        schedule_daily_reminders,
        CronTrigger(hour=10, minute=0),
        id="daily_reminders",
        replace_existing=True,
    )
    # 4. No-reply followups roz 11:00 AM
    scheduler.add_job(
        schedule_no_reply_followups,
        CronTrigger(hour=11, minute=0),
        id="no_reply_followups",
        replace_existing=True,
    )
    # 5. Cleanup roz 3:00 AM
    scheduler.add_job(
        cleanup_old_data,
        CronTrigger(hour=3, minute=0),
        id="cleanup",
        replace_existing=True,
    )

    scheduler.start()
    log.info(f"Scheduler started with {len(scheduler.get_jobs())} jobs")

    yield

    scheduler.shutdown()
    log.info("Worker shutting down")


app = FastAPI(
    title="Dukaan AI Worker",
    version="0.1.0",
    lifespan=lifespan,
)


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():
    try:
        with get_connection() as conn:
            conn.execute(text("SELECT 1"))
        jobs = [{"id": j.id, "next_run": str(j.next_run_time)}
                for j in scheduler.get_jobs()]
        return {
            "status": "ok",
            "worker_id": WORKER_ID,
            "jobs_count": len(jobs),
            "jobs": jobs,
        }
    except Exception as e:
        log.error(f"Health check failed: {e}")
        return {"status": "error", "error": str(e)}


# =========================================================
# TASK ENQUEUE — Vercel FastAPI isko call karega
# =========================================================

class EnqueueRequest(BaseModel):
    shop_id: int
    task_type: str
    payload: dict
    run_at: Optional[str] = None      # ISO format ya None
    max_attempts: int = 3


@app.post("/tasks/enqueue")
def api_enqueue(
    req: EnqueueRequest,
    x_worker_key: Optional[str] = Header(None),
):
    verify_worker_key(x_worker_key)

    run_at_dt = None
    if req.run_at:
        try:
            run_at_dt = datetime.fromisoformat(req.run_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="run_at ISO format galat hai")

    task_id = enqueue_task(
        shop_id=req.shop_id,
        task_type=req.task_type,
        payload=req.payload,
        run_at=run_at_dt,
        max_attempts=req.max_attempts,
    )
    if task_id is None:
        raise HTTPException(status_code=500, detail="Task enqueue fail")
    return {"success": True, "task_id": task_id}


@app.post("/tasks/process-now")
def api_process_now(x_worker_key: Optional[str] = Header(None)):
    """Testing ke liye — manually trigger task processor."""
    verify_worker_key(x_worker_key)
    process_pending_tasks(batch_size=20)
    return {"success": True}


@app.post("/tasks/cancel")
def api_cancel(
    shop_id: int,
    task_type: str,
    customer_id: Optional[int] = None,
    x_worker_key: Optional[str] = Header(None),
):
    verify_worker_key(x_worker_key)
    from worker.db_worker import cancel_tasks
    count = cancel_tasks(task_type, shop_id, customer_id)
    return {"success": True, "cancelled": count}


# =========================================================
# AGENT INVOCATION — Phase 1 mein fill hoga
# =========================================================

class AgentRequest(BaseModel):
    shop_id: int
    channel: str                     # 'whatsapp' | 'voice' | 'web'
    external_id: str                 # phone ya call SID
    message: str
    customer_id: Optional[int] = None


@app.post("/agent/handle")
def api_agent_handle(
    req: AgentRequest,
    x_worker_key: Optional[str] = Header(None),
):
    """
    Vercel webhook isko call karega jab customer message aaye.
    Orchestrator chalayega.
    """
    verify_worker_key(x_worker_key)
    # TODO Phase 1:
    # from agent.orchestrator import handle_message
    # return handle_message(...)
    log.info(f"[agent/handle] shop={req.shop_id} ch={req.channel} msg='{req.message[:80]}'")
    return {
        "success": True,
        "status": "stub",
        "message": "Phase 1 mein orchestrator yahan chalega",
    }


# =========================================================
# ADMIN — manual triggers
# =========================================================

@app.post("/admin/run-daily-reminders")
def api_run_daily_reminders(x_worker_key: Optional[str] = Header(None)):
    verify_worker_key(x_worker_key)
    schedule_daily_reminders()
    return {"success": True}


@app.post("/admin/cleanup")
def api_cleanup(x_worker_key: Optional[str] = Header(None)):
    verify_worker_key(x_worker_key)
    result = cleanup_old_data()
    return {"success": True, "result": result}


@app.post("/admin/recover-stuck")
def api_recover(x_worker_key: Optional[str] = Header(None)):
    verify_worker_key(x_worker_key)
    count = recover_stuck_tasks()
    return {"success": True, "recovered": count}