import { useMemo } from "react";
import { Calendar } from "primereact/calendar";

function toKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// PrimeReact Calendar с наружным контрактом "YYYY-MM-DD" (такой же ждёт бэкенд)
export function DateField({
  value,
  placeholder,
  onChange,
}: {
  value: string; // "YYYY-MM-DD" или ""
  placeholder: string;
  onChange: (v: string) => void;
}) {
  const parsed = useMemo(() => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const d = new Date(`${value}T00:00:00`);
    return Number.isNaN(d.getTime()) ? null : d;
  }, [value]);
  return (
    <Calendar
      className="datefield"
      value={parsed}
      placeholder={placeholder}
      dateFormat="dd.mm.yy"
      showIcon
      showButtonBar
      showOtherMonths
      onChange={(e) => {
        const v = e.value as Date | null;
        onChange(v ? toKey(v) : "");
      }}
    />
  );
}
