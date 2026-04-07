# Charu AI Website Design Spec

**Date:** 2026-04-07
**Domain:** charuai.com
**Approach:** Single-page scroll landing + /privacy + /terms
**Goal:** Drive WhatsApp opt-in (wa.me link on mobile, QR code on desktop)

---

## Tech Stack

- **Framework:** Next.js (App Router)
- **Styling:** Tailwind CSS
- **Typography:** DM Serif Display (headlines), Inter (body)
- **Hosting:** Separate repo from main app
- **Location:** `website/` folder in project root, added to `.gitignore`

## Color Palette

| Token | Value | Usage |
|-------|-------|-------|
| Primary | `#5c3520` | CTAs, headings, active elements |
| Background | `#fdf8f3` | Page background (warm cream) |
| Surface | `#ffffff` | Cards, elevated surfaces |
| Text | `#3b2314` | Body text (brown-tinted dark) |
| Muted | `#8a6d4f` | Secondary text, descriptions |
| Accent Surface | `#f5ece3` | Section backgrounds, pain/about sections |
| Warm Gray | `#e8e0d8` | Borders, dividers (brown-tinted) |
| Dark | `#3b2314` | Footer background |

All neutrals are derived from the brown hue by desaturating (monochromatic system). No pure grays.

## Border Radius Scale

| Size | Value | Usage |
|------|-------|-------|
| Small | `8px` | Inputs, badges |
| Medium | `16px` | Cards, sections |
| Large | `24px` | Buttons, CTAs (pill style) |

Coordinate inner/outer radii on nested elements.

## Typography Scale

- **Hero headline:** DM Serif Display, 48-56px desktop, 36-40px mobile
- **Section headings:** DM Serif Display, 36px desktop, 28px mobile
- **Body:** Inter, 16px (1rem), line-height 1.6
- **Body small:** Inter, 14px, for footer/meta
- **Intro paragraphs:** Inter, 18px (1.125rem) for lead text
- All sizing in `rem` units for accessibility.

## Target Audience (Validated)

- ADHD adults 25-40, especially "Drowning Professionals" (lawyers, accountants, engineers losing billable hours)
- Remote knowledge workers struggling with task initiation
- People who've tried planners, apps, reminders — all abandoned within weeks
- People who need external human-like presence, not another notification

## Core Positioning

"This audience does not need another tool they have to remember to open. They need something that opens them."

- **Not a productivity app.** An accountability presence.
- **Not reminders.** Phone calls you can't swipe away.
- **Not a chatbot.** A friend who gets it.
- **Not motivation.** Structure. Presence. Activation.

## Key Research-Backed Copy Principles

1. **Use customer language verbatim:** "executive dysfunction," "task paralysis," "body doubling," "adultsitter," "time blindness," "ghosting," "the gap between knowing and doing"
2. **Never say:** "just do it," "productivity hack," "motivation," "fix/cure" language around ADHD, "remind" (the word is poisoned)
3. **Lead with pain, then dream, then fix** (pain-dream-fix arc)
4. **Sell the outcome:** "Actually finish things" not "AI-powered task management"
5. **Acknowledge the tool graveyard:** They've tried everything. Name it.
6. **Non-judgment is structural, not tonal:** It's the core value prop
7. **Frame as accommodation, not crutch:** Normalize the need

---

## Page Structure

### 1. Navbar

- **Sticky** on scroll, `#fdf8f3` cream bg, subtle `#e8e0d8` bottom border
- **Logo** (left): "Charu" in DM Serif Display, `#5c3520`
- **Nav links** (center): How It Works / Features / About (anchor scroll)
  - Inter, `#8a6d4f`, 14px — smaller than body, doesn't compete with hero
  - Concise labels, consistent format
- **CTA button** (right): "Try Charu" — `#5c3520` bg, white text, 24px pill radius
  - Visually distinct from nav active states
- **Mobile:** Hamburger menu, CTA stays visible
- Max 5 nav items

### 2. Hero (Promise)

**Layout:** Two-column desktop (text left 55%, mockup right 45%). Single column mobile (text → mockup → CTA). Generous negative space between columns.

**Headline** (DM Serif Display, 48-56px):
> "You know what you need to do. Charu gets you to actually do it."

- Specific to the product's differentiator (the knowing-doing gap)
- Uses "you" directly
- Names the product

**Subhead** (Inter, 18px, `#8a6d4f`):
> "An AI accountability partner that calls you on WhatsApp. Daily check-ins, calendar sync, and task tracking — no new app to download."

- Explains what + who + how
- "Calls you" is the differentiator
- "No new app" removes friction

**CTA:**
- Desktop: WhatsApp QR code (white card with subtle shadow) + text "Scan to chat with Charu" + fallback text link "Or open on your phone"
- Mobile: Full-width pill button "Message Charu on WhatsApp" → wa.me link
- CTA aligned left, within natural scan path
- Button doesn't overpower headline

**Visual:** Right side — styled WhatsApp conversation mockup showing:
- Charu: "Hey! How are you feeling about today?"
- User: "I have 20 things to do and can't start"
- Charu: "That happens. Let's pick one small thing. What feels most urgent?"

Mockup designed as unified composition with text (not a separate asset). Fits within ~720px real viewport.

**Consistent alignment:** Headline, subhead, and CTA all left-aligned. Common left edge. Headline width doesn't exceed subhead block significantly.

### 3. Pain Section

**Background:** `#f5ece3` (visually distinct break from hero)

**Heading** (DM Serif Display, 36px):
> "Your to-do list is running your life"

- Names the pain directly (not generic "Sound familiar?")
- Emotionally loaded

**3 pain cards** (white bg, 16px radius, subtle shadow), each with icon + text:

1. "You have 20 things to do and can't start any of them. It's not laziness — your brain just won't bridge the gap between knowing and doing."

2. "Your day ends and you're not sure what you actually did. The guilt hits at 11pm, and you promise tomorrow will be different. It never is."

3. "You've tried the planners. The apps. The reminders you swipe away without reading. They all worked for a week — then became background noise."

- Each card names a pain + adds an emotional layer (guilt, the graveyard)
- Uses exact customer language
- Centered layout, generous whitespace

**Dream bridge** (after cards, centered text):
> "Imagine ending every day knowing you actually did the thing. Not everything — just the thing that mattered."

- Flips pain into desired outcome before showing the fix
- Modest, not grandiose

### 4. How It Works (Fix)

**Background:** White (clean separation from pain section)

**Heading** (DM Serif Display, 36px):
> "How Charu works"

**Subhead:**
> "Three steps to a calmer day."

**3 numbered steps** — horizontal desktop, vertical mobile. Visual connectors (subtle lines/arrows) between steps to reinforce sequence.

**Step 1: "Say hi on WhatsApp"**
- "Message Charu. Tell her your name and when you'd like your calls. That's it — two minutes, no app to download."
- Visual: WhatsApp screenshot of onboarding conversation

**Step 2: "Get daily check-in calls"**
- "Charu calls you every morning to plan, midday to nudge, and evening to reflect. She asks what matters, then helps you start."
- Visual: Phone notification mockup of incoming Charu call

**Step 3: "Actually finish things"**
- "Tasks tracked. Calendar blocked. Emails handled. All through the same WhatsApp chat you already use."
- Visual: Task list / calendar mockup in WhatsApp

Each step: Large number (DM Serif Display, `#5c3520`), bold headline (Inter), description (Inter, `#8a6d4f`), visual mockup.

**CTA after steps:**
> "Start on WhatsApp" — pill button, `#5c3520` bg

**Outcome closer** (centered text after CTA):
> "Your day, handled."

### 5. Features (Evidence)

**Background:** `#fdf8f3` (cream)

**Heading** (DM Serif Display, 36px):
> "Everything through one chat"

**Grid:** 3x2 desktop, single column mobile. One featured card (Daily accountability calls) spans 2 columns on desktop for visual hierarchy.

**Featured card (larger):**
- **Daily accountability calls** — "Morning, midday, and evening. Charu calls you — so you actually start. Not a notification you swipe away. A voice that shows up."

**Standard cards (5):**
- **WhatsApp check-ins** — "Quick nudges between calls. No new app to learn — just the chat you already have."
- **Google Calendar sync** — "See your day, find free time, and block hours for the work that matters."
- **Gmail management** — "Surface emails that need replies. Charu drafts them — you just say yes."
- **Task tracking** — "Mention something you need to do. It's saved. Finish it? It's done. No separate app."
- **Smart scheduling** — "Reschedule calls, skip one, or say 'call me in 30 minutes.' Charu adapts to your day."

Cards: white bg, 16px radius, subtle shadow, left-aligned text. Icon + bold headline (Inter, `#3b2314`) + one-line outcome-focused copy (Inter, `#8a6d4f`). Clear type contrast between headline and description.

### 6. About / Why Charu (Trust)

**Background:** `#f5ece3` (distinct section break)

**Heading** (DM Serif Display, 36px):
> "Built for you, when starting feels impossible"

- Uses "you" (direct address)
- Names the pain

**Copy** (Inter, 16px, 2-3 short paragraphs):

> "Starting tasks is genuinely hard. It's not a character flaw — it's a gap between knowing and doing that no planner or reminder can fix."

> "Charu doesn't say 'just do it' or guilt you into productivity. She shows up like a friend who gets it — calls you, asks what matters, and helps you take the first step. If you miss a call, no big deal. She'll be there tomorrow."

> "People already pay humans $30-40 a month for exactly this — daily accountability calls that actually work. We built the AI version, so it never ghosts, never judges, and costs a fraction of a coach."

- **Transformation outcome:** "Your morning starts with a plan. Your day has structure. And you stop going to bed wondering where it all went."
- **Social proof line:** "Built from thousands of real conversations about what people with ADHD actually need — not what productivity gurus think they need."
- **Supporting imagery:** Warm illustration or abstract visual reinforcing human connection

**Soft CTA at end of section:**
> "See how Charu works" (anchor link to How It Works section)

### 7. CTA Banner (Final Push)

**Background:** `#6b4430` (slightly lighter/warmer than footer `#3b2314` to ensure visual separation)

**Heading** (DM Serif Display, 36px, white):
> "Ready to stop planning and start doing?"

**Subhead** (Inter, 16px, `rgba(255,255,255,0.75)` — lower opacity for hierarchy):
> "Two minutes to set up. No app to download. No judgment if you miss a day."

**CTA:**
- Desktop: QR code on white card + fallback clickable link
- Mobile: Full-width white pill button "Message Charu on WhatsApp" (brown text inside, min 44px touch target)

Verify WCAG AA contrast: use pure white for body text on brown bg. Cream only for subtle elements.

### 8. Footer

**Background:** `#3b2314` (dark brown, clearly distinct from CTA section)

- **Logo** (left): "Charu" in cream
- **Links** (right): Privacy Policy / Terms & Conditions — subtle underline or lighter color for discoverability
- **Copyright:** "2026 Charu AI. Made with care for people who struggle to start."
- Body-small (14px), subdued, consistent styling on legal links
- Footer reduces visual intensity — never pulls attention from CTA above

### 9. /privacy

**Layout:** Simple title-and-body, max-width `680px`, shared navbar + footer

**Typography:**
- H1: page title (DM Serif Display, 36px)
- H2: section headings that carry the narrative for skimmers
- Body: Inter, 16px/1rem, `#3b2314`, line-height 1.6-1.7
- Lead paragraph: 18px to orient readers
- Meta ("Last updated"): 14px body-small
- Heading spacing (2rem) > paragraph spacing (1rem)
- Mobile responsive: scale H1/H2 below 640px

**Content sections:**
1. What data we collect (phone number, name, timezone, Google OAuth tokens)
2. How we use your data (accountability calls, task management, calendar/email integration)
3. WhatsApp & Twilio (message routing, call delivery, no message content stored by Twilio beyond delivery)
4. Google integration (OAuth scopes: Calendar read/write, Gmail read/send — used only for features user explicitly connects)
5. Data storage & security (PostgreSQL on GCP, encrypted tokens, no plain-text credentials)
6. Data retention (active account data kept while subscribed, deleted within 30 days of account deletion)
7. Your rights (access, export, delete your data — contact email provided)
8. Third-party services (Google Cloud, Twilio, Vertex AI — links to their privacy policies)
9. Changes to this policy (notification via WhatsApp)
10. Contact (email address)

### 10. /terms

**Layout:** Same as /privacy

**Content sections:**
1. Service description (AI accountability assistant via WhatsApp, voice calls, calendar/email integration)
2. Eligibility (18+, valid WhatsApp account)
3. Account & access (phone number is identity, one account per number)
4. Acceptable use (no abuse, no automated access, no reverse engineering)
5. Google integration disclaimer (Charu accesses Calendar/Gmail only with explicit consent, user can revoke anytime)
6. WhatsApp & Twilio dependency (service depends on third-party platforms, not responsible for their outages)
7. AI-generated content disclaimer (Charu is AI, not a therapist/coach/medical professional, responses may be imperfect)
8. Limitation of liability (service provided as-is, no guarantees on productivity outcomes)
9. Account termination (user can stop anytime by messaging "stop", we can terminate for abuse)
10. Changes to terms (notification via WhatsApp, continued use = acceptance)
11. Governing law (specify jurisdiction)

---

## Responsive Behavior

- **Desktop (>1024px):** Full two-column hero, 3x2 feature grid, horizontal steps
- **Tablet (640-1024px):** Single column hero, 2x3 feature grid, vertical steps
- **Mobile (<640px):** Single column everything, full-width CTAs, scaled-down headings, hamburger nav

## CTA Logic (Desktop vs Mobile)

```
if (viewport < 768px) {
  show wa.me pill button (full-width)
} else {
  show QR code + fallback text link
}
```

Detect via CSS media queries + JS for QR code generation.

---

## Key Research Sources

All copy is informed by validated research from the personal-assistant-research repo:
- **assumptions.md:** 20+ validated hypotheses with engagement-weighted Reddit data
- **sales-safari-reddit.md:** 50+ real user quotes with engagement scores (4,000+ upvotes on key posts)
- **deep-research/:** Executive dysfunction, notification fatigue, accountability partner failure analysis
- **watering-holes.md:** Target audience demographics, online hangouts, SEO keyword clusters

## Copy Voice Rules (from research)

1. Mirror the audience's own language — "executive dysfunction" not "lack of motivation"
2. Acknowledge the tool graveyard — they've tried everything
3. Name the shame, then dissolve it — "it's not a character flaw"
4. Position as presence, not tool — "something that opens you" not "something you open"
5. Non-judgment is structural — after missed calls, tone is "no big deal"
6. Never imply willpower deficit — the problem is neurological, not moral
7. The word "remind" is toxic — use "check in," "show up," "call"
