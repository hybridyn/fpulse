"""F-Pulse connector framework — declarative SaaS, JDBC, CDC, and vector connectors."""

from .rest_framework import (
    RestConnectorManifest,
    RestApiSourceNode,
    load_manifests,
    list_manifests,
    get_manifest,
)

__all__ = [
    "RestConnectorManifest",
    "RestApiSourceNode",
    "load_manifests",
    "list_manifests",
    "get_manifest",
]
