# COpenClaw Proposal: `continuous_improvement` Task Type

## 1) Goal
Design a first-class task type for hours-to-days autonomous improvement loops that can:
- iterate with bounded autonomy,
- self-evaluate and reflect each cycle,
- checkpoint progress durably,
- recover safely after worker/app failures,
- remain observable and user-controllable.

This proposal is grounded in current COpenClaw architecture:
- Task model and persistence: `src/copenclaw/core/tasks.py`
- Task/tool protocol and dispatch hooks: `src/copenclaw/mcp/protocol.py`
- Scheduler loop and watchdog: `src/copenclaw/core/gateway.py`, `src/copenclaw/core/scheduler.py`
- Worker/supervisor runtime: `src/copenclaw/core/worker.py`
- Event stream observability: `src/copenclaw/core/task_events.py`

## 2) Current architecture constraints that shape the design

1. Existing task statuses are broad (`proposed|pending|running|paused|needs_input|completed|failed|cancelled`) and widely used (`tasks.py`, `protocol.py`, router/tests).
2. Supervisor and watchdog already provide periodic checks and intervention/restart behavior.
3. `on_complete` hooks already chain follow-up work.
4. Recovery of stale tasks already exists (`recovery_pending` flow in `tasks.py` and `gateway.py`).
5. Scheduler already supports recurring jobs and payload dispatch.

Design implication: keep status compatibility and add loop-specific substate/metadata instead of replacing the core lifecycle.

## 3) Proposed data model/schema

### 3.1 Extend `Task` model (minimal-break approach)
Add to `src/copenclaw/core/tasks.py` (`Task` dataclass + `to_dict/from_dict`):

- `task_type: str = "standard"`  # `standard | continuous_improvement`
- `ci_config: dict = field(default_factory=dict)`
- `ci_state: dict = field(default_factory=dict)`

### 3.2 `ci_config` schema (stored on task)

```json
{
  "objective": "Improve test reliability and reduce flaky failures",
  "max_wall_clock_seconds": 172800,
  "max_iterations": 120,
  "iteration_timeout_seconds": 1800,
  "min_iteration_interval_seconds": 60,
  "max_consecutive_failures": 5,
  "max_no_improvement_iterations": 8,
  "quality_gate": {
    "metric": "composite_score",
    "min_delta": 0.02,
    "target_score": 0.90,
    "required_evidence": ["tests", "benchmarks", "diff_summary"]
  },
  "retry_policy": {
    "max_attempts_per_iteration": 3,
    "initial_backoff_seconds": 10,
    "backoff_multiplier": 2.0,
    "max_backoff_seconds": 300,
    "jitter": true
  },
  "safety": {
    "require_supervisor_gate": true,
    "require_human_approval_on": ["scope_expansion", "destructive_change", "budget_exceeded"],
    "max_files_changed_per_iteration": 40,
    "max_commits_per_iteration": 3,
    "allowed_paths": ["OwnCode/src", "OwnCode/tests", "OwnCode/README.md"]
  },
  "resume_policy": "checkpoint_only"
}
```

### 3.3 `ci_state` schema (runtime)

```json
{
  "phase": "execute",
  "started_at": "...",
  "last_iteration_started_at": "...",
  "last_iteration_finished_at": "...",
  "iteration": 12,
  "consecutive_failures": 1,
  "no_improvement_iterations": 2,
  "best_score": 0.78,
  "last_score": 0.77,
  "last_checkpoint_id": "chk-000012",
  "last_checkpoint_at": "...",
  "circuit_open_until": null,
  "stop_reason": ""
}
```

## 4) Lifecycle/state machine

Keep top-level `Task.status` unchanged for compatibility. Add loop `ci_state.phase`:

- `plan` -> `execute` -> `reflect` -> `evaluate` -> (`gate` if needed) -> `checkpoint` -> `execute` (next iteration)
- terminal phases: `succeeded`, `budget_exhausted`, `halted_by_safety`, `failed_unrecoverable`

Top-level mapping:
- active phases map to `status=running`
- user pause maps to `status=paused`
- safety/human gate maps to `status=needs_input`
- terminal success maps to `status=completed`
- terminal failure maps to `status=failed`

This avoids broad breakage in router/protocol/tests while allowing richer loop semantics.

## 5) Iteration loop control

Per iteration controller logic:
1. Validate budgets (`wall_clock`, `iteration_count`) and safety constraints.
2. Run worker objective slice for current iteration.
3. Collect artifacts/evidence (diff stats, test/benchmark outputs, logs).
4. Reflection stage: worker emits what changed, why, and what failed.
5. Evaluation stage: supervisor scores quality delta and risk.
6. Apply gate rules:
   - continue,
   - request human input,
   - stop as completed,
   - stop as failed.
7. Persist checkpoint and schedule next tick (`min_iteration_interval_seconds` + backoff/jitter if needed).

### Stop conditions
- `iteration >= max_iterations`
- `elapsed >= max_wall_clock_seconds`
- `consecutive_failures >= max_consecutive_failures`
- `no_improvement_iterations >= max_no_improvement_iterations`
- safety rule breach
- explicit user cancel

## 6) Reflection/evaluation contract

Use existing `task_report` channel with structured conventions (no protocol break required initially):
- worker `type=progress` with summary prefix `ITERATION_RESULT:` and JSON detail payload
- supervisor `type=assessment` with summary prefix `ITERATION_SCORE:` and JSON detail payload

Recommended detail payload fields:
- `iteration`, `objective_slice`, `changes`, `tests`, `metrics`, `risk_flags`, `proposed_next_step`

In hardening phase, add explicit protocol fields (see Section 10).

## 7) Persistence and checkpoint model

For each continuous task directory (`.tasks/<task_id>/`):
- `ci-checkpoints.jsonl` (append-only)
- `ci-latest-checkpoint.json` (atomic latest pointer)
- `ci-iterations.jsonl` (iteration summaries)

Checkpoint record:

```json
{
  "checkpoint_id": "chk-000012",
  "task_id": "task-...",
  "iteration": 12,
  "phase": "checkpoint",
  "ts": "...",
  "worker_session_id": "...",
  "supervisor_session_id": "...",
  "task_prompt_hash": "...",
  "objective": "...",
  "score": 0.77,
  "best_score": 0.78,
  "artifacts": ["..."],
  "idempotency_keys": ["iter12:test", "iter12:patch"],
  "resume_hint": "continue_from_iteration_13"
}
```

Persistence rules:
- write-ahead: persist intended iteration metadata before execution,
- append-only checkpoint log,
- atomic update of latest pointer,
- resume only from last committed checkpoint.

## 8) Failure and restart recovery semantics

### 8.1 Worker-level failures
- Apply per-iteration retry policy (bounded exponential backoff + jitter).
- Retry only retryable failure classes (timeouts/network/transient tool failures).
- Non-retryable failures immediately increment hard-failure counters.

### 8.2 Circuit breaker
If repeated transient failure rate crosses threshold:
- open circuit (`ci_state.circuit_open_until`),
- pause new iterations,
- request supervisor/human intervention if open duration exceeded.

### 8.3 App restart / stale task recovery
Leverage existing recovery flow in `TaskManager.stale_active_tasks()` and `gateway._notify_stale_tasks()`:
- for `task_type=continuous_improvement`, resume only when checkpoint exists and integrity checks pass,
- otherwise set `needs_input` with explicit remediation message.

### 8.4 Exactly-once vs at-least-once
Guarantee practical at-least-once execution with idempotency guards for side effects; exactly-once cannot be guaranteed across all external systems.

## 9) Observability and user controls

### 9.1 Status surfaces
Enhance `tasks_status` (`protocol.py`) for continuous tasks with:
- `task_type`
- `ci_phase`
- `iteration`
- `best_score`, `last_score`
- `budgets_remaining`
- `consecutive_failures`
- `last_checkpoint_at`
- `stop_reason` (if terminal)

### 9.2 Logs and events
- log each iteration boundary and gate decision into timeline/events.
- use `task_events.py` stream for auditable loop actions.
- optional `tasks_logs(log_type="activity")` remains compatible.

### 9.3 User controls
Reuse existing controls:
- `tasks_send(pause/resume/cancel)` for runtime control,
- `tasks_send(instruction|redirect)` to change objective safely,
- `tasks_status` for live progress.

Hardening addition: `tasks_send(msg_type="priority")` payload convention for budget tuning.

## 10) MCP/API additions and backward compatibility

## 10.1 Extend existing tools (optional fields)
In `src/copenclaw/mcp/protocol.py` schema for `tasks_propose` and `tasks_create`:
- `task_type` enum: `standard | continuous_improvement` (default `standard`)
- `continuous` object: corresponds to `ci_config`

In `tasks_status` response:
- include optional `continuous` block only when `task_type=continuous_improvement`.

## 10.2 Optional new tools (phase 2+)
- `tasks_checkpoint(task_id)` -> force checkpoint
- `tasks_resume_from(task_id, checkpoint_id)` -> deterministic resume point
- `tasks_set_budget(task_id, budget_patch)` -> dynamic budget control

## 10.3 Backward compatibility
- existing clients calling `tasks_create/propose` without new fields behave unchanged.
- existing statuses remain unchanged.
- new response fields are additive and optional.
- existing tests should continue passing with no changes for standard tasks.

## 11) Exact code touchpoints

### Core model/state
- `src/copenclaw/core/tasks.py`
  - `Task` dataclass: add `task_type`, `ci_config`, `ci_state`
  - `to_dict/from_dict`: serialize new fields
  - `create_task(...)`: accept `task_type`, `ci_config`
  - `handle_report(...)`: parse iteration metadata conventions, update `ci_state`

### Protocol/tooling
- `src/copenclaw/mcp/protocol.py`
  - tool schemas: `tasks_propose`, `tasks_create`, `tasks_status`
  - `_tool_tasks_propose`, `_tool_tasks_create`: pass through new fields
  - `_start_task(...)`: bootstrap loop state for continuous tasks
  - `_tool_task_report(...)`: score/gate bookkeeping and terminal logic

### Scheduler and orchestration loops
- `src/copenclaw/core/gateway.py`
  - `_deliver_job(...)`: add payload type `continuous_tick`
  - `_scheduler_loop(...)`: support rescheduling continuous iteration ticks
  - `_watchdog_loop(...)`: integrate with `ci_state` (avoid unsafe restart loops)

- `src/copenclaw/core/scheduler.py`
  - no schema break needed; optional payload validation for `continuous_tick`

### Worker/supervisor runtime
- `src/copenclaw/core/worker.py`
  - worker/supervisor template context can include iteration objective slice
  - preserve current resume-session support and integrate checkpoint-aware prompts

### Observability
- `src/copenclaw/core/task_events.py`
  - keep append-only format; add standardized iteration/gate event summaries

### Config defaults
- `src/copenclaw/core/config.py`
  - optional env defaults for global caps (e.g., max continuous wall clock)

## 12) Implementation roadmap

## Phase 1 - MVP (minimal schema + loop bookkeeping)
Scope:
- add `task_type`, `ci_config`, `ci_state` to Task model.
- extend `tasks_create/propose/status` for new optional fields.
- implement iteration bookkeeping and budget stop conditions through `task_report` conventions.
- add checkpoint files (`ci-checkpoints.jsonl`, latest pointer).

Acceptance criteria:
- can create `continuous_improvement` task via MCP.
- `tasks_status` shows iteration counters/budgets.
- restart resumes from latest committed checkpoint.
- hitting any budget limit results in deterministic terminal reason.

Tests:
- `tests/test_tasks.py`: serialization, budget transitions, state updates.
- `tests/test_e2e_worker.py`: create continuous task and run 2+ iterations.
- `tests/test_task_events.py`: checkpoint event persistence.

## Phase 2 - Hardening (retry classes, circuit breaker, robust recovery)
Scope:
- retry classification + bounded exponential backoff + jitter.
- circuit breaker state in `ci_state`.
- checkpoint integrity validation before resume.
- explicit watchdog interaction rules for continuous tasks.

Acceptance criteria:
- transient failure storms do not cause runaway restart loops.
- circuit opens and later re-closes according to policy.
- corrupted/missing checkpoint triggers `needs_input` instead of unsafe resume.

Tests:
- `tests/test_scheduler.py`: continuous tick scheduling/backoff behavior.
- `tests/test_e2e_worker.py`: crash/restart recovery scenarios.
- add targeted protocol tests for circuit-open states.

## Phase 3 - Advanced (quality scoring and human gates)
Scope:
- formal reflection/evaluation payloads.
- optional dedicated MCP tools (`tasks_checkpoint`, `tasks_set_budget`, `tasks_resume_from`).
- richer dashboard-style `tasks_status` metrics.

Acceptance criteria:
- quality gate can auto-stop when target score reached.
- human approval gate works for configured safety events.
- users can safely tune budget without restarting task.

Tests:
- end-to-end gate workflow tests (worker -> supervisor -> user -> continue/stop).
- regression tests for standard tasks ensure no behavioral drift.

## 13) Reliability targets and confidence limits

Perfect reliability is impossible here because:
1. LLM behavior is non-deterministic and can diverge on replay.
2. External dependencies (network, APIs, filesystem, toolchain) fail unpredictably.
3. Side effects outside COpenClaw cannot always guarantee exactly-once semantics.
4. Host-level failures (power loss, disk corruption, abrupt kill) can occur between writes.
5. Human-in-the-loop delays and ambiguous instructions introduce unavoidable variance.

Practical SLO targets for this task type:
- Control-loop availability: >=99.5% monthly (task can make forward progress when dependencies are healthy).
- Checkpoint durability: >=99.9% successful checkpoint commits.
- Crash recovery RTO: <=10 minutes to resume from last checkpoint.
- Duplicate side-effect rate: <0.5% for guarded operations with idempotency keys.
- Safety response: 100% of hard safety breaches transition to `needs_input` or `failed` within one watchdog interval.

These are realistic for a single-node orchestrator with append-only local persistence and bounded retries.

## 14) Research references (cited)

- AWS Step Functions error handling and retry/backoff/catch: https://docs.aws.amazon.com/step-functions/latest/dg/concepts-error-handling.html
- Azure Durable orchestrations (event sourcing, checkpointing, deterministic replay): https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-orchestrations
- Temporal retry policies and workflow/activity retry guidance: https://docs.temporal.io/encyclopedia/retry-policies
- Azure Retry pattern (idempotency, bounded retries): https://learn.microsoft.com/en-us/azure/architecture/patterns/retry
- Azure Circuit Breaker pattern (open/half-open/closed): https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker
- Airflow best practices (idempotent task outputs across retries): https://airflow.apache.org/docs/apache-airflow/stable/best-practices.html
- Idempotent receiver pattern (duplicate request handling): https://martinfowler.com/articles/patterns-of-distributed-systems/idempotent-receiver.html
- Google SRE on reliability-risk tradeoffs and realistic targets: https://sre.google/sre-book/embracing-risk/
- NIST AI Risk Management Framework (AI RMF 1.0): https://www.nist.gov/itl/ai-risk-management-framework
- NIST AI RMF Generative AI Profile (NIST AI 600-1): https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence
- OECD AI Principles (trustworthy AI governance): https://oecd.ai/en/ai-principles
- EU AI Act Article 14 (human oversight requirements): https://artificialintelligenceact.eu/article/14/

## 15) Recommendation
Implement Phase 1 immediately with additive schema and no status breaking changes, then Phase 2 before enabling autonomous multi-day runs by default.