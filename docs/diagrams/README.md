# voxhub presentation diagrams

A coherent set of light-theme flow diagrams for presenting voxhub to users and
stakeholders. All are authored at **1280×720 (16:9)** and stay within safe margins, so
they also drop cleanly onto 16:10 slides (slight letterboxing top/bottom).

| # | File | Use it to show |
|---|------|----------------|
| 1 | `01-overview.svg` | The whole system at a glance — the collaborative pull → annotate → push → integrate loop |
| 2 | `02-pull.svg` | How pulling data works (list-stores → prepare-pull → rsync → cleanup → manifest) |
| 3 | `03-annotate.svg` | Annotating locally in 3D Slicer, guided by an ontology |
| 4 | `04-push-integrate.svg` | The validation gate on push, then server-side integration + provenance |
| 5 | `05-architecture.svg` | The three packages, the dependency DAG, and the server/local hard wall |
| 6 | `06-storage-provenance.svg` | The annotator-scoped zarr layout and how it stays conflict-free and queryable |

Suggested deck order: **1** as the opener, then **2–4** for the workflow story, then
**5–6** for the "how it holds up" technical backing.

## Design language

- Light background, one accent colour per slide (blue / teal / violet / amber / indigo).
- Shared header, footer wordmark, and step-card style across all six so they read as a set.
- Fonts use the system sans stack (`Segoe UI` / `Helvetica Neue` / `Arial`); no embedded
  fonts, so they render natively on any machine.

## Editing / exporting

SVGs are plain text — tweak labels directly, or open in Inkscape / Figma / Illustrator.
Most slide tools (PowerPoint, Keynote, Google Slides) import SVG directly. To rasterize:

```sh
# PNG at 2× for crisp slides
uv run --with cairosvg python -c "import cairosvg; cairosvg.svg2png(url='01-overview.svg', write_to='01-overview.png', output_width=2560, output_height=1440)"
```
