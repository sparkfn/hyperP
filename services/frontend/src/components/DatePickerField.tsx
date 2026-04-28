"use client";

import { useCallback, type ReactElement } from "react";

import { DatePicker } from "@mui/x-date-pickers/DatePicker";
import { LocalizationProvider } from "@mui/x-date-pickers/LocalizationProvider";
import { AdapterDayjs } from "@mui/x-date-pickers/AdapterDayjs";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import customParseFormat from "dayjs/plugin/customParseFormat";

dayjs.extend(customParseFormat);

const DISPLAY_FORMAT = "DD MMM YYYY";
const ISO_FORMAT = "YYYY-MM-DD";

interface DatePickerFieldProps {
  label: string;
  value: string;
  onChange: (isoDate: string) => void;
  disabled?: boolean;
  minDate?: Dayjs;
  maxDate?: Dayjs;
}

export default function DatePickerField({
  label,
  value,
  onChange,
  disabled = false,
  minDate,
  maxDate,
}: DatePickerFieldProps): ReactElement {
  const parsed = value ? dayjs(value, ISO_FORMAT, true) : null;

  const handleChange = useCallback(
    (newValue: Dayjs | null): void => {
      if (newValue && newValue.isValid()) {
        onChange(newValue.format(ISO_FORMAT));
      } else {
        onChange("");
      }
    },
    [onChange],
  );

  return (
    <LocalizationProvider dateAdapter={AdapterDayjs} adapterLocale="en-gb">
      <DatePicker
        label={label}
        value={parsed?.isValid() ? parsed : null}
        onChange={handleChange}
        disabled={disabled}
        minDate={minDate}
        maxDate={maxDate}
        format={DISPLAY_FORMAT}
        slotProps={{
          textField: {
            size: "small",
            fullWidth: true,
          },
          field: {
            clearable: true,
            onClear: () => onChange(""),
          },
        }}
      />
    </LocalizationProvider>
  );
}