// The popover owns opening/dismissal. Inspection mode and native-pixel scroll
// position are shared within the rendered album/review, never persisted.
(() => {
    const openers = new WeakMap();
    const peers = viewer => Array.from(document.querySelectorAll('.art-full'))
        .filter(other => other.dataset.artGallery === viewer.dataset.artGallery);
    const dimensions = image => [
        image.naturalWidth || Number(image.getAttribute('width')),
        image.naturalHeight || Number(image.getAttribute('height')),
    ];

    function render(viewer) {
        if (!viewer.matches(':popover-open')) return;
        const stage = viewer.querySelector('.art-full__stage');
        const image = viewer.querySelector('img');
        const [width, height] = dimensions(image);
        if (!width || !height) return; // The image's load event will measure it.
        const native = viewer.querySelector('.art-full__mode').value === 'native';
        const scale = native ? 1 : Math.min(1,
            stage.clientWidth / width, stage.clientHeight / height);
        image.style.width = `${width * scale}px`;
        image.style.height = `${height * scale}px`;
        viewer.querySelector('output').textContent = native
            ? '100% · 1 image pixel per CSS pixel'
            : `${(scale * 100).toFixed(1)}% · fit to viewer`;
    }

    // A CAA response replaces the album/review thumbnails AND popovers. Carry the
    // live inspection through that swap, not through the request: the user may
    // switch images, change scale, or dismiss the viewer while it is in flight.
    const inspections = new WeakMap();
    document.addEventListener('htmx:beforeSwap', event => {
        const target = event.detail.target;
        if (!target.matches('#album-artwork, .assignment-artwork') ||
            event.defaultPrevented || !event.detail.shouldSwap) return;
        const viewer = target.querySelector('.art-full:popover-open');
        if (!viewer) return;
        const stage = viewer.querySelector('.art-full__stage');
        const focus = ['.art-full__stage', '.art-full__mode', '.art-full__image-choice', '[autofocus]']
            .find(selector => viewer.querySelector(selector) === document.activeElement);
        inspections.set(target, {
            id: viewer.id,
            source: viewer.querySelector('img').getAttribute('src'),
            mode: viewer.querySelector('.art-full__mode').value,
            position: [stage.scrollLeft, stage.scrollTop],
            opener: openers.get(viewer)?.getAttribute('popovertarget'),
            focus,
        });
    });
    // HTMX runs inserted autofocus controls during settling. Restore focus
    // afterwards so the viewer's Close button cannot steal it back.
    document.addEventListener('htmx:afterSettle', event => {
        const target = event.detail.target;
        const inspection = inspections.get(target);
        if (!inspection) return;
        inspections.delete(target);
        const viewer = document.getElementById(inspection.id);
        // Do not reopen a different image if the refreshed gallery lost this one.
        if (!viewer || !target.contains(viewer) ||
            viewer.querySelector('img').getAttribute('src') !== inspection.source) return;
        const opener = Array.from(target.querySelectorAll('button[popovertarget]'))
            .find(button => button.getAttribute('popovertarget') === inspection.opener);
        if (opener) openers.set(viewer, opener);
        viewer.querySelector('.art-full__mode').value = inspection.mode;
        viewer.showPopover();
        render(viewer);
        viewer.querySelector('.art-full__stage').scrollTo(...inspection.position);
        viewer.querySelector(inspection.focus || '[autofocus]').focus({preventScroll: true});
    });

    const renderOpen = () => document.querySelectorAll('.art-full:popover-open').forEach(render);
    document.addEventListener('click', event => {
        // Safari does not focus a button on pointer activation. Remember the
        // actual trigger so Escape can return to it after switching images.
        const trigger = event.target.closest('button[popovertarget]');
        const viewer = trigger && document.getElementById(trigger.getAttribute('popovertarget'));
        if (viewer?.matches('.art-full')) {
            openers.set(viewer, trigger);
            trigger.focus({preventScroll: true});
        }
    }, true);
    document.addEventListener('toggle', event => {
        const viewer = event.target;
        if (!viewer.matches('.art-full')) return;
        if (event.newState === 'open') {
            render(viewer);
        } else if (!peers(viewer).some(other => other.matches(':popover-open'))
                   && (document.activeElement === document.body || viewer.contains(document.activeElement))) {
            openers.get(viewer)?.focus();
        }
    }, true);
    document.addEventListener('change', event => {
        const control = event.target;
        const viewer = control.closest('.art-full');
        if (!viewer) return;
        if (control.matches('.art-full__mode')) {
            render(viewer);
            viewer.querySelector('.art-full__stage').scrollTo(0, 0);
        } else if (control.matches('.art-full__image-choice')) {
            const next = document.getElementById(control.value);
            if (!next || !peers(viewer).includes(next) || next === viewer) return;
            const stage = viewer.querySelector('.art-full__stage');
            const position = [stage.scrollLeft, stage.scrollTop];
            const mode = viewer.querySelector('.art-full__mode').value;
            // Reset the old select: reopening its thumbnail must name its image.
            control.value = viewer.id;
            next.querySelector('.art-full__mode').value = mode;
            const opener = openers.get(viewer);
            viewer.hidePopover();
            next.showPopover();
            openers.set(next, opener);
            render(next);
            next.querySelector('.art-full__stage').scrollTo(...(mode === 'native' ? position : [0, 0]));
            next.querySelector('[autofocus]').focus({preventScroll: true});
        }
    });
    document.addEventListener('load', event => {
        if (event.target instanceof HTMLImageElement && event.target.closest('.art-full')) renderOpen();
    }, true);
    window.addEventListener('resize', renderOpen);

    // The Artwork section's previous/next row controls (#659). They move the
    // check between the rows' radios; the stylesheet does the rest, so this
    // makes no request. Stops at either end rather than wrapping: the picker
    // jumping from the last row back to the first reads as a page that moved.
    //
    // The same for the picker's images (‹ ›, #659), stepping over any image
    // that Front only is hiding — so an image is only ever stepped TO if it
    // can be seen.
    const shownImages = form => {
        const frontOnly = form.querySelector('.art-pick__front-only input[type="checkbox"]');
        return Array.from(form.querySelectorAll('input[name="candidate"]')).filter(radio =>
            !(frontOnly && frontOnly.checked && radio.closest('.art-pick__slide').dataset.front === 'false'));
    };
    document.addEventListener('click', event => {
        const step = event.target.closest('[data-art-step]');
        if (!step) return;
        const radios = step.dataset.artGroup === 'candidate'
            ? shownImages(step.form)
            : Array.from(step.form.querySelectorAll('input[name="row"]'));
        const at = radios.findIndex(radio => radio.checked);
        const next = radios[at + Number(step.dataset.artStep)];
        const more = name => step.form.querySelector(`.art-pick__more[data-more="${name}"]`);
        // A click rather than setting `checked`, so the row is selected exactly
        // as a click on it would select it — events and all.
        if (next) {
            next.click();
            // Arrived at the last image: list the next release now, so it is
            // there by the time › is pressed (#659).
            if (step.dataset.artGroup === 'candidate' && next === radios[radios.length - 1]) {
                more('ahead')?.click();
            }
        } else if (step.dataset.artGroup === 'candidate' && Number(step.dataset.artStep) > 0) {
            // Past the last image: on to the next release, if there may be one.
            more('step')?.click();
        }
    });
    // Ticking Front only while a back cover is shown would leave nothing
    // shown: move to the first image that can be.
    document.addEventListener('change', event => {
        if (!event.target.matches('.art-pick__front-only input[type="checkbox"]')) return;
        const radios = shownImages(event.target.form);
        if (radios.length && !radios.some(radio => radio.checked)) radios[0].click();
    });
})();
