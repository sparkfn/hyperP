"use client";

import {
  useCallback,
  useEffect,
  useState,
  type ReactElement,
} from "react";

import CopyIcon from "@mui/icons-material/ContentCopy";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import Gate from "@/components/auth/Gate";
import { bffFetch, BffError } from "@/lib/api-client";
import type {
  CreateOAuthClientRequest,
  OAuthClient,
  OAuthClientCreated,
  OAuthClientSecret,
  OAuthClientSecretCreated,
} from "@/lib/api-types-ops";
import { OAUTH_CLIENT_SCOPES } from "@/lib/api-types-ops";

type OneTimeSecret = OAuthClientCreated | OAuthClientSecretCreated;

export default function OAuthClientsAdminPage(): ReactElement {
  const [clients, setClients] = useState<OAuthClient[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  const [createOpen, setCreateOpen] = useState<boolean>(false);
  const [newSecret, setNewSecret] = useState<OneTimeSecret | null>(null);

  const [name, setName] = useState<string>("");
  const [scopes, setScopes] = useState<string[]>(["persons:read"]);
  const [expiresInDays, setExpiresInDays] = useState<string>("365");
  const [formErr, setFormErr] = useState<string | null>(null);
  const [copied, setCopied] = useState<boolean>(false);

  const loadClients = useCallback(async (): Promise<void> => {
    try {
      setLoading(true);
      const envelope = await bffFetch<OAuthClient[]>("/bff/admin/oauth-clients");
      setClients(envelope);
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to load OAuth clients");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadClients();
  }, [loadClients]);

  const toggleScope = useCallback((scope: string): void => {
    setScopes((prev) =>
      prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope],
    );
  }, []);

  const handleCreate = useCallback(async (): Promise<void> => {
    setFormErr(null);
    if (!name.trim()) {
      setFormErr("Name is required.");
      return;
    }
    if (scopes.length === 0) {
      setFormErr("At least one scope is required.");
      return;
    }
    const expiresDays = parseExpiryDays(expiresInDays);
    if (expiresDays === "invalid") {
      setFormErr("Secret expiry must be between 1 and 730 days (or blank for no expiry).");
      return;
    }
    setBusy(true);
    try {
      const payload: CreateOAuthClientRequest = {
        name: name.trim(),
        entity_key: null,
        scopes,
        secret_expires_in_days: expiresDays,
      };
      const created = await bffFetch<OAuthClientCreated>("/bff/admin/oauth-clients", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setNewSecret(created);
      setCreateOpen(false);
      setName("");
      setScopes(["persons:read"]);
      setExpiresInDays("365");
      await loadClients();
    } catch (e: unknown) {
      setFormErr(e instanceof BffError ? e.message : "Failed to create OAuth client");
    } finally {
      setBusy(false);
    }
  }, [name, scopes, expiresInDays, loadClients]);

  const handleRotateSecret = useCallback(async (clientId: string): Promise<void> => {
    try {
      const created = await bffFetch<OAuthClientSecretCreated>(
        `/bff/admin/oauth-clients/${encodeURIComponent(clientId)}/secrets`,
        { method: "POST", body: JSON.stringify({ expires_in_days: 365 }) },
      );
      setNewSecret(created);
      await loadClients();
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to rotate client secret");
    }
  }, [loadClients]);

  const handleRevokeSecret = useCallback(
    async (clientId: string, secretId: string): Promise<void> => {
      try {
        await bffFetch<void>(
          `/bff/admin/oauth-clients/${encodeURIComponent(clientId)}/secrets/${encodeURIComponent(secretId)}`,
          { method: "POST" },
        );
        await loadClients();
      } catch (e: unknown) {
        setErr(e instanceof BffError ? e.message : "Failed to revoke client secret");
      }
    },
    [loadClients],
  );

  const handleDisableClient = useCallback(async (clientId: string): Promise<void> => {
    try {
      await bffFetch<void>(`/bff/admin/oauth-clients/${encodeURIComponent(clientId)}`, {
        method: "POST",
      });
      await loadClients();
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to disable OAuth client");
    }
  }, [loadClients]);

  const handleDeleteClient = useCallback(async (clientId: string): Promise<void> => {
    try {
      await bffFetch<void>(`/bff/admin/oauth-clients/${encodeURIComponent(clientId)}`, {
        method: "DELETE",
      });
      await loadClients();
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to delete OAuth client");
    }
  }, [loadClients]);

  const copySecret = useCallback(async (secret: string): Promise<void> => {
    await navigator.clipboard.writeText(secret);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, []);

  return (
    <Stack spacing={3}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Box>
          <Typography variant="h5" fontWeight={700}>
            OAuth clients
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Server-to-server OAuth client credentials for external integrations.
          </Typography>
        </Box>
        <Gate mode="admin" disableInsteadOfHide>
          <Button variant="contained" onClick={() => setCreateOpen(true)}>
            Create client
          </Button>
        </Gate>
      </Stack>

      {err ? <Alert severity="error" onClose={() => setErr(null)}>{err}</Alert> : null}

      {loading ? (
        <Typography variant="body2" color="text.secondary">Loading…</Typography>
      ) : clients.length === 0 ? (
        <Paper variant="outlined" sx={{ p: 3, textAlign: "center" }}>
          <Typography color="text.secondary">No OAuth clients yet.</Typography>
          <Typography variant="caption" color="text.secondary">
            OAuth clients are used for server-to-server authentication. Create one from the button above.
          </Typography>
        </Paper>
      ) : (
        <Paper variant="outlined">
          <List disablePadding>
            {clients.map((client, idx) => (
              <OAuthClientRow
                key={client.client_id}
                client={client}
                onRotateSecret={handleRotateSecret}
                onRevokeSecret={handleRevokeSecret}
                onDisableClient={handleDisableClient}
                onDeleteClient={handleDeleteClient}
                isLast={idx === clients.length - 1}
              />
            ))}
          </List>
        </Paper>
      )}

      <Dialog open={createOpen} onClose={() => setCreateOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Create OAuth client</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <TextField
              label="Name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Fundbox Integration"
              fullWidth
              size="small"
              autoFocus
            />
            <Box>
              <Typography variant="body2" fontWeight={500} sx={{ mb: 1 }}>
                Scopes
              </Typography>
              <Stack direction="row" flexWrap="wrap" spacing={1}>
                {OAUTH_CLIENT_SCOPES.map((scope) => (
                  <Chip
                    key={scope}
                    label={scope}
                    variant={scopes.includes(scope) ? "filled" : "outlined"}
                    color={scopes.includes(scope) ? "primary" : "default"}
                    onClick={() => toggleScope(scope)}
                    onDelete={() => toggleScope(scope)}
                    sx={{ cursor: "pointer" }}
                  />
                ))}
              </Stack>
              <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: "block" }}>
                The &ldquo;admin&rdquo; scope grants full access and supersedes all other scopes.
              </Typography>
            </Box>
            <TextField
              label="Initial secret expires in (days)"
              value={expiresInDays}
              onChange={(e) => setExpiresInDays(e.target.value)}
              placeholder="365"
              type="number"
              fullWidth
              size="small"
              inputProps={{ min: 1, max: 730 }}
              helperText="Leave blank or use 0 for no expiry"
            />
            {formErr ? <Alert severity="error" sx={{ py: 0 }}>{formErr}</Alert> : null}
            <Stack direction="row" justifyContent="flex-end" spacing={1}>
              <Button onClick={() => setCreateOpen(false)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="contained" onClick={() => void handleCreate()} disabled={busy}>
                {busy ? "Creating…" : "Create client"}
              </Button>
            </Stack>
          </Stack>
        </DialogContent>
      </Dialog>

      <Dialog open={newSecret !== null} onClose={() => setNewSecret(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <Chip label="Save this now" color="warning" size="small" />
          Client secret will not be shown again
        </DialogTitle>
        <DialogContent>
          <Alert severity="warning" sx={{ mb: 2 }}>
            Copy and store this client secret securely. It will never be shown again.
          </Alert>
          <Paper
            variant="outlined"
            sx={{
              p: 2,
              fontFamily: "monospace",
              fontSize: 14,
              wordBreak: "break-all",
              bgcolor: "grey.900",
              color: "success.main",
              position: "relative",
            }}
          >
            {newSecret?.client_secret ?? ""}
            <Tooltip title={copied ? "Copied!" : "Copy client secret"}>
              <IconButton
                size="small"
                onClick={() => void copySecret(newSecret?.client_secret ?? "")}
                sx={{ position: "absolute", top: 8, right: 8, color: "grey.400" }}
              >
                <CopyIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Paper>
          <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: "block" }}>
            Client ID: <code>{newSecret?.client_id}</code>
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            Prefix for identification: <code>{newSecret?.secret_prefix}</code>
          </Typography>
          <Stack direction="row" justifyContent="flex-end" sx={{ mt: 2 }}>
            <Button variant="contained" onClick={() => setNewSecret(null)}>
              Done
            </Button>
          </Stack>
        </DialogContent>
      </Dialog>
    </Stack>
  );
}

function parseExpiryDays(value: string): number | null | "invalid" {
  if (!value || value === "0") {
    return null;
  }
  const expiresDays = parseInt(value, 10);
  if (isNaN(expiresDays) || expiresDays < 1 || expiresDays > 730) {
    return "invalid";
  }
  return expiresDays;
}

interface OAuthClientRowProps {
  client: OAuthClient;
  onRotateSecret: (clientId: string) => Promise<void>;
  onRevokeSecret: (clientId: string, secretId: string) => Promise<void>;
  onDisableClient: (clientId: string) => Promise<void>;
  onDeleteClient: (clientId: string) => Promise<void>;
  isLast: boolean;
}

function OAuthClientRow(props: OAuthClientRowProps): ReactElement {
  const [busy, setBusy] = useState<boolean>(false);
  const [confirmDelete, setConfirmDelete] = useState<boolean>(false);
  const [confirmDisable, setConfirmDisable] = useState<boolean>(false);

  const isDisabled = props.client.disabled_at !== null;

  async function rotateSecret(): Promise<void> {
    setBusy(true);
    await props.onRotateSecret(props.client.client_id);
    setBusy(false);
  }

  async function revokeSecret(secretId: string): Promise<void> {
    setBusy(true);
    await props.onRevokeSecret(props.client.client_id, secretId);
    setBusy(false);
  }

  async function disableClient(): Promise<void> {
    setBusy(true);
    await props.onDisableClient(props.client.client_id);
    setBusy(false);
    setConfirmDisable(false);
  }

  async function deleteClient(): Promise<void> {
    setBusy(true);
    await props.onDeleteClient(props.client.client_id);
    setBusy(false);
    setConfirmDelete(false);
  }

  return (
    <ListItem
      disableGutters
      sx={{ px: 2, py: 1.5, flexDirection: "column", alignItems: "stretch" }}
      divider={!props.isLast}
    >
      <Stack direction="row" alignItems="flex-start" justifyContent="space-between" spacing={2}>
        <Box>
          <Stack direction="row" alignItems="center" spacing={1}>
            <Typography variant="body2" fontWeight={600} fontFamily="monospace">
              {props.client.client_id}
            </Typography>
            {isDisabled ? <Chip label="disabled" size="small" color="default" /> : null}
          </Stack>
          <Typography variant="caption" color="text.secondary">
            {props.client.name} &mdash; created by {props.client.created_by}
            {props.client.entity_key ? ` · entity: ${props.client.entity_key}` : ""}
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} alignItems="center" justifyContent="flex-end" flexWrap="wrap">
          <Stack direction="row" spacing={0.5} flexWrap="wrap">
            {props.client.scopes.map((scope) => (
              <Chip key={scope} label={scope} size="small" variant="outlined" />
            ))}
          </Stack>
          <Gate mode="admin" disableInsteadOfHide>
            <ClientActions
              busy={busy}
              isDisabled={isDisabled}
              confirmDisable={confirmDisable}
              confirmDelete={confirmDelete}
              onRotateSecret={() => void rotateSecret()}
              onAskDisable={() => setConfirmDisable(true)}
              onCancelDisable={() => setConfirmDisable(false)}
              onDisable={() => void disableClient()}
              onAskDelete={() => setConfirmDelete(true)}
              onCancelDelete={() => setConfirmDelete(false)}
              onDelete={() => void deleteClient()}
            />
          </Gate>
        </Stack>
      </Stack>
      <ClientTimestamps client={props.client} />
      <Box sx={{ mt: 1.5, pl: { xs: 0, md: 2 } }}>
        <Typography variant="caption" color="text.secondary" fontWeight={600}>
          Client secrets
        </Typography>
        {props.client.secrets.length === 0 ? (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            No client secrets.
          </Typography>
        ) : (
          <Stack spacing={0.75} sx={{ mt: 0.5 }}>
            {props.client.secrets.map((secret) => (
              <OAuthClientSecretRow
                key={secret.secret_id}
                secret={secret}
                busy={busy}
                onRevoke={() => void revokeSecret(secret.secret_id)}
              />
            ))}
          </Stack>
        )}
      </Box>
    </ListItem>
  );
}

interface ClientActionsProps {
  busy: boolean;
  isDisabled: boolean;
  confirmDisable: boolean;
  confirmDelete: boolean;
  onRotateSecret: () => void;
  onAskDisable: () => void;
  onCancelDisable: () => void;
  onDisable: () => void;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onDelete: () => void;
}

function ClientActions(props: ClientActionsProps): ReactElement {
  if (props.confirmDelete) {
    return (
      <>
        <Typography variant="caption" color="error">Delete?</Typography>
        <Button size="small" color="error" onClick={props.onDelete} disabled={props.busy}>
          Yes
        </Button>
        <Button size="small" onClick={props.onCancelDelete} disabled={props.busy}>
          No
        </Button>
      </>
    );
  }

  if (props.confirmDisable) {
    return (
      <>
        <Typography variant="caption" color="warning.main">Disable?</Typography>
        <Button size="small" color="warning" onClick={props.onDisable} disabled={props.busy}>
          Yes
        </Button>
        <Button size="small" onClick={props.onCancelDisable} disabled={props.busy}>
          No
        </Button>
      </>
    );
  }

  return (
    <>
      <Tooltip title="Create a new client secret and show it once">
        <Button size="small" onClick={props.onRotateSecret} disabled={props.busy || props.isDisabled}>
          Rotate secret
        </Button>
      </Tooltip>
      {!props.isDisabled ? (
        <Tooltip title="Disable this client and stop all of its secrets from validating">
          <Button size="small" color="warning" onClick={props.onAskDisable} disabled={props.busy}>
            Disable
          </Button>
        </Tooltip>
      ) : null}
      <Button
        size="small"
        color="error"
        variant="outlined"
        onClick={props.onAskDelete}
        disabled={props.busy}
      >
        Delete
      </Button>
    </>
  );
}

interface ClientTimestampsProps {
  client: OAuthClient;
}

function ClientTimestamps(props: ClientTimestampsProps): ReactElement {
  return (
    <Stack spacing={0.25} sx={{ mt: 0.5 }}>
      {props.client.last_used_at ? (
        <Typography variant="caption" color="text.secondary">
          Last used: {new Date(props.client.last_used_at).toLocaleString()}
        </Typography>
      ) : null}
      {props.client.disabled_at ? (
        <Typography variant="caption" color="text.secondary">
          Disabled: {new Date(props.client.disabled_at).toLocaleString()}
        </Typography>
      ) : null}
    </Stack>
  );
}

interface OAuthClientSecretRowProps {
  secret: OAuthClientSecret;
  busy: boolean;
  onRevoke: () => void;
}

function OAuthClientSecretRow(props: OAuthClientSecretRowProps): ReactElement {
  const expiresDate = props.secret.expires_at ? new Date(props.secret.expires_at) : null;
  const isExpired = expiresDate !== null && expiresDate < new Date();
  const isRevoked = props.secret.revoked_at !== null;

  return (
    <Stack
      direction={{ xs: "column", md: "row" }}
      spacing={1}
      alignItems={{ xs: "flex-start", md: "center" }}
      justifyContent="space-between"
      sx={{ borderTop: 1, borderColor: "divider", pt: 0.75 }}
    >
      <Box>
        <Stack direction="row" alignItems="center" spacing={1}>
          <Typography variant="body2" fontFamily="monospace">
            {props.secret.secret_prefix}{"*".repeat(20)}
          </Typography>
          {isRevoked ? <Chip label="revoked" size="small" color="default" /> : null}
          {!isRevoked && isExpired ? <Chip label="expired" size="small" color="error" /> : null}
        </Stack>
        <Stack direction="row" spacing={1} flexWrap="wrap">
          {props.secret.last_used_at ? (
            <Typography variant="caption" color="text.secondary">
              Last used: {new Date(props.secret.last_used_at).toLocaleString()}
            </Typography>
          ) : null}
          {expiresDate ? (
            <Typography variant="caption" color="text.secondary">
              Expires: {expiresDate.toLocaleDateString()}
              {isExpired ? " (expired)" : ""}
            </Typography>
          ) : null}
        </Stack>
      </Box>
      <Gate mode="admin" disableInsteadOfHide>
        {!isRevoked ? (
          <Tooltip title="Revoke this client secret immediately">
            <Button size="small" color="warning" onClick={props.onRevoke} disabled={props.busy}>
              Revoke secret
            </Button>
          </Tooltip>
        ) : null}
      </Gate>
    </Stack>
  );
}
