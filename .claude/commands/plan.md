---
description: Plan a task against requirements.md before writing code, then wait for approval
argument-hint: <task description or requirement IDs>
---

Plan this task: $ARGUMENTS

Do not write or edit any code yet. Read `docs/requirements.md` and the relevant existing code first, then produce:

1. **Requirements:** the IDs this implements and their acceptance criteria. If the task isn't covered, is `P2`/`Later`, or is blocked by an open question, say so and stop.
2. **Understanding:** the requirement in 2–3 lines, plus any open questions. Don't assume; ask.
3. **Contract:** `shared/` Pydantic models, error codes, and enums added or changed.
4. **Server:** endpoints (method, path, status codes, request/response models), service rules and authorization, repository functions, ORM model and migration changes, audit events written.
5. **Client:** UI components, controllers, services, which thread each piece of work runs on, and which UI states (loading/error/empty/populated) apply.
6. **Sync impact:** effect on the sync engine, local index, or change journal; data-loss and conflict cases. Write "none" if not applicable.
7. **Steps:** small, ordered, each independently verifiable, each with its test.
8. **Risks and tradeoffs:** anything non-obvious, Oracle-specific concerns, and any dependency you would want to add (with the alternative).

Keep it under ~50 lines. Stop and wait for my approval.
