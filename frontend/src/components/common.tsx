import React, { createContext, useCallback, useContext, useId, useRef, useState, type ReactNode } from 'react';
import Box from '@mui/material/Box';
import Paper from '@mui/material/Paper';
import Popper from '@mui/material/Popper';
import IconButton from '@mui/material/IconButton';
import ClickAwayListener from '@mui/material/ClickAwayListener';
import Typography from '@mui/material/Typography';
import Stack from '@mui/material/Stack';
import Skeleton from '@mui/material/Skeleton';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import Snackbar from '@mui/material/Snackbar';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import Fade from '@mui/material/Fade';
import HelpOutlineRounded from '@mui/icons-material/HelpOutlineRounded';
import type { SxProps, Theme } from '@mui/material/styles';

/* ------------------------------------------------------------------ toggletip
 * A "?" button that reveals a short explanation. Opens on click/Enter (and on
 * hover for pointer users); Esc, click-away or a second press closes it.
 * aria-expanded + aria-controls on the trigger, role="tooltip" on the hint. */
export function HelpTip({ title, children, label }: { title?: string; children: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false);
  const [byHover, setByHover] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const id = useId();
  const close = () => {
    setOpen(false);
    setByHover(false);
  };
  return (
    <Box component="span" sx={{ display: 'inline-flex', verticalAlign: 'middle' }} onKeyDown={(e) => {
      if (e.key === 'Escape' && open) {
        e.stopPropagation();
        close();
        anchor.current?.focus();
      }
    }}>
      <IconButton
        ref={anchor}
        size="small"
        aria-label={`Help: ${label ?? title ?? 'more information'}`}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          if (open && byHover) setByHover(false);
          else setOpen((o) => !o);
        }}
        onMouseEnter={() => {
          if (!open) {
            setOpen(true);
            setByHover(true);
          }
        }}
        onMouseLeave={() => {
          if (byHover) close();
        }}
        sx={{ p: 0.25, ml: 0.25, color: 'text.secondary' }}
      >
        <HelpOutlineRounded sx={{ fontSize: 18 }} />
      </IconButton>
      {/* Portalled so dialogs and scroll containers can't clip it; aria-controls links it to the trigger. */}
      <Popper open={open} anchorEl={anchor.current} placement="bottom-start" transition sx={{ zIndex: 1600 }}
        modifiers={[{ name: 'preventOverflow', options: { padding: 8 } }, { name: 'flip', options: { padding: 8 } }]}>
        {({ TransitionProps }) => (
          <Fade {...TransitionProps} timeout={120}>
            <div>
              <ClickAwayListener onClickAway={(e) => {
                if (!anchor.current?.contains(e.target as Node)) close();
              }}>
                <Paper id={id} role="tooltip" elevation={6}
                  sx={{ p: 1.5, maxWidth: 320, mt: 0.5, borderRadius: 2, bgcolor: 'background.paper', border: 1, borderColor: 'divider' }}>
                  {title && <Typography variant="subtitle2" component="p" sx={{ mb: 0.5 }}>{title}</Typography>}
                  <Typography variant="body2" component="div" color="text.secondary">{children}</Typography>
                </Paper>
              </ClickAwayListener>
            </div>
          </Fade>
        )}
      </Popper>
    </Box>
  );
}

/* --------------------------------------------------------------- page bits */
export function PageHeader({ title, sub, actions }: { title: string; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ mb: 2.5, alignItems: { sm: 'flex-end' }, justifyContent: 'space-between' }}>
      <Box>
        <Typography variant="h1">{title}</Typography>
        {sub && <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{sub}</Typography>}
      </Box>
      {actions && <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>{actions}</Stack>}
    </Stack>
  );
}

export function Section({ title, titleId, action, children, sx, help }: {
  title?: ReactNode; titleId?: string; action?: ReactNode; children: ReactNode; sx?: SxProps<Theme>; help?: ReactNode;
}) {
  const fallback = useId();
  const hid = titleId ?? fallback;
  return (
    <Card component="section" aria-labelledby={title ? hid : undefined} sx={{ p: { xs: 2, sm: 2.5 }, ...sx as object }}>
      {(title || action) && (
        <Stack direction="row" sx={{ alignItems: 'center', justifyContent: 'space-between', mb: 1.5, gap: 1, minHeight: 32 }}>
          <Typography variant="h2" id={hid} tabIndex={-1} sx={{ display: 'flex', alignItems: 'center', outline: 'none' }}>
            {title}
            {help}
          </Typography>
          {action}
        </Stack>
      )}
      {children}
    </Card>
  );
}

export function LoadingBlock({ lines = 3, height }: { lines?: number; height?: number }) {
  return (
    <Box aria-busy="true" aria-label="Loading">
      {height ? <Skeleton variant="rounded" height={height} /> : Array.from({ length: lines }, (_, i) => <Skeleton key={i} height={28} width={`${90 - i * 12}%`} />)}
    </Box>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Alert severity="error" action={onRetry && <Button color="inherit" size="small" onClick={onRetry}>Try again</Button>}>
      {message}
    </Alert>
  );
}

export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <Box sx={{ py: 3, px: 2, textAlign: 'center', color: 'text.secondary', border: 1, borderStyle: 'dashed', borderColor: 'divider', borderRadius: 3 }}>
      <Typography variant="body2">{children}</Typography>
      {action && <Box sx={{ mt: 1.5 }}>{action}</Box>}
    </Box>
  );
}

/** Label + value pair list (dense, two columns). */
export function Facts({ rows }: { rows: ([ReactNode, ReactNode] | null | false)[] }) {
  return (
    <Box component="dl" sx={{ display: 'grid', gridTemplateColumns: 'minmax(8rem, max-content) 1fr', columnGap: 2, rowGap: 0.75, m: 0, fontSize: '0.875rem' }}>
      {rows.filter(Boolean).map((r, i) => {
        const [k, v] = r as [ReactNode, ReactNode];
        return [
          <Box component="dt" key={`k${i}`} sx={{ color: 'text.secondary' }}>{k}</Box>,
          <Box component="dd" key={`v${i}`} sx={{ m: 0 }}>{v}</Box>,
        ];
      })}
    </Box>
  );
}

/* ------------------------------------------------------------------ toasts */
type Toast = { message: string; severity: 'success' | 'info' | 'warning' | 'error'; key: number };
const ToastCtx = createContext<(message: string, severity?: Toast['severity']) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<Toast | null>(null);
  const notify = useCallback((message: string, severity: Toast['severity'] = 'success') => setToast({ message, severity, key: Date.now() }), []);
  return (
    <ToastCtx.Provider value={notify}>
      {children}
      <Snackbar key={toast?.key} open={!!toast} autoHideDuration={5000} onClose={(_, r) => r !== 'clickaway' && setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }} sx={{ bottom: { xs: 88, md: 24 } }}>
        <Alert variant="filled" severity={toast?.severity ?? 'success'} onClose={() => setToast(null)} sx={{ minWidth: 280 }}>
          {toast?.message}
        </Alert>
      </Snackbar>
    </ToastCtx.Provider>
  );
}

/* ------------------------------------------------------------ confirmation */
export function ConfirmDialog({ open, title, children, confirm, danger, busy, onConfirm, onClose, error, disabled, cancelLabel = 'Keep' }: {
  open: boolean; title: string; children?: ReactNode; confirm: string; danger?: boolean; busy?: boolean; error?: string | null; disabled?: boolean; cancelLabel?: string;
  onConfirm: () => void; onClose: () => void;
}) {
  const id = useId();
  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} aria-labelledby={id} maxWidth="xs" fullWidth>
      <DialogTitle id={id}>{title}</DialogTitle>
      <DialogContent>
        {children}
        {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy} autoFocus>{cancelLabel}</Button>
        <Button variant="contained" color={danger ? 'error' : 'primary'} onClick={onConfirm} disabled={busy || disabled}>{confirm}</Button>
      </DialogActions>
    </Dialog>
  );
}

/* --------------------------------------------------------- copyable value */
export function CopyField({ label, value, multiline, helper }: { label: string; value: string; multiline?: boolean; helper?: ReactNode }) {
  const toast = useToast();
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      toast(`${label} copied.`);
    } catch {
      toast('Couldn’t copy. Select the text and copy it by hand.', 'error');
    }
  }
  return (
    <Stack direction="row" spacing={1} sx={{ alignItems: 'flex-start' }}>
      <Box component="label" sx={{ flex: 1, minWidth: 0, display: 'block' }}>
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, display: 'block', mb: 0.5 }}>{label}</Typography>
        <Box component={multiline ? 'textarea' : 'input'} readOnly value={value} rows={multiline ? Math.min(8, value.split('\n').length) : undefined}
          onFocus={(e: React.FocusEvent<HTMLInputElement>) => e.currentTarget.select()}
          sx={{
            width: '100%', font: '0.8125rem/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', color: 'text.primary', bgcolor: 'action.hover',
            border: 1, borderColor: 'divider', borderRadius: 2, px: 1.25, py: 1, resize: 'none', overflow: 'auto', whiteSpace: multiline ? 'pre' : undefined,
            '&:focus-visible': { outline: '2px solid', outlineColor: 'secondary.main' },
          }} />
        {helper && <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>{helper}</Typography>}
      </Box>
      <Button size="small" variant="outlined" onClick={copy} sx={{ mt: 2.75, flexShrink: 0 }} aria-label={`Copy ${label}`}>Copy</Button>
    </Stack>
  );
}
