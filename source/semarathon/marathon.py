import datetime
import json
import time
from typing import List, Dict

import stackapi

from semarathon.utils import StoppableThread

with open('data/SE-Sites.json') as db:
    SITES = json.load(db)
DEFAULT_SITES = ('stackoverflow', 'math', 'tex')
APIS = {name: stackapi.StackAPI(name) for name in DEFAULT_SITES}


def get_api(site: str):
    if site not in APIS:
        APIS[site] = stackapi.StackAPI(site)
    return APIS[site]


class UserError(LookupError):
    def __init__(self, user: 'Participant.User'):
        self.user = user

    @property
    def site(self):
        return self.user.site


class UserNotFoundError(UserError):
    def __str__(self):
        return "User {} not found at {}".format(self.user.name,
                                                SITES[self.user.site]['name'])


class MultipleUsersFoundError(UserError):
    def __str__(self):
        return "Multiple candidates found for user '{}' at {}".format(self.user.name,
                                                                      SITES[self.user.site]['name'])


class SiteError(LookupError):
    def __init__(self, site):
        self.site = site


class SiteNotFoundError(SiteError):
    def __str__(self):
        return "Site '{}' not found on SE network".format(self.site)


class Participant:
    name: str

    class User:
        link: str
        last_checked: int

        def __init__(self, site: str, username: str):
            self.site = site
            self.name = username

            results = get_api(site).fetch('users', inname=username, sort='name', order='desc',
                                          min=username, max=username)['items']
            if not results:
                raise UserNotFoundError(self)
            elif len(results) > 1:
                raise MultipleUsersFoundError(self)
            user_data = results[0]

            self.id = user_data['user_id']
            self.link = SITES[site]['site_url'] + '/users/{}/'.format(self.id)
            self.last_checked = int(time.time())
            self.score = 0

        def update(self) -> bool:
            results = get_api(self.site).fetch('users/{}/reputation'.format(self.id),
                                               fromdate=self.last_checked)
            updates = results['items']
            if updates: self.last_checked = updates[0]['on_date'] + 1
            increment = sum(u['reputation_change'] for u in updates)
            return increment


    def __init__(self, username: str):
        self.name = username
        self._users = {}

    def __str__(self):
        return self.name

    @property
    def score(self):
        return sum(u.score for u in self._users.values())

    def user(self, site: str):
        if site not in self._users:
            self._users[site] = Participant.User(site, self.name)
        return self._users[site]

    def fetch_users(self, *sites: str):
        for site in sites:
            self._users[site] = Participant.User(site, self.name)


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
    start_time: datetime.datetime
    end_time: datetime.datetime
    poll_thread: StoppableThread

    def __init__(self, *sites: str):
        self.sites = list(sites) if sites else list(DEFAULT_SITES)
        self.participants = {}
        self.duration = datetime.timedelta(hours=12)
        self.start_time = None
        self.end_time = None
        self.poll_thread = None

    @property
    def elapsed_remaining(self) -> (datetime.timedelta, datetime.timedelta):
        if not self.is_running:
            raise RuntimeError("Marathon isn't running yet")
        now = datetime.datetime.now()
        return now - self.start_time, self.end_time - now

    @property
    def refresh_interval(self) -> datetime.timedelta:
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
        return self.poll_thread is not None and not self.poll_thread.stopped

    def add_site(self, site: str):
        if site not in SITES: raise SiteNotFoundError(site)
        self.sites.append(site)
        for participant in self.participants.values():
            participant.fetch_users(site)

    def clear_sites(self):
        self.sites.clear()

    def add_participant(self, username: str):
        p = Participant(username)
        p.fetch_users(*self.sites)
        self.participants[username] = p

    def poll(self):
        for participant in self.participants.values():
            update = Update(participant)
            for site in self.sites:
                increment = participant.user(site).update()
                if increment: update[site] = increment
            if update: yield update

    def start(self, target: callable):
        self.start_time = datetime.datetime.now()
        self.end_time = self.start_time + self.duration

        def run():
            while datetime.datetime.now() < self.end_time:
                for update in self.poll():
                    target.send(update)
                time.sleep(10)

        self.poll_thread = StoppableThread(name="MarathonPoll", target=run, daemon=True)
        self.poll_thread.start()

    def destroy(self):
        if self.is_running:
            self.poll_thread.stop()
            self.poll_thread.join()
