/**
 * L2 vector distance → a legible "match" score in [0,1].
 *
 * felix's embeddings are unit-normalised, so L2 distance `d` relates to cosine
 * similarity as `sim = 1 − d²/2`; clamp to [0,1] for display. Shared by the
 * evidence panel and the incident-library search so the two show the same bar.
 */
export function relevance(distance: number): number {
  return Math.max(0, Math.min(1, 1 - (distance * distance) / 2));
}

export function relevancePct(distance: number): number {
  return Math.round(relevance(distance) * 100);
}
