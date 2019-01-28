import json
import threading
from time import time, sleep
from typing import List, Dict

import stackapi

with open('db/SE-Sites.json') as db:
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
        return "User {} not found at {}".format(self.user.name, self.user.site)


class MultipleUsersFoundError(UserError):
    def __str__(self):
        return "Multiple candidates found for user '{}' at {}".format(self.user.name, self.user.site)


class SiteError(LookupError):
    def __init__(self, site):
        self.site = site


class SiteNotFoundError(SiteError):
    def __str__(self):
        return "Site '{}' not found on SE network".format(self.site)


class Participant:
    name: str

    class User:
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
            self.last_checked = time()
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
    duration: int
    start_time: int
    end_time: int

    def __init__(self, *sites: str):
        self.sites = list(sites) if sites else list(DEFAULT_SITES)
        self.participants = {}
        self.duration = 12
        self.start_time = None
        self.end_time = None

    def add_site(self, site: str):
        if site not in SITES: raise SiteNotFoundError(site)
        self.sites.append(site)
        for participant in self.participants.values():
            participant.fetch_users(site)

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
        self.start_time = time()
        self.end_time = self.start_time + 3600*self.duration

        def run():
            while time() < self.end_time:
                for update in self.poll():
                    target.send(update)
                sleep(10)

        poll_thread = threading.Thread(name="MarathonPoll", target=run, daemon=True)
        poll_thread.start()
