from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from evidence_gate import (
    EvidenceGateNode,
    FindingField,
    ProvenanceContract,
    StrictCoveragePolicy,
    find_uncited_numeric_or_comparative_claims,
    project_typed_finding,
    project_typed_findings,
    validate_provenance,
)


def _draft(**overrides):
    base = {
        "gate_kind": "draft",
        "status": AgentStatus.SUCCESS.value,
        "cited_source_ids": ["src_1"],
        "retrieved_records": [{"source_id": "src_1", "kind": "event_summary", "source_tier": "stage1"}],
        "evidence_map": [
            {
                "claim_id": "C-01",
                "claim_text": "event is sourced",
                "source_ids": ["src_1"],
                "asserted_attrs": {"src_1": {"kind": "event_summary"}},
            }
        ],
    }
    base.update(overrides)
    return base


def test_call_path_accepts_existing_citations():
    node = EvidenceGateNode()
    result = node({**_draft(), "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["cited_source_ids"] == ["src_1"]


def test_execute_projects_contract_fields_and_never_returns_reducer_managed_state():
    node = EvidenceGateNode()
    result = node.execute(
        {
            **_draft(),
            "node_history": ["upstream"],
            "error_log": ["upstream error"],
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "unrelated": "must not pass",
        }
    )

    assert "node_history" not in result
    assert "error_log" not in result
    assert "unrelated" not in result
    assert result["gate_kind"] == "draft"
    assert result["cited_source_ids"] == ["src_1"]


def test_call_path_appends_only_this_node_to_existing_history():
    node = EvidenceGateNode()
    result = node(
        {
            **_draft(),
            "node_history": ["upstream"],
            "error_log": ["upstream error"],
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )

    assert result["node_history"] == ["EvidenceGateNode"]
    assert "error_log" not in result


def test_fabricated_cited_id_raises_fail_closed():
    with pytest.raises(SecurityViolationError, match="non-existent"):
        validate_provenance(_draft(cited_source_ids=["src_1", "src_fake"]))


def test_evidence_map_source_id_must_exist_and_be_safe():
    bad = _draft(evidence_map=[{"claim_id": "C", "source_ids": ["src ghost"]}])

    with pytest.raises(SecurityViolationError, match="unsafe source id"):
        validate_provenance(bad)


def test_asserted_attrs_must_match_actual_record():
    bad = _draft(
        evidence_map=[
            {"claim_id": "C", "source_ids": ["src_1"], "asserted_attrs": {"src_1": {"kind": "other"}}}
        ]
    )

    with pytest.raises(SecurityViolationError, match="misattributes"):
        validate_provenance(bad)


def test_missing_evidence_fields_raise_on_draft_path():
    with pytest.raises(SecurityViolationError, match="must carry evidence fields"):
        validate_provenance({"gate_kind": "draft", "status": AgentStatus.SUCCESS.value})


@pytest.mark.parametrize(
    "evidence_map",
    ([None], [["nested"]], ["string"]),
    ids=("none-claim", "nested-list-claim", "string-claim"),
)
def test_public_validate_provenance_rejects_non_dict_evidence_map_claims(evidence_map):
    with pytest.raises(SecurityViolationError, match="evidence_map.*dict"):
        validate_provenance(_draft(evidence_map=evidence_map))


@pytest.mark.parametrize(
    "source_ids",
    ("", None, "S-1", ["S-1", 123]),
    ids=("empty-string", "none", "bare-string", "mixed-list"),
)
def test_public_validate_provenance_requires_claim_source_ids_to_be_list_of_strings(source_ids):
    bad = _draft(
        cited_source_ids=["S-1"],
        retrieved_records=[{"source_id": "S-1"}],
        evidence_map=[{"claim_id": "C", "source_ids": source_ids}],
    )

    with pytest.raises(SecurityViolationError, match=r"source_ids.*list\[str\]"):
        validate_provenance(bad)


@pytest.mark.parametrize("asserted_attrs", (None, [], "sensitive-assertion"), ids=("none", "list", "string"))
def test_public_validate_provenance_requires_asserted_attrs_to_be_dict(asserted_attrs):
    bad = _draft(
        evidence_map=[{"claim_id": "C", "source_ids": ["src_1"], "asserted_attrs": asserted_attrs}]
    )

    with pytest.raises(SecurityViolationError, match="asserted_attrs.*dict"):
        validate_provenance(bad)


def test_public_validate_provenance_requires_each_asserted_attrs_value_to_be_dict():
    bad = _draft(
        evidence_map=[
            {"claim_id": "C", "source_ids": ["src_1"], "asserted_attrs": {"src_1": "sensitive-value"}}
        ]
    )

    with pytest.raises(SecurityViolationError, match="asserted_attrs.*dict"):
        validate_provenance(bad)


def test_malformed_claim_error_does_not_echo_llm_value():
    llm_value = "SENSITIVE-LLM-SOURCE"
    bad = _draft(evidence_map=[{"claim_id": "C", "source_ids": llm_value}])

    with pytest.raises(SecurityViolationError) as exc_info:
        validate_provenance(bad)

    assert llm_value not in str(exc_info.value)


def test_public_node_call_rejects_malformed_claim_without_relying_on_prior_validation():
    bad = _draft(
        evidence_map=[{"claim_id": "C", "source_ids": ""}],
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL.value,
    )

    result = EvidenceGateNode()(bad)

    assert result["status"] == AgentStatus.ERROR.value
    assert "source_ids" in result["error_log"][0]
    assert "list[str]" in result["error_log"][0]


def test_well_formed_evidence_map_remains_accepted_by_public_validator():
    draft = _draft()

    assert validate_provenance(draft) is draft


def test_strict_flags_numeric_claims_in_recursive_free_text_without_claim_coverage():
    payload = {"output": {"investigation_report": {"section": "発生件数は42件に増加した。"}}}
    flags = find_uncited_numeric_or_comparative_claims(payload, [])

    assert flags
    assert flags[0]["path"] == "output.investigation_report.section"


def test_strict_does_not_flag_when_numeric_claim_is_covered_by_cited_claim():
    payload = {"output": {"investigation_report": {"section": "発生件数は42件に増加した。"}}}
    evidence_map = [{"claim_text": "発生件数は42件に増加した。", "source_ids": ["src_1"]}]

    assert find_uncited_numeric_or_comparative_claims(payload, evidence_map) == []


def test_strict_flags_arabic_number_with_declared_domain_unit():
    """宣言済みドメイン単位は、アラビア数字と 1 トークンとして切り出される。

    単位語の役割は検出範囲を広げることではなく token 境界を決めること。
    本文の "62デシベル" が covered claim の "62デシベル" と一致し、
    裸の "62" とは一致しないようにするためにある。
    """
    payload = {"output": {"investigation_report": "騒音は62デシベルだった。"}}
    policy = StrictCoveragePolicy(additional_unit_terms=("デシベル",))

    flags = find_uncited_numeric_or_comparative_claims(payload, [], policy=policy)

    assert [flag["text"] for flag in flags] == ["62デシベル"]


def test_strict_nfkc_normalizes_output_and_covered_claim_tokens():
    payload = {"output": {"investigation_report": "発生件数は１２件だった。"}}
    evidence_map = [{"claim_text": "発生件数は12件だった。", "source_ids": ["src_1"]}]

    assert find_uncited_numeric_or_comparative_claims(payload, evidence_map) == []


def test_strict_does_not_treat_12_as_covered_by_112():
    payload = {"output": {"investigation_report": "発生件数は12件だった。"}}
    evidence_map = [{"claim_text": "発生件数は112件だった。", "source_ids": ["src_1"]}]

    flags = find_uncited_numeric_or_comparative_claims(payload, evidence_map)

    assert [flag["text"] for flag in flags] == ["12件"]


def test_strict_treats_exact_12_token_as_covered():
    payload = {"output": {"investigation_report": "発生件数は12件だった。"}}
    evidence_map = [{"claim_text": "発生件数は12件だった。", "source_ids": ["src_1"]}]

    assert find_uncited_numeric_or_comparative_claims(payload, evidence_map) == []


@pytest.mark.parametrize(
    "text",
    (
        # 漢数字
        "騒音は六十二デシベルだった", "不備は三件だった",
        # 日本語の慣用句（漢数字を検出対象にすると軒並み誤検出される）
        "十分な証拠が揃っている", "一部の記録に不備がある", "第三者の確認が必要",
        "万全の体制", "一貫した運用",
        # 英語の数詞・曖昧な数量表現
        "sixteen items were reviewed", "twenty-two cases remain",
        "a dozen records were missing", "several reports were incomplete",
    ),
    ids=(
        "kanji-compound", "kanji-with-unit",
        "idiom-sufficient", "idiom-partial", "idiom-third-party",
        "idiom-fully-prepared", "idiom-consistent",
        "english-sixteen", "english-twenty-two", "english-a-dozen", "english-several",
    ),
)
def test_strict_does_not_detect_lexical_quantity_expressions(text):
    """意図的な非検出。網羅は目標にしない（2026-08-07 確定）。

    数量の言い表し方は言語横断で無限に開いている。漢数字を足しても
    `sixteen` / `a dozen` / `several` は依然として検出できないので、
    部品側で語彙を足しても実効性は上がらず保守負担と誤検出だけが増える。
    実際、漢数字を検出対象にすると「十分」「一部」「第三者」といった
    慣用句が軒並み数値主張として flag され、シグナルが埋もれた。

    このテストは「まだ検出できていない」ではなく「検出しない設計である」ことの記録。
    ここに語彙を足す修正を入れる前に、`parts/TESTING.md` の V-3-bis を読むこと。
    ドメイン固有の数量表現が要るテンプレは `StrictCoveragePolicy` を差し替える。
    """
    policy = StrictCoveragePolicy(additional_unit_terms=("デシベル",))
    assert find_uncited_numeric_or_comparative_claims({"report": text}, [], policy=policy) == []


def test_strict_flags_arabic_number_with_default_unit():
    flags = find_uncited_numeric_or_comparative_claims({"report": "不備は3件だった。"}, [])

    assert [flag["text"] for flag in flags] == ["3件"]


def test_strict_flags_uncited_comparative_terms_without_any_number():
    """比較語だけの主張も flag される（数字が無いケースを単独で固定）。

    数字と比較語を同時に含むテストしか無いと、比較語の検出を丸ごと外す変異が
    数字の側だけで緑になり、素通りする。
    """
    flags = find_uncited_numeric_or_comparative_claims(
        {"report": "リスクは増加した。More cases remain."}, []
    )

    assert sorted(flag["text"] for flag in flags) == ["More", "増加"]


def test_strict_ascii_comparative_is_not_covered_by_a_longer_word():
    """cited claim の 'Moreover' が本文の 'More' を covered 扱いしない。

    ASCII の比較語は単語境界付きで照合する。境界が無いと、無関係な語に
    部分一致した cited claim が本文の実際の比較主張を飲み込む。
    """
    payload = {"report": "More cases remain."}
    evidence_map = [{"claim_text": "Moreover, controls were reviewed.", "source_ids": ["S-1"]}]

    flags = find_uncited_numeric_or_comparative_claims(payload, evidence_map)

    assert [flag["text"] for flag in flags] == ["More"]


def test_strict_still_flags_bare_arabic_number():
    flags = find_uncited_numeric_or_comparative_claims({"report": "測定値は12だった。"}, [])

    assert [flag["text"] for flag in flags] == ["12"]


def test_unknown_cited_ids_are_recorded_in_audit_trace_but_not_exception(monkeypatch):
    events = []
    monkeypatch.setattr(
        "evidence_gate.gate_node.emit_trace_event",
        lambda event, payload, _state: events.append((event, payload)),
    )
    bad = _draft(cited_source_ids=["src_1", "src_fake_z", "src_fake_a"])

    with pytest.raises(SecurityViolationError) as exc_info:
        validate_provenance(bad)

    assert events == [
        ("provenance_violation", {"unknown_ids": ["src_fake_a", "src_fake_z"]})
    ]
    assert "src_fake_a" not in str(exc_info.value)
    assert "src_fake_z" not in str(exc_info.value)


def test_asserted_attr_mismatch_records_source_id_and_keys_only_in_audit_trace(monkeypatch):
    events = []
    monkeypatch.setattr(
        "evidence_gate.gate_node.emit_trace_event",
        lambda event, payload, _state: events.append((event, payload)),
    )
    bad = _draft(
        evidence_map=[
            {
                "claim_id": "C",
                "source_ids": ["src_1"],
                "asserted_attrs": {"src_1": {"kind": "other", "source_tier": "stage2"}},
            }
        ]
    )

    with pytest.raises(SecurityViolationError):
        validate_provenance(bad)

    assert events == [
        (
            "provenance_violation",
            {"source_id": "src_1", "mismatched_keys": ["kind", "source_tier"]},
        )
    ]


def test_execute_emits_projection_event_with_structural_payload_only(monkeypatch):
    events = []
    monkeypatch.setattr(
        "evidence_gate.gate_node.emit_trace_event",
        lambda event, payload, state: events.append((event, payload, state)),
    )
    state = {**_draft(), "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}

    EvidenceGateNode().execute(state)

    assert len(events) == 1
    event, payload, emitted_state = events[0]
    assert event == "provenance_gate_projected"
    assert payload == {
        "gate_kind": "draft",
        "projected_keys": [
            "cited_source_ids",
            "evidence_map",
            "gate_kind",
            "retrieved_records",
            "status",
        ],
        "cited_id_count": 1,
        "retrieved_record_count": 1,
        "evidence_claim_count": 1,
    }
    # The full state is handed to the emitter so the record carries
    # trace_id / correlation_id / session_id, but nothing from it is copied
    # into the payload: no claim text, no source IDs, no draft body.
    assert emitted_state is state
    assert "event is sourced" not in str(payload)
    assert "src_1" not in str(payload)


def test_accepted_draft_still_produces_a_domain_event_on_the_call_path(monkeypatch):
    """The S-3 hook emits only on violation; a clean draft must still be audited."""
    events = []
    monkeypatch.setattr(
        "evidence_gate.gate_node.emit_trace_event",
        lambda event, payload, state: events.append(event),
    )

    result = EvidenceGateNode()({**_draft(), "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert events == ["provenance_gate_projected"]


@pytest.mark.parametrize(
    ("observed_kind", "reported"),
    [
        ("draft", "draft"),
        ("ingest", "ingest"),
        ("feedback", "feedback"),
        ("draft\nInjected: line", "unknown"),
        (None, "unknown"),
    ],
)
def test_projection_event_folds_gate_kind_into_a_bounded_vocabulary(monkeypatch, observed_kind, reported):
    events = []
    monkeypatch.setattr(
        "evidence_gate.gate_node.emit_trace_event",
        lambda event, payload, state: events.append(payload),
    )

    EvidenceGateNode().execute(_draft(gate_kind=observed_kind))

    assert events[0]["gate_kind"] == reported


def test_projection_event_vocabulary_follows_the_contract_not_the_literal_draft(monkeypatch):
    """`gate_kind` is an adaptation point: the accepted value is the contract's own.

    A template that declares `gate_kind="brief"` reports `brief`, and the shipped
    default `draft` becomes `unknown` for it -- the bounded vocabulary is
    (contract.gate_kind, "ingest", "feedback"), not the literal "draft".
    """
    events = []
    monkeypatch.setattr(
        "evidence_gate.gate_node.emit_trace_event",
        lambda event, payload, state: events.append(payload["gate_kind"]),
    )
    node = EvidenceGateNode(contract=ProvenanceContract(gate_kind="brief"))

    for observed in ("brief", "ingest", "feedback", "draft"):
        node.execute(_draft(gate_kind=observed))

    assert events == ["brief", "ingest", "feedback", "unknown"]


def test_projection_event_reports_none_rather_than_a_string_length(monkeypatch):
    events = []
    monkeypatch.setattr(
        "evidence_gate.gate_node.emit_trace_event",
        lambda event, payload, state: events.append(payload),
    )

    EvidenceGateNode().execute(_draft(cited_source_ids="src_1,src_2", retrieved_records=None))

    assert events[0]["cited_id_count"] is None
    assert events[0]["retrieved_record_count"] is None
    assert events[0]["evidence_claim_count"] == 1


_FINDING_FIELDS = (
    FindingField("description", str, required=True),
    FindingField("cited_source_ids", list, required=True, item_type=str),
    FindingField("severity", str, allowed_values=("candidate", "review")),
    FindingField("rank", int),
    FindingField("confidence", (int, float)),
    FindingField("requires_human_review", bool),
)


def test_typed_finding_projection_accepts_declared_normal_values_in_field_order():
    projected = project_typed_finding(
        {
            "requires_human_review": True,
            "confidence": 0.75,
            "rank": 2,
            "severity": "candidate",
            "cited_source_ids": ["src_1", "src_2"],
            "description": "Evidence requires review.",
        },
        _FINDING_FIELDS,
    )

    assert projected == {
        "description": "Evidence requires review.",
        "cited_source_ids": ["src_1", "src_2"],
        "severity": "candidate",
        "rank": 2,
        "confidence": 0.75,
        "requires_human_review": True,
    }


def test_typed_findings_projection_accepts_optional_fields_being_absent():
    projected = project_typed_findings(
        [{"description": "One", "cited_source_ids": []}],
        _FINDING_FIELDS,
    )

    assert projected == [{"description": "One", "cited_source_ids": []}]


def test_typed_finding_projection_rejects_undeclared_key_without_echoing_it_or_its_value():
    unknown_key = "raw_document_text"
    secret_value = "SENSITIVE-RAW-SENTINEL"

    with pytest.raises(SecurityViolationError) as exc_info:
        project_typed_finding(
            {
                "description": "Review",
                "cited_source_ids": ["src_1"],
                unknown_key: secret_value,
            },
            _FINDING_FIELDS,
        )

    assert unknown_key not in str(exc_info.value)
    assert secret_value not in str(exc_info.value)


def test_typed_finding_projection_rejects_nested_dict_in_declared_string_field():
    with pytest.raises(SecurityViolationError, match=r"description.*str.*dict"):
        project_typed_finding(
            {
                "description": {"raw_document_text": "SENSITIVE-RAW-SENTINEL"},
                "cited_source_ids": ["src_1"],
            },
            _FINDING_FIELDS,
        )


def test_typed_finding_projection_rejects_non_string_direct_list_item():
    with pytest.raises(SecurityViolationError, match=r"cited_source_ids.*str.*dict"):
        project_typed_finding(
            {
                "description": "Review",
                "cited_source_ids": [{"raw": "SENSITIVE-RAW-SENTINEL"}],
            },
            _FINDING_FIELDS,
        )


def test_typed_finding_projection_rejects_missing_required_field():
    with pytest.raises(SecurityViolationError, match=r"required field 'description'.*missing"):
        project_typed_finding({"cited_source_ids": []}, _FINDING_FIELDS)


def test_typed_finding_projection_rejects_enum_value_without_echoing_it():
    llm_value = "SENSITIVE-UNDECLARED-SEVERITY"

    with pytest.raises(SecurityViolationError) as exc_info:
        project_typed_finding(
            {
                "description": "Review",
                "cited_source_ids": [],
                "severity": llm_value,
            },
            _FINDING_FIELDS,
        )

    assert llm_value not in str(exc_info.value)


def test_typed_finding_projection_does_not_accept_bool_as_int():
    with pytest.raises(SecurityViolationError, match=r"rank.*int.*bool"):
        project_typed_finding(
            {
                "description": "Review",
                "cited_source_ids": [],
                "rank": True,
            },
            _FINDING_FIELDS,
        )


@pytest.mark.parametrize("value", ({}, "finding", None), ids=("dict", "string", "none"))
def test_typed_findings_projection_requires_a_list(value):
    with pytest.raises(SecurityViolationError, match="findings must be list"):
        project_typed_findings(value, _FINDING_FIELDS)


@pytest.mark.parametrize("value", (None, "finding", []), ids=("none", "string", "list"))
def test_typed_finding_projection_requires_dict_items(value):
    with pytest.raises(SecurityViolationError, match="finding must be dict"):
        project_typed_finding(value, _FINDING_FIELDS)


def test_finding_field_item_type_requires_a_list_value_contract():
    with pytest.raises(ValueError, match="item_type requires expected_type=list"):
        FindingField("description", str, item_type=str)
    with pytest.raises(ValueError, match="item_type requires expected_type=list"):
        FindingField("ambiguous", (list, str), item_type=str)


def test_finding_projection_rejects_empty_or_duplicate_contracts():
    with pytest.raises(ValueError, match="at least one field"):
        project_typed_finding({}, ())
    with pytest.raises(ValueError, match="unique"):
        project_typed_finding(
            {"description": "Review"},
            (FindingField("description", str), FindingField("description", str)),
        )


def test_part_modules_compile_without_escape_sequence_warnings():
    """Every module in this part must compile clean under -W error.

    `strict.py` documents the detector using `\\d` inside its module docstring.
    Written as a plain string that is an invalid escape sequence: a
    DeprecationWarning today, a SyntaxWarning on newer interpreters, and a hard
    SyntaxError under `-W error` -- which is how CI configurations and future
    Python versions will see it. The docstring must stay raw.

    This is checked by compiling in a subprocess with warnings promoted to
    errors, because importing the module here would not re-trigger the warning
    once a .pyc is cached.
    """
    import subprocess
    import sys
    from pathlib import Path

    # Resolve through the import, not the directory layout. This test file is
    # copied verbatim into adopting repos as tests/parts/evidence-gate/, where a
    # path relative to __file__ would point at a directory that does not exist.
    import evidence_gate

    part_src = Path(evidence_gate.__file__).resolve().parent
    modules = sorted(part_src.glob("*.py"))
    assert modules, "no modules found to compile"

    for module in modules:
        completed = subprocess.run(
            [sys.executable, "-W", "error::SyntaxWarning", "-W", "error::DeprecationWarning",
             "-c", f"import py_compile; py_compile.compile({str(module)!r}, doraise=True, cfile=None)"],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, (
            f"{module.name} does not compile cleanly under -W error:\n{completed.stderr}"
        )

