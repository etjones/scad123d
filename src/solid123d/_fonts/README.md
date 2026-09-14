# Vendored font

`LiberationSans-Regular.ttf`, from Liberation 2.00.1 — the same release
OpenSCAD ships in its own application bundle.

It is here for one reason: **OpenSCAD falls back to Liberation Sans when
it cannot resolve a font family**, silently and with no warning. A model
naming a font nobody has still renders, in that face. To agree with
OpenSCAD on such a model we have to fall back to the same face, and we
cannot rely on it being installed — it is absent from a stock macOS, and
from the CI runners this project tests on.

Vendoring it also makes the text tests deterministic everywhere, which
matters because the alternative is asserting against whichever font the
host happens to substitute.

Licensed under the SIL Open Font License, Version 1.1; see `LICENSE`.
The OFL permits bundling, provided the licence travels with the font and
the Reserved Font Name is not used for modified versions. This copy is
unmodified.
