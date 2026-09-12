"""Manus API v2 client - every call here matches a confirmed OpenAPI spec
pasted directly from Manus's own docs during development (file.upload,
task.create, task.listMessages), not a third-party summary."""
import time

import requests

import config


def _headers():
    return {"x-manus-api-key": config.MANUS_API_KEY}


def _check_ok(data, what):
    if not data.get("ok"):
        err = data.get("error", {})
        raise RuntimeError(f"Manus {what} failed: {err.get('code')}: {err.get('message')}")


def upload_file(local_path: str, filename: str) -> str:
    """Two-step upload per the real file.upload spec: create a record, then
    PUT the bytes to the presigned URL it returns. Returns the file_id."""
    resp = requests.post(
        f"{config.MANUS_BASE_URL}/v2/file.upload",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"filename": filename},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _check_ok(data, "file.upload")

    with open(local_path, "rb") as f:
        put_resp = requests.put(data["upload_url"], data=f, timeout=60)
    put_resp.raise_for_status()

    return data["file"]["id"]


def create_task(content_parts: list, structured_output_schema: dict | None = None) -> str:
    """content_parts is the message.content array - text and file parts
    mixed together, per the real spec (there is no separate 'attachments'
    field). Returns task_id (flat in the response, confirmed)."""
    body = {"message": {"content": content_parts}}
    if structured_output_schema:
        body["structured_output_schema"] = structured_output_schema

    resp = requests.post(
        f"{config.MANUS_BASE_URL}/v2/task.create",
        headers={**_headers(), "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _check_ok(data, "task.create")
    return data["task_id"]


def list_messages(task_id: str, limit: int = 10) -> list:
    resp = requests.get(
        f"{config.MANUS_BASE_URL}/v2/task.listMessages",
        headers=_headers(),
        params={"task_id": task_id, "order": "desc", "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _check_ok(data, "task.listMessages")
    return data["messages"]


class ManusWaiting(Exception):
    """Raised when the task is waiting on a question/confirmation this
    pipeline doesn't implement task.sendMessage/task.confirmAction for."""


class ManusTaskError(Exception):
    pass


def poll_task(task_id: str, timeout_s: int = 600, interval_s: int = 8) -> dict:
    """Polls task.listMessages until agent_status leaves 'running'. Returns
    the structured_output_result event's value on success. Raises
    ManusWaiting/ManusTaskError/TimeoutError otherwise - the caller decides
    how those map to job.phase."""
    deadline = time.time() + timeout_s
    while True:
        messages = list_messages(task_id)
        status_msg = next((m for m in messages if m["type"] == "status_update"), None)
        agent_status = status_msg["status_update"]["agent_status"] if status_msg else "running"

        if agent_status == "running":
            if time.time() >= deadline:
                raise TimeoutError(f"Manus task {task_id} did not finish within {timeout_s}s")
            time.sleep(interval_s)
            continue

        if agent_status == "stopped":
            result_msg = next((m for m in messages if m["type"] == "structured_output_result"), None)
            outcome = result_msg["structured_output_result"] if result_msg else None
            if not outcome or not outcome.get("success") or not outcome.get("value"):
                raise ManusTaskError(
                    f"Manus stopped without a usable structured_output_result: {outcome.get('error') if outcome else 'no result event'}"
                )
            return outcome["value"]

        if agent_status == "error":
            err_msg = next((m for m in messages if m["type"] == "error_message"), None)
            detail = err_msg["error_message"]["content"] if err_msg else "unknown error"
            raise ManusTaskError(f"Manus task errored: {detail}")

        if agent_status == "waiting":
            raise ManusWaiting(
                "Manus task is waiting on a question or confirmation this pipeline does not handle "
                "(task.sendMessage/task.confirmAction not implemented) - check the task in the Manus web app."
            )

        raise ManusTaskError(f"Unexpected agent_status: {agent_status}")
