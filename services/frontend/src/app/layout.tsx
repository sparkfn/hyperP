import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";

import { AppRouterCacheProvider } from "@mui/material-nextjs/v15-appRouter";
import Box from "@mui/material/Box";
import CssBaseline from "@mui/material/CssBaseline";

import { auth } from "@/auth";
import SidebarNav from "@/components/SidebarNav";
import { ToastProvider } from "@/components/ToastProvider";
import SessionProviderClient from "@/components/auth/SessionProviderClient";
import { ThemeContextProvider } from "@/lib/ThemeContext";

export const metadata: Metadata = {
  title: "HyperP",
  description: "Customer profile unification and relationship intelligence",
};

interface RootLayoutProps {
  children: ReactNode;
}

export default async function RootLayout({
  children,
}: RootLayoutProps): Promise<ReactElement> {
  const session = await auth();
  const role = session?.user?.role ?? null;
  const email: string | null | undefined = session?.user?.email;
  const hideNav: boolean = !session || role === "first_time";

  return (
    <html lang="en">
      <body>
        <AppRouterCacheProvider>
          <ThemeContextProvider>
            <CssBaseline />
            <SessionProviderClient>
              <ToastProvider>
                <Box sx={{ display: "flex", minHeight: "100vh" }}>
                  <SidebarNav
                    hideNav={hideNav}
                    email={email}
                    displayName={session?.user?.displayName ?? null}
                    role={role}
                    entityKey={session?.user?.entityKey ?? null}
                    sessionError={session?.error}
                  />
                  <Box
                    component="main"
                    sx={{ flex: 1, minWidth: 0, px: "5%", py: 2.5, overflow: "auto" }}
                  >
                    {children}
                  </Box>
                </Box>
              </ToastProvider>
            </SessionProviderClient>
          </ThemeContextProvider>
        </AppRouterCacheProvider>
      </body>
    </html>
  );
}
