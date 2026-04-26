# 91 - Landing Page CRO Copywriting

Date: 2026-04-26

## Requirement

Optimize the marketing landing page for conversion rate using copywriting principles and Codemigo guidance.

## Research Inputs

- Codemigo knowledge search: `landing page CRO copywriting SaaS homepage conversion value proposition proof CTA`
- Codemigo use-case search: `Next.js landing page copywriting conversion optimization hero social proof pricing CTA implementation`
- Gartner Digital Markets: landing page copy should communicate value proposition through headline/subhead, use simple language, and emphasize benefits before features.
- Semrush: value proposition should be prominent, audience-specific, and paired with benefit-first copy.
- HubSpot: visitors skim landing pages, so headlines, subheads, images, CTA buttons, section headings, bullets, and short paragraphs carry the conversion burden.
- Unbounce: CRO improves yield from existing traffic by optimizing post-click clarity and action.

## Key Concepts

- Make the hero answer: what is this, who is it for, why does it matter, what should I do next?
- Use one primary conversion action and repeat it at decision points.
- Keep CTA wording consistent and low-friction.
- Make section headings carry the story for skimmers.
- Put product visuals near the hero so users can imagine the experience.
- Lead with benefits and use features as proof.
- Address objections close to the final CTA: control, setup friction, integrations, and missed-call uncertainty.
- Avoid vague superlatives; use concrete language around calls, WhatsApp, tasks, goals, calendar, and Gmail.

## Page Strategy

Primary conversion:

- Start a WhatsApp conversation with Charu.

Audience:

- People who know what they need to do but struggle to initiate and follow through.

Core promise:

- Charu calls at chosen times, helps pick one next action, records the commitment, and follows up in existing tools.

Proof/control points:

- Phone-first accountability.
- WhatsApp-first setup.
- No new app to manage.
- User-chosen call windows.
- Calendar/Gmail only after connection.

## Implementation Notes

- Keep `website/src/app/(marketing)/page.tsx` as a server component.
- Existing landing components are client components because they use `FadeIn`/Framer Motion.
- Use existing Tailwind tokens and Heroicons.
- Do not add forms or a second conversion path.
- Keep CTA language consistent: `Start on WhatsApp` / `Message Charu on WhatsApp`.
- Add an objection section before the final CTA.

## Gotchas

- Do not claim outcomes that are not measured yet.
- Do not claim medical/ADHD treatment benefits.
- Do not imply Calendar/Gmail are mandatory for everyone.
- Do not add fake testimonials or fake customer counts.
- Keep mobile text short enough to avoid wrapping badly inside buttons/cards.

## Relevant Links

- https://www.gartner.com/en/digital-markets/insights/landing-page-copywriting
- https://www.semrush.com/blog/landing-page-copywriting/
- https://blog.hubspot.com/marketing/landing-page-writing-tips
- https://unbounce.com/conversion-rate-optimization/cro-best-practices/

## Required Packages

No new packages required.
