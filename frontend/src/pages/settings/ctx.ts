import { createContext, useContext } from 'react';

export interface SettingsDoc {
  settings: any;
  plugins: any[];
  configured_secrets: Record<string, string[]>;
  channel_kinds: any[];
  commissioned: any;
  last_tests: Record<string, string | null>;
}
export interface SettingsState {
  doc: SettingsDoc;
  reload: () => Promise<void>;
  /** PUT the whole settings object back (after a deep copy + edit). */
  save: (settings: any) => Promise<void>;
}
export const SettingsCtx = createContext<SettingsState | null>(null);
export const useSettings = () => useContext(SettingsCtx)!;
export const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));
