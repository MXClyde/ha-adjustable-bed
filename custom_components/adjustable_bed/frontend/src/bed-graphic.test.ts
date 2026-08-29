import { expect, test } from "bun:test";
import { bedHasGraphicFeedback, selectBedGraphicMotors } from "./bed-graphic-state";
import type { BedEntities, HomeAssistant, MotorEntity } from "./types";

const motor = (key: string): MotorEntity => ({ key, position: `number.bed_${key}` });

test("bed graphic requires both upper and lower feedback groups", () => {
  expect(selectBedGraphicMotors([motor("back")])).toBeUndefined();
  expect(selectBedGraphicMotors([motor("legs")])).toBeUndefined();
  expect(selectBedGraphicMotors([motor("back"), motor("head")])).toBeUndefined();
  expect(selectBedGraphicMotors([motor("legs"), motor("feet")])).toBeUndefined();
});

test("bed graphic maps one upper and one lower feedback axis", () => {
  const back = motor("back");
  const feet = motor("feet");

  expect(selectBedGraphicMotors([feet, back])).toEqual({ upper: back, lower: feet });
});

test("bed graphic availability requires degree feedback for both panels", () => {
  const bed = {
    motors: [motor("back"), motor("legs")],
  } as BedEntities;
  const hass = {
    states: {
      "number.bed_back": { attributes: { unit_of_measurement: "°" } },
      "number.bed_legs": { attributes: { unit_of_measurement: "%" } },
    },
  } as HomeAssistant;

  expect(bedHasGraphicFeedback(bed, hass)).toBe(false);
  hass.states["number.bed_legs"].attributes.unit_of_measurement = "°";
  expect(bedHasGraphicFeedback(bed, hass)).toBe(true);
});
