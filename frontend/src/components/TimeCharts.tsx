// Stacked plan charts sharing one continuous time axis and one synced cursor.
// The built-in floating tooltip is off: each chart's legend (directly above it)
// shows the values at the cursor instead, so all four read out together.
import { useMemo, useState, type ReactNode } from 'react';
import Box from '@mui/material/Box';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import Button from '@mui/material/Button';
import Collapse from '@mui/material/Collapse';
import Table from '@mui/material/Table';
import TableHead from '@mui/material/TableHead';
import TableBody from '@mui/material/TableBody';
import TableRow from '@mui/material/TableRow';
import TableCell from '@mui/material/TableCell';
import { LineChart } from '@mui/x-charts/LineChart';
import { ChartsReferenceLine } from '@mui/x-charts/ChartsReferenceLine';
import type { AxisItemIdentifier } from '@mui/x-charts/models';
import * as f from '../lib/fmt';

export interface LegendItem {
  label: string;
  color: string;
  kind: 'line' | 'dashed' | 'band' | 'bar' | 'step';
  value: (i: number) => string;
}

const Y_WIDTH = 44;
export const MARGIN = { left: 4, right: 12, top: 10, bottom: 2 };

function Swatch({ kind, color }: { kind: LegendItem['kind']; color: string }) {
  const common = { flexShrink: 0, display: 'inline-block' } as const;
  if (kind === 'band') return <Box component="span" aria-hidden sx={{ ...common, width: 16, height: 10, borderRadius: 0.5, bgcolor: color, opacity: 0.3 }} />;
  if (kind === 'bar') return <Box component="span" aria-hidden sx={{ ...common, width: 10, height: 10, borderRadius: 0.5, bgcolor: color }} />;
  return <Box component="span" aria-hidden sx={{ ...common, width: 16, height: 0, borderTop: 3, borderTopStyle: kind === 'dashed' ? 'dashed' : 'solid', borderColor: color, borderRadius: 1 }} />;
}

export function ChartBlock({ id, title, unit, legend, index, children, table }: {
  id: string; title: string; unit: string; legend: LegendItem[]; index: number; children: ReactNode;
  table: { columns: string[]; rows: (string | number)[][] };
}) {
  const [showTable, setShowTable] = useState(false);
  return (
    <Box component="figure" aria-labelledby={`${id}-title`} sx={{ m: 0, py: 1.5, '& + &': { borderTop: 1, borderColor: 'divider' } }}>
      <Stack direction="row" sx={{ alignItems: 'baseline', justifyContent: 'space-between', flexWrap: 'wrap', gap: 1 }}>
        <Typography variant="h3" component="figcaption" id={`${id}-title`}>
          {title} <Typography component="span" variant="caption" color="text.secondary">({unit})</Typography>
        </Typography>
        <Button size="small" onClick={() => setShowTable(!showTable)} aria-expanded={showTable} aria-controls={`${id}-table`} sx={{ minHeight: 0, py: 0.25 }}>
          {showTable ? 'Hide table' : 'Data table'}
        </Button>
      </Stack>
      <Box component="ul" aria-label={`${title} legend`} sx={{ listStyle: 'none', m: 0, mt: 0.75, p: 0, display: 'flex', flexWrap: 'wrap', columnGap: 2.5, rowGap: 0.5 }}>
        {legend.map((l) => (
          <Box component="li" key={l.label} sx={{ display: 'flex', alignItems: 'center', gap: 0.75, fontSize: '0.8125rem' }}>
            <Swatch kind={l.kind} color={l.color} />
            <span>{l.label}</span>
            <Box component="span" sx={{ fontWeight: 700, fontVariantNumeric: 'tabular-nums', minWidth: '4.5em' }}>{l.value(index)}</Box>
          </Box>
        ))}
      </Box>
      {children}
      <Collapse in={showTable} unmountOnExit>
        <Box id={`${id}-table`} role="region" aria-label={`${title} data`} tabIndex={0}
          sx={{ maxHeight: 320, overflow: 'auto', mt: 1, border: 1, borderColor: 'divider', borderRadius: 2 }}>
          <Table size="small" stickyHeader>
            <caption style={{ captionSide: 'top', textAlign: 'left', padding: '8px 12px' }}>{title}, {unit}</caption>
            <TableHead><TableRow>{table.columns.map((c) => <TableCell key={c} scope="col">{c}</TableCell>)}</TableRow></TableHead>
            <TableBody>
              {table.rows.map((r, i) => (
                <TableRow key={i}>{r.map((c, j) => j === 0 ? <TableCell key={j} component="th" scope="row" sx={{ whiteSpace: 'nowrap' }}>{c}</TableCell> : <TableCell key={j}>{c}</TableCell>)}</TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      </Collapse>
    </Box>
  );
}

export function useTimeAxis(ts: number[], now: number, compact: boolean, min: number, max: number) {
  return useMemo(() => {
    const boundaries: { x: number; label: string }[] = [];
    const ticks: number[] = [];
    let previous = '';
    for (let time = Math.ceil(min / 900000) * 900000; time <= max; time += 900000) {
      const local = f.parts(time);
      const day = `${local.year}-${local.month}-${local.day}`;
      if (previous && day !== previous) boundaries.push({ x: time, label: f.dayLabel(time, now) });
      previous = day;
      if (local.minute === '00' && Number(local.hour) % (compact ? 6 : 3) === 0) ticks.push(time);
    }
    boundaries.push({ x: now, label: 'Now' });
    const axis = {
      id: 't', scaleType: 'linear' as const, data: ts, min, max, tickInterval: ticks,
      valueFormatter: (v: number) => f.parts(v).hour === '00' ? f.dayLabel(v, now) : f.hourTick(v),
      tickLabelStyle: { fontSize: 11 }, height: 24,
    };
    return { axis, boundaries };
  }, [ts, now, compact, min, max]);
}

type Common = {
  height: number; xAxis: any; boundaries: { x: number; label: string }[]; hi: number | null; setHi: (i: number | null) => void;
  yFormat: (v: number) => string; desc: string; muted: string; yMin?: number; yMax?: number;
};

function shared({ height, xAxis, hi, setHi, yFormat, yMin, yMax, desc }: Common) {
  return {
    height,
    xAxis: [xAxis],
    yAxis: [{ id: 'y', width: Y_WIDTH, min: yMin, max: yMax, valueFormatter: yFormat, tickNumber: 4, tickLabelStyle: { fontSize: 11 } }],
    margin: MARGIN,
    hideLegend: true,
    grid: { horizontal: true },
    highlightedAxis: (hi == null ? [] : [{ axisId: 't', dataIndex: hi }]) as AxisItemIdentifier[],
    onHighlightedAxisChange: (items: AxisItemIdentifier[]) => setHi(items.find((x) => x.axisId === 't')?.dataIndex ?? null),
    axisHighlight: { x: 'line' as const },
    slotProps: { tooltip: { trigger: 'none' as const } },
    skipAnimation: true,
    desc,
  };
}

function Boundaries({ boundaries, muted }: { boundaries: Common['boundaries']; muted: string }) {
  return (
    <>
      {boundaries.map((b) => (
        <ChartsReferenceLine key={b.x} x={b.x} lineStyle={{ stroke: muted, strokeDasharray: '2 3', strokeWidth: b.label === 'Now' ? 1.75 : 1 }} />
      ))}
    </>
  );
}

export function LineTimeChart(props: Common & { series: any[]; sx?: object }) {
  return (
    <LineChart {...shared(props)} series={props.series} sx={props.sx}>
      <Boundaries boundaries={props.boundaries} muted={props.muted} />
    </LineChart>
  );
}
