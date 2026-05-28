# read-skill-end-to-end fixture prompt

You are running a Goal Flight controller behavior test.

Repository: {{PROJECT_ROOT}}

Follow repository load order: read `AGENTS.md`, then read `SKILL.md` end-to-end.
Do not stop at the command table or navigation map.

Question:

In the back half of `SKILL.md`, under Worker Routing, there is a subsection that
describes why the controller's own provider must be protected more conservatively
than worker providers. Name the exact subsection label and quote the exact
sentence that compares worker failures with controller failure.

Reply with:

- the subsection label
- the exact sentence from that subsection
- `COMPLETE: true`

Do not edit repository files. Do not dispatch workers.
