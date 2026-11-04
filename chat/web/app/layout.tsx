import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stock chat",
  description: "Ask for today's price and news about a stock ticker.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
