import HomeRounded from '@mui/icons-material/HomeRounded';
import BatteryChargingFullRounded from '@mui/icons-material/BatteryChargingFullRounded';
import NorthEastRounded from '@mui/icons-material/NorthEastRounded';
import WbSunnyRounded from '@mui/icons-material/WbSunnyRounded';
import PauseCircleRounded from '@mui/icons-material/PauseCircleRounded';
import HelpOutlineRounded from '@mui/icons-material/HelpOutlineRounded';
import type { SvgIconComponent } from '@mui/icons-material';

export const ACTIONS: Record<string, { label: string; help: string; Icon: SvgIconComponent }> = {
  self_use: { label: 'Self-use', help: 'Solar to the home first, then the battery, then the grid.', Icon: HomeRounded },
  force_charge: { label: 'Charge from grid', help: 'The battery charges from the grid while power is cheap or free.', Icon: BatteryChargingFullRounded },
  force_discharge: { label: 'Export from battery', help: 'The battery powers the home and sells to the grid.', Icon: NorthEastRounded },
  export_priority: { label: 'Solar to grid first', help: 'Solar goes to the grid first, keeping battery room for later.', Icon: WbSunnyRounded },
  hold: { label: 'Hold battery', help: 'Keep the battery at its current level.', Icon: PauseCircleRounded },
};
export const action = (a?: string | null) => ACTIONS[a ?? ''] ?? { label: a || '–', help: '', Icon: HelpOutlineRounded };

const FOX: Record<string, string> = {
  SelfUse: 'Self Use',
  Feedin: 'Feed-in First',
  ForceCharge: 'Force Charge',
  ForceDischarge: 'Force Discharge',
  'SelfUse(min SoC)': 'Self Use, with Min SoC raised to the current level',
  Backup: 'Backup',
};
export const foxMode = (m?: string | null) => (m ? FOX[m] ?? m : '–');
