import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import Button from "@mui/material/Button";

import { auth, signOut } from "@/auth";

export default async function PendingPage(): Promise<ReactElement> {
  const session = await auth();
  const email: string | null | undefined = session?.user?.email;

  return (
    <Box
      sx={{
        minHeight: "70vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Paper sx={{ p: 4, maxWidth: 520, width: "100%" }} elevation={2}>
        <Stack spacing={2} alignItems="center" textAlign="center">
          <Typography variant="h5" fontWeight={700}>
            Awaiting approval
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Your account ({email ?? "unknown"}) has been created but is not yet
            assigned to a tenant. An administrator must grant you access before
            you can use the application.
          </Typography>
          <Typography variant="caption" color="text.secondary">
            If this takes longer than expected, contact your administrator.
          </Typography>
          <form
            action={async () => {
              "use server";
              await signOut({ redirectTo: "/login" });
            }}
          >
            <Button type="submit" variant="outlined" color="inherit">
              Sign out
            </Button>
          </form>
        </Stack>
      </Paper>
    </Box>
  );
}
