import { describe, expect, test } from "bun:test";
import {
  type CustomCardsWindow,
  registerCustomCard,
} from "./registration";
import type { HomeAssistant } from "./types";

function hassWithEntity(
  platform: string,
  deviceId: string | undefined,
): HomeAssistant {
  return {
    entities: {
      "cover.bed_back": {
        entity_id: "cover.bed_back",
        device_id: deviceId,
        platform,
      },
    },
    states: {},
    devices: {},
    locale: { language: "en" },
    language: "en",
    themes: {},
    callService: async () => undefined,
  };
}

describe("custom card registration", () => {
  test("registers picker metadata once", () => {
    const target: CustomCardsWindow = {};

    registerCustomCard(target);
    registerCustomCard(target);

    expect(target.customCards).toHaveLength(1);
    expect(target.customCards?.[0]).toMatchObject({
      type: "adjustable-bed-card",
      name: "Adjustable Bed Card",
      preview: true,
    });
  });

  test("replaces stale metadata from an older bundle", () => {
    const target: CustomCardsWindow = {
      customCards: [
        {
          type: "adjustable-bed-card",
          name: "Old card metadata",
          description: "Old description",
        },
      ],
    };

    registerCustomCard(target);

    expect(target.customCards).toHaveLength(1);
    expect(target.customCards?.[0].name).toBe("Adjustable Bed Card");
    expect(target.customCards?.[0].getEntitySuggestion).toBeFunction();
  });

  test("suggests the card for Adjustable Bed entities", () => {
    const target: CustomCardsWindow = {};
    registerCustomCard(target);

    const suggestion = target.customCards?.[0].getEntitySuggestion?.(
      hassWithEntity("adjustable_bed", "bed-device"),
      "cover.bed_back",
    );

    expect(suggestion).toEqual({
      config: {
        type: "custom:adjustable-bed-card",
        device_id: "bed-device",
      },
    });
  });

  test("does not suggest the card for unrelated or device-less entities", () => {
    const target: CustomCardsWindow = {};
    registerCustomCard(target);
    const suggest = target.customCards?.[0].getEntitySuggestion;

    expect(
      suggest?.(hassWithEntity("light", "bed-device"), "cover.bed_back"),
    ).toBeNull();
    expect(
      suggest?.(
        hassWithEntity("adjustable_bed", undefined),
        "cover.bed_back",
      ),
    ).toBeNull();
  });
});
