"use client";

import type { ReactElement } from "react";

import {
  goldenProfileFieldLabel,
  type GoldenProfileChoice,
  type GoldenProfileFieldName,
} from "@/lib/golden-profile-choices";
import styles from "@/app/review/review.module.css";

const FIELD_ORDER: readonly GoldenProfileFieldName[] = [
  "preferred_full_name",
  "preferred_dob",
  "preferred_phone",
  "preferred_email",
  "preferred_address",
  "preferred_nric",
];

const svgProps = {
  width: 14,
  height: 14,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

function FieldIcon({ fieldName }: { fieldName: GoldenProfileFieldName }): ReactElement {
  switch (fieldName) {
    case "preferred_full_name":
      return <svg {...svgProps}><circle cx="12" cy="8" r="4" /><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" /></svg>;
    case "preferred_dob":
      return <svg {...svgProps}><rect width="18" height="18" x="3" y="4" rx="2" /><line x1="16" x2="16" y1="2" y2="6" /><line x1="8" x2="8" y1="2" y2="6" /><line x1="3" x2="21" y1="10" y2="10" /></svg>;
    case "preferred_phone":
      return <svg {...svgProps}><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12 19.79 19.79 0 0 1 1.61 3.41 2 2 0 0 1 3.6 1h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.6a16 16 0 0 0 6 6l.96-.96a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z" /></svg>;
    case "preferred_email":
      return <svg {...svgProps}><rect width="20" height="16" x="2" y="4" rx="2" /><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" /></svg>;
    case "preferred_address":
      return <svg {...svgProps}><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" /><circle cx="12" cy="10" r="3" /></svg>;
    case "preferred_nric":
      return <svg {...svgProps}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>;
  }
}

interface GoldenProfilePickerProps {
  choices: readonly GoldenProfileChoice[];
  selectedChoiceKeys: Partial<Record<GoldenProfileFieldName, string>>;
  leftPersonId: string;
  rightPersonId: string;
  onChange: (fieldName: GoldenProfileFieldName, choiceKey: string) => void;
  disabled: boolean;
}

export default function GoldenProfilePicker({
  choices,
  selectedChoiceKeys,
  leftPersonId,
  rightPersonId,
  onChange,
  disabled,
}: GoldenProfilePickerProps): ReactElement {
  return (
    <div className={styles.mergeCompare}>
      <div className={styles.mergeFieldHeader}>
        <span />
        <span className={styles.mergeFieldHeaderRole}>Left person</span>
        <span className={styles.mergeFieldHeaderRole}>Right person</span>
      </div>
      <div className={styles.mergeFieldList}>
        {FIELD_ORDER.map((fieldName) => {
          const fieldChoices = choices.filter((choice) => choice.fieldName === fieldName);
          if (fieldChoices.length === 0) return null;
          const leftChoices = fieldChoices.filter((choice) => choice.personId === leftPersonId);
          const rightChoices = fieldChoices.filter((choice) => choice.personId === rightPersonId);
          const selectedKey = selectedChoiceKeys[fieldName] ?? "";
          return (
            <div key={fieldName} className={styles.mergeFieldRow}>
              <span className={styles.mergeFieldLabel} title={goldenProfileFieldLabel(fieldName)}>
                <FieldIcon fieldName={fieldName} />
              </span>
              <div className={styles.mergeFieldChoiceStack}>
                {leftChoices.length > 0 ? leftChoices.map((choice) => (
                  <label key={choice.key} className={styles.mergeFieldOption}>
                    <input
                      type="radio"
                      name={`golden-${fieldName}`}
                      checked={selectedKey === choice.key}
                      disabled={disabled}
                      onChange={() => onChange(fieldName, choice.key)}
                    />
                    <span className={styles.mergeFieldVal}>{choice.displayValue}</span>
                  </label>
                )) : <span className={styles.mergeFieldEmpty}>—</span>}
              </div>
              <div className={styles.mergeFieldChoiceStack}>
                {rightChoices.length > 0 ? rightChoices.map((choice) => (
                  <label key={choice.key} className={styles.mergeFieldOption}>
                    <input
                      type="radio"
                      name={`golden-${fieldName}`}
                      checked={selectedKey === choice.key}
                      disabled={disabled}
                      onChange={() => onChange(fieldName, choice.key)}
                    />
                    <span className={styles.mergeFieldVal}>{choice.displayValue}</span>
                  </label>
                )) : <span className={styles.mergeFieldEmpty}>—</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
