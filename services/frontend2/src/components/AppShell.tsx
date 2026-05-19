"use client";

import { useEffect, useState, type ReactElement, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { signOut } from "next-auth/react";

import GlobalSearch from "@/components/GlobalSearch";
import Sidebar from "@/components/Sidebar";
import ThemeToggle from "@/components/ThemeToggle";
import { usePopoverClose } from "@/lib/usePopoverClose";
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
    await signOut({ callbackUrl: "/login" });
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
  const pathname = usePathname();

  // Close mobile drawer on navigation
  useEffect(() => { setMobileOpen(false); }, [pathname]);

  function handleHamburger(): void {
    if (window.matchMedia("(max-width: 768px)").matches) {
      setMobileOpen((o) => !o);
    } else {
      setCollapsed((c) => !c);
    }
  }

  return (
    <div className={`${styles.shell} ${collapsed ? styles.shellCollapsed : ""}`}>
      {/* Desktop sidebar — hidden on mobile via wrapper */}
      <div className={styles.desktopSidebar}>
        <Sidebar />
      </div>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className={styles.mobileOverlay} onClick={() => setMobileOpen(false)} aria-hidden="true" />
      )}
      <div className={`${styles.mobileSidebar} ${mobileOpen ? styles.mobileSidebarOpen : ""}`}>
        <Sidebar />
      </div>

      <div className={styles.main}>
        <header className={styles.navbar}>
          <button
            type="button"
            className={styles.navbarToggle}
            onClick={handleHamburger}
            title="Toggle navigation"
            aria-label="Toggle navigation"
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

        <main className={styles.content}>
          <div className={styles.contentInner}>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
