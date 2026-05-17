import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";
import { headers } from "next/headers";

import { auth } from "@/auth";
import AppShell from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "HyperP",
  description: "Customer profile unification and relationship intelligence",
};

const themeScript = `
  try {
    const t = localStorage.getItem('theme');
    document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
  } catch {}
`;

export default async function RootLayout({ children }: { children: ReactNode }): Promise<ReactElement> {
  const [session, headersList] = await Promise.all([auth(), headers()]);
  const pathname = headersList.get("x-pathname") ?? "";
  const role = session?.user?.role ?? null;
  const isChromeless = !session || role === "first_time"
    || pathname === "/login"
    || pathname.startsWith("/public")
    || pathname.startsWith("/dev");
  const email = session?.user?.email ?? null;
  const displayName = session?.user?.displayName ?? session?.user?.name ?? null;
  const initials = displayName
    ? displayName.split(" ").filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase()
    : (email?.[0] ?? "?").toUpperCase();

  if (isChromeless) {
    return (
      <html lang="en" suppressHydrationWarning>
        <head><script dangerouslySetInnerHTML={{ __html: themeScript }}/></head>
        <body>{children}</body>
      </html>
    );
  }

  return (
    <html lang="en" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeScript }}/></head>
      <body>
        <AppShell initials={initials} email={email} displayName={displayName}>
          {children}
        </AppShell>
      </body>
    </html>
  );
}
