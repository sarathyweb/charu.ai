# Charu AI Website - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the charuai.com marketing website as a Next.js + Tailwind single-page landing with /privacy and /terms pages.

**Architecture:** Next.js App Router with static export. All content is static (no API calls). Landing page is a single scroll with 8 sections (navbar, hero, pain, how-it-works, features, about, CTA banner, footer). Two additional routes for legal pages. QR code generated client-side via `qrcode` library. Responsive CTA toggles via Tailwind classes (no JS viewport detection).

**Tech Stack:** Next.js 15 (App Router), Tailwind CSS 4, TypeScript, DM Serif Display + Inter (Google Fonts via next/font), qrcode library for WhatsApp QR.

**Design spec:** `docs/superpowers/specs/2026-04-07-charuai-website-design.md`

**WhatsApp number for CTA:** The wa.me link and QR code should use a configurable constant. Use `+14155238886` as default (from .env.example). The actual number will be swapped before launch.

**IMPORTANT copy rules:** No em dashes. No emojis. No AI writing tells (rule-of-three lists, "Not X, but Y" parallelisms, words like "delve", "tapestry", "vibrant", "foster", "underscore", "showcase", "pivotal", "crucial", "enhance"). Write like a person, not a language model.

---

### File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `website/package.json` | Next.js project config |
| Create | `website/tsconfig.json` | TypeScript config |
| Create | `website/next.config.ts` | Next.js config |
| Create | `website/tailwind.config.ts` | Tailwind config with custom colors, radius, fonts |
| Create | `website/src/app/globals.css` | Tailwind directives + base styles |
| Create | `website/src/app/layout.tsx` | Root layout with fonts, metadata, navbar, footer |
| Create | `website/src/app/page.tsx` | Landing page (all sections) |
| Create | `website/src/app/privacy/page.tsx` | Privacy policy page |
| Create | `website/src/app/terms/page.tsx` | Terms of service page |
| Create | `website/src/components/Navbar.tsx` | Sticky navbar with mobile hamburger |
| Create | `website/src/components/Hero.tsx` | Hero section with WhatsApp mockup |
| Create | `website/src/components/PainSection.tsx` | Pain cards + dream bridge |
| Create | `website/src/components/HowItWorks.tsx` | 3-step process section |
| Create | `website/src/components/Features.tsx` | Feature cards grid |
| Create | `website/src/components/About.tsx` | About/trust section |
| Create | `website/src/components/CtaBanner.tsx` | Final CTA section |
| Create | `website/src/components/Footer.tsx` | Footer with legal links |
| Create | `website/src/components/WhatsAppCta.tsx` | Responsive QR/button CTA component |
| Create | `website/src/components/WhatsAppMockup.tsx` | Styled chat mockup for hero |
| Create | `website/src/lib/constants.ts` | WhatsApp number, wa.me URL, UTM params |
| Modify | `.gitignore` | Add `website/` entry |

---

### Task 1: Scaffold Next.js project and add website/ to .gitignore

**Files:**
- Create: `website/` (via create-next-app)
- Modify: `.gitignore`

- [ ] **Step 1: Add website/ to .gitignore**

Add this line to the end of `/home/sarathy/projects/charu.ai/.gitignore`:

```
# Website (separate repo for deployment)
website/
```

- [ ] **Step 2: Scaffold the Next.js project**

Run:
```bash
cd /home/sarathy/projects/charu.ai && npx create-next-app@latest website --typescript --tailwind --eslint --app --src-dir --no-import-alias --use-npm
```

When prompted, accept defaults. This creates the `website/` folder with Next.js + Tailwind + TypeScript + App Router + src directory.

Expected: `website/` folder exists with `package.json`, `src/app/`, `tailwind.config.ts`, etc.

- [ ] **Step 3: Verify it runs**

Run:
```bash
cd /home/sarathy/projects/charu.ai/website && npm run dev
```

Expected: Server starts at http://localhost:3000 with the default Next.js page.

Kill the dev server after confirming.

- [ ] **Step 4: Commit .gitignore change only**

```bash
cd /home/sarathy/projects/charu.ai
git add .gitignore
git commit -m "chore: add website/ to .gitignore"
```

Note: The website/ folder itself is gitignored and won't be committed to this repo.

---

### Task 2: Configure Tailwind theme, fonts, and global styles

**Files:**
- Modify: `website/tailwind.config.ts`
- Modify: `website/src/app/globals.css`
- Modify: `website/src/app/layout.tsx`
- Create: `website/src/lib/constants.ts`

- [ ] **Step 1: Create constants file**

Create `website/src/lib/constants.ts`:

```typescript
export const WHATSAPP_NUMBER = "14155238886";
export const WHATSAPP_URL = `https://wa.me/${WHATSAPP_NUMBER}?text=${encodeURIComponent("Hi Charu!")}`;
export const WHATSAPP_URL_WITH_UTM = `${WHATSAPP_URL}&utm_source=charuai.com&utm_medium=website&utm_campaign=launch`;
export const CONTACT_EMAIL = "sarathy@sarathywebservices.com";
```

- [ ] **Step 2: Configure Tailwind theme**

Replace `website/tailwind.config.ts` with:

```typescript
import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: "#5c3520",
        background: "#fdf8f3",
        surface: "#ffffff",
        text: "#3b2314",
        muted: "#7a5c3f",
        "accent-surface": "#f5ece3",
        "warm-gray": "#e8e0d8",
        dark: "#3b2314",
        "cta-brown": "#6b4430",
      },
      borderRadius: {
        sm: "8px",
        md: "16px",
        lg: "24px",
      },
      maxWidth: {
        container: "1120px",
        prose: "680px",
      },
      boxShadow: {
        card: "0 2px 12px rgba(0,0,0,0.06)",
        qr: "0 4px 20px rgba(0,0,0,0.08)",
      },
      fontFamily: {
        serif: ["var(--font-dm-serif)", "serif"],
        sans: ["var(--font-inter)", "sans-serif"],
      },
    },
  },
  plugins: [],
};
export default config;
```

- [ ] **Step 3: Set up global CSS**

Replace `website/src/app/globals.css` with:

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  html {
    scroll-behavior: smooth;
    scroll-padding-top: 4rem;
  }

  body {
    @apply bg-background text-text font-sans;
    font-size: 1rem;
    line-height: 1.6;
  }

  h1, h2, h3 {
    @apply font-serif text-primary;
  }
}
```

- [ ] **Step 4: Set up root layout with fonts and metadata**

Replace `website/src/app/layout.tsx` with:

```tsx
import type { Metadata } from "next";
import { DM_Serif_Display, Inter } from "next/font/google";
import "./globals.css";

const dmSerif = DM_Serif_Display({
  weight: "400",
  subsets: ["latin"],
  variable: "--font-dm-serif",
  display: "swap",
});

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Charu AI - Your accountability partner that actually calls you",
  description:
    "Charu calls your phone every day to help you start, stay on track, and finish. Daily check-ins on WhatsApp. No new app to download.",
  openGraph: {
    title: "Charu AI - Your accountability partner that actually calls you",
    description:
      "Charu calls your phone every day to help you start, stay on track, and finish. Daily check-ins on WhatsApp. No new app to download.",
    type: "website",
    url: "https://charuai.com",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${dmSerif.variable} ${inter.variable}`}>
      <body className="antialiased">{children}</body>
    </html>
  );
}
```

- [ ] **Step 5: Verify the setup**

Run:
```bash
cd /home/sarathy/projects/charu.ai/website && npm run dev
```

Expected: Page loads with cream background (#fdf8f3) and Inter font applied to body text.

---

### Task 3: Build Navbar component

**Files:**
- Create: `website/src/components/Navbar.tsx`

- [ ] **Step 1: Create the Navbar**

Create `website/src/components/Navbar.tsx`:

```tsx
"use client";

import { useState } from "react";
import Link from "next/link";

const navLinks = [
  { label: "How It Works", href: "#how-it-works" },
  { label: "Features", href: "#features" },
  { label: "About", href: "#about" },
];

export default function Navbar() {
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <nav className="sticky top-0 z-50 bg-background/95 backdrop-blur-sm border-b border-warm-gray">
      <div className="mx-auto max-w-container px-6 h-16 flex items-center justify-between">
        <Link href="/" className="font-serif text-primary text-2xl">
          Charu
        </Link>

        {/* Desktop nav */}
        <div className="hidden md:flex items-center gap-8">
          {navLinks.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="text-muted text-sm hover:text-primary transition-colors"
            >
              {link.label}
            </a>
          ))}
          <a
            href="#cta"
            className="bg-primary text-white text-sm px-5 py-2 rounded-lg hover:opacity-90 transition-opacity"
          >
            Try Charu
          </a>
        </div>

        {/* Mobile hamburger */}
        <div className="flex md:hidden items-center gap-4">
          <a
            href="#cta"
            className="bg-primary text-white text-sm px-4 py-2 rounded-lg"
          >
            Try Charu
          </a>
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="text-primary p-2"
            aria-label="Toggle menu"
          >
            <svg
              className="w-6 h-6"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              {menuOpen ? (
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              ) : (
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M4 6h16M4 12h16M4 18h16"
                />
              )}
            </svg>
          </button>
        </div>
      </div>

      {/* Mobile menu dropdown */}
      {menuOpen && (
        <div className="md:hidden border-t border-warm-gray bg-background px-6 py-4 space-y-3">
          {navLinks.map((link) => (
            <a
              key={link.href}
              href={link.href}
              onClick={() => setMenuOpen(false)}
              className="block text-muted text-sm hover:text-primary"
            >
              {link.label}
            </a>
          ))}
        </div>
      )}
    </nav>
  );
}
```

---

### Task 4: Build Footer component

**Files:**
- Create: `website/src/components/Footer.tsx`

- [ ] **Step 1: Create the Footer**

Create `website/src/components/Footer.tsx`:

```tsx
import Link from "next/link";

export default function Footer() {
  return (
    <footer className="bg-dark text-accent-surface">
      <div className="mx-auto max-w-container px-6 py-10 flex flex-col md:flex-row items-center justify-between gap-4">
        <span className="font-serif text-xl text-accent-surface">Charu</span>

        <div className="flex gap-6 text-sm">
          <Link
            href="/privacy"
            className="underline underline-offset-2 hover:text-white transition-colors"
          >
            Privacy Policy
          </Link>
          <Link
            href="/terms"
            className="underline underline-offset-2 hover:text-white transition-colors"
          >
            Terms & Conditions
          </Link>
        </div>

        <p className="text-sm text-accent-surface/70">
          2026 Charu AI. Made with care for people who struggle to start.
        </p>
      </div>
    </footer>
  );
}
```

---

### Task 5: Build WhatsApp CTA and Mockup components

**Files:**
- Create: `website/src/components/WhatsAppCta.tsx`
- Create: `website/src/components/WhatsAppMockup.tsx`

- [ ] **Step 1: Install qrcode library**

Run:
```bash
cd /home/sarathy/projects/charu.ai/website && npm install qrcode @types/qrcode
```

- [ ] **Step 2: Create WhatsAppCta component**

Create `website/src/components/WhatsAppCta.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { WHATSAPP_URL_WITH_UTM } from "@/lib/constants";

export default function WhatsAppCta() {
  const [qrSrc, setQrSrc] = useState<string>("");

  useEffect(() => {
    QRCode.toDataURL(WHATSAPP_URL_WITH_UTM, {
      width: 200,
      margin: 2,
      color: { dark: "#3b2314", light: "#ffffff" },
    }).then(setQrSrc);
  }, []);

  return (
    <>
      {/* Desktop: QR code */}
      <div className="hidden md:flex flex-col items-start gap-3">
        <div className="bg-white p-4 rounded-md shadow-qr">
          {qrSrc && (
            <img src={qrSrc} alt="Scan to chat with Charu on WhatsApp" width={200} height={200} />
          )}
        </div>
        <p className="text-sm text-muted">Scan to chat with Charu</p>
        <a
          href={WHATSAPP_URL_WITH_UTM}
          className="text-sm text-primary underline underline-offset-2"
        >
          Or open on your phone
        </a>
      </div>

      {/* Mobile: Button */}
      <div className="block md:hidden w-full">
        <a
          href={WHATSAPP_URL_WITH_UTM}
          className="block w-full text-center bg-primary text-white py-4 rounded-lg text-base font-medium hover:opacity-90 transition-opacity"
        >
          Message Charu on WhatsApp
        </a>
      </div>
    </>
  );
}
```

- [ ] **Step 3: Create WhatsAppMockup component**

Create `website/src/components/WhatsAppMockup.tsx`:

```tsx
export default function WhatsAppMockup() {
  const messages = [
    { from: "charu", text: "Hey! How are you feeling about today?" },
    { from: "user", text: "I have 20 things to do and can't start" },
    {
      from: "charu",
      text: "That happens. Let's pick one small thing. What feels most urgent?",
    },
  ];

  return (
    <div className="bg-white rounded-md shadow-card p-5 max-w-sm mx-auto md:mx-0">
      {/* Mock header */}
      <div className="flex items-center gap-3 pb-4 border-b border-warm-gray mb-4">
        <div className="w-10 h-10 rounded-full bg-primary flex items-center justify-center text-white font-serif text-sm">
          C
        </div>
        <div>
          <p className="font-medium text-text text-sm">Charu</p>
          <p className="text-xs text-muted">online</p>
        </div>
      </div>

      {/* Messages */}
      <div className="space-y-3">
        {messages.map((msg, i) => (
          <div
            key={i}
            className={`max-w-[80%] px-3 py-2 rounded-md text-sm leading-relaxed ${
              msg.from === "charu"
                ? "bg-accent-surface text-text"
                : "bg-primary text-white ml-auto"
            }`}
          >
            {msg.text}
          </div>
        ))}
      </div>
    </div>
  );
}
```

---

### Task 6: Build Hero section

**Files:**
- Create: `website/src/components/Hero.tsx`

- [ ] **Step 1: Create Hero component**

Create `website/src/components/Hero.tsx`:

```tsx
import WhatsAppCta from "./WhatsAppCta";
import WhatsAppMockup from "./WhatsAppMockup";

export default function Hero() {
  return (
    <section className="bg-background py-12 md:py-20">
      <div className="mx-auto max-w-container px-6 flex flex-col md:flex-row items-center gap-12">
        {/* Text column */}
        <div className="md:w-[55%] space-y-6">
          <h1 className="font-serif text-primary text-4xl md:text-5xl lg:text-[3.5rem] leading-tight">
            You know what you need to do. Charu gets you to actually do it.
          </h1>
          <p className="text-muted text-lg max-w-xl">
            An AI accountability partner that calls your phone and checks in on
            WhatsApp. Daily calls, calendar sync, and task tracking - no new app
            to download.
          </p>
          <WhatsAppCta />
        </div>

        {/* Mockup column */}
        <div className="md:w-[45%]">
          <WhatsAppMockup />
        </div>
      </div>
    </section>
  );
}
```

---

### Task 7: Build Pain Section

**Files:**
- Create: `website/src/components/PainSection.tsx`

- [ ] **Step 1: Create PainSection component**

Create `website/src/components/PainSection.tsx`:

```tsx
const pains = [
  "You have 20 things to do and can't start any of them. It's not laziness - your brain just won't bridge the gap between knowing and doing.",
  "Your day ends and you're not sure what you actually did. The guilt hits at 11pm, and you promise tomorrow will be different. It never is.",
  "You've tried the planners. The apps. The reminders you swipe away without reading. They all worked for a week - then became background noise.",
];

export default function PainSection() {
  return (
    <section className="bg-accent-surface py-16 md:py-20">
      <div className="mx-auto max-w-container px-6">
        <h2 className="font-serif text-primary text-3xl md:text-4xl text-center mb-12">
          Your to-do list is running your life
        </h2>

        <div className="grid md:grid-cols-3 gap-6 max-w-4xl mx-auto">
          {pains.map((pain, i) => (
            <div
              key={i}
              className="bg-white rounded-md shadow-card p-6 text-text text-base leading-relaxed"
            >
              {pain}
            </div>
          ))}
        </div>

        <p className="text-center text-muted text-lg mt-12 max-w-xl mx-auto">
          Imagine ending every day knowing you actually did the thing. Not
          everything - just the thing that mattered.
        </p>
      </div>
    </section>
  );
}
```

---

### Task 8: Build How It Works section

**Files:**
- Create: `website/src/components/HowItWorks.tsx`

- [ ] **Step 1: Create HowItWorks component**

Create `website/src/components/HowItWorks.tsx`:

```tsx
import WhatsAppCta from "./WhatsAppCta";

const steps = [
  {
    num: "1",
    title: "Say hi on WhatsApp",
    desc: "Message Charu. Tell her your name and when you'd like your calls. That's it - two minutes, no app to download.",
  },
  {
    num: "2",
    title: "Get daily check-in calls",
    desc: "Morning call to plan your day. Afternoon call to check in. Evening call to wrap up. She asks what matters most and helps you start.",
  },
  {
    num: "3",
    title: "Actually finish things",
    desc: "Your tasks get tracked, your calendar gets blocked, your emails get answered. All through the same WhatsApp chat you already use.",
  },
];

export default function HowItWorks() {
  return (
    <section id="how-it-works" className="bg-white py-16 md:py-20">
      <div className="mx-auto max-w-container px-6">
        <h2 className="font-serif text-primary text-3xl md:text-4xl text-center">
          How Charu works
        </h2>
        <p className="text-muted text-lg text-center mt-3 mb-12">
          Three steps to a calmer day.
        </p>

        <div className="grid md:grid-cols-3 gap-8 max-w-4xl mx-auto">
          {steps.map((step) => (
            <div key={step.num} className="text-center md:text-left">
              <span className="font-serif text-primary text-5xl block mb-3">
                {step.num}
              </span>
              <h3 className="font-sans font-bold text-text text-lg mb-2">
                {step.title}
              </h3>
              <p className="text-muted text-base leading-relaxed">
                {step.desc}
              </p>
            </div>
          ))}
        </div>

        <div className="text-center mt-12 space-y-4">
          <div className="inline-block">
            <WhatsAppCta />
          </div>
          <p className="text-muted text-sm">Your day, handled.</p>
        </div>
      </div>
    </section>
  );
}
```

---

### Task 9: Build Features section

**Files:**
- Create: `website/src/components/Features.tsx`

- [ ] **Step 1: Create Features component**

Create `website/src/components/Features.tsx`:

```tsx
const featured = {
  title: "Daily accountability calls",
  desc: "Three calls a day. Your phone rings, you pick up, and someone asks what you need to get done. That's it. No notification to swipe away.",
};

const cards = [
  {
    title: "WhatsApp check-ins",
    desc: "Quick nudges between calls. Like body doubling, but in your pocket - just the chat you already have.",
  },
  {
    title: "Your calendar, handled",
    desc: "Charu sees your day, finds the gaps, and blocks time for the work that matters - so you don't have to fight executive dysfunction alone.",
  },
  {
    title: "Emails that need replies",
    desc: "She surfaces the ones you're avoiding. Drafts a reply. You just say yes. No more inbox paralysis.",
  },
  {
    title: "Tasks you mention, tracked",
    desc: "Say it once, it's saved. Finish it, it's done. No separate app to open and abandon after a week.",
  },
  {
    title: "Adapts to your day",
    desc: "Reschedule calls, skip one, or say 'call me in 30 minutes.' She never ghosts, never judges.",
  },
];

export default function Features() {
  return (
    <section id="features" className="bg-background py-16 md:py-20">
      <div className="mx-auto max-w-container px-6">
        <h2 className="font-serif text-primary text-3xl md:text-4xl text-center mb-12">
          She shows up. Every single day.
        </h2>

        {/* Featured card */}
        <div className="bg-white rounded-md shadow-card p-8 mb-6 max-w-4xl mx-auto">
          <h3 className="font-sans font-bold text-text text-xl mb-2">
            {featured.title}
          </h3>
          <p className="text-muted text-base leading-relaxed">
            {featured.desc}
          </p>
        </div>

        {/* Standard cards grid */}
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6 max-w-4xl mx-auto">
          {cards.map((card) => (
            <div
              key={card.title}
              className="bg-white rounded-md shadow-card p-6"
            >
              <h3 className="font-sans font-bold text-text text-base mb-2">
                {card.title}
              </h3>
              <p className="text-muted text-sm leading-relaxed">{card.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
```

---

### Task 10: Build About section

**Files:**
- Create: `website/src/components/About.tsx`

- [ ] **Step 1: Create About component**

Create `website/src/components/About.tsx`:

```tsx
export default function About() {
  return (
    <section id="about" className="bg-accent-surface py-16 md:py-20">
      <div className="mx-auto max-w-prose px-6">
        <h2 className="font-serif text-primary text-3xl md:text-4xl mb-8">
          Built for you, when starting feels impossible
        </h2>

        <div className="space-y-6 text-text text-base leading-relaxed">
          <p>
            Starting tasks is genuinely hard. It's not a character flaw - it's a
            gap between knowing and doing that no planner or reminder can fix.
          </p>
          <p>
            Charu doesn't say 'just do it' or guilt you into productivity. She
            shows up like a friend who gets it - calls you, asks what matters,
            and helps you take the first step. If you miss a call, no big deal.
            She'll be there tomorrow.
          </p>
          <p>
            People already pay humans $30-40 a month for exactly this - daily
            accountability calls that actually work. We built the AI version, so
            it never ghosts, never judges, and costs a fraction of a coach.
          </p>
        </div>

        <p className="mt-8 text-muted text-base italic">
          Your morning starts with a plan. Your day has structure. And you stop
          going to bed wondering where it all went.
        </p>

        <p className="mt-6 text-sm text-muted">
          We read thousands of posts from people describing what they actually
          need. Then we built that.
        </p>

        <a
          href="#how-it-works"
          className="inline-block mt-8 text-primary text-sm font-medium underline underline-offset-2"
        >
          See how Charu works
        </a>
      </div>
    </section>
  );
}
```

---

### Task 11: Build CTA Banner section

**Files:**
- Create: `website/src/components/CtaBanner.tsx`

- [ ] **Step 1: Create CtaBanner component**

Create `website/src/components/CtaBanner.tsx`:

```tsx
import WhatsAppCta from "./WhatsAppCta";

export default function CtaBanner() {
  return (
    <section id="cta" className="bg-cta-brown py-16 md:py-20">
      <div className="mx-auto max-w-container px-6 text-center">
        <h2 className="font-serif text-white text-3xl md:text-4xl mb-4">
          Ready to stop planning and start doing?
        </h2>
        <p className="text-white/75 text-base mb-10 max-w-lg mx-auto">
          Two minutes to set up. No app to download. No judgment if you miss a
          day.
        </p>

        <div className="flex justify-center">
          <WhatsAppCta />
        </div>
      </div>
    </section>
  );
}
```

Note: The WhatsAppCta component uses white text on the primary brown button. On the `cta-brown` background, the desktop QR code card (white bg) will stand out well. The mobile button text color is white on primary brown - both pass WCAG AA.

---

### Task 12: Assemble the landing page

**Files:**
- Modify: `website/src/app/page.tsx`
- Modify: `website/src/app/layout.tsx`

- [ ] **Step 1: Update layout.tsx to include Navbar and Footer**

In `website/src/app/layout.tsx`, update the `<body>` contents:

```tsx
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";

// ... (keep existing font and metadata code)

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${dmSerif.variable} ${inter.variable}`}>
      <body className="antialiased">
        <Navbar />
        <main>{children}</main>
        <Footer />
      </body>
    </html>
  );
}
```

- [ ] **Step 2: Create the landing page**

Replace `website/src/app/page.tsx` with:

```tsx
import Hero from "@/components/Hero";
import PainSection from "@/components/PainSection";
import HowItWorks from "@/components/HowItWorks";
import Features from "@/components/Features";
import About from "@/components/About";
import CtaBanner from "@/components/CtaBanner";

export default function Home() {
  return (
    <>
      <Hero />
      <PainSection />
      <HowItWorks />
      <Features />
      <About />
      <CtaBanner />
    </>
  );
}
```

- [ ] **Step 3: Verify the full landing page**

Run:
```bash
cd /home/sarathy/projects/charu.ai/website && npm run dev
```

Expected: Full landing page renders at http://localhost:3000 with all 8 sections (navbar, hero, pain, how-it-works, features, about, CTA banner, footer). Scroll anchors work. Mobile hamburger menu works. QR code renders on desktop. Button shows on mobile (resize browser to test).

---

### Task 13: Build Privacy Policy page

**Files:**
- Create: `website/src/app/privacy/page.tsx`

- [ ] **Step 1: Create privacy page**

Create `website/src/app/privacy/page.tsx`:

```tsx
import type { Metadata } from "next";
import { CONTACT_EMAIL } from "@/lib/constants";

export const metadata: Metadata = {
  title: "Privacy Policy - Charu AI",
  description: "How Charu AI handles your data.",
};

export default function PrivacyPage() {
  return (
    <article className="mx-auto max-w-prose px-6 py-12 md:py-20">
      <h1 className="font-serif text-primary text-3xl md:text-4xl mb-2">
        Privacy Policy
      </h1>
      <p className="text-sm text-muted mb-10">Last updated: April 7, 2026</p>

      <div className="space-y-8 text-text text-base leading-relaxed">
        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            What data we collect
          </h2>
          <p>
            When you use Charu, we collect your phone number, the name you give
            us, and your timezone. If you connect Google Calendar or Gmail, we
            store encrypted OAuth tokens that let Charu access those services on
            your behalf.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            How we use your data
          </h2>
          <p>
            Your data powers the accountability calls, task tracking, calendar
            integration, and email management that Charu provides. We do not
            sell your data. We do not use it for advertising. It exists so Charu
            can do her job.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            WhatsApp and Twilio
          </h2>
          <p>
            Charu communicates with you through WhatsApp via Twilio. Twilio
            routes messages and calls between you and our servers. Twilio does
            not store message content beyond what is needed for delivery. You can
            read{" "}
            <a
              href="https://www.twilio.com/en-us/legal/privacy"
              className="text-primary underline underline-offset-2"
              target="_blank"
              rel="noopener noreferrer"
            >
              Twilio's privacy policy
            </a>{" "}
            for details.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Google integration
          </h2>
          <p>
            If you connect Google Calendar, Charu can read your events and
            create time blocks. If you connect Gmail, Charu can read emails that
            need replies and send replies you approve. Both use OAuth scopes
            that you explicitly grant. You can disconnect either service at any
            time by asking Charu or revoking access in your Google account
            settings.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Data storage and security
          </h2>
          <p>
            Your data is stored in a PostgreSQL database hosted on Google Cloud
            Platform. OAuth tokens are encrypted at rest using Fernet symmetric
            encryption. We do not store plain-text credentials.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Data retention
          </h2>
          <p>
            We keep your data while your account is active. If you delete your
            account, we remove your data within 30 days. To delete your account,
            message Charu with "delete my account" or email us.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">Your rights</h2>
          <p>
            You can ask us to export or delete your data at any time. Email{" "}
            <a
              href={`mailto:${CONTACT_EMAIL}`}
              className="text-primary underline underline-offset-2"
            >
              {CONTACT_EMAIL}
            </a>{" "}
            and we will respond within 7 days.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Third-party services
          </h2>
          <p>
            Charu uses Google Cloud Platform for hosting and AI (Vertex AI),
            Twilio for WhatsApp and voice calls. Each has its own privacy
            policy.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Changes to this policy
          </h2>
          <p>
            If we change this policy, we will let you know through WhatsApp. If
            you keep using Charu after that, it means you accept the changes.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">Contact</h2>
          <p>
            Questions? Email{" "}
            <a
              href={`mailto:${CONTACT_EMAIL}`}
              className="text-primary underline underline-offset-2"
            >
              {CONTACT_EMAIL}
            </a>
            .
          </p>
        </section>
      </div>
    </article>
  );
}
```

- [ ] **Step 2: Verify**

Run: `npm run dev` and visit http://localhost:3000/privacy

Expected: Clean, readable privacy policy with constrained width, DM Serif Display headings, proper spacing.

---

### Task 14: Build Terms of Service page

**Files:**
- Create: `website/src/app/terms/page.tsx`

- [ ] **Step 1: Create terms page**

Create `website/src/app/terms/page.tsx`:

```tsx
import type { Metadata } from "next";
import { CONTACT_EMAIL } from "@/lib/constants";

export const metadata: Metadata = {
  title: "Terms of Service - Charu AI",
  description: "Terms of service for using Charu AI.",
};

export default function TermsPage() {
  return (
    <article className="mx-auto max-w-prose px-6 py-12 md:py-20">
      <h1 className="font-serif text-primary text-3xl md:text-4xl mb-2">
        Terms of Service
      </h1>
      <p className="text-sm text-muted mb-10">Last updated: April 7, 2026</p>

      <div className="space-y-8 text-text text-base leading-relaxed">
        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            What Charu is
          </h2>
          <p>
            Charu is an AI accountability assistant. She calls your phone, chats
            with you on WhatsApp, and can connect to your Google Calendar and
            Gmail if you ask her to. She is not a therapist, life coach, or
            medical professional.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Who can use Charu
          </h2>
          <p>
            You need to be at least 18 years old and have a valid WhatsApp
            account. Your phone number is your account - one number, one
            account.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            What you agree to
          </h2>
          <p>
            Use Charu for its intended purpose. Do not abuse the service,
            attempt automated access, or reverse engineer it.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Google integration
          </h2>
          <p>
            Charu only accesses your Google Calendar and Gmail if you explicitly
            connect them. You can revoke access at any time through your Google
            account settings or by asking Charu.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            WhatsApp and Twilio
          </h2>
          <p>
            Charu depends on WhatsApp and Twilio to work. If those services go
            down, Charu goes down too. We are not responsible for their outages.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            AI-generated content
          </h2>
          <p>
            Charu is AI. Her responses are generated by language models and may
            not always be perfect. She is not a substitute for professional
            advice of any kind.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            What we are not responsible for
          </h2>
          <p>
            Charu is provided as-is. We do not guarantee any specific
            productivity outcomes. Use her as a tool to help you, not as a
            promise that everything will get done.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Stopping or leaving
          </h2>
          <p>
            You can stop using Charu at any time by messaging her "stop." We can
            also terminate your account if you abuse the service.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Changes to these terms
          </h2>
          <p>
            If we change these terms, we will let you know through WhatsApp. If
            you keep using Charu after that, it means you accept the changes.
          </p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">
            Governing law
          </h2>
          <p>These terms are governed by the laws of India.</p>
        </section>

        <section>
          <h2 className="font-serif text-primary text-xl mb-3">Contact</h2>
          <p>
            Questions? Email{" "}
            <a
              href={`mailto:${CONTACT_EMAIL}`}
              className="text-primary underline underline-offset-2"
            >
              {CONTACT_EMAIL}
            </a>
            .
          </p>
        </section>
      </div>
    </article>
  );
}
```

- [ ] **Step 2: Verify**

Run: `npm run dev` and visit http://localhost:3000/terms

Expected: Clean, readable terms page matching privacy page styling.

---

### Task 15: Final verification and build test

**Files:** None (verification only)

- [ ] **Step 1: Run production build**

```bash
cd /home/sarathy/projects/charu.ai/website && npm run build
```

Expected: Build completes without errors. All pages are statically generated.

- [ ] **Step 2: Test production build locally**

```bash
cd /home/sarathy/projects/charu.ai/website && npm run start
```

Visit http://localhost:3000 and verify:
- Landing page: all 8 sections render correctly
- Smooth scroll anchors work (click "How It Works" in nav)
- Mobile hamburger menu works (resize to <768px)
- QR code renders on desktop
- WhatsApp button shows on mobile
- /privacy page renders correctly
- /terms page renders correctly
- Footer links navigate to privacy and terms

- [ ] **Step 3: Verify .gitignore**

```bash
cd /home/sarathy/projects/charu.ai && git status
```

Expected: `website/` does not appear in git status (it is gitignored). Only `.gitignore` changes should be tracked.
