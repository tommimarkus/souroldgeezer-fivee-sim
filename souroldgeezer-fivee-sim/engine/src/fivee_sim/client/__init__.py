"""``fivee`` — the engine's REST surface, driven from a shell.

This package is a *client*. It holds no rules, no map format, no encounter
state, and it reaches the engine only over HTTP: the one thing it imports from
the rest of this tree is :mod:`fivee_sim.paths`, which says where a running
server records itself and is not an operation. ``tests/test_layering.py``
enforces that as an import rule, and the reason it is worth enforcing is the
whole point of the package — every feature this CLI has is a feature
``/api/v1`` serves, so "the REST surface is the engine's whole surface" is a
test rather than a claim.

Three modules, one job each:

* :mod:`~fivee_sim.client.discovery` — find a running server through its state
  file, or spawn ``python -m fivee_sim.web`` detached and wait for the port.
* :mod:`~fivee_sim.client.http` — one request, and RFC 9457 problem+json
  rendered as something a person can act on.
* :mod:`~fivee_sim.client.cli` — ``argv`` to an operation, with the operation
  list and every argument's type read off the live server.

The console script is ``fivee``, and ``python -m fivee_sim.client`` is the same
entry point without needing a ``PATH``.
"""
