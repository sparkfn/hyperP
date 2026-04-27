import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";
import Link from "next/link";

import { AppRouterCacheProvider } from "@mui/material-nextjs/v15-appRouter";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Container from "@mui/material/Container";
import CssBaseline from "@mui/material/CssBaseline";
import Stack from "@mui/material/Stack";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import { ThemeProvider } from "@mui/material/styles";

import { auth } from "@/auth";
import HealthIndicator from "@/components/HealthIndicator";
import { ToastProvider } from "@/components/ToastProvider";
import SessionProviderClient from "@/components/auth/SessionProviderClient";
import { UserMenu } from "@/components/auth/UserMenu";
import theme from "@/theme";

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

  // Chromeless screens hide the top nav. Middleware enforces auth/redirects,
  // so we only need to suppress nav for the auth-flow pages.
  const hideNav: boolean = !session || role === "first_time";

  return (
    <html lang="en">
      <body>
        <AppRouterCacheProvider>
          <ThemeProvider theme={theme}>
            <CssBaseline />
            <SessionProviderClient>
              <ToastProvider>
                {!hideNav ? (
                  <AppBar position="static" color="primary" elevation={0}>
                    <Toolbar variant="dense" sx={{ gap: 1 }}>
                      <Typography
                        variant="subtitle1"
                        component="div"
                        sx={{ fontWeight: 700, letterSpacing: 0.3 }}
                      >
                        HyperP
                      </Typography>
                      <Typography variant="caption" sx={{ ml: 1, opacity: 0.8 }}>
                        Profile Unifier
                      </Typography>
                      <Box sx={{ flexGrow: 1 }} />
                      <Stack direction="row" spacing={0.5} alignItems="center">
                        <Button component={Link} href="/persons" color="inherit">
                          Persons
                        </Button>
                        <Button component={Link} href="/entities" color="inherit">
                          Entities
                        </Button>
                        <Button component={Link} href="/reports" color="inherit">
                          Reports
                        </Button>
                        <Button component={Link} href="/graph" color="inherit">
                          Graph
                        </Button>
                        <Button component={Link} href="/review" color="inherit">
                          Review
                        </Button>
                        <Button component={Link} href="/ingestion" color="inherit">
                          Ingestion
                        </Button>
                        <Button component={Link} href="/events" color="inherit">
                          Events
                        </Button>
                        <Button component={Link} href="/admin" color="inherit">
                          Admin
                        </Button>
                        <Box sx={{ ml: 1, display: "flex", alignItems: "center" }}>
                          <HealthIndicator />
                        </Box>
                        {email ? (
                          <UserMenu
                            email={email}
                            displayName={session?.user?.displayName ?? null}
                            role={role === "admin" || role === "employee" ? role : "first_time"}
                            entityKey={session?.user?.entityKey ?? null}
                            sessionError={session?.error}
                          />
                        ) : null}
                      </Stack>
                    </Toolbar>
                  </AppBar>
                ) : null}
                <Container maxWidth={false} sx={{ px: "5%", py: 2 }}>
                  <Box>{children}</Box>
                </Container>
              </ToastProvider>
            </SessionProviderClient>
          </ThemeProvider>
        </AppRouterCacheProvider>
      </body>
    </html>
  );
}
