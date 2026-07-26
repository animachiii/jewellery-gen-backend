from app.models.enums import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    V1_SERVICES,
    V2_SERVICES,
    ErrorCode,
    JobStatus,
    ServiceType,
)

# needs_input is terminal for API purposes but is resumable via POST /jobs/{id}/resolve
# (docs/schema.md §1) — it is the sole exception to "terminal states have no successors".
RESUMABLE_TERMINAL_STATUSES = frozenset({JobStatus.NEEDS_INPUT})


def test_no_status_maps_to_itself() -> None:
    for status, successors in LEGAL_TRANSITIONS.items():
        assert status not in successors


def test_fully_terminal_statuses_have_no_successors() -> None:
    for status in TERMINAL_STATUSES - RESUMABLE_TERMINAL_STATUSES:
        assert LEGAL_TRANSITIONS[status] == frozenset()


def test_needs_input_resumes_via_resolving_only() -> None:
    assert LEGAL_TRANSITIONS[JobStatus.NEEDS_INPUT] == frozenset({JobStatus.RESOLVING})


def test_non_terminal_statuses_have_a_successor() -> None:
    for status in JobStatus:
        if status not in TERMINAL_STATUSES:
            assert len(LEGAL_TRANSITIONS[status]) > 0


def test_job_status_values_match_schema() -> None:
    assert JobStatus.NEEDS_INPUT.value == "needs_input"
    expected = {
        "queued",
        "classifying",
        "resolving",
        "submitting",
        "generating",
        "storing",
        "succeeded",
        "failed",
        "needs_input",
        "needs_review",
    }
    assert {s.value for s in JobStatus} == expected


def test_v1_and_v2_services_partition_service_type() -> None:
    assert V1_SERVICES | V2_SERVICES == set(ServiceType)
    assert V1_SERVICES.isdisjoint(V2_SERVICES)


def test_error_code_count_matches_schema() -> None:
    assert len(list(ErrorCode)) == 17
