# PART: evidence-gate v0.3.2 (parts@09b50d1)
from .gate_node import EvidenceGateNode, ProvenanceContract, validate_provenance
from .projection import FindingField, project_typed_finding, project_typed_findings
from .strict import StrictCoveragePolicy, find_uncited_numeric_or_comparative_claims

__all__ = [
    "EvidenceGateNode",
    "FindingField",
    "ProvenanceContract",
    "StrictCoveragePolicy",
    "find_uncited_numeric_or_comparative_claims",
    "project_typed_finding",
    "project_typed_findings",
    "validate_provenance",
]
