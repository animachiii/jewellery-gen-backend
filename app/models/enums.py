from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    CLASSIFYING = "classifying"
    RESOLVING = "resolving"
    SUBMITTING = "submitting"
    GENERATING = "generating"
    STORING = "storing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_INPUT = "needs_input"
    NEEDS_REVIEW = "needs_review"


class ErrorCode(str, Enum):
    INVALID_IMAGE = "INVALID_IMAGE"
    IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    NOT_JEWELRY = "NOT_JEWELRY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    CLASSIFIER_ERROR = "CLASSIFIER_ERROR"
    MATRIX_MISS = "MATRIX_MISS"
    MATRIX_UNAVAILABLE = "MATRIX_UNAVAILABLE"
    PROVIDER_SUBMIT_FAILED = "PROVIDER_SUBMIT_FAILED"
    ORPHANED_SUBMIT = "ORPHANED_SUBMIT"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    SHEETS_ERROR = "SHEETS_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class JewelryType(str, Enum):
    """Reconciled against the client's real matrix (Sheet1 header row) in Phase 0a
    Step 6. The client's sheet is the source of truth (docs/business-rules.md R9)."""

    RING = "RING"
    NECKLACE = "NECKLACE"
    EARRING = "EARRING"
    BRACELET = "BRACELET"
    BANGLE = "BANGLE"
    ANKLET = "ANKLET"
    HIPBELT = "HIPBELT"


class ServiceType(str, Enum):
    """v1 values reconciled against the client's real matrix in Phase 0a Step 6: the
    sheet crosses 4 model/context categories with 2 styles (Traditional/Modern),
    not the originally-assumed MODEL_SHOT/CATALOG_WHITE/LIFESTYLE/CLOSEUP_MACRO."""

    FEMALE_MODEL_TRADITIONAL = "FEMALE_MODEL_TRADITIONAL"
    FEMALE_MODEL_MODERN = "FEMALE_MODEL_MODERN"
    MALE_MODEL_TRADITIONAL = "MALE_MODEL_TRADITIONAL"
    MALE_MODEL_MODERN = "MALE_MODEL_MODERN"
    MANNEQUIN_TRADITIONAL = "MANNEQUIN_TRADITIONAL"
    MANNEQUIN_MODERN = "MANNEQUIN_MODERN"
    PRODUCT_STYLING_TRADITIONAL = "PRODUCT_STYLING_TRADITIONAL"
    PRODUCT_STYLING_MODERN = "PRODUCT_STYLING_MODERN"
    # v2, deferred — never appear in the sheet; kept as a documented future contract
    REMOVE_BG = "REMOVE_BG"
    CHANGE_BG = "CHANGE_BG"
    MIX_PIECES = "MIX_PIECES"


class TypeSource(str, Enum):
    PROVIDED = "PROVIDED"
    CLASSIFIED = "CLASSIFIED"
    RESOLVED = "RESOLVED"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.NEEDS_INPUT,
        JobStatus.NEEDS_REVIEW,
    }
)

LEGAL_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.CLASSIFYING, JobStatus.RESOLVING, JobStatus.FAILED}),
    JobStatus.CLASSIFYING: frozenset(
        {JobStatus.RESOLVING, JobStatus.NEEDS_INPUT, JobStatus.FAILED}
    ),
    JobStatus.RESOLVING: frozenset({JobStatus.SUBMITTING, JobStatus.FAILED}),
    JobStatus.SUBMITTING: frozenset(
        {JobStatus.GENERATING, JobStatus.NEEDS_REVIEW, JobStatus.FAILED}
    ),
    JobStatus.GENERATING: frozenset({JobStatus.STORING, JobStatus.FAILED}),
    JobStatus.STORING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.NEEDS_INPUT: frozenset({JobStatus.RESOLVING}),
    JobStatus.NEEDS_REVIEW: frozenset(),
}

V1_SERVICES: frozenset[ServiceType] = frozenset(
    {
        ServiceType.FEMALE_MODEL_TRADITIONAL,
        ServiceType.FEMALE_MODEL_MODERN,
        ServiceType.MALE_MODEL_TRADITIONAL,
        ServiceType.MALE_MODEL_MODERN,
        ServiceType.MANNEQUIN_TRADITIONAL,
        ServiceType.MANNEQUIN_MODERN,
        ServiceType.PRODUCT_STYLING_TRADITIONAL,
        ServiceType.PRODUCT_STYLING_MODERN,
    }
)

V2_SERVICES: frozenset[ServiceType] = frozenset(
    {
        ServiceType.REMOVE_BG,
        ServiceType.CHANGE_BG,
        ServiceType.MIX_PIECES,
    }
)
