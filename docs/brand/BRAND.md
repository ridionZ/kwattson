# kWattson brand

**kWattson** (said *kilo-watt-son*): a dependable assistant for every kilowatt in your home.

## Name
- Always written **kWattson**: lower-case **k**, capital **W**, as in the unit kW.
- Package, CLI, Docker image and URLs use lower case: `kwattson`.
- Don't write *KWattson*, *Kwattson*, *kWatson* or *K-Wattson*.

## Tagline
> Every kilowatt, wisely planned.

Short descriptor: *Self-hosted home energy planner: solar · battery · EV · tariffs.*

## Logo
| File | Use |
|---|---|
| `mark.svg` | App icon, favicon, avatar. Sun rising over the battery/horizon line, with a bolt. |
| `logo-light.svg` | Full logo on light backgrounds |
| `logo-dark.svg` | Full logo on dark backgrounds |
| `social-preview.png` | GitHub social preview (Settings → Social preview), 1280×640 |
| `mark-512.png` | Raster icon |
| `src/` | Editable sources with live text (needs Montserrat installed) |

**In the app UI** (headers, nav, small spaces) use the **mark + live text**: `mark.svg` followed by
"kWattson" as real text in Montserrat 700 ("kW" amber, "attson" navy, or white on dark), never the
full logo image. The full logo with tagline is only for large uses (README, social card, splash;
roughly 400 px wide or more) where the tagline is readable.

All delivered logo SVGs have text converted to outlines, so they render identically everywhere.
Keep clear space around the logo of at least half the mark's width. Don't recolour, stretch or add effects.

## Colour
| Role | Name | Hex | Notes |
|---|---|---|---|
| Primary | Grid Navy | `#0E2A47` | Backgrounds, headings, the mark tile |
| Accent | Sunlight Amber | `#F5A524` | "kW", solar, highlights on dark |
| Accent (on light) | Deep Amber | `#D97706` | Amber text on white (≥3:1, large text only) |
| Secondary | Charge Teal | `#2DD4BF` | Battery/charging, positive states |
| Neutral | Slate | `#475569` / `#CBD5E1` | Body text on light / on dark |
| Background dark | Night | `#0B1220` | Dark-mode page background |
| Background light | Paper | `#F8FAFC` | Light-mode page background |

Data colours in charts should stay distinct from these brand accents where needed (solar = amber,
battery = teal, grid import = slate/navy, export = a cooler blue). Never rely on colour alone.

## Typography
- **Montserrat** (SIL Open Font License) for the wordmark and display headings.
- UI and body text: the system font stack (`system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`),
  falling back gracefully; the dashboard loads no remote fonts (privacy and CSP).

## Voice
Plain, honest and friendly. Australian English. Say what the system did and why, with numbers.
Label modelled figures as modelled. No hype: "saved about $1.40 today (estimated)" rather than "Massive savings!".
