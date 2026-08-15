"""
CST8917 - Serverless Applications
Assignment 2: Expense Approval Workflow
Version B - Logic Apps + Azure Service Bus

Author: Harshdeep Puri

Design note (this is the core contrast with Version A):
This Function App is intentionally thin. It owns ONLY the synchronous
concerns - request validation and handing a clean message to Service Bus.
The actual business orchestration (auto-approve threshold check, manager
approval with timeout, and routing the final decision) is implemented in
the Logic App workflow (see ../logic-app-workflow.json), using the
Service Bus and Office 365 Outlook connectors in the visual designer.

Message flow:
  HTTP POST /api/expenses
      -> validate_and_submit_expense (this file)
      -> Service Bus queue "incoming-expenses"
      -> Logic App consumes the queue (peek-lock), applies the
         auto-approve / manager-approval-with-timeout logic, and
         publishes the outcome to Service Bus topic "expense-outcomes"
         with a custom "status" application property.
      -> Topic subscriptions filter on that property
         (status = 'approved' | 'rejected' | 'escalated')
      -> The three functions below each consume one subscription and
         send the final notification to the employee.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ---------------------------------------------------------------------------
# Business rule constants (identical rules to Version A, enforced here
# because Service Bus / Logic Apps have no built-in JSON schema validation)
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

SERVICE_BUS_CONNECTION_SETTING = "ServiceBusConnection"
INCOMING_QUEUE_NAME = "incoming-expenses"
OUTCOMES_TOPIC_NAME = "expense-outcomes"


def _validate_expense(expense: dict) -> list:
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

    return errors


def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, default=str),
        status_code=status_code,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# HTTP: POST /api/expenses
# Validates the payload and, if valid, drops it onto the "incoming-expenses"
# Service Bus queue for the Logic App to pick up. This is the ONLY place
# business-rule validation happens in Version B.
# ---------------------------------------------------------------------------
@app.function_name(name="validate_and_submit_expense")
@app.route(route="expenses", methods=["POST"])
@app.service_bus_queue_output(
    arg_name="msg",
    queue_name=INCOMING_QUEUE_NAME,
    connection=SERVICE_BUS_CONNECTION_SETTING,
)
def validate_and_submit_expense(req: func.HttpRequest, msg: func.Out[str]) -> func.HttpResponse:
    try:
        expense = req.get_json()
    except ValueError:
        return _json_response({"status": "validation_error", "errors": ["Request body must be valid JSON"]}, 400)

    if not isinstance(expense, dict):
        return _json_response({"status": "validation_error", "errors": ["Request body must be a JSON object"]}, 400)

    errors = _validate_expense(expense)
    if errors:
        logging.warning("Expense validation failed: %s", errors)
        return _json_response({"status": "validation_error", "errors": errors}, 400)

    expense_id = str(uuid.uuid4())
    enriched_expense = {
        **expense,
        "expense_id": expense_id,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    msg.set(json.dumps(enriched_expense))
    logging.info("Expense %s validated and queued to '%s'", expense_id, INCOMING_QUEUE_NAME)

    return _json_response(
        {
            "status": "accepted",
            "expense_id": expense_id,
            "message": (
                "Expense validated and queued for processing. The approval "
                "decision (auto-approve, manager approval, or timeout "
                "escalation) is handled asynchronously by the Logic App "
                "workflow consuming 'incoming-expenses'."
            ),
        },
        202,
    )


# ---------------------------------------------------------------------------
# Service Bus Topic triggers: one per outcome subscription.
# The Logic App publishes final decisions to 'expense-outcomes' with a
# 'status' application property; each subscription's SQL filter routes the
# message to exactly one of these handlers, which sends the employee
# notification. Content-based routing replaces the single notification
# activity function used in Version A.
# ---------------------------------------------------------------------------
def _notify(msg: func.ServiceBusMessage, expected_status: str) -> None:
    body = json.loads(msg.get_body().decode("utf-8"))
    expense = body.get("expense", body)
    employee_email = expense.get("employee_email", "unknown")

    message_text = (
        f"Hi {expense.get('employee_name', 'there')}, your {expense.get('category', 'N/A')} "
        f"expense of ${expense.get('amount', 'N/A')} ('{expense.get('description', '')}') "
        f"has been {expected_status.upper()}."
    )

    # In production this would call an email/notification service
    # (e.g., SendGrid output binding or Office 365 Outlook connector).
    # Logged here so the outcome is observable in Functions logs /
    # Application Insights during local and cloud testing.
    logging.info("[NOTIFICATION -> %s] %s", employee_email, message_text)


@app.function_name(name="notify_approved")
@app.service_bus_topic_trigger(
    arg_name="msg",
    topic_name=OUTCOMES_TOPIC_NAME,
    subscription_name="approved-subscription",
    connection=SERVICE_BUS_CONNECTION_SETTING,
)
def notify_approved(msg: func.ServiceBusMessage) -> None:
    _notify(msg, "approved")


@app.function_name(name="notify_rejected")
@app.service_bus_topic_trigger(
    arg_name="msg",
    topic_name=OUTCOMES_TOPIC_NAME,
    subscription_name="rejected-subscription",
    connection=SERVICE_BUS_CONNECTION_SETTING,
)
def notify_rejected(msg: func.ServiceBusMessage) -> None:
    _notify(msg, "rejected")


@app.function_name(name="notify_escalated")
@app.service_bus_topic_trigger(
    arg_name="msg",
    topic_name=OUTCOMES_TOPIC_NAME,
    subscription_name="escalated-subscription",
    connection=SERVICE_BUS_CONNECTION_SETTING,
)
def notify_escalated(msg: func.ServiceBusMessage) -> None:
    _notify(msg, "escalated")
