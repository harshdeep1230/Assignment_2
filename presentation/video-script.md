# Video Script - CST8917 Assignment 2: Expense Approval Workflow

**Presenter:** Harshdeep Puri
**Target length:** 10-15 minutes
**Format:** Slide-by-slide narration + two live demo segments (Version A, Version B)

Each block below is `[mm:ss target] SLIDE / DEMO LABEL` followed by the spoken script and, for demo blocks, the exact actions to perform on screen. Timings are cumulative targets, not hard stops - pace to content, not the clock.

---

## [0:00-0:30] Slide 1 - Title

**On screen:** "Expense Approval Workflow: Durable Functions vs. Logic Apps" / Harshdeep Puri / CST8917 - Serverless Applications

**Say:**
"Hi, I'm Harshdeep Puri. This is my Assignment 2 for CST8917, Serverless Applications: a single expense-approval business process, implemented two different ways on Azure - once with Azure Durable Functions in Python, and once with Logic Apps orchestrating Azure Service Bus. I'll walk through the business rules, show both versions running live, and then compare them on cost, testability, and observability to make a production recommendation."

---

## [0:30-1:15] Slide 2 - The business problem

**On screen:** bullet list of the 6 business rules (payload fields, validation, auto-approve < $100, manager approval >= $100, timeout -> escalated, notification)

**Say:**
"The scenario is the same for both versions. An employee submits an expense with their name, email, amount, category, description, and their manager's email. We validate the category against a fixed list and reject anything missing or malformed. If the amount is under $100, it's auto-approved - no human needed. $100 or more requires the manager to explicitly approve or reject it. And if the manager doesn't respond within a timeout window, the system auto-approves it anyway but flags it as escalated, so someone downstream knows an SLA was missed. Finally, the employee always gets notified of the outcome. That escalation-on-timeout rule is the interesting part - it's a race between a human decision and a clock, and how each platform expresses that race is the core of this comparison."

---

## [1:15-2:15] Slide 3 - Architecture overview (both versions side by side)

**On screen:** two-column diagram - left: HTTP -> Orchestrator -> Activities (Durable Functions); right: HTTP Function -> Service Bus queue -> Logic App -> Service Bus topic -> 3 filtered subscriptions -> Functions

**Say:**
"Version A is Azure Durable Functions: one Python app owns the entire flow - the HTTP trigger that starts it, the orchestrator that contains the business logic, and the activity functions that do validation and notification. Version B splits the same problem into pieces connected by Azure Service Bus: a thin Python function only validates input and drops a message on a queue called incoming-expenses; a Logic App consumes that queue and does the actual approve/reject/timeout branching using a visual designer and connectors instead of code; it publishes the outcome to a topic called expense-outcomes with a status property; and three topic subscriptions - one each for approved, rejected, and escalated - filter that property and route to three small notification functions. Same business rules, fundamentally different execution model."

---

## [2:15-4:00] Slide 4 - Version A code walkthrough

**On screen:** `version-a-durable-functions/function_app.py` open in editor, scrolled to the orchestrator function

**Say:**
"Here's the orchestrator. It calls `validate_expense_activity` first - if that fails, we notify and stop. If the amount is under $100 we mark it approved immediately. Otherwise, this is the key line: we create a durable timer for the deadline, wait for an external event called ManagerDecision, and pass both into `context.task_any`. That's the race - whichever one finishes first wins, and I explicitly cancel whichever one loses so we don't leave a stray timer running. The manager's decision arrives through a separate HTTP endpoint, `manager_decision`, which just raises that ManagerDecision event on the right instance ID. Everything here is plain, testable Python - the only rule I had to follow is that orchestrator code has to be replay-safe, so the timeout minutes get read from configuration in the HTTP starter, not inside the orchestrator itself."

---

## [4:00-8:00] DEMO SEGMENT 1 - Version A live run

**Setup before recording:** `cd version-a-durable-functions`, `func start` running in a visible terminal, `test-durable.http` open in VS Code with the REST Client extension.

**Say while performing each step:**

1. **(0:30) Auto-approve.** "First, an expense under $100." Send Scenario 1 request. Send the follow-up status GET. "You can see runtime_status is Completed and the output status is approved - no manager step happened at all."
2. **(1:00) Manager approves.** "Now a $450 travel expense - this needs a decision." Send Scenario 2 submit request. Send the status GET - "Notice runtime_status is Running and custom_status says 'Awaiting manager decision' - that's the `context.set_custom_status` call giving us free observability into exactly where this instance is." Send the manager-decision POST with `"decision": "approved"`. Send the status GET again - "Completed, status approved, and you can see the decision_detail records the manager's email and comment."
3. **(1:00) Manager rejects.** Send Scenario 3 submit, then the decision POST with `"decision": "rejected"`, then the final status GET. "Same shape, different outcome - status is rejected."
4. **(1:30) Timeout -> escalated.** Send Scenario 4 submit. "I'm not going to send a decision for this one at all - the app setting has the timeout set to 2 minutes for this demo." Let the terminal sit, showing the Functions host logs firing the timer. Send the status GET after the wait. "And there it is - status escalated, decided_by system, reason: manager did not respond in time. That's the timer winning the race in `task_any`."
5. **(0:30) Validation errors.** Quickly send Scenario 5 (missing fields) and Scenario 6 (invalid category), showing the `validation_error` status and the specific error messages in each output.

---

## [8:00-9:30] Slide 5 - Version B workflow walkthrough

**On screen:** Logic App Designer canvas (or the Code View from `logic-app-workflow.json` if not yet deployed) showing trigger -> Parse JSON -> Condition -> approval branch -> outcome send

**Say:**
"Version B's Python function does one thing: validate, then send to the incoming-expenses queue. Everything after that is this Logic App. It triggers on a peek-lock read from the queue, parses the JSON, and checks the same amount-under-$100 condition. If it's under $100, it composes an approved outcome directly. If not, it hits this action: 'Send approval email and wait for a response,' addressed to the manager, with a timeout set on the action itself. That single built-in connector action is doing exactly what `task_any` did in Version A - racing a human response against a clock - except here it's configuration, not code. A Switch on the response - Approve, Reject, or the timeout default - decides the final status, and every branch publishes to the expense-outcomes topic with a status property before completing the queue message."

---

## [9:30-12:30] DEMO SEGMENT 2 - Version B live run

**Setup before recording:** Function App running locally (`func start` in `version-b-logic-apps/`) for the validation endpoint; Logic App deployed to Azure with a real Office 365 connection for the approval email; Service Bus Explorer and Logic App Run History open in browser tabs.

**Say while performing each step:**

1. **(1:00) Submit and watch the queue.** Send the Scenario 1 request from `test-expense.http` (under $100). "202 Accepted from the function - now watch Service Bus Explorer... the message briefly appears on incoming-expenses, and the Logic App run history shows a new run starting." Switch to Run History, show the run taking the auto-approve branch. Peek the approved-subscription in Service Bus Explorer - "there's our outcome message, with status approved as a custom property."
2. **(1:30) Manager approves via email.** Send the Scenario 2 request ($450 travel). Show the Logic App run paused on the approval action. Switch to the manager's inbox, open the approval email, click Approve. Switch back to Run History - "the run completes, and the approved-subscription gets a new message with the manager's email in decided_by."
3. **(1:00) Escalation via timeout.** Send the Scenario 4 request and do NOT respond to the email. "The action's timeout here is set to 5 minutes for this demo." Fast-forward or wait, then show the Run History run completing on the timeout/default branch, and peek the escalated-subscription for the resulting message.
4. **(0:30) Notification functions.** Switch to the Function App's log stream and show the `[NOTIFICATION -> ...]` log lines fired by `notify_approved` / `notify_escalated` as those topic messages land.

---

## [12:30-14:00] Slide 6 - Comparison highlights

**On screen:** the comparison table from README.md section 5 (condensed to 4-5 bullet rows: dev experience, testability, human interaction, observability, cost at 100/day vs 10,000/day)

**Say:**
"The full write-up is in the README, but the headline points: Version A is fully unit- and integration-testable locally with no Azure deployment - all six scenarios in test-durable.http run against func start and Azurite. Version B's business logic only really runs once deployed, because Logic Apps Consumption has no local designer runtime. Version A gets a free, queryable instance status API out of the box; Version B has no equivalent unless you build one yourself. And on cost: at 100 expenses a day both are cheap, but Service Bus Standard's fixed namespace fee gives Version A a small edge even at low volume. At 10,000 a day the gap widens a lot, because Logic Apps bills per action executed, at a rate that's roughly two orders of magnitude more expensive per unit of work than plain Functions compute."

---

## [14:00-15:00] Slide 7 - Recommendation and close

**On screen:** one-line recommendation + "Thank you" / contact or repo link

**Say:**
"My recommendation: for a process that's expected to grow toward thousands of submissions a day, maintained by a team that can write and test Python, Version A - Durable Functions - is the stronger production choice on cost, testability, and observability. Version B stays the right call for an organization without dedicated backend engineers, or one that wants business analysts editing the approval flow directly in the designer, or that needs tight native Office 365 integration. Both versions are in the repo with full test suites and setup instructions. Thanks for watching."

---

## Recording checklist

- [ ] `func start` running and visible for both demo segments (separate terminals/takes if recorded separately)
- [ ] `local.settings.json` created locally from each `local.settings.example.json` (not committed) with real values before recording
- [ ] Azurite running for Version A's storage emulation
- [ ] Version B Logic App deployed to Azure with a working Office 365 Outlook connection authenticated to a real/test mailbox before recording, so the approval email demo isn't faked
- [ ] Service Bus Explorer and Logic App Run History tabs pre-opened and logged in
- [ ] `APPROVAL_TIMEOUT_MINUTES=2` (Version A) and the Logic App's `limit.timeout: PT5M` (Version B) left at their short demo values so the escalation path doesn't require dead air
- [ ] Screen resolution/font size large enough that JSON responses and Run History details are readable on a recording
- [ ] Final video uploaded and its link recorded in `presentation/video-link.md`
