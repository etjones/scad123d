# Native STEP export

This adds a STEP exporter built on OpenCASCADE, as an optional compile-time
dependency that is off by default. With `ENABLE_OCCT=OFF` the binary is
unchanged and links nothing new.

The exporter is exact where the geometry has an exact form and a mesh where it does 
not, and the output is checked against itself before the file is handed back. The
complete design is written up in `doc/step-export.md`.

## What it does

STEP is a boundary representation, so the exporter walks the evaluated
node tree and rebuilds it in OpenCASCADE rather than converting the mesh
the other exporters receive. Primitives, booleans, transforms, extrusions
and the closed forms of `hull` and `minkowski` come out as exact surfaces.
Anything without an exact form, such as `offset`, `projection`, `resize`,
twisted extrusion and general hulls, is rendered by OpenSCAD's own
evaluator and sewn into a solid, with a warning that names the region.
The intent is to guarantee that the STEP is never worse than the STL of the same model.

## Choices, and what else we considered

**The dependency and startup time.** OpenCASCADE adds 33 MB compressed to
the macOS download and 26 dynamic libraries, and loading them costs
about 37 ms per launch on the CLI (36 to 73 ms to `--version`). We have
kept it a link-time dependency because that is how CGAL, Manifold and
lib3mf are already treated. Two alternatives are open: 
- 1) Linking OpenCASCADE statically removes most of the launch cost with no code 
change, where a static build is available. 
- 2) Loading the exporter as a module on first use removes all of the launch 
cost and lets the bytes be an optional download, at the cost
of a pattern with no precedent in this codebase, exported symbols from the
executable, and real difficulty on Windows. We're happy to take direction here.

**Faceting.** A `$fn` below a configurable threshold (default 20) is
treated as deliberate and exported polygonal, so `circle($fn=6)` is a
hexagon; default `$fa`/`$fs` never facet, so a plain `cylinder()` is a
cylinder. Alternatives were always-exact and always-honor-`$fn`; both
give worse files for common models.

**color.** `color()` follows the preview's rule, outermost wins. Each
body carries its color as a style, and by default bodies are also
grouped by color in the file's assembly tree. That spends the tree on an
attribute, which we know is unconventional; we do it because the tree is
the only grouping every consumer honors, and color is the one intent
that survives a boolean. It is a setting, on by default.

**Trusting the kernel.** OpenCASCADE sometimes returns a valid solid that
is not the answer. Each boolean is checked against statements about the
answer: bounds on its volume, its piece count, and whether it still holds
every operand and nothing inside a cutter. Failures are retried with fuzzy
tolerances and then one body at a time. What happens when nothing works
was decided by measurement: a union hands back its operands unjoined, a
cut falls back to a mesh. Sending unions to a mesh as well made five of
500 corpus models badly wrong, so it is deliberately not done.

**Checking the file.** Every export reads its own file back and compares
it with what was built. This costs about 14% of export time and has caught
two silent file-level defects, one of them ours; when it fires the file is
rewritten with a different seam strategy and checked again. It could be a
setting; we think it should default on.

**Time budget.** An optional wall-clock budget for the B-rep work, off by
default because it would make the same model export differently on
different machines.

**Fallback fineness.** A general hull rendered at a high `$fn` can carry
tens of thousands of triangles and never finish; past a budget the hull is
built from the exact children at a fineness the exporter chooses.

## Testing

Everything runs under the existing `ctest`: 42 regression models compared
through a JSON metrics export, and 13 Catch2 unit tests. Face counts are
compared with slack because Manifold's hull is not deterministic to the
unit. Beyond the tree, the exporter has been run over a 500-model sample of
a public OpenSCAD corpus against OpenSCAD's own mesh; that corpus is not
ours to contribute, but the numbers are in the design document.

## Platforms and known limits

Enabled on macOS, Linux and Windows with OpenCASCADE 7.6 or newer.
Windows needed two pieces of work. The msys2 test job cannot use msys2's
OpenCASCADE package: GCC 16.2 at -O3 miscompiles (or exposes undefined
behaviour in) the kernel's curve-curve extrema, so any point classified
against a boolean result segfaults inside the kernel before our code runs.
A standalone probe pins it to one pass, `-fipa-cp-clone`, which -O3 turns
on; the same sources at -O2 pass. The job therefore builds OpenCASCADE
7.9.3 from source at -O2, cached, and passes all 2743 tests; we will report
the package to msys2.
The shipped Windows binary is cross-compiled with MXE and GCC 11, where
the kernel is fine but no OpenCASCADE package existed; we wrote one
(static, the STEP and XDE toolkits) and the probe runs clean on Windows
against it. Linking the release build to it is the remaining step.
One corpus model still never finishes, inside a kernel boolean that does
not poll for cancellation. A threaded part exports as a mesh where an
exact form exists but the kernel will not produce it. The assembly tree is
spent on color, and layers are not written. The exporter runs on the GUI thread with no
progress or cancel.

## Questions

- Is a dependency of this size acceptable as a link-time option, or would
  you want it loaded on demand?
- Is default fineness producing exact curves the rule you would choose?
- Should an unverifiable cut fall back to a mesh, as now, or fail the
  export?
- Should the read-back check be a setting?
- Grouping bodies by color uses STEP's assembly tree only for expressing color. 
That's probably what most OpenSCAD users need it for, in 3D printing, and 
consuming apps honor grouping in STEP while many don't honor other attributes, 
like layers. But traditional CAD is more likely to use assembly trees for separate
bodies or assemblies; we use it differently. We use this color-grouping strategy 
by default; should we?


