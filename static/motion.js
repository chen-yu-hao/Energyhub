/* Shared selection motion: native inputs keep keyboard and screen-reader behavior. */
'use strict';
(() => {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const registered = new WeakSet();
  const previous = new WeakMap();
  let frame = 0;
  let animateNext = true;
  const groups = '.segments, .option-grid, .nav-links';

  function selectedElement(group) {
    if (group.classList.contains('nav-links')) return group.querySelector('[aria-current="page"]');
    const input = group.querySelector('input:checked:not(:disabled)');
    if (input) return group.classList.contains('option-grid') ? input.closest('.option-card') : input.nextElementSibling;
    return group.querySelector('[role="radio"][aria-checked="true"], .segment.selected');
  }

  function position(group, animate) {
    if (!group.isConnected || !group.getClientRects().length) return;
    const target = selectedElement(group);
    if (!target || !target.getClientRects().length) return;
    const box = group.getBoundingClientRect();
    const selected = target.getBoundingClientRect();
    const dimensions = [selected.left - box.left, selected.top - box.top, selected.width, selected.height];
    const key = dimensions.map(value => value.toFixed(2)).join(',');
    let indicator = group.querySelector(':scope > .selection-indicator');
    if (!indicator) {
      indicator = document.createElement('span');
      indicator.className = 'selection-indicator';
      indicator.setAttribute('aria-hidden', 'true');
      group.append(indicator);
      group.classList.add('has-selection-motion');
    }
    if (previous.get(group) === key) return;
    const immediate = !previous.has(group) || !animate || reduced.matches;
    indicator.style.transition = immediate ? 'none' : '';
    ['--selection-x', '--selection-y', '--selection-width', '--selection-height'].forEach((name, index) => indicator.style.setProperty(name, `${dimensions[index]}px`));
    indicator.style.opacity = '1';
    previous.set(group, key);
    if (immediate) {
      void indicator.offsetWidth;
      indicator.style.transition = '';
    }
  }

  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => refresh(false)) : null;
  function refresh(animate = true) {
    animateNext = frame ? animateNext && animate : animate;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      document.querySelectorAll(groups).forEach(group => {
        if (!registered.has(group)) { registered.add(group); observer?.observe(group); }
        position(group, animateNext);
      });
      animateNext = true;
    });
  }

  const visibility = new WeakMap();
  function reveal(element, shown, animate = true) {
    const old = visibility.get(element);
    visibility.set(element, shown);
    if (old === shown) return;
    const wasHidden = element.hidden;
    const currentHeight = element.getBoundingClientRect().height;
    const visual = getComputedStyle(element);
    const currentMargin = parseFloat(visual.marginTop) || 0;
    const currentPadding = [visual.paddingTop, visual.paddingBottom];
    const currentOpacity = visual.opacity;
    element.getAnimations().forEach(animation => animation.cancel());
    if (old === undefined || reduced.matches || !animate || !element.parentElement?.getClientRects().length) { element.hidden = !shown; refresh(false); return; }
    element.hidden = false;
    const natural = element.getBoundingClientRect().height;
    const naturalStyle = getComputedStyle(element);
    const margin = parseFloat(naturalStyle.marginTop) || 0;
    const from = { height: `${wasHidden ? 0 : currentHeight}px`, marginTop: `${wasHidden ? 0 : currentMargin}px`, paddingTop: wasHidden ? '0px' : currentPadding[0], paddingBottom: wasHidden ? '0px' : currentPadding[1], opacity: wasHidden ? 0 : currentOpacity, overflow: 'hidden' };
    const to = { height: `${shown ? natural : 0}px`, marginTop: `${shown ? margin : 0}px`, paddingTop: shown ? naturalStyle.paddingTop : '0px', paddingBottom: shown ? naturalStyle.paddingBottom : '0px', opacity: shown ? 1 : 0, overflow: 'hidden' };
    const animation = element.animate([from, to], { duration: 280, easing: 'cubic-bezier(.22,1,.36,1)', fill: 'both' });
    animation.onfinish = () => {
      if (visibility.get(element) !== shown) return;
      element.hidden = !shown;
      animation.cancel();
      refresh(false);
    };
  }

  function feedback(element) {
    if (reduced.matches) return;
    element.animate([{ opacity: .55, transform: 'translateY(2px)' }, { opacity: 1, transform: 'translateY(0)' }], { duration: 220, easing: 'cubic-bezier(.22,1,.36,1)' });
  }
  reduced.addEventListener?.('change', () => refresh(false));
  window.addEventListener('resize', () => refresh(false));
  document.fonts?.ready.then(() => refresh(false));
  window.EnergyhubMotion = { refresh, reveal, feedback };
})();
