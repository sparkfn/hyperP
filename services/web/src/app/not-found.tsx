import type { ReactElement } from "react";

import Alert from "@mui/material/Alert";
import AlertTitle from "@mui/material/AlertTitle";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";

export default function NotFound(): ReactElement {
  return (
    <Box sx={{ py: 4 }}>
      <Alert severity="warning" sx={{ mb: 3 }}>
        <AlertTitle>Not found</AlertTitle>
        The resource you were looking for does not exist.
      </Alert>
      <Button variant="contained" href="/">
        Back to search
      </Button>
    </Box>
  );
}
