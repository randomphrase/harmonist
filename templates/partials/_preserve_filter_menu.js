// Keep a native filter menu open through background refreshes only. User
// navigation replaces the choices and closes the menu as usual.
(function () {
    const rootId = {{ menu_root | tojson }};
    const refreshMarker = {{ refresh_marker | tojson }};
    let held = null;
    document.body.addEventListener('htmx:beforeSwap', function (event) {
        if (event.detail.target.id !== rootId) return;
        held = null;
        if (event.defaultPrevented || !event.detail.shouldSwap
                || !event.detail.requestConfig.elt.matches(refreshMarker)) return;
        const menu = event.detail.target.querySelector('.library-update-menu:popover-open');
        if (!menu) return;
        const focused = document.activeElement;
        const opener = document.querySelector(`[popovertarget="${menu.id}"]`);
        const holdsFocus = focused === opener || menu.contains(focused);
        held = {
            id: menu.id,
            focusId: holdsFocus ? focused.id : '',
            focusHref: holdsFocus ? focused.getAttribute('href') : null,
        };
    });
    document.body.addEventListener('htmx:afterSwap', function (event) {
        if (event.detail.target.id !== rootId || !held) return;
        const {id, focusId, focusHref} = held;
        held = null;
        const menu = document.getElementById(id);
        if (!menu || menu.matches(':popover-open')) return;
        menu.showPopover();
        const focus = focusId ? document.getElementById(focusId)
            : Array.from(menu.querySelectorAll('a[href]')).find(a => a.getAttribute('href') === focusHref);
        focus?.focus({preventScroll: true});
    });
})();
