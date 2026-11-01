import Link from "next/link";
import type { ReactNode } from "react";

function BiscuitMark({ small = false }: { small?: boolean }) {
  return (
    <span className={small ? "brand-mark brand-mark--small" : "brand-mark"} aria-hidden="true">
      <svg viewBox="0 0 40 40" fill="none">
        <rect x="2" y="2" width="36" height="36" rx="12" stroke="currentColor" strokeWidth="1.5" />
        <path d="M12 12.5h9.2a5.1 5.1 0 0 1 0 10.2H12V12.5Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
        <path d="M12 22.7h10.4a5.1 5.1 0 0 1 0 10.2H12V22.7Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
        <circle cx="28.5" cy="12" r="2" fill="currentColor" />
        <circle cx="28.5" cy="28" r="2" fill="currentColor" />
      </svg>
    </span>
  );
}

function ArrowIcon({ diagonal = false }: { diagonal?: boolean }) {
  return diagonal ? (
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M4 12 12 4M5 4h7v7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ) : (
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M2.5 8h10M8 3.5 12.5 8 8 12.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ArchiveIllustration() {
  return (
    <div className="visual-shell" aria-label="Illustration of discussions becoming verified decision knowledge">
      <div className="visual-topline">
        <span className="visual-live"><i /> KNOWLEDGE GRAPH</span>
        <span className="visual-index">FIG. 01 / MEMORY SYSTEM</span>
      </div>
      <div className="graph-stage">
        <svg className="graph-lines" viewBox="0 0 600 390" fill="none" aria-hidden="true">
          <path d="M112 91C174 91 166 188 239 188M112 284C177 284 174 188 239 188M239 188C309 188 308 91 390 91M239 188C309 188 309 284 390 284M390 91C443 91 436 188 495 188M390 284C443 284 437 188 495 188" stroke="url(#lineA)" strokeWidth="1.5" strokeDasharray="4 6" />
          <path d="M112 91C174 91 166 188 239 188M112 284C177 284 174 188 239 188" stroke="#D9A86C" strokeOpacity=".38" strokeWidth="1.5" />
          <defs>
            <linearGradient id="lineA" x1="112" y1="188" x2="495" y2="188" gradientUnits="userSpaceOnUse">
              <stop stopColor="#D9A86C" stopOpacity=".12" />
              <stop offset=".52" stopColor="#D9A86C" stopOpacity=".8" />
              <stop offset="1" stopColor="#D9A86C" stopOpacity=".14" />
            </linearGradient>
          </defs>
        </svg>
        <div className="source-node source-node--one">
          <span className="node-icon node-icon--github">GH</span>
          <span><b>Pull request</b><small>Discussion · 2019</small></span>
        </div>
        <div className="source-node source-node--two">
          <span className="node-icon node-icon--mail">M</span>
          <span><b>Mailing list</b><small>Thread · 42 replies</small></span>
        </div>
        <div className="decision-node">
          <span className="decision-kicker"><i /> DECISION RECOVERED</span>
          <strong>Null-aware<br />operator syntax</strong>
          <div className="decision-divider" />
          <span className="decision-status"><span /> Rejected alternative</span>
        </div>
        <div className="evidence-node evidence-node--one">
          <span className="evidence-check">✓</span>
          <span><b>Exact evidence</b><small>Source verified</small></span>
        </div>
        <div className="evidence-node evidence-node--two">
          <span className="evidence-code">&lt;/&gt;</span>
          <span><b>Code anchor</b><small>Symbol traced</small></span>
        </div>
      </div>
      <div className="visual-foot">
        <span>FRAGMENTS IN</span>
        <span className="visual-foot-line" />
        <span>CONTEXT PRESERVED</span>
        <span className="visual-foot-line" />
        <span>KNOWLEDGE OUT</span>
      </div>
    </div>
  );
}

const pillars = [
  {
    number: "01",
    label: "THE PAST",
    title: "Recover the why behind the code.",
    description:
      "Search years of engineering discussions to reconstruct proposals, objections, trade-offs and the human decisions that settled them.",
    detail: "HISTORICAL KNOWLEDGE RECOVERY",
    icon: "history",
  },
  {
    number: "02",
    label: "DAY 1+",
    title: "Keep today's decisions from disappearing.",
    description:
      "Bring live team discussions into a reviewable memory. Draft decision records, preserve evidence and let maintainers approve what becomes official.",
    detail: "LIVING DECISION INTELLIGENCE",
    icon: "pulse",
  },
];

const steps = [
  {
    index: "01",
    title: "Collect the conversation",
    text: "Bring together threads, reviews and messages while preserving their original source and revision history.",
    tag: "INGEST",
  },
  {
    index: "02",
    title: "Reconstruct the debate",
    text: "Separate distinct alternatives, individual arguments, rebuttals and actual resolutions—not just keywords in a thread.",
    tag: "UNDERSTAND",
  },
  {
    index: "03",
    title: "Verify every claim",
    text: "Tie quotations to exact source spans, retain provenance and distinguish model suggestions from human authority.",
    tag: "VERIFY",
  },
  {
    index: "04",
    title: "Connect knowledge to code",
    text: "Trace decision alternatives to code symbols and changes, so historical context can inform today's pull requests.",
    tag: "GROUND",
  },
];

const principles = [
  {
    n: "A",
    title: "Evidence before confidence",
    text: "A plausible summary is not proof. Claims should lead back to the exact source that supports them.",
  },
  {
    n: "B",
    title: "A model is not an authority",
    text: "AI can surface candidates and draft records. Maintainers establish authoritative outcomes.",
  },
  {
    n: "C",
    title: "Uncertainty is a valid result",
    text: "When evidence is incomplete, stale or ambiguous, Biscuit should abstain—not invent certainty.",
  },
];

function SectionLabel({ children }: { children: ReactNode }) {
  return <div className="section-label"><span />{children}</div>;
}

export default function HomePage() {
  return (
    <main>
      <header className="site-header">
        <div className="nav-wrap">
          <Link href="#top" className="brand" aria-label="Biscuit home">
            <BiscuitMark />
            <span>BISCUIT<span className="brand-period">.</span></span>
          </Link>
          <nav className="desktop-nav" aria-label="Main navigation">
            <Link href="#why">The problem</Link>
            <Link href="#architecture">Architecture</Link>
            <Link href="#principles">Principles</Link>
          </nav>
          <Link
            className="nav-cta"
            href="https://github.com/MRiARC/biscuit"
            target="_blank"
            rel="noreferrer"
          >
            Explore repository <ArrowIcon diagonal />
          </Link>
        </div>
      </header>

      <section className="hero" id="top">
        <div className="hero-grid" aria-hidden="true" />
        <div className="hero-inner">
          <div className="hero-copy">
            <SectionLabel>OPEN-SOURCE DECISION INTELLIGENCE</SectionLabel>
            <h1>Software remembers the code.<br /><em>Biscuit remembers why.</em></h1>
            <p className="hero-lede">
              Engineering decisions are buried in years of discussions. Biscuit turns that scattered
              context into verifiable knowledge—and carries it forward as projects evolve.
            </p>
            <div className="hero-actions">
              <Link className="button button--light" href="#architecture">
                Discover the system <ArrowIcon />
              </Link>
              <Link
                className="button button--outline"
                href="https://github.com/MRiARC/biscuit"
                target="_blank"
                rel="noreferrer"
              >
                View on GitHub <ArrowIcon diagonal />
              </Link>
            </div>
            <div className="hero-meta">
              <span><i className="meta-dot" /> BUILT FOR OPEN SOURCE</span>
              <span>PAST, PRESENT &amp; WHAT COMES NEXT</span>
            </div>
          </div>
          <div className="hero-visual">
            <ArchiveIllustration />
            <div className="visual-caption">
              <span>FROM FRAGMENTED DISCUSSIONS</span>
              <span className="caption-arrow">↘</span>
              <span>TO TRACEABLE DECISIONS</span>
            </div>
          </div>
        </div>
        <div className="hero-bottom">
          <span>01 / THE INTRODUCTION</span>
          <span>SCROLL TO EXPLORE <span className="scroll-mark">↓</span></span>
        </div>
      </section>

      <section className="problem-section section-pad" id="why">
        <div className="section-container">
          <div className="problem-heading">
            <SectionLabel>THE PROBLEM</SectionLabel>
            <h2>Open source has a memory problem.</h2>
          </div>
          <div className="problem-body">
            <p className="problem-lede">
              The code survives. The reasoning behind it often doesn&apos;t.
            </p>
            <p className="body-copy">
              A rejected proposal can live across a hundred comments, a mailing-list thread and a
              pull-request review. Years later, a new contributor sees the same problem—but not the
              context that came before. When maintainers move on, institutional knowledge moves
              with them.
            </p>
            <div className="problem-points">
              <div><span>01</span><b>Decisions scattered across platforms</b></div>
              <div><span>02</span><b>Rejected alternatives are hard to rediscover</b></div>
              <div><span>03</span><b>Code evolves beyond its original context</b></div>
            </div>
          </div>
        </div>
        <div className="problem-rule"><span>THE KNOWLEDGE GAP</span><span>DISCUSSION → DECISION → CODE</span></div>
      </section>

      <section className="architecture-section section-pad" id="architecture">
        <div className="section-container">
          <div className="architecture-intro">
            <div>
              <SectionLabel>ONE MEMORY. TWO DIRECTIONS.</SectionLabel>
              <h2>Understand what happened.<br /><em>Remember what happens next.</em></h2>
            </div>
            <p className="body-copy">
              Biscuit connects historical decision recovery with living team knowledge. Both
              directions feed one auditable memory of the project.
            </p>
          </div>
          <div className="pillar-grid">
            {pillars.map((pillar) => (
              <article className="pillar-card" key={pillar.number}>
                <div className="pillar-top">
                  <span className="pillar-number">{pillar.number}</span>
                  <span className="pillar-icon" aria-hidden="true">
                    {pillar.icon === "history" ? (
                      <svg viewBox="0 0 32 32" fill="none"><path d="M7 10a11 11 0 1 1-1 11M7 5v5h5M16 9v8l5 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/></svg>
                    ) : (
                      <svg viewBox="0 0 32 32" fill="none"><path d="M4 17h6l4-9 6 17 4-9h4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/><circle cx="4" cy="17" r="1.5" fill="currentColor"/></svg>
                    )}
                  </span>
                </div>
                <span className="pillar-label">{pillar.label}</span>
                <h3>{pillar.title}</h3>
                <p>{pillar.description}</p>
                <div className="pillar-footer"><span>{pillar.detail}</span><ArrowIcon diagonal /></div>
              </article>
            ))}
          </div>
          <div className="shared-memory">
            <div className="shared-line" />
            <div className="shared-memory-box">
              <span className="shared-icon"><BiscuitMark small /></span>
              <span><b>One shared decision memory</b><small>VERIFIABLE · ALTERNATIVE-AWARE · CONTINUOUS</small></span>
            </div>
            <div className="shared-line" />
          </div>
        </div>
      </section>

      <section className="workflow-section section-pad">
        <div className="section-container">
          <div className="workflow-heading">
            <SectionLabel>HOW IT WORKS</SectionLabel>
            <h2>From conversation<br />to <em>institutional memory.</em></h2>
            <p className="body-copy">A deliberate pipeline that keeps source material, machine reasoning and human decisions distinct.</p>
          </div>
          <div className="workflow-list">
            {steps.map((step) => (
              <article className="workflow-step" key={step.index}>
                <span className="workflow-index">{step.index}</span>
                <div className="workflow-content">
                  <span className="workflow-tag">{step.tag}</span>
                  <h3>{step.title}</h3>
                  <p>{step.text}</p>
                </div>
                <span className="workflow-arrow"><ArrowIcon diagonal /></span>
              </article>
            ))}
          </div>
          <div className="workflow-footnote">
            <span className="footnote-mark">↳</span>
            <span>AI helps interpret the discussion. Deterministic checks and human authority govern what becomes trusted knowledge.</span>
          </div>
        </div>
      </section>

      <section className="principles-section section-pad" id="principles">
        <div className="section-container principles-layout">
          <div className="principles-heading">
            <SectionLabel>THE TRUST CONTRACT</SectionLabel>
            <h2>Intelligence is useful.<br /><em>Trust is essential.</em></h2>
            <p className="body-copy">
              A system that remembers engineering decisions must be able to explain where every
              important claim came from—and know when it cannot.
            </p>
          </div>
          <div className="principle-list">
            {principles.map((principle) => (
              <article className="principle-item" key={principle.n}>
                <span className="principle-letter">{principle.n}</span>
                <div><h3>{principle.title}</h3><p>{principle.text}</p></div>
              </article>
            ))}
            <div className="trust-stamp">
              <span className="stamp-icon">✓</span>
              <span><b>PROVENANCE FIRST</b><small>Exact sources. Clear authority. Honest uncertainty.</small></span>
            </div>
          </div>
        </div>
      </section>

      <section className="closing-section">
        <div className="closing-grid" aria-hidden="true" />
        <div className="closing-inner">
          <SectionLabel>BUILDING BETTER ENGINEERING MEMORY</SectionLabel>
          <h2>Don&apos;t make the next contributor<br /><em>rediscover the past.</em></h2>
          <p>Give every decision a source. Give every source its context. Give the project a memory.</p>
          <Link
            className="button button--light"
            href="https://github.com/MRiARC/biscuit"
            target="_blank"
            rel="noreferrer"
          >
            Explore Biscuit on GitHub <ArrowIcon diagonal />
          </Link>
        </div>
      </section>

      <footer className="site-footer">
        <div className="footer-main">
          <Link href="#top" className="brand brand--footer">
            <BiscuitMark />
            <span>BISCUIT<span className="brand-period">.</span></span>
          </Link>
          <span className="footer-tagline">A memory layer for open-source engineering.</span>
          <Link className="footer-github" href="https://github.com/MRiARC/biscuit" target="_blank" rel="noreferrer">
            GITHUB <ArrowIcon diagonal />
          </Link>
        </div>
        <div className="footer-bottom">
          <span>© 2026 BISCUIT · MRiARC</span>
          <span>OPEN SOURCE · APACHE 2.0</span>
          <span>AN OPEN PROJECT, EVOLVING IN PUBLIC</span>
        </div>
      </footer>
    </main>
  );
}
