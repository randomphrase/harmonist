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

    // A CAA response replaces the album's thumbnails AND popovers. Carry the
    // live inspection through that swap, not through the request: the user may
    // switch images, change scale, or dismiss the viewer while it is in flight.
    const inspections = new WeakMap();
    document.addEventListener('htmx:beforeSwap', event => {
        const target = event.detail.target;
        if (target.id !== 'album-artwork' || event.defaultPrevented || !event.detail.shouldSwap) return;
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
})();
