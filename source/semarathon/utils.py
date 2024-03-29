import collections.abc
import datetime
import functools
import logging
import threading
from typing import Callable, ClassVar, TypeVar, Union

import telegram as tg
import telegram.ext.filters


def debug_print(msg: str):
    # noinspection SpellCheckingInspection
    print("\t".join(("[SEMB]", msg)))


class Text(str):
    def __new__(cls, initializer, *args, **kwargs):
        return super().__new__(cls, initializer)

    def __init__(self, source: str, parse_mode: tg.ParseMode):
        super().__init__()
        self.source = source
        self.parse_mode = parse_mode

    def __repr__(self):
        return f"Text(source={self.source}, parse_mode={self.parse_mode})"

    @classmethod
    @functools.lru_cache
    def load(cls, filename: str) -> "Text":
        """Find a text file and return its contents.

        Searches the `text` sub-folder first and then the root working directory for
        files with a certain name and whose extension is either ``.txt`` or ``.md``.
        This function is memoized, so loading the same text will be much faster after
        the first time.

        :param filename: name of the text file (without the extension)

        :return: Text object with the contents of the text file (if found), with a
            parse mode automatically deduced from the file extension.
        :raises: FileNotFoundError if the file isn't found after trying all
            combinations.
        """
        extension_to_parse_mode = {
            "md": tg.ParseMode.MARKDOWN_V2,
            "txt": None,
        }

        for prefix in ("text", "."):
            for extension, parse_mode in extension_to_parse_mode.items():
                try:
                    path = "{}/{}.{}".format(prefix, filename, extension)
                    with open(path, encoding="utf-8") as file:
                        text = file.read().strip()
                        return cls(text, parse_mode)
                except FileNotFoundError:
                    continue
        raise FileNotFoundError("could not find `{}` text file".format(filename))


class ReplyToMessage(telegram.ext.filters.BaseFilter):
    message_id: int

    def __init__(self, message: telegram.Message):
        self.message_id = message.message_id

    def filter(self, message: telegram.Message):
        return message.reply_to_message.message_id == self.message_id


def coroutine(func: Callable):
    def start(*args, **kwargs):
        coro = func(*args, **kwargs)
        coro.send(None)
        return coro

    return start


class StoppableThread(threading.Thread):
    """Thread class with a stop() method. The thread itself has to check
    regularly for the stopped() condition."""

    logger: ClassVar[logging.Logger] = logging.getLogger("threading")

    def __init__(self, *args, **kwargs):
        super(StoppableThread, self).__init__(*args, **kwargs)
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()
        self.logger.debug(f"stop_event set for thread {self}")

    @property
    def stopped(self):
        return self.stop_event.is_set()


class TimedStoppableThread(StoppableThread):
    def __init__(self, duration: Union[float, datetime.timedelta], *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(duration, datetime.timedelta):
            duration = duration.total_seconds()
        self.timer = threading.Timer(duration, self.stop)

    def run(self) -> None:
        self.timer.start()
        super().run()
        self.logger.debug(f"End of thread {self}")

    def stop(self):
        super(TimedStoppableThread, self).stop()
        self.timer.cancel()


class ReadOnlyDictView(collections.abc.Mapping):
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)


def format_exception_md(exception) -> str:
    """Format a markdown string from an exception, to be sent through Telegram"""
    assert isinstance(exception, Exception)
    msg = "`{}`".format(type(exception).__name__)
    extra = str(exception)
    if extra:
        msg += "`:` {}".format(extra)
    return msg


_C = TypeVar("_C", bound=Callable)
Decorator = Callable[[_C], _C]
