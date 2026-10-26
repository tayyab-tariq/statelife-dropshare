---
description: Turn a new feature brief into requirement entries in requirements.md and list clarifying questions
argument-hint: <paste the brief>
---

Here is the brief for a new feature or change:

$ARGUMENTS

Do not write any code.

1. Read `docs/requirements.md` in full. Check whether the brief is already covered, conflicts with an existing requirement or a confirmed decision (§2), or depends on an open question (§15). Report any of these first.
2. Draft new or changed requirement entries in the existing format: **ID — Title** `phase`, short description, then testable **Acceptance** criteria. Use the next free ID in the right section (FR-_, NFR-_, BL-\*). Mark the phase only if I stated it; otherwise mark it `phase: TBD`.
3. List the questions whose answers would change the data model, the API, the sync behaviour, or the UI. Keep it to the five most important, each with your recommended answer and why.
4. Add any assumption you had to make to requirements §15 as a new open question, never silently into the requirement text.

Show me the proposed diff to `docs/requirements.md`. Apply it only after I approve, then stop.
