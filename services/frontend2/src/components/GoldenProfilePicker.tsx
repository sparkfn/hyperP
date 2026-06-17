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
              <span className={styles.mergeFieldLabel}>{goldenProfileFieldLabel(fieldName)}</span>
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
                    <span className={styles.mergeFieldVal}>{choice.value}</span>
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
                    <span className={styles.mergeFieldVal}>{choice.value}</span>
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
