# Biscuit — Product Introduction

A standalone Next.js App Router experience introducing Biscuit, the open-source decision intelligence and knowledge recovery project.

## Requirements

- Node.js 20.9 or later
- pnpm (or npm)

## Run locally

Run the app from its own directory. This matters because Biscuit's repository root is a Python project.

~~~bash
cd intro
pnpm install
rm -rf .next
pnpm dev
~~~

Open http://localhost:3000.

The default development command uses **Webpack** as a workaround for the reported Turbopack /_not-found/page runtime error. The production build remains on Next.js 16's default Turbopack builder.

## Trying Turbopack explicitly

~~~bash
pnpm dev:turbo
~~~

The app pins turbopack.root to the intro/ directory and includes an explicit app/not-found.tsx fallback. If Turbopack still reports a missing /_not-found/page module:

1. Stop the dev server completely (Ctrl + C).
2. Make sure your terminal is inside the intro/ directory.
3. Remove the generated cache: rm -rf .next.
4. Retry with pnpm dev:turbo.

A similar stale App Router manifest issue has been reported upstream; restarting the dev server after clearing .next is a useful diagnostic. Do not create a second app/ folder at the repository root.

## Other commands

~~~bash
pnpm typecheck
pnpm build
pnpm start
~~~

## Project notes

- This app is intentionally isolated from the Python package at the repository root.
- It is a static, responsive product-introduction site; it does not call the Biscuit backend.
- Product copy describes the intended architecture and does not imply every workstream is production-ready.
- Styling is custom CSS; no component library or icon package is required.
