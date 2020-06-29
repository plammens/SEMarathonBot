import datetime
import functools
import json
import logging
import re
from typing import Dict, Generator, Iterator, Mapping, Optional, Tuple, Union

import stackexchange

from semarathon.utils import ReadOnlyDictView, Text, TimedStoppableThread

with open("data/SE-Sites.json") as db:
    SITES = json.load(db)
DEFAULT_SITES_KEYS = ("stackoverflow", "math", "tex")
SE_APP_KEY = Text.load("se-app-key")

logger = logging.getLogger(__name__)


# TODO: load participant from network ID


class Participant:
    name: str

    def __init__(self, username: str):
        self.name = username
        self._users: Dict[str, Participant.UserProfile] = {}
        self._score = 0

    def __str__(self):
        return self.name

    @property
    def score(self):
        return self._score

    class UserProfile(stackexchange.User):
        site: stackexchange.Site
        id: int
        score: int

        @classmethod
        def from_username(
            cls, site_key: str, username: str
        ) -> "Participant.UserProfile":
            """Create a UserProfile object given a unique username

            :param site_key: site API key for the SE site this user pertains to
            :param username: unique username for the given site
            :return: a UserProfile object corresponding to the user with said username
            """
            results = get_api(site_key).users_by_name(username)
            if not results:
                raise UserNotFoundError(site_key, username)
            elif len(results) > 1:
                raise MultipleUsersFoundError(site_key, username, results)
            return results[0]

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.score = 0
            self._last_checked = datetime.datetime.now()

        @property
        def link(self):
            return f"{self.site.domain}/users/{self.id}/"

        def update(self) -> int:
            updates = self.reputation_detail.fetch(
                fromdate=int(self._last_checked.timestamp())
            )
            if len(updates) > 0:
                self._last_checked = updates[0].on_date
            increment = sum(
                u.json["reputation_change"]
                for u in updates
                if u.on_date > self._last_checked
            )
            return increment

    def get_user(self, site_key: str) -> UserProfile:
        if site_key not in self._users:
            self._users[site_key] = self.UserProfile.from_username(site_key, self.name)
        return self._users[site_key]

    def fetch_users(self, *site_keys: str) -> Mapping[str, UserProfile]:
        return {key: self.get_user(key) for key in site_keys}


class ScoreUpdate:
    participant: Participant
    per_site: Dict[str, int]

    def __init__(self, participant):
        self.participant = participant
        self.per_site = {}

    @property
    def total(self) -> int:
        return sum(increment for increment in self.per_site.values())

    def __getitem__(self, key: str) -> int:
        return self.per_site[key]

    def __setitem__(self, key: str, value: int):
        self.per_site[key] = value

    def __bool__(self) -> bool:
        return bool(self.per_site)


class Marathon:
    sites: Mapping[str, stackexchange.Site]
    participants: Dict[str, Participant]
    duration: datetime.timedelta
    start_time: Optional[datetime.datetime]
    end_time: Optional[datetime.datetime]

    def __init__(self, *sites: str, duration: Union[float, datetime.timedelta] = 4):
        """Initialise a new marathon

        :param sites: API keys for the SE sites to track
        :param duration: duration of the marathon
        """
        sites = sites or DEFAULT_SITES_KEYS
        self._sites = {key: get_api(key) for key in sites}
        self.participants = {}
        self.duration = duration
        self.start_time = None
        self.end_time = None
        self._poll_thread: Optional[TimedStoppableThread] = None

    @property
    def sites(self):
        return ReadOnlyDictView(self._sites)

    @property
    def duration(self):
        return self._duration

    @duration.setter
    def duration(self, value: Union[float, datetime.timedelta]):
        if not isinstance(value, datetime.timedelta):
            value = datetime.timedelta(hours=value)
        if value.total_seconds() < 0:
            raise ValueError(f"duration should be positive (received {value})")
        self._duration = value

    @property
    def elapsed_remaining(self) -> Tuple[datetime.timedelta, datetime.timedelta]:
        if not self.is_running:
            raise RuntimeError("Marathon isn't running yet")
        assert self.start_time is not None
        assert self.end_time is not None
        now = datetime.datetime.now()
        return now - self.start_time, self.end_time - now

    @property
    def refresh_interval(self) -> datetime.timedelta:
        """Time interval between each query when polling the SE API"""
        if self.duration >= datetime.timedelta(hours=2):
            return datetime.timedelta(minutes=30)
        elif self.duration >= datetime.timedelta(minutes=45):
            return datetime.timedelta(minutes=15)
        elif self.duration >= datetime.timedelta(minutes=15):
            return datetime.timedelta(minutes=5)
        elif self.duration >= datetime.timedelta(minutes=10):
            return datetime.timedelta(minutes=2)
        else:
            return datetime.timedelta(minutes=1)

    @property
    def is_running(self) -> bool:
        return self._poll_thread is not None and not self._poll_thread.stopped

    def add_site(self, site: str):
        self._sites[site] = get_api(site)
        for participant in self.participants.values():
            participant.fetch_users(site)

    def clear_sites(self):
        self._sites.clear()

    def add_participant(self, username: str):
        p = Participant(username)
        p.fetch_users(*self.sites)
        self.participants[username] = p

    def poll(self) -> Iterator[ScoreUpdate]:
        """Lazily yield updates for each participant whose reputation has changed"""
        for participant in self.participants.values():
            update = ScoreUpdate(participant)
            for site in self.sites:
                increment = participant.get_user(site).update()
                if increment:
                    update[site] = increment
            if update:
                yield update

    def start(self, handler: Generator[None, ScoreUpdate, None]):
        """Start the marathon in a separate thread

        Creates a new thread that polls the Stack Exchange API

        :param handler: coroutine to which updates will be sent
        """
        timeout = self.refresh_interval.total_seconds()

        def run():
            logger.info("Marathon thread started")
            while not self._poll_thread.stop_event.wait(timeout=timeout):
                for update in self.poll():
                    handler.send(update)
            logger.info("Ending marathon thread")
            handler.close()

        self.start_time = datetime.datetime.now()
        self.end_time = self.start_time + self.duration
        self._poll_thread = TimedStoppableThread(
            duration=self.duration, name="MarathonPoll", target=run, daemon=True
        )
        self._poll_thread.start()

    def stop(self):
        if self.is_running:
            logger.debug("Stopping marathon")
            self._poll_thread.stop()
            self._poll_thread.join()
        else:
            logger.warning("Tried to stop a marathon that isn't running")


# ------------------------------- Exceptions  -------------------------------


class SEMarathonError(Exception):
    pass


class UserError(SEMarathonError, LookupError):
    pass


class UserNotFoundError(UserError, LookupError):
    def __init__(self, site_key: str, username: str, *args):
        super().__init__(username, site_key, *args)

    def __str__(self):
        username, site_key, *_ = self.args
        return f"User {repr(username)} not found at {SITES[site_key]['name']}"


class MultipleUsersFoundError(UserError, LookupError):
    def __init__(
        self,
        site_key: str,
        username: str,
        candidates: Sequence[stackexchange.User],
        *args,
    ):
        super().__init__(username, site_key, candidates, *args)
        self.username = username
        self.site_key = site_key
        self.candidates = candidates

    def __str__(self):
        return (
            f"Multiple candidates found for user {repr(self.username)} "
            f"at {SITES[self.site_key]['name']} (found {len(self.candidates)} matches)"
        )


class SiteError(SEMarathonError):
    def __init__(self, site):
        self.site = site


class SiteNotFoundError(SiteError, LookupError):
    def __str__(self):
        return f"Site '{self.site}' not found on SE network"


# ------------------------------- Misc helpers  -------------------------------


@functools.lru_cache
def get_api(key: str) -> stackexchange.Site:
    """Get a Site object corresponding to the given site key

    :param key: a Stack Exchange API site key (e.g. "stackoverflow")
    """
    domain = _get_domain(key)
    return stackexchange.Site(
        domain, app_key=SE_APP_KEY, cache=60, impose_throttling=True
    )


def _to_site_domain(site: Union[str, stackexchange.Site]):
    if isinstance(site, str):
        return site
    elif isinstance(site, stackexchange.Site):
        return site.domain
    else:
        raise TypeError(f"expected either Site or str, got {repr(site)}")


def _get_domain(site_api_key: str):
    try:
        url = SITES[site_api_key]["site_url"]
        return re.match(r"https?://(?P<domain>.*)", url).group("domain")
    except KeyError:
        raise SiteNotFoundError(site_api_key)
