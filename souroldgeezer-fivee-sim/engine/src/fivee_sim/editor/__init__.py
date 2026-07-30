"""The interactive map editor: a localhost REST adapter over the map service.

The same rule that governs the MCP server governs this package: endpoints are
serialization and error mapping only, and everything they do is a call into
:mod:`fivee_sim.service.maps`. The HTTP server lives in
:mod:`~fivee_sim.editor.http_server`, the launcher and state-file conventions
in :mod:`~fivee_sim.editor.cli`, and the served pages under ``static/`` as
package data.
"""

from .http_server import EditorServer

__all__ = ["EditorServer"]
