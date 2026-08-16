# CST8917 - Assignment 2: Dual Implementation of an Expense Approval Workflow


**Vedio:** (https://youtu.be/z1JGzPAbYyQ)


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


**Key design decisions:**
- The approval timeout   read from  app setting in  HTTP starter, *not* inside the orchestrator because orchestrator code should be replay, reading environment variables directly inside an orchestrator is unsafe
- `task_any` was chosen over `task_all`/sequential `yield` because the requirement is explicitly a race
- The losing task is explicitly cancelled to avoid leaving an orphaned durable timer running after a decision arrives early.

## 4. Version B - Logic Apps + Azure Service Bus

**Location:** `version-b-logic-apps/`

 it owns only synchronous input validation and handing a clean message to Service Bus. The approval branch, the manager-approval-with-timeout race, and outcome routing  built visually in a Logic App workflow 


**Key design decisions:**
- Validation  in Python because Service Bus Apps have no native JSON-schema validation step that produces a clean `400` with field-level errors - Parse JSON in the designer use something didn't match the schema, not a curated error list.
- Business branching was deliberately kept out of Python and placed in the Logic App to make this version an honest representation of the "low-code" alternative, rather than a Durable Functions 
- Outcome fan-out uses topic *subscriptions with SQL filters* instead of three separate `if` branches inside the workflow, so adding a fourth outcome type later means adding a subscription, not editing the workflow.
- There is deliberately no "get status of expense X" API in this version  - see the Observability discussion below.

## 5. Comparative analysis

### Development experience

With Version A, it was as if writing regular Python but with an additional requirement that the orchestrator code must be replay-safe. With this rule learned in mind, the remainder should be easy to understand: `validate_expense_activity` and `send_notification_activity` are simple functions; the entire state machine is contained in a single file. In Version B, the interesting code was removed from Python. Constructing the Condition/Switch/timeout logic required using Workflow Definition Language expressions  and thinking about the shapes  of the connectors' inputs and outputs instead of a debugger. There is a "save and rerun" loop for iterating on function_app.py, and a "re-test a whole run" loop  to use when iterating on the Logic App itself: using the save and rerun loop saves you from having to test a whole run each iteration, and using the re-test a whole run loop saves you from having to write the explicit code for an approval-with-timeout pattern.

### Testability

The activity functions for Version A are pure Python and unit-testable without using an Azure emulator. Durable Functions Python testing utilities allow for the orchestrator to be exercised without deployment to Azure, and the end-to-end flow is exercised with Azurite + `func start` without any code deployed to Azure; all 6 mandatory scenarios and timeout path are exercised. This is only true for the ingestion step in Version B; `test-expense.http` is capable of testing the 400s and the 202/enqueue behavior locally while the actual approval/timeout/routing logic can only be tested once it is deployed to the cloud, as Logic Apps Consumption has no local designer runtime, and Service Bus historically had none of its full fidelity runtime emulator locally. This is directly evident in the test file: If you try to run the scenarios for Version B, the scenarios after the HTTP boundary will need to be verified by portal in the Run History, the Service Bus Explorer and an actual inbox.


### Error handling

The clean, field-level `400` response is returned in both cases, since validation is done in Python. After that they split ways. Version A attaches `RetryOptions` to any `call_activity`, and errors appear as regular Python exceptions with full traceback in Application Insights. Version B has no explicit exception handling and the post validation error handling is now using the default values in the queue, as specified by its `maxDeliveryCount` (5), with less detail in the error message than a Python traceback, unless it explicitly includes `runAfter: Failed` scopes in the designer - which is extra work for Version A that comes with the `try`/`except`


### Human interaction pattern

This is the most direct point for point comparison. Version A: use a context.task_any([context.wait_for_external_event("ManagerDecision"), context.create_timer(deadline)]), have explicit, visible, unit-testable branch logic, and then an explicit .cancel() on the losing task. Version B: `Send_approval_email_and_wait_for_a_response`, a built-in connector action with an identical shape. Version A gives you more control for more code (any option channel can be substituted for with an external event as it's simply an external event); Version B gives you more speed (the pattern is free but is specific to the webhook and the SelectedOptionn in Office 365 Outlook's web service, and testing requires a live mailbox).

### Observability

Version A has an instance history via free facilities: `client.get_status()`/the `statusQueryGetUri` plus `context.set_custom_status()`  and you have a single authoritative "where is expense X right now" answer, without any extra infrastructure involved. Version B's Log History of the Logic App is good for looking at a single run, but no equivalent single API, you have to go through the Function's Log, the Logic App run and Service Bus Explorer's queue/topic state by hand to trace one expense end-to-end. The intent of this project isn't to add a persisted status store for Version B  so much as to reveal it.

### Cost analysis (illustrative, Canada Central, Consumption tiers)

 figures are order-of-magnitude estimates from  Azure Pricing Calculator methodology, not quotes  real cost depends on region, memory allocation and exact action count

| Volume | Version A (Durable Functions) | Version B (Functions + Service Bus + Logic Apps) |
|---|---|---|
| 100 expenses/day (~3,000/mo) | Effectively **$0** - well inside the Functions Consumption free monthly grant ; a few cents of Azure Storage transactions for orchestration history. | **~$10-15/mo**, dominated by the Service Bus **Standard** tier's fixed namespace cost plus a small, *volume-independent* cost from the Logic App's polling trigger checking the queue every 30 seconds around the clock. |
| 10,000 expenses/day | Still mostly inside or just over the free grant; Functions Consumption execution pricing  keeps this in the low single-digit dollars per month. | **Meaningfully higher** - Logic Apps Consumption bills **per action executed** , and this workflow runs 5-6 actions per message. At 300,000 messages/month that is on the order of ~1.5-1.8M billed action roughly $150-225/month for the Logic App alone, on top of the Service Bus base fee and Function executions. |

The bottom line: Per-action pricing for Logic Apps Consumption is about 20 times more expensive per use than Azure Functions Consumption compute pricing. At low volume, that difference is not noticeable in absolute dollars; at high volume, that difference is compounded by just adding in message count for Version A, with the cost remaining fixed to plain Functions execution pricing regardless of the amount of orchestration logic being added.

## 6. Recommendation

If the workflow is anticipated to grow to thousands of expenses per day and is supposed to be a long-term solution to be handled by a team of people who are familiar with coding and testing Python, then **Version A (Durable Functions) is the better production option**. It is also materially cheaper at scale, 100% unit- and integration-testable (and remains that way without deploying anything to Azure), includes built-in per-instance observability capabilities (statusQueryGetUri, custom status), and leaves the entire approval SLA (including what happens if it expires) as a reviewable, version-controlled piece of code, not a workflow definition in a portal-configured connector.

Version B is better when the following constraints apply: When the organization is not building custom backend engineers, the business analyst needs to customize the approval flow in the visual designer, without the need to write any custom code to integrate natively with Office 365 approvals or Teams without any additional costs due to the Service Bus namespace's fixed pricing, or the Logic App's per-action pricing not being used enough for the cost to take its toll. This shape, being asynchronous and driven by messages (Function -> queue -> Logic App -> topic -> Function) is also a valid architectural design choice by itself, if the team wish to have validation and orchestration as independent deployable and scalable services, and not a single Durable Functions app.

In this particular scenario (company-wide expense approval process that is likely to expand from a small pilot team to thousands of approvals a day), the recommendation is to go with this scenario's Version A and have Version B in mind if the company's focus shifts from cost to citizen developer maintainability or richer Microsoft 365 integration.


## 7. References

- Microsoft Learn - [Durable Functions overview](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-overview)

- Microsoft Learn - [Service Bus messaging overview](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-messaging-overview)
- Microsoft Learn - [Logic Apps overview](https://learn.microsoft.com/en-us/azure/logic-apps/logic-apps-overview)
- Azure Pricing - [Azure Functions pricing](https://azure.microsoft.com/en-us/pricing/details/functions/)
- Azure Pricing - [Service Bus pricing](https://azure.microsoft.com/en-us/pricing/details/service-bus/)
- Azure Pricing - [Logic Apps pricing](https://azure.microsoft.com/en-us/pricing/details/logic-apps/)

## 8. Mandatory AI Disclosure

This project was used the assistance of **Claude**, for coding used:

- The Durable Functions orchestrator, activity functions, and HTTP endpoints in `version-a-durable-functions/function_app.py` 
- The Version B Function App (`function_app.py`), the Service Bus provisioning schema (`servicebus-config.json`),


