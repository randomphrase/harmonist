// The popover owns opening/dismissal. Inspection mode and native-pixel scroll
// position are shared within the rendered album/review, never persisted.
(() => {
    const openers = new WeakMap();
    const peers = viewer => Array.from(document.querySelectorAll('.art-full'))
        .filter(other => other.dataset.artGallery === viewer.dataset.artGallery);
    const sourceOf = image => image.getAttribute('src');
    const dimensions = image => [
        image.naturalWidth || Number(image.getAttribute('width')),
        image.naturalHeight || Number(image.getAttribute('height')),
    ];

    // The scale control says what the window makes of the image (#918): how
    // far Fit shrinks it, or that it is at its own pixels — and nothing to
    // press where Fit already is 100%. `note` is what else 100% did.
    function label(button, fit, native, note = '') {
        button.disabled = fit >= 1;
        button.setAttribute('aria-pressed', String(native));
        button.textContent = fit >= 1 || native ? `100%${note}` : `Fit · ${Math.round(fit * 100)}%`;
        button.title = fit >= 1 ? 'Shown at full size' : native ? 'Fit to the window' : 'Show at full size';
    }
    // The pane's cursor says what a click on the image will do.
    const cursor = (stage, fit, native) => {
        stage.dataset.zoom = fit >= 1 ? 'same' : native ? 'native' : 'fit';
    };
    // Where on the image a click landed, as fractions of it: what 100% centres.
    const pointOn = (image, event) => {
        if (!event || event.target !== image) return [.5, .5];
        const box = image.getBoundingClientRect();
        return [(event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height];
    };
    const centre = (stage, image, [x, y]) => stage.scrollTo(
        x * image.offsetWidth - stage.clientWidth / 2, y * image.offsetHeight - stage.clientHeight / 2);

    function render(viewer) {
        if (!viewer.matches(':popover-open')) return;
        const stage = viewer.querySelector('.art-full__stage');
        const image = viewer.querySelector('img');
        const [width, height] = dimensions(image);
        if (!width || !height) return; // The image's load event will measure it.
        const fit = Math.min(1, stage.clientWidth / width, stage.clientHeight / height);
        const native = viewer.dataset.zoom === 'native' && fit < 1;
        const scale = native ? 1 : fit;
        image.style.width = `${width * scale}px`;
        image.style.height = `${height * scale}px`;
        cursor(stage, fit, native);
        label(viewer.querySelector('[data-art-zoom]'), fit, native);
    }
    function zoom(viewer, event) {
        const stage = viewer.querySelector('.art-full__stage');
        if (stage.dataset.zoom === 'same') return;
        const image = viewer.querySelector('img');
        const point = pointOn(image, event);
        viewer.dataset.zoom = viewer.dataset.zoom === 'native' ? 'fit' : 'native';
        render(viewer);
        if (viewer.dataset.zoom === 'native') centre(stage, image, point);
        else stage.scrollTo(0, 0);
    }

    // A CAA response or an enclosing Inbox refresh can replace/detach the open
    // popover. Carry the live inspection through any swap containing it, not
    // through the request: the user may switch images, change scale, or dismiss
    // the viewer while it is in flight.
    const inspections = new WeakMap();
    const viewerFocus = ['.art-full__stage', '[data-art-zoom]', '.art-full__nav > .art-arrow:first-child',
                         '.art-full__nav > .art-arrow:last-child', '[data-autofocus]'];
    document.addEventListener('htmx:beforeSwap', event => {
        const target = event.detail.target;
        if (event.defaultPrevented || !event.detail.shouldSwap) return;
        const viewer = target.querySelector('.art-full:popover-open');
        if (!viewer) return;
        const stage = viewer.querySelector('.art-full__stage');
        const focus = viewerFocus.find(selector => viewer.querySelector(selector) === document.activeElement);
        inspections.set(target, {
            id: viewer.id,
            source: sourceOf(viewer.querySelector('img')),
            mode: viewer.dataset.zoom,
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
        viewer.dataset.zoom = inspection.mode;
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
    // ‹ and ›: on to the gallery's next viewer, at the same scale and — at
    // 100% — the same place, with the same arrow under the keyboard where it
    // still goes somewhere.
    function switchTo(viewer, arrow) {
        const next = document.getElementById(arrow.dataset.artView);
        if (!next || !peers(viewer).includes(next) || next === viewer) return;
        const stage = viewer.querySelector('.art-full__stage');
        const position = [stage.scrollLeft, stage.scrollTop];
        next.dataset.zoom = viewer.dataset.zoom;
        const opener = openers.get(viewer);
        viewer.hidePopover();
        next.showPopover();
        openers.set(next, opener);
        render(next);
        next.querySelector('.art-full__stage').scrollTo(...(next.dataset.zoom === 'native' ? position : [0, 0]));
        const same = next.querySelector(`.art-full__nav > .art-arrow:${arrow.matches(':first-child') ? 'first' : 'last'}-child`);
        (same && !same.disabled ? same : next.querySelector('[data-autofocus]')).focus({preventScroll: true});
    }
    document.addEventListener('click', event => {
        const viewer = event.target.closest('.art-full');
        if (!viewer) return;
        const arrow = event.target.closest('[data-art-view]');
        if (arrow) switchTo(viewer, arrow);
        else if (event.target.closest('[data-art-zoom]') || event.target.matches('.art-full__stage img')) {
            zoom(viewer, event);
        }
    });
    // ← and → do what ‹ and › do — except in a picture at 100% with the
    // keyboard in it, where they scroll it.
    document.addEventListener('keydown', event => {
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        const viewer = event.target.closest?.('.art-full:popover-open');
        const which = {ArrowLeft: 'first', ArrowRight: 'last'}[event.key];
        if (!viewer || !which) return;
        if (event.target.matches('.art-full__stage') && viewer.dataset.zoom === 'native') return;
        const arrow = viewer.querySelector(`.art-full__nav > .art-arrow:${which}-child`);
        if (!arrow || arrow.disabled) return;
        event.preventDefault();
        switchTo(viewer, arrow);
    });
    document.addEventListener('load', event => {
        if (!(event.target instanceof HTMLImageElement)) return;
        if (event.target.closest('.art-full')) renderOpen();
        const panel = event.target.closest('.art-pick__picker');
        if (panel) renderPanel(panel);
    }, true);
    window.addEventListener('resize', () => {
        renderOpen();
        document.querySelectorAll('.art-pick__picker:popover-open').forEach(renderPanel);
    });

    // The picker, maximized (#918): the checked row's image beside the shown
    // one, drawn at the same size so a spot on one lines up with the same spot
    // on the other. Fit shows both whole; 100% is the larger at its own pixels
    // and the smaller scaled to match, saying by how much, since that is where
    // softness shows.
    const checkedRow = form => form.querySelector('input[name="row"]:checked');
    const shownPair = panel => {
        const form = panel.closest('form');
        const row = checkedRow(form)?.id.split('-').pop();
        const candidate = form.querySelector('input[name="candidate"]:checked')?.value;
        return [
            panel.querySelector(`.art-max__stage--now [data-row="${row}"]`),
            panel.querySelector(`.art-max__stage--cand [data-cand="${candidate}"]`),
        ];
    };
    function renderPanel(panel) {
        if (!panel.matches(':popover-open')) return;
        const [now, offered] = shownPair(panel);
        fetchOriginal(offered);
        status(panel);
        // The smaller pane's room: the left carries the border between them.
        const panes = Array.from(panel.querySelectorAll('.art-max__stage'));
        const paneWidth = Math.min(...panes.map(pane => pane.clientWidth));
        const paneHeight = Math.min(...panes.map(pane => pane.clientHeight));
        // An image's size, where it is known: an archive image's only once its
        // original is here. Until then its thumbnail stands in, filling the pane.
        const sizeOf = image => {
            if (!(image instanceof HTMLImageElement) || image.dataset.state === 'preview') return null;
            // An original's size is known from fetching it, before the pane's
            // own copy has loaded — so it is never drawn at its own size first.
            const [width, height] = originals.get(image.dataset.original)?.size || dimensions(image);
            return width && height ? [width, height] : null;
        };
        const known = [now, offered].map(sizeOf);
        const sized = known.filter(Boolean);
        const widest = Math.max(0, ...sized.map(([width]) => width));
        const fitWidth = ([width, height]) => Math.min(paneWidth, paneHeight * width / height);
        const fittedWidth = sized.length ? Math.min(widest, ...sized.map(fitWidth)) : 0;
        const fit = widest ? fittedWidth / widest : 1;
        const waiting = offered?.dataset.state === 'preview';
        const native = panel.dataset.zoom === 'native' && fit < 1 && !waiting;
        const shownWidth = native ? widest : fittedWidth;
        [now, offered].forEach((image, at) => {
            if (!(image instanceof HTMLImageElement)) return;
            const size = known[at] || dimensions(image);
            // Not shown until it can be sized, rather than at its own size first.
            image.style.visibility = size[0] && size[1] ? '' : 'hidden';
            if (!size[0] || !size[1]) return;
            const width = known[at] ? shownWidth : (shownWidth || fitWidth(size));
            image.style.width = `${width}px`;
            image.style.height = `${width * size[1] / size[0]}px`;
        });
        const smallest = Math.min(...sized.map(([width]) => width));
        const note = native && sized.length === 2 && smallest < widest
            ? ` · smaller one at ${(widest / smallest).toFixed(1)}×` : '';
        panel.querySelectorAll('.art-max__stage').forEach(pane => cursor(pane, waiting ? 1 : fit, native));
        const button = panel.querySelector('[data-art-zoom]');
        label(button, fit, native, note);
        if (waiting) button.disabled = true;
    }
    function zoomPanel(panel, event) {
        const stage = event?.target.closest('.art-max__stage') || panel.querySelector('.art-max__stage--cand');
        if (stage.dataset.zoom === 'same') return;
        const image = event?.target instanceof HTMLImageElement ? event.target : null;
        const point = pointOn(image, event);
        panel.dataset.zoom = panel.dataset.zoom === 'native' ? 'fit' : 'native';
        renderPanel(panel);
        if (panel.dataset.zoom !== 'native') {
            panel.querySelectorAll('.art-max__stage').forEach(pane => pane.scrollTo(0, 0));
        } else if (image) {
            centre(stage, image, point);
        } else {
            const shown = shownPair(panel)[1];
            if (shown) centre(stage, shown, point);
        }
    }
    // Panned together: one pane's scroll is the other's. The other's own
    // scroll event then finds the two already level and goes no further.
    document.addEventListener('scroll', event => {
        const pane = event.target;
        if (!(pane instanceof Element) || !pane.matches('.art-max__stage')) return;
        const other = Array.from(pane.closest('.art-pick__picker').querySelectorAll('.art-max__stage'))
            .find(stage => stage !== pane);
        if (other.scrollLeft !== pane.scrollLeft) other.scrollLeft = pane.scrollLeft;
        if (other.scrollTop !== pane.scrollTop) other.scrollTop = pane.scrollTop;
    }, true);

    // An archive image's original, fetched when the maximized picker first
    // shows it — never for an image nobody looked at. Kept by address, so a
    // re-rendered section finds what is already here, and a slow fetch goes on
    // counting while the user steps away and back.
    const originals = new Map();
    function fetchOriginal(image) {
        const address = image?.dataset.original;
        if (!address) return;
        let load = originals.get(address);
        if (!load || load.state === 'failed' && image.dataset.retry) {
            delete image.dataset.retry;
            load = {state: 'loading', started: Date.now()};
            originals.set(address, load);
            const fetching = new Image();
            fetching.onload = () => {
                load.state = 'loaded';
                load.size = [fetching.naturalWidth, fetching.naturalHeight];
                document.querySelectorAll('.art-pick__picker:popover-open').forEach(renderPanel);
            };
            fetching.onerror = () => {
                load.state = 'failed';
                document.querySelectorAll('.art-pick__picker:popover-open').forEach(status);
            };
            fetching.src = address;
        }
        if (load.state === 'loaded') {
            if (image.getAttribute('src') !== address) image.src = address;
            image.dataset.state = 'loaded';
        } else {
            image.dataset.state = 'preview';
        }
    }
    // What the shown pane is waiting on: another release's images being
    // listed, the original on its way, or the archive having failed to send it.
    function status(panel) {
        const line = panel.querySelector('.art-max__status');
        const address = shownPair(panel)[1]?.dataset.original;
        const load = address && originals.get(address);
        let text = '';
        if (panel.closest('form')?.classList.contains('htmx-request') && panel.dataset.listing !== undefined) {
            text = 'Loading artwork…';
        } else if (load?.state === 'loading') {
            const seconds = Math.floor((Date.now() - load.started) / 1000);
            text = `Fetching the original from the archive${seconds >= 2 ? ` · ${seconds} s` : '…'}`;
        } else if (load?.state === 'failed') {
            if (line.dataset.failed !== address) {
                line.dataset.failed = address;
                line.replaceChildren("The archive didn't send this image. ");
                const retry = document.createElement('button');
                retry.type = 'button';
                retry.dataset.artRetry = '';
                retry.textContent = 'Try again';
                line.append(retry);
            }
            return;
        }
        delete line.dataset.failed;
        if (line.textContent !== text) line.textContent = text;
    }
    setInterval(() => document.querySelectorAll('.art-pick__picker:popover-open').forEach(status), 1000);

    document.addEventListener('toggle', event => {
        const panel = event.target;
        if (!panel.matches('.art-pick__picker')) return;
        const open = event.newState === 'open';
        const size = panel.querySelector('.art-pick__size');
        size.setAttribute('aria-label', open ? 'Put the artwork picker back' : 'Maximize the artwork picker');
        size.title = open ? 'Back to the page' : 'Compare full size';
        // A dialog while maximized; on the page, part of the section.
        if (open) {
            panel.setAttribute('role', 'dialog');
            panel.setAttribute('aria-label', 'Artwork picker');
            // Opened from its image, which the larger layout hides: the
            // keyboard goes to the toggle that put it there, ready for ← →.
            const focused = document.activeElement;
            if (!panel.contains(focused) || !focused.checkVisibility()) size.focus({preventScroll: true});
            renderPanel(panel);
        } else {
            panel.removeAttribute('role');
            panel.removeAttribute('aria-label');
            panel.dataset.zoom = 'fit';
        }
    }, true);
    document.addEventListener('click', event => {
        const panel = event.target.closest('.art-pick__picker');
        if (!panel?.matches(':popover-open')) return;
        if (event.target.closest('.art-pick__use--go')) {
            // Use puts the picker back, to the row's After Apply: where what
            // is about to be written is shown, and taken back with its ×.
            panel.hidePopover();
        } else if (event.target.closest('[data-art-zoom]') || event.target.matches('.art-max__stage img')) {
            zoomPanel(panel, event);
        } else if (event.target.closest('[data-art-retry]')) {
            const shown = shownPair(panel)[1];
            shown.dataset.retry = '1';
            renderPanel(panel);
        }
    });
    // The arrow keys step as the arrows do: ↑ ↓ the rows, ← → the images —
    // except in a pane at 100% with the keyboard in it, where they scroll.
    document.addEventListener('keydown', event => {
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        const panel = event.target.closest?.('.art-pick__picker:popover-open');
        const press = {
            ArrowUp: '.art-pick__step[data-art-step="-1"]',
            ArrowDown: '.art-pick__step[data-art-step="1"]',
            ArrowLeft: '.art-pick__turn--back',
            ArrowRight: '.art-pick__turn--on',
        }[event.key];
        if (!panel || !press) return;
        if (event.target.matches('.art-max__stage') && panel.dataset.zoom === 'native') return;
        event.preventDefault();
        const button = panel.querySelector(press);
        if (button && !button.disabled) button.click();
    });
    document.addEventListener('change', event => {
        if (!event.target.matches('input[name="row"], input[name="candidate"], .art-pick__front-only input')) return;
        const panel = event.target.form?.querySelector('.art-pick__picker');
        if (panel) renderPanel(panel);
    });

    // A section re-rendered while maximized — Don't suggest, a step on to
    // another release, the archive's answer arriving — stays maximized, at
    // the same scale and place. (Use puts the picker back before it is sent.)
    const maximized = new WeakMap();
    const panelFocus = ['.art-pick__turn--back', '.art-pick__turn--on', '.art-pick__step[data-art-step="-1"]',
                        '.art-pick__step[data-art-step="1"]', '[data-art-zoom]', '.art-pick__size',
                        '.art-max__stage--now', '.art-max__stage--cand', 'input[name="rejected"]',
                        '.art-pick__front-only input[type="checkbox"]'];
    document.addEventListener('htmx:beforeSwap', event => {
        const target = event.detail.target;
        if (event.defaultPrevented || !event.detail.shouldSwap) return;
        const panel = target.querySelector('.art-pick__picker:popover-open');
        if (!panel) return;
        const stage = panel.querySelector('.art-max__stage--cand');
        maximized.set(target, {
            id: panel.id,
            zoom: panel.dataset.zoom,
            position: [stage.scrollLeft, stage.scrollTop],
            focus: panelFocus.find(selector => {
                const control = panel.querySelector(selector);
                return control && control === document.activeElement?.closest(selector);
            }),
        });
    });
    document.addEventListener('htmx:afterSettle', event => {
        const target = event.detail.target;
        const was = maximized.get(target);
        if (!was) return;
        maximized.delete(target);
        const panel = document.getElementById(was.id);
        if (!panel || !target.contains(panel)) return;
        panel.dataset.zoom = was.zoom;
        panel.showPopover();
        renderPanel(panel);
        panel.querySelector('.art-max__stage--cand').scrollTo(...was.position);
        const focus = was.focus && panel.querySelector(was.focus);
        (focus && !focus.disabled ? focus : panel.querySelector('.art-pick__size')).focus({preventScroll: true});
    });

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
            // Past the last image: on to the next release, if there may be one
            // — which the maximized picker says it is listing (#918).
            const going = more('step');
            const panel = step.form.querySelector('.art-pick__picker');
            if (going && panel) {
                panel.dataset.listing = '';
                going.click();
                status(panel);
            }
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
    // ‹ and › only where they go somewhere, and ↑ and ↓ the same: ‹ not on the
    // first image shown, › not on the last — unless another release may follow
    // it. Disabled, which the stylesheet hides, so a press that would do
    // nothing is never offered. Between ‹ and ›, where the shown image is among
    // those Front only lets it show (#918). Re-read after anything that moves
    // the picker or changes what it shows.
    const turns = form => {
        const rows = Array.from(form.querySelectorAll('input[name="row"]'));
        const row = rows.findIndex(radio => radio.checked);
        form.querySelectorAll('.art-pick__step').forEach(step => {
            step.disabled = Number(step.dataset.artStep) < 0 ? row <= 0 : row === rows.length - 1;
        });
        const back = form.querySelector('.art-pick__turn--back');
        const on = form.querySelector('.art-pick__turn--on');
        if (!back || !on) return;
        const radios = shownImages(form);
        const at = radios.findIndex(radio => radio.checked);
        back.disabled = at <= 0;
        on.disabled = at === radios.length - 1 && !form.querySelector('.art-pick__more[data-more="step"]');
        form.querySelector('.art-pick__count').textContent = `${at + 1} of ${radios.length}`;
    };
    document.addEventListener('htmx:load', event => {
        const root = event.detail.elt;
        const forms = root.matches?.('form.art-compare') ? [root]
            : Array.from(root.querySelectorAll?.('form.art-compare') ?? []);
        forms.forEach(turns);
    });
    document.addEventListener('change', event => {
        if (event.target.matches('input[name="row"], input[name="candidate"], .art-pick__front-only input')) {
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
