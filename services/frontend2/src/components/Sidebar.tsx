"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactElement } from "react";
import { MOCK_REVIEW_CASES } from "@/lib/mock-data";
import styles from "@/app/layout.module.css";

interface NavItem {
  href: string;
  label: string;
  exact?: boolean;
  badge?: number;
  icon: ReactElement;
}

interface NavSection {
  label: string;
  items: NavItem[];
}

const sections: NavSection[] = [
  {
    label: "",
    items: [
      {
        href: "/dashboard",
        exact: true,
        label: "Dashboard",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>,
      },
      {
        href: "/review",
        label: "Review Queue",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>,
      },
      {
        href: "/persons",
        label: "Persons",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>,
      },
      {
        href: "/graph",
        label: "Relationships",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="2"/><circle cx="6" cy="12" r="2"/><circle cx="18" cy="19" r="2"/><line x1="8" y1="11" x2="16" y2="6"/><line x1="8" y1="13" x2="16" y2="18"/></svg>,
      },
      {
        href: "/reports",
        label: "Reports",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>,
      },
    ],
  },
  {
    label: "Operations",
    items: [
      {
        href: "/ingestion",
        label: "Ingestion",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>,
      },
      {
        href: "/events",
        label: "Events",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>,
      },
    ],
  },
  {
    label: "System",
    items: [
      {
        href: "/entities",
        label: "Entities",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-4 0v2M8 7V5a2 2 0 0 0-4 0v2"/></svg>,
      },
      {
        href: "/admin",
        label: "Admin",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v2M12 20v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M2 12h2M20 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>,
      },
    ],
  },
];

const openReviewCount = MOCK_REVIEW_CASES.filter((r) => r.queue_state === "open").length;

export default function Sidebar(): ReactElement {
  const pathname = usePathname();

  const isActive = (href: string, exact?: boolean): boolean => {
    if (exact) return pathname === href;
    if (href === "/admin") return pathname.startsWith("/admin");
    return pathname.startsWith(href);
  };

  return (
    <aside className={styles.sidebar}>
      {/* Logo */}
      <Link href="/dashboard" className={styles.sidebarLogo}>
        <div className={styles.sidebarLogoMark}>H</div>
        <div>
          <div className={styles.sidebarLogoText}>Hyper<span>P</span></div>
          <div className={styles.sidebarLogoSub}>Profile Unifier</div>
        </div>
      </Link>


      {/* Grouped nav */}
      <nav className={styles.sidebarNav}>
        {sections.map((section) => (
          <div key={section.label} className={styles.navSection}>
            {section.label && <div className={styles.navSectionLabel}>{section.label}</div>}
            {section.items.map((item) => {
              const badge = item.href === "/review" ? openReviewCount : undefined;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`${styles.navItem} ${isActive(item.href, item.exact) ? styles.navItemActive : ""}`}
                >
                  {item.icon}
                  <span style={{ flex: 1 }}>{item.label}</span>
                  {badge !== undefined && badge > 0 && (
                    <span style={{
                      minWidth: 18, height: 18, borderRadius: 9,
                      background: "#dc2626", color: "#fff",
                      fontSize: 10, fontWeight: 700,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      padding: "0 5px",
                    }}>
                      {badge}
                    </span>
                  )}
                </Link>
              );
            })}
          </div>
        ))}
      </nav>
    </aside>
  );
}
