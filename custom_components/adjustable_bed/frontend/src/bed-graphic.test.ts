import { expect, test } from "bun:test";
import { selectBedGraphicMotors } from "./bed-graphic";
import type { MotorEntity } from "./types";

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
