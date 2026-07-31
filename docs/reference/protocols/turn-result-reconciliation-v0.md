# Turn Result and Reconciliation v0

Status: experimental additive contract.

`turn_result_record_v0` separates what one governed host proposes from what
LoopX later commits. `turn_reconciliation_receipt_v0` records how a mechanical
reconciler compared that proposal with current canonical state.

The split is:

```text
host candidate
    -> immutable result record
    -> independent validation
    -> reconciliation receipt
    -> canonical todo/state/quota/scheduler effects
```

This version writes the result record and a `not_attempted` or `not_required`
receipt into both the existing per-Turn journal and a goal-scoped append-only
runtime ledger. The default `shadow` mode keeps the existing direct writeback
path and appends a separate `shadow_match` or `shadow_conflict` observation.
The opt-in `enforce` mode checks the live action revision before the first
effect, resumes only from its journaled checked revision, and appends an
`applied` receipt after the complete effect sequence. The callbacks remain the
canonical effect providers; semantic review remains a later stage.

Use `--reconciliation-mode enforce` to opt in. If a Turn stops with
`reconciliation_blocked`, rerun that exact Turn with
`--reconciliation-mode shadow` to use the explicit rollback path. Shadow
remains the default so existing callers and benchmark adapters do not change
behavior implicitly.

## Why Two Records

A host result is evidence, not authority. It can describe validated progress,
request repair, or request replan, but it cannot prove that the selected todo
and goal state are still at the revision on which the host acted.

The result record therefore freezes:

- the Turn, goal, agent, and todo lineage;
- the action revision observed before host execution;
- the normalized public-safe candidate result; and
- the exact effects that the candidate proposes.

The reconciliation receipt separately freezes:

- the expected and observed revisions;
- the proposed effects considered;
- the subset actually applied; and
- the mechanical disposition.

Keeping these records separate allows retries and semantic escalation without
rewriting the original host claim.

## `turn_result_record_v0`

```json
{
  "schema_version": "turn_result_record_v0",
  "record_id": "sha256:<canonical-record-content>",
  "turn_key": "sha256:<turn-transaction-identity>",
  "lineage": {
    "goal_id": "example-goal",
    "agent_id": "example-worker",
    "todo_id": "todo_example0001"
  },
  "based_on_revision": "sha256:<quota-decision-or-action-signature>",
  "action_signature": {
    "coverage": "turn_envelope_action_dimensions_v0",
    "source_hash": "sha256:<action-signature>"
  },
  "result_kind": "validated_progress",
  "candidate_result": {
    "schema_version": "loopx_turn_result_v0",
    "turn_key": "sha256:<turn-transaction-identity>",
    "result_kind": "validated_progress",
    "completed_phases": ["host_execute", "typed_result"],
    "classification": "validated_progress",
    "recommended_action": "Continue the selected todo",
    "next_action": "Run the next bounded validation",
    "delivery_batch_scale": "implementation",
    "delivery_outcome": "outcome_progress",
    "path_delta_mode": "unchanged",
    "vision_unchanged_reason": "The accepted goal path is unchanged."
  },
  "proposed_effects": [
    {
      "effect_id": "sha256:<turn-kind-target-payload>",
      "kind": "goal_state_refresh",
      "target": "goal:example-goal",
      "payload": {
        "agent_id": "example-worker",
        "classification": "validated_progress"
      }
    },
    {
      "effect_id": "sha256:<turn-kind-target-payload>",
      "kind": "quota_spend",
      "target": "goal:example-goal",
      "payload": {
        "agent_id": "example-worker",
        "slots": 1
      }
    },
    {
      "effect_id": "sha256:<turn-kind-target-payload>",
      "kind": "scheduler_reconcile",
      "target": "goal:example-goal",
      "payload": {
        "agent_id": "example-worker"
      }
    }
  ]
}
```

### Identity and Immutability

- `record_id` is the SHA-256 digest of the canonical JSON document excluding
  `record_id`.
- Rebuilding the record from the same normalized candidate and Turn plan must
  produce the same byte-independent identity.
- `turn_key` binds host, execution mode, scheduler owner, session action, and
  goal/agent/todo/action lineage through the existing transaction contract.
- `based_on_revision` uses
  `action_signature.source_decision_hash` when available. Older compatible
  envelopes use `action_signature.source_hash`.
- A changed candidate, action revision, lineage value, or proposed effect must
  produce a different `record_id`.
- A result ledger must append a new record. It must never replace a record with
  the same `record_id`.

### Proposed Effect Vocabulary

| Kind | When proposed | Target |
| --- | --- | --- |
| `todo_note_update` | `repair_required` or `replan_required` | Current todo |
| `todo_complete` | `validated_completion` when a lifecycle adapter supports it | Current todo |
| `goal_state_refresh` | Every material result | Current goal |
| `quota_spend` | Every material result, after validated writeback | Current goal |
| `scheduler_reconcile` | Every material result, after spend handling | Current goal |

`wait` and `user_action_required` propose no canonical effects. Failure results
that do not contain a schema-valid normalized host candidate do not create a
result record.

Effect ids are deterministic hashes of `turn_key`, effect kind, target, and
payload. They are idempotency identities, not grants of authority. A
reconciler still checks validation, current revision, todo lifecycle, write
scope, capabilities, user gates, and repository policy.

## `turn_reconciliation_receipt_v0`

```json
{
  "schema_version": "turn_reconciliation_receipt_v0",
  "receipt_id": "sha256:<canonical-receipt-content>",
  "result_record_id": "sha256:<canonical-record-content>",
  "turn_key": "sha256:<turn-transaction-identity>",
  "expected_revision": "sha256:<revision-observed-before-host>",
  "observed_revision": null,
  "status": "not_attempted",
  "proposed_effect_ids": [
    "sha256:<effect-id>"
  ],
  "applied_effect_ids": [],
  "reason": "legacy_direct_writeback_not_reconciled"
}
```

`receipt_id` is the SHA-256 digest of the canonical JSON document excluding
`receipt_id`. Receipts are append-only observations. A later shadow or
enforcement receipt does not mutate an earlier `not_attempted` receipt.

Allowed statuses:

| Status | Meaning |
| --- | --- |
| `not_attempted` | The candidate has effects, but no v0 reconciler evaluated them. |
| `not_required` | The typed result has no proposed canonical effects. |
| `shadow_match` | Shadow reconciliation predicts the existing write path would apply the same effects. |
| `shadow_conflict` | Shadow reconciliation predicts different effects or ordering. |
| `applied` | Every proposed effect was applied after validation and revision checks. |
| `already_applied` | Every proposed effect was already present by idempotency identity. |
| `revision_conflict` | Canonical state no longer matches `expected_revision`. |
| `semantic_review_required` | Mechanical rules cannot safely resolve the candidate. |
| `rejected` | Validation, lineage, authority, or another fail-closed rule rejected the candidate. |

`applied` and `already_applied` require `applied_effect_ids` to cover every
`proposed_effect_id`. `not_required` is valid only when the proposed effect set
is empty. Applied ids must always be a subset of the result record's proposed
ids.

## Current Behavior Characterization

The per-Turn journal and exclusive sibling lock remain authoritative for
transaction recovery. The goal-scoped result ledger independently preserves
validated result and reconciliation observations:

| Scenario | Required observation |
| --- | --- |
| Same Turn called concurrently | One caller performs host, writeback, spend, and scheduler callbacks; the other replays the committed journal. |
| Process exits after validation but before writeback | The normalized host result and immutable result record remain journaled; resume does not reinvoke the host. |
| Process exits after writeback but before spend | Resume reuses the journaled writeback phase and does not repeat it. |
| Committed Turn replay | The same result and reconciliation record ids are returned with zero new effects or ledger rows. |
| Task validation retry | The host candidate and result record remain stable while validation is retried. |
| Direct path completes | Shadow reconciliation appends a `shadow_match` receipt covering the proposed effect sequence. |
| Direct path stops partway | Shadow reconciliation appends a `shadow_conflict` receipt covering only the observed effect prefix. |
| Enforced path sees the expected revision | The journal freezes the observed revision before effects and appends an `applied` receipt only after the complete effect sequence. |
| Enforced path sees a stale revision | No callback runs; a `revision_conflict` receipt is appended and the Turn becomes `reconciliation_blocked`. |
| Enforced path resumes after a committed phase | The journaled checked revision is reused and already completed callbacks are not repeated. |
| Operator rolls back a blocked enforced Turn | The same journal resumes in `shadow` mode; the conflict receipt remains immutable evidence. |

The journal remains a mutable transaction checkpoint. The ledger validates
every row before append, rejects dangling receipts and duplicate identities,
and reuses byte-equivalent record ids on replay. A later shadow receipt never
replaces the initial receipt or an earlier partial-path shadow observation.

## Promotion Sequence

1. **Contract and characterization**: emit deterministic records in the
   existing journal and cover concurrency, crash recovery, replay, and
   tamper-detection behavior. Keep direct writeback unchanged.
2. **Ledger and shadow**: append records and receipts to a dedicated
   immutable ledger; compare mechanical reconciliation with direct writeback.
3. **Mechanical enforcement (current, opt-in)**: check the live action revision
   before effects, rely on journal phase identities to avoid repeating
   completed callbacks, record the final applied effect set, and retain
   `shadow` as the explicit rollback control.
4. **Semantic escalation**: invoke bounded semantic review only for typed
   mechanical ambiguity.

No stage may infer authority from a result or receipt. Benchmark launch,
production effects, credentials, merge authority, and public publishing remain
separate explicit gates.
