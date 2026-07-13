"""voxhub-schema: protocol contract, ontology definitions, and validation."""

from voxhub_schema.manifest import (
    ManifestError,
    ManifestStatus,
    PullAnnotationEntry,
    PullManifest,
    RemoteManifest,
    RemoteManifestEntry,
)
from voxhub_schema.models import (
    PROTOCOL_VERSION,
    AnnotationInfo,
    ChecksumEntry,
    CleanupResponse,
    DatasetAttributes,
    GcResponse,
    IntegratedAnnotation,
    IntegrateRequest,
    IntegrateResponse,
    IntegrateResult,
    IssueRecord,
    LengthUnit,
    Modality,
    PreparePushResponse,
    PrepareRequest,
    PrepareResponse,
    Resolution,
    ServerError,
    StoreInfo,
    WriteMode,
    serialize,
)
from voxhub_schema.naming import (
    NANO_ID_ALPHABET,
    NANO_ID_LENGTH,
    AnnotatorSlugError,
    parse_annotator_slug,
)
from voxhub_schema.nano_id import (
    ALPHABET,
    DEFAULT_SIZE,
    generate_nano_id,
)
from voxhub_schema.ontology import (
    UNCONSTRAINED_SEGMENTATION,
    ChannelMode,
    Ontology,
    OntologyLabel,
    OntologyType,
    list_ontologies,
    load_ontology,
)
from voxhub_schema.validation import (
    validate_lmk_preflight,
    validate_seg_preflight,
)

__all__ = [
    # nano_id
    'ALPHABET',
    'DEFAULT_SIZE',
    # naming
    'NANO_ID_ALPHABET',
    'NANO_ID_LENGTH',
    # models
    'PROTOCOL_VERSION',
    'UNCONSTRAINED_SEGMENTATION',
    'AnnotationInfo',
    'AnnotatorSlugError',
    # ontology
    'ChannelMode',
    'ChecksumEntry',
    'CleanupResponse',
    'DatasetAttributes',
    'GcResponse',
    'IntegrateRequest',
    'IntegrateResponse',
    'IntegrateResult',
    'IntegratedAnnotation',
    'IssueRecord',
    'LengthUnit',
    # manifest
    'ManifestError',
    'ManifestStatus',
    'Modality',
    'Ontology',
    'OntologyLabel',
    'OntologyType',
    'PreparePushResponse',
    'PrepareRequest',
    'PrepareResponse',
    'PullAnnotationEntry',
    'PullManifest',
    'RemoteManifest',
    'RemoteManifestEntry',
    'Resolution',
    'ServerError',
    'StoreInfo',
    'WriteMode',
    'generate_nano_id',
    'list_ontologies',
    'load_ontology',
    'parse_annotator_slug',
    'serialize',
    # validation
    'validate_lmk_preflight',
    'validate_seg_preflight',
]
