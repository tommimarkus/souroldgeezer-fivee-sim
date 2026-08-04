"""``python -m fivee_sim.client`` runs the ``fivee`` command.

The installed console script is the ordinary way in; this is the way in that
needs no ``PATH``, which is what a spawned process and a test both want.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
