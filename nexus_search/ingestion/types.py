"""Shared type all ingestion connectors produce, regardless of source."""
from dataclasses import dataclass, field


@dataclass
class IngestDoc:
    doc_id: str
    title: str
    content: str
    doc_type: str
    metadata: dict = field(default_factory=dict)
