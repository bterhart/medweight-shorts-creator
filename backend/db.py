"""MySQL-backed job store, replacing job.json-on-disk."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pymysql
import pymysql.cursors

import config


def get_connection():
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_job(job: dict) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (id, phase, step, data) VALUES (%s, %s, %s, %s)",
                (job["jobId"], job["phase"], job["step"], json.dumps(job)),
            )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: str) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            return json.loads(row["data"]) if row else None
    finally:
        conn.close()


def save_job(job: dict) -> None:
    """Overwrite a job's stored state. Callers mutate the job dict in place
    (mirroring the n8n version's pattern) then call this once per step."""
    job["updatedAt"] = now_iso()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET phase = %s, step = %s, data = %s WHERE id = %s",
                (job["phase"], job["step"], json.dumps(job), job["jobId"]),
            )
        conn.commit()
    finally:
        conn.close()


def claim_next_job(lease_minutes: int = 10) -> dict | None:
    """Atomically claim one job that still needs work (phase 'prepare' or
    'rendering'), skipping anything locked by another worker within the
    lease window. Returns the job dict, already locked, or None if nothing
    needs doing right now."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM jobs
                WHERE phase IN ('prepare', 'rendering')
                  AND (locked_at IS NULL OR locked_at < UTC_TIMESTAMP() - INTERVAL %s MINUTE)
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE
                """,
                (lease_minutes,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            job_id = row["id"]
            cur.execute("UPDATE jobs SET locked_at = UTC_TIMESTAMP() WHERE id = %s", (job_id,))
            cur.execute("SELECT data FROM jobs WHERE id = %s", (job_id,))
            data = json.loads(cur.fetchone()["data"])
        conn.commit()
        return data
    finally:
        conn.close()


def release_job(job_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET locked_at = NULL WHERE id = %s", (job_id,))
        conn.commit()
    finally:
        conn.close()


def new_job_id() -> str:
    return str(uuid.uuid4())
