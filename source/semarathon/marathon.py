import datetime
import functools
import json
import logging
import re
from typing import (
    Dict,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import stackauth
import stackexchange as se
from multimethod import multimethod

from semarathon.utils import ReadOnlyDictView, Text, TimedStoppableThread

with open("data/SE-Sites.json") as db:
    SITES = json.load(db)
SITES_BY_URL = {site["site_url"]: site for site in SITES.values()}
DEFAULT_SITES_KEYS = ("stackoverflow", "math", "tex")
SE_APP_KEY = Text.load("se-app-key")

logger = logging.getLogger(__name__)

_stack_auth = stackauth.StackAuth()


class Participant:
    marathon: "Marathon"
    network_id: int

    def __init__(self, marathon: "Marathon", name: str, network_id: int):
        self.marathon = marathon
        self._name = name
        self.network_id = network_id
        self._users: Dict[str, Participant.UserProfile] = {}
        self._score = 0

    def __str__(self):
        return self.name

    @property
    def name(self) -> str:
        return self._name

    @property
    def score(self):
        return self._score

    @property
    def user_profiles(self) -> Mapping[str, "Participant.UserProfile"]:
        return ReadOnlyDictView(self._users)

    class UserProfile:
        participant: "Participant"
        site_key: str
        score: int

        def __init__(self, participant: "Participant", site_user: se.User):
            self.participant = participant
            url = _domain_to_url(site_user.site.domain)
            self.site_key = SITES_BY_URL[url]["api_site_parameter"]
            self._site_user = site_user
            self.score = 0
            self._last_checked: Optional[datetime.datetime] = None

        @classmethod
        def from_id(
            cls, participant: "Participant", site_key: str, user_id: int
        ) -> "Participant.UserProfile":
            """Create a UserProfile object given a user id for a SE site

            :param participant: marathon Participant to which this user profile pertains
            :param site_key: site API key for the SE site this user pertains to
            :param user_id: user id for the given site

            :return: a UserProfile object corresponding to the user with the given id
            :raises UserNotFoundError: if no user with the given id exists in the site
            """
            results = get_api(site_key).users((user_id,))
            assert len(results) <= 1
            if len(results) == 0:
                raise UserNotFoundError(site_key, user_id)
            return Participant.UserProfile(participant, results[0])

        @classmethod
        def from_username(
            cls, participant: "Participant", site_key: str, username: str
        ) -> "Participant.UserProfile":
            """Create a UserProfile object given a unique username for a SE site

            :param participant: marathon Participant to which this user profile pertains
            :param site_key: site API key for the SE site this user pertains to
            :param username: unique username for the given site

            :return: a UserProfile object corresponding to the user with said username
            :raises UserNotFoundError: if no user with the given username exists for
                                       the given site
            :raises MultipleUsersFoundError: if more than one user with the given
                                             username exists for the given site
            """
            results: Sequence[se.User] = get_api(site_key).users_by_name(username)
            if not results:
                raise UserNotFoundError(site_key, username)
            elif len(results) > 1:
                raise MultipleUsersFoundError(site_key, username, results)
            return Participant.UserProfile(participant, results[0])

        @property
        def user_id(self) -> int:
            return self._site_user.id

        @property
        def display_name(self) -> str:
            return self._site_user.display_name

        @property
        def link(self):
            return f"https://{self._site_user.site.domain}/users/{self.user_id}/"

        def update(self) -> int:
            """Return any score changes since the last time that was checked

            The associated marathon must be running in order to call this method.
            If update had never been called up to now, returns changes since the start
            of the marathon.
            Any changes are recorded internally so that `self.score` is always accurate
            for the score since the marathon start up to the last :method:`update` call.

            :return: the increment (or decrement) in score since the last call to
                     `update` or the start of the marathon
            :raises MarathonRuntimeError: if called before the marathon starts
            """
            marathon = self.participant.marathon
            if not marathon.is_running:
                raise MarathonRuntimeError(
                    "Called update() when marathon isn't running"
                )
            last_time = self._last_checked or marathon.start_time
            updates: Sequence[se.RepChange] = self._site_user.reputation_detail.fetch(
                fromdate=int(last_time.timestamp())
            )
            if len(updates) > 0:
                self._last_checked = updates[0].on_date
            increment = sum(
                u.json_ob.reputation_change
                for u in updates
                if u.on_date > self._last_checked
            )
            return increment

    @multimethod
    def add_user_profile(self, user_profile: "Participant.UserProfile") -> None:
        self._users[user_profile.site_key] = user_profile

    @add_user_profile.register
    def add_user_profile(self, site_key: str, user_id: int) -> None:
        """Add user by ID; see :method:`Participant.UserProfile.from_id`"""
        self.add_user_profile(self.UserProfile.from_id(self, site_key, user_id))

    @add_user_profile.register
    def add_user_profile(self, site_key: str, username: str) -> None:
        """Add user by username; see :method:`Participant.UserProfile.from_username`"""
        self.add_user_profile(self.UserProfile.from_username(self, site_key, username))

    @add_user_profile.register
    def add_user_profile(self, site_key: str) -> None:
        """Add user by association to the network account"""
        url = SITES[site_key]["site_url"]
        results = _stack_auth.associated_from_assoc(self.network_id, only_valid=True)
        matching = [ua for ua in results if ua.json_ob.site_url == url]
        assert len(matching) <= 1
        if not matching:
            raise UserNotFoundError(site_key, f"{self.network_id} (network id)")
        self.add_user_profile(site_key, matching[0].json_ob.user_id)


class ScoreUpdate:
    participant: Participant
    per_site: Dict[str, int]

    def __init__(self, participant):
        self.participant = participant
        self.per_site = {}

    @property
    def total(self) -> int:
        return sum(increment for increment in self.per_site.values())

    def fetch_updates(self, sites: Iterable[str]) -> "ScoreUpdate":
        """Query the API for score changes for `self.participant` for a set of sites"""
        for site in sites:
            increment = self.participant.user_profiles[site].update()
            if increment:
                self.per_site[site] = increment
        return self

    def __bool__(self) -> bool:
        return bool(self.per_site)


class Marathon:
    sites: Mapping[str, se.Site]
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
        """A tuple of (elapsed time, remaining time)

        :raises MarathonRuntimeError: if the marathon isn't running
        """
        if not self.is_running:
            raise MarathonRuntimeError("Marathon isn't running yet")
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
        if self._poll_thread is None:
            return False
        stopped = self._poll_thread.stopped
        alive = self._poll_thread.is_alive()
        if not stopped and not alive:
            raise MarathonRuntimeError(
                f"{self._poll_thread} died without stopping: {stopped=}, {alive=}"
            )
        return alive

    def add_site(self, site_key: str):
        self._sites[site_key] = get_api(site_key)
        for participant in self.participants.values():
            participant.add_user_profile(site_key)

    def clear_sites(self):
        self._sites.clear()

    @multimethod
    def add_participant(self, participant: Participant) -> None:
        self.participants[participant.name] = participant
        for site in self.sites:
            participant.add_user_profile(site)

    @add_participant.register
    def add_participant(self, name: str, network_id: int) -> None:
        self.add_participant(Participant(self, name, network_id))

    def poll(self) -> Iterator[ScoreUpdate]:
        """Lazily yield updates for each participant whose reputation has changed"""
        for participant in self.participants.values():
            if update := ScoreUpdate(participant).fetch_updates(self.sites):
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

        self._check_all_participants_have_users()
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

    def _check_all_participants_have_users(self):
        for participant in self.participants.values():
            for site in self._sites:
                if site not in participant.user_profiles:
                    raise SEMarathonError(
                        f"Missing a {SITES[site]['name']} user profile for {participant}"
                    )


# ------------------------------- Exceptions  -------------------------------


class SEMarathonError(Exception):
    pass


class MarathonRuntimeError(SEMarathonError, RuntimeError):
    pass


class UserLookupError(SEMarathonError, LookupError):
    def __init__(self, site_key: str, username_or_id: Union[str, int], *args):
        super().__init__(site_key, username_or_id, *args)
        self.username_or_id = username_or_id
        self.site_key = site_key


class UserNotFoundError(UserLookupError):
    def __init__(self, site_key: str, username_or_id: Union[str, int]):
        super().__init__(site_key, username_or_id)

    def __str__(self):
        return f"User {repr(self.username_or_id)} not found at {SITES[self.site_key]['name']}"


class MultipleUsersFoundError(UserLookupError):
    def __init__(
        self, site_key: str, username_or_id: str, candidates: Sequence[se.User]
    ):
        super(MultipleUsersFoundError, self).__init__(
            site_key, username_or_id, candidates
        )
        self.candidates = candidates

    def __str__(self):
        return (
            f"Multiple candidates found for user {repr(self.username_or_id)} "
            f"at {SITES[self.site_key]['name']} (found {len(self.candidates)} matches)"
        )


class SiteError(SEMarathonError):
    def __init__(self, site):
        super(SiteError, self).__init__(site)
        self.site = site


class SiteNotFoundError(SiteError, LookupError):
    def __str__(self):
        return f"Site '{self.site}' not found on SE network"


# ------------------------------- Misc helpers  -------------------------------


@functools.lru_cache
def get_api(key: str) -> se.Site:
    """Get a Site object corresponding to the given site key

    :param key: a Stack Exchange API site key (e.g. "stackoverflow")
    """
    domain = _get_domain(key)
    return se.Site(domain, app_key=SE_APP_KEY, cache=60, impose_throttling=True)


def _to_site_domain(site: Union[str, se.Site]):
    if isinstance(site, str):
        return site
    elif isinstance(site, se.Site):
        return site.domain
    else:
        raise TypeError(f"expected either Site or str, got {repr(site)}")


def _get_domain(site_api_key: str):
    try:
        url = SITES[site_api_key]["site_url"]
        return _extract_domain(url)
    except KeyError:
        raise SiteNotFoundError(site_api_key)


def _extract_domain(url: str):
    return re.match(r"https?://(?P<domain>[^/]*)", url).group("domain")


def _domain_to_url(domain: str):
    return f"https://{domain}"
