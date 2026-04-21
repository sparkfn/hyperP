"use client";

import { useEffect, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import AlertTitle from "@mui/material/AlertTitle";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

interface ErrorPageProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function ErrorPage({ error, reset }: ErrorPageProps): ReactElement {
  useEffect(() => {
    // Surface to the browser console for debugging — Next.js hides server
    // stack traces in production.
    console.error(error);
  }, [error]);

  return (
    <Box sx={{ py: 4 }}>
      <Alert severity="error" sx={{ mb: 3 }}>
        <AlertTitle>Something went wrong</AlertTitle>
        {error.message || "An unexpected error occurred while loading this page."}
      </Alert>
      {error.digest !== undefined ? (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
          digest: {error.digest}
        </Typography>
      ) : null}
      <Stack direction="row" spacing={2}>
        <Button variant="contained" onClick={reset}>
          Try again
        </Button>
        <Button variant="outlined" href="/">
          Back to search
        </Button>
      </Stack>
    </Box>
  );
}
