import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";

import { AppRouterCacheProvider } from "@mui/material-nextjs/v15-appRouter";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Container from "@mui/material/Container";
import CssBaseline from "@mui/material/CssBaseline";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import { ThemeProvider } from "@mui/material/styles";

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
            <AppBar position="static" color="primary" elevation={0}>
              <Toolbar>
                <Typography variant="h6" component="div" sx={{ fontWeight: 600 }}>
                  HyperP
                </Typography>
                <Typography variant="body2" sx={{ ml: 2, opacity: 0.85 }}>
                  Profile Unifier
                </Typography>
              </Toolbar>
            </AppBar>
            <Container maxWidth="lg" sx={{ py: 4 }}>
              <Box>{children}</Box>
            </Container>
          </ThemeProvider>
        </AppRouterCacheProvider>
      </body>
    </html>
  );
}
