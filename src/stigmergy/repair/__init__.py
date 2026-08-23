"""`stigmergy.repair` — a person's page removal: what may go, what has to be rewritten, and the
permanent record of what left.

**A human decides; code performs.** Somebody names pages at `brain_delete` or on the console's
Remove pages button, and this package answers the questions code owns: which of those paths are
pages this lane may delete at all, which pages in the corpus refer to them, and exactly what bytes
every one of those pages must end up carrying so the reference does not survive as a dead link.
The one thing code cannot do is write the prose — a sentence that cited a removed page still has to
read — so `sweep.py` asks a model for the bodies and `deletion.py` refuses anything it wrote that
touched more than the body, byte for byte.

The worker performs it, because it holds the only checkout and the only credential this system
has. `librarian.processing.process_delete_item` is that flow; nothing here opens a connection or
commits.

**What used to be here and is not.** An elective loop turned gardener findings into proposed
repairs — additive edits, drafted entity bodies, entity merges — derived by a model overnight and
applied without anybody being asked. Measured against `docs/DESIGN.md` §2 it had applied five
repairs in three weeks of daily use, its detectors are gone with the gardener's model passes, and a
capture now brings a page up to date directly. It was removed. `schema.RETIRED_KINDS` is the half
of it a deployed database still holds.

No module here reads the environment except `settings.py`. Per-module import edges are pinned by
`tests/test_architecture.py`. See `index.md` for the code map.
"""
