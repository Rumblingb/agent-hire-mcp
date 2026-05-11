#!/usr/bin/env python3
"""
AgentHire MCP Server — Agent Hiring Marketplace with Escrow.

A Model Context Protocol server that provides tools for posting tasks,
bidding, escrow management, completion confirmation, and dispute resolution.
Data is stored as JSON in ~/.agenthire/.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from mcp.server import FastMCP
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

DATA_DIR = os.path.expanduser("~/.agenthire")
os.makedirs(DATA_DIR, exist_ok=True)

TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
BIDS_FILE = os.path.join(DATA_DIR, "bids.json")
ESCROWS_FILE = os.path.join(DATA_DIR, "escrows.json")


def _load(filepath: str) -> list[dict]:
    """Load JSON list from file, returning [] if missing or corrupt."""
    if not os.path.isfile(filepath):
        return []
    try:
        with open(filepath, "r") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save(filepath: str, records: list[dict]) -> None:
    """Atomically write JSON list to file."""
    tmp = filepath + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(records, fh, indent=2, default=str)
    os.replace(tmp, filepath)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

TASK_STATUS_OPEN = "open"
TASK_STATUS_ASSIGNED = "assigned"
TASK_STATUS_DISPUTED = "disputed"
TASK_STATUS_COMPLETED = "completed"

BID_STATUS_PENDING = "pending"
BID_STATUS_ACCEPTED = "accepted"
BID_STATUS_REJECTED = "rejected"

ESCROW_STATUS_HELD = "held"
ESCROW_STATUS_RELEASED = "released"
ESCROW_STATUS_REFUNDED = "refunded"


def _task_by_id(task_id: str) -> Optional[dict]:
    for t in _load(TASKS_FILE):
        if t["id"] == task_id:
            return t
    return None


def _update_task(task_id: str, updates: dict) -> bool:
    tasks = _load(TASKS_FILE)
    for t in tasks:
        if t["id"] == task_id:
            t.update(updates)
            _save(TASKS_FILE, tasks)
            return True
    return False


def _escrow_for_task(task_id: str) -> Optional[dict]:
    for e in _load(ESCROWS_FILE):
        if e["task_id"] == task_id:
            return e
    return None


# ---------------------------------------------------------------------------
# MCP App
# ---------------------------------------------------------------------------

mcp = FastMCP("agenthire")

# -- Tool 1: hire_post_task ------------------------------------------------

class PostTaskInput(BaseModel):
    task_description: str = Field(description="Description of the task to be completed")
    required_capabilities: list[str] = Field(description="List of required capabilities/skills")
    max_budget: float = Field(description="Maximum budget for the task in USD", gt=0)
    deadline: str = Field(description="Deadline in ISO 8601 date format (e.g. 2025-06-15)")


@mcp.tool()
def hire_post_task(task_description: str, required_capabilities: list[str],
                   max_budget: float, deadline: str) -> str:
    """Post a new task to the AgentHire marketplace for agents to bid on."""
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "description": task_description,
        "capabilities": required_capabilities,
        "max_budget": max_budget,
        "deadline": deadline,
        "status": TASK_STATUS_OPEN,
        "created_at": _now(),
    }
    tasks = _load(TASKS_FILE)
    tasks.append(task)
    _save(TASKS_FILE, tasks)
    return json.dumps({"task_id": task_id, "status": TASK_STATUS_OPEN}, indent=2)


# -- Tool 2: hire_search_tasks ---------------------------------------------

class SearchTasksInput(BaseModel):
    capability: Optional[str] = Field(default=None, description="Optional capability to filter by")


@mcp.tool()
def hire_search_tasks(capability: Optional[str] = None) -> str:
    """Search for open tasks on the marketplace. Optionally filter by capability."""
    tasks = _load(TASKS_FILE)
    results = [t for t in tasks if t["status"] == TASK_STATUS_OPEN]
    if capability:
        cap_lower = capability.lower()
        results = [
            t for t in results
            if any(cap_lower in c.lower() for c in t.get("capabilities", []))
        ]
    return json.dumps(results, indent=2, default=str)


# -- Tool 3: hire_submit_bid -----------------------------------------------

class SubmitBidInput(BaseModel):
    task_id: str = Field(description="ID of the task to bid on")
    agent_id: str = Field(description="Identifier of the bidding agent")
    bid_amount: float = Field(description="Bid amount in USD", gt=0)
    estimated_completion: str = Field(description="Estimated completion date (ISO 8601)")


@mcp.tool()
def hire_submit_bid(task_id: str, agent_id: str,
                    bid_amount: float, estimated_completion: str) -> str:
    """Submit a bid on an open task."""
    task = _task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task {task_id} not found"})
    if task["status"] != TASK_STATUS_OPEN:
        return json.dumps({"error": f"Task {task_id} is not open for bidding (status: {task['status']})"})
    if bid_amount > task["max_budget"]:
        return json.dumps({"error": f"Bid amount ${bid_amount:.2f} exceeds max budget ${task['max_budget']:.2f}"})

    bid_id = str(uuid.uuid4())
    bid = {
        "id": bid_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "amount": bid_amount,
        "estimated_completion": estimated_completion,
        "status": BID_STATUS_PENDING,
        "created_at": _now(),
    }
    bids = _load(BIDS_FILE)
    bids.append(bid)
    _save(BIDS_FILE, bids)
    return json.dumps({"bid_id": bid_id, "task_id": task_id, "status": BID_STATUS_PENDING}, indent=2)


# -- Tool 4: hire_accept_bid -----------------------------------------------

class AcceptBidInput(BaseModel):
    task_id: str = Field(description="ID of the task")
    bid_id: str = Field(description="ID of the bid to accept")


@mcp.tool()
def hire_accept_bid(task_id: str, bid_id: str) -> str:
    """Accept a bid on a task, creating an escrow for the bid amount."""
    task = _task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task {task_id} not found"})
    if task["status"] != TASK_STATUS_OPEN:
        return json.dumps({"error": f"Task {task_id} is not open (status: {task['status']})"})

    bids = _load(BIDS_FILE)
    target_bid = None
    for b in bids:
        if b["id"] == bid_id and b["task_id"] == task_id and b["status"] == BID_STATUS_PENDING:
            target_bid = b
            break
    if target_bid is None:
        return json.dumps({"error": f"Pending bid {bid_id} on task {task_id} not found"})

    # Mark bid as accepted, all others as rejected
    for b in bids:
        if b["task_id"] == task_id and b["status"] == BID_STATUS_PENDING:
            if b["id"] == bid_id:
                b["status"] = BID_STATUS_ACCEPTED
            else:
                b["status"] = BID_STATUS_REJECTED
    _save(BIDS_FILE, bids)

    # Create escrow
    escrow_id = str(uuid.uuid4())
    escrow = {
        "id": escrow_id,
        "task_id": task_id,
        "bid_id": bid_id,
        "agent_id": target_bid["agent_id"],
        "amount": target_bid["amount"],
        "status": ESCROW_STATUS_HELD,
        "created_at": _now(),
    }
    escrows = _load(ESCROWS_FILE)
    escrows.append(escrow)
    _save(ESCROWS_FILE, escrows)

    # Update task status
    _update_task(task_id, {"status": TASK_STATUS_ASSIGNED})

    return json.dumps({
        "escrow_id": escrow_id,
        "task_id": task_id,
        "bid_id": bid_id,
        "agent_id": target_bid["agent_id"],
        "amount": target_bid["amount"],
        "status": ESCROW_STATUS_HELD,
    }, indent=2)


# -- Tool 5: hire_confirm_completion ---------------------------------------

class ConfirmCompletionInput(BaseModel):
    task_id: str = Field(description="ID of the task to mark as completed")


@mcp.tool()
def hire_confirm_completion(task_id: str) -> str:
    """Confirm task completion and release escrow payment to the agent."""
    task = _task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task {task_id} not found"})
    if task["status"] != TASK_STATUS_ASSIGNED:
        return json.dumps({"error": f"Task {task_id} is not assigned (status: {task['status']})"})

    escrow = _escrow_for_task(task_id)
    if escrow is None:
        return json.dumps({"error": f"No escrow found for task {task_id}"})
    if escrow["status"] != ESCROW_STATUS_HELD:
        return json.dumps({"error": f"Escrow for task {task_id} is not held (status: {escrow['status']})"})

    # Release escrow
    escrows = _load(ESCROWS_FILE)
    for e in escrows:
        if e["id"] == escrow["id"]:
            e["status"] = ESCROW_STATUS_RELEASED
            e["released_at"] = _now()
            break
    _save(ESCROWS_FILE, escrows)

    # Complete task
    _update_task(task_id, {"status": TASK_STATUS_COMPLETED})

    return json.dumps({
        "task_id": task_id,
        "escrow_id": escrow["id"],
        "amount_released": escrow["amount"],
        "status": TASK_STATUS_COMPLETED,
    }, indent=2)


# -- Tool 6: hire_dispute --------------------------------------------------

class DisputeInput(BaseModel):
    task_id: str = Field(description="ID of the task to dispute")
    reason: str = Field(description="Reason for the dispute")


@mcp.tool()
def hire_dispute(task_id: str, reason: str) -> str:
    """Raise a dispute on a task, freezing escrow funds until resolved."""
    task = _task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task {task_id} not found"})
    if task["status"] not in (TASK_STATUS_OPEN, TASK_STATUS_ASSIGNED):
        return json.dumps({"error": f"Task {task_id} cannot be disputed (status: {task['status']})"})

    _update_task(task_id, {"status": TASK_STATUS_DISPUTED, "dispute_reason": reason, "disputed_at": _now()})

    # Escrow stays held (frozen) until dispute is resolved externally
    return json.dumps({
        "task_id": task_id,
        "reason": reason,
        "status": TASK_STATUS_DISPUTED,
        "message": "Dispute raised. Escrow funds are frozen pending resolution.",
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
