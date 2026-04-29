"use client";

import {
  useCallback,
  useEffect,
  useState,
  type ReactElement,
} from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CopyIcon from "@mui/icons-material/ContentCopy";
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

import { bffFetch, BffError } from "@/lib/api-client";
import type {
  ApiKey,
  ApiKeyCreated,
  CreateApiKeyRequest,
} from "@/lib/api-types-ops";
import { API_KEY_SCOPES } from "@/lib/api-types-ops";
import Gate from "@/components/auth/Gate";

export default function ApiKeysAdminPage(): ReactElement {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  const [createOpen, setCreateOpen] = useState<boolean>(false);
  const [newKey, setNewKey] = useState<ApiKeyCreated | null>(null);

  // Create form state
  const [name, setName] = useState<string>("");
  const [scopes, setScopes] = useState<string[]>(["persons:read"]);
  const [expiresInDays, setExpiresInDays] = useState<string>("365");
  const [formErr, setFormErr] = useState<string | null>(null);

  const loadKeys = useCallback(async (): Promise<void> => {
    try {
      setLoading(true);
      const envelope = await bffFetch<ApiKey[]>("/bff/admin/api-keys");
      setKeys(envelope);
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to load API keys");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadKeys();
  }, [loadKeys]);

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
    const expiresDays = expiresInDays ? parseInt(expiresInDays, 10) : null;
    if (expiresDays !== null && (isNaN(expiresDays) || expiresDays < 1 || expiresDays > 730)) {
      setFormErr("Expiry must be between 1 and 730 days (or blank for no expiry).");
      return;
    }
    setBusy(true);
    try {
      const payload: CreateApiKeyRequest = {
        name: name.trim(),
        entity_key: null,
        scopes,
        expires_in_days: expiresDays,
      };
      const created = await bffFetch<ApiKeyCreated>("/bff/admin/api-keys", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setNewKey(created);
      setCreateOpen(false);
      await loadKeys();
    } catch (e: unknown) {
      setFormErr(e instanceof BffError ? e.message : "Failed to create key");
    } finally {
      setBusy(false);
    }
  }, [name, scopes, expiresInDays, loadKeys]);

  const handleRevoke = useCallback(async (keyId: string): Promise<void> => {
    try {
      await bffFetch<void>(`/bff/admin/api-keys/${encodeURIComponent(keyId)}`, {
        method: "POST",
      });
      await loadKeys();
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to revoke key");
    }
  }, [loadKeys]);

  const handleDelete = useCallback(async (keyId: string): Promise<void> => {
    try {
      await bffFetch<void>(`/bff/admin/api-keys/${encodeURIComponent(keyId)}`, {
        method: "DELETE",
      });
      await loadKeys();
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to delete key");
    }
  }, [loadKeys]);

  const [copied, setCopied] = useState<boolean>(false);
  const copyKey = useCallback(async (key: string): Promise<void> => {
    await navigator.clipboard.writeText(key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, []);

  return (
    <Stack spacing={3}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Box>
          <Typography variant="h5" fontWeight={700}>
            API Keys
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Server-to-server authentication for external integrations.
          </Typography>
        </Box>
        <Gate mode="admin" disableInsteadOfHide>
          <Button variant="contained" onClick={() => setCreateOpen(true)}>
            Create Key
          </Button>
        </Gate>
      </Stack>

      {err ? <Alert severity="error" onClose={() => setErr(null)}>{err}</Alert> : null}

      {loading ? (
        <Typography variant="body2" color="text.secondary">Loading…</Typography>
      ) : keys.length === 0 ? (
        <Paper variant="outlined" sx={{ p: 3, textAlign: "center" }}>
          <Typography color="text.secondary">No API keys yet.</Typography>
          <Typography variant="caption" color="text.secondary">
            Keys are used for server-to-server authentication. Create one from the button above.
          </Typography>
        </Paper>
      ) : (
        <Paper variant="outlined">
          <List disablePadding>
            {keys.map((key, idx) => (
              <ApiKeyRow
                key={key.id}
                keyData={key}
                onRevoke={handleRevoke}
                onDelete={handleDelete}
                isLast={idx === keys.length - 1}
              />
            ))}
          </List>
        </Paper>
      )}

      {/* --- Create Dialog --- */}
      <Dialog open={createOpen} onClose={() => setCreateOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Create API Key</DialogTitle>
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
                {API_KEY_SCOPES.map((scope) => (
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
              label="Expires in (days)"
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
                {busy ? "Creating…" : "Create Key"}
              </Button>
            </Stack>
          </Stack>
        </DialogContent>
      </Dialog>

      {/* --- New Key Secret Dialog --- */}
      <Dialog open={newKey !== null} onClose={() => setNewKey(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <Chip label="Save this now" color="warning" size="small" />
          Secret will not be shown again
        </DialogTitle>
        <DialogContent>
          <Alert severity="warning" sx={{ mb: 2 }}>
            Copy and store this secret securely. It will never be shown again.
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
            {newKey?.key ?? ""}
            <Tooltip title={copied ? "Copied!" : "Copy secret"}>
              <IconButton
                size="small"
                onClick={() => void copyKey(newKey?.key ?? "")}
                sx={{ position: "absolute", top: 8, right: 8, color: "grey.400" }}
              >
                <CopyIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Paper>
          <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: "block" }}>
            Prefix for identification: <code>{newKey?.key_prefix}</code>
          </Typography>
          <Stack direction="row" justifyContent="flex-end" sx={{ mt: 2 }}>
            <Button variant="contained" onClick={() => setNewKey(null)}>
              Done
            </Button>
          </Stack>
        </DialogContent>
      </Dialog>
    </Stack>
  );
}

interface ApiKeyRowProps {
  keyData: ApiKey;
  onRevoke: (id: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  isLast: boolean;
}

function ApiKeyRow(props: ApiKeyRowProps): ReactElement {
  const [busy, setBusy] = useState<boolean>(false);
  const [confirmDelete, setConfirmDelete] = useState<boolean>(false);

  const expiresDate = props.keyData.expires_at
    ? new Date(props.keyData.expires_at)
    : null;
  const isExpired = expiresDate && expiresDate < new Date();
  const isRevoked = props.keyData.is_revoked;

  async function revoke(): Promise<void> {
    setBusy(true);
    await props.onRevoke(props.keyData.id);
    setBusy(false);
  }

  async function doDelete(): Promise<void> {
    setBusy(true);
    await props.onDelete(props.keyData.id);
    setBusy(false);
    setConfirmDelete(false);
  }

  return (
    <>
      <ListItem
        disableGutters
        sx={{ px: 2, py: 1.5, flexDirection: "column", alignItems: "stretch" }}
        divider={!props.isLast}
      >
        <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={2}>
          <Box>
            <Stack direction="row" alignItems="center" spacing={1}>
              <Typography variant="body2" fontWeight={600} fontFamily="monospace" sx={isRevoked ? { opacity: 0.5 } : undefined}>
                {props.keyData.key_prefix}{"*".repeat(20)}
              </Typography>
              {isRevoked && <Chip label="revoked" size="small" color="default" />}
              {!isRevoked && isExpired && <Chip label="expired" size="small" color="error" />}
            </Stack>
            <Typography variant="caption" color="text.secondary">
              {props.keyData.name} &mdash; created by {props.keyData.created_by}
              {props.keyData.entity_key ? ` · entity: ${props.keyData.entity_key}` : ""}
            </Typography>
          </Box>
          <Stack direction="row" spacing={1} alignItems="center">
            <Stack direction="row" spacing={0.5} flexWrap="wrap">
              {props.keyData.scopes.map((s) => (
                <Chip key={s} label={s} size="small" variant="outlined" />
              ))}
            </Stack>
            <Gate mode="admin" disableInsteadOfHide>
              {confirmDelete ? (
                <>
                  <Typography variant="caption" color="error">Delete?</Typography>
                  <Button size="small" color="error" onClick={() => void doDelete()} disabled={busy}>
                    Yes
                  </Button>
                  <Button size="small" onClick={() => setConfirmDelete(false)} disabled={busy}>
                    No
                  </Button>
                </>
              ) : (
                <>
                  {!isRevoked && (
                    <Tooltip title="Soft-revoke: key stops validating immediately but stays visible for audit">
                      <Button size="small" color="warning" onClick={() => void revoke()} disabled={busy}>
                        Revoke
                      </Button>
                    </Tooltip>
                  )}
                  <Button
                    size="small"
                    color="error"
                    variant="outlined"
                    onClick={() => setConfirmDelete(true)}
                    disabled={busy}
                  >
                    Delete
                  </Button>
                </>
              )}
            </Gate>
          </Stack>
        </Stack>
        {props.keyData.last_used_at ? (
          <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
            Last used: {new Date(props.keyData.last_used_at).toLocaleString()}
          </Typography>
        ) : null}
        {expiresDate ? (
          <Typography variant="caption" color="text.secondary">
            Expires: {expiresDate.toLocaleDateString()}
            {isExpired ? " (expired)" : ""}
          </Typography>
        ) : null}
      </ListItem>
    </>
  );
}