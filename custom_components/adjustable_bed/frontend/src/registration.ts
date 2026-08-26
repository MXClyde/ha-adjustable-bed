import { PLATFORM } from "./discovery";
import type { AdjustableBedCardConfig, HomeAssistant } from "./types";

export interface CustomCardSuggestion {
  config: AdjustableBedCardConfig;
}

export interface CustomCardEntry {
  type: string;
  name: string;
  description: string;
  preview?: boolean;
  documentationURL?: string;
  getEntitySuggestion?: (
    hass: HomeAssistant,
    entityId: string,
  ) => CustomCardSuggestion | null;
}

export interface CustomCardsWindow {
  customCards?: CustomCardEntry[];
}

const CARD_TYPE = "adjustable-bed-card";

const cardEntry: CustomCardEntry = {
  type: CARD_TYPE,
  name: "Adjustable Bed Card",
  description: "Native control card for the Adjustable Bed integration.",
  preview: true,
  documentationURL: "https://github.com/kristofferR/ha-adjustable-bed",
  getEntitySuggestion: (hass, entityId) => {
    const entity = hass.entities[entityId];
    if (entity?.platform !== PLATFORM || !entity.device_id) return null;

    return {
      config: {
        type: `custom:${CARD_TYPE}`,
        device_id: entity.device_id,
      },
    };
  },
};

export function registerCustomCard(target: CustomCardsWindow): void {
  const cards = (target.customCards ??= []);
  const existingIndex = cards.findIndex((card) => card.type === CARD_TYPE);

  if (existingIndex === -1) {
    cards.push(cardEntry);
  } else {
    cards[existingIndex] = cardEntry;
  }
}

// Register before the custom elements are defined. A cache-busted second copy
// can then refresh picker metadata even if the browser rejects duplicate custom
// element definitions later in that module evaluation.
if (typeof window !== "undefined") {
  registerCustomCard(window as CustomCardsWindow);
}
