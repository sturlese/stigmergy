const cache = new Map<string, string>();

/** A CSS color (including oklch and custom properties) as rgb(), for renderers that parse only sRGB. */
export function toRgb(color: string, alpha = 1): string {
  const cacheKey = `${color}|${alpha}`;
  const hit = cache.get(cacheKey);
  if (hit) return hit;
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = 1;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return color;
  context.fillStyle = color;
  context.fillRect(0, 0, 1, 1);
  const [r, g, b] = context.getImageData(0, 0, 1, 1).data;
  const value = alpha < 1 ? `rgba(${r},${g},${b},${alpha})` : `rgb(${r},${g},${b})`;
  cache.set(cacheKey, value);
  return value;
}

/** A solid sRGB color between two CSS colors; WebGL renderers blend alpha poorly over the page. */
export function mix(color: string, base: string, percent: number): string {
  return toRgb(`color-mix(in oklab, ${color} ${percent}%, ${base})`);
}

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function clearColorCache() {
  cache.clear();
}
