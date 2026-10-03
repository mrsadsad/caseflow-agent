# CaseFlow internal implementation contract

Independent Python 3.11+ source project, stdlib runtime. All files under `caseflow/`.

## Store API (store.py, domain implementation)

- `Store(path)` creates SQLite schema; `seed()` inserts fixtures idempotently (never resets mutations).
- `list_cases()` / `get_case(case_id)` return dictionaries; cases contain `id,title,request,tenant_id,customer_id,order_id` (order_id may be null).
- `create_run(case_id, mode, max_steps=12)` returns a run dictionary with `id,case_id,mode,status,revision,state`. Initial `status=ready`, `state={messages:[], steps:0, tokens:0, usage_complete:True, max_steps:...}`.
- `get_run(run_id)` / `list_runs()`; `save_run(run_id, expected_revision, state, status)` CAS updates and returns run; raises `Conflict` on stale revision.
- `add_event(run_id, kind, payload)`; `events(run_id, after=0)` returns `id,kind,payload` dictionaries. No credentials or private model reasoning in events.
- `recover()` marks queued `ready` and executing `running` runs `interrupted` after service restart. Single active process per database is a documented constraint.
- `approve(proposal_id, approved: bool)` validates exact current proposal, versions and amount, then atomically creates unique outbox row and changes run status to `queued`; rejected => `rejected`.
- `dispatch(fail_after_effect=False)` processes at most one pending outbox item, writes local refund through a UNIQUE idempotency key, then acknowledges outbox and run; injected crash happens between these transactions. Returns a dictionary or None. A failed/repeated delivery must never double refund.
- `proposals(run_id=None)`, `outbox()`, `ledger()` return dictionaries. `snapshot()` returns dashboard counts (optional).
- `Conflict`, `DomainError` are public exceptions. `DomainError` text is safe for UI.

## Tools API (tools.py, domain implementation)

`TOOL_SCHEMAS`: Chat Completions function schemas. `Tools(store, run_id).execute(name, arguments)` returns JSON dictionary; validation/domain errors return `{error: code, detail: ...}` (no exception for model mistakes).

Names: `get_order(order_id)`, `search_policy(query)`, `propose_refund(order_id, amount_cents, reason, policy_ids)`, `ask_customer(question)`, `escalate(reason)`, `finish(answer, policy_ids)`.

All scope comes from stored case; model cannot choose tenant/customer. `search_policy` filters tenant and active version before ranking. Results include source IDs/version/text. Proposal validates source was retrieved in this run, amount, ownership, refund window/order state and versions. Tool success returns `terminal_status` for propose (`awaiting_approval`), ask (`needs_input`), escalate (`escalated`), finish (`completed`). No tool may approve or dispatch. Tool calls/events remain persisted for evidence.

## Runtime API (runtime.py, root implementation)

`Runtime(store, provider=None).run(run_id)` runs ready/interrupted tasks, persists model request reservation before network and result after. Demo is a clearly labeled deterministic policy. Live provider has `complete(messages, tools) -> {message, tokens}`. Bounded loop, persisted messages and steps; max budget enforced on resume. Unknown request outcome has usage_complete false; no hidden retries.

## UI / HTTP API (server.py, UI implementation)

`serve(store, runtime_factory, host='127.0.0.1', port=8780)`; provide `make_server(store, runtime_factory, host='127.0.0.1', port=8780)` returning HTTPServer for tests. `runtime_factory()` returns Runtime. ThreadPoolExecutor(2) and at most eight active tasks; reject extra submissions before creating run.

- GET `/api/bootstrap`: `{cases, runs, proposals, outbox, ledger}`
- GET `/api/runs/{id}`: `{run, events, proposals}`
- POST `/api/runs`: `{case_id, mode:'demo'|'live', max_steps:12}` starts task
- POST `/api/runs/{id}/resume`: resume interrupted/error/budget exhausted only if Runtime allows (no budget reset)
- POST `/api/proposals/{id}/decision`: `{approved:boolean}`; approval queues local simulator
- POST `/api/dispatch`: process one outbox item with normal dispatch
- POST `/api/dispatch-crash`: intentional local simulation `dispatch(fail_after_effect=True)`

UI uses polling, safe text rendering, persistent history, approval proposal details (amount, policy, order version), event timeline, ledger/outbox crash exercise. Clearly distinguish fixed demo and real model. Local-only bind, strict Host/Origin checks for writes, JSON content-type and body bounds, no remote services or public authentication. No secrets in browser. No framework dependency required.

Root owns provider.py, runtime.py, evaluation.py, __main__.py, project metadata/docs/runtime+eval tests. Domain agent owns store.py, tools.py, fixtures.py and domain tests. UI agent owns server.py, web/index.html and server tests. Coordinate API deviations first.
