# Biscuit — Product Introduction

A standalone Next.js App Router experience introducing Biscuit, the open-source decision intelligence and knowledge recovery project.

## Run locally

Requirements: Node.js 20.9 or later.

```bash
cd intro
pnpm install
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000).

## Other commands

```bash
pnpm typecheck
pnpm build
pnpm start
```

## Project notes

- This app is intentionally isolated from the Python package at the repository root.
- It is a static, responsive product-introduction site; it does not call the Biscuit backend.
- Product copy describes the intended architecture and does not imply every workstream is production-ready.
- Styling is custom CSS; no component library or icon package is required.
