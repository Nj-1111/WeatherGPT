from __future__ import annotations

from app.schemas.ceo import CanonicalEvidenceObject
from app.services.variable_registry import validate_semantics


def validated_evidence(evs: list[CanonicalEvidenceObject]) -> tuple[list[CanonicalEvidenceObject], list[str]]:
    accepted: list[CanonicalEvidenceObject] = []
    rejected: list[str] = []
    for evidence in evs:
        ok, reason = validate_semantics(evidence.variable.value, evidence.statistic.value, evidence.unit,
                                        evidence.evidence_class.value, evidence.accumulation_window_hours)
        if ok:
            accepted.append(evidence)
        else:
            rejected.append(f"{evidence.evidence_id}: {reason}")
    return accepted, rejected
