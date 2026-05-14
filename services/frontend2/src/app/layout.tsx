import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";

import { auth } from "@/auth";
import Sidebar from "@/components/Sidebar";
import GlobalSearch from "@/components/GlobalSearch";
import ThemeToggle from "@/components/ThemeToggle";
import styles from "./layout.module.css";
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
  const session = await auth();
  const role = session?.user?.role ?? null;
  const isChromeless = !session || role === "first_time";
  const email = session?.user?.email ?? null;
  const initials = email ? (email[0] ?? "?").toUpperCase() : "?";

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
        <div className={styles.shell}>
          <Sidebar />

          <div className={styles.main}>
            {/* Navbar */}
            <header className={styles.navbar}>
              {/* Left: breadcrumb placeholder */}
              <div className={styles.navbarLeft}>
                <span className={styles.navbarBrand}>HyperP</span>
              </div>

              {/* Center: global search */}
              <GlobalSearch />

              {/* Right: actions + user */}
              <div className={styles.navbarRight}>
                <ThemeToggle />

                {/* Notification bell */}
                <button className={styles.navbarIconBtn} title="Notifications">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                    <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
                  </svg>
                </button>

                {/* User avatar */}
                <div className={styles.navbarAvatar} title={email ?? ""}>
                  {initials}
                </div>
              </div>
            </header>

            {/* Page content */}
            <main className={styles.content}>
              {children}
            </main>
          </div>
        </div>
      </body>
    </html>
  );
}
