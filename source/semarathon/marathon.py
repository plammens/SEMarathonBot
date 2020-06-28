import datetime
import functools
import json
import logging
import time
from typing import Dict, Generator, Iterator, List, Optional, Tuple, Union

import stackapi

from semarathon.utils import TimedStoppableThread

with open("data/SE-Sites.json") as db:
    SITES = json.load(db)
DEFAULT_SITES = ("stackoverflow", "math", "tex")

logger = logging.getLogger(__name__)


class Participant:
    name: str

    class UserProfile:
        link: str
        last_checked: int

        def __init__(self, site: str, username: str):
            self.site = site
            self.name = username

            results = get_api(site).fetch(
                "users",
                inname=username,
                sort="name",
                order="desc",
                min=username,
                max=username,
            )["items"]
            if not results:
                raise UserNotFoundError(self)
            elif len(results) > 1:
                raise MultipleUsersFoundError(self)
            user_data = results[0]

            self.id = user_data["user_id"]
            self.link = SITES[site]["site_url"] + "/users/{}/".format(self.id)
            self.last_checked = int(time.time())
            self.score = 0

        def update(self) -> bool:
            results = get_api(self.site).fetch(
                "users/{}/reputation".format(self.id), fromdate=self.last_checked
            )
            updates = results["items"]
            if updates:
                self.last_checked = updates[0]["on_date"] + 1
            increment = sum(u["reputation_change"] for u in updates)
            return bool(increment)

    def __init__(self, username: str):
        self.name = username
        self._users: Dict[str, Participant.UserProfile] = {}

    def __str__(self):
        return self.name

    @property
    def score(self):
        return sum(u.score for u in self._users.values())

    def user(self, site: str):
        if site not in self._users:
            self._users[site] = Participant.UserProfile(site, self.name)
        return self._users[site]

    def fetch_users(self, *sites: str):
        for site in sites:
            self._users[site] = Participant.UserProfile(site, self.name)


class Update:
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
    sites: List[str]
    participants: Dict[str, Participant]
    duration: datetime.timedelta
    start_time: Optional[datetime.datetime]
    end_time: Optional[datetime.datetime]

    def __init__(self, *sites: str, duration: Union[float, datetime.timedelta] = 4):
        self.sites = list(sites) if sites else list(DEFAULT_SITES)
        self.participants = {}
        self.duration = duration
        self.start_time = None
        self.end_time = None
        self._poll_thread: Optional[TimedStoppableThread] = None

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
        if site not in SITES:
            raise SiteNotFoundError(site)
        self.sites.append(site)
        for participant in self.participants.values():
            participant.fetch_users(site)

    def clear_sites(self):
        self.sites.clear()

    def add_participant(self, username: str):
        p = Participant(username)
        p.fetch_users(*self.sites)
        self.participants[username] = p

    def poll(self) -> Iterator[Update]:
        """Lazily yield updates for each participant whose reputation has changed"""
        for participant in self.participants.values():
            update = Update(participant)
            for site in self.sites:
                increment = participant.user(site).update()
                if increment:
                    update[site] = increment
            if update:
                yield update

    def start(self, handler: Generator[None, Update, None]):
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
    def __init__(self, user: "Participant.UserProfile"):
        self.user = user

    @property
    def site(self):
        return self.user.site


class UserNotFoundError(UserError):
    def __str__(self):
        return "User {} not found at {}".format(
            self.user.name, SITES[self.user.site]["name"]
        )


class MultipleUsersFoundError(UserError):
    def __str__(self):
        return "Multiple candidates found for user '{}' at {}".format(
            self.user.name, SITES[self.user.site]["name"]
        )


class SiteError(SEMarathonError):
    def __init__(self, site):
        self.site = site


class SiteNotFoundError(SiteError, LookupError):
    def __str__(self):
        return "Site '{}' not found on SE network".format(self.site)


# ------------------------------- Misc helpers  -------------------------------


@functools.lru_cache
def get_api(site: str) -> stackapi.StackAPI:
    return stackapi.StackAPI(site)
