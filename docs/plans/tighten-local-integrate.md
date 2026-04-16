# Tighten local `integrate` to explicit ontology policy

Follow-up to the ontology-policy tightening that landed for the **server** CLI
(`_run_integrate_annotations`) in PR #1 of the pull-session-rework branch. The
server path now requires exactly one of `--expected-ontology <name>` or
`--unconstrained`; silent fallback to unconstrained was identified as a
ground-truth-corruption hazard and removed.

This plan extends the same policy to the **local** integrate path, closes two
related asymmetries, and updates the supporting tests. Should be attacked in a
fresh branch; lives entirely inside `voxhub-core` (no schema or client
changes).

---

## 1. What the local path is

Distinct from the server CLI. Two entrypoints, one backing function:

- **Library**: `voxhub_core.integrate.integrate(...)` — called in-process by
  test suites and by local workflows. Reads a staging dir, validates + writes
  annotations into zarr stores, records provenance is the *caller's* problem
  (this path does not call `record_provenance`).
- **CLI**: `voxhub_core.cli._run_integrate` wraps `integrate()` for the
  `voxhub integrate` subcommand (`packages/voxhub-core/src/voxhub_core/cli.py`).

The two are decoupled from the server's `_run_integrate_annotations`; they
share only lower-level building blocks (`parse_seg_nrrd`, `validate_*`,
`write_*_to_zarr`).

## 2. Current asymmetries that break the ground-truth story

Three distinct issues, all on the local path, all worth fixing in this PR:

**2.1 `ontology=None` is a silent default.** `integrate()`'s signature at
`packages/voxhub-core/src/voxhub_core/integrate.py:432` is `ontology:
Ontology | None = None` with no error path — omission silently integrates
unconstrained and records `ontology='unconstrained'` in zarr attrs. Same
hazard the server path just closed.

**2.2 Segmentation validation ignores the declared ontology.** At
`integrate.py:524`, `validate_segmentation(seg_data, manifest_entry)` is
called **without** the `ontology=` kwarg, even when one was supplied. Only
the landmarks branch (`integrate.py:543`) threads it through. This means the
local path never ontology-validates segmentations — a declared ontology is
recorded in zarr attrs but never enforced against the seg label map. A data
integrity hole independent of the silent-default issue.

**2.3 Landmarks `ont_name` default is `'landmarks'`, not `'unconstrained'`.**
At `integrate.py:615`, when `ontology` is None, the provenance name becomes
`'landmarks'` (a legacy default). Under the strict policy, the only way
`ontology` reaches None is via an explicit `--unconstrained` opt-in, and the
name should then be the canonical `'unconstrained'` — matching the seg
branch (`integrate.py:601`) and the server CLI.

**2.4 Uniform ontology across all discovered stores.** The local
`integrate()` signature takes **one** `ontology` object and applies it to
every store and both annotation types in the staging dir. This is less
expressive than the server path, which accepts a list of declared ontology
names and matches per annotation type. Preserving a single-ontology signature
is fine — the local workflow is smaller in scope — but the strict policy
should still apply: if a landmark file exists and only a seg ontology was
supplied (or vice versa), integrate should error rather than silently
unconstrain.

---

## 3. Goal

Mirror the server CLI's explicit-intent policy in the local path, with the
local signature kept minimal (single ontology per call, not a list) to match
the simpler local workflow.

After this lands:

- `integrate()` requires an explicit ontology policy. Callers pass either a
  resolved `Ontology` instance or `unconstrained=True`. Passing both or
  neither is a `ValueError` at the library level.
- `voxhub integrate` CLI exposes `--ontology <name>` (resolved internally via
  `load_ontology`) and `--unconstrained` as mutually exclusive flags,
  matching the server CLI's surface.
- Segmentation validation receives the declared ontology and enforces it.
- Provenance ontology name is uniformly `'unconstrained'` under the explicit
  opt-in, for both seg and lmk annotations.
- If a staging-dir annotation's type doesn't match the declared ontology's
  type (e.g., declared a seg ontology but staging has landmarks), per-store
  integration records an error issue and skips the mismatched annotation
  — never silently falls back.

---

## 4. Concrete changes

### 4.1 `integrate()` signature (`packages/voxhub-core/src/voxhub_core/integrate.py:426`)

Replace the current keyword-only `ontology: Ontology | None = None` with:

```python
def integrate(
    staging_dir: str | Path,
    stores_dir: str | Path,
    *,
    annotator_id: str,
    nano_id: str,
    ontology: Ontology | None = None,
    unconstrained: bool = False,
    force: bool = False,
    validate_only: bool = False,
    console: Console | None = None,
) -> dict[str, list[IssueRecord]]:
    ...
```

Validation at function entry (before the per-store loop):

```python
if ontology is None and not unconstrained:
    raise ValueError(
        'integrate() requires an explicit ontology policy: pass '
        '`ontology=<Ontology>` for enforced integration, or '
        '`unconstrained=True` to explicitly opt out.'
    )
if ontology is not None and unconstrained:
    raise ValueError(
        '`ontology` and `unconstrained=True` are mutually exclusive.'
    )
```

`ValueError` (not `RuntimeError`) because this is a caller misuse, not a
runtime failure of the integration itself. Matches Python convention for
bad-argument errors.

Keep `ontology: Ontology | None = None` as the default (rather than making
it required) so the error message at entry is strictly better than
argparse's generic "missing required" — the docstring + error both explain
the `unconstrained=True` escape hatch.

### 4.2 Close the seg-validation hole (`integrate.py:524`)

Pass the ontology through:

```python
seg_issues = validate_segmentation(
    seg_data, manifest_entry, ontology=ontology
)
```

Matches the landmarks branch (line 543). This change alone is a bug fix
independent of the policy work.

### 4.3 Close the "no matching type" silent-fallback

Adopt the same per-annotation-type check the server CLI added. Before
writing, for each annotation:

- If `unconstrained` is True, proceed with `ontology=None`.
- Else, check the declared ontology's `type` field against the annotation's
  type (`'segmentation'` / `'landmarks'`). On mismatch, append an error
  `IssueRecord` and skip the write.

Error message should point at `--unconstrained` as the explicit escape
hatch, exactly like the server CLI does
(`server/cli.py::_run_integrate_annotations`).

### 4.4 Uniform `'unconstrained'` provenance name

Change `integrate.py:615` from `ont_name = ontology.name if ontology else
'landmarks'` to `ont_name = ontology.name if ontology else 'unconstrained'`,
matching the seg branch at line 601 and the server CLI. This is
semantically the correct name under the explicit-intent policy — `ontology
is None` now only happens on `unconstrained=True`.

### 4.5 Local CLI (`voxhub_core/cli.py:_run_integrate` + `_build_integrate_parser`)

Parser changes (`cli.py:143–157`):

```python
ontology_group = parser.add_mutually_exclusive_group()
ontology_group.add_argument(
    '--ontology',
    default=None,
    metavar='NAME',
    help=(
        'Ontology name to validate against and record. Mutually '
        'exclusive with --unconstrained; exactly one must be specified.'
    ),
)
ontology_group.add_argument(
    '--unconstrained',
    action='store_true',
    help=(
        'Explicit opt-in to unconstrained integration (no ontology '
        'enforcement). Mutually exclusive with --ontology.'
    ),
)
```

`add_mutually_exclusive_group()` is preferable to hand-rolled validation
here — argparse already produces a clean usage message on conflict. Neither
arg can be marked `required` on a mutually-exclusive group prior to Python
3.13's `required=True` on the group itself (which works) — use that so
argparse itself errors on the "neither" case.

Handler (`cli.py:244`):

```python
ontology = None
if args.ontology:
    ontology = load_ontology(args.ontology)

try:
    integrate(
        args.staging_dir,
        args.stores_dir,
        annotator_id=args.annotator_id,
        nano_id=args.nano_id,
        ontology=ontology,
        unconstrained=args.unconstrained,
        force=args.force,
        validate_only=args.validate_only,
    )
except (RuntimeError, FileNotFoundError, ValueError) as exc:
    print(f'Error: {exc}', file=sys.stderr)
    sys.exit(1)
```

Catching `ValueError` from `integrate()` means a library misuse from the CLI
path exits 1 with a clean message instead of a traceback — desirable even
though argparse's mutually-exclusive-required group should prevent reaching
that branch in practice.

---

## 5. Tests to add / update

### 5.1 New tests in `packages/voxhub-core/tests/test_integrate.py`

Mirror the four policy tests that landed for the server CLI in
`test_server_cli.py`:

1. `test_neither_ontology_nor_unconstrained_raises_value_error` — call
   `integrate()` with neither, assert `ValueError` with a helpful message.
2. `test_both_ontology_and_unconstrained_raises_value_error` — pass both,
   assert `ValueError`.
3. `test_unconstrained_flag_records_unconstrained_in_provenance` — pass
   `unconstrained=True`, assert integrated annotation's zarr attrs show
   `ontology='unconstrained'` for both seg and lmk.
4. `test_type_mismatch_records_error_not_silent_fallback` — pass a
   segmentation-typed ontology, stage a landmark-only file, assert error
   issue recorded and no annotation written.
5. `test_seg_ontology_is_actually_enforced` — regression for 2.2. Stage a
   segmentation whose label map violates the ontology (e.g., extra label),
   call with the ontology, assert an ontology-violation error is recorded.
   This test *fails today* because `validate_segmentation` is called
   without the ontology kwarg.

### 5.2 Existing tests in `test_integrate.py`

All existing call sites of `integrate(...)` need an explicit policy.
Straightforward find-and-replace: pass `ontology=some_ontology` (if the
test already loads one) or `unconstrained=True` (for legacy
no-ontology-declared tests). Grep for `integrate(` in
`packages/voxhub-core/tests/test_integrate.py:362, 381, 396, 422, 434, 451,
467` — seven call sites.

### 5.3 Workflow tests in `tests/test_workflow.py`

Seven call sites (`tests/test_workflow.py:131, 179, 215, 238, 326, 424, 437,
458`). Same migration. Several already pass `ontology=...`; the rest need
`unconstrained=True` added to express intent.

### 5.4 Subprocess test

The local CLI has no subprocess-level test suite today (unlike the server
CLI's `test_server_cli_subprocess.py`). Not strictly in scope for this
plan, but worth a line: argparse's mutually-exclusive-required-group
behaviour is *not* covered by the in-process tests above, and a single
subprocess smoke test invoking the CLI without any ontology flag would
close that gap.

---

## 6. Migration & rollout

Single-PR change, ~200 lines across three files plus test updates. No wire
protocol change, no cross-package coupling. Suggested order:

1. Fix the seg-validation bug (§4.2) — independent, shippable as its own
   commit. Add the regression test (§5.1 test #5) at the same time.
2. Library policy (§4.1, §4.3, §4.4) + tests (§5.1 #1–#4, §5.2).
3. CLI surface (§4.5).
4. Migrate `tests/test_workflow.py` (§5.3).

Could bundle all four in one PR since they're all tightly related.

### Breaking change surface

- **Library**: callers of `integrate()` with `ontology=None` (implicit
  unconstrained) now raise `ValueError`. They must pass
  `unconstrained=True` explicitly. All in-repo callers are test code;
  there are no public consumers outside this monorepo.
- **CLI**: `voxhub integrate` without `--ontology` now errors; users must
  pass `--ontology <name>` or `--unconstrained`. This is a user-facing
  break — worth a note in the release notes for whoever cuts the next
  version.

---

## 7. Non-goals

- **Not** redesigning the local workflow's ontology discovery. The staging
  dir remains ontology-agnostic; the caller declares intent at integrate
  time, same as before, just now required.
- **Not** changing `validate_segmentation` / `validate_landmarks`
  signatures — those already accept `ontology=` kwargs. Only the call
  site at `integrate.py:524` is wrong.
- **Not** unifying local and server code paths. The server uses a
  per-store list of ontology names matched per annotation type; the local
  path uses a single `Ontology` instance. Two shapes, different call
  ergonomics. Unifying them would be a follow-up plan, not this one.
- **Not** changing `record_provenance` — the local path doesn't call it
  today. Whether it should is an orthogonal question.

---

## 8. Success criteria

- All tests in `packages/voxhub-core/tests/test_integrate.py`,
  `tests/test_workflow.py`, and any local-CLI subprocess tests pass.
- `integrate()` called with `ontology=None, unconstrained=False` raises
  `ValueError`; with both set, raises `ValueError`.
- `voxhub integrate` without either flag prints argparse's
  mutually-exclusive-required-group usage and exits 2.
- Ontology-violating segmentation (seg label map not conforming to
  declared ontology) now produces an error issue — the §2.2 gap is
  closed.
- Zarr attrs' `ontology` field is uniformly `'unconstrained'` on the
  explicit opt-in path; never `'landmarks'` as a silent legacy default.
- Grep for any remaining `integrate(` call site without an explicit
  policy declaration returns empty.

---

## 9. Open questions for the implementer

- **`required=True` on argparse mutex group vs. manual check**: Python's
  `add_mutually_exclusive_group(required=True)` gives the cleanest UX.
  Worth confirming the project's minimum Python version supports the
  behaviour (CLAUDE.md says Python 3.12+, which is fine — `required=True`
  has been available on mutex groups since 3.7).
- **Should `validate_only` bypass the policy?** Today `integrate(..., validate_only=True)`
  doesn't write; arguably the ontology declaration is less load-bearing.
  Recommendation: **still require the declaration** — consistency beats
  the minor UX win, and validating without an ontology is a legitimate
  query (`unconstrained=True` expresses it). Keep the policy uniform
  across `validate_only=True` and `False`.
- **Provenance coverage for local integrate**: separate from this PR, but
  noting: server CLI writes to `.meta/provenance.jsonl`; local does not.
  If local-written annotations ever need to be audited, this gap matters.
  Out of scope here — flagged as a follow-up for whoever owns provenance.
