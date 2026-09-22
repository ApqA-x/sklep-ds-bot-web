import { useState } from "react";
import { Checkbox } from "primereact/checkbox";
import { Dropdown } from "primereact/dropdown";
import { CartesianGrid } from "recharts";

// Полоски фона по умолчанию полупрозрачные — не перетягивают внимание на себя.
export function Grid({ show = true }: { show?: boolean }) {
  if (!show) return null;
  return <CartesianGrid strokeDasharray="3 3" stroke="var(--overlay0)" strokeOpacity={0.18} />;
}

export function useGridPref(): [boolean, (v: boolean) => void] {
  return useState(true);
}

export function ChartControls({
  top,
  setTop,
  topOptions,
  grid,
  setGrid,
}: {
  top?: number;
  setTop?: (n: number) => void;
  topOptions?: number[];
  grid: boolean;
  setGrid: (v: boolean) => void;
}) {
  return (
    <div className="chart-controls">
      {setTop && topOptions && (
        <label className="chart-control">
          <span>в кадре</span>
          <Dropdown
            value={top}
            options={topOptions.map((n) => ({ label: `топ-${n}`, value: n }))}
            optionValue="value"
            onChange={(e) => setTop(e.value as number)}
          />
        </label>
      )}
      <label className="chart-control">
        <span>линии фона</span>
        <Checkbox checked={grid} onChange={() => setGrid(!grid)} />
      </label>
    </div>
  );
}
