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
                <Toolbar>
                  <Typography variant="h6" component="div" sx={{ fontWeight: 600 }}>
                    HyperP
                  </Typography>
                  <Typography variant="body2" sx={{ ml: 2, opacity: 0.85 }}>
                    Profile Unifier
                  </Typography>
                  <Box sx={{ flexGrow: 1 }} />
                  <Stack direction="row" spacing={1}>
                    <Button component={Link} href="/" color="inherit" size="small">
                      Search
                    </Button>
                    <Button component={Link} href="/explore" color="inherit" size="small">
                      Explore
                    </Button>
                    <Button component={Link} href="/review" color="inherit" size="small">
                      Review Queue
                    </Button>
                    <Button component={Link} href="/ingestion" color="inherit" size="small">
                      Ingestion
                    </Button>
                    <Button component={Link} href="/events" color="inherit" size="small">
                      Events
                    </Button>
                    <Button component={Link} href="/admin" color="inherit" size="small">
                      Admin
                    </Button>
                    <Box sx={{ ml: 1, display: "flex", alignItems: "center" }}>
                      <HealthIndicator />
                    </Box>
                  </Stack>
                </Toolbar>
              </AppBar>
              <Container maxWidth="lg" sx={{ py: 4 }}>
                <Box>{children}</Box>
              </Container>
            </ToastProvider>
          </ThemeProvider>
        </AppRouterCacheProvider>
      </body>
    </html>
  );
}
