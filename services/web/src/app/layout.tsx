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

import HealthIndicator from "@/components/HealthIndicator";
import { ToastProvider } from "@/components/ToastProvider";
import theme from "@/theme";

export const metadata: Metadata = {
  title: "HyperP",
  description: "Customer profile unification and relationship intelligence",
};

interface RootLayoutProps {
  children: ReactNode;
}

export default function RootLayout({ children }: RootLayoutProps): ReactElement {
  return (
    <html lang="en">
      <body>
        <AppRouterCacheProvider>
          <ThemeProvider theme={theme}>
            <CssBaseline />
            <ToastProvider>
              <AppBar position="static" color="primary" elevation={0}>
                <Toolbar variant="dense" sx={{ gap: 1 }}>
                  <Typography variant="subtitle1" component="div" sx={{ fontWeight: 700, letterSpacing: 0.3 }}>
                    HyperP
                  </Typography>
                  <Typography variant="caption" sx={{ ml: 1, opacity: 0.8 }}>
                    Profile Unifier
                  </Typography>
                  <Box sx={{ flexGrow: 1 }} />
                  <Stack direction="row" spacing={0.5}>
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
                  </Stack>
                </Toolbar>
              </AppBar>
              <Container maxWidth="xl" sx={{ py: 2 }}>
                <Box>{children}</Box>
              </Container>
            </ToastProvider>
          </ThemeProvider>
        </AppRouterCacheProvider>
      </body>
    </html>
  );
}
