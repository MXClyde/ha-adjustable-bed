import type { BedEntities, HomeAssistant, MotorEntity } from "./types";

export interface BedGraphicMotors {
  upper: MotorEntity;
  lower: MotorEntity;
}

export function selectBedGraphicMotors(
  motors: readonly MotorEntity[],
): BedGraphicMotors | undefined {
  const upper = motors.find(
    (motor) => motor.key === "back" || motor.key === "head",
  );
  const lower = motors.find(
    (motor) => motor.key === "legs" || motor.key === "feet",
  );
  return upper && lower ? { upper, lower } : undefined;
}

export function bedHasGraphicFeedback(
  bed: BedEntities,
  hass: HomeAssistant,
): boolean {
  const degreeMotors = bed.motors.filter((motor) => {
    const entityId = motor.angle ?? motor.position;
    return hass.states[entityId ?? ""]?.attributes.unit_of_measurement === "°";
  });
  return selectBedGraphicMotors(degreeMotors) !== undefined;
}
