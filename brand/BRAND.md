# billet — brand reference

Everything runs on one metaphor: a **posting**. A `billet` is an assignment of
quarters. A **Host** has berths; billet **posts** each repo into one. Three
berths posted, three left open.

- **Repo:** github.com/rinman24/billet · MIT · Python 3.11+
- **Tagline:** A stateless CLI for shared devcontainers.
- **One-liner:** Post any repo's devcontainer to a shared Host — from the command line.

---

## 1. The mark

A **berth rack**: 2 columns × 3 rows. Three berths posted (accent-filled), three
open (outlined). Square-first so it works as a favicon or app icon.

| Property | Value |
|---|---|
| Grid | 2 × 3 berths |
| Berth ratio | 1 : 0.7 |
| Gap | ¼ berth |
| Corner radius | 0.15 × berth width |
| Posted | 3 of 6 (magenta, mint, magenta) |
| Clearspace | 1 berth on every side |
| Minimum size | 16px |

- **Occupied berth** — carries an accent (magenta or mint).
- **Open berth** — outline only, in ink. Below ~32px the outline is replaced by a
  **solid recessive fill `#9C8BB2`** so berths stay crisp down to 16px.
- **Never fill all six.** billet always leaves room.

---

## 2. Color

The ground dominates. Accents are punctuation — one action per view. **Magenta
leads and means _running_; mint supports and means _building_.**

### Core (dark theme)

| Name | Hex | Role |
|---|---|---|
| Aubergine | `#17101F` | Ground — the background |
| Surface | `#211829` | Cards, terminals, raised panels |
| Ink | `#EDE6F2` | Headings & body on the ground |
| Muted | `#837390` | Meta, labels, quiet detail |
| **Magenta** | `#C05CE0` | **Running · lead** — prompt, active state, the one action |
| **Mint** | `#3FD2BE` | **Building · second** — progress, supporting accent |
| Open fill | `#9C8BB2` | Open-berth fill at small sizes only |

### Light theme neutrals

| Name | Hex | Role |
|---|---|---|
| Paper | `#FAF9F7` | Light ground |
| White | `#FFFFFF` | Cards |
| Ink | `#141414` | Text |
| Line | `#E7E4DF` | Hairline |
| Gray | `#8C8A85` | Secondary |

---

## 3. Typography

Two families. One does words, one does machines.

- **Space Grotesk** — wordmark, display, headings, body, UI. Weights 400 / 500 / 600 / 700.
- **JetBrains Mono** — code, CLI, labels, status, data. Weights 400 / 500 / 600 / 700.

Both are on Google Fonts.

**Wordmark rule:** always lowercase, Space Grotesk **600**, letter-spacing
**−0.045em**. Never substitute another face. The wordmark stays ink — never colored.

### Type scale

| Role | Family | Spec |
|---|---|---|
| Display | Space Grotesk 600 | 54px · −0.045em |
| Heading | Space Grotesk 600 | 32px · −0.02em |
| Body | Space Grotesk 400 | 20px · line-height 1.6 |
| Label | JetBrains Mono 500 | 13px · .14em · UPPERCASE |
| Code | JetBrains Mono 400 | 15px · line-height 1.6 |

---

## 4. Voice

- Terse and lowercase. No exclamation.
- Present tense — *posting*, not *will post*.
- Status is a **color**, then a word.
- Commands read like a sentence: `billet post api`.

Command surface: `post <repo>` · `ls` · `shell <repo>` · `logs <repo>` · `rm <repo>`.

---

## 5. Do / Don't

**Do**
- Keep the wordmark ink (never colored).
- 3 posted · 3 open — always leave berths open.
- Keep the mark upright and flat.
- Magenta = running; mint = building.

**Don't**
- Color the wordmark.
- Fill every berth.
- Rotate or skew the rack.
- Add glow, gradient, or shadow to the mark.
- Stretch or condense the wordmark.
- Crowd the mark — keep one berth of clearspace.

---

## 6. Asset index

```
brand/
├── README.md                 folder overview + embedding snippets
├── BRAND.md                  this file
├── guidelines.html           full interactive brand kit (open in a browser)
├── social-card.png           1200×630 OpenGraph / README hero
├── logo/
│   ├── mark-on-dark.svg           mark only (light ink)
│   ├── mark-on-light.svg          mark only (dark ink)
│   ├── wordmark-on-dark.svg       wordmark only
│   ├── wordmark-on-light.svg
│   ├── logo-horizontal-on-dark.svg   mark + wordmark
│   ├── logo-horizontal-on-light.svg
│   ├── logo-horizontal-on-dark.png   raster, for READMEs
│   ├── logo-horizontal-on-light.png
│   ├── logo-stacked-on-dark.svg      mark above wordmark
│   └── logo-stacked-on-light.svg
└── favicon/
    ├── favicon.svg                scalable, aubergine tile baked in
    ├── favicon.ico                16 · 32 · 48 multi-res
    ├── favicon-16.png
    ├── favicon-32.png
    ├── favicon-48.png
    ├── apple-touch-icon.png       180×180
    ├── icon-192.png               PWA / maskable
    └── icon-512.png               PWA / maskable
```

**SVG wordmark caveat:** lockup/wordmark SVGs use live text in Space Grotesk.
Install the font or outline the glyphs for production; for GitHub/README display
use the PNG lockups (GitHub strips webfonts from inline SVG).

Usage snippets (favicon `<link>`s, README `<picture>` swap, `og:image`) are in
[`README.md`](./README.md).
