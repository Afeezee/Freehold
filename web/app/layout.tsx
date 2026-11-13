import type { Metadata, Viewport } from "next";
import "./globals.css";

const site = "https://freehold.vercel.app";
const description =
  "Backups only you can read, on storage you can change. Files are " +
  "encrypted on your own machine before they leave it. No accounts, " +
  "no servers, no lock-in.";

export const metadata: Metadata = {
  metadataBase: new URL(site),
  title: {
    default: "Freehold — backups only you can read",
    template: "%s — Freehold",
  },
  description,
  applicationName: "Freehold",
  keywords: [
    "backup", "encrypted backup", "zero knowledge", "deduplication",
    "self hosted", "GitHub backup", "Cloudflare R2", "Backblaze B2",
  ],
  authors: [{ name: "Afeez Olagunju", url: "https://github.com/Afeezee" }],
  openGraph: {
    type: "website",
    url: site,
    siteName: "Freehold",
    title: "Freehold — backups only you can read",
    description,
    images: [{ url: "/og.png", width: 1200, height: 630,
               alt: "Freehold — backups only you can read" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "Freehold — backups only you can read",
    description,
    images: ["/og.png"],
  },
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/favicon.ico", sizes: "any" },
    ],
    apple: "/apple-touch-icon.png",
  },
  manifest: "/manifest.webmanifest",
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FBFBFA" },
    { media: "(prefers-color-scheme: dark)", color: "#131518" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
