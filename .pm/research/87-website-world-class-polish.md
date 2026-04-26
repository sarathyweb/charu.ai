# Website world-class polish

## Requirement

Raise the website quality across marketing and authenticated product surfaces:

- stronger first impression and clearer conversion path,
- product-specific visual storytelling,
- better trust/proof hierarchy,
- polished responsive layout and accessibility,
- no regressions in dashboard API parity or frontend tests.

## Research

### Codemigo knowledge/usecase

- A strong landing page hero needs immediate value clarity, a prominent CTA, product visuals, and trust signals above the fold.
- Marketing pages benefit from expressive typography, whitespace, visual hierarchy, and repeated conversion opportunities.
- World-class product pages use real or realistic product visuals instead of abstract decoration, because buyers want to see how the product works.
- SaaS/product dashboards should be quieter and denser than marketing pages: restrained typography, clear controls, structural neutrals, and predictable edit affordances.
- Reuse a small set of card/list/form patterns so the page feels intentional rather than assembled from unrelated modules.
- Responsive polish should be judged at both the section level and full-page level; a shared grid and consistent spacing rhythm matter more than one-off decorative effects.

### Official/local docs

The local Next 16 production checklist emphasizes:

- use layouts and `<Link>` for navigation,
- keep `"use client"` boundaries intentional,
- use optimized fonts and the Next `<Image>` component to reduce layout shift,
- run ESLint and `next build` before production,
- pay attention to Core Web Vitals and accessibility.

The local Next image docs emphasize:

- `<Image>` prevents layout shift through known dimensions,
- local images can be served from `public`,
- remote image allowlists should be specific.

### Existing site audit

- The hero was a split text/media layout with a gradient and decorative blobs; it did not put the product/brand as the main first-viewport signal.
- The page had good base content but weak proof density above the fold.
- Product visuals were mostly a chat mockup, while the newly implemented dashboard has a richer accountability surface to show.
- Some shared navigation imagery already moved to `next/image` during the dashboard parity pass.
- Dashboard API parity tests now protect core website behavior and should keep passing after visual polish.

## Implementation plan

- Replace the split hero with a full-bleed product scene: brand/product headline, proof metrics, primary CTA, and realistic dashboard/phone previews in the background layer.
- Remove decorative blob/orb utilities from the marketing surface and use structured bands, borders, and product panels instead.
- Add a proof strip and an operating-system style product section that shows calls, tasks, goals, calendar, and email working together.
- Tighten feature/CTA copy around concrete outcomes.
- Keep dashboard product UI functional and restrained; only add polish that does not alter the API contract.

## Gotchas

- Keep hero text out of a card.
- Do not add decorative orbs or abstract SVG hero art.
- Do not make text scale with viewport width.
- Keep test assertions stable by preserving key visible dashboard labels.

## Relevant references

- Local Next docs: `website/node_modules/next/dist/docs/01-app/02-guides/production-checklist.md`
- Local Next docs: `website/node_modules/next/dist/docs/01-app/01-getting-started/12-images.md`
- Web search: Next production and image optimization docs, W3C/WCAG accessibility guidance, and Core Web Vitals guidance.

## Required packages

No new runtime dependencies are required.
