# billet — brand kit

A stateless CLI that *billets* each repo's devcontainer onto a shared cloud Host.
This folder is the source of truth for the billet identity. Everything here is
self-contained — copy assets out, don't hotlink into external designs.

> **Full interactive guidelines:** open [`guidelines.html`](./guidelines.html) in a browser.
> **Quick reference:** [`BRAND.md`](./BRAND.md) (colors, type, rules, usage snippets).

---

## The mark — the berth rack

Two columns, three rows. Three berths are **posted** (filled with an accent),
three are left **open** (outlined). It is square-first, so it drops straight
into a favicon or app icon. **Never fill all six** — billet always leaves room.

| File | Use |
|---|---|
| `logo/mark-on-dark.svg` / `logo/mark-on-light.svg` | Mark only |
| `logo/logo-horizontal-on-dark.svg` / `…-on-light.svg` | Mark + wordmark, side by side |
| `logo/logo-stacked-on-dark.svg` / `…-on-light.svg` | Mark above wordmark |
| `logo/wordmark-on-dark.svg` / `…-on-light.svg` | Wordmark only |
| `logo/logo-horizontal-on-dark.png` / `…-on-light.png` | Raster lockups for READMEs / docs |

Use the `-on-dark` variant on dark grounds, `-on-light` on light grounds — the
magenta + mint accents stay identical; only ink and ground flip.

> **SVG note:** the wordmark/lockup SVGs use live `<text>` in Space Grotesk. They
> render correctly in a browser or any editor with the font installed. For print
> or guaranteed rendering (e.g. GitHub, which strips webfonts from SVG), **outline
> the text** or use the **PNG** lockups.

### README header (auto-swaps with GitHub light/dark theme)

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="brand/logo/logo-horizontal-on-dark.png">
  <img alt="billet" src="brand/logo/logo-horizontal-on-light.png" width="240">
</picture>
```

---

## Favicon & app icon

The mark **is** the favicon — no wordmark. The aubergine tile ships baked in, so
it stays self-contained on any browser theme. At 32px and below, the open berths
trade their outline for a solid recessive fill (`#9C8BB2`) to stay crisp to 16px.

```
favicon/favicon.svg            scalable, tile baked in
favicon/favicon.ico            16 · 32 · 48 (multi-res)
favicon/favicon-16/32/48.png
favicon/apple-touch-icon.png   180×180
favicon/icon-192.png           PWA / maskable
favicon/icon-512.png           PWA / maskable
```

```html
<link rel="icon" href="/brand/favicon/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/brand/favicon/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/brand/favicon/apple-touch-icon.png">
```

---

## Social / OpenGraph card

`social-card.png` — 1200×630, ready for `og:image`. Doubles as a README hero.

```html
<meta property="og:image"
      content="https://raw.githubusercontent.com/rinman24/billet/main/brand/social-card.png">
```

---

See [`BRAND.md`](./BRAND.md) for the full color palette, type scale, voice, and
do / don't rules.
