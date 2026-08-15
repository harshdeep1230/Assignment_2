# Screenshots checklist

Capture these once Version B is deployed and exercised end-to-end, then reference them from the main `README.md`:

1. Service Bus namespace overview showing `incoming-expenses` queue and `expense-outcomes` topic.
2. `expense-outcomes` topic subscriptions list (`approved-subscription`, `rejected-subscription`, `escalated-subscription`) with their SQL filter rules.
3. Logic App Designer canvas (top-level view: trigger -> Condition -> approval/timeout branch -> outcome send).
4. Logic App run history showing a successful "auto-approved" run (Scenario 1).
5. Approval email received by the manager (Office 365 Outlook "Send approval email" connector), showing Approve/Reject buttons.
6. Logic App run history showing a run that took the "manager approved" or "manager rejected" branch.
7. Logic App run history showing a run that timed out and took the "escalated" branch.
8. Function App log stream (Application Insights Live Metrics or `func start` console) showing a `[NOTIFICATION -> ...]` line for each of approved/rejected/escalated.
