"""Bridge that translates dyndns2 update requests into Hetzner Cloud DNS API calls."""

from .app import __version__, create_app
from .config import Settings

__all__ = ["Settings", "__version__", "create_app"]
