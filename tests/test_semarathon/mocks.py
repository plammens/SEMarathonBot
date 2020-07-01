import time
from collections import defaultdict
from unittest import mock
from unittest.mock import MagicMock

import stackexchange


def mock_target_no_participants():
    update = yield
    assert False, f"received update {update} with no participants"


class RepDetailsMock:
    class UserMock(stackexchange.User):
        def __init__(self, json, site, **kwargs):
            super().__init__(json, site, **kwargs)

            rep_changes = self._get_rep_changes(site)

            rep_detail_mock = MagicMock()
            rep_detail_mock.__len__.side_effect = lambda: len(
                self._get_rep_changes(site)
            )
            rep_detail_mock.__iter__.side_effect = lambda: iter(
                self._get_rep_changes(site)
            )
            rep_detail_mock.__getitem__.side_effect = lambda x: self._get_rep_changes(
                site
            )[x]
            rep_detail_mock.fetch.return_value = rep_detail_mock

            self.reputation_detail = rep_detail_mock

        @staticmethod
        def _get_rep_changes(site):
            rep_change_json_mock = defaultdict(MagicMock)
            rep_change_json_mock["reputation_change"] = 10
            rep_change_json_mock["on_date"] = int(time.time())
            rep_change = stackexchange.RepChange(rep_change_json_mock, site)
            rep_changes = (rep_change,)
            return rep_changes

    URL_roots_mock = stackexchange.Site.URL_Roots
    URL_roots_mock[UserMock] = URL_roots_mock[stackexchange.User]

    def __init__(self):
        self.patch_user = mock.patch("stackexchange.site.User", new=self.UserMock)
        self.patch_url_roots = mock.patch(
            "stackexchange.site.Site.URL_Roots", new=self.URL_roots_mock
        )

    def __enter__(self):
        self.patch_url_roots.__enter__()
        return self.patch_user.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.patch_user.__exit__()
        self.patch_url_roots.__exit__()
