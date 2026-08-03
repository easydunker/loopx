"""Turn-scoped capability evidence required before result promotion."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from ...authority import validate_public_safe_text
from .result_records import validate_turn_result_record

TURN_CAPABILITY_OBSERVATION_SCHEMA_VERSION = "turn_capability_observation_v0"
TURN_PROMOTION_ATTESTATION_SCHEMA_VERSION = "turn_promotion_attestation_v0"
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
OBSERVATION_FIELDS = {
    "schema_version",
    "capability",
    "provider_id",
    "verification_kind",
    "evidence_ref",
}
ATTESTATION_FIELDS = {
    "schema_version",
    "attestation_id",
    "result_record_id",
    "turn_key",
    "required_capabilities",
    "capability_observations",
    "authority_evidence",
    "attested_effect_ids",
}
AUTHORITY_EVIDENCE_FIELDS = {
    "source",
    "action_revision",
    "action_signature_hash",
    "boundary_hash",
    "required_write_scopes",
    "allowed_write_scopes",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _token(label: str, value: Any) -> str:
    text = str(value or "").strip()
    if not TOKEN_RE.fullmatch(text):
        raise ValueError(f"{label} must be one public-safe token")
    return text


def _tokens(label: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a list")
    items = [_token(label, item) for item in value]
    if len(items) != len(set(items)):
        raise ValueError(f"{label} must be unique")
    return sorted(items)


def _scopes(label: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a list")
    scopes: list[str] = []
    for item in value:
        text = str(item or "").strip()
        validate_public_safe_text(label, text)
        if not text or text.startswith(("/", "~", "file:")) or ".." in text.split("/"):
            raise ValueError(f"{label} must contain repository-relative public scopes")
        scopes.append(text)
    if len(scopes) != len(set(scopes)):
        raise ValueError(f"{label} must be unique")
    return sorted(scopes)


def _scope_is_authorized(required: str, allowed: str) -> bool:
    if allowed in {"*", "**"} or required == allowed:
        return True
    if allowed.endswith("/**"):
        prefix = allowed[:-3].rstrip("/")
        return required == prefix or required.startswith(prefix + "/")
    return False


def normalize_turn_capability_observation(value: Mapping[str, Any]) -> dict[str, str]:
    """Validate one public-safe, Turn-scoped provider observation."""

    observation = dict(value)
    unknown = sorted(set(observation) - OBSERVATION_FIELDS)
    if unknown:
        raise ValueError(
            "unsupported capability observation fields: " + ", ".join(unknown)
        )
    if observation.get("schema_version") != TURN_CAPABILITY_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("unsupported capability observation schema")
    evidence_ref = str(observation.get("evidence_ref") or "").strip()
    validate_public_safe_text("capability_observation.evidence_ref", evidence_ref)
    if (
        not evidence_ref
        or len(evidence_ref) > 240
        or evidence_ref.startswith(("/", "~", "file:"))
    ):
        raise ValueError(
            "capability observation evidence_ref must be a bounded public reference"
        )
    return {
        "schema_version": TURN_CAPABILITY_OBSERVATION_SCHEMA_VERSION,
        "capability": _token(
            "capability observation capability", observation.get("capability")
        ),
        "provider_id": _token(
            "capability observation provider_id", observation.get("provider_id")
        ),
        "verification_kind": _token(
            "capability observation verification_kind",
            observation.get("verification_kind"),
        ),
        "evidence_ref": evidence_ref,
    }


def build_turn_promotion_attestation(
    plan: Mapping[str, Any],
    result_record: Mapping[str, Any],
    capability_observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind explicit capability observations and boundary authority to one result."""

    result_validation = validate_turn_result_record(result_record)
    if not result_validation["ok"]:
        raise ValueError("; ".join(result_validation["errors"]))
    envelope = _mapping(plan.get("turn_envelope"))
    boundary = _mapping(envelope.get("boundary"))
    if not isinstance(boundary.get("capability_gate"), Mapping):
        raise ValueError("Turn promotion requires an explicit capability gate")
    gate = _mapping(boundary.get("capability_gate"))
    action = _mapping(envelope.get("action"))
    selected_todo = _mapping(action.get("selected_todo"))
    signature = _mapping(envelope.get("action_signature"))

    required = _tokens(
        "capability gate required_capabilities",
        gate.get("required_capabilities"),
    )
    available = _tokens(
        "capability gate available_capabilities",
        gate.get("available_capabilities"),
    )
    missing = _tokens(
        "capability gate missing_capabilities",
        gate.get("missing_capabilities"),
    )
    if (
        gate.get("action") != "run"
        or missing
        or not set(required).issubset(available)
    ):
        raise ValueError("Turn promotion capability gate is not runnable")

    observations = [
        normalize_turn_capability_observation(item) for item in capability_observations
    ]
    observations.sort(key=lambda item: item["capability"])
    observed = [item["capability"] for item in observations]
    if len(observed) != len(set(observed)):
        raise ValueError("Turn promotion capability observations must be unique")
    if observed != required:
        raise ValueError(
            "Turn promotion requires one explicit observation for every required capability"
        )

    required_scopes = _scopes(
        "selected todo required_write_scopes",
        selected_todo.get("required_write_scopes"),
    )
    allowed_scopes = _scopes("boundary write_scope", boundary.get("write_scope"))
    unauthorized = [
        scope
        for scope in required_scopes
        if not any(_scope_is_authorized(scope, allowed) for allowed in allowed_scopes)
    ]
    if unauthorized:
        raise ValueError(
            "Turn promotion required write scopes exceed boundary authority: "
            + ", ".join(unauthorized)
        )

    action_revision = str(result_record.get("based_on_revision") or "")
    source_hash = str(signature.get("source_hash") or "")
    source_decision_hash = str(signature.get("source_decision_hash") or source_hash)
    if (
        signature.get("matches") is not True
        or not source_hash
        or source_decision_hash != action_revision
    ):
        raise ValueError(
            "Turn promotion authority evidence has mismatched action lineage"
        )
    effect_ids = [
        str(_mapping(effect).get("effect_id") or "")
        for effect in result_record.get("proposed_effects") or []
    ]
    document = {
        "schema_version": TURN_PROMOTION_ATTESTATION_SCHEMA_VERSION,
        "result_record_id": result_record["record_id"],
        "turn_key": result_record["turn_key"],
        "required_capabilities": required,
        "capability_observations": observations,
        "authority_evidence": {
            "source": "turn_envelope_action_boundary",
            "action_revision": action_revision,
            "action_signature_hash": source_hash,
            "boundary_hash": _canonical_hash(boundary),
            "required_write_scopes": required_scopes,
            "allowed_write_scopes": allowed_scopes,
        },
        "attested_effect_ids": effect_ids,
    }
    return {**document, "attestation_id": _canonical_hash(document)}


def validate_turn_promotion_attestation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable identity and internal promotion evidence semantics."""

    attestation = dict(value)
    errors: list[str] = []
    unknown = sorted(set(attestation) - ATTESTATION_FIELDS)
    if unknown:
        errors.append(
            "unsupported Turn promotion attestation fields: " + ", ".join(unknown)
        )
    if attestation.get("schema_version") != TURN_PROMOTION_ATTESTATION_SCHEMA_VERSION:
        errors.append("unsupported Turn promotion attestation schema")
    attestation_id = str(attestation.pop("attestation_id", "") or "")
    if not attestation_id or attestation_id != _canonical_hash(attestation):
        errors.append(
            "Turn promotion attestation_id does not match its canonical content"
        )
    try:
        required = _tokens("required_capabilities", value.get("required_capabilities"))
        observations_value = value.get("capability_observations")
        if not isinstance(observations_value, list):
            raise TypeError("capability_observations must be a list")
        observations = [
            normalize_turn_capability_observation(_mapping(item))
            for item in observations_value
        ]
        observed = [item["capability"] for item in observations]
        if observed != required or len(observed) != len(set(observed)):
            raise ValueError(
                "capability observations do not exactly cover requirements"
            )
        authority = _mapping(value.get("authority_evidence"))
        unknown_authority = sorted(set(authority) - AUTHORITY_EVIDENCE_FIELDS)
        if unknown_authority:
            raise ValueError(
                "unsupported authority evidence fields: " + ", ".join(unknown_authority)
            )
        required_scopes = _scopes(
            "authority_evidence.required_write_scopes",
            authority.get("required_write_scopes"),
        )
        allowed_scopes = _scopes(
            "authority_evidence.allowed_write_scopes",
            authority.get("allowed_write_scopes"),
        )
        if authority.get("source") != "turn_envelope_action_boundary":
            raise ValueError("authority evidence source is unsupported")
        if not all(
            str(authority.get(field) or "").startswith("sha256:")
            for field in (
                "action_revision",
                "action_signature_hash",
                "boundary_hash",
            )
        ):
            raise ValueError("authority evidence lineage is incomplete")
        if any(
            not any(_scope_is_authorized(scope, allowed) for allowed in allowed_scopes)
            for scope in required_scopes
        ):
            raise ValueError("authority evidence has unauthorized write scopes")
        effect_ids = value.get("attested_effect_ids")
        if (
            not isinstance(effect_ids, list)
            or not all(isinstance(item, str) and item for item in effect_ids)
            or len(effect_ids) != len(set(effect_ids))
        ):
            raise ValueError("attested_effect_ids must be unique public ids")
        if not all(
            str(value.get(field) or "") for field in ("result_record_id", "turn_key")
        ):
            raise ValueError("Turn promotion attestation lineage is incomplete")
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    return {
        "ok": not errors,
        "schema_version": TURN_PROMOTION_ATTESTATION_SCHEMA_VERSION,
        "attestation_id": attestation_id or None,
        "errors": errors,
    }
