// Shared form fields for the system and vehicle wizards.
import TextField from '@mui/material/TextField';
import MenuItem from '@mui/material/MenuItem';
import Switch from '@mui/material/Switch';
import FormControlLabel from '@mui/material/FormControlLabel';
import InputAdornment from '@mui/material/InputAdornment';
import { HelpTip } from '../../components/common';

export const slug = (s: string, fallback = 'system') => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || fallback;

export function Num({ label, unit, value, onChange, help, required }: { label: string; unit: string; value: string; onChange: (v: string) => void; help?: string; required?: boolean }) {
  return (
    <TextField label={label} type="number" value={value} onChange={(e) => onChange(e.target.value)} required={required}
      slotProps={{ htmlInput: { step: 'any', min: 0 }, input: { endAdornment: <InputAdornment position="end">{unit}{help && <HelpTip label={label}>{help}</HelpTip>}</InputAdornment> } }} />
  );
}

export const pretty = (s: string) => s.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase()).replace(/\bkw\b/gi, 'kW').replace(/\bsoc\b/gi, 'SoC').replace(/\bpv\b/gi, 'PV').replace(/\burl\b/gi, 'URL');

export function SchemaField({ name, schema, value, onChange, required }: { required?: boolean; name: string; schema: any; value: any; onChange: (v: any) => void }) {
  const label = pretty(schema.title && schema.title !== name ? schema.title : name);
  const help = schema.description ? <InputAdornment position="end"><HelpTip label={label}>{schema.description}</HelpTip></InputAdornment> : undefined;
  const v = value ?? schema.default;
  if (schema.enum) {
    return <TextField select required={required} label={label} value={v ?? ''} onChange={(e) => onChange(schema.enum.find((option: any) => String(option) === String(e.target.value)))}>
      {schema.enum.map((option: string | number) => <MenuItem key={option} value={option}>{String(option)}</MenuItem>)}
    </TextField>;
  }
  if (schema.type === 'boolean') {
    return <FormControlLabel control={<Switch checked={!!v} onChange={(e) => onChange(e.target.checked)} />} label={label} />;
  }
  if (schema.type === 'number' || schema.type === 'integer') {
    return (
      <TextField required={required} label={label} type="number" value={v ?? ''} onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
        slotProps={{ htmlInput: { min: schema.minimum, max: schema.maximum, step: schema.type === 'integer' ? 1 : 'any' }, input: { endAdornment: help } }} />
    );
  }
  return <TextField required={required} label={label} value={v ?? ''} onChange={(e) => onChange(e.target.value)} slotProps={{ htmlInput: { maxLength: schema.maxLength }, input: { endAdornment: help } }} />;
}
