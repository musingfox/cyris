"""Fetch module for article ingestion from multiple sources."""

from cyris.adapters.fetch.miniflux_source import MinifluxSource
from cyris.adapters.fetch.newsletter_source import NewsletterArchiveSource
from cyris.service_layer.ports import FetchSource

__all__ = ["FetchSource", "MinifluxSource", "NewsletterArchiveSource"]
