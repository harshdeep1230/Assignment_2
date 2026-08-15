# CST8917 - Assignment 2: Dual Implementation of an Expense Approval Workflow

**Student:** Harshdeep Puri
**Course:** CST8917 - Serverless Applications (Spring/Summer 2026)
**Assignment:** Assignment 2 - Compare & Contrast: Dual Implementation of an Expense Approval Workflow
**Last updated:** 2026-08-12

---

## 1. Repository structure

```text
CST8917-FinalProject-HarshdeepPuri/
├── README.md
├── version-a-durable-functions/
│   ├── function_app.py
│   ├── requirements.txt
│   ├── host.json
│   ├── local.settings.example.json
│   └── test-durable.http
├── version-b-logic-apps/
│   ├── function_app.py
│   ├── requirements.txt
│   ├── host.json
│   ├── local.settings.example.json
│   ├── servicebus-config.json
│   ├── logic-app-workflow.json
│   ├── test-expense.http
│   └── screenshots/
└── presentation/
    ├── slides.md
    └── video-link.md
```

## 2. Business rules (shared by both implementations)

| Rule | Detail |
|---|---|
| Payload fields | `employee_name`, `employee_email`, `amount`, `category`, `description`, `manager_email` |
| Validation | All fields required. `category` must be one of `travel`, `meals`, `supplies`, `equipment`, `software`, `other`. `amount` must be a positive number. |
| Auto-approve | `amount < $100` is approved automatically, no manager step. |
| Manager approval | `amount >= $100` requires an explicit `approved`/`rejected` decision from the manager. |
| Timeout / escalation | If the manager does not respond inside the approval window, the expense is auto-approved and flagged `escalated`. |
| Notification | The employee is notified of the final status: `approved`, `rejected`, or `escalated`. |

Both versions implement identical business rules so the comparison below is about the *platform*, not the *logic*.

## 3. Version A - Azure Durable Functions

**Location:** `version-a-durable-functions/`

Everything - validation, the auto-approve/manager-approval branch, the timeout race, and the notification - is implemented as Python code running inside a single Durable Functions app (Python v2 programming model).

**Flow:**
1. `submit_expense` (HTTP POST `/api/expenses`) starts a new orchestration instance and returns the Durable Functions status-query URIs.
2. `expense_approval_orchestrator` calls `validate_expense_activity`. Invalid input short-circuits to a `validation_error` result.
3. If `amount < 100`, the orchestrator marks the expense `approved` immediately.
4. Otherwise, it races `context.wait_for_external_event("ManagerDecision")` against `context.create_timer(deadline)` using `context.task_any([...])` - whichever completes first wins, and the other task is cancelled.
5. `manager_decision` (HTTP POST `/api/expenses/{instance_id}/decision`) raises the `ManagerDecision` external event that the orchestrator is waiting on.
6. `send_notification_activity` logs the final outcome message to the employee.
7. `context.set_custom_status(...)` is called at each stage so `get_expense_status` (and the built-in `statusQueryGetUri`) always reflects exactly where an instance is.

**Key design decisions:**
- The approval timeout (`APPROVAL_TIMEOUT_MINUTES`) is read from an app setting in the HTTP starter, *not* inside the orchestrator, because orchestrator code must be replay-deterministic - reading environment variables directly inside an orchestrator is unsafe.
- `task_any` was chosen over `task_all`/sequential `yield` because the requirement is explicitly a race ("whichever happens first").
- The losing task is explicitly cancelled (`timer_task.cancel()`) to avoid leaving an orphaned durable timer running after a decision arrives early.

## 4. Version B - Logic Apps + Azure Service Bus

**Location:** `version-b-logic-apps/`

The Python Function App here is intentionally thin: it owns only synchronous input validation and handing a clean message to Service Bus. The approval branch, the manager-approval-with-timeout race, and outcome routing are built visually in a Logic App workflow (`logic-app-workflow.json`), using Service Bus and Office 365 Outlook connectors.

**Flow:**
1. `validate_and_submit_expense` (HTTP POST `/api/expenses`) validates the payload. Invalid input returns `400` immediately and nothing is queued.
2. Valid expenses are enriched with an `expense_id`/`submitted_at` and sent to the Service Bus queue `incoming-expenses`.
3. The Logic App (`logic-app-workflow.json`) receives the queued message with a peek-lock trigger, parses it, and branches on `amount < 100`:
   - **True:** composes an `approved` outcome directly.
   - **False:** runs `Send_approval_email_and_wait_for_a_response` (Office 365 Outlook connector) addressed to `manager_email`, with `limit.timeout` set (demo value `PT5M`; production would use something like `P2D`). This single connector action is the Logic-App-native equivalent of Version A's `task_any(timer, event)` - it inherently races the human response against the timeout.
   - A `Switch` on the action's `SelectedOption` (`Approve` / `Reject` / timed-out default) determines the final status.
4. Every branch publishes its outcome to the Service Bus topic `expense-outcomes` with a custom `status` application property, then completes the original queue message.
5. Three topic subscriptions (`approved-subscription`, `rejected-subscription`, `escalated-subscription`), each with a SQL filter like `status = 'approved'`, route the outcome to one of three lightweight Service-Bus-triggered functions (`notify_approved`, `notify_rejected`, `notify_escalated`) that log the employee notification.

**Key design decisions:**
- Validation stays in Python because Service Bus/Logic Apps have no native JSON-schema validation step that produces a clean `400` with field-level errors - Parse JSON in the designer only tells you *that* something didn't match the schema, not a curated error list.
- Business branching was deliberately kept out of Python and placed in the Logic App to make this version an honest representation of the "low-code" alternative, rather than a Durable Functions app wearing a Logic App costume.
- Outcome fan-out uses topic *subscriptions with SQL filters* instead of three separate `if` branches inside the workflow, so adding a fourth outcome type later means adding a subscription, not editing the workflow.
- There is deliberately no "get status of expense X" API in this version (unlike Version A's free `statusQueryGetUri`) - see the Observability discussion below.

## 5. Comparative analysis

### Development experience

Version A felt like writing ordinary Python with one extra constraint: orchestrator code has to be replay-safe. Once that rule (no direct I/O, no `datetime.now()`, no non-deterministic randomness inside the orchestrator function) is internalized, the rest is familiar - `validate_expense_activity` and `send_notification_activity` are plain functions, and the whole state machine is visible in one file. Version B moved the interesting logic out of Python entirely. Building the Condition/Switch/timeout logic meant working in Workflow Definition Language expressions (`@coalesce(...)`, `@body('Parse_expense_JSON')?['amount']`) and reasoning about connector-specific input/output shapes (e.g., the Office 365 approval action's `SelectedOption` field) rather than a debugger. Iterating on `function_app.py` is a save-and-rerun loop; iterating on the Logic App means re-testing a whole run from the designer or Run History, which is slower per change but faster to get an initial working version, since the approval-with-timeout pattern that took explicit code in Version A is a single pre-built connector action here.

### Testability

Version A's activity functions are pure Python and unit-testable with plain `pytest`, no Azure emulator required. The orchestrator can be exercised with the Durable Functions Python testing utilities, and the full flow is exercised end-to-end by `test-durable.http` against `func start` plus Azurite - all 6 mandatory scenarios, including the timeout path, run locally with nothing deployed to Azure. Version B only offers that same level of local testability for the ingestion step; `test-expense.http` can verify the 400s and the 202/enqueue behavior locally, but the actual approval/timeout/routing logic only runs once deployed, because Logic Apps Consumption has no local designer runtime and Service Bus historically has no full-fidelity local emulator. This is reflected directly in the test file: Version A's scenarios are fully automatable, Version B's require manual portal verification (Run History, Service Bus Explorer, an actual inbox) for anything past the HTTP boundary.

### Error handling

Both versions return the same clean, field-level `400` for validation errors, because both perform validation in Python. Past that point they diverge. Version A can attach `RetryOptions` to any `call_activity`, and failures surface as normal Python exceptions with full stack traces in Application Insights; the `manager_decision` endpoint explicitly checks orchestration status to return `404`/`409` for bad instance IDs or double-decisions. Version B's post-validation error handling shifts to infrastructure defaults: a message that fails `Parse_expense_JSON` in the Logic App will be redelivered and eventually dead-lettered per the queue's `maxDeliveryCount` (5), with far less granular diagnostic detail than a Python traceback unless explicit `runAfter: Failed` scopes are added in the designer - which is extra design work Version A gets from `try`/`except` for free.

### Human interaction pattern

This is the most direct point-for-point comparison. Version A: `yield context.task_any([context.wait_for_external_event("ManagerDecision"), context.create_timer(deadline)])`, explicit, visible, and unit-testable branch logic, with an explicit `.cancel()` on the losing task. Version B: `Send_approval_email_and_wait_for_a_response` with `limit.timeout`, a single built-in connector action that already implements the identical "race a human response against a timeout" shape. Version A trades more code for more control (any decision channel could be swapped in - HTTP, Teams, SMS - since it is just an external event); Version B trades control for speed (the pattern is free, but tied to Office 365 Outlook's specific webhook and `SelectedOption`/`TimedOut` semantics, and testing it requires a live mailbox).

### Observability

Version A gets a queryable instance history for free: `client.get_status()`/the `statusQueryGetUri` plus `context.set_custom_status()` (used here to expose strings like `"Awaiting manager decision..."`) give a single, authoritative "where is expense X right now" answer with zero extra infrastructure. Version B's Logic App Run History is excellent for inspecting one run in isolation, but there is no equivalent single API - tracing one expense end-to-end means correlating the Function's log, the Logic App run, and Service Bus Explorer's queue/topic state by hand. This project does not add a persisted status store for Version B (e.g., a Table Storage entity updated per stage) precisely to make this observability gap visible rather than papering over it.

### Cost analysis (illustrative, Canada Central, Consumption tiers)

These figures are order-of-magnitude estimates from the Azure Pricing Calculator methodology (see References), not quotes - real cost depends on region, memory allocation, and exact action count.

| Volume | Version A (Durable Functions) | Version B (Functions + Service Bus + Logic Apps) |
|---|---|---|
| 100 expenses/day (~3,000/mo) | Effectively **$0** - well inside the Functions Consumption free monthly grant (1M executions / 400,000 GB-s); a few cents of Azure Storage transactions for orchestration history. | **~$10-15/mo**, dominated by the Service Bus **Standard** tier's fixed namespace cost (Topics require Standard, roughly $10/mo) plus a small, *volume-independent* cost from the Logic App's polling trigger checking the queue every 30 seconds around the clock. |
| 10,000 expenses/day (~300,000/mo) | Still mostly inside or just over the free grant; Functions Consumption execution pricing (~$0.20 per million executions after the free tier) keeps this in the low single-digit dollars per month. | **Meaningfully higher** - Logic Apps Consumption bills **per action executed** (roughly $0.000125/action for standard connectors), and this workflow runs 5-6 actions per message. At 300,000 messages/month that is on the order of ~1.5-1.8M billed actions, i.e., roughly $150-225/month for the Logic App alone, on top of the Service Bus base fee and the (still cheap) Function executions. |

The one-line takeaway: Logic Apps Consumption's per-action pricing is roughly two orders of magnitude more expensive per unit of work than Azure Functions Consumption compute pricing. At low volume that difference is invisible in absolute dollars; at high volume it compounds directly with message count, while Version A's cost stays anchored to plain Functions execution pricing regardless of how much orchestration logic is added.

## 6. Recommendation

For a workflow that is expected to scale toward thousands of expenses per day and will be maintained by a team comfortable writing and testing Python, **Version A (Durable Functions) is the stronger production choice**. It is materially cheaper at scale, fully unit- and integration-testable without deploying anything to Azure, ships built-in per-instance observability (`statusQueryGetUri`, custom status) with no extra infrastructure, and keeps the entire approval SLA - including what happens on timeout - as reviewable, version-controlled code rather than a workflow definition that lives partly in a portal-configured connector.

Version B remains the better choice under different constraints: an organization without dedicated backend engineers, a need for business analysts to modify the approval flow themselves in the visual designer, a requirement to integrate natively with Office 365 approvals or Teams without custom code, or a genuinely low, steady volume where the Service Bus namespace's fixed cost and the Logic App's per-action pricing never get exercised hard enough to matter. Its asynchronous, message-driven shape (Function -> queue -> Logic App -> topic -> Function) is also a legitimate architectural preference in its own right when the team wants validation and orchestration to be independently deployable and scalable services rather than one Durable Functions app.

For this assignment's specific scenario - a company-wide expense approval process that plausibly grows from a pilot team to thousands of submissions per day - the recommendation is **Version A for production**, with Version B kept in mind as the right call if the organization's priority shifts from cost/testability toward citizen-developer maintainability or deeper Microsoft 365 integration.

## 7. References

- Microsoft Learn - [Durable Functions overview](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-overview)
- Microsoft Learn - [Durable Functions types and features](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-types-features-overview)
- Microsoft Learn - [Service Bus messaging overview](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-messaging-overview)
- Microsoft Learn - [Logic Apps overview](https://learn.microsoft.com/en-us/azure/logic-apps/logic-apps-overview)
- Azure Pricing - [Azure Functions pricing](https://azure.microsoft.com/en-us/pricing/details/functions/)
- Azure Pricing - [Service Bus pricing](https://azure.microsoft.com/en-us/pricing/details/service-bus/)
- Azure Pricing - [Logic Apps pricing](https://azure.microsoft.com/en-us/pricing/details/logic-apps/)

## 8. Mandatory AI Disclosure

This project was developed with the assistance of **Claude (Anthropic)**, an AI coding assistant, used as a pair-programmer throughout the assignment. Specifically:

- The Durable Functions orchestrator, activity functions, and HTTP endpoints in `version-a-durable-functions/function_app.py` were scaffolded by Claude from the assignment's business-rule specification and refined in conversation with the student.
- The Version B Function App (`function_app.py`), the Service Bus provisioning schema (`servicebus-config.json`), and the Logic App workflow definition (`logic-app-workflow.json`) were likewise drafted by Claude, then reviewed by the student.
- Test files (`test-durable.http`, `test-expense.http`) and this README's structure and comparative analysis were drafted by Claude and reviewed/edited by the student.
- The student reviewed all generated code and documentation, ran the syntax/JSON validation checks noted in the accompanying development session, and is responsible for further testing (deploying to Azure, capturing the screenshots in `version-b-logic-apps/screenshots/`, and validating each of the 6 mandatory scenarios end-to-end) before submission.

This disclosure is provided in accordance with Algonquin College's academic integrity policy on the use of generative AI tools in coursework.
