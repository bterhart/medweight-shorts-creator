# Working rules for this repo (set by the repo owner - follow them every session)

- One thing at a time. When walking through server, deploy, or debugging steps, give exactly one
  command or one action, wait for its output, then give the next. Never batch steps. Never pre-answer
  "if X then Y" branches - ask for the output that decides it.
- Do not assume state on the server, in the database, in AWS, or in the UI. If it matters, ask for the
  exact output that shows it before acting on it.
- Propose code; do not implement until directed. Acknowledge directions and ask for clarification when a
  request is ambiguous.
- Before declaring any change done: read every touched file in full (not just the diff), trace every
  touchpoint (schema, worker, API, UI, docs, .env.example, deployment doc, IAM/infra notes), and verify -
  syntax at minimum, an actual run where one is possible.
- Never amend or force-push published commits; follow-up fixes are new commits.
