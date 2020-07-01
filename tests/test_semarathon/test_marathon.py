import datetime
import json
import random
import time

import pytest
import requests
from freezegun import freeze_time

from semarathon import marathon as mth
from semarathon.utils import coroutine
from .mocks import RepDetailsMock, mock_target_no_participants

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


# TODO: mock Site API (fetch)

# noinspection PyPep8Naming
@pytest.mark.usefixtures("use_single_http_session")
class TestMarathon:
    @pytest.fixture(autouse=True)
    def reraise_(self, reraise):
        return reraise

    def test_init_noArgs_defaultSites(self):
        marathon = mth.Marathon()
        assert set(marathon.sites) == set(mth.DEFAULT_SITES_KEYS)

    @pytest.mark.parametrize(
        "sites", argvalues=[random.choices(SITE_NAMES, k=10) for _ in range(3)]
    )
    def test_init_sitesList_getRegistered(self, sites):
        marathon = mth.Marathon(*sites)
        assert set(marathon.sites) == set(sites)
        # TODO: refactor as list
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
        ["sites", "name", "network_id"],
        argvalues=[([], "Anakhand", 8120429), ([], "maxbp", 11213456)],
    )
    def test_addParticipant_networkID_works(self, sites, name, network_id):
        marathon = mth.Marathon(*sites)
        marathon.add_participant(name, network_id)

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
        marathon.duration = datetime.timedelta(seconds=1)
        now = datetime.datetime.now()
        with freeze_time(now):
            marathon.start(mock_target_no_participants())
        assert marathon.is_running
        assert marathon.start_time == now
        assert marathon.end_time == marathon.start_time + marathon.duration

    def test_start_withZeroDuration_ends(self, marathon):
        marathon.duration = datetime.timedelta(0)
        marathon.start(mock_target_no_participants())
        time.sleep(0.01)
        assert not marathon.is_running

    def test_start_withPositiveDuration_lastsEnough(self, marathon):
        delta = datetime.timedelta(seconds=0.1)
        marathon.duration = delta
        marathon.start(mock_target_no_participants())
        time.sleep(0.095)
        assert marathon.is_running
        time.sleep(0.02)
        assert not marathon.is_running

    @pytest.mark.parametrize(
        "refresh_interval",
        argvalues=[datetime.timedelta(seconds=0.25), datetime.timedelta(seconds=0.5),],
    )
    def test_start_someRefreshInterval_receiveUpdatesOnTime(
        self, marathon, refresh_interval, reraise
    ):
        expected_interval_seconds = refresh_interval.total_seconds()
        received = False
        last_time = datetime.datetime.now()

        @coroutine
        def update_handler():
            nonlocal last_time, received

            while True:
                update = yield
                now = datetime.datetime.now()
                received = received or bool(update)
                interval_seconds = (now - last_time).total_seconds()
                assert interval_seconds == pytest.approx(expected_interval_seconds)
                assert update.total == 3 * 10
                last_time = now

        with RepDetailsMock():
            marathon.add_participant("Anakhand", 8120429)
            marathon.refresh_interval = refresh_interval
            marathon.start(update_handler())
            time.sleep(expected_interval_seconds)
            marathon.stop()

        assert received

    def test_stop_endsMarathon(self, marathon):
        marathon.start(mock_target_no_participants())
        marathon.stop()
        assert not marathon.is_running
