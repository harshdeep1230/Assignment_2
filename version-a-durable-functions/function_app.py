"""
CST8917 - Serverless Applications
Assignment 2: Expense Approval Workflow
Version A - Azure Durable Functions (Python v2 programming model)

Author: Harshdeep Puri

Pattern implemented: Human Interaction / Async HTTP API pattern.
The orchestrator races a "ManagerDecision" external event against a
durable timer using context.task_any(). Whichever completes first
determines the outcome:
  - Manager responds in time  -> status = approved / rejected
  - Timer fires first          -> status = escalated (auto-approved)
"""

import json
import logging
import os
from datetime import timedelta

import azure.functions as func
import azure.durable_functions as df

myApp = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ---------------------------------------------------------------------------
# Business rule constants
# ---------------------------------------------------------------------------
REQUIRED_FIELDS = [
    "employee_name",
    "employee_email",
    "amount",
    "category",
    "description",
    "manager_email",
]
VALID_CATEGORIES = {"travel", "meals", "supplies", "equipment", "software", "other"}
AUTO_APPROVE_THRESHOLD = 100
DEFAULT_TIMEOUT_MINUTES = 2  # short default so the timeout path is easy to test locally


def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, default=str),
        status_code=status_code,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# HTTP Starter: POST /api/expenses
# ---------------------------------------------------------------------------
@myApp.route(route="expenses", methods=["POST"])
@myApp.durable_client_input(client_name="client")
async def submit_expense(req: func.HttpRequest, client) -> func.HttpResponse:
    try:
        expense_data = req.get_json()
    except ValueError:
        return _json_response({"error": "Request body must be valid JSON"}, 400)

    if not isinstance(expense_data, dict):
        return _json_response({"error": "Request body must be a JSON object"}, 400)

    # Timeout window is read here (in the HTTP trigger, not the orchestrator)
    # and passed into the orchestration input, keeping the orchestrator
    # replay-deterministic.
    timeout_minutes = int(os.environ.get("APPROVAL_TIMEOUT_MINUTES", str(DEFAULT_TIMEOUT_MINUTES)))
    expense_data["_timeout_minutes"] = timeout_minutes

    instance_id = await client.start_new("expense_approval_orchestrator", client_input=expense_data)
    logging.info("Started expense approval orchestration with ID = %s", instance_id)

    return client.create_check_status_response(req, instance_id)


# ---------------------------------------------------------------------------
# HTTP: GET /api/expenses/{instance_id}  -> friendly status wrapper
# ---------------------------------------------------------------------------
@myApp.route(route="expenses/{instance_id}", methods=["GET"])
@myApp.durable_client_input(client_name="client")
async def get_expense_status(req: func.HttpRequest, client) -> func.HttpResponse:
    instance_id = req.route_params.get("instance_id")
    status = await client.get_status(instance_id)

    if status is None:
        return _json_response({"error": f"No orchestration found with ID '{instance_id}'"}, 404)

    return _json_response(
        {
            "instance_id": instance_id,
            "runtime_status": status.runtime_status.name if status.runtime_status else None,
            "custom_status": status.custom_status,
            "output": status.output,
        }
    )


# ---------------------------------------------------------------------------
# HTTP: POST /api/expenses/{instance_id}/decision  -> Manager approval endpoint
# ---------------------------------------------------------------------------
@myApp.route(route="expenses/{instance_id}/decision", methods=["POST"])
@myApp.durable_client_input(client_name="client")
async def manager_decision(req: func.HttpRequest, client) -> func.HttpResponse:
    instance_id = req.route_params.get("instance_id")

    try:
        body = req.get_json()
    except ValueError:
        return _json_response({"error": "Request body must be valid JSON"}, 400)

    decision = str(body.get("decision", "")).lower()
    if decision not in ("approved", "rejected"):
        return _json_response({"error": "Field 'decision' must be 'approved' or 'rejected'"}, 400)

    status = await client.get_status(instance_id)
    if status is None:
        return _json_response({"error": f"No orchestration found with ID '{instance_id}'"}, 404)

    if status.runtime_status not in (
        df.OrchestrationRuntimeStatus.Running,
        df.OrchestrationRuntimeStatus.Pending,
    ):
        return _json_response(
            {"error": f"Orchestration is already in a terminal state: {status.runtime_status.name}"},
            409,
        )

    await client.raise_event(
        instance_id,
        "ManagerDecision",
        {"decision": decision, "comments": body.get("comments", "")},
    )

    return _json_response(
        {"message": f"Decision '{decision}' submitted for instance '{instance_id}'"},
        202,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
@myApp.orchestration_trigger(context_name="context")
def expense_approval_orchestrator(context: df.DurableOrchestrationContext):
    expense = context.get_input()

    validation_result = yield context.call_activity("validate_expense_activity", expense)

    if not validation_result["is_valid"]:
        final_result = {
            "status": "validation_error",
            "errors": validation_result["errors"],
            "expense": expense,
        }
        context.set_custom_status("Validation failed")
        yield context.call_activity("send_notification_activity", final_result)
        return final_result

    amount = float(expense["amount"])

    if amount < AUTO_APPROVE_THRESHOLD:
        context.set_custom_status("Auto-approved (below threshold)")
        final_status = "approved"
        decision_detail = {
            "decided_by": "system",
            "reason": f"Auto-approved: amount is below the ${AUTO_APPROVE_THRESHOLD} threshold",
        }
    else:
        timeout_minutes = expense.get("_timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
        deadline = context.current_utc_datetime + timedelta(minutes=timeout_minutes)

        context.set_custom_status(f"Awaiting manager decision (timeout in {timeout_minutes} min)")

        timer_task = context.create_timer(deadline)
        approval_task = context.wait_for_external_event("ManagerDecision")

        winner = yield context.task_any([approval_task, timer_task])

        if winner == approval_task:
            if not timer_task.is_completed:
                timer_task.cancel()

            manager_response = approval_task.result
            decision = str(manager_response.get("decision", "")).lower()
            final_status = decision if decision in ("approved", "rejected") else "escalated"
            decision_detail = {
                "decided_by": expense.get("manager_email"),
                "manager_comments": manager_response.get("comments", ""),
            }
        else:
            final_status = "escalated"
            decision_detail = {
                "decided_by": "system",
                "reason": "Manager did not respond within the timeout window; "
                "expense auto-approved and flagged as escalated.",
            }

    final_result = {
        "status": final_status,
        "expense": expense,
        "decision_detail": decision_detail,
    }

    context.set_custom_status(f"Completed: {final_status}")
    yield context.call_activity("send_notification_activity", final_result)

    return final_result


# ---------------------------------------------------------------------------
# Activity: validate_expense_activity
# ---------------------------------------------------------------------------
@myApp.activity_trigger(input_name="expense")
def validate_expense_activity(expense: dict) -> dict:
    errors = []

    for field in REQUIRED_FIELDS:
        if field not in expense or expense[field] in (None, ""):
            errors.append(f"Missing required field: '{field}'")

    if "category" in expense and expense["category"] not in VALID_CATEGORIES:
        errors.append(
            f"Invalid category '{expense['category']}'. "
            f"Must be one of: {sorted(VALID_CATEGORIES)}"
        )

    if "amount" in expense and expense["amount"] not in (None, ""):
        try:
            amount_value = float(expense["amount"])
            if amount_value <= 0:
                errors.append("Field 'amount' must be greater than 0")
        except (TypeError, ValueError):
            errors.append("Field 'amount' must be a valid number")

    return {"is_valid": len(errors) == 0, "errors": errors}


# ---------------------------------------------------------------------------
# Activity: send_notification_activity
# ---------------------------------------------------------------------------
@myApp.activity_trigger(input_name="result")
def send_notification_activity(result: dict) -> dict:
    expense = result["expense"]
    status = result["status"]
    employee_email = expense.get("employee_email", "unknown")

    if status == "validation_error":
        message = (
            f"Your expense submission could not be processed due to validation "
            f"errors: {'; '.join(result.get('errors', []))}"
        )
    else:
        message = (
            f"Hi {expense.get('employee_name', 'there')}, your {expense.get('category', 'N/A')} "
            f"expense of ${expense.get('amount', 'N/A')} ('{expense.get('description', '')}') "
            f"has been {status.upper()}."
        )

    # In production this would call an email/notification service
    # (e.g., SendGrid or Azure Communication Services). For this assignment
    # the notification is logged to satisfy the "send notification" step
    # in a way that is observable in the Functions logs / Application Insights.
    logging.info("[NOTIFICATION -> %s] %s", employee_email, message)

    return {"sent_to": employee_email, "message": message}
