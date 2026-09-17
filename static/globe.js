/* Cobe WebGL globe: adapted from the supplied component for this vanilla app. */
(() => {
  const canvas = document.getElementById('discovery-globe');
  if (!canvas) return;

  const markers = [
    { location: [51.1694, 71.4491], size: 0.09 }, // Astana
    { location: [40.7128, -74.006], size: 0.06 },
    { location: [51.5072, -0.1276], size: 0.05 },
    { location: [25.2048, 55.2708], size: 0.055 },
    { location: [1.3521, 103.8198], size: 0.055 },
    { location: [35.6762, 139.6503], size: 0.06 },
    { location: [-33.8688, 151.2093], size: 0.05 },
    { location: [-23.5505, -46.6333], size: 0.05 },
    { location: [19.4326, -99.1332], size: 0.05 },
  ];
  let phi = -0.42, width = 0, dragStart = null, dragDelta = 0, globe, rendered = false;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');

  function resize() { width = canvas.offsetWidth; }
  function release() {
    dragStart = null;
    canvas.style.cursor = 'grab';
  }
  function move(event) {
    if (dragStart === null) return;
    dragDelta = event.clientX - dragStart;
  }
  async function mount() {
    // Cobe is the exact globe renderer requested in the supplied prompt.
    const module = await import('./vendor/cobe.bundle.mjs');
    const createGlobe = module.default;
    resize();
    globe = createGlobe(canvas, {
      width: width * 2,
      height: width * 2,
      devicePixelRatio: Math.min(devicePixelRatio || 1, 2),
      phi,
      theta: 0.28,
      dark: 0,
      diffuse: 0.45,
      mapSamples: 16000,
      mapBrightness: 1.18,
      baseColor: [1, 1, 1],
      markerColor: [251 / 255, 100 / 255, 21 / 255],
      glowColor: [1, 1, 1],
      markers,
      onRender(state) {
        if (!rendered) {
          rendered = true;
          document.getElementById('landing')?.classList.add('cobe-ready');
        }
        if (dragStart === null && !reduced.matches) phi += 0.0038;
        state.phi = phi + dragDelta / 200;
        state.width = width * 2;
        state.height = width * 2;
      },
    });
    canvas.style.opacity = '1';
    canvas.style.cursor = 'grab';
  }
  canvas.addEventListener('pointerdown', event => {
    dragStart = event.clientX - dragDelta;
    canvas.setPointerCapture?.(event.pointerId);
    canvas.style.cursor = 'grabbing';
  });
  canvas.addEventListener('pointermove', move);
  for (const event of ['pointerup', 'pointercancel', 'pointerleave']) canvas.addEventListener(event, release);
  new ResizeObserver(resize).observe(canvas);
  mount().catch(() => {
    // Keep the page usable if a visitor is offline; the real Cobe globe is used whenever its module loads.
    canvas.classList.add('globe-unavailable');
  });
  window.setTimeout(() => {
    if (!rendered) canvas.classList.add('globe-unavailable');
  }, 1400);
  window.addEventListener('beforeunload', () => globe?.destroy());
})();
