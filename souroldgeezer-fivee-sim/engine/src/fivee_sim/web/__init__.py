"""The engine's HTTP face: a localhost REST adapter over :mod:`fivee_sim.service`.

The same rule that governs the MCP server governs this package: endpoints are
validation, serialization and error mapping only, and everything they do is one
call into the service layer. :mod:`~fivee_sim.web.routes` declares every
operation once; :mod:`~fivee_sim.web.http_server` dispatches from that table
and :mod:`~fivee_sim.web.openapi` publishes it; the launcher and its state-file
conventions live in :mod:`~fivee_sim.web.cli`, and the two browser pages under
``static/`` are package data this adapter serves with the launch's own
configuration injected.

It is named ``web`` rather than ``editor`` because the map editor is one page
it serves, not the extent of what it answers.
"""

from .http_server import EngineServer

__all__ = ["EngineServer"]
