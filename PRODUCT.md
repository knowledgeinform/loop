# Product

## Register

product

## Users

Materials-science researchers at the **Entropy for Energy Laboratory** (Johns Hopkins, Dept. of Materials Science & Engineering) and affiliated research groups (e.g. APL, Oak Ridge). They span PIs, postdocs, grad students, and new lab members. Their context is bench-and-desk science: they arrive with experimental XRD data, literature records, or DFT computations and need to catalog, find, compare, and analyze records on high-entropy materials. Access is affiliation-gated — a user only sees data their group is cleared for.

The jobs to be done:
- **Browse & find** — locate materials, recipes, trials, and literature by composition, structure family, or semantic similarity, then read dense detail pages without losing their place.
- **Add & upload** — enter experiments, literature, and computational records, including batch uploads and long-running Rietveld refinements run in-request.
- **Manage** — maintain a personal library of precursors and synthesis protocols; superusers administer MongoDB collections directly.

## Product Purpose

LOOP is a content-addressable catalog for high-entropy materials data. Every record is keyed by a deterministic AUID (SHA256 of canonical content), so the same material or recipe deduplicates identically across experimental, literature, and computational sources. It exists to make a lab's — and a consortium's — accumulated materials knowledge searchable, comparable, and reusable rather than siloed in spreadsheets and drives.

Success looks like: a researcher trusts LOOP as the authoritative record, finds what they need in a few scans, contributes data without friction, and reads a detail page as confidently as a lab notebook.

## Brand Personality

**Institutional, authoritative, precise.** LOOP carries the pedigree of a national-lab-adjacent university consortium — it should feel serious, established, and official, the way an instrument readout or a peer-reviewed record does. Voice is exact and unadorned: labels say what they mean, numbers are legible to the last significant figure, nothing is dressed up. Trust is earned through rigor and consistency, not decoration. Confident, not flashy; official, not stuffy.

## Anti-references

- **Generic SaaS dashboard.** No gradient hero, purple/violet accent, glassmorphism, or endless icon-card grids. This is not a startup landing page.
- **Cluttered legacy academic site.** Avoid the dense, gray, link-soup department-page feel the current build partly has — justified body text, hairline `border-bottom` headings, undifferentiated blue links, no spacing rhythm.
- **Consumer / playful.** No rounded-bubbly shapes, bright candy color, gamification, or mascot energy. Restraint signals seriousness here.

## Design Principles

1. **The data is the interface.** Compositions, AUIDs, badges, and measured values are the content — typography, alignment, and spacing exist to make them scan fast and read exactly. Never let chrome compete with the record.
2. **Density with hierarchy.** Researchers want a lot on screen, but organized. Earn density through clear grouping, alignment, and rhythm — not by cramming. A dense page should still have an obvious reading order.
3. **Color carries meaning, not decoration.** Affiliation and structure-family colors are a data encoding. Keep that system disciplined and distinguishable; don't spend those hues on ornament.
4. **Instrument-grade trust.** Consistent tokens, predictable components, exact numbers, honest states (loading, empty, error). Rietveld and batch jobs are slow and synchronous — the UI must always say what's happening.
5. **One coherent system.** Replace ad-hoc Bootstrap-plus-SCSS drift with shared tokens (color, type scale, spacing, radius) and reusable components, so every page feels built by the same hand.

## Accessibility & Inclusion

- Target **WCAG 2.1 AA**: body text ≥ 4.5:1 contrast, large/bold text ≥ 3:1, visible focus states, full keyboard operability of nav, forms, dropdowns, and modals.
- **Colorblind-safe encodings.** Because affiliation and structure-family meaning is carried by color, never rely on hue alone — pair every colored badge with its text label (already the pattern) and ensure the palette is distinguishable across common color-vision deficiencies.
- **Respect `prefers-reduced-motion`** — any added motion needs a crossfade/instant fallback.
- Dense data pages must remain legible and operable at mobile widths and at 200% zoom.
