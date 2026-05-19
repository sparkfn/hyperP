"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactElement } from "react";
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
        href: "/persons",
        label: "Persons",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>,
      },
      {
        href: "/entities",
        label: "Entities",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-4 0v2M8 7V5a2 2 0 0 0-4 0v2"/></svg>,
      },
      {
        href: "/graph",
        label: "Graph",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="2"/><circle cx="6" cy="12" r="2"/><circle cx="18" cy="19" r="2"/><line x1="8" y1="11" x2="16" y2="6"/><line x1="8" y1="13" x2="16" y2="18"/></svg>,
      },
      {
        href: "/review",
        label: "Review",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>,
      },
      {
        href: "/ingestion",
        label: "Ingestion",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>,
      },
      {
        href: "/events",
        label: "Events",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>,
      },
      {
        href: "/admin",
        label: "Admin",
        icon: <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L3 7v5c0 5.25 3.75 10.15 9 11.25C17.25 22.15 21 17.25 21 12V7z"/></svg>,
      },
    ],
  },
];


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
        <div className={styles.sidebarLogoText}>HyperP</div>
      </Link>


      {/* Grouped nav */}
      <nav className={styles.sidebarNav}>
        {sections.map((section) => (
          <div key={section.label} className={styles.navSection}>
            {section.label && <div className={styles.navSectionLabel}>{section.label}</div>}
            {section.items.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`${styles.navItem} ${isActive(item.href, item.exact) ? styles.navItemActive : ""}`}
              >
                {item.icon}
                <span className={styles.navLabel}>{item.label}</span>
                <span className={styles.navTooltip} aria-hidden="true">{item.label}</span>
              </Link>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}
