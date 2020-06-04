import threading

import telegram.ext.filters


def debug_print(msg: str):
    print('\t'.join(('[SEMB]', msg)))


class ReplyToMessage(telegram.ext.filters.BaseFilter):
    message_id: int

    def __init__(self, message: telegram.Message):
        self.message_id = message.message_id

    def filter(self, message: telegram.Message):
        return message.reply_to_message.message_id == self.message_id


def coroutine(func: callable):
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
