"""Keep pytest out of the generator template tree.

The files under ``new-case/`` are *templates* containing ``{{ token }}``
placeholders (e.g. ``test_{{case_name}}.py``). They are valid Python but are NOT
runnable tests — they are rendered by ``ack.generator`` into a real workspace.
Without this, ``pytest core/`` would try to collect the template test skeleton.
"""
collect_ignore_glob = ["**/*.py"]
