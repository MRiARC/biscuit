# Biscuit — Product Introduction

A standalone Next.js App Router experience introducing Biscuit, the open-source decision intelligence and knowledge recovery project.

## Requirements

- Node.js 20.9 or later
- pnpm (or npm)

## Run locally

Run the app from its own directory. This matters because Biscuit's repository root is a Python project.

```bash
cd intro
pnpm install
rm -rf .next
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000).

## If Next.js reports a missing `/_not-found/page` module

1. Stop the dev server completely (`Ctrl + C`).
2. Make sure your terminal is inside the `intro/` directory.
3. Remove the generated Turbopack/Next cache: `rm -rf .next`.
4. Start it again with `pnpm dev`.

The app pins `turbopack.root` to the `intro/` directory and includes an explicit `app/not-found.tsx` fallback. Do not create a second `app/` folder at the repository root.

For comparison, you can temporarily run the Webpack dev server with `pnpm dev:webpack`. If Webpack works but Turbopack still fails after a clean restart, capture the terminal output and report it with the Next.js version.

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
