// The popover owns opening/dismissal. Only inspection scale and scroll position
// are shared between images in the same rendered album/review, never persisted.
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
        // Fit the largest extents in the group so switching does not silently
        // give a smaller source more magnification than its comparison image.
        const sizes = peers(viewer).map(other => dimensions(other.querySelector('img')));
        const scale = native ? 1 : Math.min(1,
            stage.clientWidth / Math.max(...sizes.map(size => size[0])),
            stage.clientHeight / Math.max(...sizes.map(size => size[1])));
        image.style.width = `${width * scale}px`;
        image.style.height = `${height * scale}px`;
        viewer.querySelector('output').textContent = native
            ? '100% · 1 image pixel per CSS pixel'
            : `${(scale * 100).toFixed(1)}% · same scale for all images`;
    }

    const renderOpen = () => document.querySelectorAll('.art-full:popover-open').forEach(render);
    document.addEventListener('beforetoggle', event => {
        const viewer = event.target;
        if (!viewer.matches('.art-full') || event.newState !== 'open') return;
        if (!document.activeElement.closest('.art-full')) {
            openers.set(viewer, document.activeElement);
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
            next.querySelector('.art-full__stage').scrollTo(...position);
            next.querySelector('.art-full__image-choice').focus();
        }
    });
    document.addEventListener('load', event => {
        if (event.target instanceof HTMLImageElement && event.target.closest('.art-full')) renderOpen();
    }, true);
    window.addEventListener('resize', renderOpen);
})();
