"""A release selected with Use must resolve before the background rescan."""

import pytest

from harmonist import sidecar
from harmonist.models import FoundBy, Sidecar
from test import test_web

cfg = test_web.cfg
client = test_web.client


@pytest.mark.parametrize("has_sidecar", [False, True])
@pytest.mark.parametrize("has_other_copy", [False, True])
@pytest.mark.parametrize("background_scan", [False, True])
def test_use_release_redirect_resolves_before_background_scan(
    client, cfg, monkeypatch, has_sidecar, has_other_copy, background_scan
):
    root = test_web._make_album(cfg, "ChooseRelease")
    if has_sidecar:
        sidecar.write(root, Sidecar())
    aid = test_web._id_for(cfg, root)
    mbid = "abc12345-1234-1234-1234-1234567890ab"
    if has_other_copy:
        test_web._make_tagged_album(cfg, "OtherCopy", mbid=mbid, tagged_at=None)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda selected: test_web._release_for_match(selected, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.front_image", lambda *args, **kwargs: None)
    monkeypatch.setattr("harmonist.contributions.warm", lambda album: None)

    # Production routes serve this snapshot. Hold the asynchronous scan until
    # after the redirected GET so a fast background scan cannot hide the race.
    if background_scan:
        runner = client.app.state.scan_runner
        runner.refresh_now()
        monkeypatch.setattr(runner, "is_engaged", lambda: True)
        monkeypatch.setattr(runner, "request_scan", lambda: None)

    response = client.post(
        f"/manual/{aid}/assign",
        data={"mbid": mbid, "on_album_page": "true", "found_by": FoundBy.NAME_SEARCH},
    )
    assert response.status_code == 200
    # Selected for review (#880): writing the suggestion can be the album's
    # first sidecar, so its id may still change under the redirect.
    assert sidecar.read(root).mb_match_candidate.mb_release_id == mbid
    destination = response.headers["HX-Redirect"]
    opened = client.get(destination)
    assert opened.status_code == 200, opened.text
    assert destination == f"/album/{test_web._id_for(cfg, root)}?review=chosen"
