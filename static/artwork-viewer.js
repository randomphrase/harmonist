// The popover owns opening/dismissal. Inspection mode and native-pixel scroll
// position are shared within the rendered album/review, never persisted.
(() => {
    const openers = new WeakMap();
    const peers = viewer => Array.from(document.querySelectorAll('.art-full'))
        .filter(other => other.dataset.artGallery === viewer.dataset.artGallery);
    // An archive candidate's viewer holds its image back until first opened
    // (`data-src`, #659): either attribute names the picture it shows.
    const sourceOf = image => image.getAttribute('src') || image.dataset.src;
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
        const focus = ['.art-full__stage', '.art-full__mode', '.art-full__image-choice', '[data-autofocus]']
            .find(selector => viewer.querySelector(selector) === document.activeElement);
        inspections.set(target, {
            id: viewer.id,
            source: sourceOf(viewer.querySelector('img')),
            mode: viewer.querySelector('.art-full__mode').value,
            position: [stage.scrollLeft, stage.scrollTop],
            opener: openers.get(viewer)?.getAttribute('popovertarget'),
            focus,
        });
    });
    // Restore after settling, once the swapped section's controls are in
    // place. (Close is marked `data-autofocus`, not `autofocus`, so HTMX no
    // longer focuses a closed viewer's button on every swap, #659.)
    document.addEventListener('htmx:afterSettle', event => {
        const target = event.detail.target;
        const inspection = inspections.get(target);
        if (!inspection) return;
        inspections.delete(target);
        const viewer = document.getElementById(inspection.id);
        // Do not reopen a different image if the refreshed gallery lost this one.
        if (!viewer || !target.contains(viewer) ||
            sourceOf(viewer.querySelector('img')) !== inspection.source) return;
        const opener = Array.from(target.querySelectorAll('button[popovertarget]'))
            .find(button => button.getAttribute('popovertarget') === inspection.opener);
        if (opener) openers.set(viewer, opener);
        viewer.querySelector('.art-full__mode').value = inspection.mode;
        viewer.showPopover();
        render(viewer);
        viewer.querySelector('.art-full__stage').scrollTo(...inspection.position);
        viewer.querySelector(inspection.focus || '[data-autofocus]').focus({preventScroll: true});
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
            const image = viewer.querySelector('img');
            if (!image.getAttribute('src') && image.dataset.src) image.src = image.dataset.src;
            // The focus `autofocus` would have given, without its scroll. Not
            // over a control a caller already focused on opening it.
            if (!viewer.contains(document.activeElement)) {
                viewer.querySelector('[data-autofocus]')?.focus({preventScroll: true});
            }
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
            next.querySelector('[data-autofocus]').focus({preventScroll: true});
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
    // The picker's place — its row, its image, Front only — is the page's,
    // not the server's: moving it makes no request. So a response to a request
    // sent BEFORE the user moved it (the archive check on page open, a re-read,
    // a look ahead) would put it back where it was when that request left,
    // and the picker jumps. Note where it was when each request went, and if
    // the user has moved it since, put back what they moved after the swap.
    // A response to a request sent from where it still is keeps the server's
    // answer — which is how a step to another release lands on its first image.
    // A re-render of the section must not move the window. WebKit scrolls it
    // when the section's form is replaced — synchronously, by tens of pixels,
    // with nothing on the page changing size (#659) — and Chromium does not.
    // So note where the section sits on screen before a swap, and put it back
    // there straight after, before anything is painted: wherever the browser
    // left it, the section the user is looking at stays put.
    const onScreen = new WeakMap();
    document.addEventListener('htmx:beforeSwap', event => {
        const target = event.detail.target;
        if (target.id !== 'album-artwork' || event.defaultPrevented || !event.detail.shouldSwap) return;
        onScreen.set(target, target.getBoundingClientRect().top);
    });
    document.addEventListener('htmx:afterSwap', event => {
        const target = event.detail.target;
        if (!onScreen.has(target)) return;
        const moved = target.getBoundingClientRect().top - onScreen.get(target);
        onScreen.delete(target);
        if (moved) window.scrollBy({top: moved, behavior: 'instant'});
    });

    const pickerOf = target => target.querySelector('form.art-compare');
    const place = form => form && {
        row: form.querySelector('input[name="row"]:checked')?.value,
        candidate: form.querySelector('input[name="candidate"]:checked')?.value,
        frontOnly: form.querySelector('.art-pick__front-only input[type="checkbox"]')?.checked,
    };
    const sentFrom = new WeakMap();
    const movedSince = new WeakMap();
    document.addEventListener('htmx:beforeRequest', event => {
        if (event.detail.target.id !== 'album-artwork') return;
        const form = pickerOf(event.detail.target);
        if (form) sentFrom.set(event.detail.xhr, place(form));
    });
    document.addEventListener('htmx:beforeSwap', event => {
        const target = event.detail.target;
        if (target.id !== 'album-artwork' || event.defaultPrevented || !event.detail.shouldSwap) return;
        const then = sentFrom.get(event.detail.xhr);
        const now = place(pickerOf(target));
        if (!then || !now) return;
        const moved = Object.fromEntries(Object.entries(now).filter(([key, value]) => value !== then[key]));
        if (Object.keys(moved).length) movedSince.set(target, moved);
    });
    document.addEventListener('htmx:afterSwap', event => {
        const target = event.detail.target;
        const moved = movedSince.get(target);
        if (!moved) return;
        movedSince.delete(target);
        const form = pickerOf(target);
        if (!form) return;
        // By value, and only where the new section still has it: a row the
        // check merged away, or an image no longer listed, stays as served.
        const radio = (name, value) => Array.from(form.querySelectorAll(`input[name="${name}"]`))
            .find(input => input.value === value);
        if ('frontOnly' in moved) {
            const box = form.querySelector('.art-pick__front-only input[type="checkbox"]');
            if (box && moved.frontOnly !== undefined) box.checked = moved.frontOnly;
        }
        for (const name of ['row', 'candidate']) {
            const input = name in moved && radio(name, moved[name]);
            if (input) input.checked = true;
        }
    });
    // ‹ and › only where they go somewhere, as the rows' ↑ and ↓ stop at the
    // ends: ‹ not on the first image shown, › not on the last — unless another
    // release may follow it. Disabled, which the stylesheet hides, so a press
    // that would do nothing is never offered. Re-read after anything that moves
    // the carousel or changes what it shows.
    const turns = form => {
        const back = form.querySelector('.art-pick__turn--back');
        const on = form.querySelector('.art-pick__turn--on');
        if (!back || !on) return;
        const radios = shownImages(form);
        const at = radios.findIndex(radio => radio.checked);
        back.disabled = at <= 0;
        on.disabled = at === radios.length - 1 && !form.querySelector('.art-pick__more[data-more="step"]');
    };
    document.addEventListener('htmx:load', event => {
        const root = event.detail.elt;
        const forms = root.matches?.('form.art-compare') ? [root]
            : Array.from(root.querySelectorAll?.('form.art-compare') ?? []);
        forms.forEach(turns);
    });
    document.addEventListener('change', event => {
        if (event.target.matches('input[name="candidate"], .art-pick__front-only input')) {
            turns(event.target.form);
        }
    });
    document.querySelectorAll('form.art-compare').forEach(turns);
    // Ticking Front only while a back cover is shown would leave nothing
    // shown: move to the first image that can be.
    document.addEventListener('change', event => {
        if (!event.target.matches('.art-pick__front-only input[type="checkbox"]')) return;
        const radios = shownImages(event.target.form);
        if (radios.length && !radios.some(radio => radio.checked)) radios[0].click();
    });
})();
