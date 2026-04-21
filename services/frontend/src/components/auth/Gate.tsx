"use client";

import type { ReactElement, ReactNode } from "react";
import { useSession } from "next-auth/react";

import Tooltip from "@mui/material/Tooltip";
import Box from "@mui/material/Box";

import {
  canMutateAdminOnly,
  canMutateForEntity,
  type Role,
} from "@/lib/permissions";

type GateMode = "admin" | "mutator";

interface GateProps {
  mode: GateMode;
  entityKey?: string | null;
  children: ReactNode;
  /** If true, render a disabled + tooltipped wrapper instead of hiding. */
  disableInsteadOfHide?: boolean;
}

export default function Gate(props: GateProps): ReactElement | null {
  const { data: session } = useSession();
  const role: Role = session?.user?.role ?? "first_time";
  const userEntity: string | null = session?.user?.entityKey ?? null;

  const allowed: boolean =
    props.mode === "admin"
      ? canMutateAdminOnly(role)
      : canMutateForEntity(role, userEntity, props.entityKey ?? null);

  if (allowed) return <>{props.children}</>;
  if (!props.disableInsteadOfHide) return null;

  const reason: string =
    props.mode === "admin"
      ? "Requires administrator privileges."
      : "You don't have permission to mutate data for this entity.";

  return (
    <Tooltip title={reason}>
      <Box sx={{ display: "inline-block", pointerEvents: "none", opacity: 0.5 }}>
        {props.children}
      </Box>
    </Tooltip>
  );
}

export function useCanMutateAdmin(): boolean {
  const { data: session } = useSession();
  return canMutateAdminOnly(session?.user?.role ?? "first_time");
}

export function useCanMutateEntity(entityKey: string | null | undefined): boolean {
  const { data: session } = useSession();
  return canMutateForEntity(
    session?.user?.role ?? "first_time",
    session?.user?.entityKey ?? null,
    entityKey ?? null,
  );
}
