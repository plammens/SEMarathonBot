import datetime
import json
import random
import time

import pytest
import requests

from semarathon import marathon as mth

with open("data/SE-Sites.json") as db:
    SITES = json.load(db)
SITE_NAMES = list(SITES.keys())

http_session = requests.Session()


@pytest.fixture
def use_single_http_session(monkeypatch):
    monkeypatch.setattr(requests, "get", http_session.get)
    monkeypatch.setattr(requests, "post", http_session.post)


@pytest.fixture
def marathon():
    marathon = mth.Marathon()
    yield marathon
    marathon.stop()


def mock_target():
    update = yield
    assert False, f"received update {update} with no participants"


# TODO: mock Site API (fetch)

# noinspection PyPep8Naming
@pytest.mark.usefixtures("use_single_http_session")
class TestMarathon:
    def test_init_noArgs_defaultSites(self):
        marathon = mth.Marathon()
        assert set(marathon.sites) == set(mth.DEFAULT_SITES_KEYS)

    @pytest.mark.parametrize(
        "sites", argvalues=[random.choices(SITE_NAMES, k=10) for _ in range(3)]
    )
    def test_init_sitesList_getRegistered(self, sites):
        marathon = mth.Marathon(*sites)
        assert set(marathon.sites) == set(sites)
        for site in sites:
            assert marathon.sites[site].domain in SITES[site]["site_url"]

    @pytest.mark.parametrize("sites", argvalues=[[], random.choices(SITE_NAMES, k=10)])
    def test_init_any_validInitialState(self, sites):
        marathon = mth.Marathon(*sites)
        assert not marathon.is_running
        assert not marathon.participants

    @pytest.mark.parametrize("site", argvalues=random.choices(SITE_NAMES, k=5))
    def test_addSite_validSite_getsAdded(self, marathon, site):
        marathon.add_site(site)
        assert site in marathon.sites

    @pytest.mark.parametrize(
        "site", argvalues=["StackOverflow", "notasite", "alkajlkf"]
    )
    def test_addSite_invalidSite_raises(self, marathon, site):
        with pytest.raises(mth.SiteNotFoundError):
            marathon.add_site(site)

    @pytest.mark.parametrize(
        ["sites", "participant"], argvalues=[([], "Anakhand"), ([], "maxbp"),]
    )
    def test_addParticipant_uniqueUsername_works(self, sites, participant):
        marathon = mth.Marathon(*sites)
        marathon.add_participant(participant)

    @pytest.mark.parametrize(
        ["sites", "participant"], argvalues=[([], 8120429), ([], 11213456)]
    )
    def test_addParticipant_networkID_works(self, sites, participant):
        marathon = mth.Marathon(*sites)
        marathon.add_participant(participant)

    @pytest.mark.parametrize("duration", [1, 2, 3.5, 0.25])
    def test_setDuration_number_convertedToTimeDelta(self, marathon, duration):
        marathon.duration = duration
        assert isinstance(marathon.duration, datetime.timedelta)

    @pytest.mark.parametrize(
        "duration", [-1, -datetime.timedelta(hours=1), -datetime.timedelta(minutes=1)]
    )
    def test_setDuration_negative_raises(self, marathon, duration):
        with pytest.raises(ValueError):
            marathon.duration = duration

    def test_start_correctState(self, marathon):
        marathon.start(mock_target())
        assert marathon.is_running
        assert marathon.start_time is not None
        assert marathon.start_time < datetime.datetime.now()
        assert marathon.end_time == marathon.start_time + marathon.duration

    def test_start_withZeroDuration_ends(self, marathon):
        marathon.duration = datetime.timedelta(0)
        marathon.start(mock_target())
        time.sleep(0.2)
        assert not marathon.is_running

    def test_stop_endsMarathon(self, marathon):
        marathon.start(mock_target())
        marathon.stop()
        assert not marathon.is_running
