---
name: LOOP
description: An institutional catalog for high-entropy materials data — precise, authoritative, legible.
colors:
  primary: "#002d72"
  primary-tint-light: "#68ace5"
  ink: "#1a1510"
  ink-alt: "#31261d"
  surface: "#ffffff"
  surface-hero: "#f7f8fb"
  surface-hero-alt: "#e8ecf4"
  surface-toolbar: "#f6f8fc"
  note-bg: "#fffbeb"
  note-border: "#f5e6c8"
  line: "#e6e2dc"
  border-strong: "#cccccc"
  muted: "#7c756c"
  aff-s4e: "#002d72"
  aff-apl: "#68ace5"
  aff-oakridge: "#2e7d32"
  struct-rocksalt: "#5a4e9c"
  struct-pyrochlore: "#4a63b8"
  struct-spinel: "#68ace5"
  struct-perovskite: "#4f7dc4"
  struct-fluorite: "#2e7d32"
  struct-other: "#6c757d"
  danger: "#b02a37"
  success: "#2e7d32"
typography:
  display:
    fontFamily: "Lato, -apple-system, Segoe UI, sans-serif"
    fontSize: "clamp(1.6rem, 3.5vw, 2.25rem)"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "Lato, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.3rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "normal"
  title:
    fontFamily: "Lato, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "normal"
  body:
    fontFamily: "Lato, -apple-system, Segoe UI, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Lato, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.72rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.07em"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-0.02em"
rounded:
  sm: "0.35rem"
  md: "8px"
  lg: "12px"
  pill: "999px"
spacing:
  xs: "0.35rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "1.25rem"
  xl: "2rem"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    padding: "0.5rem 1rem"
  button-outline:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.sm}"
    padding: "0.5rem 1rem"
  chip-composition:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0.3rem 0.9rem"
  badge-affiliation:
    backgroundColor: "{colors.aff-s4e}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    padding: "0.2rem 0.6rem"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "1rem 1.15rem"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0.5rem"
---

# Design System: LOOP

## 1. Overview

**Creative North Star: "The Institutional Archive"**

LOOP is the authoritative catalog of a university-and-national-lab materials consortium, and it should carry itself that way: serious, exact, and permanent. The metaphor is a great research archive — a place where every record has a call number (its AUID), where the structure of the catalog is itself trustworthy, and where nothing is dressed up because the material is the point. Johns Hopkins navy (`#002d72`) is the institutional signature; it anchors headers, primary actions, and the affiliation encoding. Everything else recedes so the data — compositions, measured values, monospace AUIDs — reads first and reads exactly.

Density is a feature here, not a problem to hide. Researchers want a lot on one screen, so the system earns density through hierarchy: disciplined type scale, generous alignment, a small radius vocabulary, and rhythm between grouped sections. Detail pages already point the way — a soft gradient hero banner, monospace call-numbers, underline tabs, and flat bordered panels. That refined detail-page language is the target the rest of the platform grows toward, replacing the older Bootstrap-plus-SCSS drift with one coherent hand.

This system explicitly rejects three things. It is **not a generic SaaS dashboard** — no gradient heroes as decoration, no violet accent, no glassmorphism, no endless icon-card grids. It is **not a cluttered legacy academic page** — no justified body text, no hairline `border-bottom` headings standing in for hierarchy, no undifferentiated blue link-soup. And it is **not consumer or playful** — no bubbly rounded shapes, no candy color, no ornament. Restraint is how an archive signals that it can be trusted.

**Key Characteristics:**
- Institutional navy as signature, spent deliberately — never as a background wash.
- Monospace AUIDs treated as call-numbers: the identity of every record.
- Color is a data encoding (affiliation, structure family) before it is ever decoration.
- Flat, bordered surfaces; depth comes from tone and line, not shadow.
- Dense but ordered — hierarchy carries the load, not cramming.

## 2. Colors

A restrained institutional palette: one authoritative navy, warm near-black ink on near-white surfaces, and a disciplined set of encoding hues that carry meaning rather than mood.

### Primary
- **Hopkins Navy** (`#002d72`): The institutional signature. Primary buttons, active nav, headings, links, AUID text, hero accents, and the S4E affiliation. This is the one voice of the system; it appears where authority or action lives, never as a page background.
- **Sky Tint** (`#68ace5`): A lighter navy relative reserved for accents that carry **dark** text — the APL affiliation badge, Spinel structure badge — and never as a fill behind white text (fails contrast). Support role only; it never competes with Hopkins Navy for primary actions.

### Neutral
- **Archive Ink** (`#1a1510`): Primary body and data text on detail surfaces — a warm near-black, softer and more legible than pure black. The canonical reading color.
- **Ink Alt** (`#31261d`): The global text color inherited from the base theme; the same warm-black family as Archive Ink.
- **Muted Ink** (`#7c756c` / `rgba(26,21,16,0.58)`): Breadcrumbs, captions, secondary metadata. Reserved for genuinely secondary text — bumped toward ink whenever it risks falling under 4.5:1.
- **Paper** (`#ffffff`): The default surface for content blocks, panels, and cards.
- **Hero Wash** (`#f7f8fb` → `#e8ecf4`): The soft cool gradient of the detail hero banner — the one sanctioned gradient, structural not decorative.
- **Toolbar Wash** (`#f6f8fc` / `rgba(0,45,114,0.03)`): Faint navy-tinted fill for section toolbars and table headers.
- **Line** (`#e6e2dc` / `rgba(26,21,16,0.1)`): Hairline borders and dividers between panels and rows.

### Tertiary — Encoding Hues (meaning, not decoration)
These are a controlled vocabulary. Each maps to a fixed data value and is always paired with its text label.
- **Affiliations** — S4E `#002d72`, APL `#68ace5`, Oak Ridge `#2e7d32`.
- **Structure families** — Rocksalt `#5a4e9c`, Pyrochlore `#4a63b8`, Spinel `#68ace5`, Perovskite `#4f7dc4`, Fluorite `#2e7d32`, Other `#6c757d`.
- **Status** — Success/Open `#2e7d32`, Danger/Error `#b02a37`.
- **Note** — Amber note card, `#fffbeb` fill on `#f5e6c8` border, for advisory annotations only.

### Named Rules
**The One Navy Rule.** Hopkins Navy is the single institutional voice. It marks authority (headings, AUIDs, affiliation) and action (primary buttons, active states) — nowhere else. If navy is filling large decorative areas, it has been spent wrong.

**The Encoding Rule.** The structure-family and affiliation hues belong to data. Never reuse them for buttons, backgrounds, or ornament — doing so breaks the reader's learned mapping between color and meaning.

## 3. Typography

**Display / Body Font:** Lato (with `-apple-system, Segoe UI, sans-serif` fallback)
**Label/Mono Font:** `ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`

**Character:** One humanist sans in a disciplined range of weights (300/400/600/700) does all the prose and heading work — quiet, legible, institutional. The monospace family is not decorative: it is reserved for identity and measurement, so a reader's eye learns that "monospace means machine-exact."

### Hierarchy
- **Display** (700, `clamp(1.6rem, 3.5vw, 2.25rem)`, 1.15): Page-level `h1` / hero titles in Hopkins Navy. Deliberately restrained — an archive labels, it doesn't shout.
- **Headline** (600, `1.3rem`, 1.25): Section headings and sidebar headers.
- **Title** (600, `1.05rem`, 1.3): Sub-section and card titles, form-section headers.
- **Body** (400, `1rem`, 1.5): Reading text and data-cell content in Archive Ink. Cap prose at 65–75ch.
- **Label** (600, `0.72rem`, `+0.07em`, UPPERCASE): Toolbar titles, formula labels, table eyebrows. The one place tracked uppercase is sanctioned — as functional metadata labels, never as a decorative kicker over every section.
- **Mono** (600, `1rem`, `-0.02em`): AUIDs, hashes, and exact numeric identifiers. The call-number of the archive.

### Named Rules
**The Call-Number Rule.** Every AUID, hash, and machine-exact identifier is set in the monospace family. Prose is never monospace; identifiers are never prose. The distinction is the reader's cue to what is human text and what is a canonical key.

**The Quiet Display Rule.** Display type maxes out near `2.25rem`. LOOP never uses oversized hero type — an institution states, it doesn't advertise.

## 4. Elevation

Flat by default. Depth is built from **line and tone**, not shadow. Panels, cards, tables, and the hero banner sit on the page as bordered planes (`1px` hairlines in the Line neutral) differentiated by subtle background washes — Paper for content, Toolbar Wash for headers, Hero Wash for the banner. This flatness is what keeps a dense page calm and archival rather than floating and app-like.

Shadows are used only as a functional response to state — the Bootstrap focus ring, an open dropdown/modal lifting above content — never as ambient decoration at rest.

### Named Rules
**The Line-Not-Shadow Rule.** Separation between surfaces is drawn with a `1px` hairline and a tonal shift, not a drop shadow. If a resting surface has a shadow, it's wrong. A shadow may appear only in response to focus, hover-on-interactive, or a raised layer (dropdown, modal, toast).

## 5. Components

### Buttons
- **Shape:** Small, institutional radius (`0.35rem`). Never pill-shaped for actions.
- **Primary:** Hopkins Navy fill, Paper text (`btn-primary`). The single call-to-action per view.
- **Outline:** Paper fill, colored stroke + text — `btn-outline-secondary` (default), `btn-outline-primary` (navy), `btn-outline-danger` (destructive). Outline is the default for secondary and row-level actions; primary fill is rationed.
- **Sizes:** `btn-sm` for dense table rows and toolbars; default size for form submits.
- **Hover / Focus:** Bootstrap state transitions with a visible focus ring; keep them.

### Chips
- **Composition chip:** Pill (`999px`), Paper background, `1px` navy-tinted border, Archive Ink label with a lighter ratio suffix. Used to summarize a composition as element+ratio tokens. Chips are the one sanctioned pill shape — they are data tokens, not buttons.

### Badges (signature encoding component)
- **Style:** Small-radius (`0.35rem`) solid fill in the record's encoding hue, always with a text label inside. Two families: `org-badge-*` (affiliation) and `structure-badge-*` (structure family).
- **Rule:** Color + label always travel together; the label must remain legible on the fill (light-fill badges like APL/Spinel switch to dark `#0f223a` text). Never a color swatch alone.

### Cards / Containers (Panels)
- **Corner Style:** `12px` (`lg`) for panels and tab bodies; `10px` for the hero banner.
- **Background:** Paper; section toolbars use Toolbar Wash.
- **Shadow Strategy:** None at rest — see Elevation.
- **Border:** `1px` Line hairline. Tab bodies drop the top border to fuse with the active tab.
- **Internal Padding:** `1rem 1.15rem` body; toolbars `1rem 1.15rem` with a bottom hairline.
- **Nesting:** Never nest a card inside a card. Use a bordered sub-section or a table instead.

### Inputs / Fields
- **Style:** Paper fill, `1px` grey stroke (`#ccc`), `0.35rem` radius, `0.5rem` padding, `1rem` text. Labels are `600` weight, small margin below.
- **Focus:** Bootstrap focus ring — keep it visible; never remove outlines.
- **Error:** Errors surface in a bordered `form-errors` block (Danger text `#b02a37` on a pink-bordered card), not inline color alone.

### Navigation
- **Header:** Hopkins Navy band with the S4E logo and the LOOP wordmark (`strong` at 700 weight, remainder at 300).
- **Main nav:** Sticky **Paper** bar (not Sky — white-on-Sky fails contrast at 2.4:1), navy/muted links; the active link is navy text with a `2px` navy underline; hover tints navy. This deliberately shares its language with the detail tabs — one tab system across the app. Collapses to a hamburger below `768px`.
- **Detail tabs:** Text tabs with a `2px` navy underline on the active tab — no boxed/pill tabs. Identical treatment to the main nav.

### Data Tables (signature component)
- **Style:** Full-width, hairline row dividers, Toolbar-Wash header row with UPPERCASE Label-style column heads. Numeric columns right-aligned; AUIDs in Mono. Tables — not card grids — are the primary way LOOP presents many records.

## 6. Do's and Don'ts

### Do:
- **Do** spend Hopkins Navy (`#002d72`) only on authority and action — headings, primary buttons, active states, AUIDs, S4E affiliation. Keep it off large backgrounds.
- **Do** set every AUID, hash, and exact identifier in the monospace family (the Call-Number Rule).
- **Do** always pair an encoding color with its text label; assume a colorblind reader and never rely on hue alone.
- **Do** separate surfaces with `1px` hairlines and tonal washes; keep resting surfaces shadow-free (the Line-Not-Shadow Rule).
- **Do** present many records as data tables with right-aligned numerics and mono AUIDs.
- **Do** keep the small radius vocabulary — `0.35rem` actions/inputs, `8–12px` panels, `999px` for data chips only.
- **Do** provide honest loading, empty, and error states — Rietveld and batch jobs are slow and synchronous; always say what's happening.

### Don't:
- **Don't** build a **generic SaaS dashboard**: no decorative gradient heroes, no violet/purple accent, no glassmorphism, no endless icon-card grids.
- **Don't** slip back into the **cluttered legacy academic** look: no `text-align: justify` body copy, no hairline `border-bottom` on an `h2` as a substitute for real hierarchy, no undifferentiated blue link-soup.
- **Don't** go **consumer or playful**: no bubbly oversized radii, no candy color, no gamified flourishes, no mascot energy.
- **Don't** reuse structure-family or affiliation hues for buttons, backgrounds, or ornament — that color belongs to data (the Encoding Rule).
- **Don't** nest a card inside a card, or use a `border-left`/`border-right` greater than `1px` as a colored accent stripe.
- **Don't** oversize display type past ~`2.25rem` or use gradient text; emphasis comes from weight and navy, not spectacle.
- **Don't** remove focus outlines or convey state (error, success, selected) through color alone.
