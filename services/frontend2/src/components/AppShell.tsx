"use client";

import { useEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { signOut } from "next-auth/react";

import GlobalSearch from "@/components/GlobalSearch";
import Sidebar from "@/components/Sidebar";
import ThemeToggle from "@/components/ThemeToggle";
import { usePopoverClose } from "@/lib/usePopoverClose";
import { toBasePath } from "@/lib/route-paths";
import styles from "@/app/layout.module.css";

interface AppShellProps {
  children: ReactNode;
  initials: string;
  email: string | null;
  displayName: string | null;
}

function MenuIcon(): ReactElement {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="2" y1="4" x2="14" y2="4"/>
      <line x1="2" y1="8" x2="14" y2="8"/>
      <line x1="2" y1="12" x2="14" y2="12"/>
    </svg>
  );
}

function ProfilePopover({ email, initials, displayName, onClose }: { email: string | null; initials: string; displayName: string | null; onClose: () => void }): ReactElement {
  const ref = usePopoverClose<HTMLDivElement>(onClose);

  async function handleLogout(): Promise<void> {
    onClose();
    await signOut({ callbackUrl: toBasePath("/login") });
  }

  return (
    <div ref={ref} className={styles.profilePopover}>
      <div className={styles.profilePopoverHeader}>
        <div className={styles.profilePopoverAvatar}>{initials}</div>
        <div className={styles.profilePopoverMeta}>
          <div className={styles.profilePopoverInitials}>{displayName ?? initials}</div>
          {email && <div className={styles.profilePopoverEmail}>{email}</div>}
        </div>
      </div>
      <div className={styles.profilePopoverDivider} />
      <button
        type="button"
        className={styles.profilePopoverLogout}
        onClick={() => void handleLogout()}
      >
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 2H3a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>
          <polyline points="11 11 14 8 11 5"/>
          <line x1="14" y1="8" x2="6" y2="8"/>
        </svg>
        Sign out
      </button>
    </div>
  );
}

export default function AppShell({ children, initials, email, displayName }: AppShellProps): ReactElement {
  const [collapsed, setCollapsed] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const navToggleRef = useRef<HTMLButtonElement>(null);
  const mobileSidebarRef = useRef<HTMLDivElement>(null);
  const pathname = usePathname();

  // Close mobile drawer on navigation
  useEffect(() => { setMobileOpen(false); }, [pathname]);

  useEffect(() => {
    if (!mobileOpen) return;
    mobileSidebarRef.current?.querySelector<HTMLElement>("a, button")?.focus();
    function closeOnEscape(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        setMobileOpen(false);
        navToggleRef.current?.focus();
        return;
      }
      if (event.key === "Tab") {
        const focusable = Array.from(
          mobileSidebarRef.current?.querySelectorAll<HTMLElement>("a, button:not([disabled])") ?? [],
        );
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (!first || !last) return;
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [mobileOpen]);

  function handleHamburger(): void {
    if (window.matchMedia("(max-width: 768px)").matches) {
      setMobileOpen((o) => !o);
    } else {
      setCollapsed((c) => !c);
    }
  }

  return (
    <div className={`${styles.shell} ${collapsed ? styles.shellCollapsed : ""}`}>
      <a className={styles.skipLink} href="#main-content">Skip to content</a>
      {/* Desktop sidebar — hidden on mobile via wrapper */}
      <div className={styles.desktopSidebar}>
        <Sidebar />
      </div>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className={styles.mobileOverlay} onClick={() => setMobileOpen(false)} aria-hidden="true" />
      )}
      <div
        id="mobile-navigation"
        ref={mobileSidebarRef}
        className={`${styles.mobileSidebar} ${mobileOpen ? styles.mobileSidebarOpen : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="Navigation"
        aria-hidden={!mobileOpen}
        inert={!mobileOpen}
      >
        <Sidebar />
      </div>

      <div className={styles.main}>
        <header className={styles.navbar}>
          <button
            type="button"
            ref={navToggleRef}
            className={styles.navbarToggle}
            onClick={handleHamburger}
            title="Toggle navigation"
            aria-label="Toggle navigation"
            aria-expanded={mobileOpen || undefined}
            aria-controls="mobile-navigation"
          >
            <MenuIcon />
          </button>

          <GlobalSearch />

          <div className={styles.navbarRight}>
            <ThemeToggle />
            <div style={{ position: "relative" }}>
              <button
                type="button"
                className={styles.navbarAvatar}
                title={email ?? ""}
                onClick={() => setProfileOpen((o) => !o)}
                aria-haspopup="true"
                aria-expanded={profileOpen}
              >
                {initials}
              </button>
              {profileOpen && (
                <ProfilePopover
                  email={email}
                  initials={initials}
                  displayName={displayName}
                  onClose={() => setProfileOpen(false)}
                />
              )}
            </div>
          </div>
        </header>

        <main id="main-content" className={styles.content} tabIndex={-1}>
          <div className={styles.contentInner}>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
