"use client";

import { signIn } from "next-auth/react";
import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

export default function LoginPage(): ReactElement {
  return (
    <Box
      sx={{
        minHeight: "80vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Paper sx={{ p: 4, maxWidth: 420, width: "100%" }} elevation={2}>
        <Stack spacing={2} alignItems="center">
          <Typography variant="h5" fontWeight={700}>
            Sign in to HyperP
          </Typography>
          <Typography variant="body2" color="text.secondary" textAlign="center">
            Use your organisation Google account. Access is approved by an administrator.
          </Typography>
          <Button
            variant="contained"
            color="primary"
            fullWidth
            size="large"
            onClick={() => {
              void signIn("google", { callbackUrl: "/" });
            }}
          >
            Continue with Google
          </Button>
        </Stack>
      </Paper>
    </Box>
  );
}
