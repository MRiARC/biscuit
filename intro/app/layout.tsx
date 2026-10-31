import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Biscuit — Give Open Source a Memory",
  description:
    "Biscuit recovers the reasoning behind open-source engineering decisions and helps teams carry that knowledge forward.",
  applicationName: "Biscuit",
  keywords: [
    "open source",
    "decision intelligence",
    "engineering knowledge",
    "architectural decision records",
    "developer tools",
  ],
  openGraph: {
    title: "Biscuit — Give Open Source a Memory",
    description:
      "Not just what was decided. Why it was decided. A provenance-first memory for open-source engineering.",
    type: "website",
  },
};

export const viewport: Viewport = {
  themeColor: "#11110f",
  colorScheme: "dark",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
