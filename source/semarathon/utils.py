import functools
import threading
from typing import Callable, TypeVar

import telegram
import telegram.ext.filters


def debug_print(msg: str):
    print('\t'.join(('[SEMB]', msg)))


def markdown_safe_reply(original_message: telegram.Message, reply_txt: str):
    """
    Tries to reply to ``original_message`` in Markdown; falls back to plain text
    if it can't be parsed correctly.
    """
    try:
        original_message.reply_markdown(reply_txt)
    except telegram.error.BadRequest:
        original_message.reply_text(reply_txt)


@functools.lru_cache
def load_text(name: str) -> str:
    """Find a text file and return its contents.

    Searches the `text` sub-folder first and then the root working directory for
    files with a certain name and whose extension is either ``.txt`` or ``.md``.
    This function is memoized, so loading the same text will be much faster after the
    first time.

    :param name: name of the text file (without the extension)
    :return: contents of the text file if found
    """
    # TODO: automatically select parse mode
    for prefix in ('text', '.'):
        for extension in ('md', 'txt', ''):
            try:
                path = '{}/{}.{}'.format(prefix, name, extension)
                with open(path, encoding='utf-8') as file:
                    return file.read().strip()
            except FileNotFoundError:
                continue
    raise FileNotFoundError('could not find `{}` text file'.format(name))


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

    def __init__(self, *args, **kwargs):
        super(StoppableThread, self).__init__(*args, **kwargs)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    @property
    def stopped(self):
        return self._stop_event.is_set()


def format_exception_md(exception) -> str:
    """Format a markdown string from an exception, to be sent through Telegram"""
    assert isinstance(exception, Exception)
    msg = '`{}`'.format(type(exception).__name__)
    extra = str(exception)
    if extra:
        msg += '`:` {}'.format(extra)
    return msg


_C = TypeVar('_C', bound=Callable)
Decorator = Callable[[_C], _C]
