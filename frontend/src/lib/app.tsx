import { createContext, useContext } from 'react';

export type Role = 'admin' | 'member' | 'viewer';
export interface Session { user_id?: number; username: string; role: Role; csrf: string; mfa_enabled: boolean; version: string }
export const RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2 };

export interface AppState {
  session: Session;
  status: any | null;
  statusError: string | null;
  reloadStatus: () => Promise<any>;
  can: (role: Role) => boolean;
  openAlerts: () => void;
  signOut: () => void;
  refreshSession: () => Promise<void>;
}
export const AppCtx = createContext<AppState>(null as unknown as AppState);
export const useApp = () => useContext(AppCtx);
