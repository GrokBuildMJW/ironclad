"""MPR domain role-registry package (Spec 05).

Kept import-light on purpose: this ``__init__`` exports nothing eagerly so that pulling in
``mpr.registry.schema`` never drags in ``loader``/``adaptive`` (built in later units) or runs a
discovery side-effect at import time — mirroring ``ack.Registry.__init__`` being side-effect-free.
Import the concrete module you need (``mpr.registry.schema``, ``mpr.registry.loader``, …) directly.
"""
