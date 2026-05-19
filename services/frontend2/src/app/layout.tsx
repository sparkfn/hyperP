import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";
import { redirect } from "next/navigation";
import { headers } from "next/headers";

import { auth } from "@/auth";
import AppShell from "@/components/AppShell";
import SessionProviderClient from "@/components/SessionProviderClient";
import { getInitials } from "@/lib/display";
import { LoadingProvider } from "@/lib/LoadingContext";
import { isAdminPath, isPublicPath } from "@/lib/route-paths";
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

  if (!session?.googleIdToken && !isPublicPath(pathname)) {
    redirect("/login");
  }
  if (isAdminPath(pathname) && session?.user?.role !== "admin") {
    redirect("/persons");
  }

  const role = session?.user?.role ?? null;
  const isChromeless = !session || role === "first_time" || isPublicPath(pathname) || pathname.startsWith("/dev");
  const email = session?.user?.email ?? null;
  const displayName = session?.user?.displayName ?? session?.user?.name ?? null;
  const initials = getInitials(displayName, email?.[0] ?? "?");

  if (isChromeless) {
    return (
      <html lang="en" suppressHydrationWarning>
        <head><script dangerouslySetInnerHTML={{ __html: themeScript }}/></head>
        <body><SessionProviderClient>{children}</SessionProviderClient></body>
      </html>
    );
  }

  return (
    <html lang="en" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeScript }}/></head>
      <body>
        <LoadingProvider>
          <SessionProviderClient>
            <AppShell initials={initials} email={email} displayName={displayName}>
              {children}
            </AppShell>
          </SessionProviderClient>
        </LoadingProvider>
      </body>
    </html>
  );
}
