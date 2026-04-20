"use client";

import type { ReactElement } from "react";
import { useRouter } from "next/navigation";

import Button from "@mui/material/Button";

interface BackButtonProps {
  label: string;
}

export default function BackButton({ label }: BackButtonProps): ReactElement {
  const router = useRouter();

  function handleClick(): void {
    if (typeof window !== "undefined" && window.history.length > 1) {
      router.back();
      return;
    }
    router.push("/persons");
  }

  return (
    <Button onClick={handleClick} size="small">
      {label}
    </Button>
  );
}
