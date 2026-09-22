import { useState } from "react";
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
          <select value={top} onChange={(e) => setTop(Number(e.target.value))}>
            {topOptions.map((n) => (
              <option key={n} value={n}>
                топ-{n}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="chart-control">
        <span>линии фона</span>
        <input type="checkbox" checked={grid} onChange={(e) => setGrid(e.target.checked)} />
      </label>
    </div>
  );
}
