"""The data files the engine ships with, and nothing else.

This package holds bundled content as JSON under ``srd/``. It deliberately
contains no code and imports nothing from the engine, because
:mod:`fivee_sim.content` reads those files with
``resources.files("fivee_sim.data.srd")`` — anchoring resources on a package
imports it, and importing ``fivee_sim.data.srd`` imports this ``__init__``
first. Anything imported here would therefore be imported by ``content`` on its
way to reading the packs, and an import back into ``content`` would close a
cycle. One lived here for a while and survived only because the resource read
happens at call time.

The code that used to sit here has two owners now, split by what each half
depends on: creature construction is
:meth:`fivee_sim.model.creature.Creature.from_record`, in the layer that owns
creatures, and looking a name up in a registry is
:func:`fivee_sim.content.make_creature`, in the layer that owns registries.
``tests/test_layering.py`` keeps this package empty of imports.

The bundled slice is not privileged: it is loaded by the same parser and the
same validator as a campaign's own packs. Provenance and attribution: see
NOTICE.
"""
